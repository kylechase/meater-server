"""The entity list this device advertises, and how each entity reads its state.

Home Assistant asks for the entity list once per connection and then only
receives state updates, so the list has to be fixed before the first client
connects.  That is why probes live in numbered slots: slot 2's entities exist
from startup whether or not a second probe has ever been seen.

Each entity carries the function that reads its value from a slot, and
settable entities carry the function that applies a command.  That keeps the
protocol layer in :mod:`.server` free of any cooking logic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..presets import CUSTOM_PRESET_ID, PresetLibrary
from ..state import CookState, ProbeSlot

FNV_OFFSET_BASIS = 2166136261
FNV_PRIME = 16777619


def fnv1_hash(text: str) -> int:
    """ESPHome's own object-id hash, used as the entity key on the wire."""
    value = FNV_OFFSET_BASIS
    for char in text.encode("utf-8"):
        value = (value * FNV_PRIME) & 0xFFFFFFFF
        value ^= char
    return value


class Kind(StrEnum):
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    TEXT_SENSOR = "text_sensor"
    SELECT = "select"
    NUMBER = "number"
    BUTTON = "button"


STATE_CLASS_MEASUREMENT = 1
ENTITY_CATEGORY_NONE = 0
ENTITY_CATEGORY_DIAGNOSTIC = 2
NUMBER_MODE_BOX = 1

#: The select option that means "no cook in progress".
NO_COOK_OPTION = "None"


@dataclass(slots=True)
class Entity:
    """One advertised entity."""

    kind: Kind
    object_id: str
    name: str
    slot_index: int
    value_fn: Callable[[ProbeSlot], Any] | None = None
    command_fn: Callable[[ProbeSlot, Any], None] | None = None
    unit: str = ""
    device_class: str = ""
    state_class: int = 0
    accuracy_decimals: int = 0
    icon: str = ""
    options: tuple[str, ...] = ()
    min_value: float = 0.0
    max_value: float = 0.0
    step: float = 0.0
    mode: int = 0
    entity_category: int = ENTITY_CATEGORY_NONE
    key: int = field(init=False)

    def __post_init__(self) -> None:
        self.key = fnv1_hash(self.object_id)

    def read(self, slot: ProbeSlot) -> Any:
        """Current value, or ``None`` to report the state as missing."""
        if self.value_fn is None:
            return None
        return self.value_fn(slot)


# --- value readers --------------------------------------------------------
#
# Each takes a slot and returns the state, or None when there is nothing to
# report (an unoccupied slot, or a cook that has not started).


def _tip(slot: ProbeSlot) -> float | None:
    return slot.reading.tip_c if slot.reading else None


def _ambient(slot: ProbeSlot) -> float | None:
    return slot.reading.ambient_c if slot.reading else None


def _battery(slot: ProbeSlot) -> float | None:
    if slot.reading is None or slot.reading.battery_pct is None:
        return None
    return float(slot.reading.battery_pct)


def _rssi(slot: ProbeSlot) -> float | None:
    if slot.reading is None or slot.reading.rssi is None:
        return None
    return float(slot.reading.rssi)


def _target(slot: ProbeSlot) -> float | None:
    return slot.cook.target_c if slot.cook else None


def _pull(slot: ProbeSlot) -> float | None:
    return slot.cook.pull_c if slot.cook else None


def _seconds_remaining(slot: ProbeSlot) -> float | None:
    return slot.estimate().seconds_remaining


def _seconds_until_served(slot: ProbeSlot) -> float | None:
    """Time on the heat plus the rest, which is when you actually eat."""
    estimate = slot.estimate()
    if estimate.seconds_remaining is None or slot.cook is None:
        return estimate.seconds_remaining
    if slot.cook.state is CookState.COOKING:
        return estimate.seconds_remaining + slot.cook.rest_minutes * 60.0
    return estimate.seconds_remaining


