from typing import Literal
from gwalerter.sema.base import SemaType
from gwalerter.sema.property_format import LeftRightDot
from gwalerter.sema.property_format import SemaVersion
from gwalerter.sema.property_format import SpaceheatName


class StateMachine(SemaType):
    """Sema: https://schemas.electricity.works/types/gw.state.machine/000"""

    node_name: SpaceheatName
    state_enum: LeftRightDot
    state_enum_version: SemaVersion
    type_name: Literal["gw.state.machine"] = "gw.state.machine"
    version: Literal["000"] = "000"
