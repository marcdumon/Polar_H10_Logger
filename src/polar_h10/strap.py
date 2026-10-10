"""The strap: what the recorder needs from it (a protocol, so tests can use a stand-in), and the real Polar H10 over
Bluetooth LE with bleak. Bluetooth errors become StrapError with a message the user can act on."""

import asyncio
import contextlib
import functools
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakBluetoothNotAvailableError, BleakError

from polar_h10.config import Config
from polar_h10.protocol import (
    BATTERY_LEVEL,
    DEVICE_INFO,
    HR_MEASUREMENT,
    KIND_NAMES,
    PMD_CONTROL,
    PMD_DATA,
    Answer,
    ProtocolError,
    parse_answer,
    parse_features,
    settings_request,
    start_request,
    stop_request,
)

BLUETOOTH_OFF = 'Bluetooth is off. Switch it on, for example with: rfkill unblock bluetooth'
NOT_FOUND = 'Strap not found. Wear it with wet electrodes and stay within 10 m.'
BUSY = 'The strap refuses the connection: another app (for example on your phone) may hold it. Close that app.'
ANSWER_S = 5.0
CONNECT_S = 20.0
PMD_KINDS = ('ecg', 'acc')


class StrapError(Exception):
    """A strap or Bluetooth problem, with a message for the user."""


class BluetoothOff(StrapError):
    """The Bluetooth adapter is off, blocked or missing."""


class StrapNotFound(StrapError):
    """No strap advertised within the scan time."""


class StrapBusy(StrapError):
    """The strap advertises but refuses the connection: another app holds it."""


def classify(error: Exception) -> StrapError:
    """The meaning of a bleak error for the user."""
    text = str(error)
    if isinstance(error, BleakBluetoothNotAvailableError):
        return BluetoothOff(BLUETOOTH_OFF)
    if any(part in text for part in ('No Bluetooth adapters', 'NotReady', 'powered off', 'not powered')):
        return BluetoothOff(BLUETOOTH_OFF)
    if isinstance(error, TimeoutError) or 'le-connection-abort-by-local' in text or 'connection abort' in text:
        return StrapBusy(BUSY)
    return StrapError(f'Bluetooth error: {text}')


class Strap(Protocol):
    """What the recorder needs from a strap."""

    name: str
    address: str
    mtu: int

    async def connect(self, on_disconnect: Callable[[], None]) -> None:
        """Scan and connect; on_disconnect runs when the connection is lost."""

    async def disconnect(self) -> None:
        """End the connection."""

    async def info(self) -> dict[str, str]:
        """Name, address, manufacturer, model, serial, hardware, firmware, software."""

    async def battery(self) -> int:
        """The battery level in %."""

    async def capabilities(self) -> dict[str, dict[str, list[int]]]:
        """'hr' and each PMD measurement with the values of each setting."""

    async def start_hr(self, on_hr: Callable[[bytes], None]) -> None:
        """Heart rate notifications, raw."""

    async def start_battery(self, on_battery: Callable[[int], None]) -> None:
        """Battery level notifications."""

    async def start_pmd(self, kind: str, settings: dict[str, int], on_frame: Callable[[bytes], None]) -> None:
        """Start a measurement; its data frames, raw."""

    async def stop_pmd(self, kind: str) -> None:
        """Stop a measurement."""


