from typing import Literal
from pydantic import model_validator
from gwalerter.sema.base import SemaType
from gwalerter.sema.enums import OpsgeniePriority
from gwalerter.sema.property_format import LeftRightDot
from gwalerter.sema.property_format import NonEmptyString
from gwalerter.sema.property_format import UUID4Str


class OpsgenieAlertCreate(SemaType):
    """Sema: https://schemas.electricity.works/types/gw.opsgenie.alert.create/000"""

    alias: UUID4Str
    message: NonEmptyString
    description: NonEmptyString
    entity: NonEmptyString
    source: LeftRightDot
    priority: OpsgeniePriority
    responder_team_id: NonEmptyString
    tags: list[NonEmptyString]
    kind: NonEmptyString
    category: NonEmptyString
    subject: NonEmptyString
    house: NonEmptyString | None = None
    about: NonEmptyString | None = None
    type_name: Literal["gw.opsgenie.alert.create"] = "gw.opsgenie.alert.create"
    version: Literal["000"] = "000"

    @model_validator(mode="after")
    def check_axiom_1(self) -> "OpsgenieAlertCreate":
        """
        Axiom 1: HouseDetailsTogether
        House and About SHALL both be present or both be absent; they are present when the
        gw.alert carries an AboutGNodeAlias.
        """
        if (self.house is None) != (self.about is None):
            raise ValueError(
                "Axiom 1 (HouseDetailsTogether) failed: House and About must both be "
                "present or both be absent."
            )
        return self
