"""Annotation navigation orchestration tests: navigate, then low-speed fine-tune."""

from __future__ import annotations

from datetime import UTC, datetime
import time

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import MapAnnotation, RobotPose

client = TestClient(app)

MAP_ID = "MAP-ANN-NAV"
ANNOTATION_ID = "ANN-NAV-TEST"


def _store():
    from app.store import store

    return store


def _headers() -> dict[str, str]:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _annotation() -> MapAnnotation:
    return MapAnnotation(
        id=ANNOTATION_ID,
        mapId=MAP_ID,
        kind="shelf",
        label="测试书架点",
        pixelX=10,
        pixelY=10,
        mapX=1.5,
        mapY=-2.0,
        yaw=1.57,
        createdAt=datetime.now(UTC),
        createdBy="admin",
    )


def _prepare_robot():
    from app.store import store

    index = next(index for index, item in enumerate(store.robots) if item.id == "LR-01")
    original = store.robots[index]
    store.robots[index] = original.model_copy(
        update={
            "status": "idle",
            "mapId": MAP_ID,
            "localizationStatus": "localized",
            "pose": RobotPose(frameId="map", x=0.0, y=0.0, yaw=0.0, capturedAt=datetime.now(UTC)),
        }
    )
    return index, original


def _flow_parent() -> dict[str, object] | None:
    task = next(
        (
            item
            for item in _store().tasks
            if item.type == "navigate" and item.workflowDetails.get("annotationNavigation") is True
        ),
        None,
    )
    return task.model_dump() if task else None


def _stage_task(task_type: str) -> dict[str, object] | None:
    task = next(
        (
            item
            for item in _store().tasks
            if item.type == task_type and not item.workflowDetails.get("annotationNavigation")
        ),
        None,
    )
    return task.model_dump() if task else None


