from car_ctrl.car_odometry import CarOdometry


def test_wheel_speed_expires_after_timeout(monkeypatch) -> None:
    node = CarOdometry.__new__(CarOdometry)
    node.latest_wheel_speed = [0.1, 0.1, 0.1, 0.1]
    node.latest_wheel_speed_receive_time = 10.0
    node.wheel_speed_timeout = 0.5

    monkeypatch.setattr("car_ctrl.car_odometry.time.monotonic", lambda: 10.4)
    assert node._has_fresh_wheel_speed() is True

    monkeypatch.setattr("car_ctrl.car_odometry.time.monotonic", lambda: 10.6)
    assert node._has_fresh_wheel_speed() is False
