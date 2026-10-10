"""The live view of `polar-h10 record`: the strap, the heart rate, the session and the keys in the panel; q stops the
recording and a marker key marks once the session runs."""

from fakes import FakeStrap
from rich.console import Console
from rich.table import Table

from polar_h10.config import Config
from polar_h10.hub import Hub
from polar_h10.live import on_key, panel
from polar_h10.recorder import Recorder, default_request


def text(table: Table) -> str:
    """The panel as plain text."""
    console = Console(record=True, width=120)
    console.print(table)
    return console.export_text()


def test_the_panel_shows_strap_heart_rate_session_and_marker_keys() -> None:
    status = {'state': 'connected', 'device': {'name': 'Polar H10 E813952E', 'battery': 90},
              'last_hr': {'hr_bpm': 86, 'rr_raw': [717]},
              'session': {'id': '2026-10-09_14-30-05', 'seconds': 65.2, 'samples': {'hr': 65, 'ecg': 8450}, 'dropouts': 0,
                          'markers': 1, 'folder': '/data/x'}, 'error': ''}
    shown = text(panel(status, {'1': 'angina onset'}))
    for part in ('connected', 'Polar H10 E813952E, battery 90 %', '86 bpm, last RR 700 ms', '2026-10-09_14-30-05, 65 s',
                 'hr 65, ecg 8450', 'Keys: 1 angina onset, q stop'):
        assert part in shown


def test_the_panel_without_a_session_shows_the_state_and_the_error() -> None:
    shown = text(panel({'state': 'connecting', 'device': None, 'last_hr': None, 'session': None, 'error': 'Strap not found.'},
                       {}))
    assert 'connecting' in shown and 'Strap not found.' in shown and 'Keys: q stop' in shown


async def test_q_stops_and_a_marker_key_marks_in_a_running_session(config: Config) -> None:
    recorder = Recorder(config, FakeStrap, Hub())
    stopped: list[bool] = []
    press = on_key(recorder, {'1': 'angina onset'}, lambda: stopped.append(True))
    press('1')
    await recorder.start(default_request(config))
    press('1')
    press('x')
    assert recorder.status()['session']['markers'] == 1 and not stopped, 'a key before the session is ignored'
    press('q')
    assert stopped == [True]
    await recorder.stop()
