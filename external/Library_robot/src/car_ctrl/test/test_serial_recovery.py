import threading

from car_ctrl.ddsm_hat_diff_drive_node import DDSMHatDiffDriveNode


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def error(self, message: str) -> None:
        self.messages.append(message)

    def info(self, message: str) -> None:
        self.messages.append(message)

    def warn(self, message: str) -> None:
        self.messages.append(message)


class _Driver:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _node_for_recovery() -> DDSMHatDiffDriveNode:
    node = DDSMHatDiffDriveNode.__new__(DDSMHatDiffDriveNode)
    node._driver_lock = threading.Lock()
    node._feedback_lock = threading.Lock()
    node._driver = None
    node._last_reconnect_attempt = float("-inf")
    node._feedback_rpms = [12.0, 12.0, 12.0, 12.0]
    node._feedback_times = [1.0, 1.0, 1.0, 1.0]
    node._current_rpms = [20.0, 20.0, 20.0, 20.0]
    node._target_rpms = [20.0, 20.0, 20.0, 20.0]
    node._cmd_lock = threading.Lock()
    node._linear = 0.2
    node._angular = 0.1
    node.serial_reconnect_period = 1.0
    node.feedback_health_timeout = 2.0
    node.feedback_recovery_started_at = 0.0
    node.init_hat = True
    node.port = "/dev/chassis_serial_port"
    node.get_logger = lambda: _Logger()
    return node


def test_serial_failure_stops_outputs_and_drops_stale_feedback() -> None:
    node = _node_for_recovery()
    driver = _Driver()
    node._driver = driver

    node._mark_driver_unavailable(RuntimeError("write timeout"))

    assert driver.closed is True
    assert node._driver is None
    assert node._current_rpms == [0.0, 0.0, 0.0, 0.0]
    assert node._target_rpms == [0.0, 0.0, 0.0, 0.0]
    assert node._linear == 0.0
    assert node._angular == 0.0
    assert node._feedback_rpms == [None, None, None, None]


def test_serial_reconnect_initializes_a_new_driver_and_clears_old_feedback() -> None:
    node = _node_for_recovery()
    driver = _Driver()
    initialized = []
    node._create_driver = lambda: driver
    node._initialize_driver = initialized.append

    assert node._attempt_driver_reconnect(force=True) is True

    assert initialized == [driver]
    assert node._driver is driver
    assert node._feedback_rpms == [None, None, None, None]


def test_missing_rs485_feedback_is_unhealthy_after_grace_period(monkeypatch) -> None:
    node = _node_for_recovery()
    node.use_motor_feedback = True
    node.driver_backend = "ddsm_rs485"
    node._feedback_recovery_started_at = 10.0
    node._current_rpms = [10.0, 10.0, 10.0, 10.0]

    monkeypatch.setattr("car_ctrl.ddsm_hat_diff_drive_node.time.monotonic", lambda: 11.9)
    assert node._feedback_is_unhealthy() is False

    monkeypatch.setattr("car_ctrl.ddsm_hat_diff_drive_node.time.monotonic", lambda: 12.1)
    assert node._feedback_is_unhealthy() is True


def test_missing_rs485_feedback_is_allowed_while_stationary(monkeypatch) -> None:
    node = _node_for_recovery()
    node.use_motor_feedback = True
    node.driver_backend = "ddsm_rs485"
    node._feedback_recovery_started_at = 10.0
    node._current_rpms = [0.0, 0.0, 0.0, 0.0]

    monkeypatch.setattr("car_ctrl.ddsm_hat_diff_drive_node.time.monotonic", lambda: 20.0)
    assert node._feedback_is_unhealthy() is False


def test_missing_feedback_never_becomes_command_based_wheel_speed(monkeypatch) -> None:
    node = _node_for_recovery()
    node.wheel_radius = 0.05
    node.motor_signs = [1, -1, 1, -1]
    node.use_motor_feedback = True
    node.feedback_timeout = 0.5
    node._current_rpms = [60.0, -60.0, 60.0, -60.0]
    node._feedback_rpms = [None, None, None, None]
    node._feedback_times = [0.0, 0.0, 0.0, 0.0]

    monkeypatch.setattr("car_ctrl.ddsm_hat_diff_drive_node.time.monotonic", lambda: 10.0)
    speeds, sources = node._current_physical_wheel_speeds()

    assert speeds == [0.0, 0.0, 0.0, 0.0]
    assert sources == ["feedback_unavailable"] * 4
