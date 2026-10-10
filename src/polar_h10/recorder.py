"""One recording session at a time: connect to the strap, write every stream to its files and to the live hub, reconnect
after a dropout and go on in the same session, and stop with the summary. The strap comes from a factory, so tests use a
stand-in."""

import asyncio
import contextlib
import json
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from polar_h10 import __version__
from polar_h10.config import Config
from polar_h10.files import SessionFiles, finish, iso, write_start
from polar_h10.hub import Hub
from polar_h10.protocol import MIN_MTU, ProtocolError, UnsupportedFrame, parse_frame, parse_hr, sample_times
from polar_h10.strap import Strap, StrapError

PMD = ('ecg', 'acc')
SETTINGS = ('sample_rate', 'resolution', 'range')


class SessionRunning(Exception):
    """A session runs already."""


class NoSession(Exception):
    """No session runs."""


class StorageError(Exception):
    """The session folder cannot be made or written."""


class UnsupportedSetting(Exception):
    """A requested stream or setting the strap does not offer, with the values it does offer."""

    def __init__(self, message: str, valid: list[int]) -> None:
        """The message for the user and the valid values."""
        super().__init__(message)
        self.valid = valid


@dataclass(frozen=True)
class Request:
    """What a session records: heart rate on or off; ECG and accelerometer off (None) or on with the settings given."""

    hr: bool
    ecg: dict[str, int] | None
    acc: dict[str, int] | None


def default_request(config: Config) -> Request:
    """The session of config.toml's [streams]."""
    s = config.streams
    return Request(s.hr, {'sample_rate': s.ecg_sample_rate} if s.ecg else None,
                   {'sample_rate': s.acc_sample_rate, 'range': s.acc_range} if s.acc else None)


def _plural(count: int) -> str:
    """'1 try' or '3 tries'."""
    return f'{count} try' if count == 1 else f'{count} tries'


def resolve(request: Request, caps: dict[str, dict[str, list[int]]]) -> dict[str, dict[str, int]]:
    """The settings each stream starts with, checked against what the strap offers. A setting not given gets the
    strap's highest value: the best resolution, the widest range."""
    found: dict[str, dict[str, int]] = {'hr': {}} if request.hr else {}
    for kind in PMD:
        wanted = getattr(request, kind)
        if wanted is None:
            continue
        if kind not in caps:
            raise UnsupportedSetting(f'The strap does not offer {kind}.', [])
        offered = {name: values for name, values in caps[kind].items() if name in SETTINGS}
        for name in wanted:
            if name not in offered:
                raise UnsupportedSetting(f'{kind} has no setting {name}; it has {", ".join(offered)}.', [])
        chosen = {}
        for name, values in offered.items():
            value = wanted.get(name, max(values))
            if value not in values:
                listed = ', '.join(str(v) for v in values)
                raise UnsupportedSetting(f'The strap does not offer {kind} {name} {value}; it offers {listed}.', values)
            chosen[name] = value
        found[kind] = chosen
    return found


