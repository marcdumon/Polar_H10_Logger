"""The Bluetooth messages: heart rate notifications, PMD features, settings answers, errors, commands and data frames,
byte for byte as the Polar BLE SDK documents them."""

import pytest

from polar_h10.protocol import (
    Answer,
    Frame,
    HrReading,
    ProtocolError,
    UnsupportedFrame,
    parse_answer,
    parse_features,
    parse_frame,
    parse_hr,
    sample_times,
    settings_request,
    start_request,
    stop_request,
)

TS = (10**9).to_bytes(8, 'little')


def test_a_heart_rate_message_with_rr_intervals() -> None:
    data = bytes([0x10, 86]) + (700).to_bytes(2, 'little') + (709).to_bytes(2, 'little')
    assert parse_hr(data) == HrReading(86, None, [700, 709])


def test_a_heart_rate_message_with_every_flag() -> None:
    data = bytes([0x1F]) + (300).to_bytes(2, 'little') + bytes([0x10, 0x00]) + (1024).to_bytes(2, 'little')
    assert parse_hr(data) == HrReading(300, True, [1024]), 'uint16 bpm, contact detected, energy skipped'
    assert parse_hr(bytes([0x04, 60])) == HrReading(60, False, []), 'contact reported, not detected'


def test_a_short_heart_rate_message_is_an_error() -> None:
    with pytest.raises(ProtocolError, match='too short'):
        parse_hr(b'\x00')


def test_the_features_name_the_measurements() -> None:
    assert parse_features(bytes([0x0F, 0b00000101, 0x00])) == {'ecg', 'acc'}
    with pytest.raises(ProtocolError):
        parse_features(bytes([0x00, 0x05]))


def test_the_settings_answers_of_the_accelerometer_and_the_ecg() -> None:
    acc = bytes.fromhex('f001020000' '0004190032006400c800' '01011000' '0203020004000800')
    assert parse_answer(acc) == Answer(1, 'acc', {'sample_rate': [25, 50, 100, 200], 'resolution': [16], 'range': [2, 4, 8]})
    ecg = bytes.fromhex('f001000000' '00018200' '01010e00')
    assert parse_answer(ecg) == Answer(1, 'ecg', {'sample_rate': [130], 'resolution': [14]})


def test_an_error_answer_says_what_the_strap_refused() -> None:
    with pytest.raises(ProtocolError, match='refused acc: invalid sample rate'):
        parse_answer(bytes.fromhex('f0020208'))


def test_the_commands_byte_for_byte() -> None:
    assert start_request('ecg', {'sample_rate': 130, 'resolution': 14}) == bytes.fromhex('0200000182000101' '0e00')
    acc = start_request('acc', {'range': 8, 'sample_rate': 200, 'resolution': 16})
    assert acc == bytes.fromhex('02020001c800010110000201' '0800'), 'always in the order rate, resolution, range'
    assert stop_request('acc') == b'\x03\x02' and settings_request('ecg') == b'\x01\x00'


def test_an_ecg_frame_has_24_bit_samples_in_microvolts() -> None:
    data = bytes([0]) + TS + bytes([0]) + bytes.fromhex('ffffff' 'e80300' '18fcff') + b'\x01'
    assert parse_frame(data) == Frame('ecg', 10**9, [(-1,), (1000,), (-1000,)]), 'a cut-off sample at the end is left out'


def test_accelerometer_frames_of_8_16_and_24_bit() -> None:
    assert parse_frame(bytes([2]) + TS + bytes([1]) + bytes.fromhex('18fc0000e803') * 2) == Frame('acc', 10**9, [(-1000, 0, 1000)] * 2)
    assert parse_frame(bytes([2]) + TS + bytes([0]) + bytes([1, 0xFF, 2])).samples == [(1, -1, 2)]
    assert parse_frame(bytes([2]) + TS + bytes([2]) + bytes.fromhex('010000ffffff020000')).samples == [(1, -1, 2)]


def test_a_frame_with_only_a_header_has_no_samples() -> None:
    assert parse_frame(bytes([0]) + TS + bytes([0])).samples == []


@pytest.mark.parametrize('data', [bytes([0]) + TS + bytes([0x80]), bytes([1]) + TS + bytes([0]), bytes([2]) + TS + bytes([3])])
def test_compressed_and_unknown_frames_are_unsupported(data: bytes) -> None:
    with pytest.raises(UnsupportedFrame):
        parse_frame(data)


def test_each_sample_gets_its_strap_time_from_the_last_one() -> None:
    assert sample_times(10**9, 3, 130) == [10**9 - 15384615, 10**9 - 7692308, 10**9]
    assert sample_times(5, 1, 200) == [5]
