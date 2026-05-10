from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schemas.domain import CTEType


@dataclass(frozen=True, slots=True)
class IndustryAdapter:
    industry_type: str
    source_cte_type: CTEType = CTEType.HARVESTING
    source_reference_type: str = "Harvest Log"

    def source_location(self, engine: Any) -> Any:
        return engine.rng.choice(engine.farms)

    def source_quantity_range(self, product: Any) -> tuple[float, float]:
        return (120, 640)

    def source_kdes(
        self,
        *,
        engine: Any,
        lot: Any,
        location: Any,
        product: Any,
        timestamp: Any,
        next_location: str,
    ) -> dict[str, Any]:
        return {
            "harvest_date": timestamp.date().isoformat(),
            "farm_location": location.name,
            "field_name": f"Field-{engine.rng.randint(1, 18)}",
            "immediate_subsequent_recipient": next_location,
            "reference_document": engine._reference_document(
                lot.current_reference_type,
                lot.current_reference_number,
            ),
            "reference_document_type": lot.current_reference_type,
            "reference_document_number": lot.current_reference_number,
            "traceability_lot_code_source_reference": lot.tlc_source_reference,
        }

    def cooling_kdes(
        self,
        *,
        engine: Any,
        lot: Any,
        cooler: Any,
        timestamp: Any,
    ) -> dict[str, Any]:
        return {
            "cooling_date": timestamp.date().isoformat(),
            "cooling_location": cooler.name,
            "harvest_location": lot.origin_location,
            "reference_document": engine._reference_document(
                lot.current_reference_type,
                lot.current_reference_number,
            ),
            "reference_document_type": lot.current_reference_type,
            "reference_document_number": lot.current_reference_number,
            "traceability_lot_code_source_reference": lot.tlc_source_reference,
        }

    def packing_kdes(
        self,
        *,
        engine: Any,
        source_lot: Any,
        packed_lot: Any,
        packer: Any,
        timestamp: Any,
    ) -> dict[str, Any]:
        return {
            "packing_date": timestamp.date().isoformat(),
            "pack_date": timestamp.date().isoformat(),
            "packing_location": packer.name,
            "source_traceability_lot_code": source_lot.lot_code,
            "farm_location": source_lot.origin_location,
            "reference_document": engine._reference_document(
                packed_lot.current_reference_type,
                packed_lot.current_reference_number,
            ),
            "reference_document_type": packed_lot.current_reference_type,
            "reference_document_number": packed_lot.current_reference_number,
            "harvester_business_name": source_lot.origin_location,
            "traceability_lot_code_source_reference": packed_lot.tlc_source_reference,
        }

    def transformation_kdes(
        self,
        *,
        engine: Any,
        inputs: list[Any],
        outputs: list[Any],
        rework_lots: list[Any],
        processor: Any,
        timestamp: Any,
        reference_type: str,
        reference_number: str,
        total_input_qty: float,
        total_output_qty: float,
    ) -> dict[str, Any]:
        return {
            "transformation_date": timestamp.date().isoformat(),
            "transformation_location": processor.name,
            "location_name": processor.name,
            "input_traceability_lot_codes": [lot.lot_code for lot in inputs],
            "input_products": [lot.product_description for lot in inputs],
            "output_traceability_lot_codes": [lot.lot_code for lot in outputs],
            "reference_document": engine._reference_document(reference_type, reference_number),
            "reference_document_type": reference_type,
            "reference_document_number": reference_number,
            "yield_ratio": round(total_output_qty / total_input_qty, 3) if total_input_qty else 0,
            "commingled_input_lot_count": len(inputs),
            "rework_traceability_lot_codes": [lot.lot_code for lot in rework_lots],
            "batch_number": reference_number,
            "traceability_lot_code_source_reference": outputs[0].tlc_source_reference,
        }


