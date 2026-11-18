"""Decoding of the MEATER probe's BLE GATT payloads.

The probe exposes a vendor service with two characteristics we care about.
Everything here is derived from the community reverse engineering of the
original MEATER and MEATER+ (Apption Labs, company ID 0x037B):

``TEMPERATURE_CHAR`` notifies six little-endian bytes::

    [0:2]  tip temperature, raw
    [2:4]  ambient reading, raw
    [4:6]  ambient offset, raw

A raw value converts to Celsius with ``(raw + 8) / 16``.  The ambient
temperature is the tip value plus a scaled, floor-clamped correction built
from the other two words; the 589/1487 ratio and the offset ceiling of 48 are
the probe's own calibration constants.

``BATTERY_CHAR`` notifies a little-endian word in the range 0-10, i.e. tens of
a percent of charge.
"""

from __future__ import annotations

# Vendor service on the original MEATER and MEATER+.
MEATER_SERVICE_UUID = "a75cc7fc-c956-488f-ac2a-2dbc08b63a04"
# Seen on newer hardware (MEATER 2 Plus / SE); same characteristic layout.
MEATER_SERVICE_UUID_ALT = "49141a23-307f-4e25-ad82-0a3f00d8b90b"
MEATER_SERVICE_UUIDS = (MEATER_SERVICE_UUID, MEATER_SERVICE_UUID_ALT)

TEMPERATURE_CHAR_UUID = "7edda774-045e-4bbf-909b-45d1991a2876"
BATTERY_CHAR_UUID = "2adb4877-68d8-4884-bd3c-d83853bf27b8"

# Apption Labs, the manufacturer ID in the probe's advertisement.
APPTION_LABS_COMPANY_ID = 0x037B

#: Probes report a resting value near 0 C; anything below this means the
#: characteristic has not produced a real reading yet.
_IMPLAUSIBLE_BELOW_C = -20.0
#: The original MEATER is rated to 275 C ambient / 100 C internal.  Readings
#: far above that are decode noise rather than a hot grill.
_IMPLAUSIBLE_ABOVE_C = 400.0

_AMBIENT_OFFSET_CEILING = 48
_AMBIENT_SCALE_NUM = 16 * 589
_AMBIENT_SCALE_DEN = 1487


class DecodeError(ValueError):
    """Raised when a characteristic payload is not shaped as expected."""


def _u16le(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


def raw_to_celsius(raw: int) -> float:
    """Convert a raw probe word to degrees Celsius."""
    return (raw + 8) / 16.0


def celsius_to_raw(celsius: float) -> int:
    """Inverse of :func:`raw_to_celsius`, for building test payloads."""
    return round(celsius * 16.0 - 8)


def decode_temperatures(data: bytes) -> tuple[float, float]:
    """Return ``(tip_celsius, ambient_celsius)`` from a temperature payload.

    Payloads longer than six bytes are accepted; later bytes are not
    understood and are ignored.
    """
    if len(data) < 6:
        raise DecodeError(f"temperature payload too short: {len(data)} bytes, need 6")

    raw_tip = _u16le(data, 0)
    raw_ambient_reading = _u16le(data, 2)
    raw_ambient_offset = _u16le(data, 4)

    # Integer arithmetic, truncating toward zero, to match the probe's own
    # fixed-point calibration. The floor at zero means the ambient probe never
    # reads colder than the tip.
    correction = (
        (raw_ambient_reading - min(_AMBIENT_OFFSET_CEILING, raw_ambient_offset))
        * _AMBIENT_SCALE_NUM
    ) // _AMBIENT_SCALE_DEN
    raw_ambient = raw_tip + max(0, correction)

    return raw_to_celsius(raw_tip), raw_to_celsius(raw_ambient)


def decode_battery(data: bytes) -> int:
    """Return battery charge as a percentage from a battery payload."""
    if len(data) < 2:
        raise DecodeError(f"battery payload too short: {len(data)} bytes, need 2")
    return max(0, min(100, _u16le(data, 0) * 10))


def is_plausible_celsius(value: float) -> bool:
    """Whether a decoded temperature is within the probe's physical range."""
    return _IMPLAUSIBLE_BELOW_C <= value <= _IMPLAUSIBLE_ABOVE_C


def looks_like_meater(
    name: str | None, service_uuids: list[str], manufacturer_ids: list[int]
) -> bool:
    """Heuristic match for a MEATER probe in a BLE advertisement."""
    if APPTION_LABS_COMPANY_ID in manufacturer_ids:
        return True
    lowered = {u.lower() for u in service_uuids}
    if lowered & {u.lower() for u in MEATER_SERVICE_UUIDS}:
        return True
    return bool(name and name.strip().lower().startswith("meater"))


def c_to_f(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


def f_to_c(fahrenheit: float) -> float:
    return (fahrenheit - 32.0) * 5.0 / 9.0
