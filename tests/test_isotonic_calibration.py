"""F5 calibrated conviction (reporting only): pool-adjacent-violators against a brute force, the
200-point fit threshold, and the isotonic block of the reliability report."""

import itertools
import random

import pytest
from cryptonite_execution import calibration


def _brute_force(ys: list[float]) -> list[float]:
    """Best non-decreasing step fit by exhaustive search over contiguous blocks (each block takes its mean)."""
    n = len(ys)
    best, best_sse = None, float("inf")
    for cuts in itertools.product([0, 1], repeat=n - 1):
        blocks, start = [], 0
        for i, cut in enumerate(cuts, start=1):
            if cut:
                blocks.append(ys[start:i])
                start = i
        blocks.append(ys[start:])
        means = [sum(b) / len(b) for b in blocks]
        if any(a > b + 1e-12 for a, b in itertools.pairwise(means)):
            continue
        fit = [m for m, b in zip(means, blocks, strict=True) for _ in b]
        sse = sum((y - f) ** 2 for y, f in zip(ys, fit, strict=True))
        if sse < best_sse - 1e-12:
            best, best_sse = fit, sse
    return best


@pytest.mark.parametrize("seed", range(40))
def test_pav_matches_brute_force_on_small_cases(seed):
    rng = random.Random(seed)
    ys = [float(rng.random() < 0.5) if seed % 2 else round(rng.random(), 3) for _ in range(rng.randint(1, 8))]
    fit = calibration.pav(ys)
    assert fit == pytest.approx(_brute_force(ys))
    assert all(a <= b + 1e-12 for a, b in itertools.pairwise(fit))  # monotone


def test_pav_known_case_and_weights():
    assert calibration.pav([1.0, 0.0, 0.0, 1.0]) == pytest.approx([1 / 3, 1 / 3, 1 / 3, 1.0])
    assert calibration.pav([1.0, 0.0], [3.0, 1.0]) == pytest.approx([0.75, 0.75])
    assert calibration.pav([]) == []


def test_isotonic_fit_is_a_monotone_step_function():
    rng = random.Random(7)
    points = [(c, rng.random() < c) for c in (rng.random() for _ in range(500))]
    steps = calibration.isotonic_fit(points)
    xs, ys = [p[0] for p in steps], [p[1] for p in steps]
    assert xs == sorted(xs) and all(a <= b for a, b in itertools.pairwise(ys))
    assert 0.0 <= ys[0] and ys[-1] <= 1.0


def test_fit_only_from_200_points():
    assert calibration.MIN_ISOTONIC_POINTS == 200
    few = [(0.6, i % 2 == 0) for i in range(199)]
    assert calibration.isotonic_report(few) == {"fitted": False, "n": 199, "min_points": 200, "points": []}
    enough = [(0.4, i % 4 == 0) for i in range(100)] + [(0.8, i % 4 != 0) for i in range(100)]
    rep = calibration.isotonic_report(enough)
    assert rep["fitted"] is True and rep["n"] == 200
    assert rep["points"] == [[0.4, 0.25], [0.8, 0.75]]


def test_reliability_report_carries_the_isotonic_block_and_keeps_its_ece():
    points = [(0.9, i < 10) for i in range(20)]
    rel = calibration.reliability(points)
    assert rel["ece_pp"] == pytest.approx(40.0)  # unchanged by F5
    assert rel["isotonic"] == {"fitted": False, "n": 20, "min_points": 200, "points": []}
    assert calibration.reliability([(0.7, True)] * 250)["isotonic"]["points"] == [[0.7, 1.0]]
