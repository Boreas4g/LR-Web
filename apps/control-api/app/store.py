from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread, Timer
from typing import Any
from urllib import request as urllib_request
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from .cache import RedisCache
from .config import DEFAULT_CRUISE_POINTS_FILE, Settings
from .db import (
    DEADLOCK_RETRY_DELAYS_S,
    AlertModel,
    BookModel,
    CommandModel,
    Database,
    MapAnnotationModel,
    MapVersionModel,
    RobotModel,
    StressTestTemplateModel,
    TaskModel,
    UserModel,
    is_deadlock,
)
from .map_files import (
    CruisePoint,
    decode_base64,
    parse_cruise_points_yaml,
    parse_pgm,
    parse_ros_map_yaml,
    pgm_preview_png,
)
from .friendly_errors import (
    describe_bookarm_detail,
    describe_bookarm_failure,
    describe_bookarm_state,
    describe_navigation_failure,
    describe_task_status,
)
from .mqtt import MqttPublisher
from .schemas import (
    AlertRecord,
    Book,
    BookUpsert,
    CompleteReturnRequest,
    CompleteTakeoutRequest,
    CreateMapAnnotationRequest,
    CruiseSyncPreview,
    CruiseSyncPreviewItem,
    DispatchTask,
    ManualJogRequest,
    MapAnnotation,
    MapAnnotationResult,
    MapImportRequest,
    MapOrigin,
    MapVersion,
    MoveLiftRequest,
    MultiRoundRequest,
    ResetEmergencyStopRequest,
    RobotArmDiagnostics,
    RobotCommand,
    RobotCommandRequest,
    RobotPose,
    RobotSummary,
    SetInitialPoseRequest,
    StressTestTemplate,
    StressTestTemplateConfig,
    StressTestTemplateUpsert,
    SwitchMapRequest,
    UpdateMapAnnotationRequest,
    User,
)


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return f"{salt}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        salt, expected = encoded.split("$", 1)
        return secrets.compare_digest(_hash_password(password, salt).split("$", 1)[1], expected)
    except ValueError:
        return False


class _FlowCancelled(Exception):
    """Internal: the operator cancelled a complete-takeout/return orchestration."""


class AnnotationInUseError(ValueError):
    """A live (queued/running/paused) task still references the annotation."""


class StressTestTemplateNameConflictError(ValueError):
    """Another stress-test template already carries the requested name."""


class _BookarmRecoveryRequired(Exception):
    """Internal: the bookarm sub-task ended in RECOVERY_REQUIRED (arm may be
    holding a book mid-way) and the flow must surface a manual-recovery state
    instead of a plain failure."""


def _now_iso() -> str:
    """Real timestamp for task/robot updatedAt so the frontend can order by it."""
    return datetime.now(UTC).isoformat()


