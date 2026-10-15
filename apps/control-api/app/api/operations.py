from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, JSONResponse

import csv
import io
import zipfile
from datetime import UTC, datetime
from pathlib import PurePosixPath

from ..bookarm_client import BookarmError
from ..config import get_settings
from ..dependencies import get_current_user
from ..map_files import decode_base64
from ..map_register import register_imported_map
from ..schemas import (
    AlertRecord,
    Book,
    BookImportFailure,
    BookImportRequest,
    BookImportResult,
    BookUpsert,
    CompleteReturnRequest,
    CompleteTakeoutRequest,
    CreateMapAnnotationRequest,
    CruiseSyncPreview,
    DispatchTask,
    MapAnnotation,
    MapAnnotationResult,
    MapImportRequest,
    MapImportResult,
    MapVersion,
    MultiRoundRequest,
    StressTestTemplate,
    StressTestTemplateUpsert,
    TaskStatus,
    UpdateMapAnnotationRequest,
    User,
)
from ..store import AnnotationInUseError, StressTestTemplateNameConflictError, store

router = APIRouter(prefix="/api/v1", tags=["operations"])
settings = get_settings()


@router.get("/dispatch/tasks", response_model=list[DispatchTask])
def list_tasks(
    statuses: list[TaskStatus] | None = Query(default=None, alias="status"),
    limit: int | None = Query(default=None, ge=1, le=1000),
    _: User = Depends(get_current_user),
) -> list[DispatchTask]:
    """Task list; polling clients narrow it with ?status=…&limit=… (union:
    every task in a listed state plus the newest `limit` tasks). No query
    params returns the full list as before."""
    return store.list_tasks(statuses=statuses, limit=limit)


