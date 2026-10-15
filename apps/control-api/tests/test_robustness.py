"""Orchestration robustness: timeout cancels, 409 retry, reason passthrough,
recovery channel, startup reconciliation, manual coordinates, real updatedAt."""

from __future__ import annotations

import tempfile
import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.bookarm_client import BookarmError
from app.friendly_errors import describe_bookarm_detail, describe_navigation_failure
from app.main import app
from app.schemas import DispatchTask, RobotPose

client = TestClient(app)


def _store():
    from app.store import store

    return store


def _headers() -> dict[str, str]:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _prepare_robot() -> None:
    from datetime import UTC

    store = _store()
    index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    store.robots[index] = store.robots[index].model_copy(
        update={
            "status": "idle",
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


def _takeout_task(task_id: str) -> DispatchTask | None:
    return next((item for item in _store().tasks if item.id == task_id), None)


def _wait_task_status(task_id: str, statuses: set[str], timeout_s: float = 5.0) -> DispatchTask:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        task = _takeout_task(task_id)
        if task and task.status in statuses:
            return task
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach {statuses} in time")


def _stage_command_id(task_type: str, timeout_s: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        stage = next((item for item in _store().tasks if item.type == task_type), None)
        if stage is not None:
            return stage.id.replace("TSK-", "CMD-", 1)
        time.sleep(0.01)
    raise AssertionError(f"{task_type} stage was not dispatched in time")


def _finish_stage(
    task_type: str,
    event: str,
    details: dict[str, object] | None = None,
    robot_id: str = "LR-01",
) -> None:
    command_id = _stage_command_id(task_type)
    payload: dict[str, object] = {"event": event, "robotId": robot_id, "commandId": command_id}
    if details:
        payload["details"] = details
    _store().handle_gateway_event(payload)


def _cancel_commands_for(stage_task_id: str) -> list[object]:
    return [
        command
        for command in _store().commands.values()
        if command.command == "cancel" and command.payload.get("taskId") == stage_task_id
    ]


class FakeBookarmClient:
    """Test double with tunable create/get/cancel behaviour."""

    def __init__(
        self,
        final_state: str = "succeeded",
        error: str | None = None,
        create_failures: int = 0,
    ) -> None:
        self.final_state = final_state
        self.error = error
        self.create_failures = create_failures
        self.created: list[dict[str, object]] = []
        self.cancelled: list[str] = []
        self.dismissed: list[str] = []

    def create_takeout_task(self, **kwargs):  # noqa: ANN003, ANN201 - test double
        self.created.append(kwargs)
        if len(self.created) <= self.create_failures:
            raise BookarmError(409, "another task is already running")
        return {"id": "BT-TEST-1", "state": "running"}

    def create_midreturn_task(self, **kwargs):  # noqa: ANN003, ANN201 - test double
        return self.create_takeout_task(**kwargs)

    def get_takeout_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        return {"id": task_id, "state": self.final_state, "error": self.error}

    def get_midreturn_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        return {"id": task_id, "state": self.final_state, "error": self.error}

    def cancel_takeout_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.cancelled.append(task_id)
        return {"id": task_id, "state": "cancelled"}

    def cancel_midreturn_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.cancelled.append(task_id)
        return {"id": task_id, "state": "cancelled"}

    def get_return_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        return {"id": task_id, "state": self.final_state, "error": self.error}

    def cancel_return_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.cancelled.append(task_id)
        return {"id": task_id, "state": "cancelled"}

    def dismiss_recovery(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.dismissed.append(task_id)
        return {"id": task_id, "state": "dismissed"}

    def get_vision_debug_image(self, kind):  # noqa: ANN001, ANN202 - test double
        return f"fake-{kind}".encode()


def _setup(store, fake: FakeBookarmClient, **timeouts: float) -> None:  # noqa: ANN001
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    for name, value in timeouts.items():
        setattr(store, name, value)


def _setup(store, fake: FakeBookarmClient, **timeouts: float) -> None:  # noqa: ANN001
    store.bookarm_client_factory = lambda: fake
    store.takeout_poll_interval_s = 0.01
    # Vision snapshots must not leak into the repo data dir from tests.
    store.multi_round_vision_dir = Path(tempfile.mkdtemp(prefix="robustness-vision-"))
    for name, value in timeouts.items():
        setattr(store, name, value)


def _cleanup(store) -> None:  # noqa: ANN001 - shared singleton cleanup
    store.bookarm_client_factory = None
    store.takeout_poll_interval_s = 2.0
    store.takeout_nav_timeout_s = 300.0
    store.takeout_arm_timeout_s = 600.0
    store.multi_round_vision_dir = Path("data/multi-round-vision")
    with store._lock:
        store.tasks = [
            task
            for task in store.tasks
            if task.type not in {"takeout", "return", "fine_tune", "multi_round"}
            and not (task.type == "navigate" and task.target.startswith("取书导航"))
        ]
    deadline = time.monotonic() + 2.0
    while store._takeout_cancels and time.monotonic() < deadline:
        time.sleep(0.01)


# --------------------------------------------------------------------------
# 1. Timeout branches issue a cancel (gateway cancel / bookarm cancel_task).
# --------------------------------------------------------------------------


def test_navigation_timeout_dispatches_vehicle_cancel() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    _setup(store, fake, takeout_nav_timeout_s=0.05)
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        nav_command_id = _stage_command_id("navigate")
        nav_task_id = nav_command_id.replace("CMD-", "TSK-", 1)

        final = _wait_task_status(task_id, {"failed", "completed"})
        assert final.status == "failed"
        assert "导航超时" in (final.failureReason or "")
        assert "已下发取消" in (final.failureReason or "")
        assert _cancel_commands_for(nav_task_id), "超时后未向车端下发导航取消"
        assert fake.created == []
    finally:
        _cleanup(store)


def test_fine_tune_timeout_dispatches_vehicle_cancel() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    _setup(store, fake, takeout_nav_timeout_s=0.2)
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        _finish_stage("navigate", "navigation_succeeded")
        fine_tune_command_id = _stage_command_id("fine_tune")
        fine_tune_task_id = fine_tune_command_id.replace("CMD-", "TSK-", 1)

        final = _wait_task_status(task_id, {"failed", "completed"})
        assert final.status == "failed"
        assert "低速精调超时" in (final.failureReason or "")
        assert "已下发取消" in (final.failureReason or "")
        assert _cancel_commands_for(fine_tune_task_id), "超时后未向车端下发精调取消"
        assert fake.created == []
    finally:
        _cleanup(store)


def test_bookarm_timeout_cancels_the_bookarm_task() -> None:
    store = _store()
    fake = FakeBookarmClient(final_state="running")
    _prepare_robot()
    headers = _headers()
    _setup(store, fake, takeout_arm_timeout_s=0.05)
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        _finish_stage("navigate", "navigation_succeeded")
        _finish_stage("fine_tune", "command_completed")

        final = _wait_task_status(task_id, {"failed", "completed"})
        assert final.status == "failed"
        assert "超时" in (final.failureReason or "")
        assert "已请求机械臂停止" in (final.failureReason or "")
        assert fake.cancelled == ["BT-TEST-1"], "超时后未调用 bookarm cancel_task"
    finally:
        _cleanup(store)


# --------------------------------------------------------------------------
# 2. Single takeout/return bookarm task creation retries on 409.
# --------------------------------------------------------------------------


def test_create_takeout_retries_409_then_succeeds() -> None:
    store = _store()
    fake = FakeBookarmClient(create_failures=2)
    _prepare_robot()
    headers = _headers()
    _setup(store, fake)
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        _finish_stage("navigate", "navigation_succeeded")
        _finish_stage("fine_tune", "command_completed")

        final = _wait_task_status(task_id, {"completed", "failed"})
        assert final.status == "completed"
        assert len(fake.created) == 3, "409 后未重试创建 bookarm 任务"
    finally:
        _cleanup(store)


def test_create_takeout_persistent_409_fails_with_friendly_message() -> None:
    store = _store()
    fake = FakeBookarmClient(create_failures=99)
    _prepare_robot()
    headers = _headers()
    _setup(store, fake)
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        _finish_stage("navigate", "navigation_succeeded")
        _finish_stage("fine_tune", "command_completed")

        final = _wait_task_status(task_id, {"completed", "failed"}, timeout_s=10.0)
        assert final.status == "failed"
        assert "机械臂正在执行上一个任务，请稍候重试" in (final.failureReason or "")
        assert len(fake.created) == 5, "409 重试次数不符合预期"
    finally:
        _cleanup(store)


# --------------------------------------------------------------------------
# 3. Gateway failure reasons are passed through; cancel is terminal.
# --------------------------------------------------------------------------


def test_navigation_failure_reason_is_passed_through_and_translated() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    _setup(store, fake)
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        _finish_stage(
            "navigate",
            "navigation_failed",
            details={"reason": "mapId is not the active vehicle map"},
        )

        final = _wait_task_status(task_id, {"failed", "completed"})
        assert final.status == "failed"
        assert "导航未成功" in (final.failureReason or "")
        assert "活动地图" in (final.failureReason or "")
        nav_task = next(item for item in store.tasks if item.type == "navigate")
        assert nav_task.failureReason == "mapId is not the active vehicle map"
    finally:
        _cleanup(store)


def test_navigation_cancelled_marks_the_stage_task_cancelled_not_paused() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    _setup(store, fake)
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        _finish_stage("navigate", "navigation_cancelled")

        nav_task = next(item for item in store.tasks if item.type == "navigate")
        assert nav_task.status == "cancelled"
        # updatedAt is a real ISO timestamp after the gateway event.
        datetime.fromisoformat(nav_task.updatedAt)
        final = _wait_task_status(task_id, {"failed", "completed", "cancelled"})
        assert final.status == "failed"
        assert "导航未成功" in (final.failureReason or "")
    finally:
        _cleanup(store)


def test_friendly_error_patterns() -> None:
    assert "书脊模板不存在" in describe_bookarm_detail("unknown template_id: LIB-TPL-9")
    assert "未夹到书" in describe_bookarm_detail("gripper closed without contact")
    assert "机械臂正在执行上一个任务" in describe_bookarm_detail(
        "another task is already running"
    )
    assert "活动地图" in describe_navigation_failure("mapId is not the active vehicle map")
    assert "轮速反馈" in describe_navigation_failure("base wheel feedback is not fresh")
    assert "Nav2 拒绝" in describe_navigation_failure("Nav2 rejected the goal")
    assert "目标不可达" in describe_navigation_failure("goal unreachable")
    assert "定位不稳定" in describe_navigation_failure("localization is unstable")
    assert "低速精调超时" in describe_navigation_failure("fine_tune timed out")
    assert describe_navigation_failure("定位不稳定，请稍候") == "定位不稳定，请稍候"


# --------------------------------------------------------------------------
# 4. updatedAt is a real ISO timestamp everywhere.
# --------------------------------------------------------------------------


def test_updated_at_is_a_real_iso_timestamp() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    _setup(store, fake)
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        queued = _takeout_task(task_id)
        assert queued is not None
        datetime.fromisoformat(queued.updatedAt)
        assert queued.updatedAt != "刚刚"

        _finish_stage("navigate", "navigation_succeeded")
        _finish_stage("fine_tune", "command_completed")
        final = _wait_task_status(task_id, {"completed", "failed"})
        assert final.status == "completed"
        finished_at = datetime.fromisoformat(final.updatedAt)
        assert finished_at >= datetime.fromisoformat(queued.updatedAt)
    finally:
        _cleanup(store)


# --------------------------------------------------------------------------
# 5. Recovery channel: bookarm RECOVERY_REQUIRED parks the task; the proxy
#    endpoint settles it after the operator confirms manual handling.
# --------------------------------------------------------------------------


def test_recovery_required_parks_task_and_dismiss_settles_it() -> None:
    store = _store()
    fake = FakeBookarmClient(final_state="recovery_required", error="ik 失败")
    _prepare_robot()
    headers = _headers()
    _setup(store, fake)
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        _finish_stage("navigate", "navigation_succeeded")
        _finish_stage("fine_tune", "command_completed")

        parked = _wait_task_status(task_id, {"recovery_required", "failed", "completed"})
        assert parked.status == "recovery_required"
        assert "机械臂中断待确认" in (parked.workflowMessage or "")
        assert "ik" in (parked.failureReason or "")

        dismissed = client.post(
            "/api/v1/bookarm/tasks/BT-TEST-1/dismiss-recovery", headers=headers
        )
        assert dismissed.status_code == 200
        assert fake.dismissed == ["BT-TEST-1"]
        settled = _takeout_task(task_id)
        assert settled is not None
        assert settled.status == "failed"
        assert "人工接管" in (settled.workflowMessage or "")
    finally:
        _cleanup(store)


def test_dismiss_recovery_proxy_passes_through_bookarm_409() -> None:
    store = _store()

    class NotInRecoveryClient(FakeBookarmClient):
        def dismiss_recovery(self, task_id):  # noqa: ANN001, ANN202 - test double
            raise BookarmError(409, "task is not in recovery state")

    _prepare_robot()
    headers = _headers()
    _setup(store, NotInRecoveryClient())
    try:
        response = client.post(
            "/api/v1/bookarm/tasks/BT-DONE-1/dismiss-recovery", headers=headers
        )
        assert response.status_code == 409
        assert "recovery" in response.json()["detail"]
    finally:
        _cleanup(store)


# --------------------------------------------------------------------------
# 6. Startup reconciliation against bookarm's real task state.
# --------------------------------------------------------------------------


def _insert_orphan(
    task_id: str,
    status: str = "running",
    workflow_details: dict[str, object] | None = None,
    task_type: str = "takeout",
) -> None:
    store = _store()
    with store._lock:
        store.tasks.insert(
            0,
            DispatchTask(
                id=task_id,
                type=task_type,
                robotId="LR-01",
                target="LIB-TPL-OLD",
                status=status,  # type: ignore[arg-type]
                progress=40,
                updatedAt="2026-09-01T00:00:00+00:00",
                workflowMessage="取书执行中",
                workflowDetails=workflow_details or {},
            ),
        )


def test_reconciliation_marks_a_still_running_bookarm_task_for_recovery() -> None:
    store = _store()
    fake = FakeBookarmClient(final_state="running")
    _setup(store, fake)
    try:
        _insert_orphan("TSK-ORPHAN-RUN", workflow_details={"bookarmTaskId": "BT-ORPHAN-1"})
        store._reconcile_orphaned_flows()
        task = _takeout_task("TSK-ORPHAN-RUN")
        assert task is not None
        assert task.status == "recovery_required"
        assert "控制站重启" in (task.workflowMessage or "")
        assert "调试页" in (task.workflowMessage or "")
    finally:
        _cleanup(store)


def test_reconciliation_syncs_terminal_states_and_orphans_without_refs() -> None:
    store = _store()
    fake = FakeBookarmClient(final_state="succeeded")
    _setup(store, fake)
    try:
        _insert_orphan("TSK-ORPHAN-DONE", workflow_details={"bookarmTaskId": "BT-ORPHAN-2"})
        _insert_orphan("TSK-ORPHAN-NAV")  # 无 bookarmTaskId：未进入机械臂阶段
        store._reconcile_orphaned_flows()
        done = _takeout_task("TSK-ORPHAN-DONE")
        assert done is not None
        assert done.status == "completed"
        assert done.progress == 100
        orphan = _takeout_task("TSK-ORPHAN-NAV")
        assert orphan is not None
        assert orphan.status == "cancelled"
        assert "服务重启" in (orphan.workflowMessage or "")
    finally:
        _cleanup(store)


def test_reconciliation_unreachable_bookarm_marks_recovery() -> None:
    store = _store()

    class UnreachableClient(FakeBookarmClient):
        def get_takeout_task(self, task_id):  # noqa: ANN001, ANN202 - test double
            raise BookarmError(None, "bookarm 服务不可达")

        def get_midreturn_task(self, task_id):  # noqa: ANN001, ANN202 - test double
            raise BookarmError(None, "bookarm 服务不可达")

    _setup(store, UnreachableClient())
    try:
        _insert_orphan("TSK-ORPHAN-DOWN", workflow_details={"bookarmTaskId": "BT-ORPHAN-3"})
        store._reconcile_orphaned_flows()
        task = _takeout_task("TSK-ORPHAN-DOWN")
        assert task is not None
        assert task.status == "recovery_required"
        assert "不可达" in (task.workflowMessage or "")
    finally:
        _cleanup(store)


class _ReturnTrackingClient(FakeBookarmClient):
    """Records which bookarm getter each reconciliation query used."""

    def __init__(self, **kwargs) -> None:  # noqa: ANN003 - test double
        super().__init__(**kwargs)
        self.queries: list[str] = []

    def get_takeout_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.queries.append("takeout")
        return super().get_takeout_task(task_id)

    def get_midreturn_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.queries.append("midreturn")
        return super().get_midreturn_task(task_id)

    def get_return_task(self, task_id):  # noqa: ANN001, ANN202 - test double
        self.queries.append("return")
        return super().get_return_task(task_id)


def test_reconciliation_end_mode_return_syncs_via_the_return_getter() -> None:
    store = _store()
    fake = _ReturnTrackingClient(final_state="succeeded")
    _setup(store, fake)
    try:
        _insert_orphan(
            "TSK-ORPHAN-END",
            task_type="return",
            workflow_details={"bookarmTaskId": "BT-ORPHAN-END", "returnMode": "end"},
        )
        store._reconcile_orphaned_flows()
        task = _takeout_task("TSK-ORPHAN-END")
        assert task is not None
        assert task.status == "completed"
        assert task.progress == 100
        assert "对账确认" in (task.workflowMessage or "")
        # Live-state probe plus the terminal-outcome adoption query: both must
        # hit /return/tasks, never the midreturn or takeout getters.
        assert fake.queries == ["return", "return"]
    finally:
        _cleanup(store)


def test_reconciliation_end_mode_return_still_running_parks_for_recovery() -> None:
    store = _store()
    fake = _ReturnTrackingClient(final_state="running")
    _setup(store, fake)
    try:
        _insert_orphan(
            "TSK-ORPHAN-END-RUN",
            task_type="return",
            workflow_details={"bookarmTaskId": "BT-ORPHAN-END-RUN", "returnMode": "end"},
        )
        store._reconcile_orphaned_flows()
        task = _takeout_task("TSK-ORPHAN-END-RUN")
        assert task is not None
        assert task.status == "recovery_required"
        assert "控制站重启" in (task.workflowMessage or "")
        assert fake.queries == ["return"]
    finally:
        _cleanup(store)


def test_reconciliation_mid_mode_return_still_uses_the_midreturn_getter() -> None:
    store = _store()
    fake = _ReturnTrackingClient(final_state="succeeded")
    _setup(store, fake)
    try:
        _insert_orphan(
            "TSK-ORPHAN-MID",
            task_type="return",
            workflow_details={"bookarmTaskId": "BT-ORPHAN-MID", "returnMode": "mid"},
        )
        store._reconcile_orphaned_flows()
        task = _takeout_task("TSK-ORPHAN-MID")
        assert task is not None
        assert task.status == "completed"
        assert fake.queries == ["midreturn", "midreturn"]
    finally:
        _cleanup(store)


# --------------------------------------------------------------------------
# 7. Manual coordinates get a generated annotationId for the vehicle.
# --------------------------------------------------------------------------


def test_manual_target_gets_a_generated_annotation_id() -> None:
    store = _store()
    fake = FakeBookarmClient()
    _prepare_robot()
    headers = _headers()
    _setup(store, fake)
    try:
        created = client.post(
            "/api/v1/circulation/complete-takeout", headers=headers, json=_payload()
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        nav_command_id = _stage_command_id("navigate")
        command = store.commands[nav_command_id]
        annotation_id = command.payload["annotationId"]
        assert isinstance(annotation_id, str)
        assert annotation_id.startswith("MANUAL-"), "手填坐标未生成临时 annotationId"

        _finish_stage("navigate", "navigation_succeeded")
        _finish_stage("fine_tune", "command_completed")
        final = _wait_task_status(task_id, {"completed", "failed"})
        assert final.status == "completed", final.failureReason
    finally:
        _cleanup(store)
