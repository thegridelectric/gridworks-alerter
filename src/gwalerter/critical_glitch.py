"""The scada's own Critical glitch, relayed as a `CriticalGlitch` alert.

A glitch is a moment; the alert is the hold. A Critical glitch from a
tracked house's scada raises the alert with the glitch's summary, a
repeat of the same summary inside the hold extends it, and the hold
passing with no repeat resolves it. A Critical with a different summary
while one is open supersedes it: the open alert resolves and the new one
raises, so each distinct summary pages once. One `CriticalGlitch` alert is
open per house at a time. Warning glitches do not page.
"""

from __future__ import annotations

import uuid

from gwalerter.duration import minutes
from gwalerter.records import resolved_of
from gwalerter.sema.enums import AlertCategory, AlertState, HouseAlertKind, LogLevel
from gwalerter.sema.property_format import LeftRightDot, UTCMilliseconds
from gwalerter.sema.types import Alert, Glitch
from gwalerter.store import Store


class CriticalGlitchRule:
    def __init__(self, store: Store, *, src: LeftRightDot, hold_ms: int) -> None:
        self.store = store
        self.src = src
        self.hold_ms = hold_ms
        # The last time each open alert's summary was seen, by AlertId. An
        # alert open across a restart counts from its RaisedMs.
        self.last_seen_ms: dict[str, UTCMilliseconds] = {}

    def on_glitch(self, glitch: Glitch, *, received_ms: UTCMilliseconds) -> list[Alert]:
        """One glitch as it arrives. Returns the records to broadcast, each
        recorded in the store first: nothing for a glitch below Critical, from
        an untracked scada, or repeating the open alert's summary."""
        if glitch.type is not LogLevel.Critical:
            return []
        house = self.store.tracked_house_of(glitch.from_g_node_alias)
        if house is None:
            return []
        open_alert = self.open_glitch(house.alias)
        records: list[Alert] = []
        if open_alert is not None:
            if open_alert.summary == self.summary(glitch, house.alias):
                self.last_seen_ms[open_alert.alert_id] = received_ms
                return []
            superseded = resolved_of(
                open_alert,
                resolved_ms=received_ms,
                summary=f"superseded by {glitch.summary} ({house.alias})",
            )
            self.store.clear_alert(superseded)
            self.last_seen_ms.pop(open_alert.alert_id, None)
            records.append(superseded)
        alert = Alert(
            src=self.src,
            category=AlertCategory.House,
            kind=HouseAlertKind.CriticalGlitch,
            state=AlertState.Firing,
            about_g_node_alias=house.alias,
            alert_id=str(uuid.uuid4()),
            raised_ms=received_ms,
            summary=self.summary(glitch, house.alias),
            evidence=[],
        )
        self.store.raise_alert(alert)
        self.last_seen_ms[alert.alert_id] = received_ms
        records.append(alert)
        return records

    def evaluate(self, now_ms: UTCMilliseconds) -> list[Alert]:
        """One tick: every open `CriticalGlitch` alert whose summary has not
        repeated for the hold is resolved."""
        records: list[Alert] = []
        for open_alert in self.store.open_alerts():
            if open_alert.kind is not HouseAlertKind.CriticalGlitch:
                continue
            last = self.last_seen_ms.get(open_alert.alert_id, open_alert.raised_ms)
            if now_ms - last < self.hold_ms:
                continue
            resolved = resolved_of(
                open_alert,
                resolved_ms=now_ms,
                summary=(
                    f"no repeat for {minutes(self.hold_ms)} "
                    f"({open_alert.about_g_node_alias})"
                ),
            )
            self.store.clear_alert(resolved)
            self.last_seen_ms.pop(open_alert.alert_id, None)
            records.append(resolved)
        return records

    @staticmethod
    def summary(glitch: Glitch, house_alias: LeftRightDot) -> str:
        return f"{glitch.summary} [{glitch.node}] ({house_alias})"

    def open_glitch(self, house_alias: LeftRightDot) -> Alert | None:
        return self.store.open_alert(
            AlertCategory.House,
            HouseAlertKind.CriticalGlitch,
            about_g_node_alias=house_alias,
        )
