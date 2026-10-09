"""The NoData rule: a tracked house that has sent nothing for longer than
the silence threshold is raised once, and the first arrival from it clears
the alert with that arrival as the evidence.

Last-heard is the store's query (latest arrival of a reading or a
layout), floored at the alerter's own boot time: a house the alerter has
not yet heard from since it booted is silent from boot, not from the
beginning of time, so a fresh alerter gives every house one threshold to
speak before paging. Open-alert state lives in the store, which is what
makes a restart neither re-raise nor forget.

While a `BrokerUnreachable` alert is open in the store (the prober's
finding), every house is silent for the same reason and that one alert
says so: the rule raises no NoData. When the last one resolves, the floor
moves to that moment, so each house gets one threshold to reconnect
before it pages.

The summary names the cause from whether the house's scada and LTN have
each been heard at all inside `heard_period_ms` times `NYQUIST`; the
four clauses are in `CAUSE_CLAUSES`, re-read on every tick.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from gwalerter.sema.enums import (
    AlertCategory,
    AlertState,
    HouseAlertKind,
    PlatformAlertKind,
)
from gwalerter.sema.property_format import LeftRightDot, UTCMilliseconds
from gwalerter.sema.types import Alert, ChannelReadings
from gwalerter.store import Store, parent_alias

SCADA_SUFFIX = ".scada"

# A party expected every period is silent when not heard for more than
# twice that, with a little slack for jitter.
NYQUIST = 2.1

# A party is "speaking" when anything arrived from it inside the window,
# whatever the message; a house is its LTN and its scada.
CAUSE_CLAUSES: dict[str, str] = {
    "both_silent": "scada and ltn both silent",
    "scada_silent": "scada silent; ltn speaking",
    "ltn_silent": "scada speaking; ltn silent",
    "both_speaking": "scada and ltn both speaking; reports not arriving",
}


def iso_utc(ms: UTCMilliseconds) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


class NoDataRule:
    def __init__(
        self,
        store: Store,
        *,
        src: LeftRightDot,
        silence_ms: int,
        heard_floor_ms: UTCMilliseconds,
        heard_period_ms: int,
    ) -> None:
        self.store = store
        self.src = src
        self.silence_ms = silence_ms
        self.heard_floor_ms = heard_floor_ms
        self.heard_fresh_ms = int(heard_period_ms * NYQUIST)
        self.broker_down = False

    def evaluate(self, now_ms: UTCMilliseconds) -> list[Alert]:
        """Raise NoData on every tracked house silent past the threshold
        that has no NoData alert open; each `Firing` record is recorded in
        the store before it is returned. An open alert whose cause clause
        has moved is rewritten in the store and returned again under its
        `AlertId`. Nothing is raised while the broker is down, and the
        floor moves to the moment it comes back."""
        broker_down = self.broker_unreachable()
        if self.broker_down and not broker_down:
            self.heard_floor_ms = now_ms
        self.broker_down = broker_down
        if broker_down:
            return []
        heard: dict[str, UTCMilliseconds] = {}
        for record in self.store.houses():
            house = self.store.tracked_house_of(record.alias)
            if house is not None and record.last_heard_ms is not None:
                heard[house.alias] = record.last_heard_ms
        raised: list[Alert] = []
        for house in self.store.tracked_houses():
            last = max(heard.get(house.alias, 0), self.heard_floor_ms)
            if now_ms - last < self.silence_ms:
                continue
            clause = self.cause(house.alias, now_ms)
            open_alert = self.open_no_data(house.alias)
            if open_alert is not None:
                if open_alert.summary.endswith(f"({clause})"):
                    continue
                updated = self.firing(
                    house.alias,
                    alert_id=open_alert.alert_id,
                    raised_ms=open_alert.raised_ms,
                    since=iso_utc(last),
                    clause=clause,
                )
                self.store.update_alert(updated)
                raised.append(updated)
                continue
            alert = self.firing(
                house.alias,
                alert_id=str(uuid.uuid4()),
                raised_ms=now_ms,
                since=iso_utc(last),
                clause=clause,
            )
            self.store.raise_alert(alert)
            raised.append(alert)
        return raised

    def firing(
        self,
        house_alias: LeftRightDot,
        *,
        alert_id: str,
        raised_ms: UTCMilliseconds,
        since: str,
        clause: str,
    ) -> Alert:
        return Alert(
            src=self.src,
            category=AlertCategory.House,
            kind=HouseAlertKind.NoData,
            state=AlertState.Firing,
            about_g_node_alias=house_alias,
            alert_id=alert_id,
            raised_ms=raised_ms,
            summary=f"No data from {house_alias} since {since} ({clause})",
            evidence=[],
        )

    def cause(self, house_alias: LeftRightDot, now_ms: UTCMilliseconds) -> str:
        """Which of the house's two parties are speaking now: the LTN is the
        terminal asset's parent alias, the scada its `.scada` sibling."""
        ltn = parent_alias(house_alias)
        scada = ltn + SCADA_SUFFIX

        def speaking(alias: str) -> bool:
            last = self.store.last_arrival(alias)
            return last is not None and now_ms - last < self.heard_fresh_ms

        match speaking(scada), speaking(ltn):
            case False, False:
                return CAUSE_CLAUSES["both_silent"]
            case False, True:
                return CAUSE_CLAUSES["scada_silent"]
            case True, False:
                return CAUSE_CLAUSES["ltn_silent"]
            case _:
                return CAUSE_CLAUSES["both_speaking"]

    def on_arrival(
        self,
        scada_alias: LeftRightDot,
        *,
        arrival_ms: UTCMilliseconds,
        evidence: list[ChannelReadings],
    ) -> Alert | None:
        """A message from a scada: if its tracked house has a NoData alert
        open, resolve it with this arrival as the evidence (the report's
        readings; empty for a layout)."""
        house = self.store.tracked_house_of(scada_alias)
        if house is None:
            return None
        open_alert = self.open_no_data(house.alias)
        if open_alert is None:
            return None
        resolved = Alert(
            src=self.src,
            category=AlertCategory.House,
            kind=HouseAlertKind.NoData,
            state=AlertState.Resolved,
            about_g_node_alias=house.alias,
            alert_id=open_alert.alert_id,
            raised_ms=open_alert.raised_ms,
            resolved_ms=arrival_ms,
            summary=f"Data from {house.alias} again at {iso_utc(arrival_ms)}",
            evidence=evidence,
        )
        self.store.clear_alert(resolved)
        return resolved

    def open_no_data(self, house_alias: LeftRightDot) -> Alert | None:
        return self.store.open_alert(
            AlertCategory.House,
            HouseAlertKind.NoData,
            about_g_node_alias=house_alias,
        )

    def broker_unreachable(self) -> bool:
        """Whether the prober holds a BrokerUnreachable alert open on any
        door."""
        return any(
            alert.kind is PlatformAlertKind.BrokerUnreachable
            for alert in self.store.open_alerts()
        )
