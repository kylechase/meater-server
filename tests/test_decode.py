"""Decoding of the probe's GATT payloads."""

from __future__ import annotations

import struct

import pytest

from meater_server.decode import (
    APPTION_LABS_COMPANY_ID,
    MEATER_SERVICE_UUID,
    DecodeError,
    c_to_f,
    celsius_to_raw,
    decode_battery,
    decode_temperatures,
    f_to_c,
    is_plausible_celsius,
    looks_like_meater,
    raw_to_celsius,
)


def payload(raw_tip: int, raw_ambient: int = 0, raw_offset: int = 0, extra: bytes = b"") -> bytes:
    return struct.pack("<HHH", raw_tip, raw_ambient, raw_offset) + extra


@pytest.mark.parametrize(
    ("raw", "celsius"),
    [
        (0, 0.5),
        (312, 20.0),     # room temperature
        (875, 55.1875),  # medium-rare beef
        (1592, 100.0),   # boiling
    ],
)
def test_raw_to_celsius(raw: int, celsius: float):
    assert raw_to_celsius(raw) == pytest.approx(celsius)


def test_celsius_to_raw_inverts_raw_to_celsius():
    for raw in range(0, 4000, 37):
        assert celsius_to_raw(raw_to_celsius(raw)) == raw


def test_tip_temperature_decodes_little_endian():
    tip, _ = decode_temperatures(payload(celsius_to_raw(57.2)))
    assert tip == pytest.approx(57.2, abs=0.04)


def test_ambient_equals_tip_when_the_correction_floors_at_zero():
    """With no ambient reading the correction clamps, so ambient tracks the tip."""
    tip, ambient = decode_temperatures(payload(celsius_to_raw(25.0)))
    assert ambient == pytest.approx(tip)


def test_ambient_rises_above_the_tip_with_a_hot_grill():
    # A large ambient word with a small offset is the hot-air case.
    tip, ambient = decode_temperatures(
        payload(celsius_to_raw(25.0), raw_ambient=500, raw_offset=20)
    )
    assert ambient > tip
    # correction = (500 - min(48, 20)) * 16 * 589 / 1487, truncated
    expected_raw = celsius_to_raw(25.0) + ((500 - 20) * 16 * 589) // 1487
    assert ambient == pytest.approx(raw_to_celsius(expected_raw))


def test_ambient_offset_is_capped_at_48():
    """Offsets above the cap are treated as 48, not taken literally."""
    _, high = decode_temperatures(payload(400, raw_ambient=600, raw_offset=48))
    _, higher = decode_temperatures(payload(400, raw_ambient=600, raw_offset=5000))
    assert high == pytest.approx(higher)


def test_trailing_bytes_are_ignored():
    """Some firmware appends bytes we do not understand."""
    short = decode_temperatures(payload(700, 300, 20))
    long = decode_temperatures(payload(700, 300, 20, extra=b"\xde\xad\xbe\xef"))
    assert short == long


@pytest.mark.parametrize("data", [b"", b"\x01", b"\x01\x02\x03\x04\x05"])
def test_short_temperature_payloads_are_rejected(data: bytes):
    with pytest.raises(DecodeError):
        decode_temperatures(data)


@pytest.mark.parametrize(
    ("raw", "percent"),
    [(0, 0), (5, 50), (10, 100), (11, 100), (255, 100)],
)
def test_battery_is_tens_of_a_percent_and_clamped(raw: int, percent: int):
    assert decode_battery(struct.pack("<H", raw)) == percent


def test_short_battery_payload_is_rejected():
    with pytest.raises(DecodeError):
        decode_battery(b"\x05")


@pytest.mark.parametrize("value", [-20.0, 0.0, 100.0, 275.0, 400.0])
def test_plausible_range_accepts_real_temperatures(value: float):
    assert is_plausible_celsius(value)


@pytest.mark.parametrize("value", [-40.0, 500.0, 4000.0])
def test_plausible_range_rejects_decode_noise(value: float):
    assert not is_plausible_celsius(value)


def test_fahrenheit_conversions_round_trip():
    assert c_to_f(0) == pytest.approx(32)
    assert c_to_f(100) == pytest.approx(212)
    assert f_to_c(c_to_f(57.2)) == pytest.approx(57.2)


class TestAdvertisementMatching:
    def test_matches_on_the_manufacturer_id(self):
        assert looks_like_meater(None, [], [APPTION_LABS_COMPANY_ID])

    def test_matches_on_the_service_uuid_regardless_of_case(self):
        assert looks_like_meater(None, [MEATER_SERVICE_UUID.upper()], [])

    def test_matches_on_the_advertised_name(self):
        assert looks_like_meater("MEATER+ 1234", [], [])
        assert looks_like_meater("meater", [], [])

    def test_ignores_unrelated_devices(self):
        assert not looks_like_meater(
            "Govee Thermometer", ["0000180f-0000-1000-8000-00805f9b34fb"], [0x0001]
        )
        assert not looks_like_meater(None, [], [])
