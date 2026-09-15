"""Settings for gridworks-alerter."""

from pathlib import Path

from gwbase.config import ServiceSettings
from gwbase.config.paths import data_dir
from gwbase.transport_format import LeftRightDot
from pydantic_settings import SettingsConfigDict


class AlerterSettings(ServiceSettings):
    """Reads from env (GWALERTER_*) and/or a `.env` file at the repo root.

    Inherits `rabbit: RabbitBrokerClient`, `log_level` and the rest of
    `ServiceSettings`. The alerter is a service, not a GNode: no identity
    file, and `service_alias` is `<universe>.alerts` under the universe root.
    """

    service_alias: LeftRightDot = "d1.alerts"
    service_name: str = "alerter"  # XDG path segment for logs/state/data

    # Readings older than this leave the store; every detector reads a
    # window shorter than it.
    readings_window_s: int = 4 * 3600

    model_config = SettingsConfigDict(
        env_prefix="GWALERTER_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    def db_path(self) -> Path:
        """The sqlite file, in the service's XDG data dir."""
        return data_dir(self.service_name) / "alerter.sqlite"
