"""Complete-takeout orchestration tests: navigate, then on-robot takeout."""

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

    def create_takeout_task(self, **kwargs):  # noqa: ANN003, ANN201 - test double
        self.created.append(kwargs)
        return {"id": "BT-TEST-1", "state": "running"}

    def get_takeout_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        return {"id": task_id, "state": self.final_state, "error": self.error}

    def cancel_takeout_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.cancelled.append(task_id)
        return {"id": task_id, "state": "cancelled"}

    def get_vision_debug_image(self, kind):  # noqa: ANN001, ANN202 - test double
        return f"fake-takeout-{kind}".encode()


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
        "destinationSlot": 1,
    }
    payload.update(overrides)
    return payload


def _takeout_tasks() -> list[dict[str, object]]:
    return [
        task.model_dump()
        for task in _store().tasks
        if task.type == "takeout"
    ]


def _store():
    from app.store import store

    return store


def _wait_task_status(task_id: str, statuses: set[str], timeout_s: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        task = next((item for item in _takeout_tasks() if item["id"] == task_id), None)
        if task and task["status"] in statuses:
            return task
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach {statuses} in time")


def _navigation_command_id() -> str:
    nav = next(item for item in _store().tasks if item.type == "navigate")
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


def test_complete_takeout_requires_an_explicit_destination_slot() -> None:
    _prepare_robot()
    headers = _headers()
    payload = _payload()
    del payload["destinationSlot"]
    created = client.post(
        "/api/v1/circulation/complete-takeout", headers=headers, json=payload
    )
    assert created.status_code == 422


def test_complete_takeout_happy_path_places_into_the_requested_slot() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="flow-vision-"))
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task = created.json()
        assert task["type"] == "takeout"
        assert task["workflowDetails"]["destinationSlot"] == 1

        # The navigation command carries the raw map-frame target.
        nav_command_id = _navigation_command_id()
        command = store.commands[nav_command_id]
        assert command.command == "navigate"
        assert command.payload["x"] == 1.5
        assert command.payload["yaw"] == 1.57

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
        assert call["destination_slot"] == 1

        # The D435 spine-recognition snapshot is exposed and fetchable.
        images = final["workflowDetails"]["visionImages"]
        assert images["spine"].split("?")[0].endswith(
            f"/api/v1/circulation/complete-takeout/{task['id']}/vision/takeout-annotated.jpg"
        )
        assert "?v=" in images["spine"]
        image = client.get(images["spine"], headers=headers)
        assert image.status_code == 200
        assert image.content == b"fake-takeout-annotated"
        unknown = client.get(
            "/api/v1/circulation/complete-takeout/TSK-NOPE/vision/takeout-annotated.jpg",
            headers=headers,
        )
        assert unknown.status_code == 404
        bad = client.get(
            images["spine"].split("?")[0].replace("takeout-annotated.jpg", "evil.jpg"),
            headers=headers,
        )
        assert bad.status_code == 404
    finally:
        _cleanup(store)


def test_complete_takeout_fails_when_navigation_fails() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
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


def test_complete_takeout_fails_when_bookarm_task_fails() -> None:
    store = _store()
    fake = FakeBookarmClient(final_state="failed", error="ik 失败")
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
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


def test_complete_takeout_supersedes_a_second_concurrent_task_and_cancels() -> None:
    store = _store()
    fake = FakeBookarmClient(final_state="running")
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        first = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert first.status_code == 201
        first_id = first.json()["id"]
        second = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert second.status_code == 201

        final = _wait_task_status(first_id, {"cancelled", "failed", "completed"})
        assert final["status"] == "cancelled"

        cancelled = client.post(
            f"/api/v1/circulation/complete-takeout/{second.json()['id']}/cancel", headers=headers
        )
        assert cancelled.status_code == 202
        second_final = _wait_task_status(second.json()["id"], {"cancelled", "failed", "completed"})
        assert second_final["status"] == "cancelled"
    finally:
        _cleanup(store)


