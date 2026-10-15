"""Arm-busy interlock tests: E-stop -> bookarm safety stop + flow cancellation,
and the 'busy' robot status that locks out new work during arm operation."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import socket
import tempfile
import threading
import time

from fastapi.testclient import TestClient

from app.bookarm_client import BookarmClient
from app.main import app
from app.schemas import RobotPose

client = TestClient(app)


def _store():
    from app.store import store

    return store


def _headers() -> dict[str, str]:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _set_robot(status: str, **extra: object) -> None:
    store = _store()
    index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    store.robots[index] = store.robots[index].model_copy(update={"status": status, **extra})


def _prepare_robot() -> None:
    _set_robot(
        "idle",
        localizationStatus="localized",
        pose=RobotPose(frameId="map", x=0.0, y=0.0, yaw=0.0, capturedAt=datetime.now(UTC)),
    )


def _wait_robot_status(status: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        robot = next(item for item in _store().robots if item.id == "LR-01")
        if robot.status == status:
            return
        time.sleep(0.01)
    raise AssertionError(f"robot did not reach status {status} in time")


def _wait_task_terminal(task_id: str, timeout_s: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        task = next((item for item in _store().tasks if item.id == task_id), None)
        if task and task.status in {"completed", "failed", "cancelled"}:
            return task.model_dump()
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach a terminal state in time")


class FakeArmClient:
    """Bookarm double: the arm task stays running until cancelled."""

    def __init__(self, final_state: str = "running", safety_error: Exception | None = None) -> None:
        self.final_state = final_state
        self.safety_error = safety_error
        self.safety_calls: list[bool] = []
        self.cancelled: list[str] = []

    def create_takeout_task(self, **kwargs):  # noqa: ANN003, ANN201 - test double
        return {"id": "BT-EMG-1", "state": "running"}

    def get_takeout_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        return {"id": task_id, "state": self.final_state}

    def cancel_takeout_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.cancelled.append(task_id)
        return {"id": task_id, "state": "cancelled"}

    def get_vision_debug_image(self, kind):  # noqa: ANN001, ANN202 - test double
        return f"fake-{kind}".encode()

    def get_hardware_status(self):  # noqa: ANN202 - test double
        return {}

    def safety_stop(self, emergency: bool = False):  # noqa: ANN202 - test double
        self.safety_calls.append(emergency)
        if self.safety_error is not None:
            raise self.safety_error
        return {"accepted": True, "armStopped": True, "liftStopped": True, "taskInterrupted": True}


def _cleanup(store, original_robot) -> None:  # noqa: ANN001 - shared singleton cleanup
    store.bookarm_client_factory = None
    store.takeout_poll_interval_s = 2.0
    with store._lock:
        store.tasks = [task for task in store.tasks if task.type not in {"takeout", "return", "multi_round"}]
    index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    store.robots[index] = original_robot
    deadline = time.monotonic() + 2.0
    while (store._takeout_cancels or store._return_cancels or store._multi_round_cancels) and time.monotonic() < deadline:
        time.sleep(0.01)


def test_emergency_stop_calls_bookarm_and_cancels_live_arm_flow() -> None:
    """E-stop during the arm phase: bookarm safety stop fires, the live flow is
    cancelled through its cancel event, the robot locks to fault, and the busy
    release must not wipe the fault lock."""
    store = _store()
    original = next(item for item in store.robots if item.id == "LR-01")
    fake = FakeArmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="emg-vision-"))
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout",
            headers=headers,
            json={
                "robotId": "LR-01",
                "templateId": "LIB-TPL-1",
                "liftHeightMm": 800,
                "destinationSlot": 1,
                "sendNavigation": False,
            },
        )
        assert created.status_code == 201
        task_id = created.json()["id"]

        # The orchestration marks the robot busy for the arm phase.
        _wait_robot_status("busy")
        robot = next(item for item in store.robots if item.id == "LR-01")
        assert robot.currentTask == "机械臂作业中"

        response = client.post(
            "/api/v1/robots/LR-01/commands",
            headers=headers,
            json={"command": "emergency_stop"},
        )
        assert response.status_code == 202
        assert response.json()["detail"] == "底盘已停止，机械臂已停止"
        assert fake.safety_calls == [True]

        # The live flow is cancelled through its cancel event, not forgotten.
        final = _wait_task_terminal(task_id)
        assert final["status"] == "cancelled"
        assert fake.cancelled == ["BT-EMG-1"]

        # Fault lock wins over the arm-phase busy release.
        robot = next(item for item in store.robots if item.id == "LR-01")
        assert robot.status == "fault"
        assert robot.currentTask == "已请求紧急停止，等待 Gateway 确认"
    finally:
        _cleanup(store, original)


def test_emergency_stop_reports_arm_failure_but_still_stops_chassis() -> None:
    """Without a reachable bookarm the chassis E-stop still succeeds; the
    response detail distinguishes the arm failure."""
    store = _store()
    original = next(item for item in store.robots if item.id == "LR-01")
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = None  # real client: socket absent in tests
    try:
        response = client.post(
            "/api/v1/robots/LR-01/commands",
            headers=headers,
            json={"command": "emergency_stop"},
        )
        assert response.status_code == 202
        detail = response.json()["detail"]
        assert "底盘已停止" in detail
        assert "机械臂停止失败" in detail
        robot = next(item for item in store.robots if item.id == "LR-01")
        assert robot.status == "fault"
    finally:
        _cleanup(store, original)


def test_busy_robot_rejects_new_work_with_409() -> None:
    """Every work-creation path and the jogs answer 409 while the arm is busy;
    the E-stop itself stays allowed."""
    store = _store()
    original = next(item for item in store.robots if item.id == "LR-01")
    _prepare_robot()
    headers = _headers()
    _set_robot("busy", currentTask="机械臂作业中")
    try:
        takeout = client.post(
            "/api/v1/circulation/complete-takeout",
            headers=headers,
            json={
                "robotId": "LR-01",
                "templateId": "LIB-TPL-1",
                "liftHeightMm": 800,
                "destinationSlot": 1,
                "sendNavigation": False,
            },
        )
        assert takeout.status_code == 409
        assert "机械臂作业中" in takeout.json()["detail"]

        returning = client.post(
            "/api/v1/circulation/complete-return",
            headers=headers,
            json={
                "robotId": "LR-01",
                "templateId": "LIB-TPL-1",
                "liftHeightMm": 800,
                "sourceSlot": 1,
                "sendNavigation": False,
            },
        )
        assert returning.status_code == 409
        assert "机械臂作业中" in returning.json()["detail"]

        multi = client.post(
            "/api/v1/circulation/multi-round",
            headers=headers,
            json={
                "robotId": "LR-01",
                "takeBookIds": ["BOOK-X"],
                "returnBookIds": [],
                "sendNavigation": False,
            },
        )
        assert multi.status_code == 409
        assert "机械臂作业中" in multi.json()["detail"]

        annotation_nav = client.post(
            "/api/v1/robots/LR-01/annotations/navigate",
            headers=headers,
            json={"mapId": "MAP-X", "annotationId": "ANN-X"},
        )
        assert annotation_nav.status_code == 409
        assert "机械臂作业中" in annotation_nav.json()["detail"]

        jog = client.post(
            "/api/v1/robots/LR-01/manual-control/jog",
            json={"linear": 0.1, "angular": 0.0, "durationMs": 500},
        )
        assert jog.status_code == 409
        assert "机械臂作业中" in jog.json()["detail"]

        lift = client.post(
            "/api/v1/robots/LR-01/lift/move",
            headers=headers,
            json={"distanceCm": 5, "acknowledgeSafety": True},
        )
        assert lift.status_code == 409
        assert "机械臂作业中" in lift.json()["detail"]

        navigate = client.post(
            "/api/v1/robots/LR-01/commands",
            headers=headers,
            json={"command": "navigate", "payload": {"frameId": "map", "x": 1.0, "y": 2.0, "yaw": 0.0}},
        )
        assert navigate.status_code == 409
        assert "机械臂作业中" in navigate.json()["detail"]

        # The E-stop itself must never be blocked by the busy state.
        emergency = client.post(
            "/api/v1/robots/LR-01/commands",
            headers=headers,
            json={"command": "emergency_stop"},
        )
        assert emergency.status_code == 202
    finally:
        _cleanup(store, original)


def test_busy_release_restores_idle_after_arm_phase() -> None:
    """A completed arm phase hands the robot back to idle for the next job."""
    store = _store()
    original = next(item for item in store.robots if item.id == "LR-01")
    fake = FakeArmClient(final_state="succeeded")
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="busy-vision-"))
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout",
            headers=headers,
            json={
                "robotId": "LR-01",
                "templateId": "LIB-TPL-1",
                "liftHeightMm": 800,
                "destinationSlot": 1,
                "sendNavigation": False,
            },
        )
        assert created.status_code == 201
        final = _wait_task_terminal(created.json()["id"])
        assert final["status"] == "completed"
        _wait_robot_status("idle")
        robot = next(item for item in store.robots if item.id == "LR-01")
        assert robot.currentTask == "待命"
    finally:
        _cleanup(store, original)


def test_gateway_events_do_not_clear_arm_busy() -> None:
    """A navigation-succeeded event and telemetry frames refresh their own
    fields but must not override the orchestration-owned busy status."""
    store = _store()
    original = next(item for item in store.robots if item.id == "LR-01")
    _prepare_robot()
    headers = _headers()
    try:
        command = client.post(
            "/api/v1/robots/LR-01/commands",
            headers=headers,
            json={"command": "navigate", "payload": {"frameId": "map", "x": 1.0, "y": 2.0, "yaw": 0.0}},
        )
        assert command.status_code == 202
        command_id = command.json()["commandId"]
        from app.schemas import DispatchTask

        task = DispatchTask(
            id=command_id.replace("CMD-", "TSK-"),
            type="navigate",
            robotId="LR-01",
            target="测试位置",
            status="queued",
            progress=0,
            updatedAt="刚刚",
        )
        with store._lock:
            store.tasks.insert(0, task)
        try:
            _set_robot("busy", currentTask="机械臂作业中")

            store.handle_gateway_event(
                {"event": "navigation_succeeded", "robotId": "LR-01", "commandId": command_id}
            )
            stage = next(item for item in store.tasks if item.id == task.id)
            assert stage.status == "completed"
            robot = next(item for item in store.robots if item.id == "LR-01")
            assert robot.status == "busy"
            assert robot.currentTask == "机械臂作业中"

            map_id = store.map_versions[0].id if store.map_versions else "MAP-TEST"
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
                        "x": 3.0,
                        "y": 4.0,
                        "yaw": 0.5,
                    },
                }
            )
            robot = next(item for item in store.robots if item.id == "LR-01")
            assert robot.status == "busy"
            assert robot.pose is not None and robot.pose.x == 3.0
        finally:
            with store._lock:
                store.tasks = [item for item in store.tasks if item.id != task.id]
    finally:
        _cleanup(store, original)


def _serve_one_request(server: socket.socket, captured: list[dict[str, object]]) -> None:
    """Answer a single unix-socket HTTP request with the safety-stop payload."""
    try:
        connection, _ = server.accept()
        with connection:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
            head = data.split(b"\r\n\r\n", 1)[0].decode("utf-8", errors="replace")
            lines = head.split("\r\n")
            captured.append({"request_line": lines[0], "headers": lines[1:]})
            body = json.dumps(
                {"accepted": True, "armStopped": True, "liftStopped": True, "taskInterrupted": True}
            ).encode("utf-8")
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode("utf-8")
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
    finally:
        server.close()


def _listening_server(socket_path: str) -> socket.socket:
    """Bind+listen in the caller's thread so the client can never race the bind."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(1)
    return server


