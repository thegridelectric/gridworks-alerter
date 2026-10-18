"""gwalerter console entry point: `gwalerter rabbit` runs the alerter
actor, `gwalerter tap` the Opsgenie tap, `gwalerter probe` the broker
prober. Each is its own systemd unit (`alerter-rabbit.service`,
`alerter-tap.service`, `alerter-probe.service`); all three open the one
sqlite store.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import dotenv

from gwalerter.alerter_actor import AlerterActor
from gwalerter.config import AlerterSettings
from gwalerter.prober import Prober, doors
from gwalerter.registry_read import fetch_forest
from gwalerter.store import Store
from gwalerter.tap import Tap

logger = logging.getLogger(__name__)


def open_store(settings: AlerterSettings) -> Store:
    """The alerter's store, opened for whichever process this is. The
    package logs to stdout, which is journald under systemd (the actor's
    own log is gwbase's rotating file under the XDG state home)."""
    logging.basicConfig(
        level=settings.log_level.upper(),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return Store.open(
        settings.db_url(),
        readings_window_s=settings.readings_window_s,
        fleet_roots=settings.fleet_roots,
    )


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


def run_rabbit(settings: AlerterSettings) -> None:
    store = open_store(settings)
    seed_projection(settings, store)
    actor = AlerterActor(settings=settings, store=store)
    actor.start()
    try:
        while actor.main_loop_running:
            time.sleep(1)
    except KeyboardInterrupt:
        actor.stop()


def run_tap(settings: AlerterSettings) -> None:
    Tap(settings=settings, store=open_store(settings)).run()


def run_probe(settings: AlerterSettings) -> None:
    Prober(settings=settings, store=open_store(settings), doors=doors(settings)).run()


COMMANDS = {
    "rabbit": (run_rabbit, "run the alerter actor"),
    "tap": (run_tap, "page the store's open alerts through Opsgenie"),
    "probe": (run_probe, "check the broker's doors from outside"),
}


def main(argv: list[str] | None = None) -> None:
    dotenv.load_dotenv(dotenv.find_dotenv())  # populate env BEFORE Settings()
    parser = argparse.ArgumentParser(prog="gwalerter")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, (_, help_text) in COMMANDS.items():
        subcommands.add_parser(name, help=help_text)
    args = parser.parse_args(argv)
    COMMANDS[args.command][0](AlerterSettings.load())
