"""Stress-test task template CRUD tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _headers() -> dict[str, str]:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "两取两还日常压测",
        "config": {
            "mapId": "MAP-1",
            "targetAnnotationId": "ANN-SHELF-4",
            "takeouts": [
                {"bookId": "BOOK-A", "destinationSlot": 2},
                {"bookId": "BOOK-B", "destinationSlot": 1},
            ],
            "returns": [
                {"sourceSlot": 1, "referenceBookId": "BOOK-C"},
                {"sourceSlot": 2, "referenceBookId": "BOOK-B"},
            ],
            "liftSpeedLevel": 3,
            "maxRounds": None,
            "stopOnFailure": True,
        },
    }
    payload.update(overrides)
    return payload


def _cleanup(template_ids: list[str]) -> None:
    from app.store import store

    with store._lock:
        for template_id in template_ids:
            store.stress_test_templates.pop(template_id, None)


def test_template_crud_roundtrip() -> None:
    headers = _headers()
    created = client.post("/api/v1/debug/stress-test/templates", headers=headers, json=_payload())
    assert created.status_code == 201
    template = created.json()
    assert template["id"].startswith("STT-")
    assert template["name"] == "两取两还日常压测"
    assert template["config"]["takeouts"][0]["destinationSlot"] == 2
    assert template["config"]["returns"][1]["referenceBookId"] == "BOOK-B"
    assert template["config"]["maxRounds"] is None

    try:
        listed = client.get("/api/v1/debug/stress-test/templates", headers=headers)
        assert listed.status_code == 200
        assert any(item["id"] == template["id"] for item in listed.json())

        updated = client.put(
            f"/api/v1/debug/stress-test/templates/{template['id']}",
            headers=headers,
            json=_payload(name="两取两还（改）", config={**_payload()["config"], "maxRounds": 10}),
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "两取两还（改）"
        assert updated.json()["config"]["maxRounds"] == 10
        assert updated.json()["createdAt"] == template["createdAt"]

        deleted = client.delete(
            f"/api/v1/debug/stress-test/templates/{template['id']}", headers=headers
        )
        assert deleted.status_code == 204
        listed = client.get("/api/v1/debug/stress-test/templates", headers=headers)
        assert all(item["id"] != template["id"] for item in listed.json())
    finally:
        _cleanup([template["id"]])


def test_template_name_conflict() -> None:
    headers = _headers()
    first = client.post("/api/v1/debug/stress-test/templates", headers=headers, json=_payload())
    assert first.status_code == 201
    second = client.post("/api/v1/debug/stress-test/templates", headers=headers, json=_payload())
    assert second.status_code == 409
    other = client.post(
        "/api/v1/debug/stress-test/templates",
        headers=headers,
        json=_payload(name="另一套压测"),
    )
    assert other.status_code == 201
    created_ids = [first.json()["id"], other.json()["id"]]
    try:
        # 改成自己的现名放行。
        keep = client.put(
            f"/api/v1/debug/stress-test/templates/{first.json()['id']}",
            headers=headers,
            json=_payload(),
        )
        assert keep.status_code == 200
        # 改名撞上另一个模板则 409。
        conflict = client.put(
            f"/api/v1/debug/stress-test/templates/{other.json()['id']}",
            headers=headers,
            json=_payload(),
        )
        assert conflict.status_code == 409
    finally:
        _cleanup(created_ids)


def test_template_config_validation() -> None:
    headers = _headers()
    config = _payload()["config"]

    def post(config_override: dict[str, object]) -> int:
        response = client.post(
            "/api/v1/debug/stress-test/templates",
            headers=headers,
            json=_payload(config={**config, **config_override}),
        )
        return response.status_code

    # 取书槽位重复。
    assert (
        post(
            {
                "takeouts": [
                    {"bookId": "BOOK-A", "destinationSlot": 1},
                    {"bookId": "BOOK-B", "destinationSlot": 1},
                ]
            }
        )
        == 422
    )
    # 还书槽位没有对应取书动作。
    assert post({"returns": [{"sourceSlot": 5, "referenceBookId": "BOOK-C"}]}) == 422
    # 还书槽位重复。
    assert (
        post(
            {
                "returns": [
                    {"sourceSlot": 1, "referenceBookId": "BOOK-C"},
                    {"sourceSlot": 1, "referenceBookId": "BOOK-D"},
                ]
            }
        )
        == 422
    )
    # 至少一步取书。
    assert post({"takeouts": []}) == 422
    # 槽位越界。
    assert post({"takeouts": [{"bookId": "BOOK-A", "destinationSlot": 6}]}) == 422
    # 速度档越界。
    assert post({"liftSpeedLevel": 4}) == 422
    # 模板名为空。
    empty_name = client.post(
        "/api/v1/debug/stress-test/templates", headers=headers, json=_payload(name="")
    )
    assert empty_name.status_code == 422


def test_template_missing_records() -> None:
    headers = _headers()
    missing_update = client.put(
        "/api/v1/debug/stress-test/templates/STT-NOPE", headers=headers, json=_payload()
    )
    assert missing_update.status_code == 404
    missing_delete = client.delete("/api/v1/debug/stress-test/templates/STT-NOPE", headers=headers)
    assert missing_delete.status_code == 404


def test_template_config_ignores_legacy_origin_field() -> None:
    """直退收尾后模板不再使用原点：旧模板里的 originAnnotationId 被忽略不报错。"""
    headers = _headers()
    config = {**_payload()["config"], "originAnnotationId": "ANN-LEGACY"}
    created = client.post(
        "/api/v1/debug/stress-test/templates", headers=headers, json=_payload(config=config)
    )
    assert created.status_code == 201
    assert "originAnnotationId" not in created.json()["config"]
    _cleanup([created.json()["id"]])
