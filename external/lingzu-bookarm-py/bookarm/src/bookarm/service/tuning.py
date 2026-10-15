"""Motion-plan tuning behind the ``/api/v1/tuning`` routes.

The tuning API exposes the live take-book, return-book and mid-return motion
plans for inspection and partial JSON updates. Every accepted update is
validated by the stock profile loaders, hot-swapped into the running use case
(the use cases read ``self.plan`` per step, so the next motion picks it up)
and persisted under ``recordings/tuning`` so a restart keeps the tuned values.

Each workflow kind also owns a :class:`TuningSession` that drives the same
motions the use cases run, one step at a time, so parameters can be verified
without executing a whole workflow. All hardware actions serialize against
workflow execution through the runner's busy lock, exactly like the studio.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple, Union

from fastapi import APIRouter, HTTPException, Query

from bookarm.application.use_cases.mid_return import OffworkerArmProbe, insert_chain_poses
from bookarm.domain.models.book import BookTarget, ShelfPlacementTarget, SpineTemplate
from bookarm.domain.models.pose import BasePose, JointPose
from bookarm.domain.models.shelf import BookStandPickupPlan, SlotPlacementPlan
from bookarm.domain.plans.mid_return import MidReturnMotionPlan
from bookarm.domain.plans.return_book import ReturnBookMotionPlan
from bookarm.domain.plans.speed import apply_speed_to_gripper, remove_speed_scaling
from bookarm.domain.plans.take_book import GripperCommand, TakeBookMotionPlan
from bookarm.domain.policies.safety import ensure_joint_target
from bookarm.infrastructure.config.calibration import (
    load_book_stand_pickup_slots,
    load_book_stand_slots,
)
from bookarm.infrastructure.config.profiles import (
    load_mid_return_motion_plan,
    load_return_book_motion_plan,
    load_take_book_motion_plan,
)
from bookarm.ports.devices import ArmPort, RodPort, VisionPort
from bookarm.service.schemas import BookStandExecuteRequest, TuningPrepareRequest, TuningStepRunRequest
from bookarm.service.studio import (
    ArmNotEnabledError,
    ArmUnavailableError,
    StudioBackend,
    is_kinematics_error,
)


class TuningBusyError(RuntimeError):
    """Raised when a tuning action collides with a running workflow task."""


# Seconds a run-step waits for the busy lock after requesting cancellation of
# the in-flight workflow: the cancel lands at the next workflow stage
# boundary, and a concurrently running tuning step (a monolithic motion
# closure in another request thread) cannot and must not be aborted
# mid-motion — motions are seconds-long, so the bounded wait covers both.
RUN_PREEMPT_TIMEOUT_S = 30.0


class TuningProfileError(ValueError):
    """Raised when a profile update fails the stock loader validation."""


class TuningNotPreparedError(RuntimeError):
    """Raised when a step needs a vision target but the session has none."""


class TuningInputError(RuntimeError):
    """Raised for invalid tuning input: uncalibrated slot, bad step index."""


class TuningHardwareUnavailableError(RuntimeError):
    """Raised when a hardware command cannot reach the physical device."""


def _hardware_unavailable(error: BaseException) -> HTTPException:
    """503 view shared by every interactive execution route."""

    return HTTPException(
        status_code=503,
        detail=(
            "BookArm 硬件状态不可用：%s。请确认 can0、CANable、机械臂供电、急停及各关节/"
            "夹爪反馈；确认关节故障后再重试。" % error
        ),
    )


# Hardware-free vision result shared by both fake sessions.
FAKE_TARGET_POSE = BasePose(-0.35, -0.07, 0.23, 0.0, 0.0, 0.0)


@dataclass(frozen=True)
class TuningDevices:
    """Device services the tuning steps drive directly."""

    arm: ArmPort
    rod: RodPort
    vision: VisionPort


class FakeTuningArm:
    """Hardware-free ArmPort: accepts every motion and echoes joint targets."""

    def __init__(self) -> None:
        self._enabled = False
        self._gripper_ready = True
        self._joints = JointPose.from_values((0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        self._lock = threading.Lock()
        self.pose_moves: List[Tuple[str, BasePose, float]] = []
        # Labels of every reachability probe, in call order (tests use them to
        # prove when and on which code path IK ran).
        self.ik_probes: List[str] = []

    def is_enabled(self) -> bool:
        return self._enabled

    def is_gripper_ready(self) -> bool:
        return self._enabled and self._gripper_ready

    def enable(self) -> None:
        self._enabled = True
        self._gripper_ready = True

    def disable_at_safe_pose(self, duration_s: float = 3.0) -> Dict[str, Any]:
        duration_s = None
        self._enabled = False
        return {"fake": True, "enabled": False}

    def move_j(self, target: JointPose, duration_s: float) -> None:
        duration_s = None
        self._require_enabled()
        with self._lock:
            # read_joint_pose echoes the last target so the ensure_joint_target
            # checks mirrored from the use cases pass without hardware.
            self._joints = target

    def read_joint_pose(self) -> JointPose:
        with self._lock:
            return self._joints

    def move_to_pose_by_move_j(self, target: BasePose, duration_s: float) -> None:
        self._require_enabled()
        with self._lock:
            self.pose_moves.append(("move_p_by_j", target, float(duration_s)))

    def move_l(self, target: BasePose, duration_s: float) -> None:
        self._require_enabled()
        with self._lock:
            self.pose_moves.append(("move_l", target, float(duration_s)))

    def check_pose_reachable(
        self, target: BasePose, seed: Optional[JointPose], *, label: str
    ) -> JointPose:
        """Hardware-free reachability probe: every pose passes, echoing a fixed pose."""

        target = None
        with self._lock:
            self.ik_probes.append(label)
            return seed if seed is not None else self._joints

    def check_pose_reachable_offworker(
        self, target: BasePose, seed: JointPose, *, label: str
    ) -> JointPose:
        """Hardware-free off-worker probe: every pose passes, echoing the seed."""

        target = None
        with self._lock:
            self.ik_probes.append(label)
            return seed

    def open_gripper(self, target_deg: float, command: Optional[GripperCommand] = None) -> None:
        target_deg = command = None
        self._require_enabled()

    def close_gripper_monitored(self, command: GripperCommand) -> Dict[str, Any]:
        self._require_enabled()
        return {"fake": True, "close_deg": command.close_deg}

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise RuntimeError("fake tuning arm is not enabled")


class FakeTuningRod:
    """Hardware-free RodPort remembering only the last commanded angle."""

    def __init__(self) -> None:
        self.angle_deg = 0.0

    def move_to_deg(
        self,
        target_deg: float,
        speed: int,
        acceleration: int,
        tolerance_deg: float,
        timeout_s: float,
    ) -> None:
        speed = acceleration = tolerance_deg = timeout_s = None
        self.angle_deg = float(target_deg)


class FakeTuningVision:
    """Hardware-free VisionPort returning a fixed base-frame target."""

    def locate_book(self, template: SpineTemplate) -> BookTarget:
        template = None
        return BookTarget(
            raw_pose=FAKE_TARGET_POSE,
            grasp_pose=FAKE_TARGET_POSE,
            pixel_uv=(320, 240),
            confidence=0.99,
            capture_id="fake-tuning-capture",
        )

    def locate_shelf_placement(
        self,
        left_neighbor_template: SpineTemplate,
        *,
        reference_point_from_top_ratio: float,
    ) -> ShelfPlacementTarget:
        left_neighbor_template = reference_point_from_top_ratio = None
        return ShelfPlacementTarget(
            pose=FAKE_TARGET_POSE,
            neighbor_reference_uv=(320, 48),
            target_pixel_uv=(320, 240),
            confidence=0.99,
            capture_id="fake-tuning-capture",
        )


def default_take_slots() -> Dict[int, SlotPlacementPlan]:
    """Stand-in stand poses so fake sessions can exercise every step."""

    slots: Dict[int, SlotPlacementPlan] = {}
    for slot in (1, 2, 3, 4):
        y_m = -0.05 - 0.06 * (slot - 1)
        slots[slot] = SlotPlacementPlan(
            pre_place=BasePose(-0.30, y_m, 0.18, 0.0, 0.0, 0.0),
            place=BasePose(-0.34, y_m, 0.18, 0.0, 0.0, 0.0),
            lower_place=BasePose(-0.34, y_m, 0.14, 0.0, 0.0, 0.0),
            retreat=BasePose(-0.30, y_m, 0.18, 0.0, 0.0, 0.0),
        )
    return slots


def default_pickup_slots() -> Dict[int, BookStandPickupPlan]:
    """Stand-in pickup poses so fake sessions can exercise every step."""

    slots: Dict[int, BookStandPickupPlan] = {}
    for slot in (1, 2, 3):
        y_m = -0.05 - 0.06 * (slot - 1)
        slots[slot] = BookStandPickupPlan(
            pre_grasp=BasePose(-0.30, y_m, 0.18, 0.0, 0.0, 0.0),
            grasp=BasePose(-0.34, y_m, 0.18, 0.0, 0.0, 0.0),
        )
    return slots


def plan_to_dict(
    plan: Union[TakeBookMotionPlan, ReturnBookMotionPlan, MidReturnMotionPlan]
) -> Dict[str, Any]:
    """Serialize one motion plan into the JSON shape the profile loaders read."""

    result: Dict[str, Any] = {}
    for name in plan.__dataclass_fields__:
        value = getattr(plan, name)
        if isinstance(value, JointPose):
            result[name] = [float(item) for item in value.values_rad]
        elif isinstance(value, GripperCommand):
            result[name] = {
                field_name: getattr(value, field_name)
                for field_name in value.__dataclass_fields__
            }
        elif isinstance(value, tuple):
            result[name] = [float(item) for item in value]
        else:
            result[name] = value
    return result


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge ``patch`` onto ``base``; nested objects (gripper) merge per field."""

    merged = dict(base)
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(value, Mapping) and isinstance(existing, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class TuningStep:
    """One executable tuning step; ``requires_target`` gates it on prepare.

    ``preview`` computes (without touching hardware) the motion data the step
    would send if executed now; ``None`` for steps with no arm motion.
    """

    name: str
    requires_target: bool
    action: Callable[[], Dict[str, Any]]
    preview: Optional[Callable[[], Dict[str, Any]]] = None


def _tcp_step_preview(
    pose: BasePose, duration_s: float, method: str, *, continues: bool = False
) -> Dict[str, Any]:
    """Preview payload for one TCP-space arm motion (mm/deg view)."""

    return {
        "type": "tcp",
        "method": method,
        "duration_s": float(duration_s),
        "x_mm": pose.x_m * 1000.0,
        "y_mm": pose.y_m * 1000.0,
        "z_mm": pose.z_m * 1000.0,
        "rx_deg": math.degrees(pose.rx_rad),
        "ry_deg": math.degrees(pose.ry_rad),
        "rz_deg": math.degrees(pose.rz_rad),
        "continues": continues,
    }


def _joints_step_preview(
    joints: JointPose, duration_s: float, *, continues: bool = False
) -> Dict[str, Any]:
    """Preview payload for one joint-space move_j (deg view)."""

    return {
        "type": "joints",
        "method": "move_j",
        "duration_s": float(duration_s),
        "joints_deg": [math.degrees(value) for value in joints.values_rad],
        "continues": continues,
    }


class TuningSession:
    """Step-by-step executor for one workflow's motions.

    ``plan_provider`` is consulted every time a step runs, so profile updates
    apply to the very next step. The cached vision target survives ``reset``
    so an operator can re-walk the steps against the same target after tuning.
    """

    # Mirrors TakeBookUseCase.BOOK_GRASP_OPEN_ANGLE_DEG.
    OPEN_GRIPPER_DEG = 50.0

    def __init__(
        self,
        kind: str,
        plan_provider: Callable[
            [], Union[TakeBookMotionPlan, ReturnBookMotionPlan, MidReturnMotionPlan]
        ],
        devices: TuningDevices,
        slots: Mapping[int, Any],
        busy_lock: threading.Lock,
        preempt: Optional[Callable[[], None]] = None,
    ) -> None:
        if kind not in ("take", "return", "midreturn"):
            raise ValueError("unsupported tuning kind: %s" % kind)
        self._kind = kind
        self._plan_provider = plan_provider
        self._devices = devices
        self._slots = dict(slots)
        self._busy = busy_lock
        # Requests cancellation of the in-flight workflow task before a
        # run-step waits on the busy lock ( runner.cancel_active ).
        self._preempt = preempt
        self._run_preempt_timeout_s = RUN_PREEMPT_TIMEOUT_S
        self._state_lock = threading.Lock()
        self._step_index = 0
        self._slot: Optional[int] = None
        self._template: Optional[SpineTemplate] = None
        self._target: Optional[BookTarget] = None
        self._placement: Union[SlotPlacementPlan, BookStandPickupPlan, None] = None
        # Mid-return and return: the fresh recognition taken after the
        # book-slot pickup; the insert chain anchors on it, not on the
        # prepare target.
        self._retarget: Optional[BookTarget] = None

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "step_index": self._step_index,
                "steps": [step.name for step in self._steps(self._plan_provider())],
                "has_target": self._target is not None,
                "slot": self._slot,
            }

    def prepare(self, slot: int, template_path: Path) -> Dict[str, Any]:
        """Enable, move to debug, locate the target and cache it with the slot."""

        plan = self._plan_provider()
        placement = self._slots.get(int(slot))
        if placement is None:
            raise TuningInputError("book-stand slot %d is not calibrated" % int(slot))
        template = SpineTemplate(Path(template_path))
        with self._hold_busy():
            self._ensure_debug_pose(plan)
            target = self._devices.vision.locate_book(template)
            base_pose = target.grasp_pose
        with self._state_lock:
            self._slot = int(slot)
            self._template = template
            self._target = target
            self._placement = placement
            self._step_index = 0
            self._retarget = None
        return {
            "ok": True,
            "target_base_pose": list(base_pose.values()),
            "confidence": target.confidence,
        }

    def run_next(self) -> Dict[str, Any]:
        with self._state_lock:
            index = self._step_index
        if index >= len(self._steps(self._plan_provider())):
            return {
                "step_index": index,
                "step_name": None,
                "ok": True,
                "detail": {"finished": True},
            }
        return self._run(index)

    def run_previous(self) -> Dict[str, Any]:
        """Re-execute the previous step; all motions are absolute, hence idempotent."""

        with self._state_lock:
            index = self._step_index - 1
        if index < 0:
            raise TuningInputError("already at the first step")
        return self._run(index)

    def run_step(self, index: int) -> Dict[str, Any]:
        steps = self._steps(self._plan_provider())
        if not 0 <= int(index) < len(steps):
            raise TuningInputError(
                "step index %d is out of range (0..%d)" % (int(index), len(steps) - 1)
            )
        return self._run(int(index))

    def reset(self) -> Dict[str, Any]:
        """Rewind the step cursor; the cached target is kept on purpose."""

        with self._state_lock:
            self._step_index = 0
        return {"ok": True, "step_index": 0}

    def preview_step(self, step_index: Optional[int] = None) -> Dict[str, Any]:
        """Motion data one step would send if executed now.

        Pure computation over the live plan and the cached vision target; it
        never touches hardware and never takes the busy lock, so it is safe to
        poll after every profile hot-apply. ``step_index`` previews any step,
        not just the current one; steps whose pose depends on runtime state
        (the prepare cache) report ``motion: None`` with a
        human-readable ``reason``.
        """

        steps = self._steps(self._plan_provider())
        if step_index is None:
            with self._state_lock:
                index = self._step_index
            # Session walked off the end: keep the original "finished" answer.
            if index >= len(steps):
                return {
                    "step_index": index,
                    "step_name": None,
                    "motion": None,
                    "reason": None,
                }
        else:
            index = int(step_index)
            if not 0 <= index < len(steps):
                raise TuningInputError(
                    "step index %d is out of range (0..%d)" % (index, len(steps) - 1)
                )
        return self._preview_at(steps, index)

    def preview_all_steps(self) -> Dict[str, Any]:
        """One-shot previews of every step, in execution order."""

        steps = self._steps(self._plan_provider())
        with self._state_lock:
            current = self._step_index
        return {
            "step_index": current,
            "steps": [self._preview_at(steps, index) for index in range(len(steps))],
        }

    def _preview_at(
        self, steps: Tuple[TuningStep, ...], index: int
    ) -> Dict[str, Any]:
        step = steps[index]
        motion: Optional[Dict[str, Any]] = None
        reason: Optional[str] = None
        if step.preview is not None:
            try:
                motion = step.preview()
            except TuningNotPreparedError as error:
                reason = str(error)
            except RuntimeError as error:
                # Pure-computation failures (e.g. the return pre-insert x IK
                # search finding no reachable candidate) are reported, not 500s.
                reason = str(error)
        return {
            "step_index": index,
            "step_name": step.name,
            "motion": motion,
            "reason": reason,
        }

    def enable_arm(self) -> Dict[str, Any]:
        with self._hold_busy():
            arm = self._devices.arm
            # 臂体与夹爪分开判断：夹爪掉使能或带锁存故障时，只查臂体会让
            # 使能按钮空转、永远恢复不了（故障必须用完整使能序列清除）。
            was_enabled = arm.is_enabled() and arm.is_gripper_ready()
            if not was_enabled:
                arm.enable()
        return {"ok": True, "enabled": True, "was_enabled": was_enabled}

    def disable_arm(self, duration_s: float = 3.0) -> Dict[str, Any]:
        """Safe disable sequence, same as TakeBookUseCase.disable_arm."""

        with self._hold_busy():
            detail = self._devices.arm.disable_at_safe_pose(duration_s)
        return {"ok": True, "enabled": False, "detail": detail}

    def move_debug_pose(self) -> Dict[str, Any]:
        plan = self._plan_provider()
        with self._hold_busy():
            arm = self._devices.arm
            if not arm.is_enabled():
                arm.enable()
            arm.move_j(plan.debug_joint, plan.debug_duration_s)
            joints = arm.read_joint_pose()
        return {"ok": True, "joints_rad": list(joints.values_rad)}

    def _run(self, index: int) -> Dict[str, Any]:
        steps = self._steps(self._plan_provider())
        step = steps[index]
        with self._hold_busy_preempt():
            if step.requires_target and self._target is None:
                raise TuningNotPreparedError("请先准备（识别目标）")
            detail = step.action()
        with self._state_lock:
            self._step_index = index + 1
        return {"step_index": index + 1, "step_name": step.name, "ok": True, "detail": detail}

    @contextmanager
    def _hold_busy(self) -> Iterator[None]:
        """Serialize one hardware action against workflow execution."""

        if not self._busy.acquire(blocking=False):
            raise TuningBusyError("a bookarm workflow or tuning operation is already running")
        try:
            yield
        finally:
            self._busy.release()

    @contextmanager
    def _hold_busy_preempt(self) -> Iterator[None]:
        """Run-step variant of ``_hold_busy``: 运行 means "run now".

        If the busy lock is not immediately free, request cancellation of the
        in-flight workflow task (it cancels at the next stage boundary) and
        wait up to ``self._run_preempt_timeout_s`` for the lock instead of
        failing fast. A concurrently running tuning step is a monolithic
        motion closure in another request thread — it cannot and must not be
        aborted mid-motion; the same bounded wait simply covers it (motions
        are seconds-long). Still busy after the timeout → TuningBusyError,
        exactly like the non-preempting path.
        """

        if not self._busy.acquire(blocking=False):
            if self._preempt is not None:
                self._preempt()
            if not self._busy.acquire(timeout=self._run_preempt_timeout_s):
                raise TuningBusyError(
                    "a bookarm workflow or tuning operation is already running"
                )
        try:
            yield
        finally:
            self._busy.release()

    def _ensure_debug_pose(self, plan: Any) -> None:
        """Enable when needed and land on the debug joint pose."""

        arm = self._devices.arm
        if arm.is_enabled():
            try:
                ensure_joint_target(
                    plan.debug_joint, arm.read_joint_pose(), plan.joint_tolerance_rad
                )
                return
            except RuntimeError:
                pass
        else:
            arm.enable()
            arm.move_j(plan.zero_joint, plan.zero_duration_s)
        arm.move_j(plan.debug_joint, plan.debug_duration_s)

    def _move_debug(self, plan: Any) -> Dict[str, Any]:
        self._devices.arm.move_j(plan.debug_joint, plan.debug_duration_s)
        return {"debug_joint_rad": list(plan.debug_joint.values_rad)}

    def _move_zero(self, plan: Any) -> Dict[str, Any]:
        self._devices.arm.move_j(plan.zero_joint, plan.zero_duration_s)
        return {"zero_joint_rad": list(plan.zero_joint.values_rad)}

    def _book_target(self) -> BookTarget:
        target = self._target
        if not isinstance(target, BookTarget):
            raise TuningNotPreparedError("请先准备（识别目标）")
        return target

    def _insert_book_target(self) -> BookTarget:
        target = self._retarget
        if target is None:
            raise TuningNotPreparedError("请先执行「识别目标书」步骤")
        return target

    def _stand_placement(self) -> SlotPlacementPlan:
        placement = self._placement
        if not isinstance(placement, SlotPlacementPlan):
            raise TuningNotPreparedError("请先准备（识别目标）")
        return placement

    def _stand_pickup(self) -> BookStandPickupPlan:
        placement = self._placement
        if not isinstance(placement, BookStandPickupPlan):
            raise TuningNotPreparedError("请先准备（识别目标）")
        return placement

    def _require_template(self) -> SpineTemplate:
        if self._template is None:
            raise TuningNotPreparedError("请先准备（识别目标）")
        return self._template

    def refresh_placement_from_slots(self) -> None:
        """Re-read the cached placement after a hot slot-calibration swap."""

        with self._state_lock:
            if self._slot is None:
                return
            updated = self._slots.get(self._slot)
            if updated is not None:
                self._placement = updated

    def _steps(self, plan: Any) -> Tuple[TuningStep, ...]:
        if self._kind == "take":
            return self._take_steps(plan)
        if self._kind == "midreturn":
            return self._mid_return_steps(plan)
        return self._return_steps(plan)

    def _take_steps(self, plan: TakeBookMotionPlan) -> Tuple[TuningStep, ...]:
        """Take-book steps mirroring TakeBookUseCase._grasp_book/_place_in_stand."""

        arm = self._devices.arm
        rod = self._devices.rod

        def open_gripper() -> Dict[str, Any]:
            # Same verified-script command shape as the use case; the plan's
            # speed level scales its speeds like every other gripper motion.
            command = apply_speed_to_gripper(
                GripperCommand(open_deg=self.OPEN_GRIPPER_DEG), plan.speed_level
            )
            arm.open_gripper(self.OPEN_GRIPPER_DEG, command)
            return {"open_deg": self.OPEN_GRIPPER_DEG}

        def rod_home() -> Dict[str, Any]:
            rod.move_to_deg(
                plan.rod_home_deg,
                plan.rod_speed,
                plan.rod_acceleration,
                plan.rod_tolerance_deg,
                plan.rod_timeout_s,
            )
            return {"rod_deg": plan.rod_home_deg}

        def pregrasp_pose() -> BasePose:
            return self._book_target().grasp_pose.offset_cm(
                plan.pregrasp_offset_cm, rpy_deg=plan.grasp_rpy_deg
            )

        def pregrasp() -> Dict[str, Any]:
            pose = pregrasp_pose()
            arm.move_to_pose_by_move_j(pose, plan.pregrasp_duration_s)
            return {"base_pose": list(pose.values())}

        def approach_pose() -> BasePose:
            return self._book_target().grasp_pose.offset_cm(
                plan.approach_offset_cm, rpy_deg=plan.grasp_rpy_deg
            )

        def approach() -> Dict[str, Any]:
            pose = approach_pose()
            arm.move_l(pose, plan.approach_duration_s)
            return {"base_pose": list(pose.values())}

        def rod_grasp() -> Dict[str, Any]:
            rod.move_to_deg(
                plan.rod_grasp_deg,
                plan.rod_speed,
                plan.rod_acceleration,
                plan.rod_tolerance_deg,
                plan.rod_timeout_s,
            )
            return {"rod_deg": plan.rod_grasp_deg}

        def fine_grasp_pose() -> BasePose:
            return self._book_target().grasp_pose.offset_cm(
                plan.fine_grasp_offset_cm, rpy_deg=plan.fine_grasp_rpy_deg
            )

        def fine_grasp() -> Dict[str, Any]:
            pose = fine_grasp_pose()
            arm.move_to_pose_by_move_j(pose, plan.fine_grasp_duration_s)
            return {"base_pose": list(pose.values())}

        def close_gripper() -> Dict[str, Any]:
            return {"gripper_close": arm.close_gripper_monitored(plan.gripper)}

        def post_grasp_pose() -> BasePose:
            return fine_grasp_pose().offset_cm(
                plan.post_grasp_offset_cm, rpy_offset_deg=plan.post_grasp_rpy_offset_deg
            )

        def post_grasp() -> Dict[str, Any]:
            pose = post_grasp_pose()
            arm.move_to_pose_by_move_j(pose, plan.post_grasp_duration_s)
            return {"base_pose": list(pose.values())}

        def stand_pre_place() -> Dict[str, Any]:
            placement = self._stand_placement()
            arm.move_to_pose_by_move_j(placement.pre_place, placement.pre_place_duration_s)
            return {"base_pose": list(placement.pre_place.values())}

        def stand_place() -> Dict[str, Any]:
            placement = self._stand_placement()
            arm.move_l(placement.place, placement.place_duration_s)
            return {"base_pose": list(placement.place.values())}

        def stand_lower_release() -> Dict[str, Any]:
            placement = self._stand_placement()
            arm.move_l(placement.lower_place, placement.lower_place_duration_s)
            command = apply_speed_to_gripper(
                GripperCommand(open_deg=self.OPEN_GRIPPER_DEG), plan.speed_level
            )
            arm.open_gripper(self.OPEN_GRIPPER_DEG, command)
            return {
                "base_pose": list(placement.lower_place.values()),
                "open_deg": self.OPEN_GRIPPER_DEG,
            }

        def stand_retreat_debug() -> Dict[str, Any]:
            placement = self._stand_placement()
            # book_grasp ends at debug_joint, so leave the slot through the
            # collision-checked pre-place pose, exactly like the use case.
            arm.move_l(placement.pre_place, placement.retreat_duration_s)
            return self._move_debug(plan)

        return (
            TuningStep(
                "调试位",
                False,
                lambda: self._move_debug(plan),
                preview=lambda: _joints_step_preview(plan.debug_joint, plan.debug_duration_s),
            ),
            TuningStep("打开夹爪(50°)", False, open_gripper),
            TuningStep("推杆归零", False, rod_home),
            TuningStep(
                "预抓取位",
                True,
                pregrasp,
                preview=lambda: _tcp_step_preview(pregrasp_pose(), plan.pregrasp_duration_s, "move_p_by_j"),
            ),
            TuningStep(
                "逼近目标(move_l)",
                True,
                approach,
                preview=lambda: _tcp_step_preview(approach_pose(), plan.approach_duration_s, "move_l"),
            ),
            TuningStep("推杆拨书(rod_grasp_deg)", False, rod_grasp),
            TuningStep(
                "精抓",
                True,
                fine_grasp,
                preview=lambda: _tcp_step_preview(fine_grasp_pose(), plan.fine_grasp_duration_s, "move_p_by_j"),
            ),
            TuningStep("闭合夹爪", False, close_gripper),
            TuningStep(
                "抓取后撤",
                True,
                post_grasp,
                preview=lambda: _tcp_step_preview(post_grasp_pose(), plan.post_grasp_duration_s, "move_p_by_j"),
            ),
            TuningStep(
                "回调试位",
                False,
                lambda: self._move_debug(plan),
                preview=lambda: _joints_step_preview(plan.debug_joint, plan.debug_duration_s),
            ),
            TuningStep(
                "书立预放置(pre_place)",
                True,
                stand_pre_place,
                preview=lambda: _tcp_step_preview(
                    self._stand_placement().pre_place,
                    self._stand_placement().pre_place_duration_s,
                    "move_p_by_j",
                ),
            ),
            TuningStep(
                "书立放置(place)",
                True,
                stand_place,
                preview=lambda: _tcp_step_preview(
                    self._stand_placement().place,
                    self._stand_placement().place_duration_s,
                    "move_l",
                ),
            ),
            TuningStep(
                "下放松爪(lower_place+开50°)",
                True,
                stand_lower_release,
                preview=lambda: _tcp_step_preview(
                    self._stand_placement().lower_place,
                    self._stand_placement().lower_place_duration_s,
                    "move_l",
                    continues=True,
                ),
            ),
            # 拨杆自拨书后一直外伸；放书开爪后先收回拨杆再撤臂，避免拖带书本/书立。
            TuningStep("松开拨杆", False, rod_home),
            TuningStep(
                "撤回调试位",
                True,
                stand_retreat_debug,
                preview=lambda: _tcp_step_preview(
                    self._stand_placement().pre_place,
                    self._stand_placement().retreat_duration_s,
                    "move_l",
                    continues=True,
                ),
            ),
            TuningStep(
                "待机位(zero)",
                False,
                lambda: self._move_zero(plan),
                preview=lambda: _joints_step_preview(plan.zero_joint, plan.zero_duration_s),
            ),
        )

    def _pickup_open_step(self, plan: Any, name: str) -> TuningStep:
        """张开夹爪 before driving onto the book-stand slot (shared)."""

        arm = self._devices.arm

        def open_gripper() -> Dict[str, Any]:
            command = replace(plan.gripper, open_deg=plan.pickup_open_deg)
            arm.open_gripper(plan.pickup_open_deg, command)
            return {"open_deg": plan.pickup_open_deg}

        return TuningStep(name, False, open_gripper)

    def _stand_pickup_drive_steps(
        self, plan: Any, names: Tuple[str, str, str]
    ) -> Tuple[TuningStep, ...]:
        """Book-stand pickup drive chain shared by the mid-return and return
        sessions: drive onto the calibrated slot, close, retreat to debug.
        ``names`` are the per-kind step labels (to slot, close, retreat)."""

        arm = self._devices.arm
        slot_name, close_name, retreat_name = names

        def to_book_slot() -> Dict[str, Any]:
            pickup = self._stand_pickup()
            arm.move_to_pose_by_move_j(pickup.pre_grasp, pickup.pre_grasp_duration_s)
            arm.move_l(pickup.grasp, pickup.grasp_duration_s)
            return {
                "pre_grasp_pose": list(pickup.pre_grasp.values()),
                "grasp_pose": list(pickup.grasp.values()),
            }

        def close_gripper() -> Dict[str, Any]:
            return {"gripper_close": arm.close_gripper_monitored(plan.gripper)}

        def slot_retreat_debug() -> Dict[str, Any]:
            pickup = self._stand_pickup()
            # Leave the slot through the collision-checked pre-grasp pose,
            # exactly like the use cases.
            arm.move_l(pickup.pre_grasp, pickup.retreat_duration_s)
            return self._move_debug(plan)

        return (
            TuningStep(
                slot_name,
                True,
                to_book_slot,
                preview=lambda: _tcp_step_preview(
                    self._stand_pickup().pre_grasp,
                    self._stand_pickup().pre_grasp_duration_s,
                    "move_p_by_j",
                    continues=True,
                ),
            ),
            TuningStep(close_name, False, close_gripper),
            TuningStep(
                retreat_name,
                False,
                slot_retreat_debug,
                preview=lambda: _tcp_step_preview(
                    self._stand_pickup().pre_grasp,
                    self._stand_pickup().retreat_duration_s,
                    "move_l",
                    continues=True,
                ),
            ),
        )

    def _retarget_book_step(self, name: str) -> TuningStep:
        """识别目标书 (mid-return and return): re-locate the target book with
        the arm held at the debug pose; the insert chain anchors on this fresh
        recognition."""

        def retarget_book() -> Dict[str, Any]:
            target = self._devices.vision.locate_book(self._require_template())
            with self._state_lock:
                self._retarget = target
            return {
                "target_base_pose": list(target.grasp_pose.values()),
                "confidence": target.confidence,
                "capture_id": target.capture_id,
            }

        return TuningStep(name, True, retarget_book)

    def _insert_chain_steps(
        self,
        plan: Any,
        names: Tuple[str, str, str, str, str],
        target_resolver: Callable[[], BookTarget],
    ) -> Tuple[TuningStep, ...]:
        """预插入位 → 回抽 insert chain shared by the mid-return and return
        sessions. The return plan exposes the same attribute names as the
        mid-return plan, so the closures are plan-agnostic; ``names`` are the
        per-kind step labels and ``target_resolver`` supplies the insert
        anchor (both kinds: the synchronous 识别目标书 cache)."""

        arm = self._devices.arm
        (
            pre_insert_name,
            insert_name,
            push_name,
            release_name,
            retract_name,
        ) = names

        def insert_anchor_pose() -> BasePose:
            """Pre-offset insert anchor: y = fresh-target grasp y +
            plan.target_y_offset_cm; x/z from the post-pickup recognition,
            with z clamped up to plan.min_insert_z_m, matching the workflow
            side."""

            grasp = target_resolver().grasp_pose
            y1_m = grasp.y_m + float(plan.target_y_offset_cm) / 100.0
            # Low insert targets run the wrist into joint limits at
            # insert_rpy_deg; clamp z up to the reachable minimum rather
            # than failing IK, exactly like the use cases.
            z_m = max(grasp.z_m, float(plan.min_insert_z_m))
            return BasePose(grasp.x_m, y1_m, z_m, 0.0, 0.0, 0.0)

        def chain_poses() -> Dict[str, BasePose]:
            # Shared with the workflow use cases. Pure computation, safe for
            # previews: a plan without a pinned pre_insert_x_m (return)
            # IK-searches the outermost reachable x. The probes route off the
            # arm command worker so an HTTP preview/step never queues behind
            # a running motion; no motion is commanded either way.
            return insert_chain_poses(plan, insert_anchor_pose(), OffworkerArmProbe(arm))

        def insert_pose() -> BasePose:
            return chain_poses()["insert"]

        def pre_insert_pose() -> BasePose:
            return chain_poses()["pre_insert"]

        def pre_insert() -> Dict[str, Any]:
            pose = pre_insert_pose()
            arm.move_to_pose_by_move_j(pose, plan.pre_insert_duration_s)
            return {"base_pose": list(pose.values())}

        def move_insert() -> Dict[str, Any]:
            pose = insert_pose()
            # From the pre-insert pose the final approach is a short linear
            # move, exactly like the take flow's pregrasp→approach pair.
            arm.move_l(pose, plan.insert_duration_s)
            return {"base_pose": list(pose.values())}

        def push_pose() -> BasePose:
            return chain_poses()["push"]

        def push_into_shelf() -> Dict[str, Any]:
            pose = push_pose()
            arm.move_l(pose, plan.push_duration_s)
            return {"base_pose": list(pose.values())}

        def release_gripper() -> Dict[str, Any]:
            command = replace(plan.gripper, open_deg=plan.release_open_deg)
            arm.open_gripper(plan.release_open_deg, command)
            return {"open_deg": plan.release_open_deg}

        def retract_pose() -> BasePose:
            return chain_poses()["retract"]

        def retract() -> Dict[str, Any]:
            pose = retract_pose()
            arm.move_l(pose, plan.retract_duration_s)
            return {"base_pose": list(pose.values())}

        return (
            # 与取书的预抓取位同理：先 MovePByJ 到插入位外侧的预插入位，
            # 避免带着书横扫刮碰书立上的其他书；随后短直线 MoveL 到插入位。
            TuningStep(
                pre_insert_name,
                True,
                pre_insert,
                preview=lambda: _tcp_step_preview(
                    pre_insert_pose(), plan.pre_insert_duration_s, "move_p_by_j"
                ),
            ),
            TuningStep(
                insert_name,
                True,
                move_insert,
                preview=lambda: _tcp_step_preview(
                    insert_pose(), plan.insert_duration_s, "move_l"
                ),
            ),
            TuningStep(
                push_name,
                True,
                push_into_shelf,
                preview=lambda: _tcp_step_preview(push_pose(), plan.push_duration_s, "move_l"),
            ),
            TuningStep(release_name, False, release_gripper),
            TuningStep(
                retract_name,
                True,
                retract,
                preview=lambda: _tcp_step_preview(
                    retract_pose(), plan.retract_duration_s, "move_l"
                ),
            ),
        )

    def _mid_return_steps(self, plan: MidReturnMotionPlan) -> Tuple[TuningStep, ...]:
        """Mid-return steps: grasp chain, put the pulled book back loosely,
        pick the return book from the stand, re-locate the target book, then
        insert at y = fresh-target y + plan.target_y_offset_cm.

        The grasp chain mirrors TakeBookUseCase._grasp_book; the put-back and
        clear-out moves are single-axis offsets chained off the fine-grasp
        pose; the book slot comes from the book-stand pickup slot selected at
        prepare time; the insert pose chains off the post-pickup recognition
        (x/z) and the configured y offset, because the put-back chain may
        have shifted the neighbour book. Every step stays absolute and
        idempotent, exactly like the take/return sessions.
        """

        arm = self._devices.arm
        rod = self._devices.rod

        def open_gripper() -> Dict[str, Any]:
            command = replace(plan.gripper, open_deg=plan.pickup_open_deg)
            arm.open_gripper(plan.pickup_open_deg, command)
            return {"open_deg": plan.pickup_open_deg}

        def rod_home() -> Dict[str, Any]:
            rod.move_to_deg(
                plan.rod_home_deg,
                plan.rod_speed,
                plan.rod_acceleration,
                plan.rod_tolerance_deg,
                plan.rod_timeout_s,
            )
            return {"rod_deg": plan.rod_home_deg}

        def pregrasp_pose() -> BasePose:
            return self._book_target().grasp_pose.offset_cm(
                plan.pregrasp_offset_cm, rpy_deg=plan.grasp_rpy_deg
            )

        def pregrasp() -> Dict[str, Any]:
            pose = pregrasp_pose()
            arm.move_to_pose_by_move_j(pose, plan.pregrasp_duration_s)
            return {"base_pose": list(pose.values())}

        def approach_pose() -> BasePose:
            return self._book_target().grasp_pose.offset_cm(
                plan.approach_offset_cm, rpy_deg=plan.grasp_rpy_deg
            )

        def approach() -> Dict[str, Any]:
            pose = approach_pose()
            arm.move_l(pose, plan.approach_duration_s)
            return {"base_pose": list(pose.values())}

        def rod_grasp() -> Dict[str, Any]:
            rod.move_to_deg(
                plan.rod_grasp_deg,
                plan.rod_speed,
                plan.rod_acceleration,
                plan.rod_tolerance_deg,
                plan.rod_timeout_s,
            )
            return {"rod_deg": plan.rod_grasp_deg}

        def fine_grasp_pose() -> BasePose:
            return self._book_target().grasp_pose.offset_cm(
                plan.fine_grasp_offset_cm, rpy_deg=plan.fine_grasp_rpy_deg
            )

        def fine_grasp() -> Dict[str, Any]:
            pose = fine_grasp_pose()
            arm.move_to_pose_by_move_j(pose, plan.fine_grasp_duration_s)
            return {"base_pose": list(pose.values())}

        def close_gripper() -> Dict[str, Any]:
            return {"gripper_close": arm.close_gripper_monitored(plan.gripper)}

        def push_in_pose() -> BasePose:
            return fine_grasp_pose().offset_cm((plan.push_in_x_offset_cm, 0.0, 0.0))

        def push_in() -> Dict[str, Any]:
            pose = push_in_pose()
            arm.move_l(pose, plan.push_in_duration_s)
            return {"base_pose": list(pose.values())}

        def partial_release_gripper() -> Dict[str, Any]:
            command = replace(plan.gripper, open_deg=plan.partial_release_deg)
            arm.open_gripper(plan.partial_release_deg, command)
            return {"open_deg": plan.partial_release_deg}

        def re_push_pose() -> BasePose:
            return push_in_pose().offset_cm((plan.re_push_x_offset_cm, 0.0, 0.0))

        def re_push() -> Dict[str, Any]:
            pose = re_push_pose()
            arm.move_l(pose, plan.re_push_duration_s)
            return {"base_pose": list(pose.values())}

        def lower_pose() -> BasePose:
            return re_push_pose().offset_cm((0.0, 0.0, plan.lower_z_offset_cm))

        def lower() -> Dict[str, Any]:
            pose = lower_pose()
            arm.move_l(pose, plan.lower_duration_s)
            return {"base_pose": list(pose.values())}

        def full_open_gripper() -> Dict[str, Any]:
            command = replace(plan.gripper, open_deg=plan.full_open_deg)
            arm.open_gripper(plan.full_open_deg, command)
            return {"open_deg": plan.full_open_deg}

        def rod_putback() -> Dict[str, Any]:
            rod.move_to_deg(
                plan.rod_putback_deg,
                plan.rod_speed,
                plan.rod_acceleration,
                plan.rod_tolerance_deg,
                plan.rod_timeout_s,
            )
            return {"rod_deg": plan.rod_putback_deg}

        def shift_pose() -> BasePose:
            return lower_pose().offset_cm((0.0, plan.shift_y_offset_cm, 0.0))

        def shift() -> Dict[str, Any]:
            pose = shift_pose()
            arm.move_l(pose, plan.shift_duration_s)
            return {"base_pose": list(pose.values())}

        def carry_forward_pose() -> BasePose:
            return shift_pose().offset_cm((plan.carry_forward_x_offset_cm, 0.0, 0.0))

        def carry_forward() -> Dict[str, Any]:
            pose = carry_forward_pose()
            arm.move_l(pose, plan.carry_forward_duration_s)
            return {"base_pose": list(pose.values())}

        def forward_pose() -> BasePose:
            return carry_forward_pose().offset_cm((plan.forward_x_offset_cm, 0.0, 0.0))

        def forward() -> Dict[str, Any]:
            pose = forward_pose()
            arm.move_l(pose, plan.forward_duration_s)
            return {"base_pose": list(pose.values())}

        return (
            TuningStep("张开夹爪(pickup_open_deg)", False, open_gripper),
            TuningStep("推杆归零", False, rod_home),
            TuningStep(
                "预抓取位",
                True,
                pregrasp,
                preview=lambda: _tcp_step_preview(pregrasp_pose(), plan.pregrasp_duration_s, "move_p_by_j"),
            ),
            TuningStep(
                "逼近目标(move_l)",
                True,
                approach,
                preview=lambda: _tcp_step_preview(approach_pose(), plan.approach_duration_s, "move_l"),
            ),
            TuningStep("推杆拨书(rod_grasp_deg)", False, rod_grasp),
            TuningStep(
                "精抓",
                True,
                fine_grasp,
                preview=lambda: _tcp_step_preview(fine_grasp_pose(), plan.fine_grasp_duration_s, "move_p_by_j"),
            ),
            TuningStep("闭合夹爪", False, close_gripper),
            TuningStep(
                "推回(push_in_x)",
                True,
                push_in,
                preview=lambda: _tcp_step_preview(push_in_pose(), plan.push_in_duration_s, "move_l"),
            ),
            TuningStep("松开一点夹爪(partial_release_deg)", False, partial_release_gripper),
            # 松爪后再推一次，保证夹爪手指深入书缝，后续全开才能撑在缝里。
            TuningStep(
                "再次推回(re_push_x)",
                True,
                re_push,
                preview=lambda: _tcp_step_preview(re_push_pose(), plan.re_push_duration_s, "move_l"),
            ),
            TuningStep("推杆归位", False, rod_putback),
            TuningStep(
                "机械臂下降(lower_z)",
                True,
                lower,
                preview=lambda: _tcp_step_preview(lower_pose(), plan.lower_duration_s, "move_l"),
            ),
            # 下降放稳后先完全张开撑开书缝，再闭合夹紧书本带着横移；
            # 横移到位后轻微松开卸力，最后前移送书到位。
            TuningStep("完全张开夹爪(full_open_deg)", False, full_open_gripper),
            TuningStep("闭合夹爪", False, close_gripper),
            TuningStep(
                "机械臂横移(shift_y)",
                True,
                shift,
                preview=lambda: _tcp_step_preview(shift_pose(), plan.shift_duration_s, "move_l"),
            ),
            # 夹爪保持闭合，带着书一起前移到便于摄像头识别的位置。
            TuningStep(
                "书本前移(carry_forward_x)",
                True,
                carry_forward,
                preview=lambda: _tcp_step_preview(carry_forward_pose(), plan.carry_forward_duration_s, "move_l"),
            ),
            TuningStep("松开一点夹爪(partial_release_deg)", False, partial_release_gripper),
            TuningStep(
                "机械臂前移(forward_x)",
                True,
                forward,
                preview=lambda: _tcp_step_preview(forward_pose(), plan.forward_duration_s, "move_l"),
            ),
            TuningStep(
                "回调试位",
                False,
                lambda: self._move_debug(plan),
                preview=lambda: _joints_step_preview(plan.debug_joint, plan.debug_duration_s),
            ),
            self._pickup_open_step(plan, "张开夹爪(pickup_open_deg)"),
            *self._stand_pickup_drive_steps(
                plan,
                ("运动到书槽(pre_grasp→grasp)", "闭合夹爪", "回调试位"),
            ),
            # 放回旧书、取回待还书后重新识别目标书：放回过程可能让旧书
            # 移位，后续预插入位/插入位均基于这次识别的目标点计算。
            self._retarget_book_step("识别目标书"),
            *self._insert_chain_steps(
                plan,
                (
                    "预插入位",
                    "运动到插入位",
                    "推回书架(push_x)",
                    "张开夹爪(release_open_deg)",
                    "回抽(retract_x)",
                ),
                self._insert_book_target,
            ),
            TuningStep(
                "回调试位",
                False,
                lambda: self._move_debug(plan),
                preview=lambda: _joints_step_preview(plan.debug_joint, plan.debug_duration_s),
            ),
        )

    def _return_steps(self, plan: ReturnBookMotionPlan) -> Tuple[TuningStep, ...]:
        """Return-book steps mirroring ReturnBookUseCase stage by stage:
        debug pose, open the gripper, synchronously re-locate the reference
        book (the arm sits idle at debug, so recognition never competes with
        motion for CPU), stand-slot pickup (shared with the mid-return
        session), then the shared insert chain."""

        return (
            TuningStep(
                "调试位",
                False,
                lambda: self._move_debug(plan),
                preview=lambda: _joints_step_preview(plan.debug_joint, plan.debug_duration_s),
            ),
            self._pickup_open_step(plan, "张开夹爪"),
            self._retarget_book_step("识别目标书"),
            *self._stand_pickup_drive_steps(
                plan,
                ("运动到书槽", "闭合夹爪", "回调试位"),
            ),
            *self._insert_chain_steps(
                plan,
                ("预插入位", "运动到插入位", "推回书架", "张开夹爪", "回抽"),
                self._insert_book_target,
            ),
            TuningStep(
                "回调试位",
                False,
                lambda: self._move_debug(plan),
                preview=lambda: _joints_step_preview(plan.debug_joint, plan.debug_duration_s),
            ),
        )


