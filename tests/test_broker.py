"""Against the real dev broker: a wrapped report.event published on
amq.topic lands in the actor's store. Self-skips without a broker."""

from __future__ import annotations

import socket
import time

import pika
import pytest
from gwbase.transport_encoding import TransportClass, WrappedRoutingEnvelope
from gwbase.wrapped import wrap_bytes

from gwalerter.alerter_actor import AlerterActor
from gwalerter.config import AlerterSettings
from gwalerter.store import Store
from tests.conftest import sample


def broker_up() -> bool:
    try:
        with socket.create_connection(("localhost", 5672), timeout=1):
            return True
    except OSError:
        return False


def wait_for(predicate, timeout_s: float, what: str) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.mark.broker
def test_report_event_on_the_bus_reaches_the_store(
    settings: AlerterSettings, store: Store
) -> None:
    if not broker_up():
        pytest.skip("no broker on localhost:5672")
    actor = AlerterActor(settings=settings, store=store)
    actor.start()
    try:
        wait_for(lambda: actor.consuming, 15, "the actor to consume")
        payload = sample("report.event.004.json")
        envelope = WrappedRoutingEnvelope.from_classes(
            type_name="report.event",
            from_alias=payload["Src"],
            to_class=TransportClass.LeafTransactiveNode,
        )
        body = wrap_bytes(
            src=payload["Src"],
            dst="d1.alerts",
            inner_type_name="report.event",
            inner_payload_dict=payload,
        )
        conn = pika.BlockingConnection(
            pika.URLParameters(settings.rabbit.url.get_secret_value())
        )
        try:
            conn.channel().basic_publish("amq.topic", envelope.routing_key, body)
        finally:
            conn.close()
        wait_for(lambda: bool(store.houses()), 10, "the report to reach the store")
        (house,) = store.houses()
        assert house.alias == payload["Src"]
    finally:
        actor.stop()
