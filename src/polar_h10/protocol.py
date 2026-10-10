"""The Bluetooth messages of the Polar H10: the standard heart rate notification, and Polar Measurement Data (PMD) for
the ECG and the accelerometer: features, settings, start and stop commands, their answers and the data frames. Pure
functions, no Bluetooth."""

import struct
from dataclasses import dataclass

HR_MEASUREMENT = '00002a37-0000-1000-8000-00805f9b34fb'
BATTERY_LEVEL = '00002a19-0000-1000-8000-00805f9b34fb'
DEVICE_INFO = {
    'manufacturer': '00002a29-0000-1000-8000-00805f9b34fb', 'model': '00002a24-0000-1000-8000-00805f9b34fb',
    'serial': '00002a25-0000-1000-8000-00805f9b34fb', 'hardware': '00002a27-0000-1000-8000-00805f9b34fb',
    'firmware': '00002a26-0000-1000-8000-00805f9b34fb', 'software': '00002a28-0000-1000-8000-00805f9b34fb',
}
PMD_CONTROL = 'fb005c81-02e7-f387-1cad-8acd2d8df0c8'
PMD_DATA = 'fb005c82-02e7-f387-1cad-8acd2d8df0c8'
MIN_MTU = 232

KINDS = {'ecg': 0, 'ppg': 1, 'acc': 2, 'ppi': 3, 'gyro': 5, 'mag': 6}
KIND_NAMES = {code: name for name, code in KINDS.items()}
SETTING_TYPES = {'sample_rate': 0, 'resolution': 1, 'range': 2, 'range_milliunit': 3, 'channels': 4, 'factor': 5}
SETTING_NAMES = {code: name for name, code in SETTING_TYPES.items()}
SETTING_SIZES = {0: 2, 1: 2, 2: 2, 3: 4, 4: 1, 5: 4}
ORDER = ('sample_rate', 'resolution', 'range')
GET_SETTINGS, START, STOP = 0x01, 0x02, 0x03
ERRORS = {1: 'invalid op code', 2: 'invalid measurement type', 3: 'not supported', 4: 'invalid length', 5: 'invalid parameter',
          6: 'already in this state', 7: 'invalid resolution', 8: 'invalid sample rate', 9: 'invalid range', 10: 'invalid MTU',
          11: 'invalid number of channels', 12: 'invalid state', 13: 'the strap is in its charger'}


class ProtocolError(Exception):
    """A message the logger cannot read, or an error answer of the strap."""


class UnsupportedFrame(ProtocolError):
    """A data frame type the logger does not read (the compressed types of other Polar devices)."""


@dataclass(frozen=True)
class HrReading:
    """One heart rate notification: bpm, skin contact (None when not reported) and RR intervals in 1/1024 s."""

    hr_bpm: int
    contact: bool | None
    rr_raw: list[int]


@dataclass(frozen=True)
class Answer:
    """An answer on the control point: op code, measurement, and for a settings question the values per setting."""

    op: int
    kind: str
    settings: dict[str, list[int | float]]


@dataclass(frozen=True)
class Frame:
    """A PMD data frame: the measurement, the strap time of its last sample in ns, and the samples (one value for the
    ECG; x, y and z for the accelerometer)."""

    kind: str
    sensor_ns: int
    samples: list[tuple[int, ...]]


def parse_hr(data: bytes) -> HrReading:
    """A Heart Rate Measurement notification."""
    if len(data) < 2:
        raise ProtocolError(f'Heart rate message too short: {data.hex()}')
    flags = data[0]
    if flags & 0x01:
        hr, i = int.from_bytes(data[1:3], 'little'), 3
    else:
        hr, i = data[1], 2
    contact = bool(flags & 0x02) if flags & 0x04 else None
    if flags & 0x08:
        i += 2
    rr = [int.from_bytes(data[j:j + 2], 'little') for j in range(i, len(data) - 1, 2)] if flags & 0x10 else []
    return HrReading(hr, contact, rr)


def parse_features(data: bytes) -> set[str]:
    """The measurements the strap offers, from a read of the control point."""
    if len(data) < 2 or data[0] != 0x0F:
        raise ProtocolError(f'Not a PMD feature answer: {data.hex()}')
    return {name for name, code in KINDS.items() if (data[1] >> code) & 1}


def _settings(data: bytes) -> dict[str, list[int | float]]:
    """Settings as type, count and values; an unknown type ends the list."""
    found: dict[str, list[int | float]] = {}
    i = 0
    while i + 1 < len(data) and data[i] in SETTING_SIZES:
        code, count, size = data[i], data[i + 1], SETTING_SIZES[data[i]]
        values: list[int | float] = []
        for j in range(count):
            raw = data[i + 2 + j * size:i + 2 + (j + 1) * size]
            values.append(struct.unpack('<f', raw)[0] if code == SETTING_TYPES['factor'] else int.from_bytes(raw, 'little'))
        found[SETTING_NAMES[code]] = values
        i += 2 + count * size
    return found


def parse_answer(data: bytes) -> Answer:
    """An answer on the control point; an error answer raises ProtocolError with its meaning."""
    if len(data) < 4 or data[0] != 0xF0:
        raise ProtocolError(f'Not a PMD answer: {data.hex()}')
    op, kind, status = data[1], KIND_NAMES.get(data[2], str(data[2])), data[3]
    if status:
        raise ProtocolError(f'The strap refused {kind}: {ERRORS.get(status, f"error {status}")}.')
    return Answer(op, kind, _settings(data[5:]) if op == GET_SETTINGS else {})


def settings_request(kind: str) -> bytes:
    """Ask the settings the strap offers for a measurement."""
    return bytes([GET_SETTINGS, KINDS[kind]])


def start_request(kind: str, settings: dict[str, int]) -> bytes:
    """Start a measurement: each given setting as type, count 1 and a uint16 value, in the order rate, resolution, range."""
    body = bytearray([START, KINDS[kind]])
    for name in ORDER:
        if name in settings:
            body += bytes([SETTING_TYPES[name], 1]) + settings[name].to_bytes(2, 'little')
    return bytes(body)


def stop_request(kind: str) -> bytes:
    """Stop a measurement."""
    return bytes([STOP, KINDS[kind]])


def parse_frame(data: bytes) -> Frame:
    """A PMD data frame of the ECG (24-bit samples in µV) or the accelerometer (x, y, z in mg, 8, 16 or 24 bit)."""
    if len(data) < 10:
        raise ProtocolError(f'Data frame too short: {data.hex()}')
    kind, frame_type, body = KIND_NAMES.get(data[0], str(data[0])), data[9], data[10:]
    readable = (kind == 'ecg' and frame_type == 0) or (kind == 'acc' and frame_type <= 2)
    if not readable:
        raise UnsupportedFrame(f'{kind} frame type {frame_type}')
    size, axes = (3, 1) if kind == 'ecg' else (frame_type + 1, 3)
    step = size * axes
    samples = [tuple(int.from_bytes(body[i + a * size:i + (a + 1) * size], 'little', signed=True) for a in range(axes))
               for i in range(0, len(body) - len(body) % step, step)]
    return Frame(kind, int.from_bytes(data[1:9], 'little'), samples)


def sample_times(sensor_ns: int, count: int, sample_rate: int) -> list[int]:
    """The strap time of each sample: the frame's time is that of its last sample; the others lie 1/rate apart before it."""
    return [sensor_ns - round((count - 1 - i) * 1e9 / sample_rate) for i in range(count)]
