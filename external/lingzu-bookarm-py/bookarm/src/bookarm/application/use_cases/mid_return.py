"""Return one onboard book to the middle of a shelf gap (mid-return)."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Dict, Optional, Sequence, Union

from bookarm.api.requests import MidReturnBookRequest, MidReturnStage
from bookarm.api.results import WorkflowResult, WorkflowTask
from bookarm.application.post_vision import post_vision_check_align
from bookarm.application.task_manager import TaskManager
from bookarm.application.workflow_engine import WorkflowEngine
from bookarm.domain.models.book import BookTarget, SpineTemplate
from bookarm.domain.models.pose import BasePose, JointPose
from bookarm.domain.models.shelf import BookStandPickupPlan
from bookarm.domain.models.task import WorkflowKind
from bookarm.domain.plans.mid_return import MidReturnMotionPlan
from bookarm.domain.plans.return_book import ReturnBookMotionPlan
from bookarm.domain.policies.gripper import ensure_grasp_contact
from bookarm.domain.policies.safety import ensure_joint_target
from bookarm.ports.devices import ArmPort, LiftPort, ReservationPort, RodPort, VisionPort


# The return-book flow runs this same insert pipeline (its plan exposes the
# same attribute names), so the shared stages are module-level helpers
# parameterized by plan.
InsertChainPlan = Union[MidReturnMotionPlan, ReturnBookMotionPlan]


class OffworkerArmProbe:
    """ArmPort-shaped adapter routing IK probes off the arm command worker.

    Tuning previews/steps run on HTTP request threads and must not queue
    behind a running motion on the arm worker;
    ``check_pose_reachable_offworker`` is pure read-only kinematics (explicit
    seed, no feedback read, no motion).
    """

    def __init__(self, arm: ArmPort) -> None:
        self._arm = arm

    def check_pose_reachable(
        self, target: BasePose, seed: Optional[JointPose] = None, *, label: str
    ) -> JointPose:
        return self._arm.check_pose_reachable_offworker(target, seed, label=label)


def prepare_arm_at_debug(arm: ArmPort, plan: InsertChainPlan) -> Dict[str, Any]:
    """Enable when needed, visit zero, then land on the debug joint pose."""

    initially_enabled = arm.is_enabled()
    moved_to_zero = False
    moved_to_debug = False
    if initially_enabled:
        measured = arm.read_joint_pose()
        try:
            ensure_joint_target(plan.debug_joint, measured, plan.joint_tolerance_rad)
        except RuntimeError:
            arm.move_j(plan.debug_joint, plan.debug_duration_s)
            moved_to_debug = True
    else:
        arm.enable()
        arm.move_j(plan.zero_joint, plan.zero_duration_s)
        moved_to_zero = True
        ensure_joint_target(
            plan.zero_joint,
            arm.read_joint_pose(),
            plan.joint_tolerance_rad,
        )
        arm.move_j(plan.debug_joint, plan.debug_duration_s)
        moved_to_debug = True

    final_pose = arm.read_joint_pose()
    ensure_joint_target(plan.debug_joint, final_pose, plan.joint_tolerance_rad)
    return {
        "arm_was_enabled": initially_enabled,
        "arm_moved_to_zero": moved_to_zero,
        "arm_moved_to_debug_pose": moved_to_debug,
        "arm_debug_joint_pose_rad": plan.debug_joint.values_rad,
        "arm_measured_joint_pose_rad": final_pose.values_rad,
    }


def pickup_book_from_stand(
    arm: ArmPort,
    engine: WorkflowEngine,
    task: WorkflowTask,
    plan: InsertChainPlan,
    source_slot: int,
    pickup: BookStandPickupPlan,
) -> Dict[str, Any]:
    """Open, drive onto the stand slot, close with contact check, retreat to debug."""

    open_command = replace(plan.gripper, open_deg=plan.pickup_open_deg)
    arm.open_gripper(plan.pickup_open_deg, open_command)
    engine.raise_if_cancelled(task)
    arm.move_to_pose_by_move_j(pickup.pre_grasp, pickup.pre_grasp_duration_s)
    engine.raise_if_cancelled(task)
    arm.move_l(pickup.grasp, pickup.grasp_duration_s)
    engine.raise_if_cancelled(task)
    # Picking the return book off the stand slot: an empty close means the
    # slot holds no book or the calibrated pose no longer matches.
    grasp_result = ensure_grasp_contact(
        arm.close_gripper_monitored(plan.gripper),
        scenario="empty slot or misalignment",
    )
    engine.raise_if_cancelled(task)
    arm.move_l(pickup.pre_grasp, pickup.retreat_duration_s)
    engine.raise_if_cancelled(task)
    arm.move_j(plan.debug_joint, plan.debug_duration_s)
    final_pose = arm.read_joint_pose()
    ensure_joint_target(plan.debug_joint, final_pose, plan.joint_tolerance_rad)
    return {
        "book_stand_source_slot": source_slot,
        "gripper_grasp_result": grasp_result,
        "returned_to_debug_pose_after_pickup": True,
        "arm_measured_joint_pose_rad": final_pose.values_rad,
    }


def retarget_insert_book(
    vision: VisionPort, template: SpineTemplate, context: Dict[str, Any]
) -> Dict[str, Any]:
    """Locate the reference book and cache it as the insert anchor.

    Both flows hold the book to be returned at the debug pose while the
    camera (re-)locates the reference book: the mid-return put-back may have
    shifted it, and the return flow takes its first look after the pickup, so
    the insert chain always anchors on this fresh recognition.
    """

    target = vision.locate_book(template)
    context["insert_book_target"] = target
    return {
        "insert_book_target_raw_base_pose": target.raw_pose.values(),
        "insert_book_target_grasp_base_pose": target.grasp_pose.values(),
        "capture_id": target.capture_id,
        "confidence": target.confidence,
    }


def _insert_book_target(context: Dict[str, Any]) -> BookTarget:
    target = context.get("insert_book_target")
    if not isinstance(target, BookTarget):
        raise RuntimeError("insert book target is missing")
    return target


def _insert_target(context: Dict[str, Any]) -> BasePose:
    target = context.get("insert_target")
    if not isinstance(target, BasePose):
        raise RuntimeError("insert target is missing")
    return target


def _chain_poses_from_context(
    plan: InsertChainPlan, context: Dict[str, Any], arm: Optional[ArmPort] = None
) -> Dict[str, BasePose]:
    """The insert-chain poses for this run: the precomputed set when the
    caller cached one (the return flow's insert_target stage stores it under
    ``insert_chain_poses``), otherwise computed now from the insert target."""

    poses = context.get("insert_chain_poses")
    if poses is not None:
        return poses
    return insert_chain_poses(plan, _insert_target(context), arm)


def compute_insert_target(plan: InsertChainPlan, context: Dict[str, Any]) -> Dict[str, Any]:
    """Compute Y1 directly from the fresh (post-pickup) target book: no
    depth gap scan is run; Y1 = target grasp y + plan.target_y_offset_cm."""

    target = _insert_book_target(context)
    grasp_pose = target.grasp_pose
    y_offset_cm = float(plan.target_y_offset_cm)
    y1_m = grasp_pose.y_m + y_offset_cm / 100.0
    # Low insert targets run the wrist into joint limits at insert_rpy_deg;
    # clamp z up to the reachable minimum rather than failing IK.
    z_m = max(grasp_pose.z_m, float(plan.min_insert_z_m))
    insert_target = BasePose(grasp_pose.x_m, y1_m, z_m, 0.0, 0.0, 0.0)
    context["insert_target"] = insert_target
    return {
        "insert_book_target_grasp_base_pose": list(grasp_pose.values()),
        "target_y_offset_cm": y_offset_cm,
        "y1_m": y1_m,
        "min_insert_z_m": float(plan.min_insert_z_m),
        "insert_z_clamped": z_m > grasp_pose.z_m,
        "insert_target_base_pose": list(insert_target.values()),
    }


# Return-flow 预插入位 x search bounds: scan from the outermost candidate
# downward by the step; the first IK-converging x is the numerically greatest
# reachable one (smallest |x|), so the pre-insert pose hugs the outer reach
# boundary for clearance. Plans with pre_insert_x_max_m (return) cap the scan
# start below the near-body flip zone; other plans scan from the module
# default. Mid-return keeps its pinned plan.pre_insert_x_m.
PRE_INSERT_X_SEARCH_START_M = -0.05
PRE_INSERT_X_SEARCH_STEP_M = -0.01
PRE_INSERT_X_SEARCH_MIN_M = -0.80


def search_pre_insert_x(
    arm: ArmPort,
    pose_template: BasePose,
    seed: JointPose,
    x_max_m: Optional[float] = None,
) -> float:
    """Maximum reachable (numerically greatest, still negative) pre-insert x.

    Probes the same IK entry point as the reachability preflight, every
    candidate seeded at the debug joint pose exactly like the preflight's
    first probe, so a found x is judged by the same tolerances/joint-limit
    margins and always passes the later check. ``pose_template`` carries the
    fixed y/z and the required insert orientation; only x varies.
    ``x_max_m`` caps the scan start (return plan's pre_insert_x_max_m): the
    search never returns a shallower x.
    """

    start_m = PRE_INSERT_X_SEARCH_START_M if x_max_m is None else float(x_max_m)
    # Integer stepping avoids float drift over the [MIN, start] span.
    steps = int(round((start_m - PRE_INSERT_X_SEARCH_MIN_M) / -PRE_INSERT_X_SEARCH_STEP_M))
    for index in range(steps + 1):
        x_m = round(start_m + index * PRE_INSERT_X_SEARCH_STEP_M, 6)
        if x_m >= 0.0:
            break  # the pre-insert x must stay negative (in front of the base)
        candidate = replace(pose_template, x_m=x_m)
        try:
            arm.check_pose_reachable(candidate, seed, label="pre_insert_x_search")
        except RuntimeError:
            continue
        return x_m
    raise RuntimeError(
        "预插入位无可达 x：在 [%.2f, %.2f] m 范围内 IK 均不收敛"
        % (PRE_INSERT_X_SEARCH_MIN_M, start_m)
    )


def insert_chain_poses(
    plan: InsertChainPlan, insert_target: BasePose, arm: Optional[ArmPort] = None
) -> Dict[str, BasePose]:
    """Insert-chain arm poses in motion order, shared with the precheck.

    The pre-insert staging pose is flown MovePByJ before the final MoveL
    approach; y/z chain off the insert pose. Its x is the plan's pinned
    base-frame value when the plan carries ``pre_insert_x_m`` (mid-return —
    a relative offset proved unreachable on shallow targets); plans without
    it (return) IK-search the numerically greatest reachable x instead, so
    the staging pose hugs the outer reach boundary. When the plan carries
    ``pre_insert_x_max_m`` (return), the search starts at that cap and never
    returns a shallower x.
    """

    insert = insert_target.offset_cm(plan.insert_offset_cm, rpy_deg=plan.insert_rpy_deg)
    pre_insert_y_m = insert.y_m + float(plan.pre_insert_y_offset_cm) / 100.0
    pre_insert_z_m = insert.z_m + float(plan.pre_insert_z_offset_cm) / 100.0
    pre_insert_x_m = getattr(plan, "pre_insert_x_m", None)
    if pre_insert_x_m is None:
        if arm is None:
            raise RuntimeError("pre-insert x search requires the arm")
        template = replace(insert, y_m=pre_insert_y_m, z_m=pre_insert_z_m)
        pre_insert_x_m = search_pre_insert_x(
            arm,
            template,
            plan.debug_joint,
            x_max_m=getattr(plan, "pre_insert_x_max_m", None),
        )
    pre_insert = replace(
        insert,
        x_m=float(pre_insert_x_m),
        y_m=pre_insert_y_m,
        z_m=pre_insert_z_m,
    )
    # 末尾还书（ReturnBookMotionPlan）额外带 push_z_offset_cm / push_rpy_deg：
    # 推回时可同时微调高低与姿态（相对插入姿态的偏移）；中位还书没有这两个
    # 字段，按 0 处理（与 pre_insert_x_m 同模式）。
    push = insert.offset_cm(
        (plan.push_x_offset_cm, 0.0, float(getattr(plan, "push_z_offset_cm", 0.0))),
        rpy_offset_deg=getattr(plan, "push_rpy_deg", (0.0, 0.0, 0.0)),
    )
    return {
        "pre_insert": pre_insert,
        "insert": insert,
        "push": push,
        # 位置仍基于推回位沿 x 回抽；末尾还书带 retract_rpy_deg 时朝向替换为
        # 该固定值（不随插入/推回姿态），中位还书没有该字段，继续继承推回位
        # 姿态（getattr 缺省 None，与 push_z_offset_cm 同模式）。
        "retract": push.offset_cm(
            (plan.retract_x_offset_cm, 0.0, 0.0),
            rpy_deg=getattr(plan, "retract_rpy_deg", None),
        ),
    }


def check_insert_chain_reachable(
    arm: ArmPort, plan: InsertChainPlan, context: Dict[str, Any]
) -> Dict[str, Any]:
    """Fail fast when any insert-chain arm pose is unreachable; sends no motion.

    Seeded at the debug joint pose (the arm is parked there after the stand
    pickup); each solved pose seeds the next, mirroring execution continuity.
    """

    seed: JointPose = plan.debug_joint
    checked = []
    for label, pose in _chain_poses_from_context(plan, context, arm).items():
        seed = arm.check_pose_reachable(pose, seed, label=label)
        checked.append(label)
    return {"reachability_checked_poses": checked}


def run_insert_chain(
    arm: ArmPort,
    engine: WorkflowEngine,
    task: WorkflowTask,
    plan: InsertChainPlan,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    """Pre-insert, insert at y=Y1, push in x, release, retract, debug."""

    poses = _chain_poses_from_context(plan, context, arm)
    pre_insert = poses["pre_insert"]
    insert = poses["insert"]
    push = poses["push"]
    retract = poses["retract"]
    arm.move_to_pose_by_move_j(pre_insert, plan.pre_insert_duration_s)
    engine.raise_if_cancelled(task)
    arm.move_l(insert, plan.insert_duration_s)
    engine.raise_if_cancelled(task)
    arm.move_l(push, plan.push_duration_s)
    engine.raise_if_cancelled(task)
    release_command = replace(plan.gripper, open_deg=plan.release_open_deg)
    arm.open_gripper(plan.release_open_deg, release_command)
    engine.raise_if_cancelled(task)
    arm.move_l(retract, plan.retract_duration_s)
    engine.raise_if_cancelled(task)
    arm.move_j(plan.debug_joint, plan.debug_duration_s)
    final_pose = arm.read_joint_pose()
    ensure_joint_target(plan.debug_joint, final_pose, plan.joint_tolerance_rad)
    return {
        "insert_base_pose": insert.values(),
        "pre_insert_base_pose": pre_insert.values(),
        "push_base_pose": push.values(),
        "gripper_release_angle_deg": plan.release_open_deg,
        "retract_base_pose": retract.values(),
        "returned_to_debug_pose_after_insert": True,
        "arm_measured_joint_pose_rad": final_pose.values_rad,
    }


class MidReturnBookUseCase:
    """Seven-stage mid-return workflow mirroring the tuning session steps.

    Stages: lift to the shelf height, park the arm at the debug pose, locate
    the left-neighbor spine (first-round target), run the take-book grasp
    chain on it, put the pulled book back loosely (push in, release, clear
    out), pick the book to be returned from the book-stand pickup slot,
    re-locate the same target book (the put-back may have shifted it),
    compute the insert Y1 directly from this fresh target
    (Y1 = target y + plan.target_y_offset_cm; no depth gap scan is run),
    then insert at y=Y1 with x/z from the fresh target, push in,
    release and retract.
    """

    def __init__(
        self,
        *,
        task_manager: TaskManager,
        reservation: ReservationPort,
        lift: LiftPort,
        vision: VisionPort,
        arm: ArmPort,
        rod: RodPort,
        pickup_slots: Dict[int, BookStandPickupPlan],
        plan: Optional[MidReturnMotionPlan] = None,
        vehicle_align: Optional[Any] = None,
    ) -> None:
        self.task_manager = task_manager
        self.engine = WorkflowEngine(task_manager)
        self.reservation = reservation
        self.lift = lift
        self.vision = vision
        self.arm = arm
        self.rod = rod
        self.pickup_slots = dict(pickup_slots)
        self.plan = plan or MidReturnMotionPlan()
        self.vehicle_align = vehicle_align

    def create(self, request: MidReturnBookRequest) -> WorkflowTask:
        request = None
        return self.task_manager.create(WorkflowKind.MID_RETURN_BOOK)

    def execute(self, task: WorkflowTask, request: MidReturnBookRequest) -> WorkflowResult:
        pickup: Optional[BookStandPickupPlan] = None
        if self._requires_pickup(request.stop_after_stage):
            try:
                pickup = self._pickup_for(request)
            except RuntimeError as error:
                return self.engine.reject(task, MidReturnStage.BOOK_STAND_PICKUP.value, str(error))
        if not self.reservation.acquire("mid_return_book"):
            return self.engine.reject(
                task,
                "reservation",
                "bookarm hardware is reserved by another controller",
            )
        context: Dict[str, Any] = {
            "book_target": None,
            "fine_grasp_pose": None,
            "insert_book_target": None,
            "insert_target": None,
        }
        try:
            if request.injected_grasp_pose is not None:
                # Stress-test shortcut: the caller already knows the insert
                # anchor (e.g. the take-out flow's recognized grasp pose), so
                # skip the neighbor grasp+put-back and the fresh re-target;
                # pick up from the stand slot and run the insert chain
                # (tuning steps 24-29) anchored on the injected pose.
                context["insert_book_target"] = _injected_book_target(request.injected_grasp_pose)
                steps = (
                    ("lift_move", lambda: self._move_lift(request), False),
                    ("arm_debug_pose", self._prepare_arm, True),
                    (
                        "book_stand_pickup",
                        lambda: self._pickup_from_stand(task, request, self._require_pickup(pickup)),
                        True,
                    ),
                    (
                        "insert_target",
                        lambda: self._compute_insert_target(context),
                        False,
                    ),
                    (
                        "reachability_check",
                        lambda: self._check_insert_reachability(context),
                        False,
                    ),
                    ("shelf_insert", lambda: self._insert_at_centerline(task, context), True),
                )
            else:
                steps = (
                    ("lift_move", lambda: self._move_lift(request), False),
                    ("arm_debug_pose", self._prepare_arm, True),
                    (
                        "vision_target",
                        lambda: self._acquire_book_target(request, context),
                        False,
                    ),
                    (
                        "post_vision_align",
                        lambda: self._post_vision_align(
                            task, request, context, "book_target"
                        ),
                        False,
                    ),
                    ("book_grasp", lambda: self._grasp_chain(task, context), True),
                    ("book_put_back", lambda: self._put_back(task, context), True),
                    (
                        "book_stand_pickup",
                        lambda: self._pickup_from_stand(task, request, self._require_pickup(pickup)),
                        True,
                    ),
                    (
                        "vision_retarget",
                        lambda: self._retarget_for_insert(request, context),
                        True,
                    ),
                    (
                        "post_vision_retarget_align",
                        lambda: self._post_vision_align(
                            task, request, context, "insert_book_target"
                        ),
                        False,
                    ),
                    (
                        "insert_target",
                        lambda: self._compute_insert_target(context),
                        False,
                    ),
                    (
                        "reachability_check",
                        lambda: self._check_insert_reachability(context),
                        False,
                    ),
                    ("shelf_insert", lambda: self._insert_at_centerline(task, context), True),
                )
            return self.engine.run(
                task, self._steps_to_stop_stage(steps, request.stop_after_stage)
            )
        finally:
            self.reservation.release()

    @staticmethod
    def _requires_pickup(stop_after_stage: MidReturnStage) -> bool:
        return stop_after_stage in {
            MidReturnStage.BOOK_STAND_PICKUP,
            MidReturnStage.VISION_RETARGET,
            MidReturnStage.INSERT_TARGET,
            MidReturnStage.SHELF_INSERT,
        }

    @staticmethod
    def _require_pickup(pickup: Optional[BookStandPickupPlan]) -> BookStandPickupPlan:
        if pickup is None:
            raise RuntimeError("book-stand pickup plan was not prepared")
        return pickup

    @staticmethod
    def _steps_to_stop_stage(steps: tuple, stop_after_stage: MidReturnStage) -> tuple:
        for index, (name, _action, _recovery_required) in enumerate(steps):
            if name == stop_after_stage.value:
                return steps[: index + 1]
        raise ValueError("unsupported mid-return stop stage: %s" % stop_after_stage)

    def _move_lift(self, request: MidReturnBookRequest) -> Dict[str, Any]:
        result = self.lift.move_to_height_mm(
            request.target_height_mm,
            speed_level=request.lift_speed_level,
        )
        return {
            "lift_target_height_mm": result.target_height_mm,
            "lift_measured_height_mm": result.measured_height_mm,
            "lift_speed_level": request.lift_speed_level,
            "lift_tolerance_pulses": result.tolerance_pulses,
            "lift_target_pulses": result.controller_target_pulses,
            "lift_measured_pulses": result.controller_measured_pulses,
        }

    def _prepare_arm(self) -> Dict[str, Any]:
        """Use the same enable, zero, and debug-pose sequence as return_book."""

        return prepare_arm_at_debug(self.arm, self.plan)

    def _acquire_book_target(
        self,
        request: MidReturnBookRequest,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        target = self.vision.locate_book(request.left_neighbor_spine_template)
        context["book_target"] = target
        return {
            "left_neighbor_spine_template": request.left_neighbor_spine_template.path,
            "book_target_raw_base_pose": target.raw_pose.values(),
            "book_target_grasp_base_pose": target.grasp_pose.values(),
            "capture_id": target.capture_id,
            "confidence": target.confidence,
        }

    def _grasp_chain(self, task: WorkflowTask, context: Dict[str, Any]) -> Dict[str, Any]:
        """Steps 0-6 of the tuning session: open, rod home, pregrasp, approach,
        rod hook, fine grasp, close."""

        target = context.get("book_target")
        if not isinstance(target, BookTarget):
            raise RuntimeError("book target is missing")
        plan = self.plan
        grasp_pose = target.grasp_pose
        pregrasp = grasp_pose.offset_cm(plan.pregrasp_offset_cm, rpy_deg=plan.grasp_rpy_deg)
        approach = grasp_pose.offset_cm(plan.approach_offset_cm, rpy_deg=plan.grasp_rpy_deg)
        fine_grasp = grasp_pose.offset_cm(
            plan.fine_grasp_offset_cm, rpy_deg=plan.fine_grasp_rpy_deg
        )
        open_command = replace(plan.gripper, open_deg=plan.pickup_open_deg)
        self.arm.open_gripper(plan.pickup_open_deg, open_command)
        self.engine.raise_if_cancelled(task)
        self.rod.move_to_deg(
            plan.rod_home_deg,
            plan.rod_speed,
            plan.rod_acceleration,
            plan.rod_tolerance_deg,
            plan.rod_timeout_s,
        )
        self.engine.raise_if_cancelled(task)
        self.arm.move_to_pose_by_move_j(pregrasp, plan.pregrasp_duration_s)
        self.engine.raise_if_cancelled(task)
        self.arm.move_l(approach, plan.approach_duration_s)
        self.engine.raise_if_cancelled(task)
        self.rod.move_to_deg(
            plan.rod_grasp_deg,
            plan.rod_speed,
            plan.rod_acceleration,
            plan.rod_tolerance_deg,
            plan.rod_timeout_s,
        )
        self.engine.raise_if_cancelled(task)
        self.arm.move_to_pose_by_move_j(fine_grasp, plan.fine_grasp_duration_s)
        self.engine.raise_if_cancelled(task)
        # Grasping the neighbor book off the shelf: an empty close means the
        # book is not there or the located pose drifted.
        grasp_result = ensure_grasp_contact(
            self.arm.close_gripper_monitored(plan.gripper),
            scenario="empty slot or misalignment",
        )
        context["fine_grasp_pose"] = fine_grasp
        return {
            "pregrasp_base_pose": pregrasp.values(),
            "fine_grasp_base_pose": fine_grasp.values(),
            "gripper_grasp_result": grasp_result,
        }

    def _put_back(self, task: WorkflowTask, context: Dict[str, Any]) -> Dict[str, Any]:
        """Steps 7-16: push back in x, partially release, home the rod, then
        lower/full-open/re-close/shift/slight-release/forward clear-out and debug."""

        fine_grasp = context.get("fine_grasp_pose")
        if not isinstance(fine_grasp, BasePose):
            raise RuntimeError("fine-grasp pose is missing")
        plan = self.plan
        push_in = fine_grasp.offset_cm((plan.push_in_x_offset_cm, 0.0, 0.0))
        self.arm.move_l(push_in, plan.push_in_duration_s)
        self.engine.raise_if_cancelled(task)
        partial_command = replace(plan.gripper, open_deg=plan.partial_release_deg)
        self.arm.open_gripper(plan.partial_release_deg, partial_command)
        # Push once more so the fingers sit deep inside the gap before the
        # full-open spread; the lower/shift chain anchors on this pose.
        self.engine.raise_if_cancelled(task)
        re_push = push_in.offset_cm((plan.re_push_x_offset_cm, 0.0, 0.0))
        self.arm.move_l(re_push, plan.re_push_duration_s)
        self.engine.raise_if_cancelled(task)
        self.rod.move_to_deg(
            plan.rod_putback_deg,
            plan.rod_speed,
            plan.rod_acceleration,
            plan.rod_tolerance_deg,
            plan.rod_timeout_s,
        )
        self.engine.raise_if_cancelled(task)
        lower = re_push.offset_cm((0.0, 0.0, plan.lower_z_offset_cm))
        self.arm.move_l(lower, plan.lower_duration_s)
        # Fully open to spread the book gap, then re-grip the book so the
        # sideways shift carries it; after the shift, carry the closed-gripper
        # book forward to a camera-friendly pose, crack the gripper open just
        # enough to unload the fingers, then push forward.
        self.engine.raise_if_cancelled(task)
        full_open_command = replace(plan.gripper, open_deg=plan.full_open_deg)
        self.arm.open_gripper(plan.full_open_deg, full_open_command)
        self.engine.raise_if_cancelled(task)
        # Re-gripping the put-back book: an empty close means the book fell or
        # was pushed out of the fingers' reach during the gap spread.
        regrip_result = ensure_grasp_contact(
            self.arm.close_gripper_monitored(plan.gripper),
            scenario="book lost between the put-back release and re-grip",
        )
        self.engine.raise_if_cancelled(task)
        shift = lower.offset_cm((0.0, plan.shift_y_offset_cm, 0.0))
        self.arm.move_l(shift, plan.shift_duration_s)
        self.engine.raise_if_cancelled(task)
        carry = shift.offset_cm((plan.carry_forward_x_offset_cm, 0.0, 0.0))
        self.arm.move_l(carry, plan.carry_forward_duration_s)
        self.engine.raise_if_cancelled(task)
        self.arm.open_gripper(plan.partial_release_deg, partial_command)
        self.engine.raise_if_cancelled(task)
        forward = carry.offset_cm((plan.forward_x_offset_cm, 0.0, 0.0))
        self.arm.move_l(forward, plan.forward_duration_s)
        self.engine.raise_if_cancelled(task)
        self.arm.move_j(plan.debug_joint, plan.debug_duration_s)
        final_pose = self.arm.read_joint_pose()
        ensure_joint_target(plan.debug_joint, final_pose, plan.joint_tolerance_rad)
        return {
            "push_in_base_pose": push_in.values(),
            "forward_base_pose": forward.values(),
            "partial_release_deg": plan.partial_release_deg,
            "full_open_deg": plan.full_open_deg,
            "gripper_regrip_result": regrip_result,
            "returned_to_debug_pose": True,
            "arm_measured_joint_pose_rad": final_pose.values_rad,
        }

    def _pickup_for(self, request: MidReturnBookRequest) -> BookStandPickupPlan:
        slot = int(request.source_slot.value)
        if slot not in self.pickup_slots:
            raise RuntimeError("mid-return source slot %d is not calibrated" % slot)
        try:
            return self.pickup_slots[slot]
        except KeyError as error:
            raise RuntimeError("book-stand pickup slot %d is not calibrated" % slot) from error

    def _pickup_from_stand(
        self,
        task: WorkflowTask,
        request: MidReturnBookRequest,
        pickup: BookStandPickupPlan,
    ) -> Dict[str, Any]:
        """Steps 17-20: open, drive onto the slot, close, retreat to debug."""

        return pickup_book_from_stand(
            self.arm,
            self.engine,
            task,
            self.plan,
            int(request.source_slot.value),
            pickup,
        )

    def _post_vision_align(
        self,
        task: WorkflowTask,
        request: MidReturnBookRequest,
        context: Dict[str, Any],
        context_key: str,
    ) -> Dict[str, Any]:
        # 识别后校验：倾斜超阈值即中止；偏离正中则缓速脉冲对齐并更新锚点
        return post_vision_check_align(
            self.vision,
            request.left_neighbor_spine_template,
            context,
            context_key,
            self.vehicle_align,
            self.plan,
            engine=self.engine,
            task=task,
        )

    def _retarget_for_insert(
        self,
        request: MidReturnBookRequest,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Re-locate the target book after the put-back chain: the neighbor
        book may have shifted, so the insert pose must anchor on a fresh
        recognition taken while the arm holds the return book at debug."""

        return retarget_insert_book(self.vision, request.left_neighbor_spine_template, context)

    def _compute_insert_target(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Compute Y1 directly from the fresh (post-pickup) target book: no
        depth gap scan is run; Y1 = target grasp y + plan.target_y_offset_cm."""

        return compute_insert_target(self.plan, context)

    def _insert_poses(self, insert_target: BasePose) -> Dict[str, BasePose]:
        """Insert-chain arm poses in motion order, shared with the precheck."""

        return insert_chain_poses(self.plan, insert_target)

    def _check_insert_reachability(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Fail fast when any insert-chain arm pose is unreachable; sends no motion.

        Seeded at the debug joint pose (the arm is parked there after the stand
        pickup); each solved pose seeds the next, mirroring execution continuity.
        """

        return check_insert_chain_reachable(self.arm, self.plan, context)

    def _insert_at_centerline(self, task: WorkflowTask, context: Dict[str, Any]) -> Dict[str, Any]:
        """Steps 23-27: insert at y=Y1, push in x, release, retract, debug."""

        return run_insert_chain(self.arm, self.engine, task, self.plan, context)


def _injected_book_target(values: Sequence[float]) -> BookTarget:
    """Build the insert anchor from a caller-supplied base-frame grasp pose."""

    if len(values) != 6 or not all(math.isfinite(float(v)) for v in values):
        raise RuntimeError("injected grasp pose must be six finite values (m, m, m, rad, rad, rad)")
    pose = BasePose(*(float(v) for v in values))
    return BookTarget(raw_pose=pose, grasp_pose=pose, confidence=1.0, capture_id="injected")