def _rate(slot: ProbeSlot) -> float | None:
    return slot.estimate().rate_c_per_min


def _progress(slot: ProbeSlot) -> float | None:
    """How far from the starting temperature to the pull temperature, in percent."""
    cook, reading = slot.cook, slot.reading
    if cook is None or reading is None:
        return None
    if cook.state in (CookState.READY, CookState.RESTING, CookState.DONE):
        return 100.0
    history = slot.estimator.history
    start = history[0][1] if history else reading.tip_c
    span = cook.pull_c - start
    if span <= 0:
        return 100.0
    return max(0.0, min(100.0, (reading.tip_c - start) / span * 100.0))


def _connected(slot: ProbeSlot) -> bool:
    return slot.connected


def _cook_label(slot: ProbeSlot) -> str:
    return slot.cook.label if slot.cook else "Nothing"


def _cook_state(slot: ProbeSlot) -> str:
    return str(slot.cook.state) if slot.cook else str(CookState.IDLE)


def _probe_address(slot: ProbeSlot) -> str | None:
    return slot.address


def build_entities(
    slots: int,
    library: PresetLibrary,
    commands: SlotCommands,
) -> list[Entity]:
    """Build the full entity list for ``slots`` probe slots."""
    options = (NO_COOK_OPTION, *(label for label, _, _ in library.select_options()))
    entities: list[Entity] = []

    for index in range(1, slots + 1):
        prefix = f"probe_{index}"
        label = f"Probe {index}" if slots > 1 else "Probe"

        # The loop variables are bound as defaults rather than captured, so
        # the helper cannot silently pick up a later slot's values.
        def sensor(
            suffix: str,
            nice: str,
            reader,
            *,
            _prefix: str = prefix,
            _label: str = label,
            _index: int = index,
            **kwargs,
        ) -> Entity:
            return Entity(
                Kind.SENSOR,
                f"{_prefix}_{suffix}",
                f"{_label} {nice}",
                _index,
                value_fn=reader,
                **kwargs,
            )

        entities += [
            sensor(
                "tip_temperature",
                "tip temperature",
                _tip,
                unit="°C",
                device_class="temperature",
                state_class=STATE_CLASS_MEASUREMENT,
                accuracy_decimals=1,
                icon="mdi:thermometer",
            ),
            sensor(
                "ambient_temperature",
                "ambient temperature",
                _ambient,
                unit="°C",
                device_class="temperature",
                state_class=STATE_CLASS_MEASUREMENT,
                accuracy_decimals=1,
                icon="mdi:thermometer-lines",
            ),
            sensor(
                "target_temperature",
                "target temperature",
                _target,
                unit="°C",
                device_class="temperature",
                accuracy_decimals=1,
                icon="mdi:target",
            ),
            sensor(
                "pull_temperature",
                "pull temperature",
                _pull,
                unit="°C",
                device_class="temperature",
                accuracy_decimals=1,
                icon="mdi:arrow-up-bold-box-outline",
            ),
            sensor(
                "time_remaining",
                "time remaining",
                _seconds_remaining,
                unit="s",
                device_class="duration",
                accuracy_decimals=0,
                icon="mdi:timer-sand",
            ),
            sensor(
                "time_until_served",
                "time until served",
                _seconds_until_served,
                unit="s",
                device_class="duration",
                accuracy_decimals=0,
                icon="mdi:silverware-fork-knife",
            ),
            sensor(
                "cook_progress",
                "cook progress",
                _progress,
                unit="%",
                accuracy_decimals=0,
                icon="mdi:progress-clock",
            ),
            sensor(
                "temperature_rate",
                "temperature rate",
                _rate,
                unit="°C/min",
                accuracy_decimals=2,
                icon="mdi:chart-line",
            ),
            sensor(
                "battery",
                "battery",
                _battery,
                unit="%",
                device_class="battery",
                state_class=STATE_CLASS_MEASUREMENT,
                accuracy_decimals=0,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
            sensor(
                "signal_strength",
                "signal strength",
                _rssi,
                unit="dBm",
                device_class="signal_strength",
                state_class=STATE_CLASS_MEASUREMENT,
                accuracy_decimals=0,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
            Entity(
                Kind.BINARY_SENSOR,
                f"{prefix}_connected",
                f"{label} connected",
                index,
                value_fn=_connected,
                device_class="connectivity",
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
            Entity(
                Kind.TEXT_SENSOR,
                f"{prefix}_cook",
                f"{label} cook",
                index,
                value_fn=_cook_label,
                icon="mdi:food-steak",
            ),
            Entity(
                Kind.TEXT_SENSOR,
                f"{prefix}_cook_state",
                f"{label} state",
                index,
                value_fn=_cook_state,
                icon="mdi:state-machine",
            ),
            Entity(
                Kind.TEXT_SENSOR,
                f"{prefix}_address",
                f"{label} address",
                index,
                value_fn=_probe_address,
                icon="mdi:bluetooth",
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
            Entity(
                Kind.SELECT,
                f"{prefix}_cook_preset",
                f"{label} cook preset",
                index,
                value_fn=lambda slot: commands.current_option(slot),
                command_fn=commands.select_preset,
                options=options,
                icon="mdi:silverware-variant",
            ),
            Entity(
                Kind.NUMBER,
                f"{prefix}_target_override",
                f"{label} target override",
                index,
                value_fn=lambda slot: slot.cook.target_c if slot.cook else 0.0,
                command_fn=commands.set_target,
                unit="°C",
                device_class="temperature",
                min_value=0.0,
                max_value=120.0,
                step=0.5,
                mode=NUMBER_MODE_BOX,
                icon="mdi:thermometer-check",
                entity_category=ENTITY_CATEGORY_NONE,
            ),
            Entity(
                Kind.BUTTON,
                f"{prefix}_start_resting",
                f"{label} start resting",
                index,
                command_fn=lambda slot, _value: commands.start_resting(slot),
                icon="mdi:timer-pause",
            ),
            Entity(
                Kind.BUTTON,
                f"{prefix}_clear_cook",
                f"{label} clear cook",
                index,
                command_fn=lambda slot, _value: commands.clear_cook(slot),
                icon="mdi:close-circle-outline",
            ),
        ]

    return entities


class SlotCommands:
    """The cooking operations the entity layer needs, in one place.

    :class:`~meater_server.manager.MeaterManager` implements this; keeping it
    as an explicit surface means the entity table never reaches into manager
    internals.
    """

    def __init__(self, library: PresetLibrary) -> None:
        self._library = library

    def current_option(self, slot: ProbeSlot) -> str:
        """The select option matching the slot's cook."""
        if slot.cook is None:
            return NO_COOK_OPTION
        preset = self._library.get(slot.cook.preset_id)
        if preset is None:
            return NO_COOK_OPTION
        return self._library.option_label(preset, preset.doneness(slot.cook.doneness_id))

    def resolve_option(self, option: str) -> tuple[str, str] | None:
        """Map a select option back to ``(preset_id, doneness_id)``."""
        for label, preset_id, doneness_id in self._library.select_options():
            if label == option:
                return preset_id, doneness_id
        return None

    # Overridden by the manager.
    def select_preset(self, slot: ProbeSlot, option: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def set_target(self, slot: ProbeSlot, target_c: float) -> None:  # pragma: no cover
        raise NotImplementedError

    def start_resting(self, slot: ProbeSlot) -> None:  # pragma: no cover
        raise NotImplementedError

    def clear_cook(self, slot: ProbeSlot) -> None:  # pragma: no cover
        raise NotImplementedError


__all__ = [
    "CUSTOM_PRESET_ID",
    "NO_COOK_OPTION",
    "Entity",
    "Kind",
    "SlotCommands",
    "build_entities",
    "fnv1_hash",
]
