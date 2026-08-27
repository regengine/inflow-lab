from __future__ import annotations

import random
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from .industry_adapters import get_industry_adapter
from .schemas.domain import CTEType, OperationScale, RegEngineEvent
from .scenarios import (
    SCALE_QUANTITY_MULTIPLIER,
    Location,
    ProductSpec,
    ScenarioId,
    get_scenario,
    scale_scenario,
)


DEFAULT_MAX_FUTURE_HOURS = 20


@dataclass(slots=True)
class Lot:
    lot_code: str
    product_description: str
    quantity: float
    unit_of_measure: str
    current_location: str
    stage: str
    parents: list[str] = field(default_factory=list)
    origin_location: str = ""
    current_reference_type: str | None = None
    current_reference_number: str | None = None
    tlc_source_reference: str | None = None
    packaging_level: str = "bulk"
    packaging_hierarchy: tuple[str, ...] = ()
    continuous_flow: bool = False
    source_details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Shipment:
    lot: Lot
    from_location: str
    to_location: str
    reference_type: str
    reference_number: str
    next_stage: str


class LegitFlowEngine:
    """Generate realistic, linked FSMA 204 CTE flows.

    The engine creates lots and moves them through harvesting, cooling,
    initial packing, shipping, receiving, and optional transformation.
    Each event aligns with the shape shown in RegEngine's current ingest docs.
    """

    def __init__(
        self,
        seed: int | None = 204,
        scenario: ScenarioId | str = ScenarioId.LEAFY_GREENS_SUPPLIER,
        scale: OperationScale | str = OperationScale.MIDSIZE,
    ) -> None:
        self._initial_seed = seed
        self._initial_scenario = ScenarioId(scenario)
        self._initial_scale = OperationScale(scale)
        self.reset(seed, scenario=scenario, scale=scale)

    def reset(
        self,
        seed: int | None = None,
        scenario: ScenarioId | str | None = None,
        scale: OperationScale | str | None = None,
    ) -> None:
        # Deterministic simulator RNG, not security-sensitive.
        self.rng = random.Random(seed if seed is not None else self._initial_seed)  # nosec B311
        self._lot_counter = count(1)
        self._ref_counter = count(1)
        self._time_cursor = datetime.now(UTC) - timedelta(hours=12)
        self.scale = OperationScale(scale or self._initial_scale)
        self.quantity_multiplier = SCALE_QUANTITY_MULTIPLIER[self.scale]
        self.scenario = scale_scenario(get_scenario(scenario or self._initial_scenario), self.scale)
        self.scenario_id = self.scenario.id
        self.adapter = get_industry_adapter(self.scenario.industry_type)

        self.farms = list(self.scenario.farms)
        self.coolers = list(self.scenario.coolers)
        self.packers = list(self.scenario.packers)
        self.processors = list(self.scenario.processors)
        self.dcs = list(self.scenario.dcs)
        self.retailers = list(self.scenario.retailers)
        self.products = list(self.scenario.products)
        self.transformation_outputs = list(self.scenario.transformation_outputs)
        self.carriers = list(self.scenario.carriers)

        self.harvested: list[Lot] = []
        self.cooled: list[Lot] = []
        self.packed: list[Lot] = []
        self.processor_inventory: list[Lot] = []
        self.transformed: list[Lot] = []
        self.dc_inventory: list[Lot] = []
        self.retail_inventory: list[Lot] = []
        self.in_transit: list[Shipment] = []
        # Events minted alongside another action's return value but not yet
        # handed back to a caller -- currently just rework lots' own CTE
        # records (#97). next_event() drains this before choosing a new
        # action; see _transform.
        self._pending_events: list[tuple[RegEngineEvent, list[str]]] = []

        self.location_index = {loc.name: loc for loc in self.all_locations}

    @property
    def all_locations(self) -> list[Location]:
        return [*self.farms, *self.coolers, *self.packers, *self.processors, *self.dcs, *self.retailers]

    def next_event(self) -> tuple[RegEngineEvent, list[str]]:
        if self._pending_events:
            # #97: flush any CTE record queued by an earlier action (e.g. a
            # rework lot's own transformation-output event, see _transform)
            # before choosing a new one. This never touches self.rng, so it
            # cannot reorder or add an RNG draw relative to the unqueued
            # path.
            return self._pending_events.pop(0)
        action = self._choose_action()
        if action == "harvest":
            return self._harvest()
        if action == "cool":
            return self._cool()
        if action == "initial_pack":
            return self._initial_pack()
        if action == "ship":
            return self._ship()
        if action == "receive":
            return self._receive()
        if action == "transform":
            return self._transform()
        raise RuntimeError(f"Unhandled action: {action}")

    def snapshot(self) -> dict[str, int | str]:
        return {
            "scenario": self.scenario_id.value,
            "scale": self.scale.value,
            "locations": len(self.all_locations),
            "harvested": len(self.harvested),
            "cooled": len(self.cooled),
            "packed": len(self.packed),
            "processor_inventory": len(self.processor_inventory),
            "transformed": len(self.transformed),
            "dc_inventory": len(self.dc_inventory),
            "retail_inventory": len(self.retail_inventory),
            "in_transit": len(self.in_transit),
        }

    def _choose_action(self) -> str:
        weighted_actions: list[str] = []
        weights = self.scenario.action_weights
        if len(self.harvested) < self.scenario.harvest_target:
            weighted_actions.extend(["harvest"] * weights["harvest"])
        if self.scenario.requires_cooling and self.harvested:
            weighted_actions.extend(["cool"] * weights["cool"])
        if self._packing_source_pool():
            weighted_actions.extend(["initial_pack"] * weights["initial_pack"])
        if self.packed or self.transformed or self.dc_inventory:
            weighted_actions.extend(["ship"] * weights["ship"])
        if self.in_transit:
            weighted_actions.extend(["receive"] * weights["receive"])
        if len(self.processor_inventory) >= self.scenario.transform_min_lots:
            weighted_actions.extend(["transform"] * weights["transform"])
        if not weighted_actions:
            weighted_actions.append("harvest")
        return self.rng.choice(weighted_actions)

    def _harvest(self) -> tuple[RegEngineEvent, list[str]]:
        source_location = self.adapter.source_location(self)
        product: ProductSpec = self.rng.choice(self.products)
        lot_code = self._make_lot_code(prefix="TLC")
        quantity_low, quantity_high = self.adapter.source_quantity_range(product)
        quantity = self._quantity(
            quantity_low * self.quantity_multiplier,
            quantity_high * self.quantity_multiplier,
        )
        timestamp = self._advance_time(15, 90)
        # #164: source CTE type comes from the adapter, which is the single
        # owner of it. self.adapter is derived from self.scenario.industry_type
        # (see __init__), so adapter and scenario can never be paired
        # inconsistently -- which is exactly what a second copy of this value
        # on ScenarioPreset made possible.
        source_cte_type = self.adapter.source_cte_type
        reference_prefix = "LAND" if source_cte_type == CTEType.FIRST_LAND_BASED_RECEIVING else "HAR"
        reference_number = self._reference(reference_prefix)
        next_location = self._default_next_location()

        lot = Lot(
            lot_code=lot_code,
            product_description=product.name,
            quantity=quantity,
            unit_of_measure=product.unit,
            current_location=source_location.name,
            stage="harvested",
            origin_location=source_location.name,
            current_reference_type=self.adapter.source_reference_type,
            current_reference_number=reference_number,
            tlc_source_reference=self._reference("SRC"),
            continuous_flow=self.scenario.industry_type == "dairy",
        )
        lot.source_details = self.adapter.source_kdes(
            engine=self,
            lot=lot,
            location=source_location,
            product=product,
            timestamp=timestamp,
            next_location=next_location,
        )
        self.harvested.append(lot)

        event = RegEngineEvent(
            cte_type=source_cte_type,
            traceability_lot_code=lot.lot_code,
            product_description=lot.product_description,
            quantity=lot.quantity,
            unit_of_measure=lot.unit_of_measure,
            location_name=source_location.name,
            location_gln=self._location_gln_or_none(source_location.name),
            timestamp=timestamp,
            kdes=lot.source_details,
        )
        return event, []

    def _cool(self) -> tuple[RegEngineEvent, list[str]]:
        lot = self.harvested.pop(self.rng.randrange(len(self.harvested)))
        cooler = self.rng.choice(self.coolers)
        lot.stage = "cooled"
        lot.current_location = cooler.name
        lot.current_reference_type = "Cooling Log"
        lot.current_reference_number = self._reference("COOL")
        timestamp = self._advance_time(10, 60)
        self.cooled.append(lot)

        event = RegEngineEvent(
            cte_type=CTEType.COOLING,
            traceability_lot_code=lot.lot_code,
            product_description=lot.product_description,
            quantity=lot.quantity,
            unit_of_measure=lot.unit_of_measure,
            location_name=cooler.name,
            location_gln=self._location_gln_or_none(cooler.name),
            timestamp=timestamp,
            kdes=self.adapter.cooling_kdes(engine=self, lot=lot, cooler=cooler, timestamp=timestamp),
        )
        return event, [lot.lot_code]

    def _initial_pack(self) -> tuple[RegEngineEvent, list[str]]:
        source_pool = self._packing_source_pool()
        if not source_pool:
            raise RuntimeError("Initial packing requires a source lot")
        source_lot = source_pool.pop(self.rng.randrange(len(source_pool)))
        packer = self.rng.choice(self.packers)
        packed_lot_code = self._make_lot_code(prefix="TLC")
        packed_quantity = self._quantity(source_lot.quantity * 0.92, source_lot.quantity * 1.02)
        timestamp = self._advance_time(20, 120)
        reference_number = self._reference("PACK")

        packed_lot = Lot(
            lot_code=packed_lot_code,
            product_description=source_lot.product_description,
            quantity=packed_quantity,
            unit_of_measure=source_lot.unit_of_measure,
            current_location=packer.name,
            stage="packed",
            parents=[source_lot.lot_code],
            origin_location=source_lot.origin_location,
            current_reference_type="Packout Record",
            current_reference_number=reference_number,
            tlc_source_reference=self._reference("SRC"),
            packaging_level="master_case" if self.scenario.industry_type in {"produce", "seafood"} else "tote",
            packaging_hierarchy=tuple(self._default_packaging_hierarchy()),
            continuous_flow=source_lot.continuous_flow,
        )
        self.packed.append(packed_lot)

        event = RegEngineEvent(
            cte_type=CTEType.INITIAL_PACKING,
            traceability_lot_code=packed_lot.lot_code,
            product_description=packed_lot.product_description,
            quantity=packed_lot.quantity,
            unit_of_measure=packed_lot.unit_of_measure,
            location_name=packer.name,
            location_gln=self._location_gln_or_none(packer.name),
            timestamp=timestamp,
            kdes=self.adapter.packing_kdes(
                engine=self,
                source_lot=source_lot,
                packed_lot=packed_lot,
                packer=packer,
                timestamp=timestamp,
            ),
        )
        return event, [source_lot.lot_code]

    def _ship(self) -> tuple[RegEngineEvent, list[str]]:
        options: list[tuple[str, list[Lot]]] = []
        if self.packed:
            options.append(("packed", self.packed))
        if self.transformed:
            options.append(("transformed", self.transformed))
        if self.dc_inventory:
            options.append(("dc_inventory", self.dc_inventory))
        stage_name, source_pool = self.rng.choice(options)
        lot = source_pool.pop(self.rng.randrange(len(source_pool)))
        timestamp = self._advance_time(30, 180)
        reference_number = self._reference("BOL")
        carrier = self.rng.choice(self.carriers)

        if stage_name == "packed":
            if self.rng.random() < self.scenario.packed_to_processor_probability:
                destination = self.rng.choice(self.processors)
                next_stage = "processor_inventory"
            else:
                destination = self.rng.choice(self.dcs)
                next_stage = "dc_inventory"
        elif stage_name == "transformed":
            destination = self.rng.choice(self.dcs)
            next_stage = "dc_inventory"
        else:
            destination = self.rng.choice(self.retailers)
            next_stage = "retail_inventory"

        shipment = Shipment(
            lot=lot,
            from_location=lot.current_location,
            to_location=destination.name,
            reference_type="Bill of Lading",
            reference_number=reference_number,
            next_stage=next_stage,
        )
        self.in_transit.append(shipment)

        event = RegEngineEvent(
            cte_type=CTEType.SHIPPING,
            traceability_lot_code=lot.lot_code,
            product_description=lot.product_description,
            quantity=lot.quantity,
            unit_of_measure=lot.unit_of_measure,
            location_name=shipment.from_location,
            location_gln=self._location_gln_or_none(shipment.from_location),
            timestamp=timestamp,
            kdes={
                "ship_date": timestamp.date().isoformat(),
                "ship_from_location": shipment.from_location,
                "ship_to_location": shipment.to_location,
                "carrier": carrier,
                "reference_document": self._reference_document(shipment.reference_type, shipment.reference_number),
                "reference_document_type": shipment.reference_type,
                "reference_document_number": shipment.reference_number,
                "sscc": shipment.reference_number if self.scenario.reference_format == "GS1" else None,
                "tlc_source_reference": lot.tlc_source_reference,
                "traceability_lot_code_source_reference": lot.tlc_source_reference,
            },
        )
        return event, lot.parents or [lot.lot_code]

    def _receive(self) -> tuple[RegEngineEvent, list[str]]:
        shipment = self.in_transit.pop(self.rng.randrange(len(self.in_transit)))
        lot = shipment.lot
        lot.current_location = shipment.to_location
        lot.current_reference_type = shipment.reference_type
        lot.current_reference_number = shipment.reference_number
        timestamp = self._advance_time(45, 240)

        if shipment.next_stage == "processor_inventory":
            lot.stage = "processor_inventory"
            self.processor_inventory.append(lot)
        elif shipment.next_stage == "dc_inventory":
            lot.stage = "dc_inventory"
            self.dc_inventory.append(lot)
        else:
            lot.stage = "retail_inventory"
            self.retail_inventory.append(lot)

        event = RegEngineEvent(
            cte_type=CTEType.RECEIVING,
            traceability_lot_code=lot.lot_code,
            product_description=lot.product_description,
            quantity=lot.quantity,
            unit_of_measure=lot.unit_of_measure,
            location_name=shipment.to_location,
            location_gln=self._location_gln_or_none(shipment.to_location),
            timestamp=timestamp,
            kdes={
                "receive_date": timestamp.date().isoformat(),
                "receiving_location": shipment.to_location,
                "ship_from_location": shipment.from_location,
                "immediate_previous_source": shipment.from_location,
                "reference_document": self._reference_document(shipment.reference_type, shipment.reference_number),
                "reference_document_type": shipment.reference_type,
                "reference_document_number": shipment.reference_number,
                "sscc": shipment.reference_number if self.scenario.reference_format == "GS1" else None,
                "tlc_source_reference": lot.tlc_source_reference,
                "traceability_lot_code_source_reference": lot.tlc_source_reference,
            },
        )
        return event, lot.parents or [lot.lot_code]

    def _transform(self) -> tuple[RegEngineEvent, list[str]]:
        sample_size = min(len(self.processor_inventory), self.rng.choice(self.scenario.transform_input_choices))
        inputs = self.rng.sample(self.processor_inventory, k=sample_size)
        self.processor_inventory = [lot for lot in self.processor_inventory if lot not in inputs]

        processor = self.rng.choice(self.processors)
        output_description = self.rng.choice(self.transformation_outputs)
        total_input_qty = sum(lot.quantity for lot in inputs)
        gross_output_qty = self._quantity(total_input_qty * 0.78, total_input_qty * 0.91)
        timestamp = self._advance_time(35, 150)
        reference_number = self._reference("BATCH")
        output_count = self._transform_output_count()
        rework_qty = 0.0
        rework_lots: list[Lot] = []
        if self._should_create_rework(inputs):
            rework_qty = round(gross_output_qty * self.rng.uniform(0.06, 0.14), 2)
        sellable_qty = max(gross_output_qty - rework_qty, 1.0)
        output_quantities = self._split_quantity(sellable_qty, output_count)

        outputs: list[Lot] = []
        for index, output_qty in enumerate(output_quantities, start=1):
            lot_code = self._make_lot_code(prefix="TLC")
            output_lot = Lot(
                lot_code=lot_code,
                product_description=output_description if output_count == 1 else f"{output_description} #{index}",
                quantity=output_qty,
                unit_of_measure=self._transform_output_unit(inputs),
                current_location=processor.name,
                stage="transformed",
                parents=[lot.lot_code for lot in inputs],
                origin_location=processor.name,
                current_reference_type="Batch Record",
                current_reference_number=reference_number,
                tlc_source_reference=self._reference("SRC"),
                packaging_level="finished_good",
                packaging_hierarchy=tuple(self._default_packaging_hierarchy()),
            )
            outputs.append(output_lot)
            self.transformed.append(output_lot)

        input_lot_codes = [lot.lot_code for lot in inputs]

        if rework_qty > 0:
            rework_lot = Lot(
                lot_code=self._make_lot_code(prefix="TLC"),
                product_description=f"Rework of {output_description}",
                quantity=rework_qty,
                unit_of_measure=self._transform_output_unit(inputs),
                current_location=processor.name,
                stage="processor_inventory",
                parents=[outputs[0].lot_code],
                origin_location=processor.name,
                current_reference_type="Rework Hold Tag",
                current_reference_number=self._reference("REWORK"),
                tlc_source_reference=self._reference("SRC"),
                packaging_level="rework",
            )
            rework_lots.append(rework_lot)
            self.processor_inventory.append(rework_lot)

        kdes = self.adapter.transformation_kdes(
            engine=self,
            inputs=inputs,
            outputs=outputs,
            rework_lots=rework_lots,
            processor=processor,
            timestamp=timestamp,
            reference_type=outputs[0].current_reference_type or "Batch Record",
            reference_number=reference_number,
            total_input_qty=total_input_qty,
            total_output_qty=gross_output_qty,
        )

        event = RegEngineEvent(
            cte_type=CTEType.TRANSFORMATION,
            traceability_lot_code=outputs[0].lot_code,
            product_description=outputs[0].product_description,
            quantity=outputs[0].quantity,
            unit_of_measure=outputs[0].unit_of_measure,
            location_name=processor.name,
            location_gln=self._location_gln_or_none(processor.name),
            timestamp=timestamp,
            # #91: RegEngine's live ingest reads transformation input-lot
            # linkage only from this top-level field, never from
            # kdes["input_traceability_lot_codes"] (which stays populated
            # below for local lineage/exports/mock validation). Sourced
            # straight from `inputs` rather than read back out of `kdes`,
            # so it can never drift from what this transformation actually
            # consumed even if an industry adapter's kdes shape changes.
            input_traceability_lot_codes=input_lot_codes,
            kdes=kdes,
        )

        for output_lot in outputs[1:]:
            # #115: _transform can mint 2-3 output lots
            # (_transform_output_count), appends every one of them to
            # self.transformed, and used to build exactly ONE CTE record --
            # keyed to outputs[0]. The other output lots were named only
            # inside this event's kdes["output_traceability_lot_codes"]
            # array, never as the traceability_lot_code of a record of their
            # own. EventStore.lineage() links a record to its parents via
            # parent_lot_codes, source_traceability_lot_code and
            # input_traceability_lot_codes -- never via
            # output_traceability_lot_codes -- so lineage() for outputs[1]
            # or outputs[2] returned nothing at all until that lot shipped,
            # and _ship() then set the shipment's parent_lot_codes to the
            # *pre-transformation* inputs (lot.parents). The first stored
            # record for that lot code was therefore a SHIPPING event
            # appearing to link straight back to the input lots, skipping
            # the transformation that created it -- wrong in store.lineage()
            # and in every per-lot export built on it.
            #
            # Emitted through the same _pending_events queue #97 added for
            # rework lots, and deliberately so: queuing rather than
            # returning a second event keeps next_event()'s
            # (event, parent_lot_codes) return type intact, costs no
            # self.rng draw, and so cannot reorder or shift any later seeded
            # value. Same non-collapsing timestamp idiom too (#119).
            output_timestamp = self._time_cursor + timedelta(microseconds=1)
            self._time_cursor = output_timestamp
            # Batch-level KDEs are shared with outputs[0] -- same inputs,
            # same batch record, same yield -- and are correct for every
            # output of this transformation. Only the two source-reference
            # KDEs are per-lot: transformation_kdes keys them to outputs[0].
            output_kdes = dict(kdes)
            output_kdes["tlc_source_reference"] = output_lot.tlc_source_reference
            output_kdes["traceability_lot_code_source_reference"] = output_lot.tlc_source_reference
            output_event = RegEngineEvent(
                cte_type=CTEType.TRANSFORMATION,
                traceability_lot_code=output_lot.lot_code,
                product_description=output_lot.product_description,
                quantity=output_lot.quantity,
                unit_of_measure=output_lot.unit_of_measure,
                location_name=processor.name,
                location_gln=self._location_gln_or_none(processor.name),
                timestamp=output_timestamp,
                input_traceability_lot_codes=input_lot_codes,
                kdes=output_kdes,
            )
            self._pending_events.append((output_event, input_lot_codes))

        for rework_lot in rework_lots:
            # #97: a rework lot enters processor_inventory and can later be
            # sampled as another transformation's input (see the `inputs`
            # sampling at the top of this method), where it appears in
            # parent_lot_codes and input_traceability_lot_codes -- but
            # without a CTE record of its own, EventStore.lineage_edges
            # (app/store.py) silently drops that edge, since it only keeps
            # edges whose source lot code has a record of its own. Emit the
            # rework lot's own record here, at creation time: another
            # transformation-output event for the same batch/inputs/
            # location/reference, differing only in the identity fields
            # (lot code, description, quantity, its own reference/source
            # tags) that distinguish it from `outputs[0]`.
            #
            # Queued rather than returned as a second event from this call
            # so next_event()'s (event, parent_lot_codes) return type never
            # changes, and drained (see next_event()) before any new action
            # is chosen -- so it costs no extra `self.rng` draw and cannot
            # reorder or shift any later seeded value. The timestamp is
            # nudged forward by the smallest representable step, the same
            # non-collapsing idiom `_advance_time` uses at the live-window
            # ceiling (#119), rather than reusing `timestamp` outright,
            # since two events sharing one timestamp is exactly what that
            # fix exists to prevent.
            rework_timestamp = self._time_cursor + timedelta(microseconds=1)
            self._time_cursor = rework_timestamp
            rework_kdes = dict(kdes)
            rework_kdes["tlc_source_reference"] = rework_lot.tlc_source_reference
            rework_kdes["traceability_lot_code_source_reference"] = rework_lot.tlc_source_reference
            rework_kdes["reference_document"] = self._reference_document(
                rework_lot.current_reference_type,
                rework_lot.current_reference_number,
            )
            rework_kdes["reference_document_type"] = rework_lot.current_reference_type
            rework_kdes["reference_document_number"] = rework_lot.current_reference_number
            rework_event = RegEngineEvent(
                cte_type=CTEType.TRANSFORMATION,
                traceability_lot_code=rework_lot.lot_code,
                product_description=rework_lot.product_description,
                quantity=rework_lot.quantity,
                unit_of_measure=rework_lot.unit_of_measure,
                location_name=processor.name,
                location_gln=self._location_gln_or_none(processor.name),
                timestamp=rework_timestamp,
                input_traceability_lot_codes=input_lot_codes,
                kdes=rework_kdes,
            )
            self._pending_events.append((rework_event, input_lot_codes))

        return event, input_lot_codes

    def location_gln(self, location_name: str) -> str:
        location = self.location_index.get(location_name)
        return location.gln if location else ""

    def _location_gln_or_none(self, location_name: str) -> str | None:
        gln = self.location_gln(location_name)
        return gln or None

    def _make_lot_code(self, prefix: str) -> str:
        if self.scenario.reference_format == "GS1":
            return self._make_sgtin()
        return f"{prefix}-{self._time_cursor.strftime('%Y%m%d')}-{next(self._lot_counter):06d}"

    def _reference(self, prefix: str) -> str:
        if self.scenario.reference_format == "GS1" and prefix in {"BOL", "LAND"}:
            return self._make_sscc()
        return f"{prefix}-{self._time_cursor.strftime('%Y%m%d')}-{next(self._ref_counter):05d}"

    def _reference_document(self, reference_type: str | None, reference_number: str | None) -> str:
        if reference_type and reference_number:
            if self.scenario.reference_format == "GS1":
                if reference_type in {"Bill of Lading", "Landing Receipt"}:
                    return f"GS1-128 (00){reference_number}"
                if "Batch" in reference_type:
                    return f"GS1-128 (10){reference_number}"
                return f"GS1 {reference_type} {reference_number}"
            return f"{reference_type} {reference_number}"
        return reference_number or reference_type or ""

    def _advance_time(self, min_minutes: int, max_minutes: int) -> datetime:
        candidate = self._time_cursor + timedelta(minutes=self.rng.randint(min_minutes, max_minutes))
        live_window_ceiling = datetime.now(UTC) + timedelta(hours=_max_future_hours())
        if candidate > live_window_ceiling:
            # Once the simulated clock has caught up to the live cap,
            # clamping straight to `live_window_ceiling` would repeat that
            # same instant for every event generated within one wall-clock
            # tick. Ride the ceiling forward as it creeps ahead of real
            # time, and when it hasn't moved yet, nudge the cursor by the
            # smallest representable step so ordering never collapses.
            candidate = live_window_ceiling if live_window_ceiling > self._time_cursor else self._time_cursor + timedelta(microseconds=1)
        self._time_cursor = candidate
        return self._time_cursor

    def _quantity(self, low: float, high: float) -> float:
        return round(self.rng.uniform(float(low), float(high)), 2)

    def _packing_source_pool(self) -> list[Lot]:
        if self.scenario.requires_cooling:
            return self.cooled
        return self.harvested

    def _default_next_location(self) -> str:
        if self.scenario.requires_cooling and self.coolers:
            return self.rng.choice(self.coolers).name
        if self.packers:
            return self.rng.choice(self.packers).name
        if self.processors:
            return self.rng.choice(self.processors).name
        return ""

    def _default_packaging_hierarchy(self) -> list[str]:
        if self.scenario.industry_type == "seafood":
            return ["harvest_tote", "fillet_bag", "master_case"]
        if self.scenario.industry_type == "dairy":
            return ["farm_silo", "processing_vat", "tote", "pallet"]
        return ["bulk_bin", "individual_clamshell", "master_case"]

    def _transform_output_count(self) -> int:
        if self.scenario.industry_type in {"produce", "seafood"}:
            return self.rng.choice((1, 2, 3))
        return self.rng.choice((1, 2))

    def _transform_output_unit(self, inputs: list[Lot]) -> str:
        if self.scenario.industry_type == "produce":
            return "cases"
        return inputs[0].unit_of_measure if inputs else "cases"

    def _should_create_rework(self, inputs: list[Lot]) -> bool:
        return len(inputs) > 1 and self.rng.random() < 0.45

    def _split_quantity(self, total: float, count_: int) -> list[float]:
        if count_ <= 1:
            return [round(total, 2)]
        weights = [self.rng.uniform(0.8, 1.4) for _ in range(count_)]
        weight_total = sum(weights) or 1.0
        values = [round(total * weight / weight_total, 2) for weight in weights]
        delta = round(total - sum(values), 2)
        values[0] = round(values[0] + delta, 2)
        return values

    def _make_sgtin(self) -> str:
        serial = next(self._lot_counter)
        company_prefix = "8500000"
        item_reference = f"{10000 + (serial % 89999):05d}"
        serial_component = f"{self._time_cursor.strftime('%y%m%d')}{serial:06d}"
        return f"urn:epc:id:sgtin:{company_prefix}.{item_reference}.{serial_component}"

    def _make_sscc(self) -> str:
        company_prefix = "8500000"
        serial = next(self._ref_counter)
        # SSCC payload is extension digit (1) + company prefix (7) +
        # day-of-year (3) + serial reference, summing to exactly 17 digits
        # ahead of the check digit. Wrap the serial into the 6 digits that
        # leaves rather than building an 18-character string and slicing it
        # — slicing silently dropped the serial's last digit and made
        # distinct lots collide on the same SSCC.
        serial_component = f"{serial % 1_000_000:06d}"
        base = f"0{company_prefix}{self._time_cursor.strftime('%j')}{serial_component}"
        return f"{base}{self._gs1_check_digit(base)}"

    def _gs1_check_digit(self, digits: str) -> int:
        total = 0
        for index, digit in enumerate(reversed(digits), start=1):
            total += int(digit) * (3 if index % 2 else 1)
        return (10 - (total % 10)) % 10

    def _gps_coordinate(self) -> str:
        lat = round(self.rng.uniform(32.0, 39.5), 4)
        lon = round(self.rng.uniform(-122.5, -114.0), 4)
        return f"{lat},{lon}"

    def _plu_code(self) -> str:
        return f"{self.rng.randint(3000, 4999)}"


def _max_future_hours() -> int:
    try:
        return int(os.getenv("REGENGINE_SIM_MAX_FUTURE_HOURS", str(DEFAULT_MAX_FUTURE_HOURS)))
    except ValueError:
        return DEFAULT_MAX_FUTURE_HOURS
