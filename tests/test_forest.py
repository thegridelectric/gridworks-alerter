"""The registry projection: forests upsert by id, an older forest never
overwrites a newer one, and tracked houses are the Pending or Active
terminal assets under the fleet roots."""

from __future__ import annotations

from gwalerter.sema.codec import default_codec
from gwalerter.sema.types import GNodeForest
from gwalerter.store import Store
from tests.conftest import FLEET_ROOT, forest, g_node, house_nodes

SPRUCE = f"{FLEET_ROOT}.spruce"
BEECH = f"{FLEET_ROOT}.beech"
ELM = f"{FLEET_ROOT}.elm"
HONEYSUCKLE = "d1.isone.me.versant.bench.honeysuckle"


def load(
    store: Store, nodes: list[dict], send_time_ms: int = 1_800_000_000_000
) -> None:
    payload = forest(nodes, [FLEET_ROOT], send_time_ms)
    store.upsert_forest(default_codec.from_dict(payload, expect=GNodeForest))


def test_tracked_houses_are_pending_or_active_terminal_assets_under_the_roots(
    store: Store,
) -> None:
    load(
        store,
        house_nodes(SPRUCE)
        + house_nodes(BEECH, status="Pending")
        + house_nodes(ELM, status="Suspended")
        + house_nodes(HONEYSUCKLE),
    )
    assert [h.alias for h in store.tracked_houses()] == [
        f"{BEECH}.ta",
        f"{SPRUCE}.ta",
    ]
    assert store.tracked_house_of(f"{BEECH}.scada") is not None
    assert store.tracked_house_of(f"{ELM}.scada") is None


def test_scada_resolves_to_its_tracked_house(store: Store) -> None:
    load(store, house_nodes(SPRUCE) + house_nodes(HONEYSUCKLE))
    house = store.tracked_house_of(f"{SPRUCE}.scada")
    assert house is not None and house.alias == f"{SPRUCE}.ta"
    assert store.tracked_house_of(f"{HONEYSUCKLE}.scada") is None
    assert store.tracked_house_of(f"{FLEET_ROOT}.unknown.scada") is None


def test_rename_keeps_the_node_by_id(store: Store) -> None:
    node = g_node(f"{SPRUCE}.ta", "TerminalAsset")
    load(store, [node], send_time_ms=1_800_000_000_000)
    renamed = dict(node, Alias=f"{FLEET_ROOT}.spruce2.ta", PrevAlias=node["Alias"])
    load(store, [renamed], send_time_ms=1_800_000_001_000)
    assert [h.alias for h in store.tracked_houses()] == [f"{FLEET_ROOT}.spruce2.ta"]
    assert store.g_node(node["Alias"]) is None


def test_an_older_forest_does_not_overwrite(store: Store) -> None:
    node = g_node(f"{SPRUCE}.ta", "TerminalAsset")
    load(store, [dict(node, Status="Suspended")], send_time_ms=1_800_000_002_000)
    load(store, [node], send_time_ms=1_800_000_001_000)
    assert store.tracked_houses() == []
