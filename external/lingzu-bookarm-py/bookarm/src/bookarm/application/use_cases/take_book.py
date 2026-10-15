"""Complete take-book workflow with vision-guided arm preparation."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from bookarm.api.requests import TakeBookRequest, TakeBookStage
from bookarm.api.results import WorkflowResult, WorkflowTask
from bookarm.application.post_vision import post_vision_check_align
from bookarm.application.task_manager import TaskManager
from bookarm.application.workflow_engine import WorkflowCancelled, WorkflowEngine
from bookarm.domain.models.book import BookTarget
from bookarm.domain.models.pose import BasePose, JointPose
from bookarm.domain.models.shelf import SlotPlacementPlan
from bookarm.domain.models.task import WorkflowKind
from bookarm.domain.plans.speed import apply_speed_to_gripper
from bookarm.domain.plans.take_book import GripperCommand, TakeBookMotionPlan
from bookarm.domain.policies.gripper import ensure_grasp_contact
from bookarm.domain.policies.safety import ensure_joint_target
from bookarm.ports.devices import ArmPort, LiftPort, ReservationPort, RodPort, VisionPort


class TakeBookUseCase:
    """One full take-book operation; GUI buttons are not part of this class."""

    BOOK_GRASP_OPEN_ANGLE_DEG = 50.0

    def __init__(
        self,
        *,
        task_manager: TaskManager,
        reservation: ReservationPort,
        lift: LiftPort,
        vision: VisionPort,
        arm: ArmPort,
        rod: RodPort,
        slots: Dict[int, SlotPlacementPlan],
        plan: Optional[TakeBookMotionPlan] = None,
        grasp_step_approval: Optional[Callable[[str], bool]] = None,
        grasp_step_reporter: Optional[Callable[[str, str, Optional[str]], None]] = None,
        vehicle_align: Optional[Any] = None,
    ) -> None:
        self.task_manager = task_manager
        self.engine = WorkflowEngine(task_manager)
        self.reservation = reservation
        self.lift = lift
        self.vision = vision
        self.arm = arm
        self.rod = rod
        self.slots = dict(slots)
        self.plan = plan or TakeBookMotionPlan()
        self.grasp_step_approval = grasp_step_approval
        self.grasp_step_reporter = grasp_step_reporter
        self.vehicle_align = vehicle_align

    def create(self, request: TakeBookRequest) -> WorkflowTask:
        request = None
        return self.task_manager.create(WorkflowKind.TAKE_BOOK)

    def disable_arm(self, duration_s: float = 3.0) -> dict[str, object]:
        """Move to the designated disable pose, verify arrival, then disable the arm."""

        if not self.reservation.acquire("disable_arm"):
            raise RuntimeError("cannot disable arm: bookarm hardware is reserved by another controller")
        try:
            return self.arm.disable_at_safe_pose(duration_s)
        finally:
            self.reservation.release()

    def execute(self, task: WorkflowTask, request: TakeBookRequest) -> WorkflowResult:
        placement: Optional[SlotPlacementPlan] = None
        if self._requires_placement(request.stop_after_stage):
            # Validate the destination before commanding the lift or grasping a book.
            try:
                placement = self._placement_for(request)
            except RuntimeError as error:
                return self.engine.reject(task, "book_stand_place", str(error))
        if not self.reservation.acquire("take_book"):
            return self.engine.reject(task, "reservation", "bookarm hardware is reserved by another controller")
        context: Dict[str, Any] = {"target": None}
        try:
            steps = (
                ("lift_move", lambda: self._move_lift(request), False),
                ("vision_target", lambda: self._acquire_target(request, context), False),
                (
                    "post_vision_align",
                    lambda: self._post_vision_align(task, request, context),
                    False,
                ),
                ("arm_debug_pose", self._prepare_arm_for_grasp, True),
                (
                    "reachability_check",
                    lambda: self._check_reachability(context, placement),
                    False,
                ),
                ("book_grasp", lambda: self._grasp_book(task, context), True),
                (
                    "book_stand_place",
                    lambda: self._place_in_stand(task, request, self._require_placement(placement)),
                    True,
                ),
                ("arm_standby", self._move_to_standby, True),
            )
            return self.engine.run(task, self._steps_to_stop_stage(steps, request.stop_after_stage))
        finally:
            self.reservation.release()

    @staticmethod
    def _requires_placement(stop_after_stage: TakeBookStage) -> bool:
        return stop_after_stage in {TakeBookStage.BOOK_STAND_PLACE, TakeBookStage.ARM_STANDBY}

    @staticmethod
    def _require_placement(placement: Optional[SlotPlacementPlan]) -> SlotPlacementPlan:
        if placement is None:
            raise RuntimeError("book-stand placement was not prepared")
        return placement

    @staticmethod
    def _steps_to_stop_stage(steps: tuple, stop_after_stage: TakeBookStage) -> tuple:
        for index, (name, _action, _recovery_required) in enumerate(steps):
            if name == stop_after_stage.value:
                return steps[:index + 1]
        raise ValueError("unsupported take-book stop stage: %s" % stop_after_stage)

    def _move_lift(self, request: TakeBookRequest) -> Dict[str, Any]:
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

    def _acquire_target(self, request: TakeBookRequest, context: Dict[str, Any]) -> Dict[str, Any]:
        target = self.vision.locate_book(request.spine_template)
        context["target"] = target
        return {
            "book_target_raw_base_pose": target.raw_pose.values(),
            "book_target_grasp_base_pose": target.grasp_pose.values(),
            "capture_id": target.capture_id,
            "confidence": target.confidence,
        }

    def _post_vision_align(
        self, task: WorkflowTask, request: TakeBookRequest, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """识别后校验：书脊倾斜超阈值即中止；偏离正中则缓速脉冲对齐并更新位姿。"""

        return post_vision_check_align(
            self.vision,
            request.spine_template,
            context,
            "target",
            self.vehicle_align,
            self.plan,
            engine=self.engine,
            task=task,
        )

    def _prepare_arm_for_grasp(self) -> Dict[str, Any]:
        """Ensure the arm is enabled and safely positioned at the debug joint pose."""

        plan = self.plan
        initially_enabled = self.arm.is_enabled()
        moved_to_zero = False
        moved_to_debug = False
        if initially_enabled:
            measured = self.arm.read_joint_pose()
            try:
                ensure_joint_target(plan.debug_joint, measured, plan.joint_tolerance_rad)
            except RuntimeError:
                self.arm.move_j(plan.debug_joint, plan.debug_duration_s)
                moved_to_debug = True
        else:
            self.arm.enable()
            self.arm.move_j(plan.zero_joint, plan.zero_duration_s)
            moved_to_zero = True
            ensure_joint_target(plan.zero_joint, self.arm.read_joint_pose(), plan.joint_tolerance_rad)
            self.arm.move_j(plan.debug_joint, plan.debug_duration_s)
            moved_to_debug = True

        final_pose = self.arm.read_joint_pose()
        ensure_joint_target(plan.debug_joint, final_pose, plan.joint_tolerance_rad)
        return {
            "arm_was_enabled": initially_enabled,
            "arm_moved_to_zero": moved_to_zero,
            "arm_moved_to_debug_pose": moved_to_debug,
            "arm_debug_joint_pose_rad": plan.debug_joint.values_rad,
            "arm_measured_joint_pose_rad": final_pose.values_rad,
        }

    def _grasp_poses(self, target: BookTarget) -> Dict[str, BasePose]:
        """Grasp-chain poses in motion order, shared with the reachability precheck."""

        plan = self.plan
        grasp_pose = target.grasp_pose
        fine_grasp = grasp_pose.offset_cm(plan.fine_grasp_offset_cm, rpy_deg=plan.fine_grasp_rpy_deg)
        return {
            "arm_pregrasp": grasp_pose.offset_cm(plan.pregrasp_offset_cm, rpy_deg=plan.grasp_rpy_deg),
            "arm_approach": grasp_pose.offset_cm(plan.approach_offset_cm, rpy_deg=plan.grasp_rpy_deg),
            "arm_fine_grasp": fine_grasp,
            "arm_post_grasp": fine_grasp.offset_cm(
                plan.post_grasp_offset_cm, rpy_offset_deg=plan.post_grasp_rpy_offset_deg
            ),
        }

    def _check_reachability(
        self, context: Dict[str, Any], placement: Optional[SlotPlacementPlan]
    ) -> Dict[str, Any]:
        """Fail fast when any upcoming arm pose is unreachable; sends no motion.

        The chain is seeded at the debug joint pose (the arm's position at this
        point of the workflow) and each solved pose seeds the next, mirroring
        the execution continuity of the grasp and placement chains.
        """

        target = context.get("target")
        if not isinstance(target, BookTarget):
            raise RuntimeError("book target is missing")
        seed: JointPose = self.plan.debug_joint
        checked = []
        for label, pose in self._grasp_poses(target).items():
            seed = self.arm.check_pose_reachable(pose, seed, label=label)
            checked.append(label)
        if placement is not None:
            stand_poses = (
                ("stand_pre_place", placement.pre_place),
                ("stand_place", placement.place),
                ("stand_lower_place", placement.lower_place),
            )
            for label, pose in stand_poses:
                seed = self.arm.check_pose_reachable(pose, seed, label=label)
                checked.append(label)
        return {"reachability_checked_poses": checked}

    def _grasp_book(self, task: WorkflowTask, context: Dict[str, Any]) -> Dict[str, Any]:
        target = context.get("target")
        if not isinstance(target, BookTarget):
            raise RuntimeError("book target is missing")
        plan = self.plan

        poses = self._grasp_poses(target)
        pregrasp = poses["arm_pregrasp"]
        approach = poses["arm_approach"]
        fine_grasp = poses["arm_fine_grasp"]
        post_grasp = poses["arm_post_grasp"]
        # Use the same public ArmService.open_gripper command shape as the
        # verified gripper_test script, independent of take-book profile
        # tuning; only the plan's speed level may scale its speeds, so the
        # open follows the same level as every other gripper motion.
        grasp_open_command = apply_speed_to_gripper(
            GripperCommand(open_deg=self.BOOK_GRASP_OPEN_ANGLE_DEG), plan.speed_level
        )
        steps = (
            (
                "open_gripper",
                lambda: self.arm.open_gripper(self.BOOK_GRASP_OPEN_ANGLE_DEG, grasp_open_command),
            ),
            (
                "rod_home",
                lambda: self.rod.move_to_deg(
                    plan.rod_home_deg, plan.rod_speed, plan.rod_acceleration,
                    plan.rod_tolerance_deg, plan.rod_timeout_s,
                ),
            ),
            ("arm_pregrasp", lambda: self.arm.move_to_pose_by_move_j(pregrasp, plan.pregrasp_duration_s)),
            ("arm_approach", lambda: self.arm.move_l(approach, plan.approach_duration_s)),
            (
                "rod_grasp",
                lambda: self.rod.move_to_deg(
                    plan.rod_grasp_deg, plan.rod_speed, plan.rod_acceleration,
                    plan.rod_tolerance_deg, plan.rod_timeout_s,
                ),
            ),
            (
                "arm_fine_grasp",
                lambda: self.arm.move_to_pose_by_move_j(fine_grasp, plan.fine_grasp_duration_s),
            ),
            ("close_gripper", lambda: self._close_gripper_checked(plan.gripper)),
            ("arm_post_grasp", lambda: self.arm.move_to_pose_by_move_j(post_grasp, plan.post_grasp_duration_s)),
            ("arm_return_debug", lambda: self.arm.move_j(plan.debug_joint, plan.debug_duration_s)),
        )
        completed_steps = []
        for name, action in steps:
            try:
                self.engine.raise_if_cancelled(task)
                self._require_grasp_step_approval(name)
                self._report_grasp_step(name, "started")
                action()
            except WorkflowCancelled:
                raise
            except Exception as error:
                self._report_grasp_step(name, "failed", str(error))
                raise RuntimeError("book-grasp step %s failed: %s" % (name, error)) from error
            completed_steps.append(name)
            self._report_grasp_step(name, "completed")
        return {
            "grasp_completed": True,
            "grasp_step_mode": self.grasp_step_approval is not None,
            "grasp_steps_completed": completed_steps,
        }

    def _close_gripper_checked(self, command: GripperCommand) -> Dict[str, Any]:
        """Close on the shelf book and refuse to carry an empty gripper on."""

        return ensure_grasp_contact(
            self.arm.close_gripper_monitored(command),
            scenario="empty slot or misalignment",
        )

    def _require_grasp_step_approval(self, step_name: str) -> None:
        """Optionally gate each grasp motion for supervised test execution."""

        if self.grasp_step_approval is not None and not self.grasp_step_approval(step_name):
            raise RuntimeError(
                "operator declined book-grasp step %s; no later book-grasp action was sent" % step_name
            )

    def _report_grasp_step(self, step_name: str, event: str, error: Optional[str] = None) -> None:
        """Send optional per-step progress without allowing reporting to alter motion."""

        if self.grasp_step_reporter is None:
            return
        try:
            self.grasp_step_reporter(step_name, event, error)
        except Exception:
            pass

    def _placement_for(self, request: TakeBookRequest) -> SlotPlacementPlan:
        try:
            return self.slots[int(request.destination_slot.value)]
        except KeyError as error:
            raise RuntimeError("book-stand slot %d is not calibrated" % int(request.destination_slot.value)) from error

    def _place_in_stand(
        self, task: WorkflowTask, request: TakeBookRequest, placement: SlotPlacementPlan
    ) -> Dict[str, Any]:
        # book_grasp ends at debug_joint, so enter and leave every stand slot
        # through its collision-checked pre-place pose.
        self.arm.move_to_pose_by_move_j(placement.pre_place, placement.pre_place_duration_s)
        self.engine.raise_if_cancelled(task)
        self.arm.move_l(placement.place, placement.place_duration_s)
        self.engine.raise_if_cancelled(task)
        self.arm.move_l(placement.lower_place, placement.lower_place_duration_s)
        self.engine.raise_if_cancelled(task)
        release_command = apply_speed_to_gripper(
            GripperCommand(open_deg=self.BOOK_GRASP_OPEN_ANGLE_DEG), self.plan.speed_level
        )
        self.arm.open_gripper(self.BOOK_GRASP_OPEN_ANGLE_DEG, release_command)
        # 拨杆自拨书后一直外伸；开爪后先收回拨杆再撤臂，避免拖带书本/书立。
        self.engine.raise_if_cancelled(task)
        self.rod.move_to_deg(
            self.plan.rod_home_deg, self.plan.rod_speed, self.plan.rod_acceleration,
            self.plan.rod_tolerance_deg, self.plan.rod_timeout_s,
        )
        self.engine.raise_if_cancelled(task)
        self.arm.move_l(placement.pre_place, placement.retreat_duration_s)
        self.engine.raise_if_cancelled(task)
        self.arm.move_j(self.plan.debug_joint, self.plan.debug_duration_s)
        ensure_joint_target(
            self.plan.debug_joint,
            self.arm.read_joint_pose(),
            self.plan.joint_tolerance_rad,
        )
        return {
            "book_stand_slot": int(request.destination_slot.value),
            "returned_to_debug_pose": True,
        }

    def _move_to_standby(self) -> Dict[str, Any]:
        self.arm.move_j(self.plan.zero_joint, self.plan.zero_duration_s)
        ensure_joint_target(self.plan.zero_joint, self.arm.read_joint_pose(), self.plan.joint_tolerance_rad)
        return {"standby_joint_pose_rad": self.plan.zero_joint.values_rad}
