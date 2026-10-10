"""Bluetooth errors get a meaning the user can act on."""

from bleak.exc import BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason, BleakError

from polar_h10.strap import BLUETOOTH_OFF, BUSY, BluetoothOff, StrapBusy, StrapError, classify


def test_bluetooth_errors_get_a_meaning() -> None:
    off = classify(BleakError('No Bluetooth adapters found.'))
    assert isinstance(off, BluetoothOff) and str(off) == BLUETOOTH_OFF
    assert isinstance(classify(BleakError('[org.bluez.Error.NotReady] Resource Not Ready')), BluetoothOff)
    busy = classify(BleakError('[org.bluez.Error.Failed] le-connection-abort-by-local'))
    assert isinstance(busy, StrapBusy) and str(busy) == BUSY
    assert isinstance(classify(TimeoutError()), StrapBusy), 'a connect that times out: the strap does not answer'
    other = classify(BleakError('something else'))
    assert type(other) is StrapError and str(other) == 'Bluetooth error: something else'


def test_the_real_bluetooth_off_error_of_bleak_gets_the_rfkill_message() -> None:
    off = BleakBluetoothNotAvailableError('No powered Bluetooth adapters found. Turn on Bluetooth and try again.',
                                          BleakBluetoothNotAvailableReason.POWERED_OFF)
    assert isinstance(classify(off), BluetoothOff) and str(classify(off)) == BLUETOOTH_OFF
