from base64 import b64encode
from datetime import UTC, datetime, timedelta
import threading
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.mqtt import MqttPublisher
from app.schemas import RobotPose

client = TestClient(app)


def _fire_switch_event(event: str, details: dict | None = None, delay: float = 0.3) -> None:
    """Deliver a gateway map-switch event for the newest switch_map command."""

    def _deliver() -> None:
        from app.store import store

        command = next(
            (item for item in reversed(list(store.commands.values())) if item.command == "switch_map"),
            None,
        )
        if command is None:
            return
        store.handle_gateway_event(
            {
                "event": event,
                "robotId": command.robotId,
                "commandId": command.commandId,
                "details": details or {},
            }
        )

    threading.Timer(delay, _deliver).start()


def mark_robot_localized(robot_id: str = "LR-01") -> None:
    """Prepare the in-memory robot for tests that intentionally dispatch navigation."""
    from app.store import store

    index = next(index for index, item in enumerate(store.robots) if item.id == robot_id)
    robot = store.robots[index]
    store.robots[index] = robot.model_copy(
        update={
            "localizationStatus": "localized",
            "pose": RobotPose(
                frameId="map",
                x=0.0,
                y=0.0,
                yaw=0.0,
                capturedAt=datetime.now(UTC),
            ),
        }
    )


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["station"] == {
        "product": "lr-control-station",
        "id": "development-station",
        "name": "LR 控制站",
    }


def test_health_is_degraded_when_required_mqtt_is_unavailable() -> None:
    from app.store import store

    original_required_mqtt = store.require_mqtt
    original_publisher = store.publisher
    try:
        store.require_mqtt = True
        store.publisher = None
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
    finally:
        store.require_mqtt = original_required_mqtt
        store.publisher = original_publisher


def test_business_endpoints_accept_operator_when_auth_disabled() -> None:
    # 交付态（auth_enabled=False）：无登录，请求以合成操作员身份通过。
    response = client.get("/api/v1/robots")
    assert response.status_code == 200


def test_business_endpoints_require_authentication_when_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    response = client.get("/api/v1/robots")
    assert response.status_code == 401


def test_login_and_list_robots() -> None:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    assert login.status_code == 200
    token = login.json()["token"]

    response = client.get("/api/v1/robots", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()[0]["id"] == "LR-01"


def test_auth_me_returns_operator_without_token_when_auth_disabled() -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json()["user"] == {"username": "operator", "name": "操作员", "role": "系统管理员"}


def test_current_user_endpoint_returns_operator_when_auth_disabled() -> None:
    response = client.get("/api/v1/current-user")
    assert response.status_code == 200
    assert response.json()["role"] == "系统管理员"


def test_register_returns_operator_without_persisting_when_auth_disabled() -> None:
    from app.store import store

    response = client.post(
        "/api/v1/auth/register",
        json={"username": "newbie", "password": "secret1", "name": "新馆员"},
    )
    assert response.status_code == 201
    assert response.json()["username"] == "operator"
    assert "newbie" not in store.users


def test_mqtt_standard_telemetry_topic_is_normalized_to_a_gateway_event() -> None:
    received: list[dict[str, object]] = []
    publisher = object.__new__(MqttPublisher)
    publisher._event_handler = received.append
    publisher._on_message(
        None,
        None,
        SimpleNamespace(
            topic="fleet/LR-01/telemetry",
            payload=(
                b'{"mapId":"MAP-TEST","frameId":"map","nav2Ready":true,'
                b'"chassisReady":true,"localizationStatus":"localized",'
                b'"localizationScanMode":"dual",'
                b'"x":1.0,"y":2.0,"yaw":0.5}'
            ),
        ),
    )
    assert len(received) == 1
    event = received[0]
    assert event["event"] == "telemetry"
    assert event["robotId"] == "LR-01"
    assert isinstance(event["timestamp"], str)
    assert event["details"] == {
        "mapId": "MAP-TEST",
        "frameId": "map",
        "nav2Ready": True,
        "chassisReady": True,
        "localizationStatus": "localized",
        "localizationScanMode": "dual",
        "x": 1.0,
        "y": 2.0,
        "yaw": 0.5,
    }


def test_mqtt_tls_requires_a_secure_url_and_ca() -> None:
    with pytest.raises(ValueError, match="mqtts://"):
        MqttPublisher(
            Settings(
                mqtt_url="mqtt://broker.example:1883",
                mqtt_tls_enabled=True,
                mqtt_ca_file="/tmp/ca.crt",
            )
        )
    with pytest.raises(ValueError, match="CA_FILE"):
        MqttPublisher(
            Settings(
                mqtt_url="mqtts://broker.example:8883",
                mqtt_tls_enabled=True,
                mqtt_ca_file="",
            )
        )


def test_robot_realtime_websocket_returns_snapshot_and_telemetry_update() -> None:
    from app.realtime import robot_realtime_hub
    from app.store import store

    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    protocol = f"lr-auth.{login.json()['token']}"
    with client.websocket_connect("/api/v1/realtime/robots", subprotocols=[protocol]) as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "robots.snapshot"
        robot = next(item for item in store.robots if item.id == "LR-01")
        robot_realtime_hub.publish_robot(robot)
        update = websocket.receive_json()
        assert update["type"] == "robot.updated"
        assert update["robot"]["id"] == "LR-01"


def test_robot_realtime_websocket_accepts_operator_without_token_when_auth_disabled() -> None:
    # 交付态（auth_enabled=False）：浏览器不带 token 也能订阅实时状态。
    with client.websocket_connect("/api/v1/realtime/robots") as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "robots.snapshot"


def test_robot_realtime_websocket_requires_token_when_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/v1/realtime/robots"):
        pass


def test_create_robot_persists_in_api_store() -> None:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    created = client.post(
        "/api/v1/robots",
        json={"id": "LR-04", "name": "智巡四号", "gatewayHost": "192.168.10.104"},
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["status"] == "offline"
    robots = client.get("/api/v1/robots", headers=headers).json()
    assert any(robot["id"] == "LR-04" for robot in robots)


def test_command_idempotency() -> None:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"command": "charge", "idempotencyKey": "command-key-001"}

    first = client.post("/api/v1/robots/LR-01/commands", json=payload, headers=headers)
    second = client.post("/api/v1/robots/LR-01/commands", json=payload, headers=headers)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["commandId"] == second.json()["commandId"]


def test_fault_robot_rejects_navigation() -> None:
    from app.store import store

    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/robots",
        json={"id": "LR-03", "name": "智巡三号", "gatewayHost": "192.168.10.103"},
        headers=headers,
    )
    assert created.status_code == 201
    fault_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-03")
    store.robots[fault_index] = store.robots[fault_index].model_copy(update={"status": "fault"})
    try:
        response = client.post(
            "/api/v1/robots/LR-03/commands", json={"command": "navigate"}, headers=headers
        )
        assert response.status_code == 409
    finally:
        store.robots = [item for item in store.robots if item.id != "LR-03"]


