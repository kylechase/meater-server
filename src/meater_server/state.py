"""Runtime state: probe readings, cook sessions, and the slots that hold them."""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from enum import StrEnum

from .decode import c_to_f
from .estimator import Confidence, CookEstimator, Estimate
from .presets import Doneness, Preset, PresetLibrary


class CookState(StrEnum):
    """Where a cook has got to."""

    IDLE = "idle"
    """Nothing selected."""
    COOKING = "cooking"
    """Climbing toward the pull temperature."""
    READY = "ready"
    """At the pull temperature - take it off the heat."""
    RESTING = "resting"
    """Off the heat, carryover finishing the job."""
    DONE = "done"
    """Rest complete."""


@dataclass(frozen=True, slots=True)
class Reading:
    """One decoded sample from a probe."""

    tip_c: float
    ambient_c: float
    battery_pct: int | None
    wall_time: float
    monotonic: float
    rssi: int | None = None


@dataclass(slots=True)
class CookSession:
    """One cook: what it is, what temperature means done, and how far along."""

    preset_id: str
    doneness_id: str
    label: str
    target_c: float
    carryover_c: float
    rest_minutes: float
    started_at: float = field(default_factory=time.time)
    state: CookState = CookState.COOKING
    peak_tip_c: float | None = None
    rest_started_at: float | None = None
    ready_at: float | None = None
    """Wall-clock time the pull temperature was first reached."""
    custom_target: bool = False

    @property
    def pull_c(self) -> float:
        """Temperature to remove at, so carryover lands on the target."""
        return self.target_c - self.carryover_c

    @property
    def rest_remaining_s(self) -> float | None:
        if self.state is not CookState.RESTING or self.rest_started_at is None:
            return None
        return max(0.0, self.rest_started_at + self.rest_minutes * 60.0 - time.time())

    def to_dict(self) -> dict:
        return {
            "preset_id": self.preset_id,
            "doneness_id": self.doneness_id,
            "label": self.label,
            "target_c": self.target_c,
            "carryover_c": self.carryover_c,
            "rest_minutes": self.rest_minutes,
            "started_at": self.started_at,
            "state": str(self.state),
            "peak_tip_c": self.peak_tip_c,
            "rest_started_at": self.rest_started_at,
            "ready_at": self.ready_at,
            "custom_target": self.custom_target,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> CookSession | None:
        try:
            session = cls(
                preset_id=str(raw["preset_id"]),
                doneness_id=str(raw["doneness_id"]),
                label=str(raw["label"]),
                target_c=float(raw["target_c"]),
                carryover_c=float(raw.get("carryover_c", 0.0)),
                rest_minutes=float(raw.get("rest_minutes", 0.0)),
                started_at=float(raw.get("started_at", time.time())),
            )
        except (KeyError, TypeError, ValueError):
            return None
        try:
            session.state = CookState(str(raw.get("state", "cooking")))
        except ValueError:
            session.state = CookState.COOKING
        for attr in ("peak_tip_c", "rest_started_at", "ready_at"):
            value = raw.get(attr)
            if value is not None:
                with contextlib.suppress(TypeError, ValueError):
                    setattr(session, attr, float(value))
        session.custom_target = bool(raw.get("custom_target", False))
        return session


@dataclass
class ProbeSlot:
    """A stable, numbered place a probe lives in.

    Slots exist so the ESPHome entity list can be fixed at startup - Home
    Assistant only reads it once per connection - while probes come and go.
    A probe keeps its slot across disconnects, and across restarts.
    """

    index: int
    address: str | None = None
    name: str | None = None
    connected: bool = False
    reading: Reading | None = None
    cook: CookSession | None = None
    estimator: CookEstimator = field(default_factory=CookEstimator)
    last_seen: float | None = None
    graph: list[tuple[float, float, float]] = field(default_factory=list)
    """``(wall_time, tip_c, ambient_c)`` decimated for display."""

    GRAPH_MAX_POINTS = 720
    GRAPH_MIN_INTERVAL_S = 10.0

    @property
    def slot_id(self) -> str:
        return f"probe_{self.index}"

    @property
    def display_name(self) -> str:
        return self.name or f"Probe {self.index}"

    def record_graph_point(self, reading: Reading) -> None:
        """Append to the display series, thinning as it grows."""
        if self.graph and reading.wall_time - self.graph[-1][0] < self.GRAPH_MIN_INTERVAL_S:
            return
        self.graph.append((reading.wall_time, reading.tip_c, reading.ambient_c))
        if len(self.graph) > self.GRAPH_MAX_POINTS:
            # Drop every other point from the older half, which halves the
            # resolution of old history rather than losing the start of the cook.
            half = len(self.graph) // 2
            self.graph = self.graph[:half:2] + self.graph[half:]

    def estimate(self) -> Estimate:
        if self.cook is None:
            return Estimate(None, Confidence.NONE, "no cook selected")
        if self.cook.state is CookState.READY:
            return Estimate(0.0, Confidence.OK, "ready to remove", model="reached")
        if self.cook.state in (CookState.RESTING, CookState.DONE):
            return Estimate(
                self.cook.rest_remaining_s or 0.0, Confidence.OK, "resting", model="rest"
            )
        return self.estimator.estimate(self.cook.pull_c)

    def to_dict(self, library: PresetLibrary, units: str) -> dict:
        """Serialise for the web UI."""
        estimate = self.estimate()
        reading = self.reading
        cook = self.cook
        preset = library.get(cook.preset_id) if cook else None

        def temp(value: float | None) -> float | None:
            if value is None:
                return None
            return round(c_to_f(value) if units == "F" else value, 1)

        total_remaining = estimate.seconds_remaining
        if (
            total_remaining is not None
            and cook is not None
            and cook.state is CookState.COOKING
        ):
            # "Ready at" includes the rest, not just the time on the heat.
            total_remaining += cook.rest_minutes * 60.0

        return {
            "index": self.index,
            "slot_id": self.slot_id,
            "address": self.address,
            "name": self.display_name,
            "connected": self.connected,
            "last_seen": self.last_seen,
            "units": units,
            "tip": temp(reading.tip_c if reading else None),
            "ambient": temp(reading.ambient_c if reading else None),
            "battery": reading.battery_pct if reading else None,
            "rssi": reading.rssi if reading else None,
            "cook": (
                {
                    **cook.to_dict(),
                    "target": temp(cook.target_c),
                    "pull": temp(cook.pull_c),
                    "peak": temp(cook.peak_tip_c),
                    "preset_label": preset.label if preset else cook.label,
                    "preset_note": preset.note if preset else "",
                    "rest_remaining_s": cook.rest_remaining_s,
                }
                if cook
                else None
            ),
            "estimate": {
                "seconds_remaining": estimate.seconds_remaining,
                "seconds_until_served": total_remaining,
                "confidence": str(estimate.confidence),
                "reason": estimate.reason,
                "rate_per_min": (
                    round(estimate.rate_c_per_min * (9 / 5 if units == "F" else 1), 2)
                    if estimate.rate_c_per_min is not None
                    else None
                ),
                "model": estimate.model,
            },
            "graph": [
                [round(t, 1), temp(tip), temp(amb)] for t, tip, amb in self.graph
            ],
        }


def build_session(
    preset: Preset, doneness: Doneness, target_c: float | None = None
) -> CookSession:
    """Start a cook from a preset selection."""
    custom = target_c is not None and abs(target_c - doneness.target_c) > 0.05
    return CookSession(
        preset_id=preset.id,
        doneness_id=doneness.id,
        label=(
            f"{preset.category} {preset.label}"
            if len(preset.donenesses) == 1
            else f"{preset.category} {preset.label}, {doneness.label}"
        ),
        target_c=doneness.target_c if target_c is None else target_c,
        carryover_c=preset.carryover_c,
        rest_minutes=preset.rest_minutes,
        custom_target=custom,
    )
