from typing import Literal
from gwalerter.sema.base import SemaType
from gwalerter.sema.property_format import LeftRightDot
from gwalerter.sema.property_format import NonEmptyString
from gwalerter.sema.property_format import UUID4Str


class OpsgenieAlertClose(SemaType):
    """Sema: https://schemas.electricity.works/types/gw.opsgenie.alert.close/000"""

    alias: UUID4Str
    source: LeftRightDot
    note: NonEmptyString
    type_name: Literal["gw.opsgenie.alert.close"] = "gw.opsgenie.alert.close"
    version: Literal["000"] = "000"
