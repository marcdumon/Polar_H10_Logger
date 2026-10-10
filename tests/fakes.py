"""A stand-in strap for tests, and the bytes of heart rate messages and data frames."""

from collections.abc import Callable

CAPS = {'hr': {}, 'ecg': {'sample_rate': [130], 'resolution': [14]},
        'acc': {'sample_rate': [25, 50, 100, 200], 'resolution': [16], 'range': [2, 4, 8]}}


def hr_bytes(bpm: int, rr: tuple[int, ...] = ()) -> bytes:
    """A heart rate notification with a uint8 bpm and RR intervals in 1/1024 s."""
    return bytes([0x10 if rr else 0x00, bpm]) + b''.join(r.to_bytes(2, 'little') for r in rr)


def ecg_frame(sensor_ns: int, samples: list[int]) -> bytes:
    """An ECG data frame of 24-bit samples."""
    return bytes([0]) + sensor_ns.to_bytes(8, 'little') + bytes([0]) + b''.join(s.to_bytes(3, 'little', signed=True) for s in samples)


def acc_frame(sensor_ns: int, samples: list[tuple[int, int, int]]) -> bytes:
    """An accelerometer data frame of 16-bit x, y, z."""
    body = b''.join(v.to_bytes(2, 'little', signed=True) for sample in samples for v in sample)
    return bytes([2]) + sensor_ns.to_bytes(8, 'little') + bytes([1]) + body


class FakeStrap:
    """Sends its scripted messages as soon as a stream starts; it can fail to connect, refuse a measurement or drop."""

    def __init__(self, hr: tuple[bytes, ...] | list[bytes] = (), frames: dict[str, list[bytes]] | None = None,
                 caps: dict | None = None, fail: Exception | None = None, pmd_errors: dict[str, Exception] | None = None) -> None:
        """The messages to send, and the failures to show."""
        self.name, self.address, self.mtu = 'Polar H10 TEST', 'AA:BB:CC:DD:EE:FF', 247
        self.hr, self.frames, self.caps = list(hr), frames or {}, caps or CAPS
        self.fail, self.pmd_errors = fail, pmd_errors or {}
        self.started: list[tuple[str, dict]] = []
        self.stopped: list[str] = []
        self.connected = False
        self.disconnects = 0
        self.callbacks: dict[str, Callable] = {}
        self._on_disconnect: Callable[[], None] = lambda: None

    async def connect(self, on_disconnect: Callable[[], None]) -> None:
        """Connect, or fail as scripted."""
        if self.fail is not None:
            raise self.fail
        self.connected, self._on_disconnect = True, on_disconnect

    async def disconnect(self) -> None:
        """Disconnect, also a connection that is lost already."""
        self.connected = False
        self.disconnects += 1

    async def info(self) -> dict[str, str]:
        """The device information."""
        return {'name': self.name, 'address': self.address, 'manufacturer': 'Polar Electro Oy', 'model': 'Polar H10',
                'serial': 'E813952E', 'hardware': '39044024.10', 'firmware': '3.1.1', 'software': ''}

    async def battery(self) -> int:
        """The battery level."""
        return 90

    async def capabilities(self) -> dict:
        """The scripted settings."""
        return self.caps

    async def start_hr(self, on_hr: Callable[[bytes], None]) -> None:
        """Heart rate: the scripted messages at once."""
        self.callbacks['hr'] = on_hr
        for data in self.hr:
            on_hr(data)

    async def start_battery(self, on_battery: Callable[[int], None]) -> None:
        """Battery notifications."""
        self.callbacks['battery'] = on_battery

    async def start_pmd(self, kind: str, settings: dict[str, int], on_frame: Callable[[bytes], None]) -> None:
        """A measurement: refused as scripted, or its scripted frames at once."""
        if kind in self.pmd_errors:
            raise self.pmd_errors[kind]
        self.started.append((kind, settings))
        self.callbacks[kind] = on_frame
        for data in self.frames.get(kind, []):
            on_frame(data)

    async def stop_pmd(self, kind: str) -> None:
        """Stop a measurement."""
        self.stopped.append(kind)

    def drop(self) -> None:
        """Lose the connection, as a walk out of range does."""
        self.connected = False
        self._on_disconnect()

    def send(self, stream: str, data: bytes) -> None:
        """One more message on a stream that runs (or ran)."""
        self.callbacks[stream](data)
