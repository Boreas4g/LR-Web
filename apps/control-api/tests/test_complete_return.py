"""Complete-return orchestration tests: navigate, then on-robot return.

Covers both returnMode branches: mid (default, mid-return via
/midreturn/tasks) and end (end-of-row return via /return/tasks)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import time

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import RobotPose

client = TestClient(app)


def _mark_robot_localized(robot_id: str = "LR-01") -> None:
    """Prepare the in-memory robot for tests that dispatch navigation."""
    from datetime import UTC, datetime

    from app.store import store

    index = next(index for index, item in enumerate(store.robots) if item.id == robot_id)
    store.robots[index] = store.robots[index].model_copy(
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


class FakeBookarmClient:
    """Test double for the unix-socket bookarm client."""

    def __init__(self, final_state: str = "succeeded", error: str | None = None) -> None:
        self.final_state = final_state
        self.error = error
        self.created: list[dict[str, object]] = []
        self.cancelled: list[str] = []
        self.created_return: list[dict[str, object]] = []
        self.cancelled_return: list[str] = []

    def create_midreturn_task(self, **kwargs):  # noqa: ANN003, ANN201 - test double
        self.created.append(kwargs)
        return {"id": "BT-MID-1", "state": "running"}

    def get_midreturn_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        return {"id": task_id, "state": self.final_state, "error": self.error}

    def cancel_midreturn_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.cancelled.append(task_id)
        return {"id": task_id, "state": "cancelled"}

    def create_return_task(self, **kwargs):  # noqa: ANN003, ANN201 - test double
        self.created_return.append(kwargs)
        return {"id": "BT-RET-1", "state": "running"}

    def get_return_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        return {"id": task_id, "state": self.final_state, "error": self.error}

    def cancel_return_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.cancelled_return.append(task_id)
        return {"id": task_id, "state": "cancelled"}

    def get_vision_debug_image(self, kind):  # noqa: ANN001, ANN202 - test double
        return f"fake-return-{kind}".encode()


def _headers() -> dict[str, str]:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _prepare_robot() -> None:
    from app.store import store

    _mark_robot_localized("LR-01")
    index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    store.robots[index] = store.robots[index].model_copy(update={"status": "idle"})


def _payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "robotId": "LR-01",
        "templateId": "LIB-TPL-1",
        "target": {"x": 1.5, "y": -2.0, "yaw": 1.57},
        "liftHeightMm": 800,
        "sourceSlot": 2,
    }
    payload.update(overrides)
    return payload


def _store():
    from app.store import store

    return store


def _return_tasks() -> list[dict[str, object]]:
    return [task.model_dump() for task in _store().tasks if task.type == "return"]


def _wait_task_status(task_id: str, statuses: set[str], timeout_s: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        task = next((item for item in _return_tasks() if item["id"] == task_id), None)
        if task and task["status"] in statuses:
            return task
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach {statuses} in time")


def _navigation_command_id() -> str:
    nav = next(
        item
        for item in _store().tasks
        if item.type == "navigate" and item.target.startswith("还书导航")
    )
    return nav.id.replace("TSK-", "CMD-", 1)


def _finish_navigation(robot_id: str = "LR-01", event: str = "navigation_succeeded") -> None:
    command_id = _navigation_command_id()
    _store().handle_gateway_event(
        {"event": event, "robotId": robot_id, "commandId": command_id}
    )


def _wait_fine_tune_command_id(timeout_s: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        fine_tune = next((item for item in _store().tasks if item.type == "fine_tune"), None)
        if fine_tune is not None:
            return fine_tune.id.replace("TSK-", "CMD-", 1)
        time.sleep(0.01)
    raise AssertionError("fine_tune command was not dispatched in time")


def _finish_fine_tune(
    robot_id: str = "LR-01",
    event: str = "command_completed",
    details: dict[str, object] | None = None,
) -> None:
    command_id = _wait_fine_tune_command_id()
    payload: dict[str, object] = {"event": event, "robotId": robot_id, "commandId": command_id}
    if details:
        payload["details"] = details
    _store().handle_gateway_event(payload)


def test_complete_return_happy_path_returns_from_the_source_slot() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="flow-vision-"))
    try:
        created = client.post(
            "/api/v1/circulation/complete-return", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task = created.json()
        assert task["type"] == "return"
        assert task["workflowDetails"]["sourceSlot"] == 2

        # The navigation command carries the raw map-frame target.
        nav_command_id = _navigation_command_id()
        command = store.commands[nav_command_id]
        assert command.command == "navigate"
        assert command.payload["x"] == 1.5
        assert command.payload["yaw"] == 1.57
        assert command.payload["returnTaskId"] == task["id"]

        time.sleep(0.05)
        _finish_navigation()
        _finish_fine_tune()
        final = _wait_task_status(task["id"], {"completed", "failed"})
        assert final["status"] == "completed"
        assert final["progress"] == 100

        assert len(fake.created) == 1
        call = fake.created[0]
        assert call["template_id"] == "LIB-TPL-1"
        assert call["target_height_mm"] == 800
        assert call["source_slot"] == 2
        assert call["injected_grasp_pose"] is None
        # Default returnMode is mid: the end-of-row return workflow is unused.
        assert fake.created_return == []
        assert final["workflowDetails"]["returnMode"] == "mid"

        # The D435 spine-recognition snapshot is exposed and fetchable.
        images = final["workflowDetails"]["visionImages"]
        assert images["spine"].split("?")[0].endswith(
            f"/api/v1/circulation/complete-return/{task['id']}/vision/return-annotated.jpg"
        )
        assert "?v=" in images["spine"]
        image = client.get(images["spine"], headers=headers)
        assert image.status_code == 200
        assert image.content == b"fake-return-annotated"
        unknown = client.get(
            "/api/v1/circulation/complete-return/TSK-NOPE/vision/return-annotated.jpg",
            headers=headers,
        )
        assert unknown.status_code == 404
    finally:
        _cleanup(store)


def test_complete_return_fails_when_navigation_fails() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        created = client.post(
            "/api/v1/circulation/complete-return", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]

        time.sleep(0.05)
        _finish_navigation(event="navigation_failed")
        final = _wait_task_status(task_id, {"failed", "completed"})
        assert final["status"] == "failed"
        assert "导航" in (final["failureReason"] or "")
        assert fake.created == []
    finally:
        _cleanup(store)


def test_complete_return_fails_when_bookarm_task_fails() -> None:
    store = _store()
    fake = FakeBookarmClient(final_state="failed", error="ik 失败")
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        created = client.post(
            "/api/v1/circulation/complete-return", headers=headers, json=_payload()
        )
        task_id = created.json()["id"]
        time.sleep(0.05)
        _finish_navigation()
        _finish_fine_tune()
        final = _wait_task_status(task_id, {"failed", "completed"})
        assert final["status"] == "failed"
        assert "ik" in (final["failureReason"] or "")
    finally:
        _cleanup(store)


def test_complete_return_rejects_an_out_of_range_source_slot() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        missing = client.post(
            "/api/v1/circulation/complete-return",
            headers=headers,
            json=_payload(sourceSlot=9),
        )
        assert missing.status_code == 422
    finally:
        _cleanup(store)


def test_complete_return_supersedes_a_second_concurrent_task_and_cancels() -> None:
    store = _store()
    fake = FakeBookarmClient(final_state="running")
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        first = client.post(
            "/api/v1/circulation/complete-return", headers=headers, json=_payload()
        )
        assert first.status_code == 201
        first_id = first.json()["id"]
        second = client.post(
            "/api/v1/circulation/complete-return",
            headers=headers,
            json=_payload(sourceSlot=3),
        )
        assert second.status_code == 201

        final = _wait_task_status(first_id, {"cancelled", "failed", "completed"})
        assert final["status"] == "cancelled"

        cancelled = client.post(
            f"/api/v1/circulation/complete-return/{second.json()['id']}/cancel", headers=headers
        )
        assert cancelled.status_code == 202
        second_final = _wait_task_status(second.json()["id"], {"cancelled", "failed", "completed"})
        assert second_final["status"] == "cancelled"
    finally:
        _cleanup(store)


def _cleanup(store) -> None:  # noqa: ANN001 - shared singleton cleanup
    store.bookarm_client_factory = None
    store.takeout_poll_interval_s = 2.0
    with store._lock:
        store.tasks = [task for task in store.tasks if task.type not in {"return", "fine_tune"}]
        store.tasks = [
            task
            for task in store.tasks
            if not (task.type == "navigate" and task.target.startswith("还书导航"))
        ]
    deadline = time.monotonic() + 2.0
    while store._return_cancels and time.monotonic() < deadline:
        time.sleep(0.01)


def test_complete_return_without_navigation_starts_the_arm_in_place() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        payload = _payload(sendNavigation=False)
        del payload["target"]
        created = client.post(
            "/api/v1/circulation/complete-return", headers=headers, json=payload
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        final = _wait_task_status(task_id, {"completed", "failed"})
        assert final["status"] == "completed", final.get("failureReason")
        # No navigation or fine-tune stage tasks were dispatched.
        assert not [
            item
            for item in store.tasks
            if item.type in {"navigate", "fine_tune"} and item.target.startswith("还书")
        ]
        assert len(fake.created) == 1
    finally:
        _cleanup(store)


def test_complete_return_requires_a_target_when_navigating() -> None:
    _prepare_robot()
    headers = _headers()
    payload = _payload()
    del payload["target"]
    created = client.post(
        "/api/v1/circulation/complete-return", headers=headers, json=payload
    )
    assert created.status_code == 422


def test_complete_return_end_mode_uses_the_return_workflow() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        payload = _payload(sendNavigation=False, returnMode="end")
        del payload["target"]
        created = client.post(
            "/api/v1/circulation/complete-return", headers=headers, json=payload
        )
        assert created.status_code == 201
        task = created.json()
        assert task["workflowDetails"]["returnMode"] == "end"
        final = _wait_task_status(task["id"], {"completed", "failed"})
        assert final["status"] == "completed", final.get("failureReason")

        # The mid-return workflow is never touched; the end-of-row return
        # task is created without an injected grasp pose.
        assert fake.created == []
        assert len(fake.created_return) == 1
        call = fake.created_return[0]
        assert call["template_id"] == "LIB-TPL-1"
        assert call["target_height_mm"] == 800
        assert call["source_slot"] == 2
        assert "injected_grasp_pose" not in call
        assert final["workflowDetails"]["returnMode"] == "end"
    finally:
        _cleanup(store)


def test_complete_return_end_mode_cancel_uses_the_return_workflow() -> None:
    store = _store()
    fake = FakeBookarmClient(final_state="running")
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        payload = _payload(sendNavigation=False, returnMode="end")
        del payload["target"]
        created = client.post(
            "/api/v1/circulation/complete-return", headers=headers, json=payload
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        deadline = time.monotonic() + 5.0
        while not fake.created_return and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fake.created_return, "bookarm return task was not created in time"

        cancelled = client.post(
            f"/api/v1/circulation/complete-return/{task_id}/cancel", headers=headers
        )
        assert cancelled.status_code == 202
        final = _wait_task_status(task_id, {"cancelled", "failed", "completed"})
        assert final["status"] == "cancelled"
        assert fake.cancelled_return == ["BT-RET-1"]
        assert fake.cancelled == []
    finally:
        _cleanup(store)


def test_complete_return_end_mode_rejects_an_injected_grasp_pose() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        created = client.post(
            "/api/v1/circulation/complete-return",
            headers=headers,
            json=_payload(returnMode="end", injectedTargetGraspPose=[0.1, 0.2, 0.3, 0.0, 0.0, 0.0]),
        )
        assert created.status_code == 422
    finally:
        _cleanup(store)
