"""The files of a session, written live: a CSV per stream with its header, lines added as data arrives and flushed
after each batch; session.json at the start and completed at the end with the summary. The summary comes from the files,
so the same code completes a session that ended without a stop."""

import contextlib
import csv
import json
import logging
import math
import os
import re
import statistics
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, TextIO

from polar_h10.protocol import HrReading

HEADERS = {'hr': ['host_ns', 'hr_bpm', 'contact'], 'rr': ['host_ns', 'rr_raw', 'rr_ms'], 'ecg': ['host_ns', 'sensor_ns', 'ecg_uv'],
           'acc': ['host_ns', 'sensor_ns', 'x_mg', 'y_mg', 'z_mg'], 'markers': ['host_ns', 'label', 'source'],
           'events': ['host_ns', 'event', 'detail']}
DATA = ('hr', 'rr', 'ecg', 'acc')
log = logging.getLogger(__name__)
SESSION_ID = re.compile(r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(_\d+)?')
TAIL_BYTES = 4096


class SessionFiles:
    """The open CSV files of one session."""

    def __init__(self, folder: Path, streams: set[str]) -> None:
        """Make a new folder with the files of the streams that are on (heart rate brings RR), markers and events."""
        folder.mkdir(parents=True)
        self.folder = folder
        names = [name for name in DATA if name in streams or (name == 'rr' and 'hr' in streams)] + ['markers', 'events']
        self._handles: dict[str, TextIO] = {}
        self._writers: dict[str, Any] = {}
        for name in names:
            handle = (folder / f'{name}.csv').open('w', newline='', encoding='utf-8')
            self._handles[name], self._writers[name] = handle, csv.writer(handle)
            self._write(name, [HEADERS[name]])

    def _write(self, name: str, rows: list[list]) -> None:
        """Add rows and put them on disk at once."""
        self._writers[name].writerows(rows)
        self._handles[name].flush()

    def hr(self, host_ns: int, reading: HrReading) -> None:
        """A heart rate notification, and its RR intervals in rr.csv."""
        self._write('hr', [[host_ns, reading.hr_bpm, '' if reading.contact is None else int(reading.contact)]])
        if reading.rr_raw:
            self._write('rr', [[host_ns, raw, raw * 1000 / 1024] for raw in reading.rr_raw])

    def ecg(self, host_ns: int, times: list[int], samples: list[tuple[int, ...]]) -> None:
        """ECG samples with their strap times."""
        self._write('ecg', [[host_ns, t, s[0]] for t, s in zip(times, samples, strict=True)])

    def acc(self, host_ns: int, times: list[int], samples: list[tuple[int, ...]]) -> None:
        """Accelerometer samples (x, y, z) with their strap times."""
        self._write('acc', [[host_ns, t, *s] for t, s in zip(times, samples, strict=True)])

    def marker(self, host_ns: int, label: str, source: str) -> None:
        """A marker."""
        self._write('markers', [[host_ns, label, source]])

    def event(self, host_ns: int, event: str, detail: str = '') -> None:
        """An event of the session."""
        self._write('events', [[host_ns, event, detail]])

    def close(self) -> None:
        """Close every file."""
        for handle in self._handles.values():
            handle.close()


def iso(ns: int) -> str:
    """A time in ns since 1970 as local ISO time with its offset."""
    return datetime.fromtimestamp(ns / 1e9).astimezone().isoformat(timespec='seconds')


def write_start(folder: Path, info: dict) -> None:
    """session.json, whole or not at all: a crash or a full disk during the write keeps the old file."""
    temporary = folder / 'session.json.tmp'
    temporary.write_text(json.dumps(info, indent=2, default=str))
    os.replace(temporary, folder / 'session.json')


def _numbers(rows: list[list[str]], column: int, kind: type) -> list:
    """The values of one column that read as numbers; a line cut off by a crash is left out."""
    found = []
    for row in rows:
        with contextlib.suppress(ValueError):
            found.append(kind(row[column]))
    return found


def _rows(path: Path, fields: int) -> list[list[str]]:
    """The complete rows of a small CSV file (heart rate, RR, markers, events), without its header."""
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8') as handle:
        rows = list(csv.reader(handle))[1:]
    return [row for row in rows if len(row) == fields and row[0].isdigit()]


def _scan(path: Path, fields: int) -> tuple[int, list[str] | None, list[str] | None] | None:
    """The number of rows and the first and last complete row of a data CSV, read without loading it; None without the file."""
    if not path.exists():
        return None
    with path.open('rb') as handle:
        lines = sum(chunk.count(b'\n') for chunk in iter(lambda: handle.read(1 << 20), b''))
        handle.seek(0)
        handle.readline()
        first = handle.readline().decode().strip().split(',')
        end = handle.seek(0, 2)
        handle.seek(max(0, end - TAIL_BYTES))
        tail = [line.split(',') for line in handle.read().decode(errors='replace').splitlines()]
    count = max(0, lines - 1)
    if not count:
        return 0, None, None
    # a line cut off by a crash has too few fields; the header and a line cut at the read start fail the digit test
    complete = [row for row in tail if len(row) == fields and row[0].isdigit()]
    return count, first, complete[-1] if complete else first


def heart_summary(hr: list[int], rr_ms: list[float]) -> dict:
    """Heart rate min, mean and max; RMSSD and SDNN of the RR intervals, without artefact correction."""
    diffs = [b - a for a, b in pairwise(rr_ms)]
    return {'hr_min': min(hr) if hr else None, 'hr_mean': round(statistics.fmean(hr), 1) if hr else None,
            'hr_max': max(hr) if hr else None, 'rr_count': len(rr_ms),
            'rmssd_ms': round(math.sqrt(statistics.fmean([d * d for d in diffs])), 2) if diffs else None,
            'sdnn_ms': round(statistics.stdev(rr_ms), 2) if len(rr_ms) > 1 else None}


def finish(folder: Path, end_ns: int | None = None, cleanly: bool = True) -> dict:
    """Complete session.json from the files: end, duration, samples, dropouts, reconnects, battery, markers, clock and
    heart. Without an end time, the last time in the files is the end."""
    info = json.loads((folder / 'session.json').read_text())
    samples: dict[str, int] = {}
    last_ns: list[int] = []
    clock: dict[str, dict[str, int]] = {}
    for name in DATA:
        scanned = _scan(folder / f'{name}.csv', len(HEADERS[name]))
        if scanned is None:
            continue
        samples[name], first, last = scanned
        if last:
            last_ns.append(int(last[0]))
        if first and name in ('ecg', 'acc'):
            with contextlib.suppress(ValueError):
                clock[name] = {'host_ns': int(first[0]), 'sensor_ns': int(first[1])}
    events = _rows(folder / 'events.csv', 3)
    end = end_ns or max([*last_ns, *(int(row[0]) for row in events)], default=info['start_ns'])
    dropouts, since = [], None
    for host_ns, event, _ in events:
        if event == 'dropout':
            since = int(host_ns)
        elif event == 'reconnected' and since is not None:
            dropouts.append({'start_ns': since, 'end_ns': int(host_ns), 'seconds': round((int(host_ns) - since) / 1e9, 3)})
            since = None
    if since is not None:
        dropouts.append({'start_ns': since, 'end_ns': end, 'seconds': round((end - since) / 1e9, 3)})
    batteries = [int(detail) for _, event, detail in events if event == 'battery' and detail.isdigit()]
    hr = _numbers(_rows(folder / 'hr.csv', 3), 1, int)
    rr = _numbers(_rows(folder / 'rr.csv', 3), 2, float)
    info |= {'end': iso(end), 'end_ns': end, 'duration_s': round((end - info['start_ns']) / 1e9, 3), 'samples': samples,
             'dropouts': dropouts, 'reconnects': sum(1 for row in events if row[1] == 'reconnected'),
             'battery_end': batteries[-1] if batteries else None, 'markers': len(_rows(folder / 'markers.csv', 3)), 'clock': clock,
             'heart': heart_summary(hr, rr), 'ended_cleanly': cleanly}
    write_start(folder, info)
    return info


def finish_unfinished(storage_dir: Path) -> list[str]:
    """Complete every session that has no end (the logger stopped without a stop); their ids. A damaged session is
    logged and left as it is, so it never stops a recording from starting."""
    done = []
    for path in sorted(storage_dir.glob('*/session.json')):
        try:
            if 'end_ns' not in json.loads(path.read_text()):
                finish(path.parent, None, cleanly=False)
                done.append(path.parent.name)
        except (OSError, ValueError, KeyError) as error:
            log.warning('Session %s cannot be completed: %s', path.parent, error)
    return done


def list_sessions(storage_dir: Path) -> list[dict]:
    """All sessions, newest first: id, start, duration, streams, clean end."""
    found = []
    for path in sorted(storage_dir.glob('*/session.json'), reverse=True):
        try:
            info = json.loads(path.read_text())
            found.append({'id': info['id'], 'start': info['start'], 'duration_s': info.get('duration_s'),
                          'streams': sorted(info['settings']), 'ended_cleanly': info.get('ended_cleanly')})
        except (OSError, ValueError, KeyError) as error:
            found.append({'id': path.parent.name, 'error': f'session.json cannot be read: {error}'})
    return found


def read_session(storage_dir: Path, session_id: str) -> dict | None:
    """The session.json of a session id; None for an unknown or malformed id."""
    path = storage_dir / session_id / 'session.json'
    if not SESSION_ID.fullmatch(session_id) or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        return {'id': session_id, 'error': f'session.json cannot be read: {error}'}