def _wait_for(predicate, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met in time")


def _cleanup(index: int, original_robot, task_ids: set[str]) -> None:
    store = _store()
    store.robots[index] = original_robot
    store.map_annotations[MAP_ID] = [
        item for item in store.map_annotations.get(MAP_ID, []) if item.id != ANNOTATION_ID
    ]
    store.tasks = [item for item in store.tasks if item.id not in task_ids]


def test_annotation_navigation_runs_navigate_then_fine_tune() -> None:
    index, original_robot = _prepare_robot()
    store = _store()
    store.map_annotations.setdefault(MAP_ID, []).append(_annotation())
    created_ids: set[str] = set()
    try:
        response = client.post(
            "/api/v1/robots/LR-01/annotations/navigate",
            json={"mapId": MAP_ID, "annotationId": ANNOTATION_ID},
            headers=_headers(),
        )
        assert response.status_code == 202
        parent = response.json()
        created_ids.add(parent["id"])
        assert parent["type"] == "navigate"
        assert parent["workflowDetails"]["annotationNavigation"] is True

        # Stage 1: navigate command registered; finish it via the gateway event.
        _wait_for(lambda: _flow_parent() is not None and _flow_parent()["workflowDetails"].get("navigationTaskId"))
        nav_stage_id = _flow_parent()["workflowDetails"]["navigationTaskId"]
        created_ids.add(nav_stage_id)
        nav_stage = next(item for item in store.tasks if item.id == nav_stage_id)
        assert nav_stage.type == "navigate"
        store.handle_gateway_event(
            {
                "event": "navigation_succeeded",
                "robotId": "LR-01",
                "commandId": nav_stage_id.replace("TSK-", "CMD-", 1),
            }
        )

        # Stage 2: the flow must dispatch the low-speed fine-tune next.
        _wait_for(lambda: _flow_parent() is not None and _flow_parent()["workflowDetails"].get("fineTuneTaskId"))
        fine_tune_stage_id = _flow_parent()["workflowDetails"]["fineTuneTaskId"]
        created_ids.add(fine_tune_stage_id)
        fine_tune_stage = next(item for item in store.tasks if item.id == fine_tune_stage_id)
        assert fine_tune_stage.type == "fine_tune"
        store.handle_gateway_event(
            {
                "event": "command_completed",
                "robotId": "LR-01",
                "commandId": fine_tune_stage_id.replace("TSK-", "CMD-", 1),
            }
        )

        _wait_for(
            lambda: _flow_parent() is not None and _flow_parent()["status"] == "completed"
        )
        parent = _flow_parent()
        assert parent["progress"] == 100
        assert parent["workflowMessage"] == "已到位（导航+低速精调）"
    finally:
        _cleanup(index, original_robot, created_ids)


def test_annotation_navigation_skip_fine_tune_completes_after_navigate() -> None:
    """压测回原点场景：skipFineTune=true 时导航成功即完成，不再派发低速精调。"""
    index, original_robot = _prepare_robot()
    store = _store()
    store.map_annotations.setdefault(MAP_ID, []).append(_annotation())
    fine_tune_ids_before = {item.id for item in store.tasks if item.type == "fine_tune"}
    created_ids: set[str] = set()
    try:
        response = client.post(
            "/api/v1/robots/LR-01/annotations/navigate",
            json={"mapId": MAP_ID, "annotationId": ANNOTATION_ID, "skipFineTune": True},
            headers=_headers(),
        )
        assert response.status_code == 202
        parent = response.json()
        created_ids.add(parent["id"])
        assert parent["workflowDetails"]["skipFineTune"] is True

        # 导航阶段照常派发；网关回报成功。
        _wait_for(lambda: _flow_parent() is not None and _flow_parent()["workflowDetails"].get("navigationTaskId"))
        nav_stage_id = _flow_parent()["workflowDetails"]["navigationTaskId"]
        created_ids.add(nav_stage_id)
        store.handle_gateway_event(
            {
                "event": "navigation_succeeded",
                "robotId": "LR-01",
                "commandId": nav_stage_id.replace("TSK-", "CMD-", 1),
            }
        )

        _wait_for(lambda: _flow_parent() is not None and _flow_parent()["status"] == "completed")
        parent = _flow_parent()
        assert parent["progress"] == 100
        assert parent["workflowMessage"] == "已到位（导航，未低速精调）"
        assert not parent["workflowDetails"].get("fineTuneTaskId")
        # 全程没有新的 fine_tune 阶段任务。
        fine_tune_ids_after = {item.id for item in store.tasks if item.type == "fine_tune"}
        assert fine_tune_ids_after == fine_tune_ids_before
    finally:
        _cleanup(index, original_robot, created_ids)


def test_annotation_navigation_fails_when_fine_tune_is_rejected() -> None:
    index, original_robot = _prepare_robot()
    store = _store()
    store.map_annotations.setdefault(MAP_ID, []).append(_annotation())
    created_ids: set[str] = set()
    try:
        response = client.post(
            "/api/v1/robots/LR-01/annotations/navigate",
            json={"mapId": MAP_ID, "annotationId": ANNOTATION_ID},
            headers=_headers(),
        )
        assert response.status_code == 202
        created_ids.add(response.json()["id"])

        _wait_for(lambda: _flow_parent() is not None and _flow_parent()["workflowDetails"].get("navigationTaskId"))
        nav_stage_id = _flow_parent()["workflowDetails"]["navigationTaskId"]
        created_ids.add(nav_stage_id)
        store.handle_gateway_event(
            {
                "event": "navigation_succeeded",
                "robotId": "LR-01",
                "commandId": nav_stage_id.replace("TSK-", "CMD-", 1),
            }
        )

        _wait_for(lambda: _flow_parent() is not None and _flow_parent()["workflowDetails"].get("fineTuneTaskId"))
        fine_tune_stage_id = _flow_parent()["workflowDetails"]["fineTuneTaskId"]
        created_ids.add(fine_tune_stage_id)
        store.handle_gateway_event(
            {
                "event": "command_rejected",
                "robotId": "LR-01",
                "commandId": fine_tune_stage_id.replace("TSK-", "CMD-", 1),
                "details": {"reason": "车端正忙"},
            }
        )

        _wait_for(lambda: _flow_parent() is not None and _flow_parent()["status"] == "failed")
        parent = _flow_parent()
        assert "低速精调未成功" in (parent["failureReason"] or "")
    finally:
        _cleanup(index, original_robot, created_ids)


def test_annotation_navigation_cancel_stops_the_flow() -> None:
    index, original_robot = _prepare_robot()
    store = _store()
    store.map_annotations.setdefault(MAP_ID, []).append(_annotation())
    created_ids: set[str] = set()
    try:
        response = client.post(
            "/api/v1/robots/LR-01/annotations/navigate",
            json={"mapId": MAP_ID, "annotationId": ANNOTATION_ID},
            headers=_headers(),
        )
        assert response.status_code == 202
        task_id = response.json()["id"]
        created_ids.add(task_id)

        _wait_for(lambda: _flow_parent() is not None and _flow_parent()["workflowDetails"].get("navigationTaskId"))
        nav_stage_id = _flow_parent()["workflowDetails"]["navigationTaskId"]
        created_ids.add(nav_stage_id)

        cancelled = client.post(
            f"/api/v1/robots/LR-01/annotations/navigate/{task_id}/cancel",
            headers=_headers(),
        )
        assert cancelled.status_code == 200

        # The flow's stage wait issues a targeted vehicle cancel for the
        # in-flight navigation command before marking the parent cancelled.
        _wait_for(
            lambda: any(
                command.command == "cancel"
                and isinstance(command.payload, dict)
                and command.payload.get("taskId") == nav_stage_id
                for command in store.commands.values()
            )
        )
        _wait_for(lambda: _flow_parent() is not None and _flow_parent()["status"] == "cancelled")
    finally:
        _cleanup(index, original_robot, created_ids)


def test_annotation_navigation_guards() -> None:
    index, original_robot = _prepare_robot()
    store = _store()
    headers = _headers()
    try:
        # Annotation does not exist.
        missing = client.post(
            "/api/v1/robots/LR-01/annotations/navigate",
            json={"mapId": MAP_ID, "annotationId": ANNOTATION_ID},
            headers=headers,
        )
        assert missing.status_code == 409
        assert "标注点不存在" in missing.json()["detail"]

        store.map_annotations.setdefault(MAP_ID, []).append(_annotation())

        # Robot on a different map.
        store.robots[index] = store.robots[index].model_copy(update={"mapId": "MAP-OTHER"})
        wrong_map = client.post(
            "/api/v1/robots/LR-01/annotations/navigate",
            json={"mapId": MAP_ID, "annotationId": ANNOTATION_ID},
            headers=headers,
        )
        assert wrong_map.status_code == 409
        assert "不在该标注所在地图上" in wrong_map.json()["detail"]

        # Robot busy.
        store.robots[index] = store.robots[index].model_copy(
            update={"mapId": MAP_ID, "status": "navigating"}
        )
        busy = client.post(
            "/api/v1/robots/LR-01/annotations/navigate",
            json={"mapId": MAP_ID, "annotationId": ANNOTATION_ID},
            headers=headers,
        )
        assert busy.status_code == 409
        assert "不在空闲状态" in busy.json()["detail"]
    finally:
        _cleanup(index, original_robot, set())
