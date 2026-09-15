"""dispatch_message: tracked types reach the store, the rest are ignored,
and a bad body is logged and dropped without stopping the actor."""

from __future__ import annotations

from gwbase.transport_encoding import (
    TransportClass,
    WrappedRoutingEnvelope,
    parse_routing_key,
)
from gwbase.wrapped import wrap_bytes

from gwalerter.alerter_actor import AlerterActor
from gwalerter.config import AlerterSettings
from gwalerter.store import Store
from tests.conftest import sample


def wrapped(type_name: str, payload: dict) -> tuple[WrappedRoutingEnvelope, bytes]:
    src = payload.get("Src") or payload["FromGNodeAlias"]
    envelope = WrappedRoutingEnvelope.from_classes(
        type_name=type_name, from_alias=src, to_class=TransportClass.LeafTransactiveNode
    )
    body = wrap_bytes(
        src=src, dst="d1.alerts", inner_type_name=type_name, inner_payload_dict=payload
    )
    parsed = parse_routing_key(envelope.routing_key)
    assert isinstance(parsed, WrappedRoutingEnvelope)
    return parsed, body


def actor(settings: AlerterSettings, store: Store) -> AlerterActor:
    return AlerterActor(
        settings=settings, store=store, clock_ms=lambda: 1_800_000_000_000
    )


def test_report_event_reaches_the_store(
    settings: AlerterSettings, store: Store
) -> None:
    envelope, body = wrapped("report.event", sample("report.event.004.json"))
    actor(settings, store).dispatch_message(envelope=envelope, body=body)
    (house,) = store.houses()
    assert house.alias == envelope.from_alias
    assert house.last_heard_ms == 1_800_000_000_000


def test_older_report_event_version_is_upgraded(
    settings: AlerterSettings, store: Store
) -> None:
    envelope, body = wrapped("report.event", sample("report.event.003.json"))
    actor(settings, store).dispatch_message(envelope=envelope, body=body)
    assert [h.alias for h in store.houses()] == [envelope.from_alias]


def test_layout_lite_reaches_the_store(settings: AlerterSettings, store: Store) -> None:
    payload = sample("layout.lite.012.json")
    envelope, body = wrapped("layout.lite", payload)
    actor(settings, store).dispatch_message(envelope=envelope, body=body)
    layout = store.layout(payload["FromGNodeAlias"])
    assert layout is not None
    assert layout.from_g_node_alias == payload["FromGNodeAlias"]


def test_untracked_type_is_ignored(settings: AlerterSettings, store: Store) -> None:
    payload = sample("report.event.004.json")
    envelope, body = wrapped("report.event", payload)
    envelope = WrappedRoutingEnvelope(
        type_name="snapshot.spaceheat",
        from_alias=envelope.from_alias,
        to_class_token="ta",
    )
    actor(settings, store).dispatch_message(envelope=envelope, body=body)
    assert store.houses() == []


def test_bad_body_is_dropped(settings: AlerterSettings, store: Store) -> None:
    envelope, _body = wrapped("report.event", sample("report.event.004.json"))
    actor(settings, store).dispatch_message(envelope=envelope, body=b"not json")
    assert store.houses() == []
