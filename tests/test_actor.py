"""dispatch_message: tracked types reach the store, the rest are ignored,
and a bad body is logged and dropped without stopping the actor. A scada's
report.event and layout.lite arrive `gw`-wrapped; the registry's
g.node.forest arrives as a bare broadcast."""

from __future__ import annotations

import json

from gwbase.actor_base import OnSendMessageDiagnostic
from gwbase.transport_encoding import (
    BroadcastRoutingEnvelope,
    TransportClass,
    WrappedRoutingEnvelope,
    parse_routing_key,
)
from gwbase.wrapped import wrap_bytes

from gwalerter.alerter_actor import AlerterActor
from gwalerter.config import AlerterSettings
from gwalerter.sema.types import HackAlertSilences
from gwalerter.store import Store
from tests.conftest import sample


def wrapped(
    type_name: str, payload: dict, src: str | None = None
) -> tuple[WrappedRoutingEnvelope, bytes]:
    src = src or payload.get("Src") or payload["FromGNodeAlias"]
    envelope = WrappedRoutingEnvelope.from_classes(
        type_name=type_name, from_alias=src, to_class=TransportClass.LeafTransactiveNode
    )
    body = wrap_bytes(
        src=src, dst="d1.alerts", inner_type_name=type_name, inner_payload_dict=payload
    )
    parsed = parse_routing_key(envelope.routing_key)
    assert isinstance(parsed, WrappedRoutingEnvelope)
    return parsed, body


def broadcast(
    type_name: str, payload: dict, src: str
) -> tuple[BroadcastRoutingEnvelope, bytes]:
    """A bare `rjb` broadcast: the body is the sema type itself."""
    envelope = BroadcastRoutingEnvelope.from_classes(
        type_name=type_name, from_alias=src, from_class=TransportClass.Supervisor
    )
    parsed = parse_routing_key(envelope.routing_key)
    assert isinstance(parsed, BroadcastRoutingEnvelope)
    return parsed, json.dumps(payload).encode()


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


def test_report_counts_at_its_sent_version(
    settings: AlerterSettings, store: Store
) -> None:
    """A 003 report whose MessageId differs from Report.Id is valid as sent
    (003 carries no identity axiom) and counts as an arrival; upgrading it
    to 004 would fail 004's axiom and drop the house."""
    payload = sample("report.event.003.json")
    payload["MessageId"] = "11111111-2222-4333-8444-555555555555"
    assert payload["MessageId"] != payload["Report"]["Id"]
    envelope, body = wrapped("report.event", payload)
    actor(settings, store).dispatch_message(envelope=envelope, body=body)
    (house,) = store.houses()
    assert house.alias == envelope.from_alias
    assert house.last_heard_ms == 1_800_000_000_000


def test_layout_lite_reaches_the_store(settings: AlerterSettings, store: Store) -> None:
    payload = sample("layout.lite.012.json")
    envelope, body = wrapped("layout.lite", payload)
    actor(settings, store).dispatch_message(envelope=envelope, body=body)
    layout = store.layout(payload["FromGNodeAlias"])
    assert layout is not None
    assert layout.from_g_node_alias == payload["FromGNodeAlias"]


def test_layout_lite_011_is_stored_at_its_sent_version(
    settings: AlerterSettings, store: Store
) -> None:
    """The fleet on main sends layout.lite 011; it is stored as sent and
    counts as an arrival, like a report at an older version."""
    payload = sample("layout.lite.011.json")
    envelope, body = wrapped("layout.lite", payload)
    actor(settings, store).dispatch_message(envelope=envelope, body=body)
    layout = store.layout(payload["FromGNodeAlias"])
    assert layout is not None
    assert layout.from_g_node_alias == payload["FromGNodeAlias"]
    (house,) = store.houses()
    assert house.alias == payload["FromGNodeAlias"]


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


def test_forest_broadcast_reaches_the_projection(
    settings: AlerterSettings, store: Store
) -> None:
    from tests.conftest import FLEET_ROOT, forest, house_nodes

    payload = forest(
        house_nodes(f"{FLEET_ROOT}.spruce"), [FLEET_ROOT], 1_800_000_000_000
    )
    envelope, body = broadcast("g.node.forest", payload, src="d1.gnr")
    actor(settings, store).dispatch_message(envelope=envelope, body=body)
    assert [h.alias for h in store.tracked_houses()] == [f"{FLEET_ROOT}.spruce.ta"]


def test_untracked_scada_is_logged_once(
    settings: AlerterSettings, store: Store, caplog
) -> None:
    envelope, body = wrapped("report.event", sample("report.event.004.json"))
    a = actor(settings, store)
    a.logger.addHandler(caplog.handler)  # the actor logger does not propagate
    with caplog.at_level("WARNING"):
        a.dispatch_message(envelope=envelope, body=body)
        a.dispatch_message(envelope=envelope, body=body)
    hits = [r for r in caplog.records if "no tracked house" in r.getMessage()]
    assert len(hits) == 1 and envelope.from_alias in hits[0].getMessage()


def silences_file(settings: AlerterSettings, *aliases: str) -> None:
    word = HackAlertSilences(src="d1.alerts", silenced_g_node_aliases=list(aliases))
    settings.silences_path.write_text(json.dumps(word.to_dict()))


def test_silences_file_is_loaded_at_init_and_rebroadcast_on_change(
    settings: AlerterSettings, store: Store
) -> None:
    """The one silences file is the operator interface: its list is in
    the store before the actor consumes, a refresh with the same file
    sends nothing, and a changed file replaces the store's list and
    broadcasts the new snapshot on the mic exchange."""
    elm = "d1.isone.me.versant.keene.elm.ta"
    oak = "d1.isone.me.versant.keene.oak.ta"
    silences_file(settings, elm)
    a = actor(settings, store)
    assert store.silenced_aliases() == [elm]
    sent: list[tuple[str, bytes]] = []

    def capture(*, envelope, body, correlation_id=None):  # noqa: ANN001, ANN202, ARG001
        sent.append((envelope.routing_key, body))
        return OnSendMessageDiagnostic.MESSAGE_SENT

    a.send = capture  # type: ignore[method-assign]
    a.refresh_silences()
    assert sent == []
    silences_file(settings, elm, oak)
    a.refresh_silences()
    assert store.silenced_aliases() == [elm, oak]
    (key, body) = sent[0]
    assert key.endswith("gw-hack-alert-silences")
    word = a.codec.from_dict(json.loads(body), expect=HackAlertSilences)
    assert word.silenced_g_node_aliases == [elm, oak]


def test_a_missing_silences_file_silences_nothing(
    settings: AlerterSettings, store: Store
) -> None:
    assert not settings.silences_path.exists()
    actor(settings, store)
    assert store.silenced_aliases() == []
