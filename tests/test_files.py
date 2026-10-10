"""The files of a session: a CSV per stream with its header, lines on disk at once, markers and events kept exactly,
and session.json completed from the files, also after a crash."""

import csv
import json
from pathlib import Path

import pytest

from polar_h10.files import SessionFiles, finish, finish_unfinished, heart_summary, list_sessions, read_session, write_start
from polar_h10.protocol import HrReading


def read(path: Path) -> list[list[str]]:
    """All rows of a CSV file, the header first."""
    with path.open(newline='', encoding='utf-8') as handle:
        return list(csv.reader(handle))


def test_each_stream_gets_its_file_with_a_header_and_lines_appear_at_once(tmp_path: Path) -> None:
    files = SessionFiles(tmp_path / 's', {'hr', 'ecg'})
    assert sorted(p.name for p in (tmp_path / 's').iterdir()) == ['ecg.csv', 'events.csv', 'hr.csv', 'markers.csv', 'rr.csv']
    files.hr(5, HrReading(86, None, [700, 717]))
    files.hr(6, HrReading(87, True, []))
    files.ecg(7, [10, 20], [(-5,), (7,)])
    assert read(tmp_path / 's' / 'hr.csv') == [['host_ns', 'hr_bpm', 'contact'], ['5', '86', ''], ['6', '87', '1']], 'before close'
    assert read(tmp_path / 's' / 'rr.csv')[1:] == [['5', '700', '683.59375'], ['5', '717', '700.1953125']]
    assert read(tmp_path / 's' / 'ecg.csv') == [['host_ns', 'sensor_ns', 'ecg_uv'], ['7', '10', '-5'], ['7', '20', '7']]
    files.close()


def test_acc_markers_and_events_keep_their_values(tmp_path: Path) -> None:
    files = SessionFiles(tmp_path / 's', {'acc'})
    files.acc(1, [100], [(-1000, 0, 1000)])
    files.marker(2, 'angina, after stairs\nsecond line', 'key')
    files.event(3, 'battery', '88')
    files.close()
    assert read(tmp_path / 's' / 'acc.csv') == [['host_ns', 'sensor_ns', 'x_mg', 'y_mg', 'z_mg'], ['1', '100', '-1000', '0', '1000']]
    assert read(tmp_path / 's' / 'markers.csv')[1:] == [['2', 'angina, after stairs\nsecond line', 'key']]
    assert read(tmp_path / 's' / 'events.csv')[1:] == [['3', 'battery', '88']]
    assert not (tmp_path / 's' / 'hr.csv').exists(), 'a stream that is off has no file'


def test_a_session_folder_is_never_reused(tmp_path: Path) -> None:
    SessionFiles(tmp_path / 's', {'hr'}).close()
    with pytest.raises(FileExistsError):
        SessionFiles(tmp_path / 's', {'hr'})


def test_the_heart_summary_by_hand() -> None:
    summary = heart_summary([60, 70, 80], [800.0, 810.0, 790.0, 800.0])
    assert summary == {'hr_min': 60, 'hr_mean': 70.0, 'hr_max': 80, 'rr_count': 4, 'rmssd_ms': 14.14, 'sdnn_ms': 8.16}
    assert heart_summary([], [800.0]) == {'hr_min': None, 'hr_mean': None, 'hr_max': None, 'rr_count': 1, 'rmssd_ms': None,
                                          'sdnn_ms': None}


def test_the_summary_counts_samples_dropouts_markers_and_the_heart(tmp_path: Path) -> None:
    folder = tmp_path / 's'
    files = SessionFiles(folder, {'hr'})
    write_start(folder, {'id': 's', 'start': 'x', 'start_ns': 1 * 10**9, 'settings': {'hr': {}}})
    for n, bpm in enumerate([60, 70, 80]):
        files.hr((2 + n) * 10**9, HrReading(bpm, None, [1024]))
    files.event(6 * 10**9, 'dropout')
    files.marker(7 * 10**9, 'angina onset', 'cli')
    files.event(8 * 10**9, 'reconnected', 'after 1 try')
    files.event(9 * 10**9, 'battery', '88')
    files.event(9 * 10**9, 'dropout')
    files.close()
    info = finish(folder, 10 * 10**9)
    found = (info['duration_s'], info['samples'], info['reconnects'], info['battery_end'], info['markers'])
    assert found == (9.0, {'hr': 3, 'rr': 3}, 1, 88, 1)
    assert info['dropouts'] == [{'start_ns': 6 * 10**9, 'end_ns': 8 * 10**9, 'seconds': 2.0},
                                {'start_ns': 9 * 10**9, 'end_ns': 10 * 10**9, 'seconds': 1.0}], 'the last one lasts until the end'
    assert info['heart']['hr_mean'] == 70.0 and info['heart']['rr_count'] == 3 and info['ended_cleanly'] is True
    assert info['end_ns'] == 10 * 10**9 and info['end'].startswith('19')


