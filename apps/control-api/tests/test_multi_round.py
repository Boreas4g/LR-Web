"""Multi-round take/return orchestration tests: navigate once, then per-book
takeout into stand slots N..1 (inside-out) followed by mid-return picking
slots 1..M (outside-in)."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import Book, RobotPose

client = TestClient(app)


class FakeBookarmClient:
    """Test double for the unix-socket bookarm client (takeout + midreturn)."""

    def __init__(
        self,
        takeout_states: list[str] | None = None,
        midreturn_states: list[str] | None = None,
        running_polls: int = 0,
    ) -> None:
        self.takeout_states = list(takeout_states or [])
        self.midreturn_states = list(midreturn_states or [])
        self.takeout_created: list[dict[str, object]] = []
        self.midreturn_created: list[dict[str, object]] = []
        self.cancelled: list[str] = []
        self.park_calls: list[object] = []
        # Number of running polls (with completed vision stages) each task
        # reports before its configured terminal state.
        self.running_polls = running_polls
        self.vision_image_calls = 0
        self._takeout_ids: list[str] = []
        self._midreturn_ids: list[str] = []
        self._takeout_polls: dict[str, int] = {}
        self._midreturn_polls: dict[str, int] = {}

    def create_takeout_task(self, **kwargs):  # noqa: ANN003, ANN201 - test double
        self.takeout_created.append(kwargs)
        task_id = f"BT-TAKE-{len(self._takeout_ids) + 1}"
        self._takeout_ids.append(task_id)
        return {"id": task_id, "state": "running"}

    def get_takeout_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        index = self._takeout_ids.index(task_id)
        state = (
            self.takeout_states[index] if index < len(self.takeout_states) else "succeeded"
        )
        polls = self._takeout_polls.get(task_id, 0)
        self._takeout_polls[task_id] = polls + 1
        if polls < self.running_polls:
            return {
                "id": task_id,
                "state": "running",
                "stages": [
                    {"name": "vision_target", "status": "succeeded"},
                    {"name": "book_grasp", "status": "running"},
                ],
            }
        return {"id": task_id, "state": state, "error": "ik 失败" if state == "failed" else None}

    def cancel_takeout_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.cancelled.append(task_id)
        return {"id": task_id, "state": "cancelled"}

    def create_midreturn_task(self, **kwargs):  # noqa: ANN003, ANN201 - test double
        self.midreturn_created.append(kwargs)
        task_id = f"BT-MID-{len(self._midreturn_ids) + 1}"
        self._midreturn_ids.append(task_id)
        return {"id": task_id, "state": "running"}

    def get_midreturn_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        index = self._midreturn_ids.index(task_id)
        state = (
            self.midreturn_states[index] if index < len(self.midreturn_states) else "succeeded"
        )
        polls = self._midreturn_polls.get(task_id, 0)
        self._midreturn_polls[task_id] = polls + 1
        if polls < self.running_polls:
            return {
                "id": task_id,
                "state": "running",
                "stages": [
                    {"name": "vision_target", "status": "succeeded"},
                    {"name": "vision_retarget", "status": "succeeded"},
                    {"name": "shelf_insert", "status": "running"},
                ],
            }
        return {"id": task_id, "state": state, "error": "ik 失败" if state == "failed" else None}

    def cancel_midreturn_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.cancelled.append(task_id)
        return {"id": task_id, "state": "cancelled"}

    def disable_arm(self, duration_s=3.0):  # noqa: ANN001, ANN202 - test double
        self.park_calls.append(("disable_arm", duration_s))
        return {"ok": True, "enabled": False}

    def move_lift(self, height_mm, speed_level=2):  # noqa: ANN001, ANN202 - test double
        self.park_calls.append(("move_lift", height_mm, speed_level))
        return {"target_height_mm": height_mm}

    def get_vision_debug_image(self, kind):  # noqa: ANN001, ANN202 - test double
        self.vision_image_calls += 1
        return f"fake-{kind}-jpeg".encode()


def _headers() -> dict[str, str]:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _store():
    from app.store import store

    return store


def _prepare_robot() -> None:
    from datetime import UTC, datetime

    store = _store()
    index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    store.robots[index] = store.robots[index].model_copy(
        update={
            "status": "idle",
            "localizationStatus": "localized",
            "pose": RobotPose(
                frameId="map", x=0.0, y=0.0, yaw=0.0, capturedAt=datetime.now(UTC)
            ),
        }
    )


def _seed_books(count: int = 3) -> list[str]:
    store = _store()
    ids = []
    with store._lock:
        for index in range(1, count + 1):
            book = Book(
                id=f"BOOK-MR-{index}",
                name=f"测试书{index}",
                templateId=f"TPL-{index}",
                heightMm=300 + index * 100,
                createdAt="2026-01-01T00:00:00+00:00",
                updatedAt="2026-01-01T00:00:00+00:00",
            )
            store.books[book.id] = book
            ids.append(book.id)
    return ids


def _payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "robotId": "LR-01",
        "target": {"x": 1.5, "y": -2.0, "yaw": 1.57},
        "takeBookIds": ["BOOK-MR-1", "BOOK-MR-2", "BOOK-MR-3"],
        "returnBookIds": ["BOOK-MR-3", "BOOK-MR-1"],
    }
    payload.update(overrides)
    return payload


def _multi_round_tasks() -> list[dict[str, object]]:
    return [task.model_dump() for task in _store().tasks if task.type == "multi_round"]


def _wait_task_status(task_id: str, statuses: set[str], timeout_s: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        task = next((item for item in _multi_round_tasks() if item["id"] == task_id), None)
        if task and task["status"] in statuses:
            return task
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach {statuses} in time")


def _wait_condition(predicate, timeout_s: float = 5.0) -> None:  # noqa: ANN001 - test helper
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met in time")


def _finish_navigation(robot_id: str = "LR-01", event: str = "navigation_succeeded") -> None:
    def find() -> str | None:
        nav = next(
            (
                item
                for item in _store().tasks
                if item.type == "navigate" and item.target.startswith("多轮取还书导航")
            ),
            None,
        )
        return nav.id.replace("TSK-", "CMD-", 1) if nav else None

    _wait_condition(lambda: find() is not None)
    _store().handle_gateway_event(
        {"event": event, "robotId": robot_id, "commandId": find()}
    )


def _finish_fine_tune(robot_id: str = "LR-01", event: str = "command_completed") -> None:
    def find() -> str | None:
        fine_tune = next(
            (
                item
                for item in _store().tasks
                if item.type == "fine_tune" and item.target.startswith("多轮取还书精调")
            ),
            None,
        )
        return fine_tune.id.replace("TSK-", "CMD-", 1) if fine_tune else None

    _wait_condition(lambda: find() is not None)
    _store().handle_gateway_event(
        {"event": event, "robotId": robot_id, "commandId": find()}
    )


def test_multi_round_happy_path_takes_then_returns_in_slot_order() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    _seed_books()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="mr-vision-"))
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="mr-vision-"))
    try:
        created = client.post("/api/v1/circulation/multi-round", headers=headers, json=_payload())
        assert created.status_code == 201
        task = created.json()
        assert task["type"] == "multi_round"
        assert [entry["slot"] for entry in task["workflowDetails"]["takeBooks"]] == [3, 2, 1]
        assert [entry["slot"] for entry in task["workflowDetails"]["returnBooks"]] == [1, 2]

        _finish_navigation()
        _finish_fine_tune()
        final = _wait_task_status(task["id"], {"completed", "failed"})
        assert final["status"] == "completed", final.get("failureReason")
        assert final["progress"] == 100

        # Takeouts ran in order into slots 3..1 (inside-out) with each book's
        # own height.
        assert [call["destination_slot"] for call in fake.takeout_created] == [3, 2, 1]
        assert [call["template_id"] for call in fake.takeout_created] == [
            "TPL-1",
            "TPL-2",
            "TPL-3",
        ]
        assert [call["target_height_mm"] for call in fake.takeout_created] == [400, 500, 600]
        # Returns consumed slots 1..2 with the return targets' templates/heights.
        assert [call["source_slot"] for call in fake.midreturn_created] == [1, 2]
        assert [call["template_id"] for call in fake.midreturn_created] == ["TPL-3", "TPL-1"]
        assert [call["target_height_mm"] for call in fake.midreturn_created] == [600, 400]
        # Parked at the end: arm safe-disabled, lift lowered to 10 mm.
        assert fake.park_calls == [("disable_arm", 3.0), ("move_lift", 10.0, 2)]

        # Each take/return entry carries its spine-recognition snapshot; the
        # return flow no longer runs a gap scan, so no gap overlay is stored.
        # URLs carry a version query so a refreshed image busts the cache.
        take_entries = final["workflowDetails"]["takeBooks"]
        for entry in take_entries:
            spine = entry["visionImages"]["spine"]
            assert spine.split("?")[0].endswith(f"takeBooks-{entry['slot']}-annotated.jpg")
            assert "?v=" in spine
            assert "gap" not in entry["visionImages"]
            image = client.get(spine, headers=headers)
            assert image.status_code == 200
            assert image.content == b"fake-annotated-jpeg"
        for entry in final["workflowDetails"]["returnBooks"]:
            images = entry["visionImages"]
            assert images["spine"].split("?")[0].endswith(
                f"returnBooks-{entry['slot']}-annotated.jpg"
            )
            assert "gap" not in images
        # Bad image names and unknown tasks are rejected without path traversal.
        spine_path = spine.split("?")[0]
        assert client.get(f"{spine_path}/../{spine_path}", headers=headers).status_code == 404
        bad = client.get(
            spine_path.replace("annotated.jpg", "evil.jpg"), headers=headers
        )
        assert bad.status_code == 404
        unknown = client.get(
            "/api/v1/circulation/multi-round/TSK-NOPE/vision/takeBooks-1-annotated.jpg",
            headers=headers,
        )
        assert unknown.status_code == 404
    finally:
        _cleanup(store)


def test_multi_round_snaps_vision_images_as_each_recognition_completes() -> None:
    """While a sub-task is still running, each finished vision stage triggers
    an immediate snapshot instead of waiting for the sub-task to end."""
    store = _store()
    fake = FakeBookarmClient(running_polls=2)
    _prepare_robot()
    headers = _headers()
    _seed_books()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="mr-vision-"))
    try:
        created = client.post(
            "/api/v1/circulation/multi-round",
            headers=headers,
            json=_payload(takeBookIds=["BOOK-MR-1"], returnBookIds=["BOOK-MR-1"]),
        )
        assert created.status_code == 201
        task_id = created.json()["id"]

        _finish_navigation()
        _finish_fine_tune()
        final = _wait_task_status(task_id, {"completed", "failed"})
        assert final["status"] == "completed", final.get("failureReason")

        # One take: vision_target snapshot during the run + final snapshot at
        # task end = 2. One return: vision_target + vision_retarget during the
        # run + final snapshot = 3.
        assert fake.vision_image_calls == 5
        details = final["workflowDetails"]
        assert details["takeBooks"][0]["visionImages"]["spine"]
        assert details["returnBooks"][0]["visionImages"]["spine"]
    finally:
        _cleanup(store)


def test_wait_for_bookarm_task_fires_vision_callback_once_per_stage() -> None:
    store = _store()
    store.takeout_poll_interval_s = 0.01
    views = [
        {
            "state": "running",
            "stages": [
                {"name": "lift_move", "status": "succeeded"},
                {"name": "vision_target", "status": "running"},
            ],
        },
        {
            "state": "running",
            "stages": [
                {"name": "vision_target", "status": "succeeded"},
                {"name": "book_grasp", "status": "running"},
            ],
        },
        {
            "state": "running",
            "stages": [
                {"name": "vision_target", "status": "succeeded"},
                {"name": "vision_retarget", "status": "succeeded"},
                {"name": "shelf_insert", "status": "running"},
            ],
        },
        {"state": "succeeded", "stages": []},
    ]

    class ScriptClient:
        def __init__(self) -> None:
            self.polls = 0

        def get_takeout_task(self, task_id):  # noqa: ANN001, ANN202 - scripted view
            view = views[min(self.polls, len(views) - 1)]
            self.polls += 1
            return view

        def cancel_takeout_task(self, task_id):  # noqa: ANN001, ANN202 - scripted view
            return {"id": task_id, "state": "cancelled"}

    fired: list[str] = []
    state = store._wait_for_bookarm_task(
        "TSK-UNIT",
        ScriptClient(),
        "BT-1",
        None,
        on_vision_stage=fired.append,
    )
    assert state == "succeeded"
    assert fired == ["vision_target", "vision_retarget"]


def test_multi_round_rejects_invalid_selections() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    _seed_books()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="mr-vision-"))
    try:
        more_returns = client.post(
            "/api/v1/circulation/multi-round",
            headers=headers,
            json=_payload(takeBookIds=["BOOK-MR-1"], returnBookIds=["BOOK-MR-1", "BOOK-MR-2"]),
        )
        assert more_returns.status_code == 409
        assert "还书数量" in more_returns.json()["detail"]

        unknown_book = client.post(
            "/api/v1/circulation/multi-round",
            headers=headers,
            json=_payload(takeBookIds=["BOOK-NOPE"], returnBookIds=[]),
        )
        assert unknown_book.status_code == 409
        assert "不存在" in unknown_book.json()["detail"]

        empty_takes = client.post(
            "/api/v1/circulation/multi-round",
            headers=headers,
            json=_payload(takeBookIds=[], returnBookIds=[]),
        )
        assert empty_takes.status_code == 422
    finally:
        _cleanup(store)


def test_multi_round_fails_when_navigation_fails() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    _seed_books()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="mr-vision-"))
    try:
        created = client.post(
            "/api/v1/circulation/multi-round",
            headers=headers,
            json=_payload(takeBookIds=["BOOK-MR-1"], returnBookIds=[]),
        )
        assert created.status_code == 201
        task_id = created.json()["id"]

        _finish_navigation(event="navigation_failed")
        final = _wait_task_status(task_id, {"failed", "completed"})
        assert final["status"] == "failed"
        assert "导航" in (final["failureReason"] or "")
        assert fake.takeout_created == []
        assert fake.midreturn_created == []
        assert fake.park_calls == []
    finally:
        _cleanup(store)


def test_multi_round_mid_takeout_failure_keeps_finished_slots_occupied() -> None:
    store = _store()
    fake = FakeBookarmClient(takeout_states=["succeeded", "failed"])
    _prepare_robot()
    headers = _headers()
    _seed_books()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="mr-vision-"))
    try:
        created = client.post(
            "/api/v1/circulation/multi-round",
            headers=headers,
            json=_payload(takeBookIds=["BOOK-MR-1", "BOOK-MR-2"], returnBookIds=[]),
        )
        assert created.status_code == 201
        task_id = created.json()["id"]

        _finish_navigation()
        _finish_fine_tune()
        final = _wait_task_status(task_id, {"failed", "completed"})
        assert final["status"] == "failed"
        assert "取书 2/2" in (final["failureReason"] or "")
        assert len(fake.takeout_created) == 2
        # Two takes fill slots 2 then 1 (inside-out); the first take finished,
        # the second failed: entries reflect that.
        entries = {
            entry["slot"]: entry for entry in final["workflowDetails"]["takeBooks"]
        }
        assert entries[2]["status"] == "completed"
        assert entries[1]["status"] == "running"
        # A failed run must not park: the operator needs the arm enabled.
        assert fake.park_calls == []
    finally:
        _cleanup(store)


def test_multi_round_cancel_during_arm_phase_cancels_the_bookarm_task() -> None:
    store = _store()
    fake = FakeBookarmClient(takeout_states=["running"])
    _prepare_robot()
    headers = _headers()
    _seed_books()
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="mr-vision-"))
    try:
        created = client.post(
            "/api/v1/circulation/multi-round",
            headers=headers,
            json=_payload(takeBookIds=["BOOK-MR-1"], returnBookIds=[]),
        )
        assert created.status_code == 201
        task_id = created.json()["id"]

        _finish_navigation()
        _finish_fine_tune()
        _wait_condition(lambda: len(fake.takeout_created) == 1)

        cancelled = client.post(
            f"/api/v1/circulation/multi-round/{task_id}/cancel", headers=headers
        )
        assert cancelled.status_code == 202
        final = _wait_task_status(task_id, {"cancelled", "failed", "completed"})
        assert final["status"] == "cancelled"
        assert fake.cancelled == ["BT-TAKE-1"]
        assert fake.park_calls == []
    finally:
        _cleanup(store)


def test_multi_round_without_navigation_starts_takeout_in_place() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    _seed_books(1)
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="mr-vision-"))
    try:
        # Not localized and no pose: with sendNavigation off this must not block.
        index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
        store.robots[index] = store.robots[index].model_copy(
            update={"localizationStatus": "unknown", "pose": None}
        )

        created = client.post(
            "/api/v1/circulation/multi-round",
            headers=headers,
            json=_payload(
                target=None, sendNavigation=False, takeBookIds=["BOOK-MR-1"], returnBookIds=[]
            ),
        )
        assert created.status_code == 201
        task = created.json()
        assert task["workflowDetails"]["sendNavigation"] is False

        final = _wait_task_status(task["id"], {"completed", "failed"})
        assert final["status"] == "completed", final.get("failureReason")
        # No navigate/fine_tune dispatch rows were created for this flow.
        assert not [
            item
            for item in store.tasks
            if item.type in {"navigate", "fine_tune"}
            and item.target.startswith(("多轮取还书导航", "多轮取还书精调"))
        ]
        assert [call["destination_slot"] for call in fake.takeout_created] == [1]
        assert fake.midreturn_created == []
    finally:
        _prepare_robot()
        _cleanup(store)


def test_multi_round_navigation_requires_target() -> None:
    store = _store()
    _prepare_robot()
    headers = _headers()
    _seed_books(1)
    try:
        missing = client.post(
            "/api/v1/circulation/multi-round",
            headers=headers,
            json=_payload(target=None, takeBookIds=["BOOK-MR-1"], returnBookIds=[]),
        )
        assert missing.status_code == 422
    finally:
        _cleanup(store)


def _cleanup(store) -> None:  # noqa: ANN001 - shared singleton cleanup
    store.bookarm_client_factory = None
    store.takeout_poll_interval_s = 2.0
    store.multi_round_vision_dir = Path("data/multi-round-vision")
    with store._lock:
        store.tasks = [
            task for task in store.tasks if task.type not in {"multi_round", "fine_tune"}
        ]
        store.tasks = [
            task
            for task in store.tasks
            if not (task.type == "navigate" and task.target.startswith("多轮取还书导航"))
        ]
        for book_id in ("BOOK-MR-1", "BOOK-MR-2", "BOOK-MR-3"):
            store.books.pop(book_id, None)
    deadline = time.monotonic() + 2.0
    while store._multi_round_cancels and time.monotonic() < deadline:
        time.sleep(0.01)