class TuningKind:
    """Live plan handle, persisted profile and step session for one workflow.

    The profile document (what ``profile``/``update_profile`` read, merge and
    persist) always holds the tuned base values plus ``speed_level``; the
    profile loaders return the level-adjusted effective plan, which is what
    gets hot-swapped into the use case and the step session. Keeping the
    base document as the canonical state means a merged patch can never
    apply the level's scaling twice.
    """

    def __init__(
        self,
        kind: str,
        *,
        plan_getter: Callable[[], Any],
        plan_setter: Callable[[Any], None],
        devices: TuningDevices,
        slots: Mapping[int, Any],
        busy_lock: threading.Lock,
        profile_path: Path,
        preempt: Optional[Callable[[], None]] = None,
    ) -> None:
        self.kind = kind
        self._plan_getter = plan_getter
        self._plan_setter = plan_setter
        self._profile_path = Path(profile_path)
        self._loader = {
            "take": load_take_book_motion_plan,
            "return": load_return_book_motion_plan,
            "midreturn": load_mid_return_motion_plan,
        }[kind]
        self._update_lock = threading.Lock()
        self._base_plan = remove_speed_scaling(plan_getter())
        self.session = TuningSession(kind, plan_getter, devices, slots, busy_lock, preempt)

    def profile(self) -> Dict[str, Any]:
        return plan_to_dict(self._base_plan)

    def apply_profile(self, patch: Mapping[str, Any]) -> Dict[str, Any]:
        """Merge ``patch`` into the live plan, validate and hot-swap it.

        Same merge/validate pipeline as :meth:`update_profile`, but the
        validated plan is only swapped into the running use case; the
        persisted profile file is left untouched (see :meth:`save_profile`).
        """

        return self._update(patch, persist=False)

    def update_profile(self, patch: Mapping[str, Any]) -> Dict[str, Any]:
        """Merge ``patch`` into the live plan, validate, persist and hot-swap it.

        The merged document is written to a temporary file and loaded with the
        stock profile loader so unknown fields and malformed values are
        rejected exactly like a hand-written profile. Only a validated plan is
        persisted (atomically) and swapped into the live use case.
        """

        return self._update(patch, persist=True)

    def save_profile(self) -> Dict[str, Any]:
        """Persist the current live plan to the profile file (atomically).

        The live plan already passed the stock loader, so this is a straight
        dump; used after one or more :meth:`apply_profile` hot-swaps.
        """

        with self._update_lock:
            document = self.profile()
            temp_path = self._write_temp(document)
            os.replace(temp_path, self._profile_path)
            return document

    def _update(self, patch: Mapping[str, Any], *, persist: bool) -> Dict[str, Any]:
        if not isinstance(patch, Mapping):
            raise TuningProfileError("profile update must be a JSON object")
        with self._update_lock:
            merged = _deep_merge(self.profile(), patch)
            temp_path = self._write_temp(merged)
            try:
                plan = self._loader(temp_path)
            except (ValueError, FileNotFoundError) as error:
                temp_path.unlink(missing_ok=True)
                raise TuningProfileError(str(error)) from error
            if persist:
                os.replace(temp_path, self._profile_path)
            else:
                temp_path.unlink(missing_ok=True)
            # The loader already applied speed_level; keep the un-scaled base
            # document as canonical so the next merge never double-applies it.
            self._base_plan = remove_speed_scaling(plan)
            self._plan_setter(plan)
            return plan_to_dict(self._base_plan)

    def _write_temp(self, merged: Mapping[str, Any]) -> Path:
        self._profile_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=self._profile_path.name + ".",
            suffix=".tmp",
            dir=str(self._profile_path.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        return Path(temp_name)


class BookStandSlotsError(ValueError):
    """Raised when a book-stand slots update fails stock-loader validation."""


_BOOK_STAND_CALIBRATION_DIR = Path(__file__).resolve().parents[3] / "config" / "calibration"
DEFAULT_BOOK_STAND_SLOTS_PATH = _BOOK_STAND_CALIBRATION_DIR / "book_stand_slots.json"
DEFAULT_BOOK_STAND_PICKUP_SLOTS_PATH = _BOOK_STAND_CALIBRATION_DIR / "book_stand_pickup_slots.json"

# Slot-pose execution: action -> (pose attribute, motion method, duration
# attribute), mirroring how the real take/return flows dispatch each pose
# (take_book.py: pre_place via MovePByJ, place/lower_place/retreat via MoveL;
# return_book.py: pre_grasp via MovePByJ, grasp/retreat via MoveL).
SLOT_EXECUTE_ACTIONS: Mapping[str, Mapping[str, Tuple[str, str, str]]] = {
    "place": {
        "pre_place": ("pre_place", "move_p_by_j", "pre_place_duration_s"),
        "place": ("place", "move_l", "place_duration_s"),
        "lower_place": ("lower_place", "move_l", "lower_place_duration_s"),
        "retreat": ("retreat", "move_l", "retreat_duration_s"),
    },
    "preplace": {
        "pre_grasp": ("pre_grasp", "move_p_by_j", "pre_grasp_duration_s"),
        "grasp": ("grasp", "move_l", "grasp_duration_s"),
    },
}

SLOT_EXECUTE_DURATION_MIN_S = 0.5
SLOT_EXECUTE_DURATION_MAX_S = 10.0


class BookStandSlots:
    """Hot-swappable book-stand slot calibration with atomic persistence.

    Live semantics: the workflow use cases and the tuning sessions share their
    slot dicts by reference, and updates mutate those dicts in place, so the
    very next motion (a workflow stage or a tuning step) uses the new poses
    without a restart. The JSON files are only consulted again at process
    start; ``save``/``persist=True`` is what makes an edit survive one. A slot
    already cached by ``prepare`` is refreshed in place too.
    """

    GROUPS = ("place", "preplace")

    def __init__(
        self,
        *,
        paths: Mapping[str, Path],
        live: Mapping[str, List[Dict[int, Any]]],
        sessions: List["TuningSession"],
        busy_lock: threading.Lock,
        arm: Optional[Any] = None,
    ) -> None:
        self._paths = {group: Path(path) for group, path in paths.items()}
        self._live = {group: list(dicts) for group, dicts in live.items()}
        self._sessions = list(sessions)
        self._busy = busy_lock
        self._arm = arm
        self._lock = threading.Lock()

    def _require_group(self, group: str) -> str:
        if group not in self.GROUPS:
            raise KeyError("unknown book-stand slots group: %s" % group)
        return group

    @staticmethod
    def _loader(group: str):
        return load_book_stand_slots if group == "place" else load_book_stand_pickup_slots

    @staticmethod
    def _serialize_slot(group: str, plan: Any) -> Dict[str, Any]:
        if group == "place":
            return {
                "pre_place": list(plan.pre_place.values()),
                "place": list(plan.place.values()),
                "lower_place": list(plan.lower_place.values()),
                "retreat": list(plan.retreat.values()),
                "pre_place_duration_s": plan.pre_place_duration_s,
                "place_duration_s": plan.place_duration_s,
                "lower_place_duration_s": plan.lower_place_duration_s,
                "retreat_duration_s": plan.retreat_duration_s,
            }
        return {
            "pre_grasp": list(plan.pre_grasp.values()),
            "grasp": list(plan.grasp.values()),
            "pre_grasp_duration_s": plan.pre_grasp_duration_s,
            "grasp_duration_s": plan.grasp_duration_s,
            "retreat_duration_s": plan.retreat_duration_s,
        }

    def _serialize(self, group: str) -> Dict[str, Any]:
        # All live dicts are mutated together; the first one is canonical.
        canonical = self._live[group][0]
        return {
            "slots": {
                str(slot): self._serialize_slot(group, plan)
                for slot, plan in sorted(canonical.items())
            }
        }

    def view(self) -> Dict[str, Any]:
        with self._lock:
            return {group: self._serialize(group) for group in self.GROUPS}

    def group_view(self, group: str) -> Dict[str, Any]:
        self._require_group(group)
        with self._lock:
            return self._serialize(group)

    def update(self, group: str, document: Mapping[str, Any], *, persist: bool) -> Dict[str, Any]:
        """Validate ``document`` with the stock loader and hot-swap the group.

        With ``persist`` the validated document also replaces the JSON file
        atomically; otherwise the file is left for a later :meth:`save`.
        """

        self._require_group(group)
        if not isinstance(document, Mapping):
            raise BookStandSlotsError("book-stand slots update must be a JSON object")
        if not self._busy.acquire(blocking=False):
            raise TuningBusyError("a bookarm workflow or tuning operation is already running")
        try:
            with self._lock:
                temp_path = self._write_temp(group, document)
                try:
                    plans = self._loader(group)(temp_path)
                except (ValueError, FileNotFoundError) as error:
                    temp_path.unlink(missing_ok=True)
                    raise BookStandSlotsError(str(error)) from error
                if persist:
                    os.replace(temp_path, self._paths[group])
                else:
                    temp_path.unlink(missing_ok=True)
                for target in self._live[group]:
                    target.clear()
                    target.update(plans)
                for session in self._sessions:
                    session.refresh_placement_from_slots()
                return self._serialize(group)
        finally:
            self._busy.release()

    def save(self, group: str) -> Dict[str, Any]:
        """Persist the current live group after hot-applied (persist=False) edits."""

        self._require_group(group)
        if not self._busy.acquire(blocking=False):
            raise TuningBusyError("a bookarm workflow or tuning operation is already running")
        try:
            with self._lock:
                temp_path = self._write_temp(group, self._serialize(group))
                os.replace(temp_path, self._paths[group])
                return self._serialize(group)
        finally:
            self._busy.release()

    def _write_temp(self, group: str, document: Mapping[str, Any]) -> Path:
        path = self._paths[group]
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(document)
        payload.setdefault(
            "description",
            "BookArm book-stand slot calibration written by the tuning API; "
            "units: robot-base metres and radians.",
        )
        fd, temp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        return Path(temp_name)

    def execute(
        self,
        group: str,
        slot_id: str,
        action: str,
        *,
        duration_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Move the arm to one calibrated slot pose from the live dicts.

        The pose comes from the hot-swapped live group (not the file), and the
        motion method mirrors the real flow for that action. Both MoveL and
        MovePByJ validate IK before any motion, so a failure here leaves the
        arm untouched.
        """

        self._require_group(group)
        if self._arm is None:
            raise ArmUnavailableError("arm control is not wired into this service instance")
        try:
            slot_number = int(str(slot_id))
        except (TypeError, ValueError) as error:
            raise KeyError("unknown book-stand slot: %s" % slot_id) from error
        spec = SLOT_EXECUTE_ACTIONS[group].get(action)
        if spec is None:
            raise BookStandSlotsError(
                "unsupported book-stand action: %s (allowed: %s)"
                % (action, ", ".join(sorted(SLOT_EXECUTE_ACTIONS[group])))
            )
        pose_attr, method, duration_attr = spec
        with self._lock:
            slot = self._live[group][0].get(slot_number)
        if slot is None:
            raise KeyError("book-stand slot %d is not calibrated" % slot_number)
        pose: BasePose = getattr(slot, pose_attr)
        duration = (
            float(getattr(slot, duration_attr))
            if duration_s is None
            else float(duration_s)
        )
        if not math.isfinite(duration):
            raise BookStandSlotsError("duration_s must be a finite number")
        duration = min(
            max(duration, SLOT_EXECUTE_DURATION_MIN_S), SLOT_EXECUTE_DURATION_MAX_S
        )
        if not self._arm.is_enabled():
            raise ArmNotEnabledError("机械臂未使能：请先使能再执行槽位动作")
        if not self._busy.acquire(blocking=False):
            raise TuningBusyError("a bookarm workflow or tuning operation is already running")
        try:
            if method == "move_l":
                self._arm.move_l(pose, duration)
            else:
                self._arm.move_to_pose_by_move_j(pose, duration)
        except ValueError as error:
            # Parameter-range failures (joint limits, bad durations) are
            # operator-fixable calibration input, not service faults.
            raise BookStandSlotsError(
                "slot %d %s 执行失败：%s；请检查该槽位位姿参数范围。"
                % (slot_number, action, error)
            ) from error
        except RuntimeError as error:
            if is_kinematics_error(error):
                raise BookStandSlotsError(
                    "slot %d %s 执行失败：%s；目标位姿不可达，请检查槽位位姿。"
                    % (slot_number, action, error)
                ) from error
            # Hardware/driver RuntimeError keeps the route's 503 mapping.
            raise
        finally:
            self._busy.release()
        return {
            "ok": True,
            "group": group,
            "slot_id": str(slot_number),
            "action": action,
            "method": method,
            "duration_s": duration,
            "pose": list(pose.values()),
        }


@dataclass(frozen=True)
class TuningService:
    """All tuning kinds plus the studio used to resolve spine templates."""

    take: TuningKind
    return_book: TuningKind
    mid_return: TuningKind
    studio: Optional[StudioBackend]
    book_stand: Optional[BookStandSlots] = None

    def kind(self, name: str) -> TuningKind:
        if name == "take":
            return self.take
        if name == "return":
            return self.return_book
        if name == "midreturn":
            return self.mid_return
        raise KeyError("unknown tuning kind: %s" % name)


def create_fake_tuning(
    runner: Any,
    studio: Optional[StudioBackend],
    *,
    store_dir: Path,
) -> TuningService:
    """Wire tuning onto the fake runner's in-memory plans and fake devices."""

    devices = TuningDevices(arm=FakeTuningArm(), rod=FakeTuningRod(), vision=FakeTuningVision())
    busy_lock = runner.busy_lock
    store_dir = Path(store_dir)
    take = TuningKind(
        "take",
        plan_getter=lambda: runner.take_plan,
        plan_setter=lambda plan: setattr(runner, "take_plan", plan),
        devices=devices,
        slots=default_take_slots(),
        busy_lock=busy_lock,
        profile_path=store_dir / "take_profile.json",
        preempt=runner.cancel_active,
    )
    return_book = TuningKind(
        "return",
        plan_getter=lambda: runner.return_plan,
        plan_setter=lambda plan: setattr(runner, "return_plan", plan),
        devices=devices,
        slots=default_pickup_slots(),
        busy_lock=busy_lock,
        profile_path=store_dir / "return_profile.json",
        preempt=runner.cancel_active,
    )
    mid_return = TuningKind(
        "midreturn",
        plan_getter=lambda: runner.mid_return_plan,
        plan_setter=lambda plan: setattr(runner, "mid_return_plan", plan),
        devices=devices,
        slots=default_pickup_slots(),
        busy_lock=busy_lock,
        profile_path=store_dir / "midreturn_profile.json",
        preempt=runner.cancel_active,
    )
    return TuningService(
        take=take,
        return_book=return_book,
        mid_return=mid_return,
        studio=studio,
        book_stand=BookStandSlots(
            paths={
                "place": store_dir / "book_stand_slots.json",
                "preplace": store_dir / "book_stand_pickup_slots.json",
            },
            live={
                "place": [take.session._slots],
                "preplace": [return_book.session._slots, mid_return.session._slots],
            },
            sessions=[take.session, return_book.session, mid_return.session],
            busy_lock=busy_lock,
            arm=devices.arm,
        ),
    )


def create_sdk_tuning(
    sdk: Any,
    devices: TuningDevices,
    studio: Optional[StudioBackend],
    *,
    store_dir: Path,
    busy_lock: threading.Lock,
    book_stand_slots_path: Optional[Path] = None,
    book_stand_pickup_slots_path: Optional[Path] = None,
    preempt: Optional[Callable[[], None]] = None,
) -> TuningService:
    """Wire tuning onto the live use cases so profile updates hot-swap them.

    The use cases read ``self.plan`` per step while executing, so replacing
    the attribute takes effect for the very next motion. The slot calibration
    is reused from the use cases to keep both entry points consistent.

    The mid-return plan lives on ``sdk._mid_return`` (seeded from the
    persisted profile at composition); its insert Y offset comes from the
    plan's ``target_y_offset_cm``.
    """

    take_use_case = sdk._take_book
    return_use_case = sdk._return_book
    mid_return_use_case = sdk._mid_return
    store_dir = Path(store_dir)
    take = TuningKind(
        "take",
        plan_getter=lambda: take_use_case.plan,
        plan_setter=lambda plan: setattr(take_use_case, "plan", plan),
        devices=devices,
        slots=take_use_case.slots,
        busy_lock=busy_lock,
        profile_path=store_dir / "take_profile.json",
        preempt=preempt,
    )
    return_book = TuningKind(
        "return",
        plan_getter=lambda: return_use_case.plan,
        plan_setter=lambda plan: setattr(return_use_case, "plan", plan),
        devices=devices,
        slots=return_use_case.pickup_slots,
        busy_lock=busy_lock,
        profile_path=store_dir / "return_profile.json",
        preempt=preempt,
    )
    mid_return = TuningKind(
        "midreturn",
        plan_getter=lambda: mid_return_use_case.plan,
        plan_setter=lambda plan: setattr(mid_return_use_case, "plan", plan),
        devices=devices,
        slots=mid_return_use_case.pickup_slots,
        busy_lock=busy_lock,
        profile_path=store_dir / "midreturn_profile.json",
        preempt=preempt,
    )
    return TuningService(
        take=take,
        return_book=return_book,
        mid_return=mid_return,
        studio=studio,
        book_stand=BookStandSlots(
            paths={
                "place": Path(book_stand_slots_path or DEFAULT_BOOK_STAND_SLOTS_PATH),
                "preplace": Path(
                    book_stand_pickup_slots_path or DEFAULT_BOOK_STAND_PICKUP_SLOTS_PATH
                ),
            },
            live={
                "place": [
                    take_use_case.slots,
                    take.session._slots,
                ],
                "preplace": [
                    return_use_case.pickup_slots,
                    mid_return_use_case.pickup_slots,
                    return_book.session._slots,
                    mid_return.session._slots,
                ],
            },
            sessions=[take.session, return_book.session, mid_return.session],
            busy_lock=busy_lock,
            arm=devices.arm,
        ),
    )


def build_tuning_router(service: TuningService, dependencies: List) -> APIRouter:
    """Build the tuning routes around one service instance."""

    tuning_router = APIRouter(prefix="/api/v1/tuning", dependencies=dependencies)

    def book_stand() -> BookStandSlots:
        if service.book_stand is None:
            raise HTTPException(
                status_code=503,
                detail="book-stand slot calibration is not wired into this service instance",
            )
        return service.book_stand

    @tuning_router.get("/book-stand/slots")
    def get_book_stand_slots() -> Dict[str, Any]:
        return book_stand().view()

    @tuning_router.put("/book-stand/slots/{group}")
    def put_book_stand_slots(
        group: str, payload: Dict[str, Any], persist: bool = True
    ) -> Dict[str, Any]:
        try:
            return book_stand().update(group, payload, persist=persist)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except BookStandSlotsError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except TuningBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @tuning_router.post("/book-stand/slots/{group}/save")
    def save_book_stand_slots(group: str) -> Dict[str, Any]:
        try:
            return book_stand().save(group)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except TuningBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @tuning_router.post("/book-stand/slots/{group}/execute")
    def execute_book_stand_slot(
        group: str, payload: BookStandExecuteRequest
    ) -> Dict[str, Any]:
        try:
            return book_stand().execute(
                group,
                payload.slot_id,
                payload.action,
                duration_s=payload.duration_s,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ArmNotEnabledError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ArmUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except BookStandSlotsError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except TuningBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (OSError, TimeoutError, RuntimeError) as error:
            # Driver/transport failures are hardware conditions, not bad input.
            raise _hardware_unavailable(error) from error

    def kind_tuning(kind: str) -> TuningKind:
        try:
            return service.kind(kind)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    def resolve_template(template_id: Optional[str]) -> Path:
        """Resolve the prepare template from the studio spine library.

        Without an explicit ``template_id`` the newest library image is used,
        which matches the interactive tuning workflow: segment, then tune.
        """

        studio = service.studio
        if studio is None:
            raise HTTPException(
                status_code=422,
                detail="template_id requires the spine studio library",
            )
        template_id = (template_id or "").strip() or None
        if template_id is None:
            items = studio.list_library()
            if not items:
                raise HTTPException(
                    status_code=422,
                    detail="template_id is required while the spine library is empty",
                )
            template_id = items[0].id
        try:
            return studio.library_image_path(template_id)
        except KeyError as error:
            raise HTTPException(
                status_code=422,
                detail="unknown template_id: %s" % template_id,
            ) from error

    def session_call(
        kind: str, action: Callable[[TuningSession], Dict[str, Any]]
    ) -> Dict[str, Any]:
        try:
            return action(kind_tuning(kind).session)
        except TuningNotPreparedError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except TuningInputError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except TuningBusyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            # Parameter-range failures (rod angle limits, joint limits, bad
            # durations) are operator-fixable tuning input, not service faults.
            raise HTTPException(
                status_code=422,
                detail="%s；请检查该步骤参数范围。" % error,
            ) from error
        except (OSError, TimeoutError, RuntimeError) as error:
            if isinstance(error, RuntimeError) and is_kinematics_error(error):
                raise HTTPException(
                    status_code=422,
                    detail="%s；目标位姿不可达，请检查偏移量/姿态参数。" % error,
                ) from error
            # Device drivers use RuntimeError for rejected feedback, fault
            # codes and arrival checks; SocketCAN itself raises OSError.  None
            # of these is a bad tuning profile or an application bug, so do
            # not hide an arm/CAN condition behind an opaque 500 response.
            raise _hardware_unavailable(error) from error

    @tuning_router.get("/{kind}/profile")
    def get_profile(kind: str) -> Dict[str, Any]:
        return kind_tuning(kind).profile()

    @tuning_router.put("/{kind}/profile")
    def update_profile(
        kind: str, payload: Dict[str, Any], persist: bool = True
    ) -> Dict[str, Any]:
        try:
            if persist:
                return kind_tuning(kind).update_profile(payload)
            return kind_tuning(kind).apply_profile(payload)
        except TuningProfileError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @tuning_router.post("/{kind}/profile/save")
    def save_profile(kind: str) -> Dict[str, Any]:
        """Persist the current live plan after hot-applied (persist=false) edits."""
        return kind_tuning(kind).save_profile()

    @tuning_router.get("/{kind}/session")
    def get_session(kind: str) -> Dict[str, Any]:
        return kind_tuning(kind).session.status()

    @tuning_router.get("/{kind}/session/step/preview")
    def preview_step(
        kind: str,
        step_index: Optional[int] = None,
        all_steps: bool = Query(False, alias="all"),
    ) -> Dict[str, Any]:
        """Motion data steps would send; pure read, no busy lock.

        ``step_index`` previews one arbitrary step (422 out of range);
        ``all=1`` returns every step's preview in one response.
        """
        try:
            if all_steps:
                return kind_tuning(kind).session.preview_all_steps()
            return kind_tuning(kind).session.preview_step(step_index)
        except TuningInputError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @tuning_router.post("/{kind}/session/prepare")
    def prepare_session(kind: str, payload: TuningPrepareRequest) -> Dict[str, Any]:
        template_path = resolve_template(payload.template_id)
        return session_call(kind, lambda session: session.prepare(payload.slot, template_path))

    @tuning_router.post("/{kind}/session/step/next")
    def step_next(kind: str) -> Dict[str, Any]:
        return session_call(kind, lambda session: session.run_next())

    @tuning_router.post("/{kind}/session/step/previous")
    def step_previous(kind: str) -> Dict[str, Any]:
        return session_call(kind, lambda session: session.run_previous())

    @tuning_router.post("/{kind}/session/step/run")
    def step_run(kind: str, payload: TuningStepRunRequest) -> Dict[str, Any]:
        return session_call(
            kind,
            lambda session: (
                session.run_step(payload.index)
                if payload.index is not None
                else session.run_next()
            ),
        )

    @tuning_router.post("/{kind}/session/reset")
    def reset_session(kind: str) -> Dict[str, Any]:
        return session_call(kind, lambda session: session.reset())

    @tuning_router.post("/{kind}/arm/enable")
    def arm_enable(kind: str) -> Dict[str, Any]:
        return session_call(kind, lambda session: session.enable_arm())

    @tuning_router.post("/{kind}/arm/disable")
    def arm_disable(kind: str) -> Dict[str, Any]:
        return session_call(kind, lambda session: session.disable_arm())

    @tuning_router.post("/{kind}/arm/debug-pose")
    def arm_debug_pose(kind: str) -> Dict[str, Any]:
        return session_call(kind, lambda session: session.move_debug_pose())

    return tuning_router