# Task states eligible for retention pruning; active and recovery-required
# tasks are never pruned.
_TASK_PRUNE_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _parse_task_updated_at(value: str) -> datetime | None:
    """Parse an ISO updatedAt; legacy display strings (e.g.「刚刚」) yield None."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


_logger = logging.getLogger(__name__)


class InMemoryStore:
    """Development store; replace this boundary with PostgreSQL/Redis repositories."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.database: Database | None = None
        self.cache: RedisCache | None = None
        self.publisher: MqttPublisher | None = None
        self._configured = False
        self.require_mqtt = False
        self.single_operator = False
        self.command_ttl_seconds = 30
        self.cruise_command_ttl_seconds = 14400
        self.control_lock_seconds = 45
        self.map_storage_dir = Path("data/maps")
        self.multi_round_vision_dir = Path("data/multi-round-vision")
        self.cruise_points_file = Path(DEFAULT_CRUISE_POINTS_FILE)
        # 巡航点注入总开关：默认关闭，导入任何地图都不会被注入开发样例点。
        self.cruise_points_enabled = False
        self.map_versions: list[MapVersion] = []
        self.map_annotations: dict[str, list[MapAnnotation]] = {}
        self.users: dict[str, tuple[User, str]] = {}
        self.sessions: dict[str, User] = {}
        self.commands: dict[str, RobotCommand] = {}
        self.idempotency: dict[str, RobotCommand] = {}
        # Complete-takeout orchestration: one cancellation event per live task.
        self._takeout_cancels: dict[str, Event] = {}
        # Complete-return orchestration: one cancellation event per live task.
        self._return_cancels: dict[str, Event] = {}
        # Multi-round take/return orchestration: one cancellation event per live task.
        self._multi_round_cancels: dict[str, Event] = {}
        # Map-switch outcomes reported by the vehicle, keyed by command id:
        # (succeeded|failed|rejected, reason). Consumed by the synchronous
        # switch endpoint so the operator gets a direct result per click.
        self._map_switch_outcomes: dict[str, tuple[str, str]] = {}
        # Annotation navigation orchestration: one cancellation event per live task.
        self._annotation_nav_cancels: dict[str, Event] = {}
        # Managed books (name + spine template + shelf height).
        self.books: dict[str, Book] = {}
        # Saved stress-test task templates (single-loop configs by name).
        self.stress_test_templates: dict[str, StressTestTemplate] = {}
        # On-robot bookarm service access (unix socket HTTP). The factory is a
        # test seam; production builds a real client per call.
        self.bookarm_socket_path = "/run/lr-bookarm/bookarm.sock"
        self.bookarm_token: str | None = None
        self.bookarm_client_factory: Callable[[], Any] | None = None
        # Bookarm health poller: when active, the control station owns
        # arm/rod/lift/camera health (the vehicle gateway's legacy HTTP reader
        # is dead since the bookarm service moved to a unix socket).
        self._bookarm_health_stop: Event | None = None
        self._bookarm_health_thread: Thread | None = None
        self._bookarm_health_active = False
        # Mapping-agent mode mirror: polls /mapping/status and reflects the
        # vehicle's mapping mode onto the robot status (mutual exclusion).
        self.robot_agent_url = ""
        self.mapping_status_fetcher: Callable[[], dict[str, Any]] | None = None
        self.mapping_sync_interval_seconds = 10.0
        self._mapping_sync_stop: Event | None = None
        self._mapping_sync_thread: Thread | None = None
        # 单机交付（控制站与唯一机器人同机）：不做遥测中断离线判定，
        # 机器人状态只随真实遥测/事件更新。
        # Task-list retention: per type keep at most the newest
        # task_retention_per_type terminal tasks and none older than
        # task_retention_days; pruning runs at startup and periodically.
        self.task_retention_per_type = 200
        self.task_retention_days = 30
        self._task_prune_interval_seconds = 3600.0
        self._task_prune_stop: Event | None = None
        self._task_prune_thread: Thread | None = None
        # 取还书编排的轮询节拍（本机 unix socket / 内存查询，开销极小）：
        # 过大会让每个动作的结束判定白白多等一个周期。
        self.takeout_poll_interval_s = 0.5
        self.takeout_nav_timeout_s = 300.0
        self.takeout_arm_timeout_s = 600.0
        self._robot_update_listener: Callable[[RobotSummary], None] | None = None
        # Debounced persistence: bursty mutations within persist_delay_seconds
        # collapse into a single full _persist_all() write-through.
        self.persist_delay_seconds = 0.5
        self._persist_timer: Timer | None = None
        self._persist_timer_lock = Lock()
        # Last telemetry state actually persisted/broadcast per robot, used to
        # throttle high-frequency frames that barely change the robot view.
        self._telemetry_published: dict[str, tuple[datetime, RobotSummary]] = {}
        # Throttles repeated telemetry-persistence error logs while the DB is
        # unhealthy (monotonic timestamp of the last emitted log).
        self._last_telemetry_persist_error_log = 0.0
        self.audit: list[dict[str, str]] = []
        self._seed()

    def configure(self, settings: Settings) -> None:
        if self._configured:
            return
        self.command_ttl_seconds = settings.command_ttl_seconds
        self.cruise_command_ttl_seconds = settings.cruise_command_ttl_seconds
        self.control_lock_seconds = settings.control_lock_seconds
        self.require_mqtt = settings.require_mqtt
        self.single_operator = settings.single_operator
        self.persist_delay_seconds = settings.persist_delay_seconds
        self.bookarm_socket_path = settings.bookarm_socket_path
        self.bookarm_token = settings.bookarm_token
        self.robot_agent_url = settings.robot_agent_url
        self.map_storage_dir = Path(settings.map_storage_dir).expanduser().resolve()
        self.map_storage_dir.mkdir(parents=True, exist_ok=True)
        self.multi_round_vision_dir = self.map_storage_dir.parent / "multi-round-vision"
        self.multi_round_vision_dir.mkdir(parents=True, exist_ok=True)
        self.cruise_points_file = Path(settings.cruise_points_file).expanduser()
        self.cruise_points_enabled = settings.cruise_points_enabled
        if settings.use_postgres:
            self.database = Database(settings)
            self.database.initialize()
            if self.database.has_users():
                self._load_from_database()
            else:
                self._schedule_persist()
        if settings.redis_url:
            self.cache = RedisCache(settings.redis_url)
            self.cache.ping()
        if settings.mqtt_url:
            self.publisher = MqttPublisher(settings, self.handle_gateway_event)
            self.publisher.start()
        self._start_bookarm_health_poller()
        self._start_mapping_sync_poller()
        self._prune_tasks()
        self._start_task_prune_watch()
        self._configured = True

    def list_robots(self) -> list[RobotSummary]:
        with self._lock:
            return list(self.robots)

    def shutdown(self) -> None:
        if self._bookarm_health_stop is not None:
            self._bookarm_health_stop.set()
        if self._mapping_sync_stop is not None:
            self._mapping_sync_stop.set()
        if self._task_prune_stop is not None:
            self._task_prune_stop.set()
        if self.publisher:
            self.publisher.stop()
        self._configured = False

    def _start_bookarm_health_poller(self) -> None:
        """Poll the on-robot bookarm service for arm/rod/lift/camera health."""
        if not self.bookarm_socket_path:
            return
        self._bookarm_health_active = True
        self._bookarm_health_stop = Event()
        self._bookarm_health_thread = Thread(
            target=self._bookarm_health_loop, name="bookarm-health", daemon=True
        )
        self._bookarm_health_thread.start()

    def _bookarm_health_loop(self) -> None:
        while self._bookarm_health_stop is not None and not self._bookarm_health_stop.wait(3.0):
            try:
                status = self._bookarm_client().get_hardware_status()
            except Exception:
                status = None
            self._apply_bookarm_health(status)

    def _apply_bookarm_health(self, status: dict[str, Any] | None) -> None:
        if status is None:
            arm_ready = rod_ready = lift_ready = camera_ready = False
            diagnostic = RobotArmDiagnostics(
                connected=False,
                feedbackHealthy=False,
                canFps=0.0,
                feedbackReason="bookarm 服务不可达",
            )
        else:
            arm_info = status.get("arm") if isinstance(status.get("arm"), dict) else {}
            arm_health = arm_info.get("health") if isinstance(arm_info.get("health"), dict) else {}
            arm_ready = bool(arm_health.get("healthy_feedback"))
            rod_info = status.get("rod") if isinstance(status.get("rod"), dict) else {}
            rod_ready = bool(rod_info.get("connected"))
            lift_info = status.get("lift") if isinstance(status.get("lift"), dict) else {}
            lift_ready = bool(lift_info.get("connected"))
            camera_info = status.get("camera") if isinstance(status.get("camera"), dict) else {}
            camera_ready = bool(camera_info.get("present"))
            can_fps = arm_health.get("can_fps")
            diagnostic = RobotArmDiagnostics(
                connected=bool(arm_info.get("wired")),
                feedbackHealthy=arm_ready,
                canFps=float(can_fps) if isinstance(can_fps, (int, float)) else 0.0,
                feedbackReason=str(arm_health.get("reason") or "正常")[:240],
            )
        with self._lock:
            for index, robot in enumerate(self.robots):
                self.robots[index] = robot.model_copy(
                    update={
                        "health": robot.health.model_copy(
                            update={
                                "arm": arm_ready,
                                "rod": rod_ready,
                                "lift": lift_ready,
                                "camera": camera_ready,
                            }
                        ),
                        "armDiagnostics": diagnostic,
                    }
                )

    def _start_mapping_sync_poller(self) -> None:
        """Mirror the on-robot mapping agent's mode onto the robot status."""
        if not self.robot_agent_url:
            return
        self._mapping_sync_stop = Event()
        self._mapping_sync_thread = Thread(
            target=self._mapping_sync_loop, name="mapping-sync", daemon=True
        )
        self._mapping_sync_thread.start()

    def _mapping_sync_loop(self) -> None:
        while self._mapping_sync_stop is not None and not self._mapping_sync_stop.wait(
            self.mapping_sync_interval_seconds
        ):
            try:
                fetcher = self.mapping_status_fetcher or self._fetch_mapping_status
                status = fetcher()
            except Exception:
                # 代理短暂不可达（建图切换期间会重启服务）不改变机器人状态。
                continue
            mode = status.get("mode") if isinstance(status, dict) else None
            if isinstance(mode, str):
                try:
                    self.sync_mapping_mode(mode)
                except Exception:
                    _logger.exception("建图模式同步失败")

    def _fetch_mapping_status(self) -> dict[str, Any]:
        request = urllib_request.Request(
            f"{self.robot_agent_url.rstrip('/')}/mapping/status", method="GET"
        )
        with urllib_request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {}

    def sync_mapping_mode(self, mode: str) -> None:
        """Reflect the vehicle mapping agent's mode on the robot status.

        The appliance drives a single vehicle, so the agent's global mode
        applies to every robot that is not faulted/offline. While mapping, the
        vehicle's navigation stack is stopped, which is why the status is a
        first-class mutual-exclusion state. Arm-busy robots keep their status:
        the orchestration owns it until release.
        """
        if mode not in {"navigation", "mapping", "transition"}:
            return
        changed: list[RobotSummary] = []
        with self._lock:
            for index, robot in enumerate(self.robots):
                if mode == "mapping" and robot.status not in {"fault", "offline", "busy", "mapping"}:
                    updated = robot.model_copy(
                        update={
                            "status": "mapping",
                            "currentTask": "手动建图中（导航栈已停止）",
                            "updatedAt": _now_iso(),
                        }
                    )
                elif mode == "navigation" and robot.status == "mapping":
                    updated = robot.model_copy(
                        update={
                            "status": "idle",
                            "currentTask": "建图已结束，导航栈已恢复",
                            "updatedAt": _now_iso(),
                        }
                    )
                else:
                    continue
                self.robots[index] = updated
                changed.append(updated)
            if changed:
                self._schedule_persist()
                for robot in changed:
                    self._audit("mapping-agent", f"mapping_mode_{mode}", robot.id)
        for robot in changed:
            self._notify_robot_update(robot)

    def dependency_status(self) -> dict[str, str]:
        return {
            "postgres": "connected" if self.database else "memory",
            "redis": "connected" if self.cache else "memory",
            "mqtt": "connected" if self.publisher and self.publisher.connected else "disconnected",
        }

    def list_tasks(self, statuses: list[str] | None = None, limit: int | None = None) -> list[DispatchTask]:
        """Newest-first task list, optionally narrowed for polling clients.

        ``statuses`` keeps every task in one of the given states; ``limit``
        additionally keeps the newest ``limit`` tasks regardless of state, so
        a task that just reached a terminal state still shows up for pollers
        tracking it. No arguments returns the full list (legacy behaviour).
        """
        with self._lock:
            tasks = list(self.tasks)
        if not statuses and limit is None:
            return tasks
        allowed = set(statuses or [])
        recent_ids = {task.id for task in tasks[:limit]} if limit is not None else set()
        return [task for task in tasks if task.status in allowed or task.id in recent_ids]

    def acknowledge_alert(self, alert_id: str, user: User) -> AlertRecord:
        """Mark one alert acknowledged (idempotent) and persist the change."""
        with self._lock:
            index = next((i for i, item in enumerate(self.alerts) if item.id == alert_id), None)
            if index is None:
                raise ValueError("告警不存在")
            alert = self.alerts[index]
            if not alert.acknowledged:
                alert = alert.model_copy(update={"acknowledged": True})
                self.alerts[index] = alert
                self._schedule_persist()
                self._audit(user.username, "acknowledge_alert", alert_id)
            return alert

    def _start_task_prune_watch(self) -> None:
        self._task_prune_stop = Event()
        self._task_prune_thread = Thread(
            target=self._task_prune_loop, name="task-prune-watch", daemon=True
        )
        self._task_prune_thread.start()

    def _task_prune_loop(self) -> None:
        while self._task_prune_stop is not None and not self._task_prune_stop.wait(
            self._task_prune_interval_seconds
        ):
            try:
                self._prune_tasks()
            except Exception:
                _logger.exception("任务列表定期裁剪失败")

    def _prune_tasks(self, now: datetime | None = None) -> int:
        """Drop terminal tasks beyond the per-type retention (newest N / age in days).

        Only completed/failed/cancelled tasks are eligible: active and
        recovery-required tasks are always kept, as is any task whose
        updatedAt cannot be parsed. Persistence is scheduled only when
        something was actually removed. Returns the number of removed tasks.
        """
        now = datetime.now(UTC) if now is None else now
        cutoff = now - timedelta(days=self.task_retention_days)
        with self._lock:
            terminal_by_type: dict[str, list[DispatchTask]] = {}
            for task in self.tasks:
                if task.status in _TASK_PRUNE_TERMINAL_STATUSES:
                    terminal_by_type.setdefault(task.type, []).append(task)
            drop: set[str] = set()
            for type_tasks in terminal_by_type.values():
                ranked = sorted(
                    type_tasks,
                    key=lambda task: _parse_task_updated_at(task.updatedAt) or datetime.min.replace(tzinfo=UTC),
                    reverse=True,
                )
                for rank, task in enumerate(ranked):
                    if rank >= self.task_retention_per_type:
                        drop.add(task.id)
                        continue
                    updated = _parse_task_updated_at(task.updatedAt)
                    if updated is not None and updated < cutoff:
                        drop.add(task.id)
            if not drop:
                return 0
            removed = len(drop)
            self.tasks = [task for task in self.tasks if task.id not in drop]
            self._schedule_persist()
            self._audit("system", "prune_tasks", f"removed:{removed}")
        return removed

    def health_status(self) -> str:
        """Mark a production station degraded when its required robot link is down."""
        if self.require_mqtt and (not self.publisher or not self.publisher.connected):
            return "degraded"
        return "ok"

    def set_robot_update_listener(self, listener: Callable[[RobotSummary], None] | None) -> None:
        self._robot_update_listener = listener

    def _notify_robot_update(self, robot: RobotSummary) -> None:
        listener = self._robot_update_listener
        if listener:
            listener(robot)

    def _load_from_database(self) -> None:
        if not self.database:
            return
        with self.database.session() as session:
            self.users = {
                item.username: (User(username=item.username, name=item.name, role=item.role), item.password_hash)
                for item in session.scalars(select(UserModel))
            }
            self.robots = [
                RobotSummary(
                    id=item.id,
                    name=item.name,
                    status=item.status,
                    location=item.location,
                    currentTask=item.current_task,
                    mapVersion=item.map_version,
                    mapId=item.map_id,
                    pose=item.pose,
                    localizationStatus=item.localization_status,
                    localizationScanMode=item.localization_scan_mode,
                    lastSeenAt=item.last_seen_at,
                    health=item.health,
                    armDiagnostics=(item.health or {}).get("armDiagnostics", {}),
                    updatedAt=item.updated_at,
                )
                for item in session.scalars(select(RobotModel))
            ]
            self.tasks = [
                DispatchTask(
                    id=item.id,
                    type=item.type,
                    robotId=item.robot_id,
                    target=item.target,
                    status=item.status,
                    progress=item.progress,
                    updatedAt=item.updated_at,
                    mapId=item.map_id,
                    annotationId=item.annotation_id,
                    shelfId=item.shelf_id,
                    routeAnnotationIds=item.route_annotation_ids or [],
                    loopCount=item.loop_count,
                    bookId=item.book_id,
                    bookTitle=item.book_title,
                    workflowMode=item.workflow_mode,
                    deliveryAnnotationId=item.delivery_annotation_id,
                    deliveryLabel=item.delivery_label,
                    workflowId=item.workflow_id,
                    workflowStep=item.workflow_step,
                    workflowStepCount=item.workflow_step_count,
                    workflowStepName=item.workflow_step_name,
                    workflowMessage=item.workflow_message,
                    workflowDetails=item.workflow_details or {},
                    requiresConfirmation=item.requires_confirmation,
                    failureReason=item.failure_reason,
                )
                for item in session.scalars(select(TaskModel))
            ]
            self.books = {
                item.id: Book(
                    id=item.id,
                    name=item.name,
                    templateId=item.template_id,
                    heightMm=item.height_mm,
                    annotationId=item.annotation_id,
                    preferredSlot=item.preferred_slot,
                    createdAt=item.created_at,
                    updatedAt=item.updated_at,
                )
                for item in session.scalars(select(BookModel))
            }
            self.stress_test_templates = {
                item.id: StressTestTemplate(
                    id=item.id,
                    name=item.name,
                    config=StressTestTemplateConfig.model_validate(item.config),
                    createdAt=item.created_at,
                    updatedAt=item.updated_at,
                )
                for item in session.scalars(select(StressTestTemplateModel))
            }
            self.alerts = [
                AlertRecord(
                    id=item.id,
                    severity=item.severity,
                    source=item.source,
                    title=item.title,
                    time=item.time,
                    acknowledged=item.acknowledged,
                )
                for item in session.scalars(select(AlertModel))
            ]
            self.commands = {
                item.command_id: RobotCommand(
                    commandId=item.command_id,
                    robotId=item.robot_id,
                    command=item.command,
                    payload=item.payload,
                    taskId=item.task_id,
                    expiresAt=item.expires_at,
                    idempotencyKey=item.idempotency_key,
                    status=item.status,
                )
                for item in session.scalars(select(CommandModel))
            }
            self.idempotency = {command.idempotencyKey: command for command in self.commands.values()}
            self.map_versions = [
                MapVersion(
                    id=item.id,
                    name=item.name,
                    version=item.version,
                    yamlFile=item.yaml_file,
                    pgmFile=item.pgm_file,
                    width=item.width,
                    height=item.height,
                    resolution=item.resolution,
                    origin=MapOrigin(x=item.origin[0], y=item.origin[1], yaw=item.origin[2]),
                    mode=item.mode,
                    negate=item.negate,
                    occupiedThreshold=item.occupied_threshold,
                    freeThreshold=item.free_threshold,
                    status=item.status,
                    createdAt=item.created_at,
                    createdBy=item.created_by,
                )
                for item in session.scalars(select(MapVersionModel).order_by(MapVersionModel.created_at.desc()))
            ]
            self.map_annotations = {}
            for item in session.scalars(select(MapAnnotationModel).order_by(MapAnnotationModel.created_at)):
                annotation = MapAnnotation(
                    id=item.id,
                    mapId=item.map_id,
                    kind=item.kind,
                    label=item.label,
                    pixelX=item.pixel_x,
                    pixelY=item.pixel_y,
                    mapX=item.map_x,
                    mapY=item.map_y,
                    yaw=item.yaw,
                    createdAt=item.created_at,
                    createdBy=item.created_by,
                )
                self.map_annotations.setdefault(annotation.mapId, []).append(annotation)
        # Orchestration threads do not survive a process restart: every
        # takeout/return/multi-round task still queued/running/paused after
        # loading is an orphan. Reconcile each one against bookarm's actual
        # task state instead of blindly killing it.
        self._reconcile_orphaned_flows()

    def _reconcile_orphaned_flows(self) -> None:
        """One-by-one reconciliation of orphaned orchestration tasks at startup.

        A task that never reached the arm phase is cancelled outright (its
        in-process orchestrator is gone). A task with a bookarm sub-task is
        synced to bookarm's real state: terminal states map to the matching
        dispatch outcome; a still-running (or unreadable) sub-task parks the
        task in recovery_required with a pointer to the debug page, because
        the arm may physically still be moving or holding a book."""
        orphans = [
            task
            for task in self.tasks
            if task.type in {"takeout", "return", "multi_round"}
            and task.status in {"queued", "running", "paused"}
        ]
        if not orphans:
            return
        client = self._bookarm_client()
        bookarm_unreachable = False
        for task in orphans:
            refs = self._bookarm_task_refs(task)
            if not refs:
                self._update_takeout_task(
                    task.id, status="cancelled", workflowMessage="服务重启，编排已终止"
                )
                continue
            if bookarm_unreachable:
                self._update_takeout_task(
                    task.id,
                    status="recovery_required",
                    workflowMessage="已中断（控制站重启），机械臂状态未知（bookarm 不可达），请到调试页确认",
                )
                continue
            # The live sub-task is the one bookarm still reports as
            # non-terminal; older refs are already terminal history.
            live_state: str | None = None
            live_detail = ""
            reachable = False
            for bookarm_task_id, kind in refs:
                try:
                    view = self._query_bookarm_state(client, bookarm_task_id, kind)
                except Exception as error:
                    if getattr(error, "status", None) is None:
                        # Transport failure: fail fast for the remaining
                        # orphans instead of blocking startup on timeouts.
                        bookarm_unreachable = True
                    continue
                reachable = True
                state = str(view.get("state", ""))
                if state and state not in self._BOOKARM_TERMINAL_STATES:
                    live_state = state
                    live_detail = str(view.get("error") or "")
                    break
            if bookarm_unreachable:
                self._update_takeout_task(
                    task.id,
                    status="recovery_required",
                    workflowMessage="已中断（控制站重启），机械臂状态未知（bookarm 不可达），请到调试页确认",
                )
                continue
            if live_state is not None:
                self._update_takeout_task(
                    task.id,
                    status="recovery_required",
                    workflowMessage="已中断（控制站重启），机械臂可能仍在作业，请到调试页确认",
                )
                continue
            if not reachable:
                # 全部引用都查询失败（非传输错误）：无法证明机械臂已停，同样置恢复提示。
                self._update_takeout_task(
                    task.id,
                    status="recovery_required",
                    workflowMessage="已中断（控制站重启），机械臂状态未知（bookarm 不可达），请到调试页确认",
                )
                continue
            # All referenced sub-tasks are terminal: adopt the last one's outcome.
            bookarm_task_id, kind = refs[-1]
            try:
                view = self._query_bookarm_state(client, bookarm_task_id, kind)
                live_state = str(view.get("state", ""))
                live_detail = str(view.get("error") or "")
            except Exception:
                live_state = None
            if live_state == "succeeded":
                self._update_takeout_task(
                    task.id,
                    status="completed",
                    progress=100,
                    workflowMessage="已完成（控制站重启后与 bookarm 对账确认）",
                )
            elif live_state == "cancelled":
                self._update_takeout_task(
                    task.id, status="cancelled", workflowMessage="已取消（控制站重启后对账确认）"
                )
            elif live_state == "recovery_required":
                self._update_takeout_task(
                    task.id,
                    status="recovery_required",
                    workflowMessage="机械臂中断待确认（可能夹着书）",
                    failureReason=describe_bookarm_failure("recovery_required", live_detail or live_state),
                )
            elif live_state in {"failed", "unsupported"}:
                self._update_takeout_task(
                    task.id,
                    status="failed",
                    workflowMessage="任务失败（控制站重启后与 bookarm 对账确认）",
                    failureReason=describe_bookarm_failure(live_state, live_detail or live_state),
                )
            else:
                self._update_takeout_task(
                    task.id, status="cancelled", workflowMessage="服务重启，编排已终止"
                )

    @staticmethod
    def _sync_table(session: Any, model: type, rows: list[Any]) -> None:
        """Upsert *rows* into *model*'s table and delete rows missing from the snapshot.

        Rows merge in primary-key order so concurrent writers take row locks in
        a consistent order; combined with the single-row telemetry updater this
        removes the lock-ordering cycles that deadlocked the previous
        DELETE-all + INSERT-all rewrite.
        """
        pk_column = next(iter(model.__table__.primary_key.columns))
        ordered = sorted(rows, key=lambda row: getattr(row, pk_column.name))
        desired_pks = {getattr(row, pk_column.name) for row in ordered}
        existing_pks = set(session.scalars(select(pk_column)).all())
        stale_pks = existing_pks - desired_pks
        if stale_pks:
            session.execute(delete(model).where(pk_column.in_(stale_pks)))
        for row in ordered:
            session.merge(row)

    def _persist_all(self) -> None:
        if not self.database:
            return
        # Snapshot under the store lock: MQTT and HTTP threads mutate these
        # collections concurrently, and iterating them unlocked crashed the
        # persist timer with "dictionary changed size during iteration". The DB
        # transaction below runs WITHOUT the lock, so the paho thread holding
        # the lock while waiting on a row lock can never deadlock this writer.
        with self._lock:
            users = [
                UserModel(username=user.username, name=user.name, role=user.role, password_hash=password)
                for user, password in self.users.values()
            ]
            map_versions = [
                MapVersionModel(
                    id=item.id,
                    name=item.name,
                    version=item.version,
                    yaml_file=item.yamlFile,
                    pgm_file=item.pgmFile,
                    width=item.width,
                    height=item.height,
                    resolution=item.resolution,
                    origin=[item.origin.x, item.origin.y, item.origin.yaw],
                    mode=item.mode,
                    negate=item.negate,
                    occupied_threshold=item.occupiedThreshold,
                    free_threshold=item.freeThreshold,
                    status=item.status,
                    created_at=item.createdAt,
                    created_by=item.createdBy,
                )
                for item in self.map_versions
            ]
            annotations = [
                MapAnnotationModel(
                    id=item.id,
                    map_id=item.mapId,
                    kind=item.kind,
                    label=item.label,
                    pixel_x=item.pixelX,
                    pixel_y=item.pixelY,
                    map_x=item.mapX,
                    map_y=item.mapY,
                    yaw=item.yaw,
                    created_at=item.createdAt,
                    created_by=item.createdBy,
                )
                for items in self.map_annotations.values()
                for item in items
            ]
            robots = [
                RobotModel(
                    id=item.id,
                    name=item.name,
                    status=item.status,
                    location=item.location,
                    current_task=item.currentTask,
                    map_version=item.mapVersion,
                    map_id=item.mapId,
                    pose=item.pose.model_dump(mode="json") if item.pose else None,
                    localization_status=item.localizationStatus,
                    localization_scan_mode=item.localizationScanMode,
                    last_seen_at=item.lastSeenAt,
                    health={
                        **item.health.model_dump(),
                        "armDiagnostics": item.armDiagnostics.model_dump(),
                    },
                    updated_at=item.updatedAt,
                )
                for item in self.robots
            ]
            tasks = [
                TaskModel(
                    id=item.id,
                    type=item.type,
                    robot_id=item.robotId,
                    target=item.target,
                    status=item.status,
                    progress=item.progress,
                    updated_at=item.updatedAt,
                    map_id=item.mapId,
                    annotation_id=item.annotationId,
                    shelf_id=item.shelfId,
                    route_annotation_ids=item.routeAnnotationIds,
                    loop_count=item.loopCount,
                    book_id=item.bookId,
                    book_title=item.bookTitle,
                    workflow_mode=item.workflowMode,
                    delivery_annotation_id=item.deliveryAnnotationId,
                    delivery_label=item.deliveryLabel,
                    workflow_id=item.workflowId,
                    workflow_step=item.workflowStep,
                    workflow_step_count=item.workflowStepCount,
                    workflow_step_name=item.workflowStepName,
                    workflow_message=item.workflowMessage,
                    workflow_details=item.workflowDetails,
                    requires_confirmation=item.requiresConfirmation,
                    failure_reason=item.failureReason,
                )
                for item in self.tasks
            ]
            alerts = [
                AlertModel(
                    id=item.id,
                    severity=item.severity,
                    source=item.source,
                    title=item.title,
                    time=item.time,
                    acknowledged=item.acknowledged,
                )
                for item in self.alerts
            ]
            commands = [
                CommandModel(
                    command_id=item.commandId,
                    robot_id=item.robotId,
                    command=item.command,
                    payload=item.payload,
                    task_id=item.taskId,
                    expires_at=item.expiresAt,
                    idempotency_key=item.idempotencyKey,
                    status=item.status,
                )
                for item in self.commands.values()
            ]
            books = [
                BookModel(
                    id=item.id,
                    name=item.name,
                    template_id=item.templateId,
                    height_mm=item.heightMm,
                    annotation_id=item.annotationId,
                    preferred_slot=item.preferredSlot,
                    created_at=item.createdAt,
                    updated_at=item.updatedAt,
                )
                for item in self.books.values()
            ]
            stress_test_templates = [
                StressTestTemplateModel(
                    id=item.id,
                    name=item.name,
                    config=item.config.model_dump(mode="json"),
                    created_at=item.createdAt,
                    updated_at=item.updatedAt,
                )
                for item in self.stress_test_templates.values()
            ]
        for delay in (*DEADLOCK_RETRY_DELAYS_S, None):
            try:
                with self.database.session() as session:
                    self._sync_table(session, UserModel, users)
                    self._sync_table(session, MapVersionModel, map_versions)
                    self._sync_table(session, MapAnnotationModel, annotations)
                    self._sync_table(session, RobotModel, robots)
                    self._sync_table(session, TaskModel, tasks)
                    self._sync_table(session, AlertModel, alerts)
                    self._sync_table(session, CommandModel, commands)
                    self._sync_table(session, BookModel, books)
                    self._sync_table(session, StressTestTemplateModel, stress_test_templates)
                return
            except OperationalError as error:
                if delay is None or not is_deadlock(error):
                    raise
                time.sleep(delay)

    def _schedule_persist(self, delay_s: float | None = None) -> None:
        """Debounce full persistence so bursts collapse into one _persist_all().

        MQTT events arrive on the paho thread while HTTP handlers run on
        uvicorn threads; the timer lock keeps the merge atomic across both.
        A delay of zero preserves the original write-through behaviour.
        """
        if not self.database:
            return
        delay = self.persist_delay_seconds if delay_s is None else delay_s
        if delay <= 0:
            self._persist_all()
            return
        with self._persist_timer_lock:
            if self._persist_timer is None:
                self._persist_timer = Timer(delay, self._run_scheduled_persist)
                self._persist_timer.daemon = True
                self._persist_timer.start()

    def _run_scheduled_persist(self) -> None:
        # Clear the pending handle first so a concurrent _schedule_persist()
        # during the write starts a fresh timer instead of being swallowed.
        with self._persist_timer_lock:
            self._persist_timer = None
        try:
            self._persist_all()
        except Exception:
            # The timer chain must survive: a crash here used to kill the
            # Timer thread silently, after which nothing ever persisted again.
            _logger.exception("定时全量持久化失败，将在下次调度时重试")

    def flush_persist(self) -> None:
        """Cancel any pending debounced persist and write through immediately."""
        with self._persist_timer_lock:
            timer = self._persist_timer
            self._persist_timer = None
        if timer is not None:
            timer.cancel()
        self._persist_all()

    def _audit(self, username: str, action: str, robot_id: str, command_id: str | None = None) -> None:
        record = {
            "time": datetime.now(UTC).isoformat(),
            "user": username,
            "action": action,
            "robotId": robot_id,
            "commandId": command_id or "",
        }
        self.audit.append(record)
        if self.database:
            self.database.audit(username, action, robot_id, command_id)

    def _seed(self) -> None:
        # 交付态（auth_enabled=False）不校验登录，该账号不会被使用；
        # 保留以便重新开启认证（LR_AUTH_ENABLED=true）后仍有管理员可登录。
        self.users["admin"] = (User(username="admin", name="值班管理员", role="系统管理员"), _hash_password("123456"))
        self.robots = [
            RobotSummary(
                id="LR-01",
                name="智巡一号",
                status="navigating",
                location="一层 A 区通道",
                currentTask="前往 A-03 书架执行取书",
                mapVersion="302lab v1.2",
                health={"navigation": True, "chassis": True, "arm": True, "rod": True, "lift": True, "camera": True},
                updatedAt=_now_iso(),
            ),
        ]
        # 任务列表来自真实运行；不再预置演示任务。
        self.tasks = []
        self.alerts = []

    def authenticate(self, username: str, password: str) -> tuple[str, User] | None:
        record = self.users.get(username.strip())
        if not record or not _verify_password(password, record[1]):
            return None
        token = secrets.token_urlsafe(32)
        with self._lock:
            self.sessions[token] = record[0]
        if self.cache:
            self.cache.set_session(token, record[0].model_dump())
        return token, record[0]

    def register(self, username: str, password: str, name: str) -> User:
        username = username.strip()
        with self._lock:
            if username in self.users:
                raise ValueError("该账号已被注册")
            user = User(username=username, name=name.strip(), role="馆员")
            self.users[username] = (user, _hash_password(password))
            self._schedule_persist()
        return user

    def user_for_token(self, token: str | None) -> User | None:
        if self.cache and token:
            cached = self.cache.get_session(token)
            if cached:
                return User.model_validate(cached)
        return self.sessions.get(token or "")

    def create_robot(self, robot_id: str, name: str, gateway_host: str, user: User) -> RobotSummary:
        robot_id = robot_id.strip().upper()
        with self._lock:
            if any(item.id == robot_id for item in self.robots):
                raise ValueError("机器人 ID 已存在")
            robot = RobotSummary(
                id=robot_id,
                name=name.strip(),
                status="offline",
                location="未分配",
                currentTask=f"等待 Gateway {gateway_host.strip()} 首次连接",
                mapVersion="未分配",
                mapId=None,
                pose=None,
                localizationStatus="unknown",
                localizationScanMode="unknown",
                lastSeenAt=None,
                health={"navigation": False, "chassis": False, "arm": False, "rod": False, "lift": False, "camera": False},
                updatedAt=_now_iso(),
            )
            self.robots.append(robot)
            self._schedule_persist()
            self._audit(user.username, "create_robot", robot.id)
            return robot

    def import_map(self, request: MapImportRequest, user: User) -> MapVersion:
        yaml_name = Path(request.yamlName).name
        pgm_name = Path(request.pgmName).name
        if yaml_name != request.yamlName or pgm_name != request.pgmName:
            raise ValueError("文件名不能包含路径")
        if not yaml_name.lower().endswith(".yaml") or not pgm_name.lower().endswith(".pgm"):
            raise ValueError("请分别选择 .yaml 和 .pgm 地图文件")
        yaml_data = decode_base64(request.yamlContent, "YAML")
        pgm_data = decode_base64(request.pgmContent, "PGM")
        metadata = parse_ros_map_yaml(yaml_data, pgm_name)
        image = parse_pgm(pgm_data)

        with self._lock:
            map_id = request.id or f"MAP-{uuid4().hex[:12].upper()}"
            if any(item.id == map_id for item in self.map_versions):
                raise ValueError("地图 ID 已存在: %s" % map_id)
            created_at = datetime.now(UTC)
            version = created_at.strftime("v%Y%m%d-%H%M%S")
            map_version = MapVersion(
                id=map_id,
                name=request.name.strip(),
                version=version,
                yamlFile=yaml_name,
                pgmFile=pgm_name,
                width=image.width,
                height=image.height,
                resolution=metadata.resolution,
                origin=MapOrigin(x=metadata.origin[0], y=metadata.origin[1], yaw=metadata.origin[2]),
                mode=metadata.mode,
                negate=metadata.negate,
                occupiedThreshold=metadata.occupied_threshold,
                freeThreshold=metadata.free_threshold,
                status="draft",
                createdAt=created_at,
                createdBy=user.username,
            )
            version_dir = self.map_storage_dir / map_id
            version_dir.mkdir(parents=True, exist_ok=False)
            try:
                (version_dir / yaml_name).write_bytes(yaml_data)
                (version_dir / pgm_name).write_bytes(pgm_data)
                (version_dir / "preview.png").write_bytes(pgm_preview_png(image, metadata))
                self.map_versions.insert(0, map_version)
                self._add_cruise_annotations(map_version, user)
                self._schedule_persist()
            except Exception:
                for file_path in version_dir.iterdir():
                    file_path.unlink()
                version_dir.rmdir()
                self.map_versions = [item for item in self.map_versions if item.id != map_id]
                self.map_annotations.pop(map_id, None)
                raise
            self._audit(user.username, "import_map", map_id)
            return map_version

    def map_preview_path(self, map_id: str) -> Path:
        if not any(item.id == map_id for item in self.map_versions):
            raise ValueError("地图版本不存在")
        preview = self.map_storage_dir / map_id / "preview.png"
        if not preview.is_file():
            raise ValueError("地图预览不存在")
        return preview

    def list_map_annotations(self, map_id: str) -> list[MapAnnotation]:
        if not any(item.id == map_id for item in self.map_versions):
            raise ValueError("地图版本不存在")
        return self.map_annotations.get(map_id, [])

    # ------------------------------------------------------------------
    # Complete takeout (navigate, then take).
    # ------------------------------------------------------------------

    def _bookarm_client(self) -> Any:
        if self.bookarm_client_factory is not None:
            return self.bookarm_client_factory()
        from .bookarm_client import BookarmClient

        return BookarmClient(self.bookarm_socket_path, self.bookarm_token)

    def bookarm_client(self) -> Any:
        """Public accessor for endpoints that proxy read-only bookarm data."""
        return self._bookarm_client()

    # ------------------------------------------------------------------
    # Managed books (name + spine template + shelf height).
    # ------------------------------------------------------------------

    def ensure_book_maintainer(self, user: User) -> None:
        """Up-front role gate for batch operations (per-row CRUD re-checks)."""
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可维护书籍信息")

    def list_books(self) -> list[Book]:
        with self._lock:
            return sorted(self.books.values(), key=lambda item: item.createdAt)

    def create_book(self, request: BookUpsert, user: User) -> Book:
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可维护书籍信息")
        with self._lock:
            book = Book(
                id=f"BOOK-{uuid4().hex[:12].upper()}",
                name=request.name.strip(),
                templateId=request.templateId,
                heightMm=request.heightMm,
                annotationId=request.annotationId or None,
                preferredSlot=request.preferredSlot,
                createdAt=datetime.now(UTC).isoformat(),
                updatedAt=datetime.now(UTC).isoformat(),
            )
            self.books[book.id] = book
            self._schedule_persist()
            self._audit(user.username, "create_book", book.id)
            return book

    def update_book(self, book_id: str, request: BookUpsert, user: User) -> Book:
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可维护书籍信息")
        with self._lock:
            book = self.books.get(book_id)
            if book is None:
                raise ValueError("书籍不存在")
            updated = book.model_copy(
                update={
                    "name": request.name.strip(),
                    "templateId": request.templateId,
                    "heightMm": request.heightMm,
                    "annotationId": request.annotationId or None,
                    "preferredSlot": request.preferredSlot,
                    "updatedAt": datetime.now(UTC).isoformat(),
                }
            )
            self.books[book_id] = updated
            self._schedule_persist()
            self._audit(user.username, "update_book", book_id)
            return updated

    def delete_book(self, book_id: str, user: User) -> None:
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可维护书籍信息")
        with self._lock:
            if book_id not in self.books:
                raise ValueError("书籍不存在")
            del self.books[book_id]
            self._schedule_persist()
            self._audit(user.username, "delete_book", book_id)

    # ------------------------------------------------------------------
    # Stress-test task templates (named single-loop configs).
    # ------------------------------------------------------------------

    def list_stress_test_templates(self) -> list[StressTestTemplate]:
        with self._lock:
            return sorted(self.stress_test_templates.values(), key=lambda item: item.createdAt)

    def create_stress_test_template(
        self, request: StressTestTemplateUpsert, user: User
    ) -> StressTestTemplate:
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可维护压测模板")
        with self._lock:
            name = request.name.strip()
            if any(item.name == name for item in self.stress_test_templates.values()):
                raise StressTestTemplateNameConflictError("同名模板已存在")
            template = StressTestTemplate(
                id=f"STT-{uuid4().hex[:12].upper()}",
                name=name,
                config=request.config,
                createdAt=datetime.now(UTC).isoformat(),
                updatedAt=datetime.now(UTC).isoformat(),
            )
            self.stress_test_templates[template.id] = template
            self._schedule_persist()
            self._audit(user.username, "create_stress_test_template", template.id)
            return template

    def update_stress_test_template(
        self, template_id: str, request: StressTestTemplateUpsert, user: User
    ) -> StressTestTemplate:
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可维护压测模板")
        with self._lock:
            template = self.stress_test_templates.get(template_id)
            if template is None:
                raise ValueError("模板不存在")
            name = request.name.strip()
            if any(
                item.name == name and item.id != template_id
                for item in self.stress_test_templates.values()
            ):
                raise StressTestTemplateNameConflictError("同名模板已存在")
            updated = template.model_copy(
                update={
                    "name": name,
                    "config": request.config,
                    "updatedAt": datetime.now(UTC).isoformat(),
                }
            )
            self.stress_test_templates[template_id] = updated
            self._schedule_persist()
            self._audit(user.username, "update_stress_test_template", template_id)
            return updated

    def delete_stress_test_template(self, template_id: str, user: User) -> None:
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可维护压测模板")
        with self._lock:
            if template_id not in self.stress_test_templates:
                raise ValueError("模板不存在")
            del self.stress_test_templates[template_id]
            self._schedule_persist()
            self._audit(user.username, "delete_stress_test_template", template_id)

    def find_annotation_by_label(self, label: str) -> MapAnnotation | None:
        """Resolve an annotation by exact label across every imported map
        (batch book imports name takeout points by their annotation label)."""
        with self._lock:
            for items in self.map_annotations.values():
                for annotation in items:
                    if annotation.label == label:
                        return annotation
        return None

    def library_export(self) -> dict[str, Any]:
        """Backup snapshot: managed books plus every map's annotations. The
        spine-library listing is attached by the caller (it needs bookarm)."""
        with self._lock:
            books = [
                book.model_dump(mode="json")
                for book in sorted(self.books.values(), key=lambda item: item.createdAt)
            ]
            annotations = [
                annotation.model_dump(mode="json")
                for items in self.map_annotations.values()
                for annotation in items
            ]
            maps = [item.model_dump(mode="json") for item in self.map_versions]
        return {
            "version": 1,
            "exportedAt": datetime.now(UTC).isoformat(),
            "books": books,
            "maps": maps,
            "annotations": annotations,
        }

    def create_complete_takeout_task(
        self, request: CompleteTakeoutRequest, user: User
    ) -> DispatchTask:
        """Create a navigate-then-takeout task; a daemon thread orchestrates it."""
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可创建会驱动机器人硬件的取书任务")
        with self._lock:
            robot = next((item for item in self.robots if item.id == request.robotId), None)
            if not robot:
                raise ValueError("机器人不存在")
            if robot.status == "busy":
                raise ValueError("机器人忙（机械臂作业中），请等待当前作业完成")
            if robot.status != "idle":
                raise ValueError(f"机器人 {robot.id} 当前不在空闲状态，不能执行完整取书任务")
            if request.sendNavigation and (
                robot.localizationStatus != "localized" or not robot.pose
            ):
                raise ValueError(f"机器人 {robot.id} 尚未完成可靠定位，暂不能导航")
            active = next(
                (
                    item
                    for item in self.tasks
                    if item.type == "takeout"
                    and item.robotId == robot.id
                    and item.status in {"queued", "running", "paused"}
                ),
                None,
            )
            superseded = None
            if active:
                # A stale non-terminal takeout (cancelled physically, orphaned by
                # a restart, or abandoned by the operator) must never permanently
                # block new takeouts: signal its orchestrator to abort at the next
                # checkpoint, mark the row superseded, and proceed immediately.
                cancel_event = self._takeout_cancels.get(active.id)
                if cancel_event is not None:
                    cancel_event.set()
                self._replace_task(
                    active.id,
                    active.model_copy(
                        update={
                            "status": "cancelled",
                            "workflowMessage": "已被新的取书任务取代",
                            "updatedAt": _now_iso(),
                        }
                    ),
                )
                superseded = active
                self._audit(user.username, "supersede_complete_takeout_task", active.id)

            task = DispatchTask(
                id=f"TSK-{uuid4().hex[:12].upper()}",
                type="takeout",
                robotId=robot.id,
                target=request.bookId or request.templateId,
                status="queued",
                progress=0,
                updatedAt=_now_iso(),
                bookId=request.bookId,
                workflowMessage="排队中",
                workflowDetails={
                    "templateId": request.templateId,
                    "destinationSlot": request.destinationSlot,
                    "target": request.target.model_dump() if request.target else None,
                    "liftHeightMm": request.liftHeightMm,
                },
            )
            self.tasks.insert(0, task)
            self._takeout_cancels[task.id] = Event()
            self._schedule_persist()
            self._audit(user.username, "create_complete_takeout_task", task.id)

        # Cancel the superseded flow's in-flight stage before the new navigation
        # starts. The cancel is targeted (taskId), so the vehicle only cancels
        # that exact command and can never kill the new one. create_command
        # takes the store lock, so it must run outside the locked section.
        if superseded is not None:
            self._cancel_in_flight_stage_tasks(robot, superseded, user)
        thread = Thread(
            target=self._run_complete_takeout,
            args=(task.id, request, request.destinationSlot, user),
            name=f"complete-takeout-{task.id}",
            daemon=True,
        )
        thread.start()
        return task

    def cancel_complete_takeout(self, task_id: str, user: User) -> DispatchTask:
        """Best-effort cancel: navigation cancel or bookarm cancel by phase."""
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可取消完整取书任务")
        with self._lock:
            task = next((item for item in self.tasks if item.id == task_id), None)
            if not task or task.type != "takeout":
                raise ValueError("完整取书任务不存在")
            if task.status in {"completed", "failed", "cancelled", "recovery_required"}:
                raise ValueError("已结束的完整取书任务不能取消")
            event = self._takeout_cancels.get(task_id)
            if event is not None:
                event.set()
            updated = task.model_copy(
                update={"workflowMessage": "已请求取消，正在停止当前阶段", "updatedAt": _now_iso()}
            )
            self._replace_task(task_id, updated)
            self._schedule_persist()
            self._audit(user.username, "cancel_complete_takeout", task_id)
            return updated

    def _update_takeout_task(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            task = next((item for item in self.tasks if item.id == task_id), None)
            if task is None:
                return
            fields["updatedAt"] = _now_iso()
            self._replace_task(task_id, task.model_copy(update=fields))
            self._schedule_persist()

    def _set_robot_arm_busy(self, robot_id: str) -> None:
        """Mark the robot busy while the bookarm runs. The orchestration owns
        the status until _release_robot_arm_busy; a fault/offline lock (e.g. an
        emergency stop that already fired) is never overwritten."""
        with self._lock:
            index = next((i for i, item in enumerate(self.robots) if item.id == robot_id), None)
            if index is None:
                return
            robot = self.robots[index]
            if robot.status in {"fault", "offline"}:
                return
            self.robots[index] = robot.model_copy(
                update={"status": "busy", "currentTask": "机械臂作业中", "updatedAt": _now_iso()}
            )
            self._schedule_persist()
            updated = self.robots[index]
        self._notify_robot_update(updated)

    def _release_robot_arm_busy(self, robot_id: str) -> None:
        """Return the robot to idle after the arm phase. Only a robot still in
        'busy' is touched, so a fault/offline/manual change that arrived during
        arm work (emergency stop, takeover) is preserved."""
        with self._lock:
            index = next((i for i, item in enumerate(self.robots) if item.id == robot_id), None)
            if index is None:
                return
            robot = self.robots[index]
            if robot.status != "busy":
                return
            self.robots[index] = robot.model_copy(
                update={"status": "idle", "currentTask": "待命", "updatedAt": _now_iso()}
            )
            self._schedule_persist()
            updated = self.robots[index]
        self._notify_robot_update(updated)

    def _cancel_robot_flows_locked(self, robot_id: str, reason: str) -> int:
        """Signal every live takeout/return/multi-round/annotation-navigation
        flow of one robot to abort; the caller must hold self._lock. Each
        orchestration loop notices its event at the next checkpoint and marks
        its own task cancelled."""
        cancelled = 0
        for task in list(self.tasks):
            if task.robotId != robot_id or task.status not in {"queued", "running", "paused"}:
                continue
            event: Event | None = None
            if task.type == "takeout":
                event = self._takeout_cancels.get(task.id)
            elif task.type == "return":
                event = self._return_cancels.get(task.id)
            elif task.type == "multi_round":
                event = self._multi_round_cancels.get(task.id)
            elif task.type == "navigate" and (task.workflowDetails or {}).get("annotationNavigation") is True:
                event = self._annotation_nav_cancels.get(task.id)
            else:
                continue
            if event is not None:
                event.set()
            self._replace_task(
                task.id,
                task.model_copy(update={"workflowMessage": reason, "updatedAt": _now_iso()}),
            )
            cancelled += 1
        return cancelled

    def _cancel_in_flight_stage_tasks(self, robot: RobotSummary, takeout_task: DispatchTask, user: User) -> None:
        """Best-effort targeted cancel for a superseded flow's in-flight stages.

        The vehicle matches the cancel by taskId, so only the stale stage
        command is cancelled; anything dispatched later is left untouched.
        """

        details = takeout_task.workflowDetails or {}
        stage_task_ids = [
            value
            for key, value in details.items()
            if key in {"navigationTaskId", "fineTuneTaskId"} and isinstance(value, str)
        ]
        for stage_task_id in stage_task_ids:
            stage_task = next((item for item in self.tasks if item.id == stage_task_id), None)
            if stage_task is None or stage_task.status not in {"queued", "running"}:
                continue
            try:
                self.create_command(
                    robot,
                    RobotCommandRequest(command="cancel", payload={"taskId": stage_task_id}),
                    user,
                    self.command_ttl_seconds,
                )
            except Exception:
                continue

    def _run_complete_takeout(
        self, task_id: str, request: CompleteTakeoutRequest, slot: int, user: User
    ) -> None:
        """Orchestrate navigation → fine-tune → on-robot takeout (daemon)."""

        cancel = self._takeout_cancels.get(task_id)
        bookarm_task_id: str | None = None
        try:
            robot = next((item for item in self.robots if item.id == request.robotId), None)
            if robot is None:
                raise ValueError("机器人不存在")
            if cancel is not None and cancel.is_set():
                raise _FlowCancelled()
            if request.sendNavigation:
                self._update_takeout_task(
                    task_id, status="running", progress=5, workflowMessage="导航至目标点"
                )
                nav_task_id = self._dispatch_takeout_navigation(task_id, robot, request, user)
                self._wait_for_navigation(task_id, nav_task_id, cancel, user)
                if cancel is not None and cancel.is_set():
                    raise _FlowCancelled()

                self._update_takeout_task(task_id, progress=40, workflowMessage="低速精调对位")
                fine_tune_task_id = self._dispatch_takeout_fine_tune(task_id, robot, request, user)
                self._wait_for_fine_tune(task_id, fine_tune_task_id, cancel, user)
                if cancel is not None and cancel.is_set():
                    raise _FlowCancelled()
            else:
                self._update_takeout_task(
                    task_id,
                    status="running",
                    progress=5,
                    workflowMessage="已跳过导航，原地开始取书",
                )

            self._update_takeout_task(task_id, progress=60, workflowMessage="取书执行中")
            self._set_robot_arm_busy(request.robotId)
            client = self._bookarm_client()
            bookarm_task_id = self._create_bookarm_task_with_retry(
                cancel,
                lambda: client.create_takeout_task(
                    template_id=request.templateId,
                    target_height_mm=request.liftHeightMm,
                    destination_slot=slot,
                    lift_speed_level=request.liftSpeedLevel,
                    book_id=request.bookId,
                ),
            )
            self._update_takeout_task(
                task_id,
                workflowMessage="取书执行中（机械臂）",
                workflowDetails={
                    **self._takeout_workflow_details(task_id),
                    "bookarmTaskId": bookarm_task_id,
                },
            )
            state = self._wait_for_bookarm_task(
                task_id,
                client,
                bookarm_task_id,
                cancel,
                on_vision_stage=lambda _stage: self._save_flow_vision(
                    task_id, client, "takeout"
                ),
            )
            self._save_flow_vision(task_id, client, "takeout")
            if cancel is not None and cancel.is_set():
                raise _FlowCancelled()
            if state != "succeeded":
                raise ValueError(f"取书流程未成功：{describe_bookarm_state(state)}")

            self._update_takeout_task(
                task_id, status="completed", progress=100, workflowMessage="取书完成"
            )
        except _FlowCancelled:
            self._update_takeout_task(
                task_id, status="cancelled", workflowMessage="已取消"
            )
        except _BookarmRecoveryRequired as error:
            self._update_takeout_task(
                task_id,
                status="recovery_required",
                workflowMessage="机械臂中断待确认（可能夹着书）",
                failureReason=str(error),
            )
        except Exception as error:
            if cancel is not None and cancel.is_set():
                self._update_takeout_task(
                    task_id, status="cancelled", workflowMessage="已取消"
                )
            else:
                self._update_takeout_task(
                    task_id,
                    status="failed",
                    workflowMessage="任务失败",
                    failureReason=str(error),
                )
        finally:
            self._release_robot_arm_busy(request.robotId)
            self._takeout_cancels.pop(task_id, None)

    def _dispatch_takeout_navigation(
        self,
        task_id: str,
        robot: RobotSummary,
        request: CompleteTakeoutRequest | CompleteReturnRequest | MultiRoundRequest,
        user: User,
        *,
        flow_label: str = "取书",
        task_key: str = "takeoutTaskId",
    ) -> str:
        """Publish the navigate command and register its dispatch task row."""

        if request.target is None:
            raise ValueError("缺少导航目标点")
        map_id = request.mapId or robot.mapId
        # 车端协议强制要求 annotationId：手填坐标（无标注）时由控制站生成一个
        # 临时标识随命令下发，保证手填模式端到端可用。
        annotation_id = request.annotationId or f"MANUAL-{uuid4().hex[:8].upper()}"
        command = self.create_command(
            robot,
            RobotCommandRequest(
                command="navigate",
                payload={
                    "mapId": map_id,
                    "annotationId": annotation_id,
                    "frameId": "map",
                    "x": request.target.x,
                    "y": request.target.y,
                    "yaw": request.target.yaw,
                    "shelfId": None,
                    task_key: task_id,
                },
            ),
            user,
            self.command_ttl_seconds,
        )
        nav_task = DispatchTask(
            id=command.commandId.replace("CMD-", "TSK-", 1),
            type="navigate",
            robotId=robot.id,
            target=f"{flow_label}导航 ({request.target.x:.2f}, {request.target.y:.2f})",
            status="queued",
            progress=0,
            updatedAt=_now_iso(),
            mapId=map_id,
            annotationId=annotation_id,
        )
        with self._lock:
            self.tasks.insert(0, nav_task)
            self._schedule_persist()
        self._update_takeout_task(
            task_id,
            workflowDetails={
                **self._takeout_workflow_details(task_id),
                "navigationTaskId": nav_task.id,
            },
        )
        return nav_task.id

    def _dispatch_takeout_fine_tune(
        self,
        task_id: str,
        robot: RobotSummary,
        request: CompleteTakeoutRequest | CompleteReturnRequest | MultiRoundRequest,
        user: User,
        *,
        flow_label: str = "取书",
    ) -> str:
        """Publish the fine_tune command and register its dispatch task row."""

        if request.target is None:
            raise ValueError("缺少精调目标点")
        map_id = request.mapId or robot.mapId
        command = self.create_command(
            robot,
            RobotCommandRequest(
                command="fine_tune",
                payload={
                    "mapId": map_id,
                    "x": request.target.x,
                    "y": request.target.y,
                    "yaw": request.target.yaw,
                },
            ),
            user,
            self.command_ttl_seconds,
        )
        fine_tune_task = DispatchTask(
            id=command.commandId.replace("CMD-", "TSK-", 1),
            type="fine_tune",
            robotId=robot.id,
            target=f"{flow_label}精调 ({request.target.x:.2f}, {request.target.y:.2f})",
            status="queued",
            progress=0,
            updatedAt=_now_iso(),
            mapId=map_id,
            annotationId=request.annotationId,
        )
        with self._lock:
            self.tasks.insert(0, fine_tune_task)
            self._schedule_persist()
        self._update_takeout_task(
            task_id,
            workflowDetails={
                **self._takeout_workflow_details(task_id),
                "fineTuneTaskId": fine_tune_task.id,
            },
        )
        return fine_tune_task.id

    def _takeout_workflow_details(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = next((item for item in self.tasks if item.id == task_id), None)
            return dict(task.workflowDetails) if task is not None else {}

    def start_annotation_navigation(self, robot_id: str, map_id: str, annotation_id: str, user: User, skip_fine_tune: bool = False) -> DispatchTask:
        """Navigate to a map annotation, then low-speed fine-tune onto its pose.

        Same navigate → fine-tune pipeline as the takeout/return flows, so the
        map page gets precise arrival plus per-stage task rows for realtime
        progress, instead of a fire-and-forget navigate command. With
        ``skip_fine_tune`` the flow completes right after navigation (used by
        the stress test's return-to-origin leg, where approximate arrival is
        enough to start the next loop).
        """
        with self._lock:
            robot = next((item for item in self.robots if item.id == robot_id), None)
            if not robot:
                raise ValueError("机器人不存在")
            if robot.status == "busy":
                raise ValueError("机器人忙（机械臂作业中），请等待当前作业完成")
            if robot.status != "idle":
                raise ValueError(f"机器人 {robot.id} 当前不在空闲状态，不能执行标注导航")
            if robot.localizationStatus != "localized" or not robot.pose:
                raise ValueError(f"机器人 {robot.id} 尚未完成可靠定位，暂不能导航")
            if robot.mapId != map_id:
                raise ValueError(f"机器人 {robot.id} 不在该标注所在地图上，请先切换地图")
            annotation = next(
                (item for item in self.map_annotations.get(map_id, []) if item.id == annotation_id),
                None,
            )
            if annotation is None:
                raise ValueError("标注点不存在")
            active = next(
                (
                    item
                    for item in self.tasks
                    if item.type == "navigate"
                    and item.robotId == robot.id
                    and item.status in {"queued", "running", "paused"}
                    and item.workflowDetails.get("annotationNavigation") is True
                ),
                None,
            )
            superseded = None
            if active:
                cancel_event = self._annotation_nav_cancels.get(active.id)
                if cancel_event is not None:
                    cancel_event.set()
                self._replace_task(
                    active.id,
                    active.model_copy(
                        update={
                            "status": "cancelled",
                            "workflowMessage": "已被新的标注导航取代",
                            "updatedAt": _now_iso(),
                        }
                    ),
                )
                superseded = active
                self._audit(user.username, "supersede_annotation_navigation", active.id)

            task = DispatchTask(
                id=f"TSK-{uuid4().hex[:12].upper()}",
                type="navigate",
                robotId=robot.id,
                target=f"标注点「{annotation.label}」",
                status="queued",
                progress=0,
                updatedAt=_now_iso(),
                mapId=map_id,
                annotationId=annotation.id,
                workflowMessage="排队中",
                workflowDetails={
                    "annotationNavigation": True,
                    "annotationId": annotation.id,
                    "skipFineTune": skip_fine_tune,
                },
            )
            self.tasks.insert(0, task)
            self._annotation_nav_cancels[task.id] = Event()
            self._schedule_persist()
            self._audit(user.username, "create_annotation_navigation", task.id)

        if superseded is not None:
            self._cancel_in_flight_stage_tasks(robot, superseded, user)
        thread = Thread(
            target=self._run_annotation_navigation,
            args=(task.id, robot_id, map_id, annotation_id, user, skip_fine_tune),
            name=f"annotation-navigation-{task.id}",
            daemon=True,
        )
        thread.start()
        return task

    def cancel_annotation_navigation(self, task_id: str, user: User) -> DispatchTask:
        """Best-effort cancel of an annotation navigation flow.

        Sets the orchestration's cancel event; the flow's stage waits then
        issue a targeted vehicle cancel for the in-flight navigate or
        fine-tune command before marking the task cancelled.
        """
        with self._lock:
            task = next((item for item in self.tasks if item.id == task_id), None)
            if (
                not task
                or task.type != "navigate"
                or task.workflowDetails.get("annotationNavigation") is not True
            ):
                raise ValueError("标注导航任务不存在")
            if task.status in {"completed", "failed", "cancelled", "recovery_required"}:
                raise ValueError("已结束的标注导航不能取消")
            event = self._annotation_nav_cancels.get(task_id)
            if event is not None:
                event.set()
            updated = task.model_copy(
                update={"workflowMessage": "已请求取消，正在停止当前阶段", "updatedAt": _now_iso()}
            )
            self._replace_task(task_id, updated)
            self._schedule_persist()
            self._audit(user.username, "cancel_annotation_navigation", task_id)
            return updated

    def _run_annotation_navigation(self, task_id: str, robot_id: str, map_id: str, annotation_id: str, user: User, skip_fine_tune: bool = False) -> None:
        """Orchestrate navigation → low-speed fine-tune for one annotation (daemon)."""

        cancel = self._annotation_nav_cancels.get(task_id)
        try:
            robot = next((item for item in self.robots if item.id == robot_id), None)
            annotation = next(
                (item for item in self.map_annotations.get(map_id, []) if item.id == annotation_id),
                None,
            )
            if robot is None or annotation is None:
                raise ValueError("机器人或标注点不存在")
            if cancel is not None and cancel.is_set():
                raise _FlowCancelled()
            self._update_takeout_task(
                task_id, status="running", progress=5, workflowMessage=f"导航至标注点「{annotation.label}」"
            )
            nav_task_id = self._dispatch_annotation_nav_stage(task_id, robot, map_id, annotation, user)
            self._wait_for_navigation(task_id, nav_task_id, cancel, user)
            if cancel is not None and cancel.is_set():
                raise _FlowCancelled()

            if skip_fine_tune:
                self._update_takeout_task(
                    task_id, status="completed", progress=100, workflowMessage="已到位（导航，未低速精调）"
                )
                return

            self._update_takeout_task(task_id, progress=60, workflowMessage="低速精调对位")
            fine_tune_task_id = self._dispatch_annotation_fine_tune_stage(task_id, robot, map_id, annotation, user)
            self._wait_for_fine_tune(task_id, fine_tune_task_id, cancel, user)
            if cancel is not None and cancel.is_set():
                raise _FlowCancelled()

            self._update_takeout_task(
                task_id, status="completed", progress=100, workflowMessage="已到位（导航+低速精调）"
            )
        except _FlowCancelled:
            self._update_takeout_task(task_id, status="cancelled", workflowMessage="已取消")
        except Exception as error:
            if cancel is not None and cancel.is_set():
                self._update_takeout_task(task_id, status="cancelled", workflowMessage="已取消")
            else:
                self._update_takeout_task(
                    task_id,
                    status="failed",
                    workflowMessage="标注导航失败",
                    failureReason=str(error),
                )
        finally:
            self._annotation_nav_cancels.pop(task_id, None)

    def _dispatch_annotation_nav_stage(
        self, task_id: str, robot: RobotSummary, map_id: str, annotation: MapAnnotation, user: User
    ) -> str:
        """Publish the navigate command and register its dispatch task row."""

        command = self.create_command(
            robot,
            RobotCommandRequest(
                command="navigate",
                payload={
                    "mapId": map_id,
                    "annotationId": annotation.id,
                    "frameId": "map",
                    "x": annotation.mapX,
                    "y": annotation.mapY,
                    "yaw": annotation.yaw,
                    "shelfId": None,
                    "annotationNavTaskId": task_id,
                },
            ),
            user,
            self.command_ttl_seconds,
        )
        nav_task = DispatchTask(
            id=command.commandId.replace("CMD-", "TSK-", 1),
            type="navigate",
            robotId=robot.id,
            target=f"标注导航 ({annotation.mapX:.2f}, {annotation.mapY:.2f})",
            status="queued",
            progress=0,
            updatedAt=_now_iso(),
            mapId=map_id,
            annotationId=annotation.id,
        )
        with self._lock:
            self.tasks.insert(0, nav_task)
            self._schedule_persist()
        self._update_takeout_task(
            task_id,
            workflowDetails={
                **self._takeout_workflow_details(task_id),
                "navigationTaskId": nav_task.id,
            },
        )
        return nav_task.id

    def _dispatch_annotation_fine_tune_stage(
        self, task_id: str, robot: RobotSummary, map_id: str, annotation: MapAnnotation, user: User
    ) -> str:
        """Publish the fine_tune command and register its dispatch task row."""

        command = self.create_command(
            robot,
            RobotCommandRequest(
                command="fine_tune",
                payload={
                    "mapId": map_id,
                    "x": annotation.mapX,
                    "y": annotation.mapY,
                    "yaw": annotation.yaw,
                },
            ),
            user,
            self.command_ttl_seconds,
        )
        fine_tune_task = DispatchTask(
            id=command.commandId.replace("CMD-", "TSK-", 1),
            type="fine_tune",
            robotId=robot.id,
            target=f"低速精调 ({annotation.mapX:.2f}, {annotation.mapY:.2f})",
            status="queued",
            progress=0,
            updatedAt=_now_iso(),
            mapId=map_id,
            annotationId=annotation.id,
        )
        with self._lock:
            self.tasks.insert(0, fine_tune_task)
            self._schedule_persist()
        self._update_takeout_task(
            task_id,
            workflowDetails={
                **self._takeout_workflow_details(task_id),
                "fineTuneTaskId": fine_tune_task.id,
            },
        )
        return fine_tune_task.id

    def _takeout_workflow_details(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = next((item for item in self.tasks if item.id == task_id), None)
            return dict(task.workflowDetails) if task is not None else {}

    def _cancel_stage_command(self, stage_task_id: str, user: User) -> None:
        """Best-effort targeted vehicle cancel for one in-flight navigate /
        fine-tune stage command (matched by taskId, so only that exact command
        is cancelled). Used by both operator cancels and stage timeouts."""
        stage_task = next((item for item in self.tasks if item.id == stage_task_id), None)
        if stage_task is None or stage_task.status not in {"queued", "running"}:
            return
        robot = next((item for item in self.robots if item.id == stage_task.robotId), None)
        if robot is None:
            return
        try:
            self.create_command(
                robot,
                RobotCommandRequest(
                    command="cancel",
                    payload={"taskId": stage_task_id},
                ),
                user,
                self.command_ttl_seconds,
            )
        except Exception:
            pass

    def _wait_for_navigation(
        self, task_id: str, nav_task_id: str, cancel: Event | None, user: User
    ) -> None:
        deadline = time.monotonic() + self.takeout_nav_timeout_s
        while True:
            if cancel is not None and cancel.is_set():
                self._cancel_stage_command(nav_task_id, user)
                raise _FlowCancelled()
            nav_task = next((item for item in self.tasks if item.id == nav_task_id), None)
            if nav_task is None:
                raise ValueError("导航任务记录丢失")
            if nav_task.status == "completed":
                return
            if nav_task.status in {"failed", "paused", "cancelled"}:
                if nav_task.failureReason:
                    raise ValueError("导航未成功：%s" % describe_navigation_failure(nav_task.failureReason))
                raise ValueError("导航未成功：%s" % describe_task_status(nav_task.status))
            if time.monotonic() >= deadline:
                # Never leave the vehicle driving after we give up waiting.
                self._cancel_stage_command(nav_task_id, user)
                raise ValueError(
                    "导航超时（%ds 内未到位，已下发取消）" % int(self.takeout_nav_timeout_s)
                )
            time.sleep(self.takeout_poll_interval_s)

    def _wait_for_fine_tune(
        self, task_id: str, fine_tune_task_id: str, cancel: Event | None, user: User
    ) -> None:
        deadline = time.monotonic() + self.takeout_nav_timeout_s
        while True:
            if cancel is not None and cancel.is_set():
                self._cancel_stage_command(fine_tune_task_id, user)
                raise _FlowCancelled()
            fine_tune_task = next(
                (item for item in self.tasks if item.id == fine_tune_task_id), None
            )
            if fine_tune_task is None:
                raise ValueError("精调任务记录丢失")
            if fine_tune_task.status == "completed":
                return
            if fine_tune_task.status in {"failed", "paused", "cancelled"}:
                reason = fine_tune_task.failureReason or describe_task_status(fine_tune_task.status)
                raise ValueError(f"低速精调未成功：{reason}")
            if time.monotonic() >= deadline:
                self._cancel_stage_command(fine_tune_task_id, user)
                raise ValueError(
                    "低速精调超时（%ds 内未完成，已下发取消）" % int(self.takeout_nav_timeout_s)
                )
            time.sleep(self.takeout_poll_interval_s)

    _BOOKARM_VISION_STAGES = {"vision_target", "vision_retarget"}

    _BOOKARM_STAGE_MESSAGES = {
        "lift_move": "升降杆升起中",
        "vision_target": "视觉目标定位中",
        "vision_retarget": "插入前重新识别目标书",
        "book_stand_grasp": "夹取图书中",
        "grasp": "夹取图书中",
        "shelf_place": "放入书架中",
        "place_in_book_stand": "放入书立中",
        "book_stand_place": "放入书立中",
        "move_to_standby": "机械臂回待机位",
        "arm_standby": "机械臂回待机位",
        "arm_debug_pose": "机械臂调试位",
    }

    def _wait_for_bookarm_task(
        self,
        task_id: str,
        client: Any,
        bookarm_task_id: str,
        cancel: Event | None,
        *,
        get_task: Callable[[str], dict[str, Any]] | None = None,
        cancel_task: Callable[[str], Any] | None = None,
        flow_label: str = "取书",
        on_vision_stage: Callable[[str], None] | None = None,
    ) -> str:
        get_task = get_task or client.get_takeout_task
        cancel_task = cancel_task or client.cancel_takeout_task
        terminal = {"succeeded", "failed", "cancelled", "unsupported", "recovery_required"}
        deadline = time.monotonic() + self.takeout_arm_timeout_s
        last_stage_message: str | None = None
        seen_vision_stages: set[str] = set()
        while True:
            if cancel is not None and cancel.is_set():
                try:
                    cancel_task(bookarm_task_id)
                except Exception:
                    pass
                raise _FlowCancelled()
            view = get_task(bookarm_task_id)
            state = str(view.get("state", ""))
            if state in terminal:
                if state == "recovery_required":
                    detail = str(view.get("error") or state)
                    raise _BookarmRecoveryRequired(describe_bookarm_failure(state, detail))
                if state != "succeeded":
                    detail = str(view.get("error") or state)
                    raise ValueError(
                        f"{flow_label}流程未成功：{describe_bookarm_failure(state, detail)}"
                    )
                return state
            # Surface the daemon's current stage (lift raise, vision, grasp…)
            # so the operator sees the physical pipeline instead of a spinner.
            stages = view.get("stages")
            if isinstance(stages, list):
                # Snapshot the recognition image the moment a vision stage
                # completes instead of waiting for the whole sub-task.
                if on_vision_stage is not None:
                    for stage in stages:
                        if not isinstance(stage, dict):
                            continue
                        stage_name = str(stage.get("name", ""))
                        if (
                            stage.get("status") == "succeeded"
                            and stage_name in self._BOOKARM_VISION_STAGES
                            and stage_name not in seen_vision_stages
                        ):
                            seen_vision_stages.add(stage_name)
                            try:
                                on_vision_stage(stage_name)
                            except Exception:
                                pass
                running = next(
                    (
                        stage
                        for stage in stages
                        if isinstance(stage, dict) and stage.get("status") == "running"
                    ),
                    None,
                )
                if running is not None:
                    stage_name = str(running.get("name", ""))
                    stage_message = self._BOOKARM_STAGE_MESSAGES.get(stage_name)
                    if stage_message and stage_message != last_stage_message:
                        last_stage_message = stage_message
                        self._update_takeout_task(task_id, workflowMessage=stage_message)
            if time.monotonic() >= deadline:
                # A timed-out bookarm task must not keep moving the arm.
                try:
                    cancel_task(bookarm_task_id)
                except Exception:
                    pass
                raise ValueError(
                    f"{flow_label}流程超时（%ds 内未完成，已请求机械臂停止）"
                    % int(self.takeout_arm_timeout_s)
                )
            time.sleep(self.takeout_poll_interval_s)

    # Complete-return orchestration: navigate, then run the on-robot
    # mid-return workflow, then release the source book-stand slot.

    def create_complete_return_task(
        self, request: CompleteReturnRequest, user: User
    ) -> DispatchTask:
        """Create a navigate-then-mid-return task; a daemon thread runs it."""
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可创建会驱动机器人硬件的还书任务")
        with self._lock:
            robot = next((item for item in self.robots if item.id == request.robotId), None)
            if not robot:
                raise ValueError("机器人不存在")
            if robot.status == "busy":
                raise ValueError("机器人忙（机械臂作业中），请等待当前作业完成")
            if robot.status != "idle":
                raise ValueError(f"机器人 {robot.id} 当前不在空闲状态，不能执行完整还书任务")
            if request.sendNavigation and (
                robot.localizationStatus != "localized" or not robot.pose
            ):
                raise ValueError(f"机器人 {robot.id} 尚未完成可靠定位，暂不能导航")
            active = next(
                (
                    item
                    for item in self.tasks
                    if item.type == "return"
                    and item.robotId == robot.id
                    and item.status in {"queued", "running", "paused"}
                ),
                None,
            )
            superseded = None
            if active:
                # Same supersede rule as takeout: a stale non-terminal return
                # must never permanently block new returns.
                cancel_event = self._return_cancels.get(active.id)
                if cancel_event is not None:
                    cancel_event.set()
                self._replace_task(
                    active.id,
                    active.model_copy(
                        update={
                            "status": "cancelled",
                            "workflowMessage": "已被新的还书任务取代",
                            "updatedAt": _now_iso(),
                        }
                    ),
                )
                superseded = active
                self._audit(user.username, "supersede_complete_return_task", active.id)

            task = DispatchTask(
                id=f"TSK-{uuid4().hex[:12].upper()}",
                type="return",
                robotId=robot.id,
                target=request.bookId or request.templateId,
                status="queued",
                progress=0,
                updatedAt=_now_iso(),
                bookId=request.bookId,
                workflowMessage="排队中",
                workflowDetails={
                    "templateId": request.templateId,
                    "sourceSlot": request.sourceSlot,
                    "target": request.target.model_dump() if request.target else None,
                    "liftHeightMm": request.liftHeightMm,
                    "returnMode": request.returnMode,
                },
            )
            self.tasks.insert(0, task)
            self._return_cancels[task.id] = Event()
            self._schedule_persist()
            self._audit(user.username, "create_complete_return_task", task.id)

        if superseded is not None:
            self._cancel_in_flight_stage_tasks(robot, superseded, user)
        thread = Thread(
            target=self._run_complete_return,
            args=(task.id, request, user),
            name=f"complete-return-{task.id}",
            daemon=True,
        )
        thread.start()
        return task

    def cancel_complete_return(self, task_id: str, user: User) -> DispatchTask:
        """Best-effort cancel: navigation cancel or bookarm cancel by phase."""
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可取消完整还书任务")
        with self._lock:
            task = next((item for item in self.tasks if item.id == task_id), None)
            if not task or task.type != "return":
                raise ValueError("完整还书任务不存在")
            if task.status in {"completed", "failed", "cancelled", "recovery_required"}:
                raise ValueError("已结束的完整还书任务不能取消")
            event = self._return_cancels.get(task_id)
            if event is not None:
                event.set()
            updated = task.model_copy(
                update={"workflowMessage": "已请求取消，正在停止当前阶段", "updatedAt": _now_iso()}
            )
            self._replace_task(task_id, updated)
            self._schedule_persist()
            self._audit(user.username, "cancel_complete_return", task_id)
            return updated

    def _run_complete_return(
        self, task_id: str, request: CompleteReturnRequest, user: User
    ) -> None:
        """Orchestrate navigation → fine-tune → on-robot return (daemon).

        returnMode selects the bookarm workflow for the arm phase: mid-return
        (default) or end-of-row return."""

        cancel = self._return_cancels.get(task_id)
        bookarm_task_id: str | None = None
        try:
            robot = next((item for item in self.robots if item.id == request.robotId), None)
            if robot is None:
                raise ValueError("机器人不存在")
            if cancel is not None and cancel.is_set():
                raise _FlowCancelled()
            if request.sendNavigation:
                self._update_takeout_task(
                    task_id, status="running", progress=5, workflowMessage="导航至目标点"
                )
                nav_task_id = self._dispatch_takeout_navigation(
                    task_id, robot, request, user, flow_label="还书", task_key="returnTaskId"
                )
                self._wait_for_navigation(task_id, nav_task_id, cancel, user)
                if cancel is not None and cancel.is_set():
                    raise _FlowCancelled()

                self._update_takeout_task(task_id, progress=40, workflowMessage="低速精调对位")
                fine_tune_task_id = self._dispatch_takeout_fine_tune(
                    task_id, robot, request, user, flow_label="还书"
                )
                self._wait_for_fine_tune(task_id, fine_tune_task_id, cancel, user)
                if cancel is not None and cancel.is_set():
                    raise _FlowCancelled()
            else:
                self._update_takeout_task(
                    task_id,
                    status="running",
                    progress=5,
                    workflowMessage="已跳过导航，原地开始还书",
                )

            self._update_takeout_task(task_id, progress=60, workflowMessage="还书执行中")
            self._set_robot_arm_busy(request.robotId)
            client = self._bookarm_client()
            # 末尾还书走 bookarm /return/tasks（不接受注入抓取位姿），其余走 /midreturn/tasks。
            end_of_row_return = request.returnMode == "end"

            def create_bookarm_task() -> dict[str, Any]:
                if end_of_row_return:
                    return client.create_return_task(
                        template_id=request.templateId,
                        target_height_mm=request.liftHeightMm,
                        source_slot=request.sourceSlot,
                        lift_speed_level=request.liftSpeedLevel,
                        book_id=request.bookId,
                    )
                return client.create_midreturn_task(
                    template_id=request.templateId,
                    target_height_mm=request.liftHeightMm,
                    source_slot=request.sourceSlot,
                    lift_speed_level=request.liftSpeedLevel,
                    book_id=request.bookId,
                    injected_grasp_pose=request.injectedTargetGraspPose,
                )

            bookarm_task_id = self._create_bookarm_task_with_retry(cancel, create_bookarm_task)
            self._update_takeout_task(
                task_id,
                workflowMessage="还书执行中（机械臂）",
                workflowDetails={
                    **self._takeout_workflow_details(task_id),
                    "bookarmTaskId": bookarm_task_id,
                },
            )
            state = self._wait_for_bookarm_task(
                task_id,
                client,
                bookarm_task_id,
                cancel,
                get_task=client.get_return_task if end_of_row_return else client.get_midreturn_task,
                cancel_task=client.cancel_return_task if end_of_row_return else client.cancel_midreturn_task,
                flow_label="还书",
                on_vision_stage=lambda _stage: self._save_flow_vision(
                    task_id, client, "return"
                ),
            )
            self._save_flow_vision(task_id, client, "return")
            if cancel is not None and cancel.is_set():
                raise _FlowCancelled()
            if state != "succeeded":
                raise ValueError(f"还书流程未成功：{describe_bookarm_state(state)}")

            self._update_takeout_task(
                task_id, status="completed", progress=100, workflowMessage="还书完成"
            )
        except _FlowCancelled:
            self._update_takeout_task(
                task_id, status="cancelled", workflowMessage="已取消"
            )
        except _BookarmRecoveryRequired as error:
            self._update_takeout_task(
                task_id,
                status="recovery_required",
                workflowMessage="机械臂中断待确认（可能夹着书）",
                failureReason=str(error),
            )
        except Exception as error:
            if cancel is not None and cancel.is_set():
                self._update_takeout_task(
                    task_id, status="cancelled", workflowMessage="已取消"
                )
            else:
                self._update_takeout_task(
                    task_id,
                    status="failed",
                    workflowMessage="任务失败",
                    failureReason=str(error),
                )
        finally:
            self._release_robot_arm_busy(request.robotId)
            self._return_cancels.pop(task_id, None)

    # Multi-round orchestration: navigate once, then take books into stand
    # slots 1..N and return slots 1..M (M <= N) without moving the chassis.

    def create_multi_round_task(self, request: MultiRoundRequest, user: User) -> DispatchTask:
        """Create a navigate-once multi take/return task; a daemon thread runs it."""
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可创建会驱动机器人硬件的多轮取还书任务")
        with self._lock:
            robot = next((item for item in self.robots if item.id == request.robotId), None)
            if not robot:
                raise ValueError("机器人不存在")
            if robot.status == "busy":
                raise ValueError("机器人忙（机械臂作业中），请等待当前作业完成")
            if robot.status != "idle":
                raise ValueError(f"机器人 {robot.id} 当前不在空闲状态，不能执行多轮取还书任务")
            if request.sendNavigation and (
                robot.localizationStatus != "localized" or not robot.pose
            ):
                raise ValueError(f"机器人 {robot.id} 尚未完成可靠定位，暂不能导航")
            if len(request.returnBookIds) > len(request.takeBookIds):
                raise ValueError("还书数量不能超过取书数量（还书按顺序使用 1、2、3 号槽位的书）")
            takes = [self._require_book(book_id) for book_id in request.takeBookIds]
            returns = [self._require_book(book_id) for book_id in request.returnBookIds]
            active = next(
                (
                    item
                    for item in self.tasks
                    if item.type == "multi_round"
                    and item.robotId == robot.id
                    and item.status in {"queued", "running", "paused"}
                ),
                None,
            )
            superseded = None
            if active:
                # Same supersede rule as takeout/return: a stale non-terminal
                # multi-round must never permanently block new tasks.
                cancel_event = self._multi_round_cancels.get(active.id)
                if cancel_event is not None:
                    cancel_event.set()
                self._replace_task(
                    active.id,
                    active.model_copy(
                        update={
                            "status": "cancelled",
                            "workflowMessage": "已被新的多轮取还书任务取代",
                            "updatedAt": _now_iso(),
                        }
                    ),
                )
                superseded = active
                self._audit(user.username, "supersede_multi_round_task", active.id)

            task = DispatchTask(
                id=f"TSK-{uuid4().hex[:12].upper()}",
                type="multi_round",
                robotId=robot.id,
                target="、".join(book.name for book in takes),
                status="queued",
                progress=0,
                updatedAt=_now_iso(),
                mapId=request.mapId,
                annotationId=request.annotationId,
                workflowMessage="排队中",
                workflowDetails={
                    "sendNavigation": request.sendNavigation,
                    "target": request.target.model_dump() if request.target else None,
                    # 物理上须从支架内侧向外侧放置：N 本取书依次进 N..1 号槽
                    # （3 本即 3、2、1 号）；还书第 j 个目标取 j 号槽的书，
                    # 从外侧向内取回，两侧都不会被已放置的书挡住。
                    "takeBooks": [
                        self._multi_round_book_entry(book, len(takes) - index)
                        for index, book in enumerate(takes)
                    ],
                    "returnBooks": [
                        self._multi_round_book_entry(book, index + 1)
                        for index, book in enumerate(returns)
                    ],
                },
            )
            self.tasks.insert(0, task)
            self._multi_round_cancels[task.id] = Event()
            self._schedule_persist()
            self._audit(user.username, "create_multi_round_task", task.id)

        if superseded is not None:
            self._cancel_in_flight_stage_tasks(robot, superseded, user)
        thread = Thread(
            target=self._run_multi_round,
            args=(task.id, request, takes, returns, user),
            name=f"multi-round-{task.id}",
            daemon=True,
        )
        thread.start()
        return task

    def cancel_multi_round(self, task_id: str, user: User) -> DispatchTask:
        """Best-effort cancel: navigation cancel or bookarm cancel by phase."""
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可取消多轮取还书任务")
        with self._lock:
            task = next((item for item in self.tasks if item.id == task_id), None)
            if not task or task.type != "multi_round":
                raise ValueError("多轮取还书任务不存在")
            if task.status in {"completed", "failed", "cancelled", "recovery_required"}:
                raise ValueError("已结束的多轮取还书任务不能取消")
            event = self._multi_round_cancels.get(task_id)
            if event is not None:
                event.set()
            updated = task.model_copy(
                update={"workflowMessage": "已请求取消，正在停止当前阶段", "updatedAt": _now_iso()}
            )
            self._replace_task(task_id, updated)
            self._schedule_persist()
            self._audit(user.username, "cancel_multi_round", task_id)
            return updated

    def _require_book(self, book_id: str) -> Book:
        book = self.books.get(book_id)
        if book is None:
            raise ValueError(f"书籍 {book_id} 不存在，请先在书籍管理中登记")
        return book

    @staticmethod
    def _multi_round_book_entry(book: Book, slot: int) -> dict[str, Any]:
        return {
            "bookId": book.id,
            "name": book.name,
            "templateId": book.templateId,
            "heightMm": book.heightMm,
            "slot": slot,
            "status": "pending",
        }

    def _update_multi_round_entry(
        self, task_id: str, key: str, slot: int, **fields: Any
    ) -> None:
        details = self._takeout_workflow_details(task_id)
        entries = details.get(key)
        if not isinstance(entries, list):
            return
        updated_entries = [
            (
                {**entry, **fields}
                if isinstance(entry, dict) and entry.get("slot") == slot
                else entry
            )
            for entry in entries
        ]
        self._update_takeout_task(
            task_id, workflowDetails={**details, key: updated_entries}
        )

    def _create_bookarm_task_with_retry(
        self, cancel: Event | None, create: Callable[[], dict[str, Any]]
    ) -> str:
        """Create a bookarm sub-task; the daemon briefly answers 409 busy right
        after the previous task ends, so retry a few times before giving up."""
        last_error: Exception | None = None
        for _attempt in range(5):
            if cancel is not None and cancel.is_set():
                raise _FlowCancelled()
            try:
                return str(create().get("id"))
            except Exception as error:
                if getattr(error, "status", None) != 409:
                    raise
                last_error = error
                time.sleep(max(self.takeout_poll_interval_s, 0.5))
        raise ValueError(
            f"bookarm 服务持续繁忙，无法创建子任务：{describe_bookarm_detail(str(last_error))}"
        )

    @staticmethod
    def _bookarm_task_refs(task: DispatchTask) -> list[tuple[str, str]]:
        """(bookarm_task_id, kind) pairs referenced by one orchestration task;
        kind is 'takeout', 'midreturn' or 'return' and selects the bookarm getter."""
        details = task.workflowDetails or {}
        refs: list[tuple[str, str]] = []
        single = details.get("bookarmTaskId")
        if isinstance(single, str) and single:
            if task.type == "return":
                # 末尾还书（returnMode=end）的子任务在 bookarm /return/tasks 下。
                kind = "return" if details.get("returnMode") == "end" else "midreturn"
            else:
                kind = "takeout"
            refs.append((single, kind))
        for key, kind in (("takeBooks", "takeout"), ("returnBooks", "midreturn")):
            entries = details.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                entry_task_id = entry.get("bookarmTaskId")
                if isinstance(entry_task_id, str) and entry_task_id:
                    refs.append((entry_task_id, kind))
        return refs

    def _query_bookarm_state(self, client: Any, bookarm_task_id: str, kind: str) -> dict[str, Any]:
        """Read one bookarm task; falls back to the other workflows' getters
        when the expected one cannot find it (id recorded under the wrong kind)."""
        if kind == "takeout":
            getters = (client.get_takeout_task, client.get_midreturn_task)
        elif kind == "return":
            getters = (client.get_return_task, client.get_midreturn_task, client.get_takeout_task)
        else:
            getters = (client.get_midreturn_task, client.get_takeout_task)
        last_error: Exception | None = None
        for getter in getters:
            try:
                return getter(bookarm_task_id)
            except Exception as error:
                # 404-style misses fall through to the other getter; transport
                # failures (status None) are retried the same way.
                last_error = error
        raise last_error if last_error is not None else ValueError("bookarm 任务查询失败")

    _BOOKARM_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "unsupported", "recovery_required"}

    def dismiss_bookarm_recovery(self, bookarm_task_id: str, user: User) -> dict[str, Any]:
        """Proxy the operator's manual-recovery confirmation to bookarm, then
        settle any orchestration task parked in recovery_required on it."""
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可确认机械臂中断恢复")
        result = self._bookarm_client().dismiss_recovery(bookarm_task_id)
        with self._lock:
            for task in list(self.tasks):
                if task.status != "recovery_required":
                    continue
                if bookarm_task_id not in {ref for ref, _kind in self._bookarm_task_refs(task)}:
                    continue
                self._replace_task(
                    task.id,
                    task.model_copy(
                        update={
                            "status": "failed",
                            "workflowMessage": "已确认人工接管处理，机械臂中断任务已清除",
                            "updatedAt": _now_iso(),
                        }
                    ),
                )
            self._schedule_persist()
            self._audit(user.username, "dismiss_bookarm_recovery", bookarm_task_id)
        return result

    _VISION_IMAGE_NAME = re.compile(
        r"^(takeBooks|returnBooks)-[1-3]-(annotated|gap_overlay)\.jpg$"
    )
    _VISION_KIND_LABELS = {"annotated": "spine", "gap_overlay": "gap"}

    def _save_multi_round_vision(
        self, task_id: str, client: Any, key: str, slot: int, kinds: tuple[str, ...]
    ) -> None:
        """Best-effort: snapshot the bookarm vision debug images for this book
        as soon as a recognition completes — the next recognition overwrites
        them, so they must be copied before the next one starts. The stored
        URL carries a version query so a refreshed image busts the frontend's
        fetched copy."""
        images: dict[str, str] = {}
        version = int(time.time() * 1000)
        task_dir = self.multi_round_vision_dir / task_id
        for kind in kinds:
            try:
                data = client.get_vision_debug_image(kind)
                task_dir.mkdir(parents=True, exist_ok=True)
                (task_dir / f"{key}-{slot}-{kind}.jpg").write_bytes(data)
            except Exception:
                continue
            label = self._VISION_KIND_LABELS[kind]
            images[label] = (
                f"/api/v1/circulation/multi-round/{task_id}/vision/"
                f"{key}-{slot}-{kind}.jpg?v={version}"
            )
        if images:
            self._update_multi_round_entry(task_id, key, slot, visionImages=images)

    def multi_round_vision_path(self, task_id: str, name: str) -> Path:
        task = next(
            (
                item
                for item in self.tasks
                if item.id == task_id and item.type == "multi_round"
            ),
            None,
        )
        if task is None:
            raise ValueError("多轮取还书任务不存在")
        if not self._VISION_IMAGE_NAME.match(name):
            raise ValueError("识别图名称不合法")
        path = self.multi_round_vision_dir / task_id / name
        if not path.is_file():
            raise ValueError("识别图不存在")
        return path

    _FLOW_VISION_IMAGE_NAME = re.compile(r"^(takeout|return)-annotated\.jpg$")

    def _save_flow_vision(self, task_id: str, client: Any, prefix: str) -> None:
        """Best-effort: snapshot the bookarm spine-recognition image for one
        complete takeout/return task as soon as a vision stage completes (and
        once more at task end). The version query busts the frontend's copy
        when a later recognition overwrites the file."""
        try:
            data = client.get_vision_debug_image("annotated")
        except Exception:
            return
        task_dir = self.multi_round_vision_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / f"{prefix}-annotated.jpg").write_bytes(data)
        endpoint = "complete-takeout" if prefix == "takeout" else "complete-return"
        images = {
            "spine": (
                f"/api/v1/circulation/{endpoint}/{task_id}/vision/"
                f"{prefix}-annotated.jpg?v={int(time.time() * 1000)}"
            )
        }
        self._update_takeout_task(
            task_id,
            workflowDetails={
                **self._takeout_workflow_details(task_id),
                "visionImages": images,
            },
        )

    def flow_vision_path(self, task_id: str, task_type: str, name: str) -> Path:
        task = next(
            (item for item in self.tasks if item.id == task_id and item.type == task_type),
            None,
        )
        if task is None:
            raise ValueError("任务不存在")
        if not self._FLOW_VISION_IMAGE_NAME.match(name):
            raise ValueError("识别图名称不合法")
        path = self.multi_round_vision_dir / task_id / name
        if not path.is_file():
            raise ValueError("识别图不存在")
        return path

    def _park_after_multi_round(
        self, cancel: Event | None, client: Any, lift_speed_level: int
    ) -> None:
        """Safe-disable the arm and lower the lift to 10 mm after a successful
        multi-round run. The daemon briefly answers 409 busy right after the
        last task ends, so retry like bookarm task creation does."""
        last_error: Exception | None = None
        for _attempt in range(5):
            if cancel is not None and cancel.is_set():
                raise _FlowCancelled()
            try:
                client.disable_arm()
                client.move_lift(10.0, speed_level=lift_speed_level)
                return
            except Exception as error:
                if getattr(error, "status", None) != 409:
                    raise
                last_error = error
                time.sleep(max(self.takeout_poll_interval_s, 0.5))
        raise ValueError(f"bookarm 服务持续繁忙，无法完成收尾（失能/降台）：{last_error}")

    def _run_multi_round(
        self,
        task_id: str,
        request: MultiRoundRequest,
        takes: list[Book],
        returns: list[Book],
        user: User,
    ) -> None:
        """When sendNavigation is on, navigate once then fine-tune; afterwards
        (or immediately when off) run per-book takeout and mid-return. N takes
        fill stand slots N..1 inside-out (three books -> 3/2/1) so a placed
        book never blocks the next placement; return target j picks slot j
        (1/2/3), taking the outermost book first. The chassis never moves
        again after the navigation + fine-tune pair (daemon thread)."""

        cancel = self._multi_round_cancels.get(task_id)
        try:
            robot = next((item for item in self.robots if item.id == request.robotId), None)
            if robot is None:
                raise ValueError("机器人不存在")
            if cancel is not None and cancel.is_set():
                raise _FlowCancelled()
            if request.sendNavigation:
                self._update_takeout_task(
                    task_id, status="running", progress=5, workflowMessage="导航至目标点"
                )
                nav_task_id = self._dispatch_takeout_navigation(
                    task_id, robot, request, user, flow_label="多轮取还书", task_key="multiRoundTaskId"
                )
                self._wait_for_navigation(task_id, nav_task_id, cancel, user)
                if cancel is not None and cancel.is_set():
                    raise _FlowCancelled()

                self._update_takeout_task(task_id, progress=35, workflowMessage="低速精调对位")
                fine_tune_task_id = self._dispatch_takeout_fine_tune(
                    task_id, robot, request, user, flow_label="多轮取还书"
                )
                self._wait_for_fine_tune(task_id, fine_tune_task_id, cancel, user)
                if cancel is not None and cancel.is_set():
                    raise _FlowCancelled()
            else:
                self._update_takeout_task(
                    task_id,
                    status="running",
                    progress=5,
                    workflowMessage="已跳过导航，原地开始取还书",
                )

            total = len(takes) + len(returns)
            done = 0
            self._set_robot_arm_busy(request.robotId)
            client = self._bookarm_client()
            for index, book in enumerate(takes):
                slot = len(takes) - index
                label = f"取书 {index + 1}/{len(takes)}：《{book.name}》（放入槽位 {slot}）"
                if cancel is not None and cancel.is_set():
                    raise _FlowCancelled()
                self._update_takeout_task(
                    task_id, progress=40 + int(55 * done / total), workflowMessage=label
                )
                bookarm_task_id = self._create_bookarm_task_with_retry(
                    cancel,
                    lambda book=book, slot=slot: client.create_takeout_task(
                        template_id=book.templateId,
                        target_height_mm=book.heightMm,
                        destination_slot=slot,
                        lift_speed_level=request.liftSpeedLevel,
                        book_id=book.name,
                    ),
                )
                self._update_multi_round_entry(
                    task_id, "takeBooks", slot, status="running", bookarmTaskId=bookarm_task_id
                )
                state = self._wait_for_bookarm_task(
                    task_id,
                    client,
                    bookarm_task_id,
                    cancel,
                    get_task=client.get_takeout_task,
                    cancel_task=client.cancel_takeout_task,
                    flow_label=label,
                    on_vision_stage=lambda _stage, slot=slot: self._save_multi_round_vision(
                        task_id, client, "takeBooks", slot, ("annotated",)
                    ),
                )
                self._save_multi_round_vision(task_id, client, "takeBooks", slot, ("annotated",))
                if cancel is not None and cancel.is_set():
                    raise _FlowCancelled()
                if state != "succeeded":
                    raise ValueError(f"{label}未成功：{describe_bookarm_state(state)}")
                self._update_multi_round_entry(task_id, "takeBooks", slot, status="completed")
                done += 1

            for index, book in enumerate(returns):
                slot = index + 1
                label = f"还书 {index + 1}/{len(returns)}：《{book.name}》（槽位 {slot}）"
                if cancel is not None and cancel.is_set():
                    raise _FlowCancelled()
                self._update_takeout_task(
                    task_id, progress=40 + int(55 * done / total), workflowMessage=label
                )
                bookarm_task_id = self._create_bookarm_task_with_retry(
                    cancel,
                    lambda book=book, slot=slot: client.create_midreturn_task(
                        template_id=book.templateId,
                        target_height_mm=book.heightMm,
                        source_slot=slot,
                        lift_speed_level=request.liftSpeedLevel,
                        book_id=book.name,
                    ),
                )
                self._update_multi_round_entry(
                    task_id, "returnBooks", slot, status="running", bookarmTaskId=bookarm_task_id
                )
                state = self._wait_for_bookarm_task(
                    task_id,
                    client,
                    bookarm_task_id,
                    cancel,
                    get_task=client.get_midreturn_task,
                    cancel_task=client.cancel_midreturn_task,
                    flow_label=label,
                    on_vision_stage=lambda _stage, slot=slot: self._save_multi_round_vision(
                        task_id, client, "returnBooks", slot, ("annotated",)
                    ),
                )
                self._save_multi_round_vision(task_id, client, "returnBooks", slot, ("annotated",))
                if cancel is not None and cancel.is_set():
                    raise _FlowCancelled()
                if state != "succeeded":
                    raise ValueError(f"{label}未成功：{describe_bookarm_state(state)}")
                self._update_multi_round_entry(task_id, "returnBooks", slot, status="completed")
                done += 1

            self._update_takeout_task(
                task_id, progress=97, workflowMessage="收尾：机械臂安全失能，升降台降至 10mm"
            )
            try:
                self._park_after_multi_round(cancel, client, request.liftSpeedLevel)
            except Exception as error:
                raise ValueError(f"取还书已全部完成，但收尾失能/降台失败：{error}") from error
            self._update_takeout_task(
                task_id,
                status="completed",
                progress=100,
                workflowMessage="多轮取还书完成（机械臂已安全失能，升降台已降至 10mm）",
            )
        except _FlowCancelled:
            self._update_takeout_task(task_id, status="cancelled", workflowMessage="已取消")
        except _BookarmRecoveryRequired as error:
            # The interrupted sub-task may be holding a book; mark its entry
            # failed and park the whole flow in the manual-recovery state.
            for key in ("takeBooks", "returnBooks"):
                for entry in (self._takeout_workflow_details(task_id).get(key) or []):
                    if isinstance(entry, dict) and entry.get("status") == "running":
                        self._update_multi_round_entry(task_id, key, int(entry["slot"]), status="failed")
            self._update_takeout_task(
                task_id,
                status="recovery_required",
                workflowMessage="机械臂中断待确认（可能夹着书）",
                failureReason=str(error),
            )
        except Exception as error:
            if cancel is not None and cancel.is_set():
                self._update_takeout_task(
                    task_id, status="cancelled", workflowMessage="已取消"
                )
            else:
                self._update_takeout_task(
                    task_id,
                    status="failed",
                    workflowMessage="任务失败",
                    failureReason=str(error),
                )
        finally:
            self._release_robot_arm_busy(request.robotId)
            self._multi_round_cancels.pop(task_id, None)

    def move_lift(self, robot_id: str, request: MoveLiftRequest, user: User) -> RobotCommand:
        if not self._can_manually_control(user):
            raise PermissionError("仅维护人员可控制升降台")
        if abs(request.distanceCm) < 0.001:
            raise ValueError("升降台移动距离不能为 0")
        robot = next((item for item in self.robots if item.id == robot_id), None)
        if not robot:
            raise ValueError("机器人不存在")
        if robot.status == "busy":
            raise ValueError("机器人忙（机械臂作业中），请等待当前作业完成")
        if robot.status != "idle" or not robot.health.lift:
            raise ValueError("机器人必须待命且升降台健康才能点动")
        return self.create_command(
            robot,
            RobotCommandRequest(
                command="lift_move",
                payload={"distanceCm": request.distanceCm, "acknowledgeSafety": True},
                expiresAt=datetime.now(UTC) + timedelta(seconds=15),
            ),
            user,
            self.command_ttl_seconds,
        )

    def _can_manually_control(self, user: User) -> bool:
        if self.single_operator:
            return True
        return user.role in {"系统管理员", "维护人员"}

    def reset_software_emergency_stop(
        self, robot_id: str, request: ResetEmergencyStopRequest, user: User
    ) -> RobotSummary:
        """Clear only a web-requested E-stop lock after an administrator's inspection.

        This is intentionally not a hardware reset and emits no chassis command.
        A physical emergency stop or an unrelated hardware fault remains outside
        this operation and must be cleared at the robot.
        """
        if not self.single_operator and user.role != "系统管理员":
            raise PermissionError("仅系统管理员可解除软件紧急停止锁定")
        with self._lock:
            robot_index = next((index for index, item in enumerate(self.robots) if item.id == robot_id), None)
            if robot_index is None:
                raise ValueError("机器人不存在")
            robot = self.robots[robot_index]
            if robot.status != "fault" or robot.currentTask != "已请求紧急停止，等待 Gateway 确认":
                raise ValueError("该机器人没有可由平台解除的软件紧急停止锁定")
            if not robot.health.chassis:
                raise ValueError("底盘健康检查未通过，不能解除软件紧急停止锁定")
            updated_robot = robot.model_copy(
                update={
                    "status": "idle",
                    "currentTask": "软件紧急停止锁定已解除，等待维护人员接管",
                    "updatedAt": _now_iso(),
                }
            )
            self.robots[robot_index] = updated_robot
            self._schedule_persist()
            self._audit(user.username, "reset_software_emergency_stop", robot_id)
        self._notify_robot_update(updated_robot)
        return updated_robot

    def send_manual_jog(self, robot_id: str, request: ManualJogRequest) -> RobotCommand:
        """免鉴权直控点动：仅校验服务端安全限值后直接经 MQTT 下发。

        点动是每秒数次的实时指令，不写命令记录、不审计、不调度持久化。
        """
        robot = next((item for item in self.robots if item.id == robot_id), None)
        if not robot:
            raise ValueError("机器人不存在")
        if robot.status == "busy":
            raise ValueError("机器人忙（机械臂作业中），请等待当前作业完成")
        if robot.status == "mapping":
            raise ValueError("机器人正在建图：导航栈已停止，不能手动点动")
        if abs(request.linear) > 0.3 or abs(request.angular) > 0.8 or request.durationMs > 750:
            raise ValueError("手动点动超出安全限值")
        command = RobotCommand(
            commandId=f"CMD-{uuid4().hex[:12].upper()}",
            robotId=robot.id,
            command="manual_jog",
            payload={"linear": request.linear, "angular": request.angular, "durationMs": request.durationMs},
            expiresAt=datetime.now(UTC) + timedelta(milliseconds=request.durationMs + 250),
            idempotencyKey=secrets.token_urlsafe(18),
        )
        published = self.publisher.publish_command(command, None) if self.publisher else False
        if not published and self.require_mqtt:
            raise ValueError("MQTT Gateway 未连接，命令未下发")
        return command

    def create_map_annotation(
        self, map_id: str, request: CreateMapAnnotationRequest, user: User
    ) -> MapAnnotationResult:
        map_version = next((item for item in self.map_versions if item.id == map_id), None)
        if not map_version:
            raise ValueError("地图版本不存在")
        if request.pixelX >= map_version.width or request.pixelY >= map_version.height:
            raise ValueError("标注点超出地图图像范围")
        # Browser pixels start at the top left; ROS map coordinates start at the bottom left.
        map_x = map_version.origin.x + request.pixelX * map_version.resolution
        map_y = map_version.origin.y + (map_version.height - 1 - request.pixelY) * map_version.resolution
        label = request.label.strip()
        annotation = MapAnnotation(
            id=f"ANN-{uuid4().hex[:12].upper()}",
            mapId=map_id,
            kind=request.kind,
            label=label,
            pixelX=request.pixelX,
            pixelY=request.pixelY,
            mapX=map_x,
            mapY=map_y,
            yaw=request.yaw,
            createdAt=datetime.now(UTC),
            createdBy=user.username,
        )
        with self._lock:
            # 重名不阻断创建（同名点可能是有意的，如多楼层同编号书架），但提醒
            # 操作员核对；该警告只出现在本次响应里，不会持久化。
            duplicate = any(item.label == label for item in self.map_annotations.get(map_id, []))
            self.map_annotations.setdefault(map_id, []).append(annotation)
            self._schedule_persist()
            self._audit(user.username, "create_map_annotation", map_id, annotation.id)
        return MapAnnotationResult(
            **annotation.model_dump(),
            labelWarning=f"当前地图已存在同名标注「{label}」，请在列表中注意区分。" if duplicate else None,
        )

    def update_map_annotation(
        self, map_id: str, annotation_id: str, request: UpdateMapAnnotationRequest, user: User
    ) -> MapAnnotation:
        map_version = next((item for item in self.map_versions if item.id == map_id), None)
        if not map_version:
            raise ValueError("地图版本不存在")
        with self._lock:
            annotations = self.map_annotations.get(map_id, [])
            for index, annotation in enumerate(annotations):
                if annotation.id != annotation_id:
                    continue
                updates: dict[str, Any] = {}
                if request.label is not None:
                    label = request.label.strip()
                    if not label:
                        raise ValueError("标注名称不能为空")
                    updates["label"] = label
                if request.x is not None or request.y is not None:
                    # An omitted axis keeps the current value so single-axis moves work.
                    map_x = request.x if request.x is not None else annotation.mapX
                    map_y = request.y if request.y is not None else annotation.mapY
                    if not math.isfinite(map_x) or not math.isfinite(map_y):
                        raise ValueError("标注坐标必须是有限数值")
                    min_x = map_version.origin.x
                    min_y = map_version.origin.y
                    max_x = min_x + (map_version.width - 1) * map_version.resolution
                    max_y = min_y + (map_version.height - 1) * map_version.resolution
                    if not (min_x <= map_x <= max_x and min_y <= map_y <= max_y):
                        raise ValueError("标注坐标超出地图范围")
                    # Inverse of create_map_annotation: browser pixels start at the
                    # top left, ROS map coordinates start at the bottom left.
                    pixel_x = round((map_x - map_version.origin.x) / map_version.resolution)
                    pixel_y = round(
                        map_version.height - 1 - (map_y - map_version.origin.y) / map_version.resolution
                    )
                    updates["mapX"] = map_x
                    updates["mapY"] = map_y
                    updates["pixelX"] = min(max(pixel_x, 0), map_version.width - 1)
                    updates["pixelY"] = min(max(pixel_y, 0), map_version.height - 1)
                if request.yaw is not None:
                    if not math.isfinite(request.yaw):
                        raise ValueError("标注朝向必须是有限数值")
                    updates["yaw"] = request.yaw
                updated = annotation.model_copy(update=updates)
                annotations[index] = updated
                self._schedule_persist()
                self._audit(user.username, "update_map_annotation", map_id, annotation_id)
                return updated
        raise ValueError("地图标注不存在")

    def sync_cruise_annotations(self, map_id: str, user: User) -> list[MapAnnotation]:
        map_version = next((item for item in self.map_versions if item.id == map_id), None)
        if not map_version:
            raise ValueError("地图版本不存在")
        if not self.cruise_points_enabled:
            raise ValueError(
                "巡航点同步未启用：控制站未配置已批准的巡航点副本"
                "（LR_CRUISE_POINTS_ENABLED 与 LR_CRUISE_POINTS_FILE）。"
            )
        with self._lock:
            created = self._add_cruise_annotations(map_version, user)
            if created:
                self._schedule_persist()
                self._audit(user.username, "sync_cruise_annotations", map_id)
            return created

    def preview_cruise_annotations(self, map_id: str) -> CruiseSyncPreview:
        """Dry-run of sync_cruise_annotations: what would be created or skipped."""
        map_version = next((item for item in self.map_versions if item.id == map_id), None)
        if not map_version:
            raise ValueError("地图版本不存在")
        if not self.cruise_points_enabled:
            raise ValueError(
                "巡航点同步未启用：控制站未配置已批准的巡航点副本"
                "（LR_CRUISE_POINTS_ENABLED 与 LR_CRUISE_POINTS_FILE）。"
            )
        items: list[CruiseSyncPreviewItem] = []
        with self._lock:
            existing = list(self.map_annotations.get(map_id, []))
        for point in self._read_cruise_points():
            pixel_x = round((point.x - map_version.origin.x) / map_version.resolution)
            pixel_y = round(map_version.height - 1 - (point.y - map_version.origin.y) / map_version.resolution)
            if not (0 <= pixel_x < map_version.width and 0 <= pixel_y < map_version.height):
                items.append(
                    CruiseSyncPreviewItem(
                        name=point.name, x=point.x, y=point.y, yaw=point.yaw,
                        action="skip", reason="坐标超出当前地图范围",
                    )
                )
                continue
            if any(
                item.kind == "cruise" and item.pixelX == pixel_x and item.pixelY == pixel_y for item in existing
            ):
                items.append(
                    CruiseSyncPreviewItem(
                        name=point.name, x=point.x, y=point.y, yaw=point.yaw,
                        pixelX=pixel_x, pixelY=pixel_y,
                        action="skip", reason="地图上已存在相同位置的巡航标注",
                    )
                )
                continue
            items.append(
                CruiseSyncPreviewItem(
                    name=point.name, x=point.x, y=point.y, yaw=point.yaw,
                    pixelX=pixel_x, pixelY=pixel_y, action="create",
                )
            )
        return CruiseSyncPreview(file=str(self.cruise_points_file), items=items)

    def _read_cruise_points(self) -> list[CruisePoint]:
        if not self.cruise_points_file.is_file():
            return []
        try:
            return parse_cruise_points_yaml(self.cruise_points_file.read_bytes())
        except OSError as error:
            raise ValueError(f"无法读取巡航点配置：{self.cruise_points_file}") from error

    def _add_cruise_annotations(self, map_version: MapVersion, user: User) -> list[MapAnnotation]:
        # 总开关关闭或没有显式部署的副本时一律不注入：仓库内的
        # cruise-points.example.yaml 只是开发样例，不能落到真实地图上。
        if not self.cruise_points_enabled or not self.cruise_points_file.is_file():
            return []
        existing = self.map_annotations.setdefault(map_version.id, [])
        created: list[MapAnnotation] = []
        for point in self._read_cruise_points():
            pixel_x = round((point.x - map_version.origin.x) / map_version.resolution)
            pixel_y = round(map_version.height - 1 - (point.y - map_version.origin.y) / map_version.resolution)
            if not (0 <= pixel_x < map_version.width and 0 <= pixel_y < map_version.height):
                continue
            # Keep renamed cruise annotations intact when an existing map is synchronized again.
            if any(
                item.kind == "cruise" and item.pixelX == pixel_x and item.pixelY == pixel_y for item in existing
            ):
                continue
            annotation = MapAnnotation(
                id=f"ANN-{uuid4().hex[:12].upper()}",
                mapId=map_version.id,
                kind="cruise",
                label=point.name,
                pixelX=pixel_x,
                pixelY=pixel_y,
                mapX=point.x,
                mapY=point.y,
                yaw=point.yaw,
                createdAt=datetime.now(UTC),
                createdBy=user.username,
            )
            existing.append(annotation)
            created.append(annotation)
        return created

    def delete_map_annotation(self, map_id: str, annotation_id: str, user: User) -> None:
        if not any(item.id == map_id for item in self.map_versions):
            raise ValueError("地图版本不存在")
        with self._lock:
            annotations = self.map_annotations.get(map_id, [])
            if not any(item.id == annotation_id for item in annotations):
                raise ValueError("地图标注不存在")
            # 只拦截进行中任务（queued/running/paused）的引用；终态任务是历史
            # 记录，不应让标注永久不可删。
            blocking = next(
                (
                    task
                    for task in self.tasks
                    if task.mapId == map_id
                    and task.status in {"queued", "running", "paused"}
                    and (task.annotationId == annotation_id or annotation_id in task.routeAnnotationIds)
                ),
                None,
            )
            if blocking is not None:
                raise AnnotationInUseError(
                    f"该标注被进行中的任务 {blocking.id}（{blocking.type}，{blocking.status}）引用，"
                    "请先取消或等待该任务结束再删除；历史任务记录不受影响。"
                )
            self.map_annotations[map_id] = [item for item in annotations if item.id != annotation_id]
            self._schedule_persist()
            self._audit(user.username, "delete_map_annotation", map_id, annotation_id)

    def delete_map_version(self, map_id: str, user: User) -> None:
        """Remove a map with its annotations and on-disk files.

        The robot's current active map refuses deletion: the vehicle keeps the
        map loaded, so removing the station record would orphan the reference
        and strand the robot on an unregistered map.
        """
        with self._lock:
            using = next((item for item in self.robots if item.mapId == map_id), None)
            if using is not None:
                raise ValueError(f"该地图正在使用：机器人 {using.id} 的当前活动地图不能删除")
            self.map_versions = [item for item in self.map_versions if item.id != map_id]
            self.map_annotations.pop(map_id, None)
            self._schedule_persist()
            self._audit(user.username, "delete_map", map_id)
        # Best-effort file cleanup: missing files or directories are ignored.
        version_dir = self.map_storage_dir / map_id
        if version_dir.is_dir():
            for file_path in version_dir.iterdir():
                try:
                    file_path.unlink()
                except OSError:
                    pass
            try:
                version_dir.rmdir()
            except OSError:
                pass

    def delete_robot(self, robot_id: str, user: User) -> None:
        """Permanently remove operational records for a robot and retain only its deletion audit event."""
        with self._lock:
            robot = next((item for item in self.robots if item.id == robot_id), None)
            if not robot:
                raise ValueError("机器人不存在")
            self.robots = [item for item in self.robots if item.id != robot_id]
            self.tasks = [item for item in self.tasks if item.robotId != robot_id]
            self.alerts = [item for item in self.alerts if robot_id not in item.source]
            command_ids = {command_id for command_id, command in self.commands.items() if command.robotId == robot_id}
            self.commands = {
                command_id: command for command_id, command in self.commands.items() if command_id not in command_ids
            }
            self.idempotency = {
                key: command for key, command in self.idempotency.items() if command.commandId not in command_ids
            }
            self._telemetry_published.pop(robot_id, None)
            self._schedule_persist()
            self._audit(user.username, "delete_robot", robot_id)

    def _emergency_stop_arm_interlock(self) -> str:
        """Best-effort bookarm stop after the chassis E-stop was dispatched.
        Never raises: the arm outcome becomes the operator-facing detail."""
        try:
            outcome = self._bookarm_client().safety_stop(emergency=True)
        except Exception as error:
            return f"底盘已停止，机械臂停止失败（{error}）"
        if outcome.get("accepted", True):
            return "底盘已停止，机械臂已停止"
        return "底盘已停止，机械臂停止失败（bookarm 未接受停止请求）"

    def create_command(self, robot: RobotSummary, request, user: User, ttl_seconds: int) -> RobotCommand:
        if robot.status in {"fault", "offline"} and request.command not in {"emergency_stop", "cancel"}:
            if robot.status == "offline":
                raise ValueError(f"机器人已离线，不能执行 {request.command}")
            raise ValueError(f"机器人 {robot.id} 当前状态不允许执行 {request.command}")
        if robot.status == "busy" and request.command not in {"emergency_stop", "cancel", "stop"}:
            raise ValueError("机器人忙（机械臂作业中），请等待当前作业完成")
        if robot.status == "mapping" and request.command not in {"emergency_stop", "cancel", "stop"}:
            raise ValueError("机器人正在建图：导航栈已停止，请先结束建图恢复导航")
        if robot.status == "manual" and request.command not in {
            "start_manual_control",
            "manual_jog",
            "stop_manual_control",
            "emergency_stop",
            "cancel",
            "stop",
        }:
            raise ValueError(f"机器人 {robot.id} 正由维护人员接管，不能执行 {request.command}")
        if request.command == "navigate" and (robot.localizationStatus != "localized" or not robot.pose):
            raise ValueError(f"机器人 {robot.id} 尚未完成可靠定位，暂不能导航")
        key = request.idempotencyKey or secrets.token_urlsafe(18)
        with self._lock:
            if self.cache:
                cached = self.cache.get_idempotent_command(key)
                if cached:
                    return RobotCommand.model_validate(cached)
            if key in self.idempotency:
                return self.idempotency[key]
            expires = request.expiresAt or datetime.now(UTC) + timedelta(seconds=ttl_seconds)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= datetime.now(UTC):
                raise ValueError("命令已过期")
            command = RobotCommand(
                commandId=f"CMD-{uuid4().hex[:12].upper()}",
                robotId=robot.id,
                command=request.command,
                payload=request.payload,
                expiresAt=expires,
                idempotencyKey=key,
            )
            requires_command_lock = request.command not in {
                "start_manual_control",
                "manual_jog",
                "stop_manual_control",
                "emergency_stop",
                "stop",
            }
            lock_seconds = self.control_lock_seconds
            if self.cache and requires_command_lock and not self.cache.acquire_robot_lock(
                robot.id, command.commandId, lock_seconds
            ):
                raise ValueError(f"机器人 {robot.id} 已有正在处理的控制命令")
            # Persist the command before publishing. MQTT can return a
            # lifecycle event on another thread immediately after publish;
            # the event must be able to resolve this command and its task.
            self.commands[command.commandId] = command
            self.idempotency[key] = command
            self._schedule_persist()
            if self.cache:
                self.cache.set_idempotent_command(key, command.model_dump(mode="json"), self.command_ttl_seconds)
            if self.publisher and not self.publisher.publish_command(command, user):
                if self.require_mqtt:
                    self.commands.pop(command.commandId, None)
                    self.idempotency.pop(key, None)
                    if self.cache:
                        self.cache.delete_idempotent_command(key)
                        self.cache.release_robot_lock(robot.id, command.commandId)
                    self._schedule_persist()
                    raise ValueError("MQTT Gateway 未连接，命令未下发")
            self._audit(user.username, request.command, robot.id, command.commandId)
            if request.command == "emergency_stop":
                index = next(index for index, item in enumerate(self.robots) if item.id == robot.id)
                self.robots[index] = robot.model_copy(
                    update={
                        "status": "fault",
                        "currentTask": "已请求紧急停止，等待 Gateway 确认",
                        "updatedAt": _now_iso(),
                    }
                )
                # Abort every live takeout/return/multi-round/annotation-nav
                # flow of this robot; each orchestration loop responds to its
                # cancel event at the next checkpoint.
                self._cancel_robot_flows_locked(robot.id, "紧急停止：已请求取消，正在停止当前阶段")
                self._schedule_persist()
        if request.command == "emergency_stop":
            # The bookarm socket call must run outside the store lock: a slow
            # daemon must never freeze dispatch state for the other threads.
            command = command.model_copy(update={"detail": self._emergency_stop_arm_interlock()})
        return command

    def _replace_task(self, task_id: str, replacement: DispatchTask) -> None:
        index = next((index for index, item in enumerate(self.tasks) if item.id == task_id), None)
        if index is not None:
            self.tasks[index] = replacement

    def set_robot_initial_pose(
        self, robot_id: str, request: SetInitialPoseRequest, user: User, ttl_seconds: int
    ) -> RobotCommand:
        """Send an operator-confirmed map pose to AMCL through the robot gateway.

        This confirms localization only. It never creates a navigation goal or
        commands the chassis to move.
        """
        robot = next((item for item in self.robots if item.id == robot_id), None)
        if not robot:
            raise ValueError("机器人不存在")
        if robot.status in {"fault", "offline"}:
            raise ValueError(f"机器人 {robot.id} 当前离线或故障，不能确认位置")
        # 允许在 localized 状态下重新确认位置：操作员主动点击即认为当前定位不可信，
        # 车端会以点击位姿为种子做激光精修并重新收敛。
        if robot.mapId != request.mapId:
            raise ValueError("请选择机器人当前加载的地图进行位置确认")
        map_version = next((item for item in self.map_versions if item.id == request.mapId), None)
        if not map_version:
            raise ValueError("地图版本不存在")
        if not all(math.isfinite(value) for value in (request.x, request.y, request.yaw)):
            raise ValueError("位置和朝向必须是有效数字")
        max_x = map_version.origin.x + (map_version.width - 1) * map_version.resolution
        max_y = map_version.origin.y + (map_version.height - 1) * map_version.resolution
        if not map_version.origin.x <= request.x <= max_x or not map_version.origin.y <= request.y <= max_y:
            raise ValueError("确认位置超出当前地图范围")
        return self.create_command(
            robot,
            RobotCommandRequest(
                command="set_initial_pose",
                idempotencyKey=secrets.token_urlsafe(18),
                payload={
                    "mapId": request.mapId,
                    "frameId": request.frameId,
                    "x": request.x,
                    "y": request.y,
                    "yaw": request.yaw,
                },
            ),
            user,
            ttl_seconds,
        )

    def auto_localize_robot(self, robot_id: str, user: User, ttl_seconds: int) -> RobotCommand:
        """Ask the robot to run a whole-map laser match and localize itself.

        This never moves the chassis; the gateway reports the same lifecycle
        events as set_initial_pose (localization_initializing / command_rejected
        / command_completed).
        """
        robot = next((item for item in self.robots if item.id == robot_id), None)
        if not robot:
            raise ValueError("机器人不存在")
        if robot.status in {"fault", "offline"}:
            raise ValueError(f"机器人 {robot.id} 当前离线或故障，不能自动定位")
        if not robot.mapId:
            raise ValueError(f"机器人 {robot.id} 尚未加载地图，不能自动定位")
        return self.create_command(
            robot,
            RobotCommandRequest(
                command="auto_localize",
                idempotencyKey=secrets.token_urlsafe(18),
                payload={"mapId": robot.mapId},
            ),
            user,
            ttl_seconds,
        )

    def switch_robot_map(self, robot_id: str, request: SwitchMapRequest, user: User) -> RobotCommand:
        """Queue a map switch; the robot re-localizes after the Gateway loads the map.

        Task occupancy never blocks a map switch: reloading the map aborts
        whatever the navigation stack was doing, so every non-terminal task of
        this robot is cancelled as part of the switch instead of rejecting it.
        """
        robot = next((item for item in self.robots if item.id == robot_id), None)
        if not robot:
            raise ValueError("机器人不存在")
        if robot.status == "mapping":
            raise ValueError("机器人正在建图，请先结束建图恢复导航后再切换地图")
        map_version = next((item for item in self.map_versions if item.id == request.mapId), None)
        if not map_version:
            raise ValueError("地图不存在")
        with self._lock:
            for task in list(self.tasks):
                if task.robotId != robot.id or task.status not in {"queued", "running", "paused"}:
                    continue
                cancel_event = (
                    self._takeout_cancels.get(task.id)
                    or self._return_cancels.get(task.id)
                    or self._multi_round_cancels.get(task.id)
                )
                if cancel_event is not None:
                    cancel_event.set()
                self._replace_task(
                    task.id,
                    task.model_copy(
                        update={
                            "status": "cancelled",
                            "workflowMessage": "地图切换，任务终止",
                            "updatedAt": _now_iso(),
                        }
                    ),
                )
                self._audit(user.username, "auto_cancel_on_map_switch", robot.id, task.id)
        return self.create_command(
            robot,
            RobotCommandRequest(
                command="switch_map",
                idempotencyKey=secrets.token_urlsafe(18),
                payload={"mapId": map_version.id},
            ),
            user,
            60,
        )

    def wait_map_switch_result(
        self, robot_id: str, map_id: str, command_id: str, timeout_seconds: float
    ) -> tuple[str, str]:
        """Block until the vehicle reports the switch outcome (direct per-click UX).

        Returns (succeeded|failed|rejected|timeout, reason). The underlying
        transport is still the reviewed MQTT command, but the caller observes
        one synchronous result instead of fire-and-forget queueing.
        """
        deadline = time.monotonic() + timeout_seconds
        try:
            while time.monotonic() < deadline:
                with self._lock:
                    outcome = self._map_switch_outcomes.pop(command_id, None)
                    robot = next((item for item in self.robots if item.id == robot_id), None)
                    switched = robot is not None and robot.mapId == map_id
                if outcome is not None:
                    return outcome
                if switched:
                    return ("succeeded", "")
                time.sleep(0.3)
            return ("timeout", "等待车端切换结果超时，请查看机器人状态")
        finally:
            with self._lock:
                self._map_switch_outcomes.pop(command_id, None)

    def handle_gateway_event(self, event: dict[str, object]) -> None:
        """Apply robot-produced MQTT lifecycle events to the dispatch view.

        MQTT ACLs must ensure that only each robot can publish its own event topic;
        this validation additionally prevents an unrelated event from mutating state.
        """
        event_name = event.get("event")
        robot_id = event.get("robotId")
        command_id = event.get("commandId")
        if not isinstance(event_name, str) or not isinstance(robot_id, str):
            return
        if command_id is not None and not isinstance(command_id, str):
            return
        with self._lock:
            command = self.commands.get(command_id) if command_id else None
            if command and command.robotId != robot_id:
                return
            robot_index = next((index for index, item in enumerate(self.robots) if item.id == robot_id), None)
            if robot_index is None:
                return
            task_id = ""
            if command_id:
                task_id = command_id.replace("CMD-", "TSK-", 1)
            task_index = next((index for index, item in enumerate(self.tasks) if item.id == task_id), None)
            task = self.tasks[task_index] if task_index is not None else None
            robot = self.robots[robot_index]
            updated_at = _now_iso()
            details = event.get("details") if isinstance(event.get("details"), dict) else {}

            if command and event_name in {
                "command_rejected",
                "command_completed",
                "localization_initializing",
                "navigation_succeeded",
                "navigation_failed",
                "navigation_cancelled",
                "cruise_succeeded",
                "cruise_failed",
                "cruise_cancelled",
                "lift_move_completed",
                "lift_move_driver_acknowledged",
                "map_switched",
                "map_switch_succeeded",
                "map_switch_failed",
            }:
                if self.cache:
                    self.cache.release_robot_lock(robot_id, command.commandId)
            if event_name == "localization_initializing" and command:
                self.robots[robot_index] = robot.model_copy(
                    update={
                        "localizationStatus": "manual_initializing",
                        "pose": None,
                        "location": f"{robot.mapVersion} / 正在确认位置",
                        "updatedAt": updated_at,
                    }
                )
                self._schedule_persist()
                self._audit("gateway", event_name, robot_id, command_id)
                self._notify_robot_update(self.robots[robot_index])
                return

            if event_name == "telemetry":
                telemetry = self._gateway_telemetry(event.get("details"), event.get("timestamp"))
                if not telemetry:
                    return
                map_version = next((item for item in self.map_versions if item.id == telemetry[0]), None)
                # Telemetry from a map the station has not registered must still
                # refresh pose/health; only the map name falls back to a
                # placeholder so the operator sees the missing map record.
                map_name = map_version.name if map_version else "未注册地图"
                pose = telemetry[1]
                health_update: dict[str, bool] = {"navigation": telemetry[2], "chassis": telemetry[3]}
                # Older Gateways do not publish book-arm fields. Keep their
                # last known values instead of treating an absent field as a
                # hardware failure during a staged vehicle upgrade. When the
                # local bookarm health poller is active it owns these keys —
                # the gateway's legacy HTTP reader is dead since the bookarm
                # service moved to a unix socket.
                for detail_key, health_key in (
                    ("armReady", "arm"),
                    ("rodReady", "rod"),
                    ("liftReady", "lift"),
                    ("cameraReady", "camera"),
                    ("liftAbsolutePositioningReady", "liftAbsolutePositioningReady"),
                ):
                    if self._bookarm_health_active and health_key in {"arm", "rod", "lift", "camera"}:
                        continue
                    value = details.get(detail_key)
                    if isinstance(value, bool):
                        health_update[health_key] = value
                # An absent or non-boolean imuReady means the Gateway cannot
                # prove the IMU is alive; fail visible instead of keeping a
                # stale healthy value (old Gateways therefore report imu=False).
                imu_ready = details.get("imuReady")
                health_update["imu"] = imu_ready if isinstance(imu_ready, bool) else False
                health = robot.health.model_copy(update=health_update)
                diagnostic_update = robot.armDiagnostics.model_dump()
                if not self._bookarm_health_active:
                    if isinstance(details.get("armConnected"), bool):
                        diagnostic_update["connected"] = details["armConnected"]
                    if isinstance(details.get("armFeedbackHealthy"), bool):
                        diagnostic_update["feedbackHealthy"] = details["armFeedbackHealthy"]
                    if isinstance(details.get("armCanFps"), (int, float)) and not isinstance(
                        details.get("armCanFps"), bool
                    ):
                        diagnostic_update["canFps"] = float(details["armCanFps"])
                    if isinstance(details.get("armFeedbackReason"), str):
                        diagnostic_update["feedbackReason"] = details["armFeedbackReason"][:240]
                arm_diagnostics = RobotArmDiagnostics.model_validate(diagnostic_update)
                robot_update: dict[str, object] = {
                    "mapId": map_version.id if map_version else telemetry[0],
                    "mapVersion": map_name,
                    "health": health,
                    "armDiagnostics": arm_diagnostics,
                    "localizationStatus": telemetry[5],
                    "localizationScanMode": telemetry[6],
                    "lastSeenAt": telemetry[4],
                    "status": "idle" if robot.status == "offline" else robot.status,
                    "updatedAt": updated_at,
                }
                if pose:
                    robot_update.update(
                        {
                            "pose": RobotPose(
                                frameId="map",
                                x=pose[0],
                                y=pose[1],
                                yaw=pose[2],
                                capturedAt=telemetry[4],
                            ),
                            "location": f"{map_name} ({pose[0]:.2f}, {pose[1]:.2f}) m",
                        }
                    )
                else:
                    # No AMCL pose means the robot is not localized; do not keep rendering a stale point as live.
                    robot_update.update({"pose": None, "location": f"{map_name} / 未定位"})
                updated_robot = robot.model_copy(update=robot_update)
                self.robots[robot_index] = updated_robot
                # Throttled frames still refresh the in-memory summary above
                # (lastSeenAt included, so gateway liveness is unaffected);
                # only the DB write and WS push are skipped.
                if not self._telemetry_publish_throttled(updated_robot):
                    self._telemetry_published[updated_robot.id] = (datetime.now(UTC), updated_robot)
                    self._persist_robot_telemetry(updated_robot)
                    self._notify_robot_update(updated_robot)
                return

            if event_name == "lift_move_completed" and command:
                self.robots[robot_index] = robot.model_copy(
                    update={"currentTask": "升降台点动完成", "updatedAt": updated_at}
                )
            elif event_name == "lift_move_driver_acknowledged" and command:
                self.robots[robot_index] = robot.model_copy(
                    update={
                        "currentTask": "升降台命令已接收，等待现场确认",
                        "updatedAt": updated_at,
                    }
                )
            elif event_name in {"map_switched", "map_switch_succeeded"} and command and command.command == "switch_map":
                # The switched map id comes from the persisted command, not
                # from free-form event details, so a stale event cannot move
                # the robot onto an unexpected map.
                switched_map_id = command.payload.get("mapId")
                if not isinstance(switched_map_id, str) or not switched_map_id:
                    return
                switched_map = next((item for item in self.map_versions if item.id == switched_map_id), None)
                switched_map_name = switched_map.name if switched_map else "未注册地图"
                self.robots[robot_index] = robot.model_copy(
                    update={
                        "mapId": switched_map_id,
                        "mapVersion": switched_map_name,
                        "localizationStatus": "global_localizing",
                        "pose": None,
                        "location": f"{switched_map_name} / 正在重新定位",
                        "updatedAt": updated_at,
                    }
                )
                self._map_switch_outcomes[command.commandId] = ("succeeded", "")
            elif event_name == "map_switch_failed" and command and command.command == "switch_map":
                reason = details.get("reason")
                self._map_switch_outcomes[command.commandId] = (
                    "failed",
                    reason if isinstance(reason, str) and reason else "地图切换失败",
                )
                self.robots[robot_index] = robot.model_copy(
                    update={"currentTask": "地图切换失败", "updatedAt": updated_at}
                )
            elif event_name == "command_rejected" and command and command.command == "switch_map":
                reason = details.get("reason")
                self._map_switch_outcomes[command.commandId] = (
                    "rejected",
                    reason if isinstance(reason, str) and reason else "车端拒绝了切换请求",
                )
                self.robots[robot_index] = robot.model_copy(
                    update={"currentTask": "地图切换被拒绝", "updatedAt": updated_at}
                )

            elif event_name == "cruise_started" and task:
                waypoint_index = details.get("waypointIndex")
                waypoint_count = details.get("waypointCount")
                loop_index = details.get("loopIndex")
                loop_count = details.get("loopCount")
                if all(
                    isinstance(value, int) and not isinstance(value, bool)
                    for value in (waypoint_index, waypoint_count, loop_index, loop_count)
                ) and waypoint_count > 0 and loop_count > 0:
                    completed_waypoints = (loop_index - 1) * waypoint_count + waypoint_index - 1
                    progress = min(99, max(0, completed_waypoints * 100 // (waypoint_count * loop_count)))
                    current_task = f"巡航第 {waypoint_index}/{waypoint_count} 点，第 {loop_index}/{loop_count} 轮"
                else:
                    progress = task.progress
                    current_task = f"正在执行 {task.target}"
                self.tasks[task_index] = task.model_copy(
                    update={"status": "running", "progress": progress, "updatedAt": updated_at}
                )
                if robot.status != "busy":
                    # During arm work the orchestration owns the robot status.
                    self.robots[robot_index] = robot.model_copy(
                        update={"status": "navigating", "currentTask": current_task, "updatedAt": updated_at}
                    )
            if event_name in {
                "navigation_dispatching",
                "navigation_started",
                "cruise_dispatching",
            } and task:
                self.tasks[task_index] = task.model_copy(update={"status": "running", "updatedAt": updated_at})
                if robot.status != "busy":
                    # During arm work the orchestration owns the robot status.
                    self.robots[robot_index] = robot.model_copy(
                        update={"status": "navigating", "currentTask": f"正在执行 {task.target}", "updatedAt": updated_at}
                    )
            elif event_name in {"navigation_succeeded", "cruise_succeeded"} and task:
                self.tasks[task_index] = task.model_copy(
                    update={"status": "completed", "progress": 100, "updatedAt": updated_at}
                )
                if robot.status != "busy":
                    # A late navigation event must not clear the arm-busy state.
                    self.robots[robot_index] = robot.model_copy(
                        update={"status": "idle", "location": task.target, "currentTask": "待命", "updatedAt": updated_at}
                    )
            elif (
                event_name == "command_completed"
                and task
                and command
                and command.command == "fine_tune"
            ):
                # fine_tune reports through the generic command events instead
                # of the navigation lifecycle, so its dispatch row ends here.
                self.tasks[task_index] = task.model_copy(
                    update={"status": "completed", "progress": 100, "updatedAt": updated_at}
                )
            elif event_name in {"command_rejected", "navigation_failed", "cruise_failed"} and task:
                failure_update: dict[str, object] = {"status": "failed", "updatedAt": updated_at}
                # Keep the vehicle's rejection/failure reason for every command
                # type (navigate, cruise, fine_tune…) so the orchestration and
                # the operator see what the vehicle actually reported.
                reason = details.get("reason")
                if isinstance(reason, str) and reason:
                    failure_update["failureReason"] = reason
                self.tasks[task_index] = task.model_copy(update=failure_update)
                if robot.status != "busy":
                    self.robots[robot_index] = robot.model_copy(
                        update={"status": "idle", "currentTask": "导航任务失败，等待调度处理", "updatedAt": updated_at}
                    )
            elif event_name in {"navigation_cancelled", "cruise_cancelled"} and task:
                # Cancelled is a terminal state: the vehicle will not resume
                # this command, so the dispatch row must not linger in paused.
                self.tasks[task_index] = task.model_copy(update={"status": "cancelled", "updatedAt": updated_at})
                if robot.status != "busy":
                    self.robots[robot_index] = robot.model_copy(
                        update={"status": "idle", "currentTask": "导航已取消，等待调度处理", "updatedAt": updated_at}
                    )
            elif event_name not in {
                "lift_move_completed",
                "lift_move_driver_acknowledged",
                "map_switched",
                "map_switch_succeeded",
                "map_switch_failed",
            } and not (
                event_name == "command_rejected"
                and command
                and command.command == "switch_map"
            ) and not (event_name == "cruise_started" and task):
                return
            self._schedule_persist()
            self._audit("gateway", event_name, robot_id, command_id)
            self._notify_robot_update(self.robots[robot_index])

    def _gateway_telemetry(
        self, details: object, timestamp: object
    ) -> tuple[str, tuple[float, float, float] | None, bool, bool, datetime, str, str] | None:
        if not isinstance(details, dict) or details.get("frameId") != "map":
            return None
        map_id = details.get("mapId")
        navigation_ready = details.get("nav2Ready")
        chassis_ready = details.get("chassisReady")
        localization_status = details.get("localizationStatus", "unknown")
        localization_scan_mode = details.get("localizationScanMode", "unknown")
        if (
            not isinstance(map_id, str)
            or not isinstance(navigation_ready, bool)
            or not isinstance(chassis_ready, bool)
        ):
            return None
        if localization_status not in {
            "starting",
            "restoring_saved_pose",
            "global_localizing",
            "manual_initializing",
            "localized",
            "manual_required",
        }:
            localization_status = "unknown"
        if localization_scan_mode not in {"dual", "single_laser_1", "single_laser_2", "unavailable"}:
            localization_scan_mode = "unknown"
        if not isinstance(timestamp, str):
            return None
        try:
            captured_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if captured_at.tzinfo is None:
            return None
        values = (details.get("x"), details.get("y"), details.get("yaw"))
        if all(value is None for value in values):
            pose = None
        elif all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
            for value in values
        ):
            pose = (float(values[0]), float(values[1]), float(values[2]))
        else:
            return None
        return (
            map_id,
            pose,
            navigation_ready,
            chassis_ready,
            captured_at.astimezone(UTC),
            localization_status,
            localization_scan_mode,
        )

    def _telemetry_publish_throttled(self, robot: RobotSummary) -> bool:
        """Return True when a telemetry frame is too similar to the last published one."""
        previous = self._telemetry_published.get(robot.id)
        if not previous:
            return False
        published_at, snapshot = previous
        if (datetime.now(UTC) - published_at).total_seconds() >= 1.0:
            return False
        if (
            robot.health != snapshot.health
            or robot.localizationStatus != snapshot.localizationStatus
            or robot.mapId != snapshot.mapId
        ):
            return False
        if robot.pose and snapshot.pose:
            distance = math.hypot(robot.pose.x - snapshot.pose.x, robot.pose.y - snapshot.pose.y)
            yaw_delta = abs(robot.pose.yaw - snapshot.pose.yaw)
            return distance < 0.02 and yaw_delta < 0.01
        # A pose appearing or disappearing is a visible change that must reach
        # operators immediately; only two pose-less frames can throttle.
        return robot.pose is None and snapshot.pose is None

    def _persist_robot_telemetry(self, robot: RobotSummary) -> None:
        if not self.database:
            return
        try:
            self.database.update_robot_telemetry(
                RobotModel(
                    id=robot.id,
                    name=robot.name,
                    status=robot.status,
                    location=robot.location,
                    current_task=robot.currentTask,
                    map_version=robot.mapVersion,
                    map_id=robot.mapId,
                    pose=robot.pose.model_dump(mode="json") if robot.pose else None,
                    localization_status=robot.localizationStatus,
                    localization_scan_mode=robot.localizationScanMode,
                    last_seen_at=robot.lastSeenAt,
                    health={
                        **robot.health.model_dump(),
                        "armDiagnostics": robot.armDiagnostics.model_dump(),
                    },
                    updated_at=robot.updatedAt,
                )
            )
        except Exception:
            # Runs on the paho MQTT thread twice per second: never let a
            # storage error escape (it once killed the paho loop thread and
            # froze all robot state), and throttle the log to avoid spam
            # while the database is unhealthy.
            now = time.monotonic()
            if now - self._last_telemetry_persist_error_log >= 30.0:
                self._last_telemetry_persist_error_log = now
                _logger.exception("遥测持久化失败，内存状态继续更新（30 秒内不再重复报告）")


store = InMemoryStore()
