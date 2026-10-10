"""The polar-h10 command with a stand-in strap: `record` runs until SIGTERM with the options given and prints the
summary; it completes a session that ended without a stop; a refused setting and an unreachable strap are said;
`capabilities` and `sessions` answer at once; `mark` without a recording says how to start one; a stop while a client
follows the event stream is immediate; a missing config is said."""

import asyncio
import json
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fakes import CAPS, FakeStrap, hr_bytes

from polar_h10.api import create_app
from polar_h10.cli import NOT_RUNNING, api_server, api_socket, build_parser, main, request_of, stop_api
from polar_h10.config import Config, load
from polar_h10.files import SessionFiles, write_start
from polar_h10.hub import Hub
from polar_h10.recorder import Recorder, Request, default_request
from polar_h10.strap import NOT_FOUND, StrapNotFound


class StoppingStrap(FakeStrap):
    """A strap whose recording gets SIGTERM as soon as its streams start, as from `kill` or Ctrl+C."""

    async def start_battery(self, on_battery: Callable[[int], None]) -> None:
        """Start the battery notifications, then send the stop signal."""
        await super().start_battery(on_battery)
        os.kill(os.getpid(), signal.SIGTERM)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """A config with the sessions in the test folder and a free API port."""
    path = tmp_path / 'config.toml'
    path.write_text(f"[storage]\ndir = '{tmp_path / 'sessions'}'\nmemory_dir = '{tmp_path / 'memory'}'\n[api]\nport = 0\n")
    return path


def run(config_file: Path, *args: str, strap: FakeStrap | None = None) -> int:
    """polar-h10 with this config and this stand-in strap."""
    found = strap or FakeStrap()
    return main(['--config', str(config_file), *args], strap_factory=lambda: found)


def test_record_runs_until_sigterm_with_the_options_given(config_file: Path, capsys: pytest.CaptureFixture) -> None:
    strap = StoppingStrap(hr=[hr_bytes(86, (700,))])
    assert run(config_file, 'record', '--no-acc', '--ecg-rate', '130', strap=strap) == 0
    assert strap.started == [('ecg', {'sample_rate': 130, 'resolution': 14})] and not strap.connected
    summary = json.loads(capsys.readouterr().out)
    assert summary['ended_cleanly'] is True and summary['samples']['hr'] == 1


def test_a_session_that_ended_without_a_stop_is_completed_by_the_next_record(config_file: Path) -> None:
    folder = load(config_file).storage_dir / '2026-10-09_10-00-00'
    SessionFiles(folder, {'hr'}).close()
    write_start(folder, {'id': folder.name, 'start': 'x', 'start_ns': 10**9, 'settings': {'hr': {}}})
    assert run(config_file, 'record', strap=StoppingStrap(hr=[hr_bytes(86, (700,))])) == 0
    assert json.loads((folder / 'session.json').read_text())['ended_cleanly'] is False


def test_a_refused_setting_is_said_with_the_valid_values(config_file: Path, capsys: pytest.CaptureFixture) -> None:
    assert run(config_file, 'record', '--acc-rate', '300') == 1
    assert 'offers 25, 50, 100, 200' in capsys.readouterr().err


def test_a_strap_that_cannot_be_reached_is_said(config_file: Path, capsys: pytest.CaptureFixture) -> None:
    assert run(config_file, 'record', strap=FakeStrap(fail=StrapNotFound(NOT_FOUND))) == 1
    assert NOT_FOUND in capsys.readouterr().err


def test_capabilities_and_sessions_answer_at_once(config_file: Path, capsys: pytest.CaptureFixture) -> None:
    assert run(config_file, 'capabilities') == 0 and json.loads(capsys.readouterr().out) == CAPS
    assert run(config_file, 'sessions') == 0 and json.loads(capsys.readouterr().out) == []


def test_the_session_holds_what_the_command_line_says(config_file: Path) -> None:
    config = load(config_file)

    def request(*args: str) -> Request:
        """The session of these arguments."""
        return request_of(build_parser().parse_args(['record', *args]), config)

    base = default_request(config)
    assert request() == base
    assert request('--no-hr', '--no-ecg', '--acc-rate', '50', '--acc-range', '2') == Request(
        False, None, base.acc | {'sample_rate': 50, 'range': 2})
    assert request('--ecg-resolution', '14').ecg == base.ecg | {'resolution': 14}


def test_mark_without_a_recording_says_how_to_start_one(config_file: Path, capsys: pytest.CaptureFixture) -> None:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    client = httpx.Client(base_url=f'http://127.0.0.1:{port}')
    assert main(['--config', str(config_file), 'mark', 'angina onset'], client=client) == 1
    assert NOT_RUNNING in capsys.readouterr().err


async def test_a_stop_ends_the_event_streams_at_once(config: Config, capfd: pytest.CaptureFixture) -> None:
    hub = Hub()
    config = Config(storage_dir=config.storage_dir, memory_dir=config.memory_dir, port=0)
    sock = api_socket(config)
    server = api_server(create_app(Recorder(config, FakeStrap, hub), hub, config))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    connected = threading.Event()

    def follow() -> None:
        """A client that keeps reading the stream, as the Automation desk does."""
        url = f'http://127.0.0.1:{sock.getsockname()[1]}/stream'
        with httpx.Client(timeout=10) as client, client.stream('GET', url, params={'streams': 'hr'}) as response:
            for _ in response.iter_lines():
                connected.set()

    reader = threading.Thread(target=follow, daemon=True)
    reader.start()
    assert await asyncio.to_thread(connected.wait, 5)
    started = time.monotonic()
    await stop_api(server, serving, hub)
    assert time.monotonic() - started < 1.0, 'the stop does not wait for the graceful-shutdown time'
    await asyncio.to_thread(reader.join, 5)
    assert 'Traceback' not in capfd.readouterr().err


def test_a_missing_config_is_said(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(['--config', str(tmp_path / 'none.toml'), 'sessions']) == 2
    assert 'Copy config.example.toml' in capsys.readouterr().err