class ProduceAdapter(IndustryAdapter):
    def __init__(self) -> None:
        super().__init__(industry_type="produce", source_reference_type="Harvest Log")

    def source_kdes(self, **kwargs: Any) -> dict[str, Any]:
        kdes = super().source_kdes(**kwargs)
        location = kwargs["location"]
        product = kwargs["product"]
        kdes["field_gps_coordinates"] = location.metadata.get("gps", kwargs["engine"]._gps_coordinate())
        kdes["plu_code"] = product.metadata.get("plu", kwargs["engine"]._plu_code())
        return kdes

    def packing_kdes(self, **kwargs: Any) -> dict[str, Any]:
        kdes = super().packing_kdes(**kwargs)
        source_lot = kwargs["source_lot"]
        clamshell_count = max(6, int(round(source_lot.quantity * kwargs["engine"].rng.uniform(2.0, 3.4))))
        master_case_count = max(1, clamshell_count // 12)
        kdes["field_gps_coordinates"] = source_lot.source_details.get("field_gps_coordinates")
        kdes["plu_code"] = source_lot.source_details.get("plu_code")
        kdes["packaging_hierarchy"] = ["bulk_bin", "individual_clamshell", "master_case"]
        kdes["packaging_conversion"] = {
            "bulk_bin_count": 1,
            "clamshell_count": clamshell_count,
            "master_case_count": master_case_count,
        }
        return kdes

    def transformation_kdes(self, **kwargs: Any) -> dict[str, Any]:
        kdes = super().transformation_kdes(**kwargs)
        kdes["packaging_hierarchy"] = ["bulk_bin", "individual_clamshell", "master_case"]
        kdes["lineage_pattern"] = "commingled_packout"
        return kdes


class SeafoodAdapter(IndustryAdapter):
    def __init__(self) -> None:
        super().__init__(
            industry_type="seafood",
            source_cte_type=CTEType.FIRST_LAND_BASED_RECEIVING,
            source_reference_type="Landing Receipt",
        )

    def source_location(self, engine: Any) -> Any:
        return engine.rng.choice(engine.coolers)

    def source_quantity_range(self, product: Any) -> tuple[float, float]:
        return (24, 140)

    def source_kdes(self, **kwargs: Any) -> dict[str, Any]:
        engine = kwargs["engine"]
        location = kwargs["location"]
        product = kwargs["product"]
        lot = kwargs["lot"]
        timestamp = kwargs["timestamp"]
        vessel_id = f"USCG-{engine.rng.randint(100000, 999999)}"
        vessel_name = engine.rng.choice(
            ["F/V Northern Star", "F/V Pacific Dawn", "F/V Glacier Point"]
        )
        return {
            "landing_date": timestamp.date().isoformat(),
            "first_land_based_receiver": location.name,
            "vessel_identifier": vessel_id,
            "vessel_name": vessel_name,
            "harvest_area": engine.rng.choice(["FAO67", "Alaska-630", "Monterey-Bay"]),
            "catch_method": engine.rng.choice(["longline", "trawl", "gillnet"]),
            "species_code": product.metadata.get("species_code", "SEAFOOD"),
            "water_temperature_c": round(engine.rng.uniform(2.1, 8.9), 1),
            "immediate_subsequent_recipient": engine.rng.choice(engine.packers).name,
            "reference_document": engine._reference_document(
                lot.current_reference_type,
                lot.current_reference_number,
            ),
            "reference_document_type": lot.current_reference_type,
            "reference_document_number": lot.current_reference_number,
            "traceability_lot_code_source_reference": lot.tlc_source_reference,
        }

    def packing_kdes(self, **kwargs: Any) -> dict[str, Any]:
        kdes = super().packing_kdes(**kwargs)
        source_lot = kwargs["source_lot"]
        kdes["landing_date"] = source_lot.source_details.get("landing_date")
        kdes["vessel_identifier"] = source_lot.source_details.get("vessel_identifier")
        kdes["vessel_name"] = source_lot.source_details.get("vessel_name")
        kdes["packaging_hierarchy"] = ["harvest_tote", "fillet_bag", "master_case"]
        return kdes

    def transformation_kdes(self, **kwargs: Any) -> dict[str, Any]:
        kdes = super().transformation_kdes(**kwargs)
        kdes["lineage_pattern"] = "landing_commingle_split"
        kdes["packaging_hierarchy"] = ["harvest_tote", "fillet_bag", "master_case"]
        return kdes


class DairyAdapter(IndustryAdapter):
    def __init__(self) -> None:
        super().__init__(industry_type="dairy", source_reference_type="Milk Collection Log")

    def source_quantity_range(self, product: Any) -> tuple[float, float]:
        return (900, 6200)

    def source_kdes(self, **kwargs: Any) -> dict[str, Any]:
        kdes = super().source_kdes(**kwargs)
        engine = kwargs["engine"]
        kdes["flow_type"] = "continuous"
        kdes["silo_identifier"] = f"SILO-{engine.rng.randint(1, 12):02d}"
        kdes["vat_identifier"] = f"VAT-{engine.rng.randint(1, 18):02d}"
        kdes["fat_test"] = kwargs["product"].metadata.get("fat_test", "3.5")
        return kdes

    def packing_kdes(self, **kwargs: Any) -> dict[str, Any]:
        kdes = super().packing_kdes(**kwargs)
        source_lot = kwargs["source_lot"]
        kdes["flow_type"] = "continuous"
        kdes["silo_identifier"] = source_lot.source_details.get("silo_identifier")
        kdes["vat_identifier"] = source_lot.source_details.get("vat_identifier")
        kdes["packaging_hierarchy"] = ["farm_silo", "processing_vat", "tote"]
        return kdes

    def transformation_kdes(self, **kwargs: Any) -> dict[str, Any]:
        kdes = super().transformation_kdes(**kwargs)
        kdes["lineage_pattern"] = "continuous_flow_rework"
        kdes["packaging_hierarchy"] = ["farm_silo", "processing_vat", "tote", "pallet"]
        return kdes


def get_industry_adapter(industry_type: str) -> IndustryAdapter:
    if industry_type == "seafood":
        return SeafoodAdapter()
    if industry_type == "dairy":
        return DairyAdapter()
    return ProduceAdapter()
