from typing import Literal
from pydantic import model_validator
from gwalerter.sema.base import SemaType
from gwalerter.sema.enums import OpsgenieAlertStatus
from gwalerter.sema.enums import OpsgeniePriority
from gwalerter.sema.property_format import NonEmptyString
from gwalerter.sema.property_format import PositiveInt
from gwalerter.sema.property_format import UTCMilliseconds
from gwalerter.sema.property_format import UUID4Str


class OpsgenieAlert(SemaType):
    """Sema: https://schemas.electricity.works/types/gw.opsgenie.alert/000"""

    alias: UUID4Str
    opsgenie_id: NonEmptyString
    tiny_id: NonEmptyString | None = None
    message: NonEmptyString
    status: OpsgenieAlertStatus
    acknowledged: bool
    count: PositiveInt
    priority: OpsgeniePriority
    source: NonEmptyString
    tags: list[NonEmptyString]
    created_ms: UTCMilliseconds
    last_occurred_ms: UTCMilliseconds
    acknowledged_by: NonEmptyString | None = None
    closed_by: NonEmptyString | None = None
    type_name: Literal["gw.opsgenie.alert"] = "gw.opsgenie.alert"
    version: Literal["000"] = "000"

    @model_validator(mode="after")
    def check_axiom_1(self) -> "OpsgenieAlert":
        """
        Axiom 1: ClosedByOnClosed
        a. If Status is Closed, ClosedBy SHALL be present. b. If Status is Open, ClosedBy
        SHALL be absent.
        """
        closed = self.status is OpsgenieAlertStatus.Closed
        if closed and self.closed_by is None:
            raise ValueError(
                "Axiom 1 (ClosedByOnClosed) failed: ClosedBy must be present when "
                "Status is Closed."
            )
        if not closed and self.closed_by is not None:
            raise ValueError(
                "Axiom 1 (ClosedByOnClosed) failed: ClosedBy must be absent when "
                "Status is Open."
            )
        return self
