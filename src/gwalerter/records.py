"""The one way a `Resolved` alert record is built from its `Firing`
record: same category, kind, subject, house and `AlertId`, so the pair
reads as one alert wherever the two are compared."""

from __future__ import annotations

from gwalerter.sema.enums import AlertState
from gwalerter.sema.property_format import UTCMilliseconds
from gwalerter.sema.types import Alert, ChannelReadings


def resolved_of(
    firing: Alert,
    *,
    resolved_ms: UTCMilliseconds,
    summary: str,
    evidence: list[ChannelReadings] | None = None,
) -> Alert:
    return Alert(
        src=firing.src,
        category=firing.category,
        kind=firing.kind,
        state=AlertState.Resolved,
        about_g_node_alias=firing.about_g_node_alias,
        subject=firing.subject,
        alert_id=firing.alert_id,
        raised_ms=firing.raised_ms,
        resolved_ms=resolved_ms,
        summary=summary,
        evidence=evidence or [],
    )