class Recorder:
    """The session of a `record` command and the strap state: idle, connecting, connected, dropout, stopping."""

    def __init__(self, config: Config, strap_factory: Callable[[], Strap], hub: Hub, clock: Callable[[], int] = time.time_ns,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """The factory makes a new strap, so a new scan, for each connection."""
        self.config, self.hub = config, hub
        self._factory, self._clock, self._sleep = strap_factory, clock, sleep
        self._lock = asyncio.Lock()
        self.state = 'idle'
        self.error = ''
        self._clear()

    def _clear(self) -> None:
        """No session."""
        self._strap: Strap | None = None
        self._files: SessionFiles | None = None
        self._session: dict | None = None
        self._caps: dict = {}
        self._device: dict | None = None
        self._battery: int | None = None
        self._last_hr: dict | None = None
        self._counts: dict[str, int] = {}
        self._markers = 0
        self._dropouts = 0
        self._failed = False
        self._lost = False
        self._reconnect: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        """Whether a session runs."""
        return self._session is not None

    async def capabilities(self) -> dict[str, dict[str, list[int]]]:
        """What the strap offers: from the running session, or from a short connection."""
        async with self._lock:
            if self._session is not None:
                return self._caps
            strap = self._factory()
            self.state = 'connecting'
            try:
                await strap.connect(lambda: None)
                return await strap.capabilities()
            finally:
                await self._goodbye(strap)
                self.state = 'idle'

    async def _goodbye(self, strap: Strap) -> None:
        """Disconnect a strap that may be gone already."""
        # after a failed connect or a dropout there may be no connection to end
        with contextlib.suppress(Exception):
            await strap.disconnect()

    async def start(self, request: Request) -> dict[str, str]:
        """Connect, check the settings, open the files and start every stream; the id and folder of the new session."""
        async with self._lock:
            if self._session is not None:
                raise SessionRunning('A session is already running. Stop it first.')
            self.error = ''
            strap = self._factory()
            self.state = 'connecting'
            # any failure after the connect ends the start cleanly: no open connection, no half session
            try:
                return await self._begin(strap, request)
            except BaseException:
                await self._abandon(strap)
                raise

    async def _begin(self, strap: Strap, request: Request) -> dict[str, str]:
        """Connect, check the settings, open the files and start every stream; the id and folder of the new session."""
        await strap.connect(self._dropped)
        caps = await strap.capabilities()
        settings = resolve(request, caps)
        device, battery = await strap.info(), await strap.battery()
        start_ns = self._clock()
        folder = self._folder(start_ns)
        config = {key: str(value) if isinstance(value, Path) else value for key, value in asdict(self.config).items()}
        try:
            self._files = SessionFiles(folder, set(settings))
            write_start(folder, {'id': folder.name, 'start': iso(start_ns), 'start_ns': start_ns, 'logger_version': __version__,
                                 'device': device, 'settings': settings, 'mtu': strap.mtu, 'battery_start': battery, 'config': config})
        except OSError as error:
            message = f'The session folder {folder} cannot be written: {error}. Check [storage] dir in config.toml.'
            raise StorageError(message) from error
        self._strap, self._caps, self._device, self._battery = strap, caps, device, battery
        self._session = {'id': folder.name, 'folder': str(folder), 'start_ns': start_ns, 'settings': settings}
        self._counts = {name: 0 for name in ('hr', 'rr', 'ecg', 'acc') if name in settings or (name == 'rr' and 'hr' in settings)}
        self._event('connected', f'{device.get("name", "")} {device.get("address", "")}'.strip())
        self._event('settings', json.dumps(settings))
        note = '' if strap.mtu >= MIN_MTU else f' (below {MIN_MTU}: ECG frames may not fit)'
        self._event('mtu', f'{strap.mtu}{note}')
        self._event('battery', str(battery))
        await self._start_streams(strap)
        self.state = 'connected'
        if self._lost:
            # the connection broke while the streams started: the dropout and reconnect path, not a dead session
            self._lost = False
            self._dropped()
        return {'id': folder.name, 'folder': str(folder)}

    async def _abandon(self, strap: Strap) -> None:
        """A start that failed: close the connection and the files, remove the folder it made (it holds no data), no session."""
        await self._goodbye(strap)
        if self._files is not None:
            with contextlib.suppress(OSError):
                self._files.close()
            shutil.rmtree(self._files.folder, ignore_errors=True)
        self._clear()
        self.state = 'idle'

    def _folder(self, start_ns: int) -> Path:
        """The session folder, named by the local start time; a second session in the same second gets _2."""
        name = datetime.fromtimestamp(start_ns / 1e9).strftime('%Y-%m-%d_%H-%M-%S')
        folder, n = self.config.storage_dir / name, 1
        while folder.exists():
            n += 1
            folder = self.config.storage_dir / f'{name}_{n}'
        return folder

    async def _start_streams(self, strap: Strap) -> None:
        """Heart rate, battery and the PMD streams of the session; a refused PMD stream is an error event, the others run."""
        settings = self._session['settings']
        if 'hr' in settings:
            await strap.start_hr(self._on_hr)
        await strap.start_battery(self._on_battery)
        for kind in PMD:
            if kind in settings:
                try:
                    await strap.start_pmd(kind, settings[kind], self._on_frame)
                except ProtocolError as error:
                    self._event('error', str(error))

    def _on_hr(self, data: bytes) -> None:
        """A heart rate notification: the files, the counts, the hub."""
        if self._files is None:
            return
        host_ns = self._clock()
        try:
            reading = parse_hr(data)
        except ProtocolError as error:
            self._event('error', str(error))
            return
        try:
            self._files.hr(host_ns, reading)
        except OSError as error:
            self._write_failed(error)
            return
        self._counts['hr'] += 1
        self._counts['rr'] += len(reading.rr_raw)
        self._last_hr = {'hr_bpm': reading.hr_bpm, 'rr_raw': reading.rr_raw}
        self.hub.publish('hr', {'host_ns': host_ns, 'hr_bpm': reading.hr_bpm, 'rr_raw': reading.rr_raw})

    def _on_frame(self, data: bytes) -> None:
        """A PMD frame: the strap time of each sample, the files, the counts, the hub."""
        if self._files is None:
            return
        host_ns = self._clock()
        try:
            frame = parse_frame(data)
        except UnsupportedFrame as error:
            self._event('unsupported_frame', str(error))
            return
        except ProtocolError as error:
            self._event('error', str(error))
            return
        settings = self._session['settings'].get(frame.kind)
        if settings is None or not frame.samples:
            return
        rate = settings['sample_rate']
        times = sample_times(frame.sensor_ns, len(frame.samples), rate)
        try:
            (self._files.ecg if frame.kind == 'ecg' else self._files.acc)(host_ns, times, frame.samples)
        except OSError as error:
            self._write_failed(error)
            return
        self._counts[frame.kind] += len(frame.samples)
        if frame.kind == 'ecg':
            self.hub.publish('ecg', {'host_ns': host_ns, 'sensor_ns_first': times[0], 'sample_rate': rate,
                                     'ecg_uv': [sample[0] for sample in frame.samples]})
        else:
            x, y, z = (list(axis) for axis in zip(*frame.samples, strict=True))
            self.hub.publish('acc', {'host_ns': host_ns, 'sensor_ns_first': times[0], 'sample_rate': rate,
                                     'x_mg': x, 'y_mg': y, 'z_mg': z})

    def _on_battery(self, level: int) -> None:
        """A battery notification."""
        self._battery = level
        self._event('battery', str(level))

    def _event(self, event: str, detail: str) -> None:
        """An event in events.csv, when the disk takes it; an error is also the status error."""
        if event == 'error':
            self.error = detail
        if self._files is not None:
            with contextlib.suppress(OSError):
                self._files.event(self._clock(), event, detail)

    def _write_failed(self, error: OSError) -> None:
        """The disk refused a write (full, or the folder is gone): stop the session once, and keep the reason."""
        if self._failed:
            return
        self._failed = True
        message = f'Writing the session files failed: {error}. The session was stopped.'
        self.error = message

        async def stop_and_keep_the_reason() -> None:
            """Stop; the reason stays in the status."""
            with contextlib.suppress(NoSession):
                await self.stop()
            self.error = message

        asyncio.get_running_loop().create_task(stop_and_keep_the_reason())

    def mark(self, label: str, source: str) -> None:
        """A marker in the running session."""
        if self._files is None:
            raise NoSession('No session is running: a marker needs one.')
        self._files.marker(self._clock(), label, source)
        self._markers += 1

    def _dropped(self) -> None:
        """The connection is lost: note it and reconnect in the background (not after our own disconnect). A loss while
        the streams start is kept, and the start or the reconnect try handles it when it is done."""
        if self._session is None or self.state in ('idle', 'stopping'):
            return
        if self.state != 'connected':
            self._lost = True
            return
        self.state = 'dropout'
        self._dropouts += 1
        self._event('dropout', '')
        self._reconnect = asyncio.get_running_loop().create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Try again with growing waits until connected, stopped, or out of tries."""
        # clean up the lost connection first: bleak kept its listener, and after the reconnect every message came twice
        await self._goodbye(self._strap)
        wait, tries = self.config.first_wait_s, 0
        while self._session is not None:
            await self._sleep(wait)
            tries += 1
            strap = self._factory()
            self._lost = False
            try:
                await strap.connect(self._dropped)
                await self._start_streams(strap)
                if self._lost:
                    raise StrapError('The connection was lost again while the streams started.')
            except asyncio.CancelledError:
                # a stop during this try: BlueZ keeps a connection the app does not close, so close it here
                await self._goodbye(strap)
                raise
            # any failure of one try (no strap, a refused connection, a Bluetooth error) leads to the next try
            except Exception as error:
                await self._goodbye(strap)
                self._event('reconnect_failed', f'try {tries}: {error}')
                if self.config.max_tries and tries >= self.config.max_tries:
                    self._event('error', f'Gave up reconnecting after {_plural(tries)}.')
                    return
                wait = min(wait * 2, self.config.max_wait_s)
                continue
            self._strap = strap
            self.state = 'connected'
            self._event('reconnected', f'after {_plural(tries)}')
            return

    async def stop(self) -> dict:
        """Stop the streams, disconnect, close the files and complete session.json; the completed session.json."""
        async with self._lock:
            if self._session is None:
                raise NoSession('No session is running.')
            self.state = 'stopping'
            if self._reconnect is not None:
                self._reconnect.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._reconnect
            strap = self._strap
            # after a dropout the strap may be gone: each step of the goodbye may fail without stopping the stop
            for kind in PMD:
                if kind in self._session['settings']:
                    with contextlib.suppress(Exception):
                        await strap.stop_pmd(kind)
            with contextlib.suppress(Exception):
                self._event('battery', str(await strap.battery()))
            await self._goodbye(strap)
            end_ns = self._clock()
            self._event('stopped', '')
            folder = Path(self._session['folder'])
            # a full disk may refuse the last flush and session.json; the session ends all the same
            with contextlib.suppress(OSError):
                self._files.close()
            try:
                summary = finish(folder, end_ns, cleanly=True)
            except OSError as error:
                summary = {'id': folder.name, 'error': f'session.json not completed: {error}'}
            self._clear()
            self.state = 'idle'
            return summary

    def status(self) -> dict:
        """The state for the API, the CLI and the live view."""
        session = None
        if self._session is not None:
            session = {'id': self._session['id'], 'folder': self._session['folder'], 'start': iso(self._session['start_ns']),
                       'seconds': round((self._clock() - self._session['start_ns']) / 1e9, 1),
                       'settings': self._session['settings'], 'samples': dict(self._counts), 'dropouts': self._dropouts,
                       'markers': self._markers}
        device = None if self._device is None else self._device | {'battery': self._battery}
        return {'version': __version__, 'state': self.state, 'device': device, 'last_hr': self._last_hr, 'session': session,
                'error': self.error}
