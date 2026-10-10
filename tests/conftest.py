"""Shared test helpers: a config that writes into the test's own folder."""

from pathlib import Path

import pytest

from polar_h10.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Sessions below the test folder, reconnect waits of 2, 4 and 8 s, two marker keys."""
    return Config(storage_dir=tmp_path / 'sessions', memory_dir=tmp_path / 'memory', first_wait_s=2.0, max_wait_s=8.0,
                  markers={'1': 'angina onset', '2': 'angina stops'})
