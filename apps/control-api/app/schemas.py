from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StationIdentity(BaseModel):
    """Non-sensitive identity exposed for operator discovery and diagnostics."""

    product: Literal["lr-control-station"] = "lr-control-station"
    id: str
    name: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = "ok"
    service: str
    environment: str
    dependencies: dict[str, str]
    station: StationIdentity


class User(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    username: str
    name: str
    role: str


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=6, max_length=200)
    name: str = Field(min_length=1, max_length=80)


class AuthResponse(BaseModel):
    user: User
    token: str


class RobotHealth(BaseModel):
    navigation: bool
    chassis: bool
    arm: bool
    # Existing deployed robots do not have this field until their Gateway is upgraded.
    rod: bool = False
    # Same upgrade window as rod: Gateways without imuReady telemetry default to False.
    imu: bool = False
    lift: bool
    camera: bool
    # Absolute layer targets are unsafe until the lift can home and report that
    # it has reached its commanded physical position.
    liftAbsolutePositioningReady: bool = False


class RobotArmDiagnostics(BaseModel):
    """Latest non-actuating arm feedback diagnostic reported by the robot."""

    connected: bool = False
    feedbackHealthy: bool = False
    canFps: float = 0.0
    feedbackReason: str = "机械臂状态尚未读取"


class RobotPose(BaseModel):
    frameId: Literal["map"]
    x: float
    y: float
    yaw: float
    capturedAt: datetime


class RobotSummary(BaseModel):
    id: str
    name: str
    status: Literal["idle", "navigating", "charging", "manual", "fault", "offline", "busy", "mapping"]
    location: str
    currentTask: str
    mapVersion: str
    mapId: str | None = None
    pose: RobotPose | None = None
    localizationStatus: Literal[
        "starting",
        "restoring_saved_pose",
        "global_localizing",
        "manual_initializing",
        "localized",
        "manual_required",
        "unknown",
    ] = "unknown"
    localizationScanMode: Literal[
        "dual", "single_laser_1", "single_laser_2", "unavailable", "unknown"
    ] = "unknown"
    lastSeenAt: datetime | None = None
    health: RobotHealth
    armDiagnostics: RobotArmDiagnostics = Field(default_factory=RobotArmDiagnostics)
    updatedAt: str


class CreateRobotRequest(BaseModel):
    id: str = Field(pattern=r"^LR-[A-Za-z0-9_-]+$", max_length=80)
    name: str = Field(min_length=1, max_length=120)
    gatewayHost: str = Field(min_length=1, max_length=255)


TaskStatus = Literal["queued", "running", "paused", "failed", "completed", "cancelled", "recovery_required"]


class DispatchTask(BaseModel):
    id: str
    type: Literal[
        "borrow", "return", "inventory", "navigate", "cruise", "charge", "arm_test", "takeout",
        "fine_tune", "multi_round",
    ]
    robotId: str
    target: str
    status: TaskStatus
    progress: int = Field(ge=0, le=100)
    updatedAt: str
    mapId: str | None = None
    annotationId: str | None = None
    shelfId: str | None = None
    routeAnnotationIds: list[str] = Field(default_factory=list)
    loopCount: int | None = Field(default=None, ge=1, le=10)
    bookId: str | None = None
    bookTitle: str | None = None
    workflowMode: Literal["takeout", "putback", "putback2", "tail_putback"] | None = None
    deliveryAnnotationId: str | None = None
    deliveryLabel: str | None = None
    workflowId: str | None = None
    workflowStep: int | None = Field(default=None, ge=0)
    workflowStepCount: int | None = Field(default=None, ge=1)
    workflowStepName: str | None = None
    workflowMessage: str | None = None
    workflowDetails: dict[str, Any] = Field(default_factory=dict)
    requiresConfirmation: bool = False
    failureReason: str | None = None


class AlertRecord(BaseModel):
    id: str
    severity: Literal["critical", "warning", "info"]
    source: str
    title: str
    time: str
    acknowledged: bool


