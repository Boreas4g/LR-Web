import math

import numpy as np
import pytest

from car_ctrl.scan_odometry import (
    ScanMatch,
    apply_translation_deadband,
    estimate_constrained_translation,
    scan_points,
)


def _current_scan_points(
    previous_points: np.ndarray, translation: np.ndarray, yaw_delta: float
) -> np.ndarray:
    """Express fixed world points in the current base frame."""
    rotation = np.array(
        [[math.cos(yaw_delta), -math.sin(yaw_delta)], [math.sin(yaw_delta), math.cos(yaw_delta)]]
    )
    return (previous_points - translation) @ rotation


def _match(previous_points: np.ndarray, current_points: np.ndarray, yaw_delta: float):
    return estimate_constrained_translation(
        previous_points,
        current_points,
        yaw_delta,
        max_correspondence_distance=0.20,
        min_correspondences=30,
        max_translation=0.20,
        max_residual_rms=0.03,
        iterations=8,
    )


def test_constrained_scan_match_recovers_small_hand_push() -> None:
    rng = np.random.default_rng(12)
    previous_points = rng.uniform([-4.0, -3.0], [5.0, 4.0], size=(300, 2))
    translation = np.array([0.064, -0.037])
    yaw_delta = math.radians(3.0)

    match = _match(
        previous_points,
        _current_scan_points(previous_points, translation, yaw_delta),
        yaw_delta,
    )

    assert match.valid is True
    assert match.correspondence_count >= 250
    assert match.residual_rms < 0.001
    assert match.translation_x == pytest.approx(translation[0], abs=0.002)
    assert match.translation_y == pytest.approx(translation[1], abs=0.002)


def test_scan_match_rejects_displacement_beyond_per_frame_bound() -> None:
    rng = np.random.default_rng(5)
    previous_points = rng.uniform([-3.0, -3.0], [3.0, 3.0], size=(200, 2))

    match = _match(
        previous_points,
        _current_scan_points(previous_points, np.array([0.35, 0.0]), 0.0),
        0.0,
    )

    assert match.valid is False


def test_scan_points_omits_invalid_ranges_and_keeps_xy_shape() -> None:
    points = scan_points(
        [0.1, 1.0, float("inf"), float("nan"), 2.0],
        0.0,
        math.pi / 2.0,
        min_range=0.2,
        max_range=1.5,
        stride=1,
    )

    assert points.shape == (1, 2)
    assert points[0, 0] == pytest.approx(0.0, abs=1e-7)
    assert points[0, 1] == pytest.approx(1.0, abs=1e-7)


def test_translation_deadband_preserves_quality_but_prevents_stationary_drift() -> None:
    result = apply_translation_deadband(
        ScanMatch(True, translation_x=0.002, translation_y=-0.003, correspondence_count=60, residual_rms=0.01),
        deadband=0.006,
    )

    assert result.valid is True
    assert result.translation_x == 0.0
    assert result.translation_y == 0.0
    assert result.correspondence_count == 60


def test_small_motion_can_accumulate_against_the_same_reference() -> None:
    rng = np.random.default_rng(8)
    previous_points = rng.uniform([-3.0, -3.0], [3.0, 3.0], size=(200, 2))
    # A 4 mm first frame is below the deadband. Matching the next frame to the
    # unchanged reference must still recover the accumulated 8 mm hand-push.
    match = _match(
        previous_points,
        _current_scan_points(previous_points, np.array([0.008, 0.0]), 0.0),
        0.0,
    )

    assert match.valid is True
    assert match.translation_x == pytest.approx(0.008, abs=0.002)
