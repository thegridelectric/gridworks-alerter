import re
import uuid
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BeforeValidator, Field


# --- patterns ---
HANDLE_NAME_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*)*$"
)

HH_MM_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")

IANA_TIMEZONE_STR_PATTERN = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$")

LEFT_RIGHT_DOT_PATTERN = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9]+)*$")

SEMA_VERSION_PATTERN = re.compile(r"^[0-9]{3}$")

SPACEHEAT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

UUID4_STR_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# --- methods ---
def is_handle_name(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"<{v}>: HandleName must be a string.")

    if not HANDLE_NAME_PATTERN.fullmatch(v):
        raise ValueError(f"<{v}>: Fails HandleName format.")

    return v


def is_hh_mm(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"<{v}>: hh.mm must be a string.")

    if not HH_MM_PATTERN.fullmatch(v):
        raise ValueError(f"<{v}>: Fails hh.mm format.")

    return v


def is_iana_timezone_str(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"<{v}>: iana.timezone.str must be a string.")

    if not IANA_TIMEZONE_STR_PATTERN.fullmatch(v):
        raise ValueError(f"<{v}>: Fails iana.timezone.str format.")

    return v


def is_left_right_dot(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"<{v}>: LeftRightDot must be a string.")

    if not LEFT_RIGHT_DOT_PATTERN.fullmatch(v):
        raise ValueError(f"<{v}>: Fails LeftRightDot format.")

    return v


def is_positive_int(v: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise TypeError("Not an int!")
    if v <= 0:
        raise ValueError(f"{v} must be positive")
    return v


def is_sema_version(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"<{v}>: SemaVersion must be a string.")

    if not SEMA_VERSION_PATTERN.fullmatch(v):
        raise ValueError(f"<{v}>: Fails SemaVersion format (three digits).")

    return v


def is_spaceheat_name(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"<{v}>: SpaceheatName must be a string.")

    if len(v) > 64:
        raise ValueError(f"<{v}>: SpaceheatName exceeds maximum length of 64.")

    if not SPACEHEAT_NAME_PATTERN.fullmatch(v):
        raise ValueError(f"<{v}>: Fails SpaceheatName format.")

    return v


def is_utc_milliseconds(v: int) -> int:
    if not isinstance(v, int):
        raise TypeError("Not an int!")
    start_date = datetime(2000, 1, 1, tzinfo=UTC)
    end_date = datetime(3000, 1, 1, tzinfo=UTC)

    start_timestamp_ms = int(start_date.timestamp() * 1000)
    end_timestamp_ms = int(end_date.timestamp() * 1000)

    if v < start_timestamp_ms:
        raise ValueError(f"{v} must be after Jan 1 2000")
    if v > end_timestamp_ms:
        raise ValueError(f"{v} must be before Jan 1 3000")
    return v


def is_utc_seconds(v: int) -> int:
    if not isinstance(v, int):
        raise ValueError("Not an int!")
    start_date = datetime(2000, 1, 1, tzinfo=UTC)
    end_date = datetime(3000, 1, 1, tzinfo=UTC)

    start_timestamp = int(start_date.timestamp())
    end_timestamp = int(end_date.timestamp())

    if v < start_timestamp:
        raise ValueError(f"{v}: Fails UTCSeconds format! Must be after Jan 1 2000")
    if v > end_timestamp:
        raise ValueError(f"{v}: Fails UTCSeconds format! Must be before Jan 1 3000")
    return v


def is_uuid4_str(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"<{v}>: uuid4.str must be a string.")

    if not UUID4_STR_PATTERN.fullmatch(v):
        raise ValueError(f"<{v}>: Fails uuid4.str format.")

    try:
        u = uuid.UUID(v)
    except Exception as e:
        raise ValueError(f"Invalid UUID4: {v}  <{e}>") from e
    if u.version != 4:
        raise ValueError(
            f"{v} is valid uid, but of version {u.version}. Fails UuidCanonicalTextual"
        )
    return str(u)


# --- annotated types ---
HandleName = Annotated[
    str,
    BeforeValidator(is_handle_name),
]

HhMm = Annotated[
    str,
    BeforeValidator(is_hh_mm),
]

IanaTimezoneStr = Annotated[
    str,
    BeforeValidator(is_iana_timezone_str),
]

LeftRightDot = Annotated[
    str,
    BeforeValidator(is_left_right_dot),
]

NonEmptyString = Annotated[
    str,
    Field(min_length=1),
]

PositiveInt = Annotated[
    int,
    BeforeValidator(is_positive_int),
]

SemaVersion = Annotated[
    str,
    BeforeValidator(is_sema_version),
]

SpaceheatName = Annotated[
    str,
    BeforeValidator(is_spaceheat_name),
]

UTCMilliseconds = Annotated[
    int,
    BeforeValidator(is_utc_milliseconds),
]

UTCSeconds = Annotated[
    int,
    BeforeValidator(is_utc_seconds),
]

UUID4Str = Annotated[
    str,
    BeforeValidator(is_uuid4_str),
]
