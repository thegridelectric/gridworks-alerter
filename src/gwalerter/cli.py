"""gwalerter console entry point — `gwalerter rabbit`.

The systemd unit invokes the actor (`alerter-rabbit.service` →
`gwalerter rabbit`).
"""

from __future__ import annotations

import argparse
import time

import dotenv

from gwalerter.alerter_actor import AlerterActor
from gwalerter.config import AlerterSettings
from gwalerter.store import Store


def run_rabbit() -> None:
    settings = AlerterSettings()
    store = Store.open(settings.db_path(), readings_window_s=settings.readings_window_s)
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
