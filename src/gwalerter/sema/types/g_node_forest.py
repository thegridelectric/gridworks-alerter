from typing import Literal
from gwalerter.sema.base import SemaType
from gwalerter.sema.property_format import LeftRightDot
from gwalerter.sema.property_format import UTCMilliseconds
from gwalerter.sema.types.connectivity_edge_gt import ConnectivityEdgeGt
from gwalerter.sema.types.g_node_gt import GNodeGt


class GNodeForest(SemaType):
    """Sema: https://schemas.electricity.works/types/g.node.forest/002"""

    roots: list[LeftRightDot]
    nodes: list[GNodeGt]
    edges: list[ConnectivityEdgeGt]
    send_time_ms: UTCMilliseconds
    proof: str | None = None
    type_name: Literal["g.node.forest"] = "g.node.forest"
    version: Literal["002"] = "002"
