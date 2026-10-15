import io
import json
from base64 import b64encode
from urllib import error as urllib_error
from urllib import request as urllib_request

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

YAML_DATA = b"""image: agent_map.pgm
mode: trinary
resolution: 0.050
origin: [-1.0, -2.0, 0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
"""
PGM_DATA = b"P5\n2 2\n255\n\x00\xfe\xcd\xfe"


def auth_headers() -> dict[str, str]:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['token']}"}


class FakeAgentResponse:
    def __init__(self, body: bytes, status_code: int = 200) -> None:
        self.status = status_code
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeAgentResponse":
        return self

    def __exit__(self, *_: object) -> bool:
        return False


def install_agent(monkeypatch, handler) -> None:
    def fake_urlopen(request, timeout=0):
        return handler(request)

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)


def save_payload(map_id: str, marks: list[dict] | None = None) -> bytes:
    payload: dict[str, object] = {
        "ok": True,
        "mapId": map_id,
        "yamlPath": f"/tmp/maps/{map_id}/agent_map.yaml",
        "pgmPath": f"/tmp/maps/{map_id}/agent_map.pgm",
        "yamlB64": b64encode(YAML_DATA).decode(),
        "pgmB64": b64encode(PGM_DATA).decode(),
    }
    if marks is not None:
        payload["marks"] = marks
    return json.dumps(payload).encode("utf-8")


def test_mapping_status_unknown_robot_returns_404(monkeypatch) -> None:
    def handler(_request):
        raise AssertionError("agent must not be called for unknown robots")

    install_agent(monkeypatch, handler)
    response = client.get("/api/v1/robots/LR-NOPE/mapping/status", headers=auth_headers())
    assert response.status_code == 404
    assert response.json()["detail"] == "机器人不存在"


def test_mapping_status_agent_unreachable_returns_502(monkeypatch) -> None:
    def handler(_request):
        raise urllib_error.URLError("connection refused")

    install_agent(monkeypatch, handler)
    response = client.get("/api/v1/robots/LR-01/mapping/status", headers=auth_headers())
    assert response.status_code == 502
    assert response.json()["detail"] == "建图代理不可达"


def test_mapping_agent_timeouts_are_aligned_with_vehicle_durations() -> None:
    from app.api import robots

    assert robots.MAPPING_AGENT_TIMEOUT_SECONDS == 45
    assert robots.MAPPING_AGENT_SAVE_TIMEOUT_SECONDS == 300


def test_mapping_start_uses_the_45s_budget(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=0):
        captured["timeout"] = timeout
        return FakeAgentResponse(b'{"ok": true, "mode": "mapping"}')

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)
    response = client.post("/api/v1/robots/LR-01/mapping/start", headers=auth_headers())
    assert response.status_code == 200
    assert captured["timeout"] == 45


def test_mapping_agent_timeout_returns_504_with_in_progress_hint(monkeypatch) -> None:
    def handler(_request):
        raise TimeoutError("timed out")

    install_agent(monkeypatch, handler)
    response = client.post("/api/v1/robots/LR-01/mapping/stop", headers=auth_headers())
    assert response.status_code == 504
    assert response.json()["detail"] == "切换/保存仍在进行，请稍后刷新状态确认结果"


def test_mapping_agent_wrapped_timeout_reason_returns_504(monkeypatch) -> None:
    def handler(_request):
        raise urllib_error.URLError(TimeoutError("timed out"))

    install_agent(monkeypatch, handler)
    response = client.get("/api/v1/robots/LR-01/mapping/status", headers=auth_headers())
    assert response.status_code == 504
    assert response.json()["detail"] == "切换/保存仍在进行，请稍后刷新状态确认结果"


