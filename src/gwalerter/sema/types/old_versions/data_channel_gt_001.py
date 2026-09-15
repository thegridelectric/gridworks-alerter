from typing import Literal
from pydantic import model_validator
from gwalerter.sema.base import SemaType
from gwalerter.sema.enums import SpaceheatTelemetryName
from gwalerter.sema.enums.old_versions.spaceheat_telemetry_name_006 import (
    SpaceheatTelemetryName006,
)
from gwalerter.sema.property_format import LeftRightDot
from gwalerter.sema.property_format import SpaceheatName
from gwalerter.sema.property_format import UTCSeconds
from gwalerter.sema.property_format import UUID4Str
from gwalerter.sema.types.data_channel_gt import DataChannelGt
from gwalerter.sema.types.spaceheat_telemetry_quantity_projection import (
    SpaceheatTelemetryQuantityProjection,
)


class DataChannelGt001(SemaType):
    """Sema: https://schemas.electricity.works/types/data.channel.gt/001"""

    name: SpaceheatName
    display_name: str
    about_node_name: SpaceheatName
    captured_by_node_name: SpaceheatName
    telemetry_name: SpaceheatTelemetryName006
    terminal_asset_alias: LeftRightDot
    in_power_metering: bool | None = None
    start_s: UTCSeconds | None = None
    id: UUID4Str
    type_name: Literal["data.channel.gt"] = "data.channel.gt"
    version: Literal["001"] = "001"

    @model_validator(mode="after")
    def check_axiom_1(self) -> "DataChannelGt001":
        """
        Axiom 1: PowerMeteringConstraint
        If InPowerMetering is true, TelemetryName SHALL equal PowerW.
        """
        if (
            self.in_power_metering
            and self.telemetry_name != SpaceheatTelemetryName006.PowerW
        ):
            raise ValueError(
                "Axiom 1 failed: telemetry_name must be PowerW when in_power_metering is true."
            )
        return self

    def upgrade(self) -> DataChannelGt:
        """
        - Quantity: add
        - TelemetryName: spaceheat.telemetry.name:006 -> 007
        - TelemetryQuantityConsistency axiom: add
        """

        data = self.model_dump()
        upgraded_telemetry_name = SpaceheatTelemetryName[self.telemetry_name.name]
        data["telemetry_name"] = upgraded_telemetry_name
        data["quantity"] = SpaceheatTelemetryQuantityProjection.project(
            upgraded_telemetry_name
        )
        data["version"] = "002"
        return DataChannelGt.model_validate(data)
