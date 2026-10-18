"""AlerterActor — the house alerter on the fleet broker.

An Orchestrator-tier actor of transport class Alerter: it broadcasts its
alert words on its own mic exchange, and it also taps the audit exchange
the way JournalKeeper does (bind everything, keep the tracked types off
the parsed envelope), decodes each body through the vendored snapshot,
and hands the typed instance to the store: readings and layouts from the
scadas, the registry's forest broadcasts into the projection. The body
is the bare sema type whatever the routing category: gwbase opens a
`gw` envelope before `process_message` runs. A scada heard from that reports for no
tracked house is logged once per boot.

The detectors run on their own thread at `detector_tick_s`, only while
the actor is consuming: while it is deaf every house looks silent for
the actor's own reason, and there is nothing to detect. Each time
consuming starts (boot and every reconnect) the NoData rule's last-heard
floor moves to that moment, so a house gets one silence threshold to be
heard before it pages. Each `gw.alert` record a rule makes is recorded
in the store, where the tap pages it, and broadcast on the mic exchange
with the alias it is about as the radio channel: the house for a House
alert, so a manager can bind by house, else the alerter's own alias.
Arrivals resolve on the consumer thread, in dispatch.

The silences file (`silences_path`, one `gw.hack.alert.silences`
instance) is the list of houses whose open alerts the tap does not
page. It is loaded into the store at init and re-read on every detector
tick; a change replaces the store's list and the snapshot is broadcast
on the mic exchange, as it is once when consuming starts. Detection is
untouched by a silence.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

from gwbase.actor_base import OnSendMessageDiagnostic
from gwbase.orchestrator import Orchestrator
from gwbase.sema.types.gridworks_header import GridworksHeader
from gwbase.topology import EAR_EXCHANGE
from gwbase.transport_encoding import RoutingEnvelope, TransportClass

from gwalerter.config import AlerterSettings
from gwalerter.no_data import NoDataRule
from gwalerter.sema.codec import SemaCodec, default_codec
from gwalerter.sema.property_format import UTCMilliseconds
from gwalerter.sema.types import (
    Alert,
    ChannelReadings,
    GNodeForest,
    HackAlertSilences,
    LayoutLite,
    ReportEvent,
)
from gwalerter.sema.types.old_versions.layout_lite_011 import LayoutLite011
from gwalerter.sema.types.old_versions.report_event_002 import ReportEvent002
from gwalerter.sema.types.old_versions.report_event_003 import ReportEvent003
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
            heard_period_ms=settings.heard_period_s * 1000,
        )
        self.silences_path = settings.silences_path
        self.silences_file_state = ""
        self.load_silences()
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
        self.hearing_resumed()
        self.announce_silences()

    def hearing_resumed(self) -> None:
        """Consuming has started: the silence until now was the actor's own
        deafness, not a house's, so last-heard is floored at this moment."""
        self.no_data.heard_floor_ms = self.clock_ms()

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
        self.refresh_silences()
        for alert in self.no_data.evaluate(self.clock_ms()):
            self.emit(alert)

    # -- silences -----------------------------------------------------------

    def load_silences(self) -> bool:
        """Make the store's list the silences file's; True when that changed
        it. A missing file is the empty list; a file that does not read
        leaves the list standing. The file's state (loaded, missing,
        unreadable) is logged when it changes, not every tick."""
        try:
            word = self.codec.from_dict(
                json.loads(self.silences_path.read_text()), expect=HackAlertSilences
            )
            state = "loaded"
        except FileNotFoundError:
            word = HackAlertSilences(src=self.alias, silenced_g_node_aliases=[])
            state = "missing"
        except (OSError, ValueError) as e:
            word = None
            state = f"unreadable: {e!r}"
        if state != self.silences_file_state:
            self.silences_file_state = state
            if word is None:
                self.logger.error(
                    "Silences file %s %s; the list stands as last loaded",
                    self.silences_path,
                    state,
                )
            elif state == "missing":
                self.logger.warning(
                    "No silences file at %s: every house pages", self.silences_path
                )
            else:
                self.logger.info(
                    "Silences file %s: %d house(s) silenced",
                    self.silences_path,
                    len(word.silenced_g_node_aliases),
                )
        return word is not None and self.store.replace_silences(word)

    def refresh_silences(self) -> None:
        """On the detector tick: a changed file replaces the store's list
        and the new snapshot is broadcast."""
        if self.load_silences():
            self.announce_silences()

    def announce_silences(self) -> None:
        """Broadcast the silences snapshot on the mic exchange, from the
        store's list so a consumer sees what the tap applies."""
        word = HackAlertSilences(
            src=self.alias, silenced_g_node_aliases=self.store.silenced_aliases()
        )
        self.logger.info(
            "%s: %d house(s) silenced",
            word.type_name,
            len(word.silenced_g_node_aliases),
        )
        diagnostic = self.send(
            envelope=self.broadcast_envelope(type_name=word.type_name),
            body=json.dumps(word.to_dict()).encode(),
        )
        if diagnostic is not OnSendMessageDiagnostic.MESSAGE_SENT:
            self.logger.error("%s not sent: %s", word.type_name, diagnostic.value)

    def emit(self, word: Alert) -> None:
        """Broadcast an alert record on the mic exchange, keyed by the alias
        it is about (`AboutGNodeAlias`, else `Src`). Best-effort by gwbase
        contract; the store already holds the record, so a failed send is
        logged, not retried."""
        about = word.about_g_node_alias or word.src
        self.logger.info(
            "%s %s %s on %s (%s)",
            word.type_name,
            word.state.value,
            word.kind.value,
            about,
            word.alert_id,
        )
        diagnostic = self.send(
            envelope=self.broadcast_envelope(
                type_name=word.type_name, radio_channel=about
            ),
            body=json.dumps(word.to_dict()).encode(),
        )
        if diagnostic is not OnSendMessageDiagnostic.MESSAGE_SENT:
            self.logger.error(
                "%s %s not sent: %s", word.type_name, word.alert_id, diagnostic.value
            )

    def process_message(
        self,
        *,
        envelope: RoutingEnvelope,
        body: bytes,
        header: GridworksHeader | None,  # noqa: ARG002 -- the header is not read
    ) -> None:
        received_ms = self.clock_ms()
        if self.store.under_roots(envelope.from_alias):
            self.store.record_arrival(envelope.from_alias, received_ms=received_ms)
        if envelope.type_name not in TRACKED_TYPES:
            return
        try:
            message = self.codec.from_dict(json.loads(body), auto_upgrade=False)
        except Exception as e:  # noqa: BLE001 -- the live path keeps running
            self.logger.error(
                "Dropped %s from %s: %r", envelope.type_name, envelope.from_alias, e
            )
            return
        if isinstance(message, (ReportEvent, ReportEvent002, ReportEvent003)):
            self.store.record_report(message, received_ms=received_ms)
            self.note_untracked(message.src)
            self.clear_on_arrival(
                message.src,
                arrival_ms=received_ms,
                evidence=message.report.channel_reading_list,
            )
        elif isinstance(message, (LayoutLite, LayoutLite011)):
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
        resolved = self.no_data.on_arrival(
            scada_alias, arrival_ms=arrival_ms, evidence=evidence
        )
        if resolved is not None:
            self.emit(resolved)

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
