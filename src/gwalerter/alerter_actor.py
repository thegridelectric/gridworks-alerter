"""AlerterActor — a tap on the fleet broker that keeps the store current.

Consumes the audit exchange the way JournalKeeper does (bind everything,
keep the tracked types off the parsed envelope), unwraps each `gw` body,
decodes it through the vendored snapshot, and hands the typed instance
to the store. No detector runs here yet.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from gwbase.actor_base import ActorBase
from gwbase.transport_encoding import RoutingEnvelope
from gwbase.wrapped import unwrap_bytes

from gwalerter.config import AlerterSettings
from gwalerter.sema.codec import SemaCodec, default_codec
from gwalerter.sema.property_format import UTCMilliseconds
from gwalerter.sema.types import LayoutLite, ReportEvent
from gwalerter.store import Store

TRACKED_TYPES: frozenset[str] = frozenset({
    ReportEvent.type_name_value(),
    LayoutLite.type_name_value(),
})


def now_ms() -> UTCMilliseconds:
    return int(time.time() * 1000)


class AlerterActor(ActorBase):
    def __init__(
        self,
        *,
        settings: AlerterSettings,
        store: Store,
        codec: SemaCodec = default_codec,
        clock_ms: Callable[[], UTCMilliseconds] = now_ms,
    ) -> None:
        super().__init__(settings=settings)
        self.store = store
        self.codec = codec
        self.clock_ms = clock_ms

    def local_rabbit_startup(self) -> None:
        """Bind everything (`#`); the tracked-type gate is in dispatch, off
        the parsed envelope, because routing-key grammar is transport
        knowledge, not the alerter's."""
        self.logger.info(
            "Binding queue %s to %s with routing key #",
            self.queue_name,
            self._consume_exchange,
        )
        self._live_channel().queue_bind(
            self.queue_name, self._consume_exchange, routing_key="#"
        )

    def local_stop(self) -> None:
        super().local_stop()
        self.store.close()

    def dispatch_message(self, *, envelope: RoutingEnvelope, body: bytes) -> None:
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
        elif isinstance(message, LayoutLite):
            self.store.record_layout(message, received_ms=received_ms)
        else:
            self.logger.warning(
                "Tracked type %s decoded as %s; not stored",
                envelope.type_name,
                type(message).__name__,
            )