def test_delete_robot_removes_robot_and_its_tasks() -> None:
    from app.schemas import DispatchTask
    from app.store import store

    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/robots",
        json={"id": "LR-02", "name": "智巡二号", "gatewayHost": "192.168.10.102"},
        headers=headers,
    )
    assert created.status_code == 201
    store.tasks = [
        DispatchTask(
            id="TSK-DELETE-LR02",
            type="inventory",
            robotId="LR-02",
            target="一层 / B 区 / B-02",
            status="running",
            progress=10,
            updatedAt="刚刚",
        ),
        *store.tasks,
    ]
    try:
        response = client.delete("/api/v1/robots/LR-02", headers=headers)
        assert response.status_code == 204
        robots = client.get("/api/v1/robots", headers=headers).json()
        tasks = client.get("/api/v1/dispatch/tasks", headers=headers).json()
        assert all(robot["id"] != "LR-02" for robot in robots)
        assert all(task["robotId"] != "LR-02" for task in tasks)
    finally:
        store.robots = [item for item in store.robots if item.id != "LR-02"]
        store.tasks = [task for task in store.tasks if task.robotId != "LR-02"]


def test_expired_command_is_rejected() -> None:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    token = login.json()["token"]
    response = client.post(
        "/api/v1/robots/LR-01/commands",
        json={"command": "charge", "expiresAt": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409


def test_manual_jog_is_unauthenticated_and_goes_straight_to_mqtt() -> None:
    from app.store import store

    # 免鉴权直控：不带 token、不带 leaseId 即可下发点动。
    jog = client.post(
        "/api/v1/robots/LR-01/manual-control/jog",
        json={"linear": 0.1, "angular": 0.2, "durationMs": 500},
    )
    assert jog.status_code == 202
    assert jog.json()["command"] == "manual_jog"
    assert jog.json()["payload"] == {"linear": 0.1, "angular": 0.2, "durationMs": 500}
    # 轻量化直控：实时点动不留下命令记录。
    assert jog.json()["commandId"] not in store.commands

    missing = client.post(
        "/api/v1/robots/LR-99/manual-control/jog",
        json={"linear": 0.1, "angular": 0.0, "durationMs": 500},
    )
    assert missing.status_code == 404

    over_limit = client.post(
        "/api/v1/robots/LR-01/manual-control/jog",
        json={"linear": 0.5, "angular": 0.0, "durationMs": 500},
    )
    assert over_limit.status_code == 409
    assert "安全限值" in over_limit.json()["detail"]

    over_duration = client.post(
        "/api/v1/robots/LR-01/manual-control/jog",
        json={"linear": 0.1, "angular": 0.0, "durationMs": 1000},
    )
    assert over_duration.status_code == 409


def test_admin_can_clear_only_the_platform_emergency_stop_lock() -> None:
    from app.store import store

    robot_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original_robot = store.robots[robot_index]
    store.robots[robot_index] = original_robot.model_copy(
        update={"status": "fault", "currentTask": "已请求紧急停止，等待 Gateway 确认"}
    )
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    try:
        response = client.post(
            "/api/v1/robots/LR-01/emergency-stop/reset",
            headers={"Authorization": f"Bearer {login.json()['token']}"},
            json={"acknowledgeSafety": True},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "idle"
        assert "软件紧急停止锁定已解除" in response.json()["currentTask"]
    finally:
        store.robots[robot_index] = original_robot


def test_gateway_navigation_events_update_dispatch_task() -> None:
    from app.schemas import DispatchTask
    from app.store import store

    mark_robot_localized()
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    command = client.post(
        "/api/v1/robots/LR-01/commands",
        json={"command": "navigate", "payload": {"frameId": "map", "x": 1.0, "y": 2.0, "yaw": 0.0}},
        headers=headers,
    )
    assert command.status_code == 202
    command_id = command.json()["commandId"]

    task = DispatchTask(
        id=command_id.replace("CMD-", "TSK-"),
        type="borrow",
        robotId="LR-01",
        target="测试位置",
        status="queued",
        progress=0,
        updatedAt="刚刚",
    )
    store.tasks.insert(0, task)
    store.handle_gateway_event({"event": "navigation_started", "robotId": "LR-01", "commandId": command_id})
    assert store.tasks[0].status == "running"
    assert next(robot for robot in store.robots if robot.id == "LR-01").status == "navigating"

    store.handle_gateway_event({"event": "navigation_succeeded", "robotId": "LR-01", "commandId": command_id})
    assert store.tasks[0].status == "completed"
    assert store.tasks[0].progress == 100


def test_gateway_telemetry_updates_robot_pose_for_the_active_map() -> None:
    from app.store import store

    map_id = store.map_versions[0].id if store.map_versions else "MAP-TEST"
    if not store.map_versions:
        return
    updates = []
    store.set_robot_update_listener(updates.append)
    try:
        store.handle_gateway_event(
            {
                "event": "telemetry",
                "robotId": "LR-01",
                "commandId": None,
                "timestamp": datetime.now(UTC).isoformat(),
                "details": {
                    "mapId": map_id,
                    "frameId": "map",
                    "nav2Ready": True,
                    "chassisReady": True,
                    "localizationStatus": "localized",
                    "localizationScanMode": "dual",
                    "x": 1.25,
                    "y": -0.75,
                    "yaw": 0.5,
                },
            }
        )
        robot = next(item for item in store.robots if item.id == "LR-01")
        assert robot.mapId == map_id
        assert robot.pose is not None
        assert robot.pose.x == 1.25
        assert robot.localizationStatus == "localized"
        assert robot.health.chassis is True
        assert updates[-1].pose == robot.pose
    finally:
        store.set_robot_update_listener(None)


def test_gateway_telemetry_without_pose_marks_gateway_seen_but_keeps_last_position() -> None:
    from app.store import store

    map_id = store.map_versions[0].id if store.map_versions else "MAP-TEST"
    if not store.map_versions:
        return
    before = next(item for item in store.robots if item.id == "LR-01")
    store.handle_gateway_event(
        {
            "event": "telemetry",
            "robotId": "LR-01",
            "commandId": None,
            "timestamp": datetime.now(UTC).isoformat(),
            "details": {
                "mapId": map_id,
                "frameId": "map",
                "nav2Ready": False,
                "chassisReady": False,
                "localizationAvailable": False,
                "localizationStatus": "global_localizing",
                "localizationScanMode": "unavailable",
            },
        }
    )
    robot = next(item for item in store.robots if item.id == "LR-01")
    assert robot.health.navigation is False
    assert robot.health.chassis is False
    assert robot.pose == before.pose
    assert robot.localizationStatus == "global_localizing"
    assert robot.lastSeenAt is not None


def test_legacy_gateway_telemetry_preserves_book_hardware_health() -> None:
    from app.store import store

    map_id = store.map_versions[0].id if store.map_versions else "MAP-TEST"
    if not store.map_versions:
        return
    robot_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original_robot = store.robots[robot_index]
    try:
        store.robots[robot_index] = original_robot.model_copy(
            update={"health": {"navigation": True, "chassis": True, "arm": True, "rod": True, "lift": True, "camera": True}}
        )
        store.handle_gateway_event(
            {
                "event": "telemetry",
                "robotId": "LR-01",
                "timestamp": datetime.now(UTC).isoformat(),
                "details": {
                    "mapId": map_id,
                    "frameId": "map",
                    "nav2Ready": True,
                    "chassisReady": True,
                    "localizationStatus": "localized",
                    "localizationScanMode": "dual",
                    "x": 0.0,
                    "y": 0.0,
                    "yaw": 0.0,
                },
            }
        )
        health = store.robots[robot_index].health
        assert health.arm is True
        assert health.rod is True
        assert health.lift is True
        assert health.camera is True
    finally:
        store.robots[robot_index] = original_robot


def test_gateway_telemetry_updates_rod_health() -> None:
    from app.store import store

    map_id = store.map_versions[0].id if store.map_versions else "MAP-TEST"
    if not store.map_versions:
        return
    robot_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original_robot = store.robots[robot_index]
    try:
        store.robots[robot_index] = original_robot.model_copy(
            update={"health": {"navigation": True, "chassis": True, "arm": False, "rod": False, "lift": True, "camera": True}}
        )
        store.handle_gateway_event(
            {
                "event": "telemetry",
                "robotId": "LR-01",
                "timestamp": datetime.now(UTC).isoformat(),
                "details": {
                    "mapId": map_id,
                    "frameId": "map",
                    "nav2Ready": True,
                    "chassisReady": True,
                    "localizationStatus": "localized",
                    "localizationScanMode": "dual",
                    "rodReady": True,
                    "x": 0.0,
                    "y": 0.0,
                    "yaw": 0.0,
                },
            }
        )
        assert store.robots[robot_index].health.rod is True
    finally:
        store.robots[robot_index] = original_robot


def test_gateway_telemetry_updates_imu_health() -> None:
    from app.store import store

    map_id = store.map_versions[0].id if store.map_versions else "MAP-TEST"
    if not store.map_versions:
        return
    robot_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original_robot = store.robots[robot_index]

    def telemetry_frame(imu_ready: bool | None) -> dict[str, object]:
        details: dict[str, object] = {
            "mapId": map_id,
            "frameId": "map",
            "nav2Ready": True,
            "chassisReady": True,
            "localizationStatus": "localized",
            "localizationScanMode": "dual",
            "x": 0.0,
            "y": 0.0,
            "yaw": 0.0,
        }
        if imu_ready is not None:
            details["imuReady"] = imu_ready
        return {
            "event": "telemetry",
            "robotId": "LR-01",
            "timestamp": datetime.now(UTC).isoformat(),
            "details": details,
        }

    try:
        store.robots[robot_index] = original_robot.model_copy(
            update={"health": original_robot.health.model_copy(update={"imu": True})}
        )
        store.handle_gateway_event(telemetry_frame(True))
        assert store.robots[robot_index].health.imu is True
        store.handle_gateway_event(telemetry_frame(False))
        assert store.robots[robot_index].health.imu is False
        # A legacy Gateway without the field must fail visible, not keep the last value.
        store.handle_gateway_event(telemetry_frame(None))
        assert store.robots[robot_index].health.imu is False
    finally:
        store.robots[robot_index] = original_robot


def test_gateway_telemetry_records_arm_feedback_diagnostics() -> None:
    from app.store import store

    map_id = store.map_versions[0].id if store.map_versions else "MAP-TEST"
    if not store.map_versions:
        return
    store.handle_gateway_event(
        {
            "event": "telemetry",
            "robotId": "LR-01",
            "timestamp": datetime.now(UTC).isoformat(),
            "details": {
                "mapId": map_id,
                "frameId": "map",
                "nav2Ready": True,
                "chassisReady": True,
                "localizationStatus": "localized",
                "localizationScanMode": "dual",
                "armReady": False,
                "armConnected": True,
                "armFeedbackHealthy": False,
                "armCanFps": 0.0,
                "armFeedbackReason": "未收到 CAN 反馈帧",
                "x": 0.0,
                "y": 0.0,
                "yaw": 0.0,
            },
        }
    )
    robot = next(item for item in store.robots if item.id == "LR-01")
    assert robot.health.arm is False
    assert robot.armDiagnostics.connected is True
    assert robot.armDiagnostics.feedbackHealthy is False
    assert robot.armDiagnostics.canFps == 0.0
    assert robot.armDiagnostics.feedbackReason == "未收到 CAN 反馈帧"


def test_lift_driver_acknowledgement_releases_the_robot_command_lock() -> None:
    from app.store import store

    class Cache:
        def __init__(self) -> None:
            self.released: list[tuple[str, str]] = []

        def release_robot_lock(self, robot_id: str, command_id: str) -> None:
            self.released.append((robot_id, command_id))

    original_cache = store.cache
    original_commands = dict(store.commands)
    try:
        command = store.commands.get("CMD-LIFT-TEST")
        if command is None:
            from app.schemas import RobotCommand

            command = RobotCommand(
                commandId="CMD-LIFT-TEST",
                robotId="LR-01",
                command="lift_move",
                payload={"distanceCm": 1.0, "acknowledgeSafety": True},
                expiresAt=datetime.now(UTC) + timedelta(seconds=15),
                idempotencyKey="lift-lock-test",
            )
        cache = Cache()
        store.commands[command.commandId] = command
        store.cache = cache

        store.handle_gateway_event(
            {
                "event": "lift_move_driver_acknowledged",
                "robotId": "LR-01",
                "commandId": command.commandId,
                "details": {"distanceCm": 1.0},
            }
        )

        assert cache.released == [("LR-01", command.commandId)]
    finally:
        store.cache = original_cache
        store.commands = original_commands


def test_navigation_is_rejected_until_robot_has_a_reliable_pose() -> None:
    from app.store import store

    index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    store.robots[index] = store.robots[index].model_copy(
        update={"localizationStatus": "global_localizing", "pose": None}
    )
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    response = client.post(
        "/api/v1/robots/LR-01/commands",
        json={"command": "navigate", "payload": {}},
        headers={"Authorization": f"Bearer {login.json()['token']}"},
    )
    assert response.status_code == 409
    assert "可靠定位" in response.json()["detail"]


def test_web_initial_pose_is_queued_for_the_robot_gateway() -> None:
    from app.store import store

    map_version = store.map_versions[0] if store.map_versions else None
    if not map_version:
        return
    index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    store.robots[index] = store.robots[index].model_copy(
        update={"mapId": map_version.id, "status": "idle", "localizationStatus": "manual_required"}
    )
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    response = client.post(
        "/api/v1/robots/LR-01/localization/initial-pose",
        json={
            "mapId": map_version.id,
            "frameId": "map",
            "x": map_version.origin.x,
            "y": map_version.origin.y,
            "yaw": 0,
        },
        headers={"Authorization": f"Bearer {login.json()['token']}"},
    )
    assert response.status_code == 202
    assert response.json()["command"] == "set_initial_pose"


def test_web_initial_pose_is_allowed_even_when_already_localized() -> None:
    """Operator re-clicking the map means the current pose is not trusted."""
    from app.store import store

    map_version = store.map_versions[0] if store.map_versions else None
    if not map_version:
        return
    index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    store.robots[index] = store.robots[index].model_copy(
        update={"mapId": map_version.id, "status": "idle", "localizationStatus": "localized"}
    )
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    response = client.post(
        "/api/v1/robots/LR-01/localization/initial-pose",
        json={
            "mapId": map_version.id,
            "frameId": "map",
            "x": map_version.origin.x,
            "y": map_version.origin.y,
            "yaw": 0,
        },
        headers={"Authorization": f"Bearer {login.json()['token']}"},
    )
    assert response.status_code == 202
    assert response.json()["command"] == "set_initial_pose"


def test_web_auto_localize_is_queued_for_the_robot_gateway() -> None:
    from app.store import store

    map_version = store.map_versions[0] if store.map_versions else None
    if not map_version:
        return
    index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    store.robots[index] = store.robots[index].model_copy(
        update={"mapId": map_version.id, "status": "idle", "localizationStatus": "manual_required"}
    )
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    response = client.post(
        "/api/v1/robots/LR-01/localization/auto-localize",
        headers={"Authorization": f"Bearer {login.json()['token']}"},
    )
    assert response.status_code == 202
    assert response.json()["command"] == "auto_localize"
    assert response.json()["payload"] == {"mapId": map_version.id}


def test_web_auto_localize_is_rejected_when_robot_is_offline() -> None:
    from app.store import store

    index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    store.robots[index] = store.robots[index].model_copy(update={"status": "offline"})
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    response = client.post(
        "/api/v1/robots/LR-01/localization/auto-localize",
        headers={"Authorization": f"Bearer {login.json()['token']}"},
    )
    assert response.status_code == 409


def test_import_ros_map_and_get_preview(tmp_path, monkeypatch) -> None:
    from app.store import store

    map_storage_dir = tmp_path / "maps"
    map_storage_dir.mkdir()
    monkeypatch.setattr(store, "map_storage_dir", map_storage_dir)
    yaml_data = b"""image: test_map.pgm
mode: trinary
resolution: 0.050
origin: [-1.0, -2.0, 0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
"""
    pgm_data = b"P5\n2 2\n255\n\x00\xfe\xcd\xfe"
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    imported = client.post(
        "/api/v1/library/maps/import",
        headers=headers,
        json={
            "name": "302 实验室",
            "yamlName": "test_map.yaml",
            "yamlContent": b64encode(yaml_data).decode(),
            "pgmName": "test_map.pgm",
            "pgmContent": b64encode(pgm_data).decode(),
        },
    )
    assert imported.status_code == 201
    map_version = imported.json()
    assert map_version["width"] == 2
    assert map_version["height"] == 2
    assert map_version["resolution"] == 0.05
    assert map_version["origin"] == {"x": -1.0, "y": -2.0, "yaw": 0.0}

    preview = client.get(f"/api/v1/library/maps/{map_version['id']}/preview", headers=headers)
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")

    annotation = client.post(
        f"/api/v1/library/maps/{map_version['id']}/annotations",
        headers=headers,
        json={"kind": "charging", "label": "C-01 充电区", "pixelX": 1, "pixelY": 0, "yaw": 1.57},
    )
    assert annotation.status_code == 201
    assert annotation.json()["mapX"] == -0.95
    assert annotation.json()["mapY"] == -1.95
    assert annotation.json()["yaw"] == 1.57

    annotations = client.get(f"/api/v1/library/maps/{map_version['id']}/annotations", headers=headers)
    assert annotations.status_code == 200
    assert any(item["label"] == "C-01 充电区" for item in annotations.json())

    renamed = client.patch(
        f"/api/v1/library/maps/{map_version['id']}/annotations/{annotation.json()['id']}",
        headers=headers,
        json={"label": "C-02 充电区"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["label"] == "C-02 充电区"

    deleted = client.delete(
        f"/api/v1/library/maps/{map_version['id']}/annotations/{annotation.json()['id']}", headers=headers
    )
    assert deleted.status_code == 204
    remaining = client.get(f"/api/v1/library/maps/{map_version['id']}/annotations", headers=headers).json()
    assert all(item["id"] != annotation.json()["id"] for item in remaining)


def test_update_map_annotation_coordinates(tmp_path, monkeypatch) -> None:
    from app.store import store

    map_storage_dir = tmp_path / "maps"
    map_storage_dir.mkdir()
    monkeypatch.setattr(store, "map_storage_dir", map_storage_dir)
    yaml_data = b"""image: coord_map.pgm
mode: trinary
resolution: 0.050
origin: [-1.0, -2.0, 0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
"""
    pgm_data = b"P5\n2 2\n255\n\x00\xfe\xcd\xfe"
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    imported = client.post(
        "/api/v1/library/maps/import",
        headers=headers,
        json={
            "name": "坐标编辑测试图",
            "yamlName": "coord_map.yaml",
            "yamlContent": b64encode(yaml_data).decode(),
            "pgmName": "coord_map.pgm",
            "pgmContent": b64encode(pgm_data).decode(),
        },
    )
    assert imported.status_code == 201
    map_id = imported.json()["id"]

    created = client.post(
        f"/api/v1/library/maps/{map_id}/annotations",
        headers=headers,
        json={"kind": "shelf", "label": "A-01 书架", "pixelX": 0, "pixelY": 0, "yaw": 0.0},
    )
    assert created.status_code == 201
    annotation_id = created.json()["id"]
    assert created.json()["mapX"] == -1.0
    assert created.json()["mapY"] == -1.95

    moved = client.patch(
        f"/api/v1/library/maps/{map_id}/annotations/{annotation_id}",
        headers=headers,
        json={"x": -0.95, "y": -2.0, "yaw": 1.57},
    )
    assert moved.status_code == 200
    moved_body = moved.json()
    assert moved_body["mapX"] == -0.95
    assert moved_body["mapY"] == -2.0
    assert moved_body["pixelX"] == 1
    assert moved_body["pixelY"] == 1
    assert moved_body["yaw"] == 1.57
    assert moved_body["label"] == "A-01 书架"

    renamed = client.patch(
        f"/api/v1/library/maps/{map_id}/annotations/{annotation_id}",
        headers=headers,
        json={"label": "A-02 书架"},
    )
    assert renamed.status_code == 200
    renamed_body = renamed.json()
    assert renamed_body["label"] == "A-02 书架"
    assert renamed_body["mapX"] == -0.95
    assert renamed_body["mapY"] == -2.0
    assert renamed_body["pixelX"] == 1
    assert renamed_body["pixelY"] == 1
    assert renamed_body["yaw"] == 1.57

    for payload in ({"x": -0.9}, {"y": -1.9}, {"x": -1.1, "y": -2.0}, {"yaw": 7.0}):
        rejected = client.patch(
            f"/api/v1/library/maps/{map_id}/annotations/{annotation_id}",
            headers=headers,
            json=payload,
        )
        assert rejected.status_code == 422

    empty = client.patch(
        f"/api/v1/library/maps/{map_id}/annotations/{annotation_id}",
        headers=headers,
        json={},
    )
    assert empty.status_code == 422

    unchanged = client.get(f"/api/v1/library/maps/{map_id}/annotations", headers=headers).json()
    current = next(item for item in unchanged if item["id"] == annotation_id)
    assert current["mapX"] == -0.95
    assert current["mapY"] == -2.0


def _import_test_map(name: str, yaml_name: str, pgm_name: str, headers: dict[str, str]) -> str:
    yaml_data = f"""image: {pgm_name}
mode: trinary
resolution: 0.050
origin: [-1.0, -2.0, 0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
""".encode()
    pgm_data = b"P5\n2 2\n255\n\x00\xfe\xcd\xfe"
    imported = client.post(
        "/api/v1/library/maps/import",
        headers=headers,
        json={
            "name": name,
            "yamlName": yaml_name,
            "yamlContent": b64encode(yaml_data).decode(),
            "pgmName": pgm_name,
            "pgmContent": b64encode(pgm_data).decode(),
        },
    )
    assert imported.status_code == 201
    return imported.json()["id"]


def test_delete_map_removes_annotations_and_files(tmp_path, monkeypatch) -> None:
    from app.store import store

    map_storage_dir = tmp_path / "maps"
    map_storage_dir.mkdir()
    monkeypatch.setattr(store, "map_storage_dir", map_storage_dir)
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    map_id = _import_test_map("待删除地图", "delete_map.yaml", "delete_map.pgm", headers)

    annotation = client.post(
        f"/api/v1/library/maps/{map_id}/annotations",
        headers=headers,
        json={"kind": "shelf", "label": "A-01 书架", "pixelX": 0, "pixelY": 0, "yaw": 0.0},
    )
    assert annotation.status_code == 201
    version_dir = map_storage_dir / map_id
    assert (version_dir / "delete_map.yaml").is_file()
    assert (version_dir / "delete_map.pgm").is_file()

    deleted = client.delete(f"/api/v1/library/maps/{map_id}", headers=headers)
    assert deleted.status_code == 204

    maps = client.get("/api/v1/library/maps", headers=headers).json()
    assert all(item["id"] != map_id for item in maps)
    annotations = client.get(f"/api/v1/library/maps/{map_id}/annotations", headers=headers)
    assert annotations.status_code == 404
    assert not version_dir.exists()


def test_delete_map_loaded_on_a_robot_returns_409(tmp_path, monkeypatch) -> None:
    """The robot's current active map refuses deletion while any robot references it."""

    from app.store import store

    map_storage_dir = tmp_path / "maps"
    map_storage_dir.mkdir()
    monkeypatch.setattr(store, "map_storage_dir", map_storage_dir)
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    map_id = _import_test_map("机器人在用地图", "loaded_map.yaml", "loaded_map.pgm", headers)

    robot_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original_robot = store.robots[robot_index]
    try:
        store.robots[robot_index] = original_robot.model_copy(update={"mapId": map_id})
        deleted = client.delete(f"/api/v1/library/maps/{map_id}", headers=headers)
        assert deleted.status_code == 409
        assert "该地图正在使用" in deleted.json()["detail"]
    finally:
        store.robots[robot_index] = original_robot

    maps = client.get("/api/v1/library/maps", headers=headers).json()
    assert any(item["id"] == map_id for item in maps)

    deleted = client.delete(f"/api/v1/library/maps/{map_id}", headers=headers)
    assert deleted.status_code == 204


def test_delete_map_returns_404_for_an_unknown_map() -> None:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    response = client.delete("/api/v1/library/maps/MAP-000000000000", headers=headers)
    assert response.status_code == 404


def test_gateway_telemetry_for_an_unregistered_map_still_updates_pose() -> None:
    from app.store import store

    robot_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original_robot = store.robots[robot_index]
    try:
        store._telemetry_published.pop("LR-01", None)
        store.handle_gateway_event(
            {
                "event": "telemetry",
                "robotId": "LR-01",
                "timestamp": datetime.now(UTC).isoformat(),
                "details": {
                    "mapId": "MAP-UNREGISTERED",
                    "frameId": "map",
                    "nav2Ready": True,
                    "chassisReady": True,
                    "localizationStatus": "localized",
                    "localizationScanMode": "dual",
                    "x": 3.5,
                    "y": 2.25,
                    "yaw": 1.0,
                },
            }
        )
        robot = store.robots[robot_index]
        assert robot.pose is not None
        assert robot.pose.x == 3.5
        assert robot.mapId == "MAP-UNREGISTERED"
        assert robot.mapVersion == "未注册地图"
        assert robot.location == "未注册地图 (3.50, 2.25) m"
        assert robot.localizationStatus == "localized"
        assert robot.lastSeenAt is not None
    finally:
        store.robots[robot_index] = original_robot
        store._telemetry_published.pop("LR-01", None)


def test_gateway_telemetry_throttles_repeated_poses_until_the_pose_changes(monkeypatch) -> None:
    from app.store import store

    robot_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original_robot = store.robots[robot_index]
    updates = []
    persisted: list[str] = []
    store.set_robot_update_listener(updates.append)
    monkeypatch.setattr(store, "_persist_robot_telemetry", lambda robot: persisted.append(robot.id))

    def send_pose(x: float, y: float, yaw: float) -> str:
        timestamp = datetime.now(UTC).isoformat()
        store.handle_gateway_event(
            {
                "event": "telemetry",
                "robotId": "LR-01",
                "timestamp": timestamp,
                "details": {
                    "mapId": "MAP-THROTTLE",
                    "frameId": "map",
                    "nav2Ready": True,
                    "chassisReady": True,
                    "localizationStatus": "localized",
                    "localizationScanMode": "dual",
                    "x": x,
                    "y": y,
                    "yaw": yaw,
                },
            }
        )
        return timestamp

    try:
        store._telemetry_published.pop("LR-01", None)
        send_pose(1.0, 2.0, 0.5)
        assert len(updates) == 1
        assert persisted == ["LR-01"]

        throttled_ts = send_pose(1.005, 2.005, 0.505)
        assert len(updates) == 1
        assert persisted == ["LR-01"]
        # The skipped frame still refreshes the in-memory view.
        robot = store.robots[robot_index]
        assert robot.pose is not None
        assert robot.pose.x == 1.005
        assert robot.lastSeenAt == datetime.fromisoformat(throttled_ts)

        send_pose(1.05, 2.0, 0.5)
        assert len(updates) == 2
        assert persisted == ["LR-01", "LR-01"]
    finally:
        store.set_robot_update_listener(None)
        store.robots[robot_index] = original_robot
        store._telemetry_published.pop("LR-01", None)


def test_gateway_telemetry_throttles_poseless_frames_but_not_state_changes(monkeypatch) -> None:
    from app.store import store

    robot_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original_robot = store.robots[robot_index]
    updates = []
    persisted: list[str] = []
    store.set_robot_update_listener(updates.append)
    monkeypatch.setattr(store, "_persist_robot_telemetry", lambda robot: persisted.append(robot.id))

    def send_poseless(chassis_ready: bool) -> str:
        timestamp = datetime.now(UTC).isoformat()
        store.handle_gateway_event(
            {
                "event": "telemetry",
                "robotId": "LR-01",
                "timestamp": timestamp,
                "details": {
                    "mapId": "MAP-THROTTLE",
                    "frameId": "map",
                    "nav2Ready": True,
                    "chassisReady": chassis_ready,
                    "localizationStatus": "global_localizing",
                    "localizationScanMode": "unavailable",
                },
            }
        )
        return timestamp

    try:
        store._telemetry_published.pop("LR-01", None)
        send_poseless(chassis_ready=True)
        assert len(updates) == 1
        assert persisted == ["LR-01"]

        throttled_ts = send_poseless(chassis_ready=True)
        assert len(updates) == 1
        assert persisted == ["LR-01"]
        assert store.robots[robot_index].lastSeenAt == datetime.fromisoformat(throttled_ts)

        send_poseless(chassis_ready=False)
        assert len(updates) == 2
        assert persisted == ["LR-01", "LR-01"]
    finally:
        store.set_robot_update_listener(None)
        store.robots[robot_index] = original_robot
        store._telemetry_published.pop("LR-01", None)


def test_switch_map_rejects_unknown_robot_or_map() -> None:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    missing_robot = client.post("/api/v1/robots/LR-99/map/switch", json={"mapId": "MAP-UNKNOWN"}, headers=headers)
    assert missing_robot.status_code == 404
    assert missing_robot.json()["detail"] == "机器人不存在"

    missing_map = client.post("/api/v1/robots/LR-01/map/switch", json={"mapId": "MAP-UNKNOWN"}, headers=headers)
    assert missing_map.status_code == 404
    assert missing_map.json()["detail"] == "地图不存在"


def test_switch_map_rejects_unavailable_robot_and_same_map_but_cancels_active_task() -> None:
    from app.schemas import DispatchTask, MapOrigin, MapVersion
    from app.store import store

    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    map_version = MapVersion(
        id="MAP-SWITCH-TEST",
        name="换图测试地图",
        version="vtest",
        yamlFile="switch_map.yaml",
        pgmFile="switch_map.pgm",
        width=3,
        height=3,
        resolution=0.05,
        origin=MapOrigin(x=0, y=0, yaw=0),
        mode="trinary",
        negate=False,
        occupiedThreshold=0.65,
        freeThreshold=0.196,
        status="published",
        createdAt=datetime.now(UTC),
        createdBy="admin",
    )
    active_task = DispatchTask(
        id="TSK-SWITCH-ACTIVE",
        type="inventory",
        robotId="LR-01",
        target="一层 / B 区 / B-02",
        status="running",
        progress=10,
        updatedAt="刚刚",
    )
    robot_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original_robot = store.robots[robot_index]
    original_tasks = list(store.tasks)
    created = client.post(
        "/api/v1/robots",
        json={"id": "LR-03", "name": "智巡三号", "gatewayHost": "192.168.10.103"},
        headers=headers,
    )
    assert created.status_code == 201
    fault_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-03")
    store.robots[fault_index] = store.robots[fault_index].model_copy(update={"status": "fault"})
    store.map_versions.insert(0, map_version)
    from app.api import robots as robots_api

    original_timeout = robots_api.MAP_SWITCH_RESULT_TIMEOUT_SECONDS
    robots_api.MAP_SWITCH_RESULT_TIMEOUT_SECONDS = 5.0
    try:
        fault = client.post("/api/v1/robots/LR-03/map/switch", json={"mapId": map_version.id}, headers=headers)
        assert fault.status_code == 409

        store.robots[robot_index] = original_robot.model_copy(update={"status": "manual"})
        manual = client.post("/api/v1/robots/LR-01/map/switch", json={"mapId": map_version.id}, headers=headers)
        assert manual.status_code == 409

        store.robots[robot_index] = original_robot.model_copy(update={"status": "idle", "mapId": map_version.id})
        # Already on the target map: idempotent success without dispatching.
        same_map = client.post("/api/v1/robots/LR-01/map/switch", json={"mapId": map_version.id}, headers=headers)
        assert same_map.status_code == 200
        assert "已在该地图上" in same_map.json()["message"]

        store.robots[robot_index] = original_robot.model_copy(update={"status": "idle", "mapId": None})
        store.tasks = [active_task, *store.tasks]
        _fire_switch_event("map_switched", {"mapId": map_version.id})
        switched = client.post("/api/v1/robots/LR-01/map/switch", json={"mapId": map_version.id}, headers=headers)
        assert switched.status_code == 200
        cancelled = next(task for task in store.tasks if task.id == active_task.id)
        assert cancelled.status == "cancelled"
    finally:
        robots_api.MAP_SWITCH_RESULT_TIMEOUT_SECONDS = original_timeout
        store.map_versions.remove(map_version)
        store.robots = [item for item in store.robots if item.id != "LR-03"]
        store.robots[robot_index] = original_robot
        store.tasks = original_tasks


def test_switch_map_returns_the_vehicle_result_and_updates_robot() -> None:
    from app.schemas import MapOrigin, MapVersion
    from app.store import store

    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    map_version = MapVersion(
        id="MAP-SWITCH-TEST",
        name="换图测试地图",
        version="vtest",
        yamlFile="switch_map.yaml",
        pgmFile="switch_map.pgm",
        width=3,
        height=3,
        resolution=0.05,
        origin=MapOrigin(x=0, y=0, yaw=0),
        mode="trinary",
        negate=False,
        occupiedThreshold=0.65,
        freeThreshold=0.196,
        status="published",
        createdAt=datetime.now(UTC),
        createdBy="admin",
    )
    robot_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original_robot = store.robots[robot_index]
    original_tasks = list(store.tasks)
    store.map_versions.insert(0, map_version)
    from app.api import robots as robots_api

    original_timeout = robots_api.MAP_SWITCH_RESULT_TIMEOUT_SECONDS
    robots_api.MAP_SWITCH_RESULT_TIMEOUT_SECONDS = 5.0
    try:
        store.robots[robot_index] = original_robot.model_copy(
            update={
                "status": "idle",
                "mapId": None,
                "pose": RobotPose(frameId="map", x=1.0, y=1.0, yaw=0.0, capturedAt=datetime.now(UTC)),
            }
        )
        store.tasks = [task for task in store.tasks if task.robotId != "LR-01"]
        _fire_switch_event("map_switched", {"mapId": map_version.id})
        response = client.post("/api/v1/robots/LR-01/map/switch", json={"mapId": map_version.id}, headers=headers)
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["mapId"] == map_version.id
        command = store.commands[result["commandId"]]
        assert command.command == "switch_map"
        assert command.payload == {"mapId": map_version.id}
        assert timedelta(seconds=50) < command.expiresAt - datetime.now(UTC) <= timedelta(seconds=60)

        robot = store.robots[robot_index]
        assert robot.mapId == map_version.id
        assert robot.mapVersion == map_version.name
        assert robot.localizationStatus == "global_localizing"
        assert robot.pose is None
        assert robot.location == f"{map_version.name} / 正在重新定位"
    finally:
        robots_api.MAP_SWITCH_RESULT_TIMEOUT_SECONDS = original_timeout
        store.map_versions.remove(map_version)
        store.robots[robot_index] = original_robot
        store.tasks = original_tasks


def test_switch_map_command_rejected_keeps_the_current_map() -> None:
    from app.schemas import MapOrigin, MapVersion
    from app.store import store

    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    map_version = MapVersion(
        id="MAP-SWITCH-TEST",
        name="换图测试地图",
        version="vtest",
        yamlFile="switch_map.yaml",
        pgmFile="switch_map.pgm",
        width=3,
        height=3,
        resolution=0.05,
        origin=MapOrigin(x=0, y=0, yaw=0),
        mode="trinary",
        negate=False,
        occupiedThreshold=0.65,
        freeThreshold=0.196,
        status="published",
        createdAt=datetime.now(UTC),
        createdBy="admin",
    )
    robot_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original_robot = store.robots[robot_index]
    original_tasks = list(store.tasks)
    store.map_versions.insert(0, map_version)
    from app.api import robots as robots_api

    original_timeout = robots_api.MAP_SWITCH_RESULT_TIMEOUT_SECONDS
    robots_api.MAP_SWITCH_RESULT_TIMEOUT_SECONDS = 5.0
    try:
        store.robots[robot_index] = original_robot.model_copy(update={"status": "idle", "mapId": None})
        store.tasks = [task for task in store.tasks if task.robotId != "LR-01"]
        _fire_switch_event("command_rejected", {"reason": "车端正在执行任务"})
        response = client.post("/api/v1/robots/LR-01/map/switch", json={"mapId": map_version.id}, headers=headers)
        assert response.status_code == 409
        assert "车端正在执行任务" in response.json()["detail"]

        robot = store.robots[robot_index]
        assert robot.mapId is None
        assert robot.currentTask == "地图切换被拒绝"
    finally:
        robots_api.MAP_SWITCH_RESULT_TIMEOUT_SECONDS = original_timeout
        store.map_versions.remove(map_version)
        store.robots[robot_index] = original_robot
        store.tasks = original_tasks


def test_bookarm_health_poller_owns_arm_health_over_gateway_telemetry() -> None:
    from app.store import store

    robot_index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original_robot = store.robots[robot_index]
    map_id = store.map_versions[0].id if store.map_versions else "MAP-TEST"
    if not store.map_versions:
        return

    status = {
        "arm": {
            "wired": True,
            "health": {"healthy_feedback": True, "can_fps": 6.2, "reason": None},
        },
        "rod": {"connected": True},
        "lift": {"connected": True},
        "camera": {"present": True},
    }
    try:
        store._bookarm_health_active = True
        store._apply_bookarm_health(status)
        robot = store.robots[robot_index]
        assert robot.health.arm is True
        assert robot.health.rod is True
        assert robot.health.lift is True
        assert robot.health.camera is True
        assert robot.armDiagnostics.connected is True
        assert robot.armDiagnostics.feedbackHealthy is True
        assert robot.armDiagnostics.canFps == 6.2

        # Gateway telemetry still carrying the broken legacy reader must not
        # clobber the poller-owned keys.
        store.handle_gateway_event(
            {
                "event": "telemetry",
                "robotId": "LR-01",
                "timestamp": datetime.now(UTC).isoformat(),
                "details": {
                    "mapId": map_id,
                    "frameId": "map",
                    "nav2Ready": True,
                    "chassisReady": True,
                    "localizationStatus": "localized",
                    "localizationScanMode": "dual",
                    "x": 0.0,
                    "y": 0.0,
                    "yaw": 0.0,
                    "armReady": False,
                    "rodReady": False,
                    "liftReady": False,
                    "cameraReady": False,
                    "armConnected": False,
                },
            }
        )
        robot = store.robots[robot_index]
        assert robot.health.arm is True
        assert robot.health.rod is True
        assert robot.armDiagnostics.connected is True

        store._apply_bookarm_health(None)
        robot = store.robots[robot_index]
        assert robot.health.arm is False
        assert robot.armDiagnostics.feedbackReason == "bookarm 服务不可达"
    finally:
        store._bookarm_health_active = False
        store.robots[robot_index] = original_robot