def test_mapping_jog_forwards_velocity_to_agent(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def handler(request):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeAgentResponse(b'{"ok": true}')

    install_agent(monkeypatch, handler)
    response = client.post(
        "/api/v1/robots/LR-01/mapping/jog",
        headers=auth_headers(),
        json={"linear": 0.2, "angular": -0.5},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert captured["url"] == "http://127.0.0.1:18090/mapping/jog"
    assert captured["method"] == "POST"
    assert captured["body"] == {"linear": 0.2, "angular": -0.5}


def test_mapping_jog_agent_conflict_is_passed_through(monkeypatch) -> None:
    def handler(request):
        raise urllib_error.HTTPError(
            request.full_url,
            409,
            "Conflict",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"detail": "当前不是建图模式"}).encode("utf-8")),
        )

    install_agent(monkeypatch, handler)
    response = client.post(
        "/api/v1/robots/LR-01/mapping/jog",
        headers=auth_headers(),
        json={"linear": 0.0, "angular": 0.0},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "当前不是建图模式"


def test_mapping_save_imports_agent_map_into_library(monkeypatch, tmp_path) -> None:
    from app.store import store

    map_id = "MAP-0123456789AB"
    monkeypatch.setattr(store, "map_storage_dir", tmp_path / "maps")
    import_calls: list[str | None] = []
    original_import_map = store.import_map

    def spy_import_map(request, user):
        import_calls.append(request.id)
        return original_import_map(request, user)

    monkeypatch.setattr(store, "import_map", spy_import_map)

    def handler(request):
        assert request.full_url == "http://127.0.0.1:18090/mapping/save"
        return FakeAgentResponse(save_payload(map_id))

    install_agent(monkeypatch, handler)
    try:
        response = client.post(
            "/api/v1/robots/LR-01/mapping/save",
            headers=auth_headers(),
            json={},
        )
        assert response.status_code == 200
        map_version = response.json()
        assert map_version["id"] == map_id
        assert map_version["name"] == f"手动建图 {map_id}"
        assert map_version["width"] == 2
        assert map_version["height"] == 2
        assert import_calls == [map_id]
        assert any(item.id == map_id for item in store.map_versions)
    finally:
        store.map_versions = [item for item in store.map_versions if item.id != map_id]
        store.map_annotations.pop(map_id, None)


def test_mapping_save_conflict_keeps_agent_files_hint(monkeypatch, tmp_path) -> None:
    from app.store import store

    map_id = "MAP-ABCDEF012345"
    monkeypatch.setattr(store, "map_storage_dir", tmp_path / "maps")

    def handler(_request):
        return FakeAgentResponse(save_payload(map_id))

    install_agent(monkeypatch, handler)
    headers = auth_headers()
    try:
        first = client.post("/api/v1/robots/LR-01/mapping/save", headers=headers, json={"name": "一层手动建图"})
        assert first.status_code == 200
        second = client.post("/api/v1/robots/LR-01/mapping/save", headers=headers, json={})
        assert second.status_code == 409
        detail = second.json()["detail"]
        assert "已存在" in detail
        assert f"/tmp/maps/{map_id}/agent_map.yaml" in detail
    finally:
        store.map_versions = [item for item in store.map_versions if item.id != map_id]
        store.map_annotations.pop(map_id, None)


def test_mapping_pose_forwards_to_agent(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def handler(request):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        return FakeAgentResponse(b'{"ok": true, "x": 1.25, "y": -0.5, "yaw": 0.785}')

    install_agent(monkeypatch, handler)
    response = client.get("/api/v1/robots/LR-01/mapping/pose", headers=auth_headers())
    assert response.status_code == 200
    assert response.json() == {"ok": True, "x": 1.25, "y": -0.5, "yaw": 0.785}
    assert captured["url"] == "http://127.0.0.1:18090/mapping/pose"
    assert captured["method"] == "GET"


def test_mapping_marks_list_forwarded_from_agent(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def handler(request):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        return FakeAgentResponse(
            json.dumps({"marks": [{"x": 1.0, "y": 2.0, "yaw": 0.5, "label": "三层左架", "at": 1720000000.0}]}).encode(
                "utf-8"
            )
        )

    install_agent(monkeypatch, handler)
    response = client.get("/api/v1/robots/LR-01/mapping/marks", headers=auth_headers())
    assert response.status_code == 200
    assert response.json() == {"marks": [{"x": 1.0, "y": 2.0, "yaw": 0.5, "label": "三层左架", "at": 1720000000.0}]}
    assert captured["url"] == "http://127.0.0.1:18090/mapping/marks"
    assert captured["method"] == "GET"


def test_mapping_mark_create_forwards_label_to_agent(monkeypatch) -> None:
    captured: dict[str, object] = {}
    mark = {"x": 1.0, "y": 2.0, "yaw": 0.0, "label": "三层左架", "at": 1720000000.0}

    def handler(request):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeAgentResponse(json.dumps({"mark": mark, "count": 3}).encode("utf-8"), status_code=201)

    install_agent(monkeypatch, handler)
    response = client.post(
        "/api/v1/robots/LR-01/mapping/mark",
        headers=auth_headers(),
        json={"label": "三层左架"},
    )
    assert response.status_code == 201
    assert response.json() == {"mark": mark, "count": 3}
    assert captured["url"] == "http://127.0.0.1:18090/mapping/mark"
    assert captured["method"] == "POST"
    assert captured["body"] == {"label": "三层左架"}


def test_mapping_mark_create_without_label_forwards_null(monkeypatch) -> None:
    captured: dict[str, object] = {}
    mark = {"x": 1.0, "y": 2.0, "yaw": 0.0, "label": "", "at": 1720000000.0}

    def handler(request):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeAgentResponse(json.dumps({"mark": mark, "count": 1}).encode("utf-8"), status_code=201)

    install_agent(monkeypatch, handler)
    response = client.post("/api/v1/robots/LR-01/mapping/mark", headers=auth_headers(), json={})
    assert response.status_code == 201
    assert captured["body"] == {"label": None}


def test_mapping_marks_clear_forwarded_to_agent(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def handler(request):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeAgentResponse(b'{"ok": true, "cleared": 4}')

    install_agent(monkeypatch, handler)
    response = client.post("/api/v1/robots/LR-01/mapping/marks/clear", headers=auth_headers())
    assert response.status_code == 200
    assert response.json() == {"ok": True, "cleared": 4}
    assert captured["url"] == "http://127.0.0.1:18090/mapping/marks/clear"
    assert captured["method"] == "POST"
    assert captured["body"] == {}


def test_mapping_save_imports_marks_as_shelf_annotations(monkeypatch, tmp_path) -> None:
    from app.store import store

    map_id = "MAP-112233445566"
    monkeypatch.setattr(store, "map_storage_dir", tmp_path / "maps")
    marks = [
        {"x": -0.95, "y": -1.95, "yaw": 1.5, "label": " 三层左架 ", "at": 1720000000.0},
        {"x": -1.0, "y": -2.0, "yaw": 7.0, "label": "", "at": 1720000001.0},
        {"x": 5.0, "y": 0.0, "yaw": 0.0, "label": "越界点", "at": 1720000002.0},
    ]

    def handler(request):
        assert request.full_url == "http://127.0.0.1:18090/mapping/save"
        return FakeAgentResponse(save_payload(map_id, marks))

    install_agent(monkeypatch, handler)
    try:
        response = client.post("/api/v1/robots/LR-01/mapping/save", headers=auth_headers(), json={})
        assert response.status_code == 200
        assert response.json()["id"] == map_id
        assert response.json()["importedMarks"] == 2
        assert response.json()["skippedMarks"] == 1
        assert response.json()["alreadySaved"] is False
        annotations = [item for item in store.list_map_annotations(map_id) if item.kind == "shelf"]
        assert len(annotations) == 2
        first, second = annotations
        assert (first.pixelX, first.pixelY) == (1, 0)
        assert first.mapX == pytest.approx(-0.95)
        assert first.mapY == pytest.approx(-1.95)
        assert first.label == "三层左架"
        assert first.yaw == 1.5
        assert (second.pixelX, second.pixelY) == (0, 1)
        assert second.label == "书架点 2"
        assert second.yaw == 6.284
    finally:
        store.map_versions = [item for item in store.map_versions if item.id != map_id]
        store.map_annotations.pop(map_id, None)


def test_mapping_save_uses_the_300s_budget(monkeypatch, tmp_path) -> None:
    from app.store import store

    map_id = "MAP-SAVEBUDGET01"
    monkeypatch.setattr(store, "map_storage_dir", tmp_path / "maps")
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=0):
        captured["timeout"] = timeout
        return FakeAgentResponse(save_payload(map_id))

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)
    try:
        response = client.post("/api/v1/robots/LR-01/mapping/save", headers=auth_headers(), json={})
        assert response.status_code == 200
        assert captured["timeout"] == 300
    finally:
        store.map_versions = [item for item in store.map_versions if item.id != map_id]
        store.map_annotations.pop(map_id, None)


def test_mapping_save_repeated_with_already_saved_returns_first_map(monkeypatch, tmp_path) -> None:
    """车端保存幂等：重复保存返回首存 mapId 与 alreadySaved，本站直接返回首存记录。"""

    from app.store import store

    map_id = "MAP-IDEMPOTENT01"
    monkeypatch.setattr(store, "map_storage_dir", tmp_path / "maps")

    def fresh_handler(_request):
        return FakeAgentResponse(save_payload(map_id))

    install_agent(monkeypatch, fresh_handler)
    try:
        first = client.post("/api/v1/robots/LR-01/mapping/save", headers=auth_headers(), json={})
        assert first.status_code == 200
        assert first.json()["alreadySaved"] is False

        def repeat_handler(_request):
            payload = json.loads(save_payload(map_id).decode("utf-8"))
            payload["alreadySaved"] = True
            return FakeAgentResponse(json.dumps(payload).encode("utf-8"))

        install_agent(monkeypatch, repeat_handler)
        second = client.post("/api/v1/robots/LR-01/mapping/save", headers=auth_headers(), json={})
        assert second.status_code == 200
        body = second.json()
        assert body["id"] == map_id
        assert body["alreadySaved"] is True
        assert body["importedMarks"] == 0
        assert body["skippedMarks"] == 0
    finally:
        store.map_versions = [item for item in store.map_versions if item.id != map_id]
        store.map_annotations.pop(map_id, None)


def test_mapping_mark_delete_forwarded_to_agent(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def handler(request):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        return FakeAgentResponse(b'{"ok": true, "remaining": 2}')

    install_agent(monkeypatch, handler)
    response = client.delete("/api/v1/robots/LR-01/mapping/marks/1", headers=auth_headers())
    assert response.status_code == 200
    assert response.json() == {"ok": True, "remaining": 2}
    assert captured["url"] == "http://127.0.0.1:18090/mapping/marks/1"
    assert captured["method"] == "DELETE"


def test_mapping_mark_delete_out_of_range_returns_agent_404(monkeypatch) -> None:
    def handler(request):
        raise urllib_error.HTTPError(
            request.full_url,
            404,
            "Not Found",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"detail": "标注序号越界"}).encode("utf-8")),
        )

    install_agent(monkeypatch, handler)
    response = client.delete("/api/v1/robots/LR-01/mapping/marks/9", headers=auth_headers())
    assert response.status_code == 404
    assert response.json()["detail"] == "标注序号越界"


