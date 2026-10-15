"""Robot status gates: mapping-mode mirror and command gating (no stale-offline)."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)


@pytest.fixture
def robot_slot():
    """Temporarily replace the LR-01 summary and restore it afterwards."""
    index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original = store.robots[index]
    try:
        yield index
    finally:
        store.robots[index] = original


def _set_robot(index: int, **update: object):
    store.robots[index] = store.robots[index].model_copy(update=update)
    return store.robots[index]


def test_no_stale_offline_watchdog_is_installed() -> None:
    """单机交付（控制站与唯一机器人同机）：不做遥测中断离线判定。"""
    assert not hasattr(store, "_apply_stale_offline")


def test_offline_robot_command_reports_offline(robot_slot) -> None:
    robot = _set_robot(robot_slot, status="offline")
    from app.schemas import RobotCommandRequest, User

    user = User(username="admin", name="值班管理员", role="系统管理员")
    with pytest.raises(ValueError, match="机器人已离线"):
        store.create_command(
            robot,
            RobotCommandRequest(command="navigate", payload={"mapId": "MAP-STALE"}),
            user,
            30,
        )


def test_mapping_mode_mirror_sets_and_clears_mapping_status(robot_slot) -> None:
    _set_robot(robot_slot, status="idle")
    store.sync_mapping_mode("mapping")
    robot = store.robots[robot_slot]
    assert robot.status == "mapping"
    assert "建图" in robot.currentTask
    store.sync_mapping_mode("navigation")
    robot = store.robots[robot_slot]
    assert robot.status == "idle"


def test_mapping_mode_mirror_keeps_fault_and_offline(robot_slot) -> None:
    _set_robot(robot_slot, status="fault")
    store.sync_mapping_mode("mapping")
    assert store.robots[robot_slot].status == "fault"
    _set_robot(robot_slot, status="offline")
    store.sync_mapping_mode("mapping")
    assert store.robots[robot_slot].status == "offline"


def test_mapping_mode_mirror_ignores_transition(robot_slot) -> None:
    _set_robot(robot_slot, status="idle")
    store.sync_mapping_mode("transition")
    assert store.robots[robot_slot].status == "idle"


def test_mapping_status_blocks_command_jog_and_map_switch(robot_slot) -> None:
    from app.schemas import ManualJogRequest, RobotCommandRequest, SwitchMapRequest, User

    user = User(username="admin", name="值班管理员", role="系统管理员")
    robot = _set_robot(robot_slot, status="mapping", mapId="MAP-STALE")
    with pytest.raises(ValueError, match="正在建图"):
        store.create_command(
            robot, RobotCommandRequest(command="navigate", payload={}), user, 30
        )
    with pytest.raises(ValueError, match="正在建图"):
        store.send_manual_jog("LR-01", ManualJogRequest(linear=0.1, angular=0.0, durationMs=300))
    with pytest.raises(ValueError, match="正在建图"):
        store.switch_robot_map("LR-01", SwitchMapRequest(mapId="MAP-OTHER"), user)


def test_mapping_emergency_stop_still_allowed(robot_slot) -> None:
    from app.schemas import RobotCommandRequest, User

    user = User(username="admin", name="值班管理员", role="系统管理员")
    robot = _set_robot(robot_slot, status="mapping")
    command = store.create_command(
        robot, RobotCommandRequest(command="emergency_stop", payload={}), user, 30
    )
    assert command.commandId