def _bluetooth(method: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """A strap method whose bleak errors become StrapError."""
    @functools.wraps(method)
    async def wrapper(self: 'BleakStrap', *args: Any, **kwargs: Any) -> Any:
        try:
            return await method(self, *args, **kwargs)
        except BleakError as error:
            raise classify(error) from error
    return wrapper


class BleakStrap:
    """The real strap over Bluetooth LE. Each connect scans again: BlueZ forgets the strap after a failed connection."""

    def __init__(self, config: Config) -> None:
        """Not connected yet."""
        self._config = config
        self._client: BleakClient | None = None
        self._answers: asyncio.Queue[bytes] = asyncio.Queue()
        self._pmd: dict[str, Callable[[bytes], None]] = {}
        self._pmd_ready = False
        self.name, self.address, self.mtu = config.name_prefix, config.address, 0

    async def _find(self) -> BLEDevice:
        """The strap by its address, else by its name."""
        try:
            if self._config.address:
                device = await BleakScanner.find_device_by_address(self._config.address, timeout=self._config.scan_seconds)
            else:
                device = await BleakScanner.find_device_by_filter(
                    lambda d, _: (d.name or '').startswith(self._config.name_prefix), timeout=self._config.scan_seconds)
        except BleakError as error:
            raise classify(error) from error
        if device is None:
            raise StrapNotFound(NOT_FOUND)
        return device

    async def connect(self, on_disconnect: Callable[[], None]) -> None:
        """Scan, connect and learn the MTU."""
        device = await self._find()
        self.name, self.address = device.name or self._config.name_prefix, device.address
        client = BleakClient(device, disconnected_callback=lambda _: on_disconnect(), timeout=CONNECT_S)
        try:
            await client.connect()
        except (BleakError, TimeoutError) as error:
            raise classify(error) from error
        # kept at once, so a cancel during the MTU step can still close the connection
        self._client = client
        backend = getattr(client, '_backend', None)
        if hasattr(backend, '_acquire_mtu'):
            # on BlueZ, bleak knows the negotiated MTU only after this call; without it mtu_size says 23
            with contextlib.suppress(Exception):
                await backend._acquire_mtu()
        self.mtu = client.mtu_size

    async def disconnect(self) -> None:
        """End the connection; a connection that is gone already is fine."""
        client, self._client = self._client, None
        self._pmd.clear()
        self._pmd_ready = False
        if client is not None:
            with contextlib.suppress(BleakError, TimeoutError):
                await client.disconnect()

    @_bluetooth
    async def info(self) -> dict[str, str]:
        """The device information service, and name and address."""
        found = {'name': self.name, 'address': self.address}
        for key, uuid in DEVICE_INFO.items():
            try:
                found[key] = bytes(await self._client.read_gatt_char(uuid)).decode(errors='replace').strip('\x00 ')
            except BleakError:
                found[key] = ''
        return found

    @_bluetooth
    async def battery(self) -> int:
        """The battery level in %."""
        return (await self._client.read_gatt_char(BATTERY_LEVEL))[0]

    async def _ready(self) -> None:
        """Answers of the control point and data frames: subscribed once per connection."""
        if not self._pmd_ready:
            await self._client.start_notify(PMD_CONTROL, lambda _, data: self._answers.put_nowait(bytes(data)))
            await self._client.start_notify(PMD_DATA, self._on_data)
            self._pmd_ready = True

    def _on_data(self, _: Any, data: bytearray) -> None:
        """A data frame to the callback of its measurement."""
        callback = self._pmd.get(KIND_NAMES.get(data[0], ''))
        if callback is not None:
            callback(bytes(data))

    async def _ask(self, request: bytes) -> Answer:
        """A command on the control point and its answer."""
        await self._ready()
        while not self._answers.empty():
            self._answers.get_nowait()
        await self._client.write_gatt_char(PMD_CONTROL, request, response=True)
        try:
            return parse_answer(await asyncio.wait_for(self._answers.get(), ANSWER_S))
        except TimeoutError as error:
            raise ProtocolError(f'The strap did not answer the measurement command within {ANSWER_S:.0f} s.') from error

    @_bluetooth
    async def capabilities(self) -> dict[str, dict[str, list[int]]]:
        """Heart rate, and the settings of each PMD measurement the strap offers."""
        await self._ready()
        features = parse_features(bytes(await self._client.read_gatt_char(PMD_CONTROL)))
        found: dict[str, dict[str, list[int]]] = {'hr': {}}
        for kind in PMD_KINDS:
            if kind in features:
                found[kind] = (await self._ask(settings_request(kind))).settings
        return found

    @_bluetooth
    async def start_hr(self, on_hr: Callable[[bytes], None]) -> None:
        """Heart rate notifications, raw."""
        await self._client.start_notify(HR_MEASUREMENT, lambda _, data: on_hr(bytes(data)))

    @_bluetooth
    async def start_battery(self, on_battery: Callable[[int], None]) -> None:
        """Battery notifications, when the strap sends them; the level is also read at the start and at the stop."""
        with contextlib.suppress(BleakError):
            await self._client.start_notify(BATTERY_LEVEL, lambda _, data: on_battery(data[0]))

    @_bluetooth
    async def start_pmd(self, kind: str, settings: dict[str, int], on_frame: Callable[[bytes], None]) -> None:
        """Start a measurement with its settings."""
        self._pmd[kind] = on_frame
        await self._ask(start_request(kind, settings))

    @_bluetooth
    async def stop_pmd(self, kind: str) -> None:
        """Stop a measurement."""
        await self._ask(stop_request(kind))
        self._pmd.pop(kind, None)
