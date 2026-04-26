from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count

from .models import CTEType, RegEngineEvent
from .scenarios import Location, ProductSpec, ScenarioId, get_scenario


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
    ) -> None:
        self._initial_seed = seed
        self._initial_scenario = ScenarioId(scenario)
        self.reset(seed, scenario=scenario)

    def reset(self, seed: int | None = None, scenario: ScenarioId | str | None = None) -> None:
        self.rng = random.Random(seed if seed is not None else self._initial_seed)
        self._lot_counter = count(1)
        self._ref_counter = count(1)
        self._time_cursor = datetime.now(UTC) - timedelta(hours=12)
        self.scenario = get_scenario(scenario or self._initial_scenario)
        self.scenario_id = self.scenario.id

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

        self.location_index = {loc.name: loc for loc in self.all_locations}

    @property
    def all_locations(self) -> list[Location]:
        return [*self.farms, *self.coolers, *self.packers, *self.processors, *self.dcs, *self.retailers]

    def next_event(self) -> tuple[RegEngineEvent, list[str]]:
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
        if self.harvested:
            weighted_actions.extend(["cool"] * weights["cool"])
        if self.cooled:
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
        farm = self.rng.choice(self.farms)
        product: ProductSpec = self.rng.choice(self.products)
        lot_code = self._make_lot_code(prefix="TLC")
        quantity = self._quantity(120, 640)
        timestamp = self._advance_time(15, 90)
        reference_number = self._reference("HAR")

        lot = Lot(
            lot_code=lot_code,
            product_description=product.name,
            quantity=quantity,
            unit_of_measure=product.unit,
            current_location=farm.name,
            stage="harvested",
            origin_location=farm.name,
            current_reference_type="Harvest Log",
            current_reference_number=reference_number,
            tlc_source_reference=self._reference("SRC"),
        )
        self.harvested.append(lot)

        event = RegEngineEvent(
            cte_type=CTEType.HARVESTING,
            traceability_lot_code=lot.lot_code,
            product_description=lot.product_description,
            quantity=lot.quantity,
            unit_of_measure=lot.unit_of_measure,
            location_name=farm.name,
            timestamp=timestamp,
            kdes={
                "harvest_date": timestamp.date().isoformat(),
                "farm_location": farm.name,
                "field_name": f"Field-{self.rng.randint(1, 18)}",
                "immediate_subsequent_recipient": self.rng.choice(self.coolers + self.packers).name,
                "reference_document_type": lot.current_reference_type,
                "reference_document_number": reference_number,
                "traceability_lot_code_source_reference": lot.tlc_source_reference,
            },
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
            timestamp=timestamp,
            kdes={
                "cooling_date": timestamp.date().isoformat(),
                "cooling_location": cooler.name,
                "harvest_location": lot.origin_location,
                "reference_document_type": lot.current_reference_type,
                "reference_document_number": lot.current_reference_number,
                "traceability_lot_code_source_reference": lot.tlc_source_reference,
            },
        )
        return event, [lot.lot_code]

    def _initial_pack(self) -> tuple[RegEngineEvent, list[str]]:
        if not self.cooled:
            raise RuntimeError("Initial packing requires a cooled source lot")
        source_lot = self.cooled.pop(self.rng.randrange(len(self.cooled)))
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
        )
        self.packed.append(packed_lot)

        event = RegEngineEvent(
            cte_type=CTEType.INITIAL_PACKING,
            traceability_lot_code=packed_lot.lot_code,
            product_description=packed_lot.product_description,
            quantity=packed_lot.quantity,
            unit_of_measure=packed_lot.unit_of_measure,
            location_name=packer.name,
            timestamp=timestamp,
            kdes={
                "pack_date": timestamp.date().isoformat(),
                "packing_location": packer.name,
                "source_traceability_lot_code": source_lot.lot_code,
                "farm_location": source_lot.origin_location,
                "reference_document_type": packed_lot.current_reference_type,
                "reference_document_number": reference_number,
                "traceability_lot_code_source_reference": packed_lot.tlc_source_reference,
            },
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
            timestamp=timestamp,
            kdes={
                "ship_date": timestamp.date().isoformat(),
                "ship_from_location": shipment.from_location,
                "ship_to_location": shipment.to_location,
                "carrier": carrier,
                "reference_document_type": shipment.reference_type,
                "reference_document_number": shipment.reference_number,
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
            timestamp=timestamp,
            kdes={
                "receive_date": timestamp.date().isoformat(),
                "receiving_location": shipment.to_location,
                "ship_from_location": shipment.from_location,
                "reference_document_type": shipment.reference_type,
                "reference_document_number": shipment.reference_number,
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
        output_qty = self._quantity(total_input_qty * 0.78, total_input_qty * 0.91)
        timestamp = self._advance_time(35, 150)
        reference_number = self._reference("BATCH")
        output_lot_code = self._make_lot_code(prefix="TLC")

        output_lot = Lot(
            lot_code=output_lot_code,
            product_description=output_description,
            quantity=output_qty,
            unit_of_measure="cases",
            current_location=processor.name,
            stage="transformed",
            parents=[lot.lot_code for lot in inputs],
            origin_location=processor.name,
            current_reference_type="Batch Record",
            current_reference_number=reference_number,
            tlc_source_reference=self._reference("SRC"),
        )
        self.transformed.append(output_lot)

        event = RegEngineEvent(
            cte_type=CTEType.TRANSFORMATION,
            traceability_lot_code=output_lot.lot_code,
            product_description=output_lot.product_description,
            quantity=output_lot.quantity,
            unit_of_measure=output_lot.unit_of_measure,
            location_name=processor.name,
            timestamp=timestamp,
            kdes={
                "transformation_date": timestamp.date().isoformat(),
                "transformation_location": processor.name,
                "input_traceability_lot_codes": [lot.lot_code for lot in inputs],
                "input_products": [lot.product_description for lot in inputs],
                "reference_document_type": output_lot.current_reference_type,
                "reference_document_number": output_lot.current_reference_number,
                "yield_ratio": round(output_qty / total_input_qty, 3) if total_input_qty else 0,
                "traceability_lot_code_source_reference": output_lot.tlc_source_reference,
            },
        )
        return event, [lot.lot_code for lot in inputs]

    def location_gln(self, location_name: str) -> str:
        location = self.location_index.get(location_name)
        return location.gln if location else ""

    def _make_lot_code(self, prefix: str) -> str:
        return f"{prefix}-{self._time_cursor.strftime('%Y%m%d')}-{next(self._lot_counter):06d}"

    def _reference(self, prefix: str) -> str:
        return f"{prefix}-{self._time_cursor.strftime('%Y%m%d')}-{next(self._ref_counter):05d}"

    def _advance_time(self, min_minutes: int, max_minutes: int) -> datetime:
        self._time_cursor += timedelta(minutes=self.rng.randint(min_minutes, max_minutes))
        return self._time_cursor

    def _quantity(self, low: float, high: float) -> float:
        return round(self.rng.uniform(float(low), float(high)), 2)