def test_mapping_agent_english_409_detail_is_translated(monkeypatch) -> None:
    def handler(request):
        raise urllib_error.HTTPError(
            request.full_url,
            409,
            "Conflict",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"detail": "mapId is not enabled on this robot"}).encode("utf-8")),
        )

    install_agent(monkeypatch, handler)
    response = client.post(
        "/api/v1/robots/LR-01/mapping/jog",
        headers=auth_headers(),
        json={"linear": 0.1, "angular": 0.0},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "该地图未注册到车端，请先在车端注册该地图后重试"


def test_mapping_loc_overlay_forwarded_from_agent(monkeypatch) -> None:
    captured: dict[str, object] = {}
    overlay = {
        "ok": True,
        "capturedAt": 1720000000,
        "frameId": "map",
        "pose": {"x": 1.0, "y": 2.0, "yaw": 0.5},
        "points": [[-0.95, -1.95], [0.0, 0.0]],
    }

    def handler(request):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        return FakeAgentResponse(json.dumps(overlay).encode("utf-8"))

    install_agent(monkeypatch, handler)
    response = client.get("/api/v1/robots/LR-01/mapping/loc-overlay", headers=auth_headers())
    assert response.status_code == 200
    assert response.json() == overlay
    assert captured["url"] == "http://127.0.0.1:18090/mapping/loc-overlay"
    assert captured["method"] == "GET"


def test_mapping_loc_overlay_agent_503_is_passed_through(monkeypatch) -> None:
    def handler(request):
        raise urllib_error.HTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"ok": False, "error": "定位未就绪"}).encode("utf-8")),
        )

    install_agent(monkeypatch, handler)
    response = client.get("/api/v1/robots/LR-01/mapping/loc-overlay", headers=auth_headers())
    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "定位未就绪"}
