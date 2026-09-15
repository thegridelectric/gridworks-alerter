from typing import Literal
from pydantic import model_validator
from gwalerter.sema.base import SemaType
from gwalerter.sema.enums import HouseAlertKind
from gwalerter.sema.property_format import LeftRightDot
from gwalerter.sema.property_format import UTCMilliseconds
from gwalerter.sema.property_format import UUID4Str
from gwalerter.sema.types.channel_readings import ChannelReadings


class HouseAlertCleared(SemaType):
    """Sema: https://schemas.electricity.works/types/gw.house.alert.cleared/000"""

    alert_id: UUID4Str
    src: LeftRightDot
    about_g_node_alias: LeftRightDot
    kind: HouseAlertKind
    cleared_ms: UTCMilliseconds
    evidence: list[ChannelReadings]
    type_name: Literal["gw.house.alert.cleared"] = "gw.house.alert.cleared"
    version: Literal["000"] = "000"

    @model_validator(mode="after")
    def check_axiom_1(self) -> "HouseAlertCleared":
        """
        Axiom 1: TerminalAssetAliasConstraint
        AboutGNodeAlias SHALL identify a TerminalAsset and therefore SHALL end with the suffix
        ".ta".
        """
        if not self.about_g_node_alias.endswith(".ta"):
            raise ValueError(
                f'TerminalAssetAliasConstraint: AboutGNodeAlias ({self.about_g_node_alias}) does not end with the suffix ".ta".'
            )
        return self
