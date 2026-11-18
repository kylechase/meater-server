"""The time-remaining estimator."""

from __future__ import annotations

import math

import pytest

from meater_server.estimator import Confidence, CookEstimator

TARGET = 57.2


def newton_cook(estimator: CookEstimator, ambient: float, start: float, k: float,
                until: float, step: float = 5.0, noise: float = 0.0) -> None:
    """Feed a physically-shaped heating curve into the estimator."""
    import random

    random.seed(11)
    t = 0.0
    while t <= until:
        true = ambient - (ambient - start) * math.exp(-k * t)
        estimator.add(t, true + (random.gauss(0, noise) if noise else 0.0), ambient)
        t += step


def true_seconds(ambient: float, start: float, k: float, target: float) -> float:
    return math.log((ambient - start) / (ambient - target)) / k


def test_recovers_the_true_eta_on_a_clean_curve():
    ambient, start, k = 150.0, 5.0, 1.2336e-4
    estimator = CookEstimator()
    elapsed = 1800.0
    newton_cook(estimator, ambient, start, k, elapsed)

    expected = true_seconds(ambient, start, k, TARGET) - elapsed
    estimate = estimator.estimate(TARGET)
    assert estimate.model == "newton"
    assert estimate.confidence is Confidence.OK
    assert estimate.seconds_remaining == pytest.approx(expected, rel=0.03)


def test_stays_accurate_with_realistic_sensor_noise():
    ambient, start, k = 150.0, 5.0, 1.2336e-4
    estimator = CookEstimator()
    elapsed = 2400.0
    newton_cook(estimator, ambient, start, k, elapsed, noise=0.3)

    expected = true_seconds(ambient, start, k, TARGET) - elapsed
    estimate = estimator.estimate(TARGET)
    # Within a minute on an hour-long cook.
    assert abs(estimate.seconds_remaining - expected) < 60


def test_newton_beats_a_straight_line_on_a_low_and_slow_cook():
    """Brisket at 225 F is where a straight-line projection falls apart.

    The target sits close to the ambient temperature, so most of the cook is
    spent on the flat part of the curve.  Projecting the current rate forward
    says four hours when the honest answer is six and a half.
    """
    ambient, start, target, k = 107.0, 5.0, 95.0, 8.0e-5
    estimator = CookEstimator()
    elapsed = 3600.0
    newton_cook(estimator, ambient, start, k, elapsed)

    expected = true_seconds(ambient, start, k, target) - elapsed
    estimate = estimator.estimate(target)
    history = estimator.history
    rate = (history[-1][1] - history[-2][1]) / (history[-1][0] - history[-2][0])
    linear = (target - history[-1][1]) / rate

    assert estimate.model == "newton"
    assert estimate.seconds_remaining == pytest.approx(expected, rel=0.05)
    # The straight line lands under half the real remaining time.
    assert linear < expected * 0.55


def test_no_estimate_before_enough_history():
    estimator = CookEstimator()
    for t in (0.0, 10.0, 20.0):
        estimator.add(t, 20.0 + t * 0.05, 150.0)
    estimate = estimator.estimate(TARGET)
    assert estimate.seconds_remaining is None
    assert estimate.confidence is Confidence.NONE
    assert "warming up" in estimate.reason


def test_confidence_is_low_until_the_window_fills():
    estimator = CookEstimator()
    newton_cook(estimator, 150.0, 5.0, 1.2336e-4, until=100.0)
    assert estimator.estimate(TARGET).confidence is Confidence.LOW


def test_ambient_below_the_target_is_reported_as_unreachable():
    estimator = CookEstimator()
    newton_cook(estimator, 50.0, 5.0, 3.0e-4, until=900.0)
    estimate = estimator.estimate(TARGET)
    assert estimate.seconds_remaining is None
    assert estimate.model == "unreachable"
    assert "turn up the heat" in estimate.reason


def test_ambient_just_above_the_target_falls_back_to_linear():
    estimator = CookEstimator()
    newton_cook(estimator, TARGET + 2.0, 5.0, 3.0e-4, until=900.0)
    estimate = estimator.estimate(TARGET)
    assert estimate.model == "linear"
    assert estimate.confidence is Confidence.LOW


def test_a_stalled_probe_gets_no_estimate():
    estimator = CookEstimator()
    for i in range(0, 900, 5):
        estimator.add(float(i), 40.0, 110.0)
    estimate = estimator.estimate(TARGET)
    assert estimate.seconds_remaining is None
    assert "not rising" in estimate.reason


def test_a_cooling_probe_gets_no_estimate():
    estimator = CookEstimator()
    for i in range(0, 900, 5):
        estimator.add(float(i), 50.0 - i * 0.005, 20.0)
    assert estimator.estimate(TARGET).seconds_remaining is None


def test_already_at_target_reports_zero():
    estimator = CookEstimator()
    newton_cook(estimator, 150.0, 50.0, 5.0e-4, until=600.0)
    estimate = estimator.estimate(TARGET)
    assert estimate.seconds_remaining == 0.0
    assert estimate.model == "reached"


def test_upward_jumps_are_damped_but_drops_are_not():
    """Opening the lid should not make the countdown leap."""
    estimator = CookEstimator()
    newton_cook(estimator, 150.0, 5.0, 1.2336e-4, until=1200.0)
    settled = estimator.estimate(TARGET).seconds_remaining

    # Ambient collapses: the honest new estimate is much longer.
    for i in range(10):
        estimator.add(1200.0 + i * 5, 35.0 + i * 0.01, 70.0)
    jumped = estimator.estimate(TARGET).seconds_remaining
    assert jumped is not None
    assert jumped < settled * 6, "an upward jump should be smoothed, not taken raw"


def test_reset_clears_history_and_smoothing():
    estimator = CookEstimator()
    newton_cook(estimator, 150.0, 5.0, 1.2336e-4, until=1200.0)
    assert estimator.estimate(TARGET).seconds_remaining is not None
    estimator.reset()
    assert estimator.history == []
    assert estimator.ambient_c() is None
    assert estimator.estimate(TARGET).seconds_remaining is None


def test_window_bounds_the_history_it_keeps():
    estimator = CookEstimator(window_s=120.0)
    newton_cook(estimator, 150.0, 5.0, 1.2336e-4, until=1200.0, step=10.0)
    span = estimator.history[-1][0] - estimator.history[0][0]
    assert span <= 130.0


def test_ambient_uses_the_median_so_one_spike_does_not_matter():
    estimator = CookEstimator()
    for i in range(0, 400, 5):
        ambient = 1000.0 if i == 200 else 150.0   # one wild sample
        estimator.add(float(i), 20.0 + i * 0.02, ambient)
    assert estimator.ambient_c() == pytest.approx(150.0)
