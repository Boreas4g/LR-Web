import pytest

from car_ctrl.car_odometry import CarOdometry


def test_gyro_bias_is_learned_while_stationary_and_subtracted_when_moving() -> None:
    node = CarOdometry.__new__(CarOdometry)
    node.gyro_bias_estimation_enabled = True
    node.gyro_bias_alpha = 0.1
    node.gyro_bias_z = 0.0
    node._wheels_stationary = lambda: True

    rate = 0.0
    for _ in range(50):
        rate = node._gyro_rate_with_bias(0.007)

    assert node.gyro_bias_z == pytest.approx(0.007, abs=1e-4)
    assert rate == pytest.approx(0.0, abs=1e-4)

    node._wheels_stationary = lambda: False
    assert node._gyro_rate_with_bias(0.107) == pytest.approx(0.1, abs=1e-3)

    # 行驶中零偏不再被更新
    bias_before = node.gyro_bias_z
    node._gyro_rate_with_bias(0.05)
    assert node.gyro_bias_z == bias_before


def test_gyro_bias_estimation_can_be_disabled() -> None:
    node = CarOdometry.__new__(CarOdometry)
    node.gyro_bias_estimation_enabled = False
    node.gyro_bias_z = 0.0
    node._wheels_stationary = lambda: True
    assert node._gyro_rate_with_bias(0.007) == 0.007
    assert node.gyro_bias_z == 0.0
