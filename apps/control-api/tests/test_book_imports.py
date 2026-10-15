"""Batch book import (CSV + image zip) and library backup export tests."""

from __future__ import annotations

import base64
import io
import zipfile

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _headers() -> dict[str, str]:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['token']}"}


class FakeBookarmClient:
    """Test double capturing spine-image uploads and serving library listings."""

    def __init__(self, fail_uploads: bool = False) -> None:
        self.uploaded: list[dict[str, object]] = []
        self.fail_uploads = fail_uploads

    def import_library_image(self, *, filename, data, name="", source="batch-import"):  # noqa: ANN001, ANN201
        if self.fail_uploads:
            from app.bookarm_client import BookarmError

            raise BookarmError(None, "bookarm 服务不可达（测试）")
        self.uploaded.append({"filename": filename, "name": name, "source": source})
        return {"id": f"LIB-IMP-{len(self.uploaded):03d}"}

    def list_library(self):  # noqa: ANN201
        return {
            "items": [
                {"id": "LIB-TPL-1", "name": "深入理解计算机系统"},
                {"id": "LIB-IMP-001", "name": "导入书 1"},
            ]
        }


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _payload(csv_text: str, entries: dict[str, bytes]) -> dict[str, str]:
    return {
        "csvName": "books.csv",
        "csvContent": _b64(csv_text.encode("utf-8-sig")),
        "imagesName": "spines.zip",
        "imagesContent": _b64(_zip_bytes(entries)),
    }


def _seed_annotation(label: str = "A-03 书架") -> None:
    from app.schemas import MapAnnotation
    from app.store import store

    store.map_annotations.setdefault("MAP-TEST-IMPORT", []).append(
        MapAnnotation(
            id="ANN-IMPORT-1",
            mapId="MAP-TEST-IMPORT",
            kind="shelf",
            label=label,
            pixelX=10,
            pixelY=20,
            mapX=1.0,
            mapY=2.0,
            yaw=1.57,
            createdAt="2026-09-30T00:00:00+00:00",
            createdBy="test",
        )
    )


def _cleanup(book_ids: list[str]) -> None:
    from app.store import store

    with store._lock:
        for book_id in book_ids:
            store.books.pop(book_id, None)
        store.map_annotations.pop("MAP-TEST-IMPORT", None)
    store.bookarm_client_factory = None


def _imported_book_ids() -> list[str]:
    from app.store import store

    return [book.id for book in store.list_books() if book.templateId.startswith("LIB-IMP-")]


def test_book_import_happy_path() -> None:
    fake = FakeBookarmClient()
    from app.store import store

    store.bookarm_client_factory = lambda: fake
    _seed_annotation()
    headers = _headers()
    csv_text = (
        "书名,书脊图文件名,高度mm,点位标注名,槽位\n"
        "导入书 1,a.jpg,550,A-03 书架,2\n"
        "导入书 2,sub/b.png,800,,\n"
    )
    try:
        response = client.post(
            "/api/v1/library/book-imports",
            headers=headers,
            json=_payload(csv_text, {"a.jpg": b"jpg-bytes", "sub/b.png": b"png-bytes"}),
        )
        assert response.status_code == 200
        result = response.json()
        assert result["total"] == 2
        assert result["succeeded"] == 2
        assert result["failed"] == []

        books = {book.name: book for book in store.list_books()}
        first = books["导入书 1"]
        assert first.templateId == "LIB-IMP-001"
        assert first.heightMm == 550
        assert first.annotationId == "ANN-IMPORT-1"
        assert first.preferredSlot == 2
        second = books["导入书 2"]
        assert second.templateId == "LIB-IMP-002"
        assert second.annotationId is None
        assert second.preferredSlot is None

        assert [item["filename"] for item in fake.uploaded] == ["a.jpg", "b.png"]
        assert all(item["name"] for item in fake.uploaded)
    finally:
        _cleanup(_imported_book_ids())


