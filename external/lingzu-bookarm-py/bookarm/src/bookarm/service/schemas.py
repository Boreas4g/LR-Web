"""HTTP serialization contracts for the bookarm workflow service."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from bookarm.api.results import WorkflowTask


class StageView(BaseModel):
    """JSON view of one workflow stage result."""

    name: str
    status: str
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class WorkflowTaskView(BaseModel):
    """JSON view of one take-out or return task, live or terminal."""

    id: str
    kind: str
    state: str
    stages: List[StageView] = Field(default_factory=list)
    error: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


# Backward-compatible name from the take-out-only service version.
TakeoutTaskView = WorkflowTaskView


class HealthView(BaseModel):
    """JSON view of the service runtime health."""

    started: bool
    mode: str


class SafetyStopView(BaseModel):
    """JSON view of one accepted safety stop request.

    Field names are camelCase by contract with the vehicle Edge Gateway.
    """

    accepted: bool
    armStopped: bool
    liftStopped: bool
    taskInterrupted: bool


class LiftHeightView(BaseModel):
    """JSON view of the current lift height."""

    height_mm: float


class LiftMoveRequest(BaseModel):
    """JSON body of one studio lift move."""

    height_mm: float = Field(ge=10.0, le=1700.0)
    speed_level: int = Field(default=2, ge=1, le=3)


class LiftMoveView(BaseModel):
    """JSON view of one completed studio lift move."""

    target_height_mm: float
    measured_height_mm: float


class RodMoveRequest(BaseModel):
    """JSON body of one studio rod move."""

    target_deg: float = Field(ge=-100.0, le=150.0)
    speed: int = Field(default=1000, ge=1, le=10000)
    acceleration: int = Field(default=50, ge=1, le=1000)
    tolerance_deg: float = Field(default=1.5, gt=0.0, le=10.0)
    timeout_s: float = Field(default=12.0, gt=0.0, le=60.0)


class RodMoveView(BaseModel):
    """JSON view of one completed studio rod move."""

    target_angle_deg: float
    measured_angle_deg: float


class ArmEnableView(BaseModel):
    """JSON view of one completed studio arm enable."""

    ok: bool
    enabled: bool
    was_enabled: bool = False


class ArmDisableRequest(BaseModel):
    """Optional JSON body for a safe arm disable."""

    duration_s: float = Field(default=3.0, gt=0.0, le=15.0)


class ArmDisableView(BaseModel):
    """JSON view of one completed safe arm disable."""

    ok: bool
    enabled: bool
    detail: Optional[Dict[str, Any]] = None


class CaptureView(BaseModel):
    """JSON view of one stored studio shelf photo."""

    id: str
    width: int
    height: int
    frame_number: int
    created_at: str


class SegmentRequest(BaseModel):
    """Optional JSON body overriding YOLO segmentation thresholds."""

    conf: float = Field(default=0.6, gt=0.0, le=1.0)
    imgsz: int = Field(default=640, ge=32)


class VisionDebugSpineCheckRequest(BaseModel):
    """书脊倾斜/居中调试请求：指定书脊库模板。"""

    library_id: str = Field(min_length=1, max_length=120)


class ManualSegmentRequest(BaseModel):
    """JSON body describing one hand-drawn spine polygon on a capture."""

    name: str = Field(min_length=1, max_length=120)
    polygon: List[Tuple[float, float]] = Field(min_length=3, max_length=40)
    pad: int = Field(default=8, ge=0, le=64)


class SelfCheckView(BaseModel):
    """JSON view of one template recognizability self-check."""

    ok: bool
    detail: str
    feature_count: int = 0


class SegmentedSpineView(BaseModel):
    """JSON view of one spine segmented into the library."""

    library_id: str
    index: int
    confidence: float
    area: int
    bbox_xyxy: List[int]
    polygon_xy: List[List[float]]
    # Populated by manual-segment so the operator learns immediately whether
    # the freshly stored template can be recognized at take-out time.
    selfCheck: Optional[SelfCheckView] = None


class SegmentView(BaseModel):
    """JSON view of all spines segmented from one capture."""

    capture_id: str
    spine_count: int
    spines: List[SegmentedSpineView]


class LibraryItemView(BaseModel):
    """JSON view of one spine crop kept in the library."""

    id: str
    capture_id: str
    spine_index: int
    confidence: float
    created_at: str
    name: str = ""
    # Populated by the import endpoint so a featureless upload is flagged in
    # the same response that stores it (the template is still kept).
    selfCheck: Optional[SelfCheckView] = None


class LibraryVerifyItemView(BaseModel):
    """JSON view of one template in the library-wide quality report."""

    id: str
    name: str
    ok: bool
    detail: str


class LibraryView(BaseModel):
    """JSON view of the whole spine library, newest first."""

    items: List[LibraryItemView] = Field(default_factory=list)


class TuningPrepareRequest(BaseModel):
    """JSON body preparing one tuning session: stand slot plus spine template."""

    slot: int = Field(ge=1, le=4)
    template_id: Optional[str] = None


class TuningStepRunRequest(BaseModel):
    """JSON body selecting one tuning step to execute directly.

    ``index`` omitted runs the session's current step and advances the
    cursor (legacy sequential behaviour); given, runs that step directly.
    """

    index: Optional[int] = Field(default=None, ge=0)


class ArmDebugPoseRequest(BaseModel):
    """Optional JSON body for resetting the arm to the book-debug pose."""

    duration_s: float = Field(default=3.0, gt=0.0, le=15.0)


class CartesianJogRequest(BaseModel):
    """JSON body for one TCP cartesian jog along a base-frame axis.

    ``delta`` is in millimetres for x/y/z and degrees for rx/ry/rz; the
    backend clamps each step (100 mm / 30°) and bounds the target workspace.
    """

    axis: str = Field(pattern="^(x|y|z|rx|ry|rz)$")
    delta: float
    duration_s: float = Field(default=2.0, gt=0.0, le=15.0)


class BookStandExecuteRequest(BaseModel):
    """JSON body for moving the arm to one calibrated book-stand slot pose."""

    slot_id: str
    action: str
    duration_s: Optional[float] = Field(default=None, gt=0.0, le=15.0)


class ArmJointsView(BaseModel):
    """JSON view of the current arm joint pose."""

    enabled: bool
    joints_rad: list[float]
    joints_deg: list[float]
    tcp: Optional[Dict[str, Any]] = None


class ArmJointsMoveRequest(BaseModel):
    """JSON body for one joint-space move to an absolute target (degrees).

    The backend validates the six-joint target through ArmService's joint
    limit checks before any motion is sent.
    """

    joints_deg: list[float] = Field(min_length=6, max_length=6)
    duration_s: float = Field(default=3.0, gt=0.0, le=15.0)


class ArmZeroRequest(BaseModel):
    """Optional JSON body for moving the arm to the built-in zero pose."""

    duration_s: float = Field(default=3.0, gt=0.0, le=15.0)


class ArmZeroCalibrationRequest(BaseModel):
    """JSON body for writing the current arm pose as the encoder zero.

    ``motor`` selects one motor (1..7, 7 is the gripper) or 0 for all six arm
    joints. ``confirm`` must be explicitly true: this redefines the arm's
    coordinate system and cannot be undone by software.
    """

    motor: int = Field(default=0, ge=0, le=7)
    confirm: Literal[True]


class ArmZeroCalibrationView(BaseModel):
    """JSON view of one completed zero calibration."""

    ok: bool
    motor: int
    results: Dict[str, bool]


class VisionDebugRunRequest(BaseModel):
    """Optional JSON body overriding one interactive spine-match + depth-gap run.

    ``template_id`` omitted uses the newest spine-library image, matching the
    interactive tuning workflow; the scan defaults mirror the mid-return plan.
    """

    template_id: Optional[str] = None
    select: str = Field(default="largest", pattern="^(largest|nearest)$")
    scan_height_ratio: float = Field(default=0.67, gt=0.0, le=1.0)
    scan_band_px: int = Field(default=5, ge=1, le=64)
    depth_gap_mm: float = Field(default=15.0, gt=0.0)


class ArmDebugPoseView(BaseModel):
    """JSON view of one completed arm reset to the book-debug pose."""

    ok: bool
    joints_rad: List[float]


def _jsonable(value: Any) -> Any:
    """Convert workflow detail values into JSON-serializable structures."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def task_view(task: WorkflowTask) -> WorkflowTaskView:
    """Map a transport-neutral workflow task onto its HTTP view."""

    result = task.result
    return WorkflowTaskView(
        id=task.id.value,
        kind=task.kind.value,
        state=task.state.value,
        stages=[
            StageView(
                name=stage.name,
                status=stage.status.value,
                message=stage.message,
                details=_jsonable(stage.details),
            )
            for stage in task.stages
        ],
        error=result.error if result is not None else None,
        payload=_jsonable(result.payload) if result is not None else {},
    )
