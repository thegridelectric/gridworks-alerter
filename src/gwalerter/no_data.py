"""Checks for three conditions, all having to do with no readings making it
up to the journalDB from a particular scada. These readings come from
report.events. The scadas will stop sending report.events (but will keep
sending snapshots) if they are not communicating with their LTN. So there
are three types of alerts:

  - ScadaSilent: nothing heard from the scada inside the fresh window.
  - LtnSilent: scada speaking, LTN not. The scada sends no reports without
    an LTN.
  - NoData: both speaking, no reports arriving.
"""

from __future__ import annotations

import uuid

from gwalerter.duration import minutes
from gwalerter.records import resolved_of
from gwalerter.sema.enums import (
    AlertCategory,
    AlertState,
    HouseAlertKind,
    PlatformAlertKind,
)
from gwalerter.sema.property_format import LeftRightDot, UTCMilliseconds
from gwalerter.sema.types import Alert
from gwalerter.store import HouseRecord, Store, parent_alias

SCADA_SUFFIX = ".scada"

# A party expected every period is silent when not heard for more than
# twice that, with a little slack for jitter.
NYQUIST = 2.1

# The kinds this rule raises; at most one is open per house.
SILENCE_KINDS = (
    HouseAlertKind.ScadaSilent,
    HouseAlertKind.LtnSilent,
    HouseAlertKind.NoData,
)

# The Summary by kind, `{d}` the silence in minutes; the alias follows in
# parens.
SUMMARIES: dict[HouseAlertKind, str] = {
    HouseAlertKind.ScadaSilent: "scada silent for {d}",
    HouseAlertKind.LtnSilent: (
        "ltn silent for {d}; scada does not send up reports with no ltn"
    ),
    HouseAlertKind.NoData: "no reports for {d}, scada and ltn both speaking",
}


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
        """One tick over the store. An open silence alert whose house has a
        report arrived since the raise is resolved, with that report's
        readings as evidence. Then every tracked house silent past the
        threshold with no silence alert open is raised, and an open alert
        whose kind has moved is rewritten under its `AlertId`. Each record
        is recorded in the store before it is returned. Nothing is raised
        or rewritten while the broker is down, and the floor moves to the
        moment it comes back."""
        heard: dict[str, HouseRecord] = {}
        for record in self.store.houses():
            house = self.store.tracked_house_of(record.alias)
            if house is not None:
                heard[house.alias] = record
        records: list[Alert] = []
        for house in self.store.tracked_houses():
            record = heard.get(house.alias)
            open_alert = self.open_silence(house.alias)
            if (
                record is None
                or open_alert is None
                or record.last_heard_ms <= open_alert.raised_ms
            ):
                continue
            resolved = self.resolved(
                open_alert,
                scada_alias=record.alias,
                arrival_ms=record.last_heard_ms,
            )
            self.store.clear_alert(resolved)
            records.append(resolved)
        broker_down = self.broker_unreachable()
        if self.broker_down and not broker_down:
            self.heard_floor_ms = now_ms
        self.broker_down = broker_down
        if broker_down:
            return records
        for house in self.store.tracked_houses():
            record = heard.get(house.alias)
            last = max(record.last_heard_ms if record else 0, self.heard_floor_ms)
            if now_ms - last < self.silence_ms:
                continue
            kind = self.cause(house.alias, now_ms)
            open_alert = self.open_silence(house.alias)
            if open_alert is not None:
                if open_alert.kind is kind:
                    continue
                updated = self.firing(
                    house.alias,
                    kind=kind,
                    alert_id=open_alert.alert_id,
                    raised_ms=open_alert.raised_ms,
                    silent_ms=now_ms - last,
                )
                self.store.update_alert(updated)
                records.append(updated)
                continue
            alert = self.firing(
                house.alias,
                kind=kind,
                alert_id=str(uuid.uuid4()),
                raised_ms=now_ms,
                silent_ms=now_ms - last,
            )
            self.store.raise_alert(alert)
            records.append(alert)
        return records

    def firing(
        self,
        house_alias: LeftRightDot,
        *,
        kind: HouseAlertKind,
        alert_id: str,
        raised_ms: UTCMilliseconds,
        silent_ms: int,
    ) -> Alert:
        return Alert(
            src=self.src,
            category=AlertCategory.House,
            kind=kind,
            state=AlertState.Firing,
            about_g_node_alias=house_alias,
            alert_id=alert_id,
            raised_ms=raised_ms,
            summary=f"{SUMMARIES[kind].format(d=minutes(silent_ms))} ({house_alias})",
            evidence=[],
        )

    def cause(
        self, house_alias: LeftRightDot, now_ms: UTCMilliseconds
    ) -> HouseAlertKind:
        """The kind to raise for the house (`house_alias` is its
        TerminalAsset GNode), from which of its two parties, the scada
        and the LTN, has been heard inside the fresh window. A silent
        scada is `ScadaSilent` whether or not the LTN is silent too: the
        page is about the scada either way."""
        ltn = parent_alias(house_alias)
        scada = ltn + SCADA_SUFFIX

        def speaking(alias: str) -> bool:
            last = self.store.last_arrival(alias)
            return last is not None and now_ms - last < self.heard_fresh_ms

        match speaking(scada), speaking(ltn):
            case False, _:
                return HouseAlertKind.ScadaSilent
            case True, False:
                return HouseAlertKind.LtnSilent
            case _:
                return HouseAlertKind.NoData

    def resolved(
        self,
        open_alert: Alert,
        *,
        scada_alias: LeftRightDot,
        arrival_ms: UTCMilliseconds,
    ) -> Alert:
        """The `Resolved` record for `open_alert`: `ResolvedMs` is the
        arrival of the report that ended the silence and the evidence is
        that report's readings from the store."""
        return resolved_of(
            open_alert,
            resolved_ms=arrival_ms,
            # Since the last data: the alert was raised one silence after it.
            summary=(
                f"data again after "
                f"{minutes(arrival_ms - open_alert.raised_ms + self.silence_ms)} "
                f"({open_alert.about_g_node_alias})"
            ),
            evidence=self.store.report_readings(scada_alias, received_ms=arrival_ms),
        )

    def open_silence(self, house_alias: LeftRightDot) -> Alert | None:
        """The house's open alert of any silence kind, if one is."""
        for kind in SILENCE_KINDS:
            open_alert = self.store.open_alert(
                AlertCategory.House, kind, about_g_node_alias=house_alias
            )
            if open_alert is not None:
                return open_alert
        return None

    def broker_unreachable(self) -> bool:
        """Whether a BrokerUnreachable alert is open on any door."""
        return any(
            alert.kind is PlatformAlertKind.BrokerUnreachable
            for alert in self.store.open_alerts()
        )
