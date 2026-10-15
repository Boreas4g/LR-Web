"""阶段 3 点位标注：删除拦截放宽、巡航点 fixture 隔离与预览、重名警告。"""

from base64 import b64encode
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import DispatchTask, MapOrigin, MapVersion, User
from app.store import InMemoryStore, store

client = TestClient(app)

YAML_DATA = b"""image: ann_map.pgm
mode: trinary
resolution: 0.050
origin: [-1.0, -2.0, 0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
"""
PGM_DATA = b"P5\n10 10\n255\n" + b"\x00" * 100

# 地图是 10×10 px、0.05 m/px、原点 (-1, -2)：有效范围 x∈[-1,-0.55]，y∈[-2,-1.55]。
CRUISE_YAML = """points:
  lobby:
    frame_id: map
    x: -0.8
    y: -1.8
    yaw: 1.57
  far_away:
    frame_id: map
    x: 100.0
    y: 100.0
    yaw: 0.0
"""


def _headers() -> dict[str, str]:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _import_map(headers: dict[str, str], name: str) -> str:
    response = client.post(
        "/api/v1/library/maps/import",
        headers=headers,
        json={
            "name": name,
            "yamlName": "ann_map.yaml",
            "yamlContent": b64encode(YAML_DATA).decode(),
            "pgmName": "ann_map.pgm",
            "pgmContent": b64encode(PGM_DATA).decode(),
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def _create_annotation(headers: dict[str, str], map_id: str, label: str = "A-01 书架") -> dict:
    response = client.post(
        f"/api/v1/library/maps/{map_id}/annotations",
        headers=headers,
        json={"kind": "shelf", "label": label, "pixelX": 1, "pixelY": 1, "yaw": 0.0},
    )
    assert response.status_code == 201
    return response.json()


def _task_referencing(map_id: str, annotation_id: str, status: str) -> DispatchTask:
    return DispatchTask(
        id=f"TSK-TEST-{annotation_id[-6:]}-{status.upper()}",
        type="navigate",
        robotId="LR-01",
        target="测试标注",
        status=status,
        progress=0,
        updatedAt="刚刚",
        mapId=map_id,
        annotationId=annotation_id,
    )


@pytest.fixture
def map_storage(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = tmp_path / "maps"
    storage.mkdir()
    monkeypatch.setattr(store, "map_storage_dir", storage)


@pytest.mark.usefixtures("map_storage")
class TestDeleteAnnotation:
    @pytest.mark.parametrize("status", ["queued", "running", "paused"])
    def test_active_task_reference_returns_409(self, status: str) -> None:
        headers = _headers()
        map_id = _import_map(headers, f"删除拦截-{status}")
        annotation = _create_annotation(headers, map_id)
        task = _task_referencing(map_id, annotation["id"], status)
        store.tasks.insert(0, task)
        try:
            deleted = client.delete(
                f"/api/v1/library/maps/{map_id}/annotations/{annotation['id']}", headers=headers
            )
            assert deleted.status_code == 409
            assert task.id in deleted.json()["detail"]
            remaining = client.get(f"/api/v1/library/maps/{map_id}/annotations", headers=headers).json()
            assert any(item["id"] == annotation["id"] for item in remaining)
        finally:
            store.tasks.remove(task)

    @pytest.mark.parametrize("status", ["completed", "cancelled", "failed", "recovery_required"])
    def test_terminal_task_reference_does_not_block(self, status: str) -> None:
        headers = _headers()
        map_id = _import_map(headers, f"删除放行-{status}")
        annotation = _create_annotation(headers, map_id)
        task = _task_referencing(map_id, annotation["id"], status)
        store.tasks.insert(0, task)
        try:
            deleted = client.delete(
                f"/api/v1/library/maps/{map_id}/annotations/{annotation['id']}", headers=headers
            )
            assert deleted.status_code == 204
            remaining = client.get(f"/api/v1/library/maps/{map_id}/annotations", headers=headers).json()
            assert all(item["id"] != annotation["id"] for item in remaining)
        finally:
            store.tasks.remove(task)

    def test_route_annotation_reference_blocks(self) -> None:
        headers = _headers()
        map_id = _import_map(headers, "删除拦截-路径点")
        annotation = _create_annotation(headers, map_id)
        task = _task_referencing(map_id, "ANN-OTHER", "running").model_copy(
            update={"annotationId": None, "routeAnnotationIds": [annotation["id"]]}
        )
        store.tasks.insert(0, task)
        try:
            deleted = client.delete(
                f"/api/v1/library/maps/{map_id}/annotations/{annotation['id']}", headers=headers
            )
            assert deleted.status_code == 409
        finally:
            store.tasks.remove(task)


@pytest.mark.usefixtures("map_storage")
class TestCruisePointIsolation:
    def test_import_does_not_inject_dev_points_by_default(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        points_file = tmp_path / "points.yaml"
        points_file.write_text(CRUISE_YAML, encoding="utf-8")
        monkeypatch.setattr(store, "cruise_points_file", points_file)
        monkeypatch.setattr(store, "cruise_points_enabled", False)
        headers = _headers()
        map_id = _import_map(headers, "隔离-默认关闭")
        annotations = client.get(f"/api/v1/library/maps/{map_id}/annotations", headers=headers).json()
        assert annotations == []

    def test_import_injects_points_only_when_enabled(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        points_file = tmp_path / "points.yaml"
        points_file.write_text(CRUISE_YAML, encoding="utf-8")
        monkeypatch.setattr(store, "cruise_points_file", points_file)
        monkeypatch.setattr(store, "cruise_points_enabled", True)
        headers = _headers()
        map_id = _import_map(headers, "隔离-开启注入")
        annotations = client.get(f"/api/v1/library/maps/{map_id}/annotations", headers=headers).json()
        # far_away 超出 10x10 px（0.5 m 见方）地图范围，只注入 lobby。
        assert [item["label"] for item in annotations] == ["lobby"]
        assert annotations[0]["kind"] == "cruise"

    def test_enabled_but_missing_file_injects_nothing(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(store, "cruise_points_file", tmp_path / "absent.yaml")
        monkeypatch.setattr(store, "cruise_points_enabled", True)
        headers = _headers()
        map_id = _import_map(headers, "隔离-文件缺失")
        annotations = client.get(f"/api/v1/library/maps/{map_id}/annotations", headers=headers).json()
        assert annotations == []

    def test_sync_and_preview_require_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(store, "cruise_points_enabled", False)
        headers = _headers()
        map_id = _import_map(headers, "隔离-同步关闭")
        preview = client.get(
            f"/api/v1/library/maps/{map_id}/annotations/sync-cruise/preview", headers=headers
        )
        assert preview.status_code == 422
        synced = client.post(f"/api/v1/library/maps/{map_id}/annotations/sync-cruise", headers=headers)
        assert synced.status_code == 422

    def test_preview_reports_create_and_skip(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        points_file = tmp_path / "points.yaml"
        points_file.write_text(CRUISE_YAML, encoding="utf-8")
        monkeypatch.setattr(store, "cruise_points_file", points_file)
        monkeypatch.setattr(store, "cruise_points_enabled", True)
        headers = _headers()
        map_id = _import_map(headers, "预览-差异")
        # 首次导入已注入 lobby；预览应报告 lobby 跳过（已存在）、far_away 跳过（出界）。
        preview = client.get(
            f"/api/v1/library/maps/{map_id}/annotations/sync-cruise/preview", headers=headers
        )
        assert preview.status_code == 200
        body = preview.json()
        assert body["file"].endswith("points.yaml")
        by_name = {item["name"]: item for item in body["items"]}
        assert by_name["lobby"]["action"] == "skip"
        assert "已存在" in by_name["lobby"]["reason"]
        assert by_name["far_away"]["action"] == "skip"
        assert "超出" in by_name["far_away"]["reason"]
        assert by_name["far_away"]["pixelX"] is None

    def test_preview_marks_new_points_as_create(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        points_file = tmp_path / "points.yaml"
        points_file.write_text(CRUISE_YAML, encoding="utf-8")
        monkeypatch.setattr(store, "cruise_points_file", points_file)
        # 关闭开关导入（不注入），再打开开关预览：lobby 应为 create。
        monkeypatch.setattr(store, "cruise_points_enabled", False)
        headers = _headers()
        map_id = _import_map(headers, "预览-新增")
        monkeypatch.setattr(store, "cruise_points_enabled", True)
        preview = client.get(
            f"/api/v1/library/maps/{map_id}/annotations/sync-cruise/preview", headers=headers
        )
        by_name = {item["name"]: item for item in preview.json()["items"]}
        assert by_name["lobby"]["action"] == "create"
        # (-0.8, -1.8) → pixelX = round((-0.8+1)/0.05) = 4；pixelY = round(9 - (-1.8+2)/0.05) = 5。
        assert by_name["lobby"]["pixelX"] == 4
        assert by_name["lobby"]["pixelY"] == 5
        synced = client.post(f"/api/v1/library/maps/{map_id}/annotations/sync-cruise", headers=headers)
        assert synced.status_code == 200
        assert [item["label"] for item in synced.json()] == ["lobby"]


@pytest.mark.usefixtures("map_storage")
class TestCreateAnnotationLabelWarning:
    def test_duplicate_label_warns_without_blocking(self) -> None:
        headers = _headers()
        map_id = _import_map(headers, "重名警告")
        first = _create_annotation(headers, map_id)
        assert first["labelWarning"] is None
        second = _create_annotation(headers, map_id)
        assert second["labelWarning"] is not None
        assert first["label"] in second["labelWarning"]
        annotations = client.get(f"/api/v1/library/maps/{map_id}/annotations", headers=headers).json()
        labels = [item["label"] for item in annotations if item["id"] in {first["id"], second["id"]}]
        assert labels == [first["label"], first["label"]]
        # 列表响应不携带临时警告字段。
        assert all("labelWarning" not in item for item in annotations)

    def test_distinct_label_has_no_warning(self) -> None:
        headers = _headers()
        map_id = _import_map(headers, "重名警告-无冲突")
        _create_annotation(headers, map_id, label="A-01 书架")
        other = _create_annotation(headers, map_id, label="A-02 书架")
        assert other["labelWarning"] is None


def _map_version() -> MapVersion:
    return MapVersion(
        id="MAP-PREVIEW-STORE",
        name="预览存储层",
        version="v1",
        yamlFile="test.yaml",
        pgmFile="test.pgm",
        width=100,
        height=100,
        resolution=0.1,
        origin=MapOrigin(x=-1.0, y=-2.0, yaw=0.0),
        mode="trinary",
        negate=False,
        occupiedThreshold=0.65,
        freeThreshold=0.196,
        status="draft",
        createdAt=datetime.now(UTC),
        createdBy="admin",
    )


class TestStoreLevelCruisePreview:
    def test_preview_skips_when_disabled(self, tmp_path) -> None:
        points_file = tmp_path / "points.yaml"
        points_file.write_text(CRUISE_YAML, encoding="utf-8")
        store_instance = InMemoryStore()
        store_instance.cruise_points_file = points_file
        store_instance.map_versions.append(_map_version())
        with pytest.raises(ValueError, match="未启用"):
            store_instance.preview_cruise_annotations("MAP-PREVIEW-STORE")

    def test_add_cruise_annotations_noop_when_disabled(self, tmp_path) -> None:
        points_file = tmp_path / "points.yaml"
        points_file.write_text(CRUISE_YAML, encoding="utf-8")
        store_instance = InMemoryStore()
        store_instance.cruise_points_file = points_file
        created = store_instance._add_cruise_annotations(
            _map_version(), User(username="admin", name="管理员", role="admin")
        )
        assert created == []
        assert store_instance.map_annotations.get("MAP-PREVIEW-STORE") is None