@router.post(
    "/circulation/complete-takeout",
    response_model=DispatchTask,
    status_code=status.HTTP_201_CREATED,
)
def create_complete_takeout(
    request: CompleteTakeoutRequest, user: User = Depends(get_current_user)
) -> DispatchTask:
    try:
        return store.create_complete_takeout_task(request, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post(
    "/circulation/complete-takeout/{task_id}/cancel",
    response_model=DispatchTask,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_complete_takeout(task_id: str, user: User = Depends(get_current_user)) -> DispatchTask:
    try:
        return store.cancel_complete_takeout(task_id, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post(
    "/circulation/complete-return",
    response_model=DispatchTask,
    status_code=status.HTTP_201_CREATED,
)
def create_complete_return(
    request: CompleteReturnRequest, user: User = Depends(get_current_user)
) -> DispatchTask:
    try:
        return store.create_complete_return_task(request, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post(
    "/circulation/complete-return/{task_id}/cancel",
    response_model=DispatchTask,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_complete_return(task_id: str, user: User = Depends(get_current_user)) -> DispatchTask:
    try:
        return store.cancel_complete_return(task_id, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/bookarm/tasks/{task_id}/dismiss-recovery")
def dismiss_bookarm_recovery(task_id: str, user: User = Depends(get_current_user)) -> dict:
    """Proxy: clear a bookarm RECOVERY_REQUIRED task after manual handling."""
    try:
        return store.dismiss_bookarm_recovery(task_id, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except BookarmError as error:
        raise HTTPException(
            status_code=error.status if error.status and 400 <= error.status < 600 else 502,
            detail=str(error),
        ) from error


@router.get("/books", response_model=list[Book])
def list_books(_: User = Depends(get_current_user)) -> list[Book]:
    return store.list_books()


@router.post("/books", response_model=Book, status_code=status.HTTP_201_CREATED)
def create_book(request: BookUpsert, user: User = Depends(get_current_user)) -> Book:
    try:
        return store.create_book(request, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error


@router.put("/books/{book_id}", response_model=Book)
def update_book(book_id: str, request: BookUpsert, user: User = Depends(get_current_user)) -> Book:
    try:
        return store.update_book(book_id, request, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.delete("/books/{book_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_book(book_id: str, user: User = Depends(get_current_user)) -> None:
    try:
        store.delete_book(book_id, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.get("/debug/stress-test/templates", response_model=list[StressTestTemplate])
def list_stress_test_templates(_: User = Depends(get_current_user)) -> list[StressTestTemplate]:
    return store.list_stress_test_templates()


@router.post(
    "/debug/stress-test/templates",
    response_model=StressTestTemplate,
    status_code=status.HTTP_201_CREATED,
)
def create_stress_test_template(
    request: StressTestTemplateUpsert, user: User = Depends(get_current_user)
) -> StressTestTemplate:
    try:
        return store.create_stress_test_template(request, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except StressTestTemplateNameConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.put("/debug/stress-test/templates/{template_id}", response_model=StressTestTemplate)
def update_stress_test_template(
    template_id: str, request: StressTestTemplateUpsert, user: User = Depends(get_current_user)
) -> StressTestTemplate:
    try:
        return store.update_stress_test_template(template_id, request, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except StressTestTemplateNameConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.delete("/debug/stress-test/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_stress_test_template(template_id: str, user: User = Depends(get_current_user)) -> None:
    try:
        store.delete_stress_test_template(template_id, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


_BOOK_IMPORT_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
_BOOK_IMPORT_IMAGE_MAX_BYTES = 10 * 1024 * 1024


def _book_import_images(archive: "zipfile.ZipFile") -> dict[str, str]:
    """Index zip entries by basename so CSV rows can name files plainly."""
    indexed: dict[str, str] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        indexed.setdefault(PurePosixPath(info.filename).name, info.filename)
    return indexed


@router.post("/library/book-imports", response_model=BookImportResult)
def import_books(
    request: BookImportRequest, user: User = Depends(get_current_user)
) -> BookImportResult:
    """Batch-register books from a CSV (书名,书脊图文件名,高度mm,点位标注名,槽位)
    plus a zip of the referenced spine images (both Base64, like map import).
    Each row is validated independently; bad rows are reported, good rows
    still import."""
    try:
        store.ensure_book_maintainer(user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error

    try:
        csv_data = decode_base64(request.csvContent, "CSV")
        zip_data = decode_base64(request.imagesContent, "图片包")
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    try:
        text = csv_data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="CSV 需为 UTF-8 编码",
        ) from error
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_data))
    except zipfile.BadZipFile as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="图片包需为 zip 格式",
        ) from error
    images = _book_import_images(archive)
    client = store.bookarm_client()

    total = 0
    succeeded = 0
    failed: list[BookImportFailure] = []
    header_checked = False
    for line_no, columns in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not any(column.strip() for column in columns):
            continue
        if not header_checked:
            header_checked = True
            # 首行高度列无法解析成数字时视为表头跳过。
            if len(columns) < 3 or not columns[2].strip().replace(".", "", 1).isdigit():
                continue
        total += 1

        def reject(reason: str) -> None:
            failed.append(BookImportFailure(row=line_no, reason=reason))

        if len(columns) < 3:
            reject("列数不足（需要 书名,书脊图文件名,高度mm[,点位标注名,槽位]）")
            continue
        name = columns[0].strip()
        image_name = columns[1].strip()
        try:
            height_mm = float(columns[2].strip())
        except ValueError:
            reject(f"高度「{columns[2].strip()}」不是数字")
            continue
        annotation_label = columns[3].strip() if len(columns) > 3 else ""
        slot_text = columns[4].strip() if len(columns) > 4 else ""

        if not name or len(name) > 80:
            reject("书名不能为空且不超过 80 字")
            continue
        if not 10.0 <= height_mm <= 1700.0:
            reject(f"高度 {height_mm:g} 超出 10–1700 mm 范围")
            continue
        annotation_id: str | None = None
        if annotation_label:
            annotation = store.find_annotation_by_label(annotation_label)
            if annotation is None:
                reject(f"点位标注「{annotation_label}」不存在")
                continue
            annotation_id = annotation.id
        preferred_slot: int | None = None
        if slot_text:
            try:
                preferred_slot = int(slot_text)
            except ValueError:
                reject(f"槽位「{slot_text}」不是整数")
                continue
            if not 1 <= preferred_slot <= 5:
                reject(f"槽位 {preferred_slot} 超出 1–5 范围")
                continue
        if not image_name:
            reject("书脊图文件名不能为空")
            continue
        # zip 索引按 basename；CSV 里写出子路径也能匹配（bookarm 只收纯文件名）。
        image_key = PurePosixPath(image_name.replace("\\", "/")).name
        entry = images.get(image_key)
        if entry is None:
            reject(f"压缩包中找不到书脊图「{image_name}」")
            continue
        suffix = "." + image_key.rsplit(".", 1)[-1].lower() if "." in image_key else ""
        if suffix not in _BOOK_IMPORT_IMAGE_SUFFIXES:
            reject("书脊图只支持 JPG / PNG")
            continue
        image_data = archive.read(entry)
        if len(image_data) > _BOOK_IMPORT_IMAGE_MAX_BYTES:
            reject("书脊图超过 10MB")
            continue
        try:
            imported = client.import_library_image(
                filename=image_key, data=image_data, name=name
            )
        except BookarmError as error:
            reject(f"书脊图导入机器人书脊库失败：{error}")
            continue
        template_id = imported.get("id") if isinstance(imported, dict) else None
        if not template_id:
            reject("机器人书脊库未返回图片 id")
            continue
        try:
            store.create_book(
                BookUpsert(
                    name=name,
                    templateId=str(template_id),
                    heightMm=height_mm,
                    annotationId=annotation_id,
                    preferredSlot=preferred_slot,
                ),
                user,
            )
        except PermissionError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
        succeeded += 1
    return BookImportResult(total=total, succeeded=succeeded, failed=failed)


@router.get("/library/export")
def export_library(_: User = Depends(get_current_user)) -> JSONResponse:
    """Download one JSON backup: books + maps + annotations + (via bookarm)
    the on-robot spine-library listing. A bookarm outage degrades the spine
    listing to an error note instead of failing the whole export."""
    payload = store.library_export()
    try:
        listing = store.bookarm_client().list_library()
        payload["spineLibrary"] = (
            listing.get("items", []) if isinstance(listing, dict) else []
        )
    except BookarmError as error:
        payload["spineLibrary"] = []
        payload["spineLibraryError"] = str(error)
    filename = f"lr-library-backup-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/circulation/multi-round",
    response_model=DispatchTask,
    status_code=status.HTTP_201_CREATED,
)
def create_multi_round(
    request: MultiRoundRequest, user: User = Depends(get_current_user)
) -> DispatchTask:
    try:
        return store.create_multi_round_task(request, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post(
    "/circulation/multi-round/{task_id}/cancel",
    response_model=DispatchTask,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_multi_round(task_id: str, user: User = Depends(get_current_user)) -> DispatchTask:
    try:
        return store.cancel_multi_round(task_id, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("/circulation/multi-round/{task_id}/vision/{name}")
def multi_round_vision_image(
    task_id: str, name: str, _: User = Depends(get_current_user)
) -> FileResponse:
    """Serve one per-book vision snapshot (spine match / gap overlay) captured
    by the multi-round orchestration right after that book's sub-task ended."""
    try:
        return FileResponse(
            store.multi_round_vision_path(task_id, name), media_type="image/jpeg"
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.get("/circulation/complete-takeout/{task_id}/vision/{name}")
def complete_takeout_vision_image(
    task_id: str, name: str, _: User = Depends(get_current_user)
) -> FileResponse:
    """Serve the spine-recognition snapshot captured during one complete
    takeout (refreshed the moment each vision stage completes)."""
    try:
        return FileResponse(
            store.flow_vision_path(task_id, "takeout", name), media_type="image/jpeg"
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.get("/circulation/complete-return/{task_id}/vision/{name}")
def complete_return_vision_image(
    task_id: str, name: str, _: User = Depends(get_current_user)
) -> FileResponse:
    """Serve the spine-recognition snapshot captured during one complete
    return (refreshed the moment each vision stage completes)."""
    try:
        return FileResponse(
            store.flow_vision_path(task_id, "return", name), media_type="image/jpeg"
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.get("/library/maps", response_model=list[MapVersion])
def list_maps(_: User = Depends(get_current_user)) -> list[MapVersion]:
    return store.map_versions


@router.post("/library/maps/import", response_model=MapImportResult, status_code=status.HTTP_201_CREATED)
def import_map(request: MapImportRequest, user: User = Depends(get_current_user)) -> MapImportResult:
    try:
        map_version = store.import_map(request, user)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    # 单机交付：入库后顺带把地图注册进本机车端地图库；失败不阻断入库，
    # 由响应里的 vehicleRegistered/vehicleRegisterNote 告知操作员。
    registered, note = register_imported_map(
        map_version, store.map_storage_dir, enabled=settings.vehicle_map_register_enabled
    )
    return MapImportResult(
        **map_version.model_dump(), vehicleRegistered=registered, vehicleRegisterNote=note
    )


@router.get("/library/maps/{map_id}/preview")
def map_preview(map_id: str, _: User = Depends(get_current_user)) -> FileResponse:
    try:
        return FileResponse(store.map_preview_path(map_id), media_type="image/png")
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.delete("/library/maps/{map_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_map(map_id: str, user: User = Depends(get_current_user)) -> None:
    if not any(item.id == map_id for item in store.map_versions):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="地图版本不存在")
    try:
        store.delete_map_version(map_id, user)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("/library/maps/{map_id}/annotations", response_model=list[MapAnnotation])
def list_map_annotations(map_id: str, _: User = Depends(get_current_user)) -> list[MapAnnotation]:
    try:
        return store.list_map_annotations(map_id)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.post(
    "/library/maps/{map_id}/annotations", response_model=MapAnnotationResult, status_code=status.HTTP_201_CREATED
)
def create_map_annotation(
    map_id: str, request: CreateMapAnnotationRequest, user: User = Depends(get_current_user)
) -> MapAnnotationResult:
    try:
        return store.create_map_annotation(map_id, request, user)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error


@router.patch("/library/maps/{map_id}/annotations/{annotation_id}", response_model=MapAnnotation)
def update_map_annotation(
    map_id: str,
    annotation_id: str,
    request: UpdateMapAnnotationRequest,
    user: User = Depends(get_current_user),
) -> MapAnnotation:
    try:
        return store.update_map_annotation(map_id, annotation_id, request, user)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error


@router.get("/library/maps/{map_id}/annotations/sync-cruise/preview", response_model=CruiseSyncPreview)
def preview_cruise_annotations(map_id: str, _: User = Depends(get_current_user)) -> CruiseSyncPreview:
    try:
        return store.preview_cruise_annotations(map_id)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error


@router.post("/library/maps/{map_id}/annotations/sync-cruise", response_model=list[MapAnnotation])
def sync_cruise_annotations(map_id: str, user: User = Depends(get_current_user)) -> list[MapAnnotation]:
    try:
        return store.sync_cruise_annotations(map_id, user)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error


@router.delete("/library/maps/{map_id}/annotations/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_map_annotation(map_id: str, annotation_id: str, user: User = Depends(get_current_user)) -> None:
    try:
        store.delete_map_annotation(map_id, annotation_id, user)
    except AnnotationInUseError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.get("/alerts", response_model=list[AlertRecord])
def list_alerts(_: User = Depends(get_current_user)) -> list[AlertRecord]:
    return store.alerts


@router.post("/alerts/{alert_id}/ack", response_model=AlertRecord)
def acknowledge_alert(alert_id: str, user: User = Depends(get_current_user)) -> AlertRecord:
    """Mark one alert acknowledged; repeating an ack is a harmless no-op."""
    try:
        return store.acknowledge_alert(alert_id, user)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