def test_book_import_row_failures() -> None:
    fake = FakeBookarmClient()
    from app.store import store

    store.bookarm_client_factory = lambda: fake
    _seed_annotation()
    headers = _headers()
    csv_text = (
        "书名,书脊图文件名,高度mm,点位标注名,槽位\n"
        "坏-点位,c.jpg,550,不存在的点位,\n"
        "坏-高度,c.jpg,5,,\n"
        "坏-图片,missing.jpg,550,,\n"
        "坏-槽位,c.jpg,550,,9\n"
        "坏-高度文本,c.jpg,abc,,\n"
        "好-普通,c.jpg,600,A-03 书架,1\n"
    )
    try:
        response = client.post(
            "/api/v1/library/book-imports",
            headers=headers,
            json=_payload(csv_text, {"c.jpg": b"jpg-bytes"}),
        )
        assert response.status_code == 200
        result = response.json()
        assert result["total"] == 6
        assert result["succeeded"] == 1
        reasons = {failure["row"]: failure["reason"] for failure in result["failed"]}
        assert set(reasons) == {2, 3, 4, 5, 6}
        assert "点位" in reasons[2]
        assert "10–1700" in reasons[3]
        assert "找不到" in reasons[4]
        assert "槽位" in reasons[5]
        assert "不是数字" in reasons[6]
        # 只有成功那一行上传了书脊图。
        assert len(fake.uploaded) == 1
        books = store.list_books()
        assert sum(1 for book in books if book.name == "好-普通") == 1
        assert all(not book.name.startswith("坏-") for book in books)
    finally:
        _cleanup(_imported_book_ids())


def test_book_import_bookarm_outage_fails_rows() -> None:
    from app.store import store

    store.bookarm_client_factory = lambda: FakeBookarmClient(fail_uploads=True)
    headers = _headers()
    try:
        response = client.post(
            "/api/v1/library/book-imports",
            headers=headers,
            json=_payload("书名,书脊图文件名,高度mm\n导入书 1,a.jpg,550\n", {"a.jpg": b"x"}),
        )
        assert response.status_code == 200
        result = response.json()
        assert result["succeeded"] == 0
        assert len(result["failed"]) == 1
        assert "书脊库" in result["failed"][0]["reason"]
    finally:
        _cleanup(_imported_book_ids())


def test_book_import_rejects_bad_files() -> None:
    headers = _headers()
    bad_zip = client.post(
        "/api/v1/library/book-imports",
        headers=headers,
        json={
            "csvName": "books.csv",
            "csvContent": _b64("书名,书脊图文件名,高度mm\n".encode()),
            "imagesName": "spines.zip",
            "imagesContent": _b64(b"not-a-zip"),
        },
    )
    assert bad_zip.status_code == 422

    not_utf8 = client.post(
        "/api/v1/library/book-imports",
        headers=headers,
        json={
            "csvName": "books.csv",
            "csvContent": _b64(b"\xff\xfe\x00bad"),
            "imagesName": "spines.zip",
            "imagesContent": _b64(_zip_bytes({})),
        },
    )
    assert not_utf8.status_code == 422


def test_library_export_includes_books_annotations_and_spines() -> None:
    fake = FakeBookarmClient()
    from app.store import store

    store.bookarm_client_factory = lambda: fake
    _seed_annotation()
    headers = _headers()
    created = client.post(
        "/api/v1/books",
        headers=headers,
        json={
            "name": "导出测试书",
            "templateId": "LIB-TPL-1",
            "heightMm": 550,
            "annotationId": "ANN-IMPORT-1",
            "preferredSlot": 1,
        },
    )
    assert created.status_code == 201
    book_id = created.json()["id"]
    try:
        response = client.get("/api/v1/library/export", headers=headers)
        assert response.status_code == 200
        assert "attachment" in response.headers.get("content-disposition", "")
        payload = response.json()
        assert payload["version"] == 1
        book = next(item for item in payload["books"] if item["id"] == book_id)
        assert book["annotationId"] == "ANN-IMPORT-1"
        assert book["preferredSlot"] == 1
        assert any(item["id"] == "ANN-IMPORT-1" for item in payload["annotations"])
        assert {item["id"] for item in payload["spineLibrary"]} >= {"LIB-TPL-1"}
    finally:
        _cleanup([book_id])


def test_library_export_degrades_when_bookarm_unreachable() -> None:
    from app.bookarm_client import BookarmError
    from app.store import store

    class BrokenBookarm:
        def list_library(self):  # noqa: ANN201
            raise BookarmError(None, "bookarm 服务不可达（测试）")

    store.bookarm_client_factory = lambda: BrokenBookarm()
    headers = _headers()
    try:
        response = client.get("/api/v1/library/export", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert payload["spineLibrary"] == []
        assert "不可达" in payload["spineLibraryError"]
    finally:
        _cleanup([])