def test_bookarm_client_safety_stop_matches_the_contract() -> None:
    """safety_stop posts to the contract endpoints and sends Bearer only when
    a token is configured."""
    with tempfile.TemporaryDirectory(prefix="bookarm-sock-") as directory:
        captured: list[dict[str, object]] = []
        emergency_server = _listening_server(str(Path(directory) / "emergency.sock"))
        server = threading.Thread(
            target=_serve_one_request, args=(emergency_server, captured), daemon=True
        )
        server.start()
        result = BookarmClient(
            str(Path(directory) / "emergency.sock"), token="secret-token", timeout_s=5.0
        ).safety_stop(emergency=True)
        server.join(timeout=5.0)
        assert result["accepted"] is True
        assert result["armStopped"] is True
        assert captured[0]["request_line"] == "POST /api/v1/safety/emergency-stop HTTP/1.1"
        assert "Authorization: Bearer secret-token" in captured[0]["headers"]

        captured.clear()
        stop_server = _listening_server(str(Path(directory) / "stop.sock"))
        server = threading.Thread(target=_serve_one_request, args=(stop_server, captured), daemon=True)
        server.start()
        result = BookarmClient(str(Path(directory) / "stop.sock"), timeout_s=5.0).safety_stop(
            emergency=False
        )
        server.join(timeout=5.0)
        assert result["taskInterrupted"] is True
        assert captured[0]["request_line"] == "POST /api/v1/safety/stop HTTP/1.1"
        assert not any(
            str(header).startswith("Authorization:") for header in captured[0]["headers"]
        )
