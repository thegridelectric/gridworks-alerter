"""Shared fixtures: every actor writes logs and data under XDG dirs, so
tests point those at a temp dir before any settings or actor exists."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gwalerter.config import AlerterSettings
from gwalerter.store import Store

SAMPLES = Path(__file__).resolve().parents[1] / "src" / "gwalerter" / "sema" / "samples"


@pytest.fixture(autouse=True)
def xdg_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    return tmp_path


@pytest.fixture
def settings() -> AlerterSettings:
    return AlerterSettings()


@pytest.fixture
def store() -> Store:
    return Store.open(":memory:", readings_window_s=4 * 3600)


def sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text())
