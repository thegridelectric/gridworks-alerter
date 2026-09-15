"""Settings for gridworks-alerter."""

from pathlib import Path
from typing import Annotated

from gwbase.config import ServiceSettings
from gwbase.config.paths import data_dir
from gwbase.transport_format import LeftRightDot
from pydantic import BeforeValidator
from pydantic_settings import NoDecode, SettingsConfigDict


def split_commas(value: object) -> object:
    """`GWALERTER_FLEET_ROOTS=a.b,c.d` in a `.env` file, not JSON."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class AlerterSettings(ServiceSettings):
    """Reads from env (GWALERTER_*) and/or a `.env` file at the repo root.

    Inherits `rabbit: RabbitBrokerClient`, `log_level` and the rest of
    `ServiceSettings`. The alerter is a service, not a GNode: no identity
    file, and `service_alias` is `<universe>.alerts` under the universe root.
    """

    service_alias: LeftRightDot = "d1.alerts"
    service_name: str = "alerter"  # XDG path segment for logs/state/data

    # The Orchestrator tier's control plane: whose heartbeats to answer and
    # whose simulated time to follow. No defaults: each universe names its
    # own.
    super_alias: LeftRightDot
    time_coordinator_alias: LeftRightDot

    # The registry subtrees this alerter pages for: every Pending or Active
    # TerminalAsset under these roots is tracked, nothing else. No default:
    # each deployment declares its fleet.
    fleet_roots: Annotated[list[LeftRightDot], NoDecode, BeforeValidator(split_commas)]

    # The registry's HTTP read, for the forest request at boot
    # (`POST <gnr_url>/gnr/g-node-forest-request`). No default.
    gnr_url: str

    # Readings older than this leave the store; every detector reads a
    # window shorter than it.
    readings_window_s: int = 4 * 3600

    # NoData: a tracked house with no arrival for this long is silent.
    no_data_silence_s: int = 600

    # How often the detectors re-evaluate their rules.
    detector_tick_s: int = 10

    model_config = SettingsConfigDict(
        env_prefix="GWALERTER_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def load(cls) -> "AlerterSettings":
        """Settings from env and `.env`. The fields with no default are what
        pyright reads as missing constructor arguments; the
        settings sources supply them, so this is the one place that call
        is made."""
        return cls()  # pyright: ignore[reportCallIssue]

    def db_path(self) -> Path:
        """The sqlite file, in the service's XDG data dir."""
        return data_dir(self.service_name) / "alerter.sqlite"

    def db_url(self) -> str:
        return f"sqlite:///{self.db_path()}"
