"""阶段 6 运维韧性：告警确认、任务列表过滤与任务清理策略。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import AlertRecord, DispatchTask
from app.store import store


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def preserved_tasks():
    """Snapshot and restore the shared task list around a test."""
    original = list(store.tasks)
    try:
        yield
    finally:
        store.tasks = original
        store._schedule_persist()


@pytest.fixture
def preserved_alerts():
    original = list(store.alerts)
    try:
        yield
    finally:
        store.alerts = original
        store._schedule_persist()


def _task(
    task_id: str,
    status: str,
    updated_at: str,
    task_type: str = "takeout",
) -> DispatchTask:
    return DispatchTask(
        id=task_id,
        type=task_type,
        robotId="LR-01",
        target="测试目标",
        status=status,
        progress=100 if status == "completed" else 0,
        updatedAt=updated_at,
    )


def _alert(alert_id: str, acknowledged: bool = False) -> AlertRecord:
    return AlertRecord(
        id=alert_id,
        severity="warning",
        source="LR-01 / 升降台",
        title=f"测试告警 {alert_id}",
        time="10:42:17",
        acknowledged=acknowledged,
    )


class TestAlertAcknowledge:
    def test_ack_marks_alert_and_is_idempotent(self, client: TestClient, preserved_alerts) -> None:
        store.alerts = [_alert("ALT-T1"), _alert("ALT-T2", acknowledged=True)]

        response = client.post("/api/v1/alerts/ALT-T1/ack")
        assert response.status_code == 200
        assert response.json()["acknowledged"] is True
        assert store.alerts[0].acknowledged is True
        # 列表接口同步反映已确认态。
        listed = {item["id"]: item for item in client.get("/api/v1/alerts").json()}
        assert listed["ALT-T1"]["acknowledged"] is True
        assert listed["ALT-T2"]["acknowledged"] is True

        # 重复确认是幂等 no-op，不报错。
        again = client.post("/api/v1/alerts/ALT-T1/ack")
        assert again.status_code == 200
        assert again.json()["acknowledged"] is True

    def test_ack_unknown_alert_returns_404(self, client: TestClient, preserved_alerts) -> None:
        store.alerts = [_alert("ALT-T1")]
        response = client.post("/api/v1/alerts/ALT-NOPE/ack")
        assert response.status_code == 404
        assert "告警不存在" in response.json()["detail"]


class TestTaskListFiltering:
    def _seed(self) -> None:
        now = datetime.now(UTC)
        store.tasks = [
            _task("T-new-running", "running", now.isoformat()),
            _task("T-queued", "queued", (now - timedelta(minutes=1)).isoformat()),
            _task("T-paused", "paused", (now - timedelta(minutes=2)).isoformat()),
            _task("T-completed", "completed", (now - timedelta(minutes=3)).isoformat()),
            _task("T-old-failed", "failed", (now - timedelta(days=1)).isoformat()),
        ]

    def test_default_returns_full_list(self, client: TestClient, preserved_tasks) -> None:
        self._seed()
        response = client.get("/api/v1/dispatch/tasks")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == [
            "T-new-running",
            "T-queued",
            "T-paused",
            "T-completed",
            "T-old-failed",
        ]

    def test_status_filter_returns_only_listed_states(
        self, client: TestClient, preserved_tasks
    ) -> None:
        self._seed()
        response = client.get(
            "/api/v1/dispatch/tasks",
            params=[("status", "running"), ("status", "queued"), ("status", "paused")],
        )
        assert response.status_code == 200
        assert {item["id"] for item in response.json()} == {
            "T-new-running",
            "T-queued",
            "T-paused",
        }

    def test_limit_unions_recent_tasks_with_status_filter(
        self, client: TestClient, preserved_tasks
    ) -> None:
        self._seed()
        # 活动状态 + 最近 4 条：刚完成的 T-completed 仍在最近 4 条内，可被
        # 追踪它的轮询看到终态；更老的 T-old-failed 被排除。
        response = client.get(
            "/api/v1/dispatch/tasks",
            params=[("status", "running"), ("limit", 4)],
        )
        assert response.status_code == 200
        assert {item["id"] for item in response.json()} == {
            "T-new-running",
            "T-queued",
            "T-paused",
            "T-completed",
        }

    def test_invalid_status_is_rejected(self, client: TestClient, preserved_tasks) -> None:
        self._seed()
        response = client.get("/api/v1/dispatch/tasks", params={"status": "bogus"})
        assert response.status_code == 422


class TestTaskPruning:
    def test_prunes_old_and_over_cap_terminal_tasks(
        self, preserved_tasks, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 9, 30, tzinfo=UTC)
        monkeypatch.setattr(store, "task_retention_per_type", 3)
        monkeypatch.setattr(store, "task_retention_days", 30)
        store.tasks = [
            # 同类型 5 条终态：超出每类保留 3 条的 2 条最旧记录被裁。
            _task("T-takeout-1", "completed", now.isoformat()),
            _task("T-takeout-2", "completed", (now - timedelta(days=1)).isoformat()),
            _task("T-takeout-3", "failed", (now - timedelta(days=2)).isoformat()),
            _task("T-takeout-4", "completed", (now - timedelta(days=3)).isoformat()),
            _task("T-takeout-5", "cancelled", (now - timedelta(days=4)).isoformat()),
            # 40 天前的终态：超过 30 天保留期。
            _task("T-return-old", "completed", (now - timedelta(days=40)).isoformat(), "return"),
            # 活动状态与待人工确认：再旧也不裁。
            _task("T-running-old", "running", (now - timedelta(days=90)).isoformat(), "return"),
            _task(
                "T-recovery-old",
                "recovery_required",
                (now - timedelta(days=90)).isoformat(),
                "return",
            ),
            # 无法解析的历史显示时间：保留。
            _task("T-legacy", "completed", "刚刚", "charge"),
        ]

        removed = store._prune_tasks(now=now)

        assert removed == 3
        remaining = {task.id for task in store.tasks}
        assert remaining == {
            "T-takeout-1",
            "T-takeout-2",
            "T-takeout-3",
            "T-running-old",
            "T-recovery-old",
            "T-legacy",
        }

    def test_prune_is_noop_when_within_retention(
        self, preserved_tasks, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        monkeypatch.setattr(store, "task_retention_per_type", 200)
        monkeypatch.setattr(store, "task_retention_days", 30)
        store.tasks = [
            _task("T-1", "completed", now.isoformat()),
            _task("T-2", "failed", (now - timedelta(days=29)).isoformat()),
        ]
        assert store._prune_tasks(now=now) == 0
        assert {task.id for task in store.tasks} == {"T-1", "T-2"}
