"""AlerterActor — the house alerter on the fleet broker.

An Orchestrator-tier actor of transport class Alerter: it broadcasts its
alert words on its own mic exchange, and it also taps the audit exchange
the way JournalKeeper does (bind everything, keep the tracked types off
the parsed envelope), unwraps each `gw` body,
decodes it through the vendored snapshot, and hands the typed instance
to the store: readings and layouts from the scadas, the registry's forest
broadcasts into the projection. A scada heard from that reports for no
tracked house is logged once per boot.

The detectors run on their own thread at `detector_tick_s`, once the
actor is consuming (a word raised while the channel is down would be
recorded and never sent). Each transition a rule records is broadcast on
the mic exchange with the house alias as the radio channel, so a manager
can bind by house. Arrivals clear on the consumer thread, in dispatch.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

from gwbase.actor_base import OnSendMessageDiagnostic
from gwbase.orchestrator import Orchestrator
from gwbase.topology import EAR_EXCHANGE
from gwbase.transport_encoding import RoutingEnvelope, TransportClass
from gwbase.wrapped import unwrap_bytes

from gwalerter.config import AlerterSettings
from gwalerter.no_data import NoDataRule
from gwalerter.sema.codec import SemaCodec, default_codec
from gwalerter.sema.property_format import UTCMilliseconds
from gwalerter.sema.types import (
    ChannelReadings,
    GNodeForest,
    HouseAlert,
    HouseAlertCleared,
    LayoutLite,
    ReportEvent,
)
from gwalerter.store import Store

TRACKED_TYPES: frozenset[str] = frozenset({
    ReportEvent.type_name_value(),
    LayoutLite.type_name_value(),
    GNodeForest.type_name_value(),
})


def now_ms() -> UTCMilliseconds:
    return int(time.time() * 1000)


class AlerterActor(Orchestrator):
    def __init__(
        self,
        *,
        settings: AlerterSettings,
        store: Store,
        codec: SemaCodec = default_codec,
        clock_ms: Callable[[], UTCMilliseconds] = now_ms,
    ) -> None:
        super().__init__(
            settings=settings,
            transport_class=TransportClass.Alerter,
            my_super_alias=settings.super_alias,
            my_time_coordinator_alias=settings.time_coordinator_alias,
        )
        self.store = store
        self.codec = codec
        self.clock_ms = clock_ms
        self.untracked_seen: set[str] = set()
        self.detector_tick_s = settings.detector_tick_s
        self.no_data = NoDataRule(
            store,
            src=settings.service_alias,
            silence_ms=settings.no_data_silence_s * 1000,
            heard_floor_ms=self.clock_ms(),
        )
        self.detectors_stop = threading.Event()
        self.detector_thread = threading.Thread(
            target=self.run_detectors, name=f"{self.alias}-detectors", daemon=True
        )

    def local_start(self) -> None:
        super().local_start()
        self.detector_thread.start()

    def local_rabbit_startup(self) -> None:
        """Tap the audit exchange with `#`, beside the class binding the
        Orchestrator tier makes; the tracked-type gate is in dispatch, off
        the parsed envelope, because routing-key grammar is transport
        knowledge, not the alerter's."""
        self.logger.info(
            "Binding queue %s to %s with routing key #", self.queue_name, EAR_EXCHANGE
        )
        self._live_channel().queue_bind(self.queue_name, EAR_EXCHANGE, routing_key="#")

    def local_stop(self) -> None:
        super().local_stop()
        self.detectors_stop.set()
        if self.detector_thread.is_alive():
            self.detector_thread.join()
        self.store.close()

    # -- detectors ----------------------------------------------------------

    def run_detectors(self) -> None:
        while not self.detectors_stop.wait(self.detector_tick_s):
            if not self.consuming:
                continue
            try:
                self.evaluate_detectors()
            except Exception:  # noqa: BLE001 -- the detector loop keeps running
                self.logger.exception("Detector evaluation failed")

    def evaluate_detectors(self) -> None:
        for alert in self.no_data.evaluate(self.clock_ms()):
            self.emit(alert)

    def emit(self, word: HouseAlert | HouseAlertCleared) -> None:
        """Broadcast an alert transition on the mic exchange, keyed by the
        house it is about. Best-effort by gwbase contract; the store already
        holds the transition, so a failed send is logged, not retried."""
        self.logger.info(
            "%s %s on %s (%s)",
            word.type_name,
            word.kind.value,
            word.about_g_node_alias,
            word.alert_id,
        )
        diagnostic = self.send(
            envelope=self.broadcast_envelope(
                type_name=word.type_name, radio_channel=word.about_g_node_alias
            ),
            body=json.dumps(word.to_dict()).encode(),
        )
        if diagnostic is not OnSendMessageDiagnostic.MESSAGE_SENT:
            self.logger.error(
                "%s %s not sent: %s", word.type_name, word.alert_id, diagnostic.value
            )

    def process_message(self, *, envelope: RoutingEnvelope, body: bytes) -> None:
        if envelope.type_name not in TRACKED_TYPES:
            return
        received_ms = self.clock_ms()
        try:
            _header, payload = unwrap_bytes(body)
            message = self.codec.from_dict(payload)
        except Exception as e:  # noqa: BLE001 -- the live path keeps running
            self.logger.error(
                "Dropped %s from %s: %r", envelope.type_name, envelope.from_alias, e
            )
            return
        if isinstance(message, ReportEvent):
            self.store.record_report(message, received_ms=received_ms)
            self.note_untracked(message.src)
            self.clear_on_arrival(
                message.src,
                arrival_ms=received_ms,
                evidence=message.report.channel_reading_list,
            )
        elif isinstance(message, LayoutLite):
            self.store.record_layout(message, received_ms=received_ms)
            self.note_untracked(message.from_g_node_alias)
            self.clear_on_arrival(
                message.from_g_node_alias, arrival_ms=received_ms, evidence=[]
            )
        elif isinstance(message, GNodeForest):
            self.store.upsert_forest(message)
        else:
            self.logger.warning(
                "Tracked type %s decoded as %s; not stored",
                envelope.type_name,
                type(message).__name__,
            )

    def clear_on_arrival(
        self,
        scada_alias: str,
        *,
        arrival_ms: UTCMilliseconds,
        evidence: list[ChannelReadings],
    ) -> None:
        cleared = self.no_data.on_arrival(
            scada_alias, arrival_ms=arrival_ms, evidence=evidence
        )
        if cleared is not None:
            self.emit(cleared)

    def note_untracked(self, scada_alias: str) -> None:
        """A scada sending data with no tracked house behind it: a forgotten
        install, or a house outside the fleet roots. Say so once."""
        if scada_alias in self.untracked_seen:
            return
        if self.store.tracked_house_of(scada_alias) is None:
            self.untracked_seen.add(scada_alias)
            self.logger.warning(
                "Heard %s, which reports for no tracked house under %s",
                scada_alias,
                self.store.fleet_roots,
            )
