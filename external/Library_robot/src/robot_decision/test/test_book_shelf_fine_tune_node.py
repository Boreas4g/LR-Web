import math

import pytest

from robot_decision.book_shelf_fine_tune_node import (
    BookShelfFineTuneNode,
    _clamp,
    _normalize_angle,
    _quaternion_to_yaw,
)


def test_clamp_limits_magnitude_and_preserves_sign() -> None:
    assert _clamp(0.1, 0.2) == pytest.approx(0.1)
    assert _clamp(-0.1, 0.2) == pytest.approx(-0.1)
    assert _clamp(0.5, 0.2) == pytest.approx(0.2)
    assert _clamp(-0.5, 0.2) == pytest.approx(-0.2)


def test_quaternion_to_yaw_uses_the_standard_z_axis_formula() -> None:
    yaw = _quaternion_to_yaw(0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4))

    assert yaw == pytest.approx(math.pi / 2)


def test_normalize_angle_wraps_to_signed_pi() -> None:
    assert _normalize_angle(3 * math.pi) == pytest.approx(math.pi)


def test_target_errors_follow_the_base_frame_while_the_base_rotates() -> None:
    node = BookShelfFineTuneNode.__new__(BookShelfFineTuneNode)
    node._current_pose = lambda: (0.0, 0.1, math.pi / 2)

    x_error, y_error, yaw_error = node._target_errors(0.0, 0.0, 0.0)

    # Facing +y with the target 0.1 m behind: the offset sits entirely in the
    # base-frame x component, the same convention the patrol fine tune uses.
    assert x_error == pytest.approx(-0.1)
    assert y_error == pytest.approx(0.0)
    assert yaw_error == pytest.approx(-math.pi / 2)
