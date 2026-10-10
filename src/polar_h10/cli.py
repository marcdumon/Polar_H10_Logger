"""The polar-h10 command. `record` connects to the strap, writes every stream to CSV files, serves the API for the
Automation desk and shows the live view until q, Ctrl+C or SIGTERM; then it stops the session and prints its summary.
`capabilities` and `sessions` answer at once; `mark` adds a marker to a running recording through its API."""

import argparse
import asyncio
import contextlib
import json
import signal
import socket
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from polar_h10 import live
from polar_h10.api import create_app
from polar_h10.config import DEFAULT_PATH, Config, ConfigError, load
from polar_h10.files import finish_unfinished, list_sessions
from polar_h10.hub import Hub
from polar_h10.protocol import ProtocolError
from polar_h10.recorder import Recorder, Request, StorageError, UnsupportedSetting, default_request
from polar_h10.strap import BleakStrap, Strap, StrapError

NOT_RUNNING = 'No recording runs. Start one with: polar-h10 record'
TIMEOUT_S = 30
# the options of each PMD stream on the command line; 'rate' is the strap's sample_rate
OPTIONS = {'ecg': ('rate', 'resolution'), 'acc': ('rate', 'resolution', 'range')}


def build_parser() -> argparse.ArgumentParser:
    """The commands and their options."""
    parser = argparse.ArgumentParser(prog='polar-h10', description='Log everything a Polar H10 sends to CSV files, live.')
    parser.add_argument('--config', type=Path, default=DEFAULT_PATH, help='the config file (default: config.toml of the project)')
    sub = parser.add_subparsers(dest='command', required=True)
    record = sub.add_parser('record', help='record until q or Ctrl+C: the files, the live view and the API; options not '
                                           'given come from config.toml')
    record.add_argument('--hr', action=argparse.BooleanOptionalAction, default=None, help='heart rate and RR intervals')
    record.add_argument('--ecg', action=argparse.BooleanOptionalAction, default=None, help='ECG')
    record.add_argument('--ecg-rate', type=int, help='ECG samples per second')
    record.add_argument('--ecg-resolution', type=int, help='ECG bits')
    record.add_argument('--acc', action=argparse.BooleanOptionalAction, default=None, help='accelerometer')
    record.add_argument('--acc-rate', type=int, help='accelerometer samples per second')
    record.add_argument('--acc-resolution', type=int, help='accelerometer bits')
    record.add_argument('--acc-range', type=int, help='accelerometer range in g')
    mark = sub.add_parser('mark', help='add a marker to the running recording')
    mark.add_argument('label')
    sub.add_parser('capabilities', help='the settings the strap offers')
    sub.add_parser('sessions', help='all sessions, newest first')
    return parser


def request_of(args: argparse.Namespace, config: Config) -> Request:
    """The session of the command line; what it leaves out comes from config.toml."""
    base = default_request(config)

    def stream(kind: str, default: dict[str, int] | None) -> dict[str, int] | None:
        """One PMD stream: as in the config, off, on, or on with the settings given."""
        settings = {('sample_rate' if option == 'rate' else option): getattr(args, f'{kind}_{option}')
                    for option in OPTIONS[kind] if getattr(args, f'{kind}_{option}') is not None}
        on = getattr(args, kind)
        if on is False:
            return None
        if settings:
            return (default or {}) | settings
        return (default or {}) if on else default

    return Request(base.hr if args.hr is None else args.hr, stream('ecg', base.ecg), stream('acc', base.acc))


class QuietServer(uvicorn.Server):
    """uvicorn without its own signal handling: the record command handles Ctrl+C and SIGTERM."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """No signal handlers of uvicorn."""
        yield


def api_server(app: FastAPI) -> QuietServer:
    """The server of the API: warnings only, at most 2 s for open requests at the stop."""
    return QuietServer(uvicorn.Config(app, log_level='warning', timeout_graceful_shutdown=2))


async def stop_api(server: QuietServer, serving: asyncio.Task, hub: Hub) -> None:
    """Stop the API server and wait until it has stopped; the event streams end first, so it need not wait for them."""
    hub.close()
    server.should_exit = True
    await serving


def api_socket(config: Config) -> socket.socket | None:
    """The bound socket of the API; None when its port is taken, and the recording runs without the API."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((config.host, config.port))
    except OSError as error:
        sock.close()
        print(f'The API cannot use {config.host}:{config.port} ({error.strerror}). The recording runs without it.',
              file=sys.stderr)
        return None
    return sock


async def run_record(config: Config, request: Request, strap_factory: Callable[[], Strap]) -> int:
    """polar-h10 record: start the session, serve the API and show the view until q, Ctrl+C or SIGTERM, then stop and
    print the summary. 0 when the session ran and ended cleanly; 1 when it could not start or ended with an error."""
    finish_unfinished(config.storage_dir)
    hub = Hub()
    recorder = Recorder(config, strap_factory, hub)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # installed before the connect, so a Ctrl+C while the strap connects stops at once after the start
    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, stop.set)
    server: QuietServer | None = None
    serving: asyncio.Task | None = None
    summary = None
    try:
        try:
            await recorder.start(request)
        except (UnsupportedSetting, StrapError, ProtocolError, StorageError) as error:
            print(error, file=sys.stderr)
            return 1
        sock = api_socket(config)
        if sock is not None:
            server = api_server(create_app(recorder, hub, config))
            serving = asyncio.create_task(server.serve(sockets=[sock]))
        with live.keys(live.on_key(recorder, config.markers, stop.set)):
            await live.show(recorder, config.markers, stop)
    finally:
        if recorder.running:
            summary = await recorder.stop()
        if server is not None:
            await stop_api(server, serving, hub)
        for number in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(number)
    if summary is None:
        print(recorder.error or 'The session ended without a summary.', file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if summary.get('error') else 0


async def run_capabilities(config: Config, strap_factory: Callable[[], Strap]) -> int:
    """polar-h10 capabilities: a short connection and the settings the strap offers."""
    try:
        caps = await Recorder(config, strap_factory, Hub()).capabilities()
    except (StrapError, ProtocolError) as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(caps, indent=2))
    return 0


def run_mark(config: Config, label: str, client: httpx.Client | None) -> int:
    """polar-h10 mark: a marker in the running recording, through its API."""
    client = client or httpx.Client(base_url=f'http://{config.host}:{config.port}', timeout=TIMEOUT_S)
    try:
        response = client.post('/markers', json={'label': label, 'source': 'cli'})
    except httpx.ConnectError:
        print(NOT_RUNNING, file=sys.stderr)
        return 1
    if response.is_error:
        print(response.json().get('detail', response.text), file=sys.stderr)
        return 1
    print(json.dumps(response.json(), ensure_ascii=False))
    return 0


def main(argv: Sequence[str] | None = None, strap_factory: Callable[[], Strap] | None = None,
         client: httpx.Client | None = None) -> int:
    """Run a command; 0 when it worked, 1 for a strap, session or API error, 2 for a config error."""
    args = build_parser().parse_args(argv)
    try:
        config = load(args.config)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    factory = strap_factory or (lambda: BleakStrap(config))
    if args.command == 'record':
        return asyncio.run(run_record(config, request_of(args, config), factory))
    if args.command == 'capabilities':
        return asyncio.run(run_capabilities(config, factory))
    if args.command == 'sessions':
        print(json.dumps(list_sessions(config.storage_dir), indent=2, ensure_ascii=False))
        return 0
    return run_mark(config, args.label, client)
