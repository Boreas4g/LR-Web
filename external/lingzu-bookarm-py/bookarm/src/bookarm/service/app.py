"""FastAPI application exposing the serial take/return runner over HTTP."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, List, Optional, Tuple
import uuid

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from bookarm.api.requests import (
    MidReturnBookRequest,
    MidReturnStage,
    ReturnBookRequest,
    ReturnBookStage,
    TakeBookRequest,
    TakeBookStage,
)
from bookarm.domain.models.book import BookIdentity, SpineTemplate
from bookarm.domain.models.shelf import BookStandSlot
from bookarm.service.runner import RunnerBusyError, WorkflowRunner
from bookarm.service.safety import SafetyController, SafetyStopReport
from bookarm.service.schemas import (
    ArmDebugPoseRequest,
    ArmDebugPoseView,
    ArmDisableRequest,
    ArmDisableView,
    ArmEnableView,
    ArmJointsMoveRequest,
    ArmJointsView,
    ArmZeroCalibrationRequest,
    ArmZeroCalibrationView,
    ArmZeroRequest,
    CaptureView,
    CartesianJogRequest,
    HealthView,
    LibraryItemView,
    LibraryVerifyItemView,
    LibraryView,
    LiftHeightView,
    LiftMoveRequest,
    LiftMoveView,
    ManualSegmentRequest,
    RodMoveRequest,
    RodMoveView,
    SafetyStopView,
    SegmentedSpineView,
    SegmentRequest,
    SegmentView,
    SelfCheckView,
    VisionDebugRunRequest,
    VisionDebugSpineCheckRequest,
    WorkflowTaskView,
    task_view,
)
from bookarm.service.studio import (
    ArmEnabledError,
    ArmJointMoveError,
    ArmNotEnabledError,
    ArmUnavailableError,
    CameraTransformError,
    CaptureRecord,
    CartesianJogError,
    SegmentationUnavailableError,
    StudioBackend,
    StudioBusyError,
)
from bookarm.service.tuning import TuningService, build_tuning_router


MAX_TEMPLATE_BYTES = 10 * 1024 * 1024
ALLOWED_TEMPLATE_SUFFIXES = (".jpg", ".jpeg", ".png")


def _bearer_token_dependency(token: str) -> Callable:
    """Build a dependency that requires ``Authorization: Bearer <token>``."""

    async def verify(authorization: Annotated[Optional[str], Header()] = None) -> None:
        if authorization != "Bearer %s" % token:
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    return verify


def _save_template(upload_dir: Path, upload: UploadFile, *, field: str) -> Tuple[Path, str]:
    """Persist one uploaded spine template with a per-task name prefix.

    Returns the saved path together with the original filename stem so the
    caller can derive the default book identifier from the user's image.
    """

    original = Path(upload.filename or "spine.png").name
    stem = Path(original).stem or "spine"
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_TEMPLATE_SUFFIXES:
        raise HTTPException(
            status_code=422,
            detail="%s must be one of: %s" % (field, ", ".join(ALLOWED_TEMPLATE_SUFFIXES)),
        )
    data = upload.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="%s must not be empty" % field)
    if len(data) > MAX_TEMPLATE_BYTES:
        raise HTTPException(status_code=422, detail="%s must be at most 10 MB" % field)
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved = upload_dir / ("%s_%s%s" % (uuid.uuid4().hex[:8], stem, suffix))
    saved.write_bytes(data)
    return saved, stem


def _resolve_template(
    upload: Optional[UploadFile],
    template_id: Optional[str],
    *,
    field: str,
    upload_dir: Path,
    studio: Optional[StudioBackend],
) -> Tuple[Path, str]:
    """Resolve the spine template from an upload or the studio library.

    Exactly one source must be given. Returns the template path together
    with the default book identifier derived from the chosen source.
    """

    template_id = (template_id or "").strip() or None
    if upload is not None and template_id is not None:
        raise HTTPException(
            status_code=422,
            detail="provide exactly one of %s or template_id, not both" % field,
        )
    if upload is None and template_id is None:
        raise HTTPException(
            status_code=422,
            detail="one of %s or template_id is required" % field,
        )
    if upload is not None:
        return _save_template(upload_dir, upload, field=field)
    if studio is None:
        raise HTTPException(
            status_code=422,
            detail="template_id requires the spine studio library",
        )
    try:
        return studio.library_image_path(template_id), template_id
    except KeyError as error:
        raise HTTPException(
            status_code=422,
            detail="unknown template_id: %s" % template_id,
        ) from error


def _get_task(runner: WorkflowRunner, task_id: str) -> WorkflowTaskView:
    try:
        return task_view(runner.status(task_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="unknown task: %s" % task_id) from error


def _cancel_task(runner: WorkflowRunner, task_id: str) -> WorkflowTaskView:
    try:
        return task_view(runner.cancel(task_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="unknown task: %s" % task_id) from error


def _dismiss_task_recovery(runner: WorkflowRunner, task_id: str) -> WorkflowTaskView:
    try:
        return task_view(runner.dismiss_recovery(task_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="unknown task: %s" % task_id) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def create_app(
    runner: WorkflowRunner,
    *,
    upload_dir: Path,
    token: Optional[str] = None,
    studio: Optional[StudioBackend] = None,
    tuning: Optional["TuningService"] = None,
    safety: Optional[SafetyController] = None,
) -> FastAPI:
    """Build the take-out, return and spine-studio HTTP APIs around one runner."""

    upload_dir = Path(upload_dir)
    dependencies = [Depends(_bearer_token_dependency(token))] if token is not None else []
    takeout_router = APIRouter(prefix="/api/v1/takeout", dependencies=dependencies)
    return_router = APIRouter(prefix="/api/v1/return", dependencies=dependencies)
    midreturn_router = APIRouter(prefix="/api/v1/midreturn", dependencies=dependencies)
    tasks_router = APIRouter(prefix="/api/v1/tasks", dependencies=dependencies)

    @takeout_router.post("/tasks", status_code=201, response_model=WorkflowTaskView)
    def create_takeout_task(
        target_height_mm: Annotated[float, Form(ge=10.0, le=1700.0)],
        spine_image: Annotated[Optional[UploadFile], File()] = None,
        template_id: Annotated[Optional[str], Form()] = None,
        destination_slot: Annotated[int, Form(ge=1, le=5)] = 1,
        lift_speed_level: Annotated[int, Form(ge=1, le=3)] = 1,
        stop_after_stage: Annotated[str, Form()] = TakeBookStage.ARM_STANDBY.value,
        book_id: Annotated[Optional[str], Form()] = None,
    ) -> WorkflowTaskView:
        try:
            stop_after = TakeBookStage(stop_after_stage)
        except ValueError as error:
            choices = ", ".join(stage.value for stage in TakeBookStage)
            raise HTTPException(
                status_code=422,
                detail="stop_after_stage must be one of: %s" % choices,
            ) from error
        template_path, template_stem = _resolve_template(
            spine_image,
            template_id,
            field="spine_image",
            upload_dir=upload_dir,
            studio=studio,
        )
        request = TakeBookRequest(
            book=BookIdentity((book_id or "").strip() or template_stem),
            target_height_mm=target_height_mm,
            spine_template=SpineTemplate(template_path),
            destination_slot=BookStandSlot(destination_slot),
            lift_speed_level=lift_speed_level,
            stop_after_stage=stop_after,
        )
        try:
            task = runner.submit_take(request)
        except RunnerBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return task_view(task)

    @takeout_router.get("/tasks/{task_id}", response_model=WorkflowTaskView)
    def get_takeout_task(task_id: str) -> WorkflowTaskView:
        return _get_task(runner, task_id)

    @takeout_router.post("/tasks/{task_id}/cancel", response_model=WorkflowTaskView)
    def cancel_takeout_task(task_id: str) -> WorkflowTaskView:
        return _cancel_task(runner, task_id)

    @takeout_router.get("/health", response_model=HealthView)
    def health() -> HealthView:
        return HealthView(started=runner.started, mode=runner.mode)

    @return_router.post("/tasks", status_code=201, response_model=WorkflowTaskView)
    def create_return_task(
        target_height_mm: Annotated[float, Form(ge=10.0, le=1700.0)],
        left_neighbor_image: Annotated[Optional[UploadFile], File()] = None,
        template_id: Annotated[Optional[str], Form()] = None,
        source_slot: Annotated[int, Form(ge=1, le=5)] = 1,
        lift_speed_level: Annotated[int, Form(ge=1, le=3)] = 1,
        stop_after_stage: Annotated[str, Form()] = ReturnBookStage.SHELF_INSERT.value,
        book_id: Annotated[Optional[str], Form()] = None,
    ) -> WorkflowTaskView:
        try:
            stop_after = ReturnBookStage(stop_after_stage)
        except ValueError as error:
            choices = ", ".join(stage.value for stage in ReturnBookStage)
            raise HTTPException(
                status_code=422,
                detail="stop_after_stage must be one of: %s" % choices,
            ) from error
        template_path, template_stem = _resolve_template(
            left_neighbor_image,
            template_id,
            field="left_neighbor_image",
            upload_dir=upload_dir,
            studio=studio,
        )
        request = ReturnBookRequest(
            book=BookIdentity((book_id or "").strip() or template_stem),
            target_height_mm=target_height_mm,
            left_neighbor_spine_template=SpineTemplate(template_path),
            source_slot=BookStandSlot(source_slot),
            lift_speed_level=lift_speed_level,
            stop_after_stage=stop_after,
        )
        try:
            task = runner.submit_return(request)
        except RunnerBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return task_view(task)

    @return_router.get("/tasks/{task_id}", response_model=WorkflowTaskView)
    def get_return_task(task_id: str) -> WorkflowTaskView:
        return _get_task(runner, task_id)

    @return_router.post("/tasks/{task_id}/cancel", response_model=WorkflowTaskView)
    def cancel_return_task(task_id: str) -> WorkflowTaskView:
        return _cancel_task(runner, task_id)

    @midreturn_router.post("/tasks", status_code=201, response_model=WorkflowTaskView)
    def create_midreturn_task(
        target_height_mm: Annotated[float, Form(ge=10.0, le=1700.0)],
        left_neighbor_image: Annotated[Optional[UploadFile], File()] = None,
        template_id: Annotated[Optional[str], Form()] = None,
        source_slot: Annotated[int, Form(ge=1, le=5)] = 1,
        lift_speed_level: Annotated[int, Form(ge=1, le=3)] = 1,
        stop_after_stage: Annotated[str, Form()] = MidReturnStage.SHELF_INSERT.value,
        book_id: Annotated[Optional[str], Form()] = None,
        injected_grasp_pose: Annotated[Optional[str], Form()] = None,
    ) -> WorkflowTaskView:
        try:
            stop_after = MidReturnStage(stop_after_stage)
        except ValueError as error:
            choices = ", ".join(stage.value for stage in MidReturnStage)
            raise HTTPException(
                status_code=422,
                detail="stop_after_stage must be one of: %s" % choices,
            ) from error
        injected_pose: Optional[Tuple[float, ...]] = None
        if injected_grasp_pose and injected_grasp_pose.strip():
            try:
                parts = [float(part.strip()) for part in injected_grasp_pose.split(",")]
            except ValueError as error:
                raise HTTPException(
                    status_code=422,
                    detail="injected_grasp_pose must be comma-separated numbers",
                ) from error
            if len(parts) != 6 or not all(math.isfinite(value) for value in parts):
                raise HTTPException(
                    status_code=422,
                    detail="injected_grasp_pose must be six finite values (m, m, m, rad, rad, rad)",
                )
            injected_pose = tuple(parts)
        template_path, template_stem = _resolve_template(
            left_neighbor_image,
            template_id,
            field="left_neighbor_image",
            upload_dir=upload_dir,
            studio=studio,
        )
        request = MidReturnBookRequest(
            book=BookIdentity((book_id or "").strip() or template_stem),
            target_height_mm=target_height_mm,
            left_neighbor_spine_template=SpineTemplate(template_path),
            source_slot=BookStandSlot(source_slot),
            lift_speed_level=lift_speed_level,
            stop_after_stage=stop_after,
            injected_grasp_pose=injected_pose,
        )
        try:
            task = runner.submit_mid_return(request)
        except RunnerBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return task_view(task)

    @midreturn_router.get("/tasks/{task_id}", response_model=WorkflowTaskView)
    def get_midreturn_task(task_id: str) -> WorkflowTaskView:
        return _get_task(runner, task_id)

    @midreturn_router.post("/tasks/{task_id}/cancel", response_model=WorkflowTaskView)
    def cancel_midreturn_task(task_id: str) -> WorkflowTaskView:
        return _cancel_task(runner, task_id)

    @tasks_router.post("/{task_id}/dismiss-recovery", response_model=WorkflowTaskView)
    def dismiss_task_recovery(task_id: str) -> WorkflowTaskView:
        """Erase a recovery_required task once the operator took over manually.

        The record is removed from the journal (not merely re-labelled), so a
        later GET on the same id answers 404.  Any other task state is a 409:
        live tasks and honestly finished tasks must stay auditable.
        """

        return _dismiss_task_recovery(runner, task_id)

    app = FastAPI(title="bookarm take-out/return service")
    app.include_router(takeout_router)
    app.include_router(return_router)
    app.include_router(midreturn_router)
    app.include_router(tasks_router)
    if safety is not None:
        app.include_router(_build_safety_router(safety, dependencies))
    if studio is not None:
        app.include_router(_build_studio_router(studio, dependencies))
    if tuning is not None:
        app.include_router(build_tuning_router(tuning, dependencies))
    return app


def _safety_stop_view(report: SafetyStopReport) -> SafetyStopView:
    return SafetyStopView(
        accepted=report.accepted,
        armStopped=report.armStopped,
        liftStopped=report.liftStopped,
        taskInterrupted=report.taskInterrupted,
    )


def _build_safety_router(safety: SafetyController, dependencies: List) -> APIRouter:
    """Build the immediate-stop routes around one safety controller."""

    safety_router = APIRouter(prefix="/api/v1/safety", dependencies=dependencies)

    @safety_router.post("/stop", response_model=SafetyStopView)
    def safety_stop() -> SafetyStopView:
        """Interrupt the workflow and halt arm and lift motion immediately."""

        return _safety_stop_view(safety.stop())

    @safety_router.post("/emergency-stop", response_model=SafetyStopView)
    def safety_emergency_stop() -> SafetyStopView:
        """Interrupt the workflow and disable all arm motors in place now."""

        return _safety_stop_view(safety.emergency_stop())

    return safety_router


def _capture_view(record: CaptureRecord) -> CaptureView:
    return CaptureView(
        id=record.id,
        width=record.width,
        height=record.height,
        frame_number=record.frame_number,
        created_at=record.created_at,
    )


def _build_studio_router(studio: StudioBackend, dependencies: List) -> APIRouter:
    """Build the spine-studio routes around one hardware-neutral backend."""

    studio_router = APIRouter(prefix="/api/v1/studio", dependencies=dependencies)

    @studio_router.get("/lift/height", response_model=LiftHeightView)
    def read_lift_height() -> LiftHeightView:
        return LiftHeightView(height_mm=studio.read_height_mm())

    @studio_router.post("/lift/move", response_model=LiftMoveView)
    def move_lift(payload: LiftMoveRequest) -> LiftMoveView:
        try:
            target, measured = studio.move_to_height_mm(
                payload.height_mm, speed_level=payload.speed_level
            )
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return LiftMoveView(target_height_mm=target, measured_height_mm=measured)

    @studio_router.post("/captures", status_code=201, response_model=CaptureView)
    def create_capture() -> CaptureView:
        try:
            record = studio.capture()
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _capture_view(record)

    @studio_router.get("/captures/{capture_id}/image")
    def get_capture_image(capture_id: str) -> FileResponse:
        try:
            path = studio.capture_image_path(capture_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown capture: %s" % capture_id) from error
        return FileResponse(path, media_type="image/jpeg")

    @studio_router.post("/captures/{capture_id}/segment", response_model=SegmentView)
    def segment_capture(capture_id: str, payload: SegmentRequest = SegmentRequest()) -> SegmentView:
        try:
            result = studio.segment_capture(capture_id, conf=payload.conf, imgsz=payload.imgsz)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown capture: %s" % capture_id) from error
        except SegmentationUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return SegmentView(
            capture_id=result.capture_id,
            spine_count=len(result.spines),
            spines=[
                SegmentedSpineView(
                    library_id=spine.library_id,
                    index=spine.index,
                    confidence=spine.confidence,
                    area=spine.area,
                    bbox_xyxy=list(spine.bbox_xyxy),
                    polygon_xy=[list(point) for point in spine.polygon_xy],
                )
                for spine in result.spines
            ],
        )

    @studio_router.post(
        "/captures/{capture_id}/manual-segment",
        status_code=201,
        response_model=SegmentedSpineView,
    )
    def segment_capture_manual(capture_id: str, payload: ManualSegmentRequest) -> SegmentedSpineView:
        """Store one hand-drawn spine polygon as a named library template."""

        try:
            spine = studio.segment_capture_manual(
                capture_id,
                [list(point) for point in payload.polygon],
                name=payload.name,
                pad=payload.pad,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown capture: %s" % capture_id) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        check = studio.check_library_template(spine.library_id)
        return SegmentedSpineView(
            library_id=spine.library_id,
            index=spine.index,
            confidence=spine.confidence,
            area=spine.area,
            bbox_xyxy=list(spine.bbox_xyxy),
            polygon_xy=[list(point) for point in spine.polygon_xy],
            selfCheck=SelfCheckView(
                ok=check.ok, detail=check.detail, feature_count=check.feature_count
            ),
        )

    @studio_router.post("/vision-debug/spine-check")
    def vision_debug_spine_check(payload: VisionDebugSpineCheckRequest) -> Dict[str, Any]:
        """倾斜/居中几何检查：拍照匹配指定书脊模板，不动车。"""

        try:
            return studio.vision_check_spine(payload.library_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown library id: %s" % payload.library_id) from error
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @studio_router.post("/vision-debug/align-run")
    def vision_debug_align_run(payload: VisionDebugSpineCheckRequest) -> Dict[str, Any]:
        """完整实测：倾斜判定 + 车辆缓速脉冲对齐（车辆会动！）。"""

        try:
            return studio.vision_align_run(payload.library_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown library id: %s" % payload.library_id) from error
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @studio_router.get("/vision-debug/vehicle-status")
    def vision_debug_vehicle_status() -> Dict[str, Any]:
        """车端缓速脉冲服务状态（离线返回 503）。"""

        try:
            return studio.vehicle_align_status()
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @studio_router.get("/library", response_model=LibraryView)
    def list_library() -> LibraryView:
        return LibraryView(
            items=[
                LibraryItemView(
                    id=record.id,
                    capture_id=record.capture_id,
                    spine_index=record.spine_index,
                    confidence=record.confidence,
                    created_at=record.created_at,
                    name=record.name,
                )
                for record in studio.list_library()
            ]
        )

    @studio_router.post("/library", status_code=201, response_model=LibraryItemView)
    def import_library_image(
        image: Annotated[UploadFile, File()],
        source: Annotated[str, Form()] = "manual-import",
        name: Annotated[str, Form()] = "",
    ) -> LibraryItemView:
        original = Path(image.filename or "spine.png").name
        suffix = Path(original).suffix.lower()
        if suffix not in ALLOWED_TEMPLATE_SUFFIXES:
            raise HTTPException(
                status_code=422,
                detail="image must be one of: %s" % ", ".join(ALLOWED_TEMPLATE_SUFFIXES),
            )
        data = image.file.read()
        if not data:
            raise HTTPException(status_code=422, detail="image must not be empty")
        if len(data) > MAX_TEMPLATE_BYTES:
            raise HTTPException(status_code=422, detail="image must be at most 10 MB")
        import cv2
        import numpy as np

        image_bgr = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image_bgr is None:
            raise HTTPException(status_code=422, detail="image could not be decoded")
        record = studio.import_library_image(
            image_bgr,
            source=source.strip() or "manual-import",
            name=name.strip(),
        )
        check = studio.check_library_template(record.id)
        return LibraryItemView(
            id=record.id,
            capture_id=record.capture_id,
            spine_index=record.spine_index,
            confidence=record.confidence,
            created_at=record.created_at,
            name=record.name,
            selfCheck=SelfCheckView(
                ok=check.ok, detail=check.detail, feature_count=check.feature_count
            ),
        )

    @studio_router.post("/library/verify-all", response_model=List[LibraryVerifyItemView])
    def verify_library_templates() -> List[LibraryVerifyItemView]:
        """Self-check every library template for the library quality report."""

        return [
            LibraryVerifyItemView(**item) for item in studio.verify_library_templates()
        ]

    @studio_router.get("/library/{library_id}/image")
    def get_library_image(library_id: str) -> FileResponse:
        try:
            path = studio.library_image_path(library_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="unknown library image: %s" % library_id
            ) from error
        return FileResponse(path, media_type="image/png")

    @studio_router.delete("/library/{library_id}", status_code=204)
    def delete_library_item(library_id: str) -> None:
        try:
            studio.delete_library_item(library_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="unknown library image: %s" % library_id
            ) from error

    @studio_router.get("/vision-debug")
    def get_vision_debug() -> Dict[str, Any]:
        try:
            return studio.vision_debug_meta()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @studio_router.get("/vision-debug/image")
    def get_vision_debug_image(kind: str = "annotated") -> FileResponse:
        try:
            path = studio.vision_debug_image_path(kind)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(path, media_type="image/jpeg")

    @studio_router.get("/vision-debug/gap")
    def get_vision_debug_gap() -> Dict[str, Any]:
        """Latest depth-gap detection metadata (widths, selected side, Y1)."""

        try:
            return studio.vision_debug_gap_meta()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @studio_router.post("/vision-debug/run")
    def run_vision_debug(
        payload: VisionDebugRunRequest = VisionDebugRunRequest(),
    ) -> Dict[str, Any]:
        """Capture one frame and run spine-match + depth-gap analysis on it."""

        try:
            return studio.run_vision_debug(
                payload.template_id,
                select=payload.select,
                scan_height_ratio=payload.scan_height_ratio,
                scan_band_px=payload.scan_band_px,
                depth_gap_mm=payload.depth_gap_mm,
            )
        except KeyError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (OSError, TimeoutError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except RuntimeError as error:
            # DepthGapLocator detection failures are scene/input issues; device
            # transport RuntimeErrors keep the hardware 503 mapping.
            status = 422 if str(error).startswith("深度书缝识别失败") else 503
            raise HTTPException(status_code=status, detail=str(error)) from error

    @studio_router.post("/arm/debug-pose", response_model=ArmDebugPoseView)
    def move_arm_to_debug_pose(
        payload: ArmDebugPoseRequest = ArmDebugPoseRequest(),
    ) -> ArmDebugPoseView:
        try:
            joints = studio.reset_arm_to_debug_pose(payload.duration_s)
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return ArmDebugPoseView(ok=True, joints_rad=joints)

    @studio_router.post("/arm/enable", response_model=ArmEnableView)
    def enable_arm() -> ArmEnableView:
        try:
            result = studio.arm_enable()
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (OSError, TimeoutError, RuntimeError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return ArmEnableView(
            ok=bool(result.get("ok", True)),
            enabled=bool(result.get("enabled", True)),
            was_enabled=bool(result.get("was_enabled", False)),
        )

    @studio_router.post("/arm/disable", response_model=ArmDisableView)
    def disable_arm(payload: ArmDisableRequest = ArmDisableRequest()) -> ArmDisableView:
        try:
            result = studio.arm_disable(payload.duration_s)
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (OSError, TimeoutError, RuntimeError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return ArmDisableView(
            ok=bool(result.get("ok", True)),
            enabled=bool(result.get("enabled", False)),
            detail=result.get("detail"),
        )

    @studio_router.post("/rod/move", response_model=RodMoveView)
    def move_rod(payload: RodMoveRequest) -> RodMoveView:
        try:
            measured = studio.move_rod_to_deg(
                payload.target_deg,
                payload.speed,
                payload.acceleration,
                payload.tolerance_deg,
                payload.timeout_s,
            )
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (OSError, TimeoutError, RuntimeError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return RodMoveView(target_angle_deg=payload.target_deg, measured_angle_deg=measured)

    @studio_router.post("/arm/cartesian-jog")
    def jog_arm_cartesian(payload: CartesianJogRequest) -> Dict[str, Any]:
        try:
            return studio.cartesian_jog(
                axis=payload.axis,
                delta=payload.delta,
                duration_s=payload.duration_s,
            )
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmNotEnabledError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except CartesianJogError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (OSError, TimeoutError, RuntimeError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @studio_router.get("/arm/joints", response_model=ArmJointsView)
    def get_arm_joints() -> ArmJointsView:
        try:
            return studio.arm_joints()
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (OSError, TimeoutError, RuntimeError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @studio_router.post("/arm/joints/move", response_model=ArmJointsView)
    def move_arm_joints(payload: ArmJointsMoveRequest) -> ArmJointsView:
        try:
            return studio.move_arm_joints(payload.joints_deg, payload.duration_s)
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmNotEnabledError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ArmJointMoveError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (OSError, TimeoutError, RuntimeError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @studio_router.post("/arm/zero", response_model=ArmJointsView)
    def move_arm_zero(payload: ArmZeroRequest = ArmZeroRequest()) -> ArmJointsView:
        try:
            return studio.move_arm_zero(payload.duration_s)
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmNotEnabledError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (OSError, TimeoutError, RuntimeError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @studio_router.post("/arm/zero-calibration", response_model=ArmZeroCalibrationView)
    def calibrate_arm_zero(payload: ArmZeroCalibrationRequest) -> ArmZeroCalibrationView:
        try:
            return studio.calibrate_arm_zero(payload.motor)
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmEnabledError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (OSError, TimeoutError, RuntimeError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @studio_router.post("/arm/faults/clear")
    def clear_arm_faults() -> Dict[str, Any]:
        """Clear latched motor fault codes (operator recovery)."""
        try:
            return studio.clear_arm_faults()
        except StudioBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (OSError, TimeoutError, RuntimeError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @studio_router.get("/camera-transform")
    def get_camera_transform() -> Dict[str, Any]:
        try:
            return studio.camera_transform_view()
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @studio_router.put("/camera-transform")
    def put_camera_transform(payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return studio.update_camera_transform(payload)
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except CameraTransformError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @studio_router.post("/camera-transform/reset")
    def reset_camera_transform() -> Dict[str, Any]:
        try:
            return studio.reset_camera_transform()
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @studio_router.get("/arm/thermal")
    def get_arm_thermal() -> Dict[str, Any]:
        try:
            return studio.arm_thermal_status()
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @studio_router.get("/hardware/status")
    def get_hardware_status() -> Dict[str, Any]:
        try:
            return studio.hardware_status()
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @studio_router.post("/arm/thermal-lockout/clear")
    def clear_arm_thermal() -> Dict[str, Any]:
        try:
            return studio.clear_arm_thermal_lockout()
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return studio_router
