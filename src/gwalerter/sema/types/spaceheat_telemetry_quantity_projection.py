from typing import Literal
from pydantic import model_validator
from gwalerter.sema.base import SemaType
from gwalerter.sema.enums import Quantity
from gwalerter.sema.enums import SpaceheatTelemetryName


_PROJECTION = {
    SpaceheatTelemetryName.Unknown: Quantity.Unknown,
    SpaceheatTelemetryName.PowerW: Quantity.Power,
    SpaceheatTelemetryName.WattHours: Quantity.Energy,
    SpaceheatTelemetryName.MilliWattHours: Quantity.Energy,
    SpaceheatTelemetryName.WaterTempCTimes1000: Quantity.Temperature,
    SpaceheatTelemetryName.WaterTempFTimes1000: Quantity.Temperature,
    SpaceheatTelemetryName.AirTempCTimes1000: Quantity.Temperature,
    SpaceheatTelemetryName.AirTempFTimes1000: Quantity.Temperature,
    SpaceheatTelemetryName.CelsiusTimes100: Quantity.Temperature,
    SpaceheatTelemetryName.GpmTimes100: Quantity.FlowRate,
    SpaceheatTelemetryName.GallonsTimes100: Quantity.Volume,
    SpaceheatTelemetryName.VoltageRmsMilliVolts: Quantity.Voltage,
    SpaceheatTelemetryName.VoltsTimesTen: Quantity.Voltage,
    SpaceheatTelemetryName.VoltsTimes100: Quantity.Voltage,
    SpaceheatTelemetryName.MicroVolts: Quantity.Voltage,
    SpaceheatTelemetryName.CurrentRmsMicroAmps: Quantity.Current,
    SpaceheatTelemetryName.HzTimes100: Quantity.Frequency,
    SpaceheatTelemetryName.MicroHz: Quantity.Frequency,
    SpaceheatTelemetryName.RelayState: Quantity.Unitless,
    SpaceheatTelemetryName.ThermostatState: Quantity.Unitless,
    SpaceheatTelemetryName.StorageLayer: Quantity.Unitless,
    SpaceheatTelemetryName.BinaryState: Quantity.Unitless,
    SpaceheatTelemetryName.PercentKeep: Quantity.Percent,
}


class SpaceheatTelemetryQuantityProjection(SemaType):
    """Sema: https://schemas.electricity.works/types/spaceheat.telemetry.quantity.projection/000"""

    telemetry_name: SpaceheatTelemetryName
    quantity: Quantity
    type_name: Literal["spaceheat.telemetry.quantity.projection"] = (
        "spaceheat.telemetry.quantity.projection"
    )
    version: Literal["000"] = "000"

    @classmethod
    def project(cls, telemetry_name: SpaceheatTelemetryName) -> Quantity:
        expected = _PROJECTION.get(telemetry_name)
        if expected is None:
            raise ValueError(
                f"No projection defined for telemetry_name {telemetry_name!r}."
            )
        return expected

    @model_validator(mode="after")
    def check_axiom_1(self) -> "SpaceheatTelemetryQuantityProjection":
        """
        Axiom 1: EnumeratedProjectionMapping
        Every (TelemetryName, Quantity) pair SHALL match the mapping declared in
        x-gridworks.projection.table. Any other combination is invalid.
        """
        expected = self.project(self.telemetry_name)
        if expected != self.quantity:
            raise ValueError(
                "Axiom 1 failed: telemetry_name and quantity do not match the enumerated projection."
            )
        return self