def test_complete_takeout_dispatches_fine_tune_between_navigation_and_bookarm() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task = created.json()

        time.sleep(0.05)
        _finish_navigation()

        # The fine_tune command goes out after navigation, before bookarm.
        fine_tune_command_id = _wait_fine_tune_command_id()
        assert fake.created == []
        command = store.commands[fine_tune_command_id]
        assert command.command == "fine_tune"
        assert command.payload["x"] == 1.5
        assert command.payload["y"] == -2.0
        assert command.payload["yaw"] == 1.57
        nav_command = store.commands[_navigation_command_id()]
        assert command.payload["mapId"] == nav_command.payload["mapId"]

        in_flight = next(item for item in _takeout_tasks() if item["id"] == task["id"])
        assert in_flight["progress"] == 40
        assert in_flight["workflowMessage"] == "低速精调对位"
        assert in_flight["workflowDetails"]["fineTuneTaskId"] == fine_tune_command_id.replace(
            "CMD-", "TSK-", 1
        )

        _finish_fine_tune()
        final = _wait_task_status(task["id"], {"completed", "failed"})
        assert final["status"] == "completed"
        assert len(fake.created) == 1
        fine_tune_row = next(item for item in store.tasks if item.type == "fine_tune")
        assert fine_tune_row.status == "completed"
    finally:
        _cleanup(store)


def test_complete_takeout_fails_when_fine_tune_is_rejected() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]

        time.sleep(0.05)
        _finish_navigation()
        _finish_fine_tune(
            event="command_rejected",
            details={"reason": "初始误差 0.62m，超过 0.40m 限制"},
        )
        final = _wait_task_status(task_id, {"failed", "completed"})
        assert final["status"] == "failed"
        assert "低速精调未成功" in (final["failureReason"] or "")
        assert "初始误差" in (final["failureReason"] or "")
        assert fake.created == []
    finally:
        _cleanup(store)


def test_complete_takeout_cancels_during_fine_tune() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]

        time.sleep(0.05)
        _finish_navigation()
        _wait_fine_tune_command_id()

        cancelled = client.post(
            f"/api/v1/circulation/complete-takeout/{task_id}/cancel", headers=headers
        )
        assert cancelled.status_code == 202
        final = _wait_task_status(task_id, {"cancelled", "failed", "completed"})
        assert final["status"] == "cancelled"
        assert fake.created == []
    finally:
        _cleanup(store)


def _cleanup(store) -> None:  # noqa: ANN001 - shared singleton cleanup
    store.bookarm_client_factory = None
    store.takeout_poll_interval_s = 2.0
    with store._lock:
        store.tasks = [
            task for task in store.tasks if task.type not in {"takeout", "fine_tune"}
        ]
        store.tasks = [
            task
            for task in store.tasks
            if not (
                task.type == "navigate" and task.target.startswith("取书导航")
            )
        ]
    deadline = time.monotonic() + 2.0
    while store._takeout_cancels and time.monotonic() < deadline:
        time.sleep(0.01)


def test_complete_takeout_supersedes_a_stale_running_task() -> None:
    """A stuck non-terminal takeout must never block a new one."""
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    stale_id = "TSK-STALE-STUCK-01"
    try:
        from app.schemas import DispatchTask

        with store._lock:
            store.tasks.insert(
                0,
                DispatchTask(
                    id=stale_id,
                    type="takeout",
                    robotId="LR-01",
                    target="LIB-TPL-OLD",
                    status="running",
                    progress=40,
                    updatedAt="昨天",
                    workflowMessage="取书执行中",
                ),
            )
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task = created.json()
        assert task["id"] != stale_id

        stale = next(item for item in store.tasks if item.id == stale_id)
        assert stale.status == "cancelled"
        assert "取代" in (stale.workflowMessage or "")

        time.sleep(0.05)
        _finish_navigation()
        _finish_fine_tune()
        final = _wait_task_status(task["id"], {"completed", "failed"})
        assert final["status"] == "completed"
    finally:
        _cleanup(store)


def test_complete_takeout_without_navigation_starts_the_arm_in_place() -> None:
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
            "/api/v1/circulation/complete-takeout", headers=headers, json=payload
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        final = _wait_task_status(task_id, {"completed", "failed"})
        assert final["status"] == "completed", final.get("failureReason")
        # No navigation or fine-tune stage tasks were dispatched.
        assert not [
            item
            for item in store.tasks
            if item.type in {"navigate", "fine_tune"} and item.target.startswith("取书")
        ]
        assert len(fake.created) == 1
    finally:
        _cleanup(store)


def test_complete_takeout_requires_a_target_when_navigating() -> None:
    _prepare_robot()
    headers = _headers()
    payload = _payload()
    del payload["target"]
    created = client.post(
        "/api/v1/circulation/complete-takeout", headers=headers, json=payload
    )
    assert created.status_code == 422