def test_a_session_without_a_stop_is_completed_from_its_files(tmp_path: Path) -> None:
    folder = tmp_path / 'sessions' / '2026-10-09_10-00-00'
    files = SessionFiles(folder, {'hr', 'ecg'})
    write_start(folder, {'id': folder.name, 'start': 'x', 'start_ns': 10**9, 'settings': {'hr': {}, 'ecg': {'sample_rate': 130}}})
    files.hr(2 * 10**9, HrReading(70, None, [800]))
    files.ecg(3 * 10**9, [5, 6], [(1,), (2,)])
    files.close()
    with (folder / 'ecg.csv').open('a') as handle:
        handle.write('4000000000,7')
    assert finish_unfinished(tmp_path / 'sessions') == [folder.name]
    info = read_session(tmp_path / 'sessions', folder.name)
    assert (info['ended_cleanly'], info['end_ns'], info['samples']['ecg']) == (False, 3 * 10**9, 2), 'the cut-off line is left out'
    assert info['clock']['ecg'] == {'host_ns': 3 * 10**9, 'sensor_ns': 5}
    assert finish_unfinished(tmp_path / 'sessions') == [], 'done once'


def test_sessions_are_listed_newest_first_and_read_by_their_id(tmp_path: Path) -> None:
    for name in ('2026-10-09_10-00-00', '2026-10-09_11-00-00'):
        files = SessionFiles(tmp_path / name, {'hr'})
        write_start(tmp_path / name, {'id': name, 'start': name, 'start_ns': 10**9, 'settings': {'hr': {}}})
        files.close()
        finish(tmp_path / name, 2 * 10**9)
    assert [s['id'] for s in list_sessions(tmp_path)] == ['2026-10-09_11-00-00', '2026-10-09_10-00-00']
    assert list_sessions(tmp_path)[0] == {'id': '2026-10-09_11-00-00', 'start': '2026-10-09_11-00-00', 'duration_s': 1.0,
                                          'streams': ['hr'], 'ended_cleanly': True}
    assert read_session(tmp_path, '2026-10-09_10-00-00')['id'] == '2026-10-09_10-00-00'
    assert read_session(tmp_path, '../etc') is None and read_session(tmp_path, '2020-01-01_00-00-00') is None
    assert list_sessions(tmp_path / 'none') == []


def test_a_damaged_session_does_not_stop_the_others(tmp_path: Path) -> None:
    empty = tmp_path / '2026-10-09_09-00-00'
    empty.mkdir()
    (empty / 'session.json').write_text('')
    cut = tmp_path / '2026-10-09_10-00-00'
    files = SessionFiles(cut, {'hr'})
    write_start(cut, {'id': cut.name, 'start': 'x', 'start_ns': 10**9, 'settings': {'hr': {}}})
    files.hr(2 * 10**9, HrReading(70, None, [800]))
    files.close()
    with (cut / 'rr.csv').open('a') as handle:
        handle.write('3000000000,700,\r\n')
    assert finish_unfinished(tmp_path) == [cut.name], 'the empty session.json is skipped, the cut RR line too'
    assert read_session(tmp_path, cut.name)['heart']['rr_count'] == 1
    listed = {s['id']: s for s in list_sessions(tmp_path)}
    assert 'cannot be read' in listed[empty.name]['error'] and listed[cut.name]['ended_cleanly'] is False
    assert 'cannot be read' in read_session(tmp_path, empty.name)['error']


def test_a_failed_write_keeps_the_old_session_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_start(tmp_path, {'id': 'old'})

    def half(self: Path, text: str, *args: object, **kwargs: object) -> None:
        """A disk that fills up halfway."""
        self.write_bytes(text[:int(len(text) / 2)].encode())
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(Path, 'write_text', half)
    with pytest.raises(OSError):
        write_start(tmp_path, {'id': 'new', 'settings': {'hr': {}}})
    monkeypatch.undo()
    assert read_session_json(tmp_path) == {'id': 'old'}


def read_session_json(folder: Path) -> dict:
    """The session.json of a folder."""
    return json.loads((folder / 'session.json').read_text())
