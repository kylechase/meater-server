"""Estimating how much longer the cook has to run.

Meat in an oven or a covered grill heats the way Newton's law of cooling
describes: the centre approaches the ambient temperature asymptotically, so
the last few degrees take far longer than the first few.  A straight-line
projection of the current rate is badly optimistic near the target, which is
exactly where you care.

So the estimator fits

    T(t) = T_ambient - (T_ambient - T_0) * exp(-k * t)

over a sliding window of recent samples.  Taking logs makes that a straight
line, ``ln(T_ambient - T) = ln(T_ambient - T_0) - k*t``, so ``k`` comes from
an ordinary least-squares fit.  Time remaining then has a closed form::

    t = (1 / k) * ln((T_ambient - T_now) / (T_ambient - T_target))

When the fit cannot be trusted - too little history, ambient not hot enough
to ever reach the target, temperature falling - the estimator says so instead
of guessing, falling back to a linear projection only where that is the more
honest answer.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from statistics import median


class Confidence(StrEnum):
    """How much to trust an estimate."""

    NONE = "none"
    """No estimate: not enough data, or the target is unreachable."""
    LOW = "low"
    """An estimate exists but the fit is young or noisy."""
    OK = "ok"
    """A settled fit over a full window."""


@dataclass(frozen=True, slots=True)
class Estimate:
    """The estimator's answer."""

    seconds_remaining: float | None
    confidence: Confidence
    reason: str
    rate_c_per_min: float | None = None
    model: str = "none"
    smoothed_tip_c: float | None = None

    @property
    def minutes_remaining(self) -> float | None:
        if self.seconds_remaining is None:
            return None
        return self.seconds_remaining / 60.0


# Tunables.  These are deliberately conservative: a missing estimate reads
# better than a wrong one.
MIN_SAMPLES = 4
MIN_SPAN_S = 75.0
"""Shortest window of history that may produce an estimate at all."""
FULL_SPAN_S = 240.0
"""Window length at which an estimate is promoted to OK confidence."""
WINDOW_S = 420.0
"""How much history the fit considers."""
MIN_RISE_C = 0.6
"""Smaller than this over the window and the rate is indistinguishable from noise."""
AMBIENT_HEADROOM_C = 4.0
"""Ambient must beat the target by this much for the Newton fit to converge."""
MAX_ESTIMATE_S = 36 * 3600.0
SMOOTHING = 0.35
"""EMA weight applied to successive estimates, to stop the countdown jittering."""


