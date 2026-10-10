"""The recorder: a session writes every stream, refuses settings the strap does not offer, survives dropouts with
growing waits, keeps going after bad messages, stops when the disk refuses a write, and ignores what arrives after the stop."""

import asyncio
import csv
import dataclasses
import itertools
from collections.abc import Callable
from pathlib import Path

import pytest
from fakes import CAPS, FakeStrap, acc_frame, ecg_frame, hr_bytes

from polar_h10.config import Config
from polar_h10.files import SessionFiles
from polar_h10.hub import Hub
from polar_h10.protocol import ProtocolError
from polar_h10.recorder import NoSession, Recorder, Request, SessionRunning, StorageError, UnsupportedSetting, default_request
from polar_h10.strap import BUSY, NOT_FOUND, StrapBusy, StrapError, StrapNotFound


def clock() -> Callable[[], int]:
    """A PC clock that moves 1 ms at each reading."""
    counter = itertools.count(10**9, 10**6)
    return lambda: next(counter)


class Waits:
    """asyncio.sleep without the wait: it keeps the waits asked."""

    def __init__(self) -> None:
        """No waits yet."""
        self.asked: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Note the wait and give the other tasks a turn."""
        self.asked.append(seconds)
        await asyncio.sleep(0)


async def settle() -> None:
    """Let the reconnect task run to its end."""
    for _ in range(20):
        await asyncio.sleep(0)


def read(path: Path) -> list[list[str]]:
    """The rows of a CSV file without its header."""
    with path.open(newline='', encoding='utf-8') as handle:
        return list(csv.reader(handle))[1:]


def events(folder: str) -> list[str]:
    """The event names of a session."""
    return [row[1] for row in read(Path(folder) / 'events.csv')]


async def test_a_session_writes_every_stream_and_ends_with_a_summary(config: Config) -> None:
    fake = FakeStrap(hr=[hr_bytes(86, (700, 709))], frames={'ecg': [ecg_frame(10**9, [10, 20, 30])],
                                                             'acc': [acc_frame(2 * 10**9, [(1, 2, 3)])]})
    hub = Hub()
    queue = hub.subscribe()
    recorder = Recorder(config, lambda: fake, hub, clock=clock())
    folder = Path((await recorder.start(default_request(config)))['folder'])
    assert fake.started == [('ecg', {'sample_rate': 130, 'resolution': 14}),
                            ('acc', {'sample_rate': 200, 'resolution': 16, 'range': 8})]
    assert [row[1:] for row in read(folder / 'ecg.csv')] == [[str(10**9 - 15384615), '10'], [str(10**9 - 7692308), '20'],
                                                            [str(10**9), '30']]
    assert [row[1:] for row in read(folder / 'acc.csv')] == [['2000000000', '1', '2', '3']]
    assert [row[1:] for row in read(folder / 'rr.csv')] == [['700', '683.59375'], ['709', '692.3828125']]
    status = recorder.status()
    assert (status['state'], status['session']['samples']) == ('connected', {'hr': 1, 'rr': 2, 'ecg': 3, 'acc': 1})
    assert status['device']['battery'] == 90 and status['last_hr'] == {'hr_bpm': 86, 'rr_raw': [700, 709]}
    published = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [event for event, _ in published] == ['hr', 'ecg', 'acc']
    assert published[1][1] | {'host_ns': 0} == {'host_ns': 0, 'sensor_ns_first': 10**9 - 15384615, 'sample_rate': 130,
                                                'ecg_uv': [10, 20, 30]}
    assert published[2][1]['x_mg'] == [1] and published[2][1]['z_mg'] == [3]
    summary = await recorder.stop()
    assert summary['samples'] == {'hr': 1, 'rr': 2, 'ecg': 3, 'acc': 1} and summary['ended_cleanly'] is True
    assert summary['device']['firmware'] == '3.1.1' and summary['battery_start'] == 90 and summary['mtu'] == 247
    assert fake.stopped == ['ecg', 'acc'] and not fake.connected and recorder.state == 'idle' and recorder.status()['session'] is None
    assert events(str(folder)) == ['connected', 'settings', 'mtu', 'battery', 'battery', 'stopped']


async def test_an_unsupported_setting_is_refused_with_the_valid_values(config: Config) -> None:
    fake = FakeStrap()
    recorder = Recorder(config, lambda: fake, Hub(), clock=clock())
    with pytest.raises(UnsupportedSetting, match='offers 25, 50, 100, 200') as caught:
        await recorder.start(Request(hr=True, ecg=None, acc={'sample_rate': 300}))
    assert caught.value.valid == [25, 50, 100, 200]
    with pytest.raises(UnsupportedSetting, match='ecg has no setting range'):
        await recorder.start(Request(hr=False, ecg={'range': 8}, acc=None))
    recorder = Recorder(config, lambda: FakeStrap(caps={'hr': {}, 'ecg': CAPS['ecg']}), Hub(), clock=clock())
    with pytest.raises(UnsupportedSetting, match='does not offer acc'):
        await recorder.start(Request(hr=True, ecg=None, acc={}))
    assert recorder.state == 'idle' and not fake.connected and not config.storage_dir.exists()


async def test_a_second_start_while_a_session_runs_is_refused(config: Config) -> None:
    recorder = Recorder(config, FakeStrap, Hub(), clock=clock())
    await recorder.start(default_request(config))
    with pytest.raises(SessionRunning):
        await recorder.start(default_request(config))
    await recorder.stop()
    with pytest.raises(NoSession):
        await recorder.stop()


async def test_a_strap_that_cannot_connect_leaves_no_session(config: Config) -> None:
    recorder = Recorder(config, lambda: FakeStrap(fail=StrapBusy(BUSY)), Hub(), clock=clock())
    with pytest.raises(StrapBusy, match='another app'):
        await recorder.start(default_request(config))
    assert recorder.state == 'idle' and recorder.status()['session'] is None and not config.storage_dir.exists()


async def test_a_dropout_reconnects_and_continues_the_same_session(config: Config) -> None:
    first, second = FakeStrap(), FakeStrap(hr=[hr_bytes(90)])
    straps = iter([first, second])
    waits = Waits()
    recorder = Recorder(config, lambda: next(straps), Hub(), clock=clock(), sleep=waits)
    folder = (await recorder.start(default_request(config)))['folder']
    first.drop()
    assert recorder.state == 'dropout'
    await settle()
    assert recorder.state == 'connected' and waits.asked == [2.0]
    assert second.started == first.started, 'the same streams with the same settings'
    summary = await recorder.stop()
    assert summary['reconnects'] == 1 and len(summary['dropouts']) == 1 and summary['samples']['hr'] == 1
    assert events(folder)[4:6] == ['dropout', 'reconnected'] and summary['id'] == Path(folder).name


async def test_a_dropout_cleans_up_the_lost_connection_before_reconnecting(config: Config) -> None:
    first, second = FakeStrap(), FakeStrap()
    straps = iter([first, second])
    recorder = Recorder(config, lambda: next(straps), Hub(), clock=clock(), sleep=Waits())
    await recorder.start(default_request(config))
    first.drop()
    await settle()
    # on the real strap (2026-10-09) bleak kept a second listener for a lost connection that was not cleaned up:
    # after the reconnect every message came twice
    assert first.disconnects == 1 and recorder.state == 'connected'
    await recorder.stop()
    assert first.disconnects == 1 and second.disconnects == 1


class HangingStrap(FakeStrap):
    """Connects, then hangs while it starts a measurement, as a slow radio does."""

    async def start_pmd(self, kind: str, settings: dict[str, int], on_frame: Callable[[bytes], None]) -> None:
        """Wait for ever."""
        await asyncio.Event().wait()


async def test_a_stop_during_a_reconnect_try_closes_that_connection(config: Config) -> None:
    first, second = FakeStrap(), HangingStrap()
    straps = iter([first, second])
    recorder = Recorder(config, lambda: next(straps), Hub(), clock=clock(), sleep=Waits())
    await recorder.start(default_request(config))
    first.drop()
    await settle()
    assert second.connected and recorder.state == 'dropout', 'the try hangs after its connect'
    await recorder.stop()
    # BlueZ keeps a connection the app does not close: the strap stays held, and the next session gets doubles
    assert not second.connected and second.disconnects == 1 and recorder.state == 'idle'


class HrRefusingStrap(FakeStrap):
    """Connects, then fails when the heart rate starts, as a connection that breaks at that moment does."""

    async def start_hr(self, on_hr: Callable[[bytes], None]) -> None:
        """Fail."""
        raise StrapError('Bluetooth error: Not connected')


async def test_a_session_folder_that_cannot_be_written_leaves_no_half_session(config: Config, tmp_path: Path) -> None:
    (tmp_path / 'a-file').write_text('')
    fake = FakeStrap()
    recorder = Recorder(dataclasses.replace(config, storage_dir=tmp_path / 'a-file' / 'sessions'), lambda: fake, Hub(), clock=clock())
    with pytest.raises(StorageError, match='cannot be written'):
        await recorder.start(default_request(config))
    assert not fake.connected and recorder.state == 'idle' and not recorder.running


async def test_a_failure_while_the_streams_start_leaves_no_half_session(config: Config) -> None:
    good = FakeStrap()
    straps = iter([HrRefusingStrap(), good])
    recorder = Recorder(config, lambda: next(straps), Hub(), clock=clock())
    with pytest.raises(StrapError, match='Not connected'):
        await recorder.start(default_request(config))
    assert recorder.state == 'idle' and not recorder.running and list(config.storage_dir.iterdir()) == [], 'no empty folder'
    await recorder.start(default_request(config))
    assert recorder.state == 'connected' and good.connected, 'the next start works'
    await recorder.stop()


class DroppingStrap(FakeStrap):
    """Loses the connection right after it starts the accelerometer, as at the edge of the radio range."""

    async def start_pmd(self, kind: str, settings: dict[str, int], on_frame: Callable[[bytes], None]) -> None:
        """Start, then drop after the accelerometer."""
        await super().start_pmd(kind, settings, on_frame)
        if kind == 'acc':
            self.drop()


async def test_a_drop_while_the_streams_start_leads_to_a_reconnect(config: Config) -> None:
    first, second = DroppingStrap(), FakeStrap()
    straps = iter([first, second])
    recorder = Recorder(config, lambda: next(straps), Hub(), clock=clock(), sleep=Waits())
    folder = (await recorder.start(default_request(config)))['folder']
    await settle()
    assert recorder.state == 'connected' and second.connected, 'not a session that looks alive and records nothing'
    assert events(folder)[4:6] == ['dropout', 'reconnected']
    await recorder.stop()


async def test_a_drop_while_a_reconnect_try_starts_the_streams_leads_to_the_next_try(config: Config) -> None:
    first, third = FakeStrap(), FakeStrap()
    straps = iter([first, DroppingStrap(), third])
    recorder = Recorder(config, lambda: next(straps), Hub(), clock=clock(), sleep=Waits())
    folder = (await recorder.start(default_request(config)))['folder']
    first.drop()
    await settle()
    assert recorder.state == 'connected' and third.connected
    assert events(folder)[4:7] == ['dropout', 'reconnect_failed', 'reconnected']
    await recorder.stop()


async def test_failed_reconnects_wait_longer_each_time(config: Config) -> None:
    first, third = FakeStrap(), FakeStrap()
    straps = iter([first, FakeStrap(fail=StrapNotFound(NOT_FOUND)), FakeStrap(fail=StrapBusy(BUSY)), third])
    waits = Waits()
    recorder = Recorder(config, lambda: next(straps), Hub(), clock=clock(), sleep=waits)
    folder = (await recorder.start(default_request(config)))['folder']
    first.drop()
    await settle()
    assert recorder.state == 'connected' and waits.asked == [2.0, 4.0, 8.0] and third.connected
    assert events(folder).count('reconnect_failed') == 2
    await recorder.stop()


async def test_reconnecting_stops_after_max_tries_and_the_session_can_still_stop(config: Config) -> None:
    first = FakeStrap()
    straps = iter([first, FakeStrap(fail=StrapNotFound(NOT_FOUND))])
    recorder = Recorder(dataclasses.replace(config, max_tries=1), lambda: next(straps), Hub(), clock=clock(), sleep=Waits())
    await recorder.start(default_request(config))
    first.drop()
    await settle()
    assert recorder.state == 'dropout' and recorder.error == 'Gave up reconnecting after 1 try.'
    summary = await recorder.stop()
    assert len(summary['dropouts']) == 1 and summary['reconnects'] == 0


async def test_a_refused_measurement_is_an_error_event_and_the_others_run(config: Config) -> None:
    fake = FakeStrap(pmd_errors={'acc': ProtocolError('The strap refused acc: invalid range.')})
    recorder = Recorder(config, lambda: fake, Hub(), clock=clock())
    folder = (await recorder.start(default_request(config)))['folder']
    assert fake.started == [('ecg', {'sample_rate': 130, 'resolution': 14})] and recorder.state == 'connected'
    assert recorder.error == 'The strap refused acc: invalid range.' and 'error' in events(folder)
    await recorder.stop()


async def test_bad_messages_become_events_and_the_session_goes_on(config: Config) -> None:
    fake = FakeStrap()
    recorder = Recorder(config, lambda: fake, Hub(), clock=clock())
    folder = (await recorder.start(default_request(config)))['folder']
    fake.send('ecg', bytes([0]) + (10**9).to_bytes(8, 'little') + bytes([0x80, 1, 2, 3]))
    fake.send('hr', b'\x00')
    fake.send('ecg', ecg_frame(10**9, []))
    fake.send('ecg', ecg_frame(2 * 10**9, [5]))
    assert events(folder)[-2:] == ['unsupported_frame', 'error']
    assert [row[1:] for row in read(Path(folder) / 'ecg.csv')] == [['2000000000', '5']], 'an empty frame adds nothing'
    assert recorder.state == 'connected'
    await recorder.stop()


async def test_a_late_notification_after_the_stop_is_ignored(config: Config) -> None:
    fake = FakeStrap()
    recorder = Recorder(config, lambda: fake, Hub(), clock=clock())
    folder = (await recorder.start(default_request(config)))['folder']
    await recorder.stop()
    fake.send('hr', hr_bytes(70, (800,)))
    fake.send('ecg', ecg_frame(10**9, [1]))
    fake.drop()
    assert read(Path(folder) / 'hr.csv') == [] and recorder.state == 'idle'


async def test_a_second_session_in_the_same_second_gets_its_own_folder(config: Config) -> None:
    recorder = Recorder(config, FakeStrap, Hub(), clock=lambda: 10**9)
    first = await recorder.start(default_request(config))
    await recorder.stop()
    second = await recorder.start(default_request(config))
    await recorder.stop()
    assert second['folder'] == first['folder'] + '_2'


async def test_markers_need_a_session(config: Config) -> None:
    recorder = Recorder(config, FakeStrap, Hub(), clock=clock())
    with pytest.raises(NoSession):
        recorder.mark('angina onset', 'key')
    folder = (await recorder.start(default_request(config)))['folder']
    recorder.mark('angina onset', 'key')
    assert [row[1:] for row in read(Path(folder) / 'markers.csv')] == [['angina onset', 'key']]
    assert recorder.status()['session']['markers'] == 1
    assert (await recorder.stop())['markers'] == 1


async def test_a_write_error_stops_the_session_and_says_why(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeStrap()
    recorder = Recorder(config, lambda: fake, Hub(), clock=clock())
    await recorder.start(default_request(config))

    def full(*_: object) -> None:
        """A disk without room."""
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(SessionFiles, 'ecg', full)
    fake.send('ecg', ecg_frame(10**9, [1]))
    fake.send('ecg', ecg_frame(2 * 10**9, [2]))
    await settle()
    assert recorder.state == 'idle' and not recorder.running and not fake.connected
    assert 'No space left on device' in recorder.error and 'The session was stopped' in recorder.error


async def test_capabilities_connect_briefly_when_idle_and_come_from_the_session_otherwise(config: Config) -> None:
    made: list[FakeStrap] = []

    def factory() -> FakeStrap:
        """A new stand-in strap, kept."""
        made.append(FakeStrap())
        return made[-1]

    recorder = Recorder(config, factory, Hub(), clock=clock())
    assert await recorder.capabilities() == CAPS and not made[0].connected and recorder.state == 'idle'
    await recorder.start(default_request(config))
    assert await recorder.capabilities() == CAPS and len(made) == 2, 'no new connection during a session'
    await recorder.stop()
