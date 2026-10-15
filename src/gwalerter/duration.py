"""The one way a Summary says how long: minutes to a tenth."""

from gwalerter.sema.property_format import UTCMilliseconds


def minutes(ms: UTCMilliseconds | int) -> str:
    return f"{ms / 60_000:.1f} minutes"