class CookEstimator:
    """Rolling temperature history for one probe, and the estimate from it."""

    __slots__ = ("_ambient", "_history", "_smoothed_s", "_window_s")

    def __init__(self, window_s: float = WINDOW_S) -> None:
        self._window_s = window_s
        self._history: deque[tuple[float, float]] = deque()
        self._ambient: deque[tuple[float, float]] = deque()
        self._smoothed_s: float | None = None

    def reset(self) -> None:
        """Forget all history, e.g. when a new cook starts."""
        self._history.clear()
        self._ambient.clear()
        self._smoothed_s = None

    def add(self, timestamp: float, tip_c: float, ambient_c: float) -> None:
        """Record a reading.  ``timestamp`` must be monotonic seconds."""
        self._history.append((timestamp, tip_c))
        self._ambient.append((timestamp, ambient_c))
        self._trim(timestamp)

    def _trim(self, now: float) -> None:
        cutoff = now - self._window_s
        while len(self._history) > MIN_SAMPLES and self._history[0][0] < cutoff:
            self._history.popleft()
        while len(self._ambient) > MIN_SAMPLES and self._ambient[0][0] < cutoff:
            self._ambient.popleft()

    @property
    def history(self) -> list[tuple[float, float]]:
        return list(self._history)

    def ambient_c(self) -> float | None:
        """Median ambient over the window, which is steadier than one sample."""
        if not self._ambient:
            return None
        return median(v for _, v in self._ambient)

    def estimate(self, target_c: float) -> Estimate:
        """Seconds until the tip reaches ``target_c``."""
        samples = list(self._history)
        if len(samples) < MIN_SAMPLES:
            return Estimate(None, Confidence.NONE, "warming up: not enough readings yet")

        t0 = samples[0][0]
        span = samples[-1][0] - t0
        if span < MIN_SPAN_S:
            return Estimate(
                None,
                Confidence.NONE,
                f"warming up: need {MIN_SPAN_S - span:.0f}s more history",
            )

        times = [t - t0 for t, _ in samples]
        temps = [v for _, v in samples]

        slope, intercept = _linear_fit(times, temps)
        tip_now = intercept + slope * times[-1]
        rate_per_min = slope * 60.0
        confidence = Confidence.OK if span >= FULL_SPAN_S else Confidence.LOW

        if tip_now >= target_c:
            return Estimate(
                0.0, confidence, "target reached", rate_per_min, "reached", tip_now
            )

        rise = max(temps) - min(temps)
        if slope <= 0 or rise < MIN_RISE_C:
            return Estimate(
                None,
                Confidence.NONE,
                "temperature is not rising",
                rate_per_min,
                "none",
                tip_now,
            )

        ambient = self.ambient_c()
        if ambient is not None and ambient <= target_c:
            # The centre can never pass the air around it.  No amount of
            # waiting fixes this; the heat has to go up.
            return Estimate(
                None,
                Confidence.NONE,
                f"ambient {ambient:.0f} C is below the {target_c:.0f} C target - "
                "turn up the heat",
                rate_per_min,
                "unreachable",
                tip_now,
            )

        newton = None
        if ambient is not None and ambient >= target_c + AMBIENT_HEADROOM_C:
            newton = _newton_seconds(times, temps, ambient, tip_now, target_c)

        if newton is not None:
            seconds, model = newton, "newton"
        else:
            seconds, model = (target_c - tip_now) / slope, "linear"
            if ambient is not None and ambient < target_c + AMBIENT_HEADROOM_C:
                # Honest about it: the projection ignores the asymptote it is
                # about to run into.
                confidence = Confidence.LOW

        if not math.isfinite(seconds) or seconds < 0:
            return Estimate(
                None, Confidence.NONE, "fit did not converge", rate_per_min, "none", tip_now
            )

        seconds = min(seconds, MAX_ESTIMATE_S)
        seconds = self._smooth(seconds)

        reason = "ok"
        if model == "linear":
            reason = (
                "linear projection: ambient is not hot enough above the target "
                "for a settled fit"
            )
        elif confidence is Confidence.LOW:
            reason = "fit is still young"
        return Estimate(seconds, confidence, reason, rate_per_min, model, tip_now)

    def _smooth(self, seconds: float) -> float:
        if self._smoothed_s is None:
            self._smoothed_s = seconds
        else:
            # Let the estimate fall freely but damp upward jumps, so a lid
            # opening does not make the countdown leap.
            alpha = 1.0 if seconds < self._smoothed_s else SMOOTHING
            self._smoothed_s = alpha * seconds + (1.0 - alpha) * self._smoothed_s
        return self._smoothed_s


def _linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least squares, returning ``(slope, intercept)``."""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, mean_y
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx
    return slope, mean_y - slope * mean_x


def _newton_seconds(
    times: list[float],
    temps: list[float],
    ambient: float,
    tip_now: float,
    target_c: float,
) -> float | None:
    """Seconds to ``target_c`` from a log-linearised Newton heating fit."""
    xs: list[float] = []
    ys: list[float] = []
    for t, v in zip(times, temps, strict=True):
        gap = ambient - v
        if gap <= 0.5:
            # Already at ambient; the log blows up and the sample tells us
            # nothing about the approach rate.
            continue
        xs.append(t)
        ys.append(math.log(gap))
    if len(xs) < MIN_SAMPLES:
        return None

    slope, _ = _linear_fit(xs, ys)
    k = -slope
    if k <= 1e-7:
        return None

    gap_now = ambient - tip_now
    gap_target = ambient - target_c
    if gap_now <= 0 or gap_target <= 0:
        return None
    return math.log(gap_now / gap_target) / k
