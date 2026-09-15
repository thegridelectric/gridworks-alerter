"""Shared fixtures: every actor writes logs and data under XDG dirs, so
tests point those at a temp dir before any settings or actor exists."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from gwalerter.config import AlerterSettings
from gwalerter.sema.property_format import LeftRightDot, UTCMilliseconds
from gwalerter.sema.types import ChannelReadings, Report, ReportEvent
from gwalerter.store import Store

SAMPLES = Path(__file__).resolve().parents[1] / "src" / "gwalerter" / "sema" / "samples"


FLEET_ROOT = "d1.isone.me.versant.keene"
GNR_URL = "http://gnr.test"


@pytest.fixture(autouse=True)
def xdg_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.setenv("GWALERTER_FLEET_ROOTS", FLEET_ROOT)
    monkeypatch.setenv("GWALERTER_GNR_URL", GNR_URL)
    monkeypatch.setenv("GWALERTER_SUPER_ALIAS", "d1.super1")
    monkeypatch.setenv("GWALERTER_TIME_COORDINATOR_ALIAS", "d1.time")
    return tmp_path


@pytest.fixture
def settings() -> AlerterSettings:
    return AlerterSettings.load()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """A store on a temp file, built by the real migration chain."""
    return Store.open(
        f"sqlite:///{tmp_path / 'alerter.sqlite'}",
        readings_window_s=4 * 3600,
        fleet_roots=[FLEET_ROOT],
    )


def sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text())


BASE_CLASS = {
    "TerminalAsset": "TerminalAsset",
    "LeafTransactiveNode": "LeafTransactiveNode",
}


def g_node(alias: str, g_node_class: str, status: str = "Active") -> dict:
    """A `g.node.gt` wire dict from the sample, re-aliased; the id is
    derived from the alias so the same alias maps to the same node."""
    node = dict(sample("g.node.gt.006.json"))
    # uuid5 bytes re-stamped as version 4, so the id is deterministic per
    # alias and still passes the uuid4.str format.
    node["GNodeId"] = str(
        uuid.UUID(bytes=uuid.uuid5(uuid.NAMESPACE_DNS, alias).bytes, version=4)
    )
    node["Alias"] = alias
    node["GNodeClass"] = g_node_class
    node["BaseClass"] = BASE_CLASS.get(g_node_class, "Logical")
    node["Status"] = status
    node.pop("PrevAlias", None)
    if node["BaseClass"] == "Logical":
        node.pop("PositionPointId", None)  # a physical node keeps the sample's
    return node


def house_nodes(house: str, status: str = "Active") -> list[dict]:
    """The three GNodes of one home: LTN, terminal asset, scada."""
    return [
        g_node(house, "LeafTransactiveNode", status),
        g_node(f"{house}.ta", "TerminalAsset", status),
        g_node(f"{house}.scada", "Scada", status),
    ]


def forest(nodes: list[dict], roots: list[str], send_time_ms: int) -> dict:
    return {
        "Roots": roots,
        "Nodes": nodes,
        "Edges": [],
        "SendTimeMs": send_time_ms,
        "TypeName": "g.node.forest",
        "Version": "002",
    }


def report_event(
    scada_alias: LeftRightDot, house_alias: LeftRightDot, *, read_ms: UTCMilliseconds
) -> ReportEvent:
    """A one-channel report from a scada, built through the snapshot
    classes so every axiom (source, id and time propagation) validates."""
    report_id = str(uuid.uuid4())
    slot_start_s = read_ms // 1000 - (read_ms // 1000) % 300
    report = Report(
        from_g_node_alias=scada_alias,
        from_g_node_instance_id=str(uuid.uuid4()),
        about_g_node_alias=house_alias,
        slot_start_unix_s=slot_start_s,
        slot_duration_s=300,
        channel_reading_list=[
            ChannelReadings(
                channel_name="hp-idu-pwr",
                value_list=[0],
                scada_read_time_unix_ms_list=[read_ms],
            )
        ],
        state_list=[],
        fsm_report_list=[],
        message_created_ms=read_ms,
        id=report_id,
    )
    return ReportEvent(
        message_id=report_id, time_created_ms=read_ms, src=scada_alias, report=report
    )
