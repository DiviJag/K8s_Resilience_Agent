from pathlib import Path

import pytest

from chaosloop.eval.calibration import (
    IsotonicCalibrator,
    brier_score,
    expected_calibration_error,
    mean_gap,
    reliability_bins,
)


def test_perfectly_calibrated_has_zero_ece():
    # 80% confident and right 4 times in 5; 20% confident and right 1 time in 5.
    confidences = [0.8] * 5 + [0.2] * 5
    correct = [True, True, True, True, False] + [True, False, False, False, False]
    assert expected_calibration_error(confidences, correct) == pytest.approx(0.0)


def test_overconfidence_shows_up_in_ece_and_gap():
    confidences = [0.95] * 10
    correct = [True] * 5 + [False] * 5
    assert expected_calibration_error(confidences, correct) == pytest.approx(0.45)
    assert mean_gap(confidences, correct) == pytest.approx(0.45)


def test_brier_score_extremes():
    assert brier_score([1.0, 0.0], [True, False]) == 0.0
    assert brier_score([1.0, 0.0], [False, True]) == 1.0
    assert brier_score([0.5], [True]) == pytest.approx(0.25)


def test_discrete_consistency_values_land_in_the_right_bins():
    """k/5 confidences sit on bin edges; float error must not drop 0.6 into the 0.5 bin."""
    bins = reliability_bins([0.2, 0.4, 0.6, 0.8, 1.0], [True] * 5, n_bins=10)
    populated = [(round(b.lower, 1), b.count) for b in bins if b.count]
    assert populated == [(0.2, 1), (0.4, 1), (0.6, 1), (0.8, 1), (0.9, 1)]


def test_bins_report_accuracy_and_mean_confidence():
    bins = reliability_bins([0.91, 0.99, 0.95], [True, False, True], n_bins=10)
    top = bins[-1]
    assert top.count == 3
    assert top.mean_confidence == pytest.approx(0.95)
    assert top.accuracy == pytest.approx(2 / 3)
    assert top.gap == pytest.approx(0.95 - 2 / 3)


def test_empty_inputs_are_not_errors():
    assert expected_calibration_error([], []) == 0.0
    assert brier_score([], []) == 0.0


def test_mismatched_lengths_rejected():
    with pytest.raises(ValueError):
        reliability_bins([0.5, 0.5], [True])


def test_isotonic_fit_is_monotone_and_pools_violators():
    # Raw accuracy dips at 0.6 (0.0) below 0.4 (0.5): PAV must pool them.
    confidences = [0.2, 0.4, 0.4, 0.6, 1.0, 1.0]
    correct = [False, True, False, False, True, True]
    calibrator = IsotonicCalibrator.fit(confidences, correct, fitted_on="dev")

    predictions = [calibrator.predict(c) for c in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)]
    assert predictions == sorted(predictions)
    assert calibrator.predict(0.4) == pytest.approx(calibrator.predict(0.6))
    assert calibrator.predict(0.4) == pytest.approx(1 / 3)
    assert calibrator.predict(1.0) == pytest.approx(1.0)


def test_isotonic_maps_overconfidence_down():
    calibrator = IsotonicCalibrator.fit([1.0] * 10, [True] * 7 + [False] * 3, fitted_on="dev")
    assert calibrator.predict(1.0) == pytest.approx(0.7)


def test_isotonic_interpolates_and_clamps():
    calibrator = IsotonicCalibrator.fit([0.2, 1.0], [False, True], fitted_on="dev")
    assert calibrator.predict(0.6) == pytest.approx(0.5)
    assert calibrator.predict(0.0) == 0.0
    assert calibrator.predict(1.0) == 1.0


def test_isotonic_round_trips(tmp_path: Path):
    calibrator = IsotonicCalibrator.fit([0.2, 0.8, 1.0], [False, True, True], fitted_on="run-x")
    loaded = IsotonicCalibrator.load(calibrator.save(tmp_path / "cal.json"))
    assert loaded == calibrator
    assert loaded.fitted_on == "run-x"


def test_fit_refuses_empty_data():
    with pytest.raises(ValueError):
        IsotonicCalibrator.fit([], [], fitted_on="dev")
