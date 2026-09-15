"""The boot forest request: a sema `g.node.forest.request` posted to the
registry's read path, its `g.node.forest` answer decoded through the codec."""

from __future__ import annotations

import httpx

from gwalerter.registry_read import FOREST_REQUEST_PATH, fetch_forest
from tests.conftest import FLEET_ROOT, GNR_URL, forest, house_nodes


def test_fetch_forest_posts_a_request_and_decodes_the_forest() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=forest(
                house_nodes(f"{FLEET_ROOT}.spruce"), [FLEET_ROOT], 1_800_000_000_000
            ),
        )

    got = fetch_forest(GNR_URL, [FLEET_ROOT], transport=httpx.MockTransport(handler))
    (request,) = seen
    assert request.url == GNR_URL + FOREST_REQUEST_PATH
    body = request.read().decode()
    assert '"TypeName":"g.node.forest.request"' in body.replace(" ", "")
    assert [n.alias for n in got.nodes] == [
        f"{FLEET_ROOT}.spruce",
        f"{FLEET_ROOT}.spruce.ta",
        f"{FLEET_ROOT}.spruce.scada",
    ]
