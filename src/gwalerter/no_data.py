"""The NoData rule: a tracked house that has sent nothing for longer than
the silence threshold is raised once, and the first arrival from it clears
the alert with that arrival as the evidence.

Last-heard is the store's query (latest arrival of a reading or a
layout), floored at the alerter's own boot time: a house the alerter has
not yet heard from since it booted is silent from boot, not from the
beginning of time, so a fresh alerter gives every house one threshold to
speak before paging. Open-alert state lives in the store, which is what
makes a restart neither re-raise nor forget.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from gwalerter.sema.enums import HouseAlertKind
from gwalerter.sema.property_format import LeftRightDot, UTCMilliseconds
from gwalerter.sema.types import ChannelReadings, HouseAlert, HouseAlertCleared
from gwalerter.store import Store


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
    ) -> None:
        self.store = store
        self.src = src
        self.silence_ms = silence_ms
        self.heard_floor_ms = heard_floor_ms

    def evaluate(self, now_ms: UTCMilliseconds) -> list[HouseAlert]:
        """Raise NoData on every tracked house silent past the threshold
        that has no NoData alert open; each raised word is recorded in
        the store before it is returned."""
        heard: dict[str, UTCMilliseconds] = {}
        for record in self.store.houses():
            house = self.store.tracked_house_of(record.alias)
            if house is not None and record.last_heard_ms is not None:
                heard[house.alias] = record.last_heard_ms
        raised: list[HouseAlert] = []
        for house in self.store.tracked_houses():
            last = max(heard.get(house.alias, 0), self.heard_floor_ms)
            if now_ms - last < self.silence_ms:
                continue
            if self.store.open_alert(house.alias, HouseAlertKind.NoData) is not None:
                continue
            alert = HouseAlert(
                src=self.src,
                about_g_node_alias=house.alias,
                kind=HouseAlertKind.NoData,
                alert_id=str(uuid.uuid4()),
                raised_ms=now_ms,
                summary=f"No data from {house.alias} since {iso_utc(last)}",
                evidence=[],
            )
            self.store.raise_alert(alert)
            raised.append(alert)
        return raised

    def on_arrival(
        self,
        scada_alias: LeftRightDot,
        *,
        arrival_ms: UTCMilliseconds,
        evidence: list[ChannelReadings],
    ) -> HouseAlertCleared | None:
        """A message from a scada: if its tracked house has a NoData alert
        open, clear it with this arrival as the evidence (the report's
        readings; empty for a layout)."""
        house = self.store.tracked_house_of(scada_alias)
        if house is None:
            return None
        open_alert = self.store.open_alert(house.alias, HouseAlertKind.NoData)
        if open_alert is None:
            return None
        cleared = HouseAlertCleared(
            alert_id=open_alert.alert_id,
            src=self.src,
            about_g_node_alias=house.alias,
            kind=HouseAlertKind.NoData,
            cleared_ms=arrival_ms,
            evidence=evidence,
        )
        self.store.clear_alert(cleared)
        return cleared
