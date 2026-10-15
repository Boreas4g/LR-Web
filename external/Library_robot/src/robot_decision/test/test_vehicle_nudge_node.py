import threading

import pytest

import robot_decision.vehicle_nudge_node as nudge_module
from geometry_msgs.msg import Twist
from robot_decision.vehicle_nudge_node import VehicleNudgeCore


class _Publisher:
    def __init__(self) -> None:
        self.messages: list[Twist] = []

    def publish(self, message: Twist) -> None:
        self.messages.append(message)


def _core_for_timer_test() -> VehicleNudgeCore:
    core = VehicleNudgeCore.__new__(VehicleNudgeCore)
    core._lock = threading.Lock()
    core._target_linear = 0.0
    core._out_linear = 0.0
    core._last_published_nonzero = False
    core._deadline = 0.0
    core._commanded_at = 0.0
    core._travel_m = 0.0
    core._travel_locked_until = 0.0
    core._last_tick = 10.0
    core.accel = 0.12
    core.rate_hz = 20.0
    core.watchdog_ms = 800
    core.pulse_speed = 0.03
    core.max_pulse_ms = 500
    core.travel_cap_m = 0.25
    core._cmd_pub = _Publisher()
    return core


def test_idle_timer_does_not_publish_zero_velocity(monkeypatch) -> None:
    monkeypatch.setattr(nudge_module.time, "monotonic", lambda: 10.0)
    core = _core_for_timer_test()

    core._on_timer()
    core._on_timer()

    assert core._cmd_pub.messages == []


def test_pulse_streams_velocity_then_publishes_one_stop(monkeypatch) -> None:
    monkeypatch.setattr(nudge_module.time, "monotonic", lambda: 10.0)
    core = _core_for_timer_test()
    core._target_linear = 0.03
    core._deadline = 11.0
    core._commanded_at = 10.0

    core._on_timer()
    core._on_timer()
    assert core._cmd_pub.messages[-1].linear.x > 0.0

    core._target_linear = 0.012
    core._out_linear = 0.004
    core._deadline = 9.0
    core._on_timer()
    core._on_timer()

    assert core._cmd_pub.messages[-1].linear.x == 0.0
    assert sum(message.linear.x == 0.0 for message in core._cmd_pub.messages) == 1


@pytest.mark.parametrize("direction, sign", [("forward", 1), ("backward", -1)])
def test_pulse_api_remains_available_after_idle(monkeypatch, direction, sign) -> None:
    monkeypatch.setattr(nudge_module.time, "monotonic", lambda: 10.0)
    core = _core_for_timer_test()
    core._on_timer()

    assert core.pulse(direction, 300)["ok"]
    core._on_timer()

    assert core._cmd_pub.messages[-1].linear.x * sign > 0.0


def test_watchdog_sends_one_stop_then_becomes_silent(monkeypatch) -> None:
    clock = [10.0]
    monkeypatch.setattr(nudge_module.time, "monotonic", lambda: clock[0])
    core = _core_for_timer_test()
    assert core.pulse("forward", 500)["ok"]
    core._on_timer()

    clock[0] = 11.0
    core._on_timer()
    core._on_timer()

    assert [message.linear.x for message in core._cmd_pub.messages] == [0.006, 0.0]
    assert core._target_linear == core._out_linear == 0.0


def test_stop_finishes_ramp_then_does_not_overwrite_other_commands(monkeypatch) -> None:
    monkeypatch.setattr(nudge_module.time, "monotonic", lambda: 10.0)
    core = _core_for_timer_test()
    core.pulse("forward", 500)
    for _ in range(5):
        core._on_timer()
    core.stop()
    for _ in range(10):
        core._on_timer()

    messages = core._cmd_pub.messages
    assert messages[-1].linear.x == 0.0
    assert sum(message.linear.x == 0.0 for message in messages) == 1
    count = len(messages)
    core.stop()
    core._on_timer()
    assert len(messages) == count


def test_failed_stop_publish_is_retried(monkeypatch) -> None:
    monkeypatch.setattr(nudge_module.time, "monotonic", lambda: 10.0)
    core = _core_for_timer_test()
    core.pulse("forward", 500)
    core._on_timer()
    core.stop()
    publish = core._cmd_pub.publish

    def fail_publish(_message) -> None:
        raise RuntimeError("publisher unavailable")

    core._cmd_pub.publish = fail_publish
    core._on_timer()
    assert core._last_published_nonzero

    core._cmd_pub.publish = publish
    core._on_timer()
    core._on_timer()
    assert [message.linear.x for message in core._cmd_pub.messages] == [0.006, 0.0]


def test_invalid_pulse_does_not_claim_velocity_output(monkeypatch) -> None:
    monkeypatch.setattr(nudge_module.time, "monotonic", lambda: 10.0)
    core = _core_for_timer_test()
    assert not core.pulse("forward", 501)["ok"]
    assert not core.pulse("sideways", 100)["ok"]
    core._on_timer()
    assert core._cmd_pub.messages == []
