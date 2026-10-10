"""The frames captured from the user's strap on 2026-10-09 parse as the spec expects."""

import json
from itertools import pairwise
from pathlib import Path

from polar_h10.protocol import parse_frame, parse_hr

FRAMES = json.loads((Path(__file__).parent / 'fixtures' / 'h10_frames.json').read_text())


def test_the_strap_offers_the_expected_settings() -> None:
    caps = FRAMES['capabilities']
    assert 130 in caps['ecg']['sample_rate'] and 200 in caps['acc']['sample_rate'] and 8 in caps['acc']['range']


def test_real_heart_rate_messages_parse() -> None:
    readings = [parse_hr(bytes.fromhex(h)) for h in FRAMES['hr']]
    assert readings and all(30 <= r.hr_bpm <= 220 for r in readings)
    # raw RR, with the noise of electrodes that settle (279 and 2199 ms in the capture); a wrong byte order would
    # give values near 0 or 60000 ms
    assert all(200 <= raw * 1000 / 1024 <= 3000 for r in readings for raw in r.rr_raw)


def test_real_ecg_and_accelerometer_frames_parse() -> None:
    ecg = [parse_frame(bytes.fromhex(h)) for h in FRAMES['ecg']]
    acc = [parse_frame(bytes.fromhex(h)) for h in FRAMES['acc']]
    assert all(f.kind == 'ecg' and f.samples and all(abs(s[0]) < 20000 for s in f.samples) for f in ecg), 'µV'
    assert all(f.kind == 'acc' and f.samples and all(abs(v) <= 9000 for s in f.samples for v in s) for f in acc), 'mg within ±8 g'
    for frames in (ecg, acc):
        assert all(b.sensor_ns > a.sensor_ns for a, b in pairwise(frames)), 'the strap clock moves on'
