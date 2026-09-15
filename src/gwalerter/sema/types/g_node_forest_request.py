from typing import Literal
from gwalerter.sema.base import SemaType
from gwalerter.sema.property_format import LeftRightDot
from gwalerter.sema.property_format import UUID4Str


class GNodeForestRequest(SemaType):
    """Sema: https://schemas.electricity.works/types/g.node.forest.request/000"""

    roots: list[LeftRightDot]
    request_id: UUID4Str
    type_name: Literal["g.node.forest.request"] = "g.node.forest.request"
    version: Literal["000"] = "000"