class MoveLiftRequest(BaseModel):
    distanceCm: float = Field(ge=-10.0, le=10.0)
    acknowledgeSafety: Literal[True]


class MapOrigin(BaseModel):
    x: float
    y: float
    yaw: float


class MapImportRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    yamlName: str = Field(min_length=1, max_length=255)
    yamlContent: str = Field(min_length=1)
    pgmName: str = Field(min_length=1, max_length=255)
    pgmContent: str = Field(min_length=1)
    # Optional explicit id so a robot-registered map keeps its fleet identity.
    # Accepts generated hex ids as well as vehicle-registered names such as
    # MAP-1813LAB, matching the map-id pattern used by the vehicle routes.
    id: str | None = Field(default=None, pattern=r"^MAP-[A-Za-z0-9_-]{1,80}$")


class MapVersion(BaseModel):
    id: str
    name: str
    version: str
    yamlFile: str
    pgmFile: str
    width: int
    height: int
    resolution: float
    origin: MapOrigin
    mode: Literal["trinary", "scale", "raw"]
    negate: bool
    occupiedThreshold: float
    freeThreshold: float
    status: Literal["draft", "published"]
    createdAt: datetime
    createdBy: str


MapAnnotationKind = Literal["zone", "shelf", "charging", "delivery", "dock", "cruise"]


class CreateMapAnnotationRequest(BaseModel):
    kind: MapAnnotationKind
    label: str = Field(min_length=1, max_length=120)
    pixelX: int = Field(ge=0)
    pixelY: int = Field(ge=0)
    yaw: float = Field(default=0.0, ge=-6.284, le=6.284)


class UpdateMapAnnotationRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    # Optional ROS map coordinates in meters; converted back to image pixels on save.
    x: float | None = Field(default=None, allow_inf_nan=False)
    y: float | None = Field(default=None, allow_inf_nan=False)
    yaw: float | None = Field(default=None, ge=-6.284, le=6.284, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> Self:
        if self.label is None and self.x is None and self.y is None and self.yaw is None:
            raise ValueError("至少提供一个待更新字段")
        return self


class MapAnnotation(BaseModel):
    id: str
    mapId: str
    kind: MapAnnotationKind
    label: str
    pixelX: int
    pixelY: int
    mapX: float
    mapY: float
    yaw: float
    createdAt: datetime
    createdBy: str


class MapAnnotationResult(MapAnnotation):
    """Create-annotation response; the warning flag never persists."""

    labelWarning: str | None = None


class CruiseSyncPreviewItem(BaseModel):
    """One cruise-config point and what a sync would do with it."""

    name: str
    x: float
    y: float
    yaw: float
    pixelX: int | None = None
    pixelY: int | None = None
    action: Literal["create", "skip"]
    reason: str | None = None


class CruiseSyncPreview(BaseModel):
    file: str
    items: list[CruiseSyncPreviewItem]


class CompleteTakeoutTarget(BaseModel):
    """Navigation goal in the SLAM map frame; yaw follows the map convention."""

    x: float
    y: float
    yaw: float = Field(default=0.0, ge=-6.284, le=6.284)


class CompleteTakeoutRequest(BaseModel):
    """Navigate to a shelf target, then run the on-robot takeout workflow.
    With sendNavigation (default) the robot navigates to the target first;
    without it the arm starts in place."""

    robotId: str = Field(min_length=1, max_length=80)
    templateId: str = Field(min_length=1, max_length=80)
    target: CompleteTakeoutTarget | None = None
    liftHeightMm: float = Field(ge=10.0, le=1700.0)
    # Physical book-stand slot the book is placed into; occupancy is not
    # tracked (books may be placed manually), so the slot is always explicit.
    destinationSlot: int = Field(ge=1, le=5)
    liftSpeedLevel: int = Field(default=2, ge=1, le=3)
    bookId: str | None = Field(default=None, max_length=80)
    # Optional map context for the navigate command; defaults to the robot's
    # currently loaded map.
    mapId: str | None = Field(default=None, max_length=80)
    annotationId: str | None = Field(default=None, max_length=80)
    sendNavigation: bool = True

    @model_validator(mode="after")
    def require_target_when_navigating(self) -> Self:
        if self.sendNavigation and self.target is None:
            raise ValueError("发送导航时必须提供目标点")
        return self


class CompleteReturnRequest(BaseModel):
    """Navigate to a shelf target, then run the on-robot return workflow.
    returnMode picks the bookarm workflow: mid (default, /midreturn/tasks) or
    end (末尾还书, /return/tasks). With sendNavigation (default) the robot
    navigates to the target first; without it the arm starts in place."""

    robotId: str = Field(min_length=1, max_length=80)
    # Spine-library template of the book the returned book goes left of.
    templateId: str = Field(min_length=1, max_length=80)
    target: CompleteTakeoutTarget | None = None
    liftHeightMm: float = Field(ge=10.0, le=1700.0)
    # The book-stand slot holding the book to return (occupancy is not tracked).
    sourceSlot: int = Field(ge=1, le=5)
    liftSpeedLevel: int = Field(default=2, ge=1, le=3)
    bookId: str | None = Field(default=None, max_length=80)
    mapId: str | None = Field(default=None, max_length=80)
    annotationId: str | None = Field(default=None, max_length=80)
    sendNavigation: bool = True
    # 还书模式：mid 中位还书（默认，保持现有行为，走 bookarm /midreturn/tasks）；
    # end 末尾还书（走 bookarm /return/tasks，不支持注入抓取位姿）。
    returnMode: Literal["mid", "end"] = "mid"
    # Stress-test shortcut: reuse a previously recognized grasp pose (6 values:
    # x/y/z metres + rx/ry/rz radians) as the insert anchor, skipping the
    # neighbor grasp+put-back and the fresh re-target (mid-return step 24 on).
    injectedTargetGraspPose: list[float] | None = Field(default=None, min_length=6, max_length=6)

    @model_validator(mode="after")
    def require_target_when_navigating(self) -> Self:
        if self.sendNavigation and self.target is None:
            raise ValueError("发送导航时必须提供目标点")
        return self

    @model_validator(mode="after")
    def reject_injected_pose_for_end_return(self) -> Self:
        if self.returnMode == "end" and self.injectedTargetGraspPose is not None:
            raise ValueError("末尾还书不支持注入抓取位姿")
        return self


class Book(BaseModel):
    """A managed book: name, spine-library template (its spine image), and
    the shelf height used as the lift target when taking or returning it."""

    id: str
    name: str = Field(min_length=1, max_length=80)
    templateId: str = Field(min_length=1, max_length=80)
    heightMm: float = Field(ge=10.0, le=1700.0)
    # 取书点（地图标注 id）：取书/还书面板选书后自动带出的导航目标。
    annotationId: str | None = Field(default=None, max_length=80)
    # 常用书立槽位（可空）：选书后自动带出，用户可改。
    preferredSlot: int | None = Field(default=None, ge=1, le=5)
    createdAt: str
    updatedAt: str


class BookUpsert(BaseModel):
    """Create/update payload for a managed book."""

    name: str = Field(min_length=1, max_length=80)
    templateId: str = Field(min_length=1, max_length=80)
    heightMm: float = Field(ge=10.0, le=1700.0)
    annotationId: str | None = Field(default=None, max_length=80)
    preferredSlot: int | None = Field(default=None, ge=1, le=5)


class StressTestTakeoutAction(BaseModel):
    """压测单步取书：把书取到书立的指定槽位；升降高度按书籍登记高度。"""

    bookId: str = Field(min_length=1, max_length=80)
    destinationSlot: int = Field(ge=1, le=5)


class StressTestReturnAction(BaseModel):
    """压测单步中位还书：从书立槽位取回该书，识别目标书后还到其左边。"""

    sourceSlot: int = Field(ge=1, le=5)
    referenceBookId: str = Field(min_length=1, max_length=80)


class StressTestTemplateConfig(BaseModel):
    """压测单循环配置：导航到目标点 → 按序取书入槽 → 按序中位还书（还到目标书
    左边）→ 收拢（失能机械臂 + 降升降台）→ 直退 1.5 米（不走导航）收尾。"""

    mapId: str = Field(min_length=1, max_length=80)
    targetAnnotationId: str = Field(min_length=1, max_length=80)
    takeouts: list[StressTestTakeoutAction] = Field(min_length=1, max_length=5)
    returns: list[StressTestReturnAction] = Field(default_factory=list, max_length=5)
    liftSpeedLevel: int = Field(default=3, ge=1, le=3)
    maxRounds: int | None = Field(default=None, ge=1)
    stopOnFailure: bool = True

    @model_validator(mode="after")
    def returns_match_takeout_slots(self) -> Self:
        slots = [takeout.destinationSlot for takeout in self.takeouts]
        if len(set(slots)) != len(slots):
            raise ValueError("取书目标槽位不能重复")
        return_slots = [action.sourceSlot for action in self.returns]
        if len(set(return_slots)) != len(return_slots):
            raise ValueError("还书槽位不能重复")
        for action in self.returns:
            if action.sourceSlot not in slots:
                raise ValueError(f"还书槽位 {action.sourceSlot} 没有对应的取书动作")
        return self


class StressTestTemplate(BaseModel):
    """一份保存的压测任务模板（按名称复用单循环配置）。"""

    id: str
    name: str = Field(min_length=1, max_length=120)
    config: StressTestTemplateConfig
    createdAt: str
    updatedAt: str


class StressTestTemplateUpsert(BaseModel):
    """Create/update payload for a stress-test task template."""

    name: str = Field(min_length=1, max_length=120)
    config: StressTestTemplateConfig


class BookImportFailure(BaseModel):
    """One rejected row of a book CSV batch import."""

    # CSV 文件行号（1 起，含表头），方便操作员回到原文件定位。
    row: int
    reason: str


class BookImportResult(BaseModel):
    """Summary of a book CSV batch import."""

    total: int
    succeeded: int
    failed: list[BookImportFailure]


class BookImportRequest(BaseModel):
    """Batch book import payload: one CSV plus a zip of spine images, both
    Base64-encoded like the map import (keeps the API JSON-only)."""

    csvName: str = Field(min_length=1, max_length=160)
    csvContent: str
    imagesName: str = Field(min_length=1, max_length=160)
    imagesContent: str


class MultiRoundRequest(BaseModel):
    """Take up to five books into stand slots N..1 (inside-out; three books
    -> slots 3/2/1) and return slots 1..M to their shelf targets (M <= N).
    With sendNavigation (default) the robot navigates to the target once
    first; without it the arm starts in place."""

    robotId: str = Field(min_length=1, max_length=80)
    target: CompleteTakeoutTarget | None = None
    takeBookIds: list[str] = Field(min_length=1, max_length=5)
    returnBookIds: list[str] = Field(default_factory=list, max_length=5)
    liftSpeedLevel: int = Field(default=2, ge=1, le=3)
    mapId: str | None = Field(default=None, max_length=80)
    annotationId: str | None = Field(default=None, max_length=80)
    sendNavigation: bool = True

    @model_validator(mode="after")
    def require_target_when_navigating(self) -> Self:
        if self.sendNavigation and self.target is None:
            raise ValueError("发送导航时必须提供目标点")
        return self


CommandName = Literal[
    "navigate",
    "fine_tune",
    "cruise",
    "set_initial_pose",
    "auto_localize",
    "charge",
    "pause",
    "resume",
    "cancel",
    "emergency_stop",
    "stop",
    "start_manual_control",
    "manual_jog",
    "stop_manual_control",
    "book_task",
    "book_task_continue",
    "book_task_cancel",
    "bookarm_test_start",
    "bookarm_test_continue",
    "bookarm_test_cancel",
    "lift_move",
    "switch_map",
]


class SetInitialPoseRequest(BaseModel):
    """A browser operator's confirmation of where a stationary robot is on its map."""

    mapId: str = Field(min_length=1, max_length=80)
    frameId: Literal["map"] = "map"
    x: float
    y: float
    yaw: float = Field(ge=-6.284, le=6.284)


class SwitchMapRequest(BaseModel):
    """An operator's request to load another registered map on the robot."""

    mapId: str = Field(min_length=1, max_length=80)


class MappingStatusResponse(BaseModel):
    """Mode report from the on-robot mapping agent."""

    mode: Literal["navigation", "mapping", "transition"]
    slamRunning: bool
    message: str
    # The agent sets this after a SLAM restart wiped the unsaved session.
    slamRestarted: bool = False


class MappingActionResponse(BaseModel):
    """Acknowledgement returned by the agent for start/stop actions."""

    ok: bool
    mode: str


class MappingJogRequest(BaseModel):
    """Velocity setpoint forwarded to the agent while mapping."""

    linear: float = Field(ge=-1.0, le=1.0)
    angular: float = Field(ge=-2.0, le=2.0)


class SaveMappingRequest(BaseModel):
    """Optional display name for a freshly built map imported into the library."""

    name: str | None = Field(default=None, max_length=120)


class SaveMappingResponse(MapVersion):
    """Library record plus shelf-mark import counters for one mapping save."""

    importedMarks: int = 0
    skippedMarks: int = 0
    # True when the agent had already saved this mapping session: the repeated
    # save returns the first mapId instead of building a second map.
    alreadySaved: bool = False


class MapImportResult(MapVersion):
    """Import outcome with the best-effort vehicle-side registration result."""

    vehicleRegistered: bool = False
    vehicleRegisterNote: str | None = None


class MappingPoseResponse(BaseModel):
    """Current robot pose in the SLAM map frame, reported by the agent."""

    ok: bool
    x: float
    y: float
    yaw: float


class MappingMark(BaseModel):
    """One shelf mark recorded on the robot while mapping, in map coordinates."""

    x: float
    y: float
    yaw: float
    label: str
    at: float


class MappingMarksResponse(BaseModel):
    marks: list[MappingMark]


class MappingMarkRequest(BaseModel):
    """Optional label for a new mark at the robot's current pose."""

    label: str | None = Field(default=None, max_length=120)


class MappingMarkResponse(BaseModel):
    mark: MappingMark
    count: int


class MappingMarksClearResponse(BaseModel):
    ok: bool
    cleared: int


class RobotCommandRequest(BaseModel):
    command: CommandName
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotencyKey: str | None = Field(default=None, min_length=8, max_length=128)
    expiresAt: datetime | None = None


class RobotCommand(BaseModel):
    commandId: str
    robotId: str
    command: CommandName
    payload: dict[str, Any]
    taskId: str | None = None
    expiresAt: datetime
    idempotencyKey: str
    accepted: bool = True
    status: Literal["queued", "rejected"] = "queued"
    # Operator-facing outcome note (e.g. the E-stop arm-interlock result).
    # Response-only: not persisted to the robot_commands table.
    detail: str | None = None


class ResetEmergencyStopRequest(BaseModel):
    """Explicit acknowledgement before clearing only the platform's E-stop lock."""

    acknowledgeSafety: Literal[True]


class ManualJogRequest(BaseModel):
    """免鉴权直控点动参数；安全上限由服务端在 store.send_manual_jog 校验。"""

    linear: float
    angular: float
    durationMs: int


class CurrentUserResponse(BaseModel):
    user: User


class ErrorResponse(BaseModel):
    detail: str
