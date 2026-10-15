"""Managed-book CRUD tests."""

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
        "name": "深入理解计算机系统",
        "templateId": "LIB-TPL-1",
        "heightMm": 800,
    }
    payload.update(overrides)
    return payload


def _cleanup(book_ids: list[str]) -> None:
    from app.store import store

    with store._lock:
        for book_id in book_ids:
            store.books.pop(book_id, None)


def test_book_crud_roundtrip() -> None:
    headers = _headers()
    created = client.post("/api/v1/books", headers=headers, json=_payload())
    assert created.status_code == 201
    book = created.json()
    assert book["id"].startswith("BOOK-")
    assert book["name"] == "深入理解计算机系统"
    assert book["templateId"] == "LIB-TPL-1"
    assert book["heightMm"] == 800
    assert book["annotationId"] is None
    assert book["preferredSlot"] is None

    try:
        listed = client.get("/api/v1/books", headers=headers)
        assert listed.status_code == 200
        assert any(item["id"] == book["id"] for item in listed.json())

        updated = client.put(
            f"/api/v1/books/{book['id']}",
            headers=headers,
            json=_payload(name="CSAPP（第 3 版）", heightMm=950),
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "CSAPP（第 3 版）"
        assert updated.json()["heightMm"] == 950
        assert updated.json()["createdAt"] == book["createdAt"]

        deleted = client.delete(f"/api/v1/books/{book['id']}", headers=headers)
        assert deleted.status_code == 204
        listed = client.get("/api/v1/books", headers=headers)
        assert all(item["id"] != book["id"] for item in listed.json())
    finally:
        _cleanup([book["id"]])


def test_book_annotation_and_slot_roundtrip() -> None:
    """Books carry their takeout annotation and preferred stand slot."""
    headers = _headers()
    created = client.post(
        "/api/v1/books",
        headers=headers,
        json=_payload(annotationId="ANN-SHELF-1", preferredSlot=2),
    )
    assert created.status_code == 201
    book = created.json()
    assert book["annotationId"] == "ANN-SHELF-1"
    assert book["preferredSlot"] == 2

    try:
        updated = client.put(
            f"/api/v1/books/{book['id']}",
            headers=headers,
            json=_payload(annotationId=None, preferredSlot=3),
        )
        assert updated.status_code == 200
        assert updated.json()["annotationId"] is None
        assert updated.json()["preferredSlot"] == 3

        listed = client.get("/api/v1/books", headers=headers)
        record = next(item for item in listed.json() if item["id"] == book["id"])
        assert record["annotationId"] is None
        assert record["preferredSlot"] == 3
    finally:
        _cleanup([book["id"]])


def test_book_annotation_slot_validation() -> None:
    headers = _headers()
    bad_slot = client.post("/api/v1/books", headers=headers, json=_payload(preferredSlot=9))
    assert bad_slot.status_code == 422
    long_annotation = client.post(
        "/api/v1/books", headers=headers, json=_payload(annotationId="A" * 81)
    )
    assert long_annotation.status_code == 422


def test_book_validation_and_missing_records() -> None:
    headers = _headers()
    empty_name = client.post("/api/v1/books", headers=headers, json=_payload(name=""))
    assert empty_name.status_code == 422
    bad_height = client.post("/api/v1/books", headers=headers, json=_payload(heightMm=5))
    assert bad_height.status_code == 422

    missing_update = client.put("/api/v1/books/BOOK-NOPE", headers=headers, json=_payload())
    assert missing_update.status_code == 404
    missing_delete = client.delete("/api/v1/books/BOOK-NOPE", headers=headers)
    assert missing_delete.status_code == 404
