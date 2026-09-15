"""The registry's HTTP read: a `g.node.forest.request` for the fleet
roots, answered with a `g.node.forest`. Used at boot to seed the store's
projection; the forest broadcasts keep it current after that."""

from __future__ import annotations

import uuid

import httpx

from gwalerter.sema.codec import SemaCodec, default_codec
from gwalerter.sema.property_format import LeftRightDot
from gwalerter.sema.types import GNodeForest, GNodeForestRequest

FOREST_REQUEST_PATH = "/gnr/g-node-forest-request"


def fetch_forest(
    gnr_url: str,
    roots: list[LeftRightDot],
    *,
    codec: SemaCodec = default_codec,
    timeout_s: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> GNodeForest:
    request = GNodeForestRequest(roots=roots, request_id=str(uuid.uuid4()))
    with httpx.Client(timeout=timeout_s, transport=transport) as client:
        response = client.post(
            gnr_url.rstrip("/") + FOREST_REQUEST_PATH, json=request.to_dict()
        )
    response.raise_for_status()
    return codec.from_dict(response.json(), expect=GNodeForest)
