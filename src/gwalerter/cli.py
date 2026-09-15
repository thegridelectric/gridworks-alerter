"""gwalerter console entry point — `gwalerter rabbit`.

The systemd unit invokes the actor (`alerter-rabbit.service` →
`gwalerter rabbit`).
"""

from __future__ import annotations

import argparse
import logging
import time

import dotenv

from gwalerter.alerter_actor import AlerterActor
from gwalerter.config import AlerterSettings
from gwalerter.registry_read import fetch_forest
from gwalerter.store import Store

logger = logging.getLogger(__name__)


def seed_projection(settings: AlerterSettings, store: Store) -> None:
    """Ask the registry for the forest under the fleet roots so the
    projection is current at boot; if the registry is unreachable the
    actor still runs and converges from the forest broadcasts."""
    try:
        store.upsert_forest(fetch_forest(settings.gnr_url, settings.fleet_roots))
    except Exception as e:  # noqa: BLE001 -- boot continues on broadcasts
        logger.error("Forest request to %s failed: %r", settings.gnr_url, e)
    logger.info(
        "Tracking %s under %s",
        [house.alias for house in store.tracked_houses()],
        settings.fleet_roots,
    )


def run_rabbit() -> None:
    settings = AlerterSettings.load()
    store = Store.open(
        settings.db_url(),
        readings_window_s=settings.readings_window_s,
        fleet_roots=settings.fleet_roots,
    )
    seed_projection(settings, store)
    actor = AlerterActor(settings=settings, store=store)
    actor.start()
    try:
        while actor.main_loop_running:
            time.sleep(1)
    except KeyboardInterrupt:
        actor.stop()


def main(argv: list[str] | None = None) -> None:
    dotenv.load_dotenv(dotenv.find_dotenv())  # populate env BEFORE Settings()
    parser = argparse.ArgumentParser(prog="gwalerter")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("rabbit", help="run the alerter actor")
    args = parser.parse_args(argv)
    if args.command == "rabbit":
        run_rabbit()
