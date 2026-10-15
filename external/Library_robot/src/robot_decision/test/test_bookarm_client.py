from pathlib import Path

import pytest

from robot_decision.bookarm_client import BookArmApiError, BookArmClient


def test_health_exposes_unhealthy_can_feedback_without_claiming_arm_ready(monkeypatch, tmp_path: Path) -> None:
    rod = tmp_path / "rod"
    lift = tmp_path / "lift"
    rod.touch()
    lift.touch()
    client = BookArmClient(
        "http://127.0.0.1:18080/api/v1",
        1.0,
        rod_device_path=rod,
        lift_device_path=lift,
    )
    responses = iter([
        {"gateway_started": True},
        {
            "state": {
                "service": {"started": True},
                "arm": {
                    "connected": True,
                    "enabled": False,
                    "can_fps": 0.0,
                    "feedback": {
                        "healthy": False,
                        "can_fps": 0.0,
                        "fresh_motor_ids": [],
                        "reason": "未收到 CAN 反馈帧",
                    },
                },
            },
        },
        {"camera": {"connected": True, "busy": False}},
    ])
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: next(responses))

    health = client.health()

    assert health.available is True
    assert health.arm_connected is True
    assert health.arm_feedback_healthy is False
    assert health.arm_ready is False
    assert health.arm_enabled is False
    assert health.arm_state == "fault"
    assert health.arm_can_fps == 0.0
    assert "未收到 CAN" in health.message


def test_health_reports_a_disabled_arm_with_live_can_feedback_as_standby(
    monkeypatch, tmp_path: Path
) -> None:
    rod = tmp_path / "rod"
    lift = tmp_path / "lift"
    rod.touch()
    lift.touch()
    client = BookArmClient(
        "http://127.0.0.1:18080/api/v1",
        1.0,
        rod_device_path=rod,
        lift_device_path=lift,
    )
    responses = iter(
        [
            {"gateway_started": True},
            {
                "state": {
                    "service": {"started": True},
                    "arm": {
                        "connected": True,
                        "enabled": False,
                        "status": {"joint_enabled": [False] * 7},
                        "feedback": {
                            "healthy": True,
                            "can_fps": 80.0,
                            "reason": "CAN 反馈正常",
                        },
                    },
                },
            },
            {"camera": {"connected": True, "busy": False}},
        ]
    )
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: next(responses))

    health = client.health()

    assert health.arm_ready is False
    assert health.arm_enabled is False
    assert health.arm_feedback_healthy is True
    assert health.arm_state == "standby"


def test_move_lift_rejects_a_failed_local_worker_job(monkeypatch, tmp_path: Path) -> None:
    client = BookArmClient(
        "http://127.0.0.1:18080/api/v1",
        1.0,
        rod_device_path=tmp_path / "rod",
        lift_device_path=tmp_path / "lift",
    )
    responses = iter(
        [
            {"job": {"status": "succeeded", "result": {"connected": True}}},
            {"job": {"status": "failed", "error": "升降台驱动器未返回完整 Modbus 回执"}},
        ]
    )
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(BookArmApiError, match="Modbus 回执"):
        client.move_lift(1.0)


def test_move_lift_returns_only_a_succeeded_driver_receipt(monkeypatch, tmp_path: Path) -> None:
    client = BookArmClient(
        "http://127.0.0.1:18080/api/v1",
        1.0,
        rod_device_path=tmp_path / "rod",
        lift_device_path=tmp_path / "lift",
    )
    result = {
        "accepted": True,
        "completion": "driver_acknowledged",
        "physical_completion_verified": False,
    }
    responses = iter(
        [
            {"job": {"status": "succeeded", "result": {"connected": True}}},
            {"job": {"status": "succeeded", "result": result}},
        ]
    )
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: next(responses))

    assert client.move_lift(-1.0) == result


def test_safe_stop_attempts_lift_stop_after_arm_cancel_failure(monkeypatch, tmp_path: Path) -> None:
    client = BookArmClient(
        "http://127.0.0.1:18080/api/v1",
        1.0,
        rod_device_path=tmp_path / "rod",
        lift_device_path=tmp_path / "lift",
    )
    calls: list[str] = []

    def request(_method: str, path: str, _payload: dict) -> dict:
        calls.append(path)
        if path == "/arm/cancel-motion":
            raise BookArmApiError("arm unavailable")
        return {"job": {"status": "succeeded", "result": {}}}

    monkeypatch.setattr(client, "_request", request)

    with pytest.raises(BookArmApiError, match="机械臂停止失败"):
        client.safe_stop()

    assert calls == ["/arm/cancel-motion", "/lift/stop"]
