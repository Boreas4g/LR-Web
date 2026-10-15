"""Return a book from the onboard stand to a shelf beside a located reference book."""

from __future__ import annotations

from typing import Any, Dict, Optional

from bookarm.api.requests import ReturnBookRequest, ReturnBookStage
from bookarm.api.results import WorkflowResult, WorkflowTask
from bookarm.application.post_vision import post_vision_check_align
from bookarm.application.task_manager import TaskManager
from bookarm.application.use_cases.mid_return import (
    check_insert_chain_reachable,
    compute_insert_target,
    insert_chain_poses,
    pickup_book_from_stand,
    prepare_arm_at_debug,
    retarget_insert_book,
    run_insert_chain,
)
from bookarm.application.workflow_engine import WorkflowEngine
from bookarm.domain.models.shelf import BookStandPickupPlan
from bookarm.domain.models.task import WorkflowKind
from bookarm.domain.plans.return_book import ReturnBookMotionPlan
from bookarm.ports.devices import ArmPort, LiftPort, ReservationPort, RodPort, VisionPort


class ReturnBookUseCase:
    """Seven-stage return workflow mirroring the return tuning session.

    Stages: lift to the shelf height, park the arm at the debug pose,
    re-locate the reference book by its spine template (synchronous — the arm
    sits idle at debug, so the camera match never competes with arm motion
    for CPU), pick the book to be returned from the calibrated book-stand
    pickup slot, compute the insert target (Y1 = target grasp y +
    plan.target_y_offset_cm, z clamped up to plan.min_insert_z_m) plus the
    IK-searched pre-insert x and the four insert-chain poses, fail fast on
    unreachable insert poses, then run the insert chain shared with the
    mid-return flow (pre-insert, insert, push in, partial release, retract,
    debug).
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
        plan: Optional[ReturnBookMotionPlan] = None,
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
        self.plan = plan or ReturnBookMotionPlan()
        self.vehicle_align = vehicle_align

    def create(self, request: ReturnBookRequest) -> WorkflowTask:
        request = None
        return self.task_manager.create(WorkflowKind.RETURN_BOOK)

    def execute(self, task: WorkflowTask, request: ReturnBookRequest) -> WorkflowResult:
        pickup: Optional[BookStandPickupPlan] = None
        if self._requires_pickup(request.stop_after_stage):
            try:
                pickup = self._pickup_for(request)
            except RuntimeError as error:
                return self.engine.reject(task, ReturnBookStage.BOOK_STAND_PICKUP.value, str(error))
        if not self.reservation.acquire("return_book"):
            return self.engine.reject(
                task,
                "reservation",
                "bookarm hardware is reserved by another controller",
            )
        context: Dict[str, Any] = {"insert_book_target": None, "insert_target": None}
        try:
            steps = (
                ("lift_move", lambda: self._move_lift(request), False),
                ("arm_debug_pose", self._prepare_arm, True),
                (
                    "vision_retarget",
                    lambda: retarget_insert_book(
                        self.vision, request.left_neighbor_spine_template, context
                    ),
                    False,
                ),
                (
                    "post_vision_align",
                    lambda: self._post_vision_align(task, request, context),
                    False,
                ),
                (
                    "book_stand_pickup",
                    lambda: self._pickup_from_stand(task, request, self._require_pickup(pickup)),
                    True,
                ),
                (
                    "insert_target",
                    lambda: self._compute_insert_target_and_poses(context),
                    True,
                ),
                (
                    "reachability_check",
                    lambda: check_insert_chain_reachable(self.arm, self.plan, context),
                    False,
                ),
                (
                    "shelf_insert",
                    lambda: run_insert_chain(self.arm, self.engine, task, self.plan, context),
                    True,
                ),
            )
            return self.engine.run(task, self._steps_to_stop_stage(steps, request.stop_after_stage))
        finally:
            self.reservation.release()

    @staticmethod
    def _requires_pickup(stop_after_stage: ReturnBookStage) -> bool:
        return stop_after_stage in {
            ReturnBookStage.BOOK_STAND_PICKUP,
            ReturnBookStage.INSERT_TARGET,
            ReturnBookStage.REACHABILITY_CHECK,
            ReturnBookStage.SHELF_INSERT,
        }

    @staticmethod
    def _require_pickup(pickup: Optional[BookStandPickupPlan]) -> BookStandPickupPlan:
        if pickup is None:
            raise RuntimeError("book-stand pickup plan was not prepared")
        return pickup

    @staticmethod
    def _steps_to_stop_stage(steps: tuple, stop_after_stage: ReturnBookStage) -> tuple:
        for index, (name, _action, _recovery_required) in enumerate(steps):
            if name == stop_after_stage.value:
                return steps[: index + 1]
        raise ValueError("unsupported return-book stop stage: %s" % stop_after_stage)

    def _move_lift(self, request: ReturnBookRequest) -> Dict[str, Any]:
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

    def _post_vision_align(
        self, task: WorkflowTask, request: ReturnBookRequest, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        # 识别后校验：邻书倾斜超阈值即中止；偏离正中则缓速脉冲对齐并更新锚点
        return post_vision_check_align(
            self.vision,
            request.left_neighbor_spine_template,
            context,
            "insert_book_target",
            self.vehicle_align,
            self.plan,
            engine=self.engine,
            task=task,
        )

    def _prepare_arm(self) -> Dict[str, Any]:
        """Use the same enable, zero, and debug-pose sequence as mid_return."""

        return prepare_arm_at_debug(self.arm, self.plan)

    def _compute_insert_target_and_poses(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Compute the insert target from the fresh recognition, then the
        pre-insert x IK search and all four insert-chain poses; the
        reachability check and the insert itself reuse them (no second
        search). A failed search fails here, while the arm holds the book."""

        details = compute_insert_target(self.plan, context)
        poses = insert_chain_poses(self.plan, context["insert_target"], self.arm)
        context["insert_chain_poses"] = poses
        details["pre_insert_x_m"] = poses["pre_insert"].x_m
        return details

    def _pickup_for(self, request: ReturnBookRequest) -> BookStandPickupPlan:
        slot = int(request.source_slot.value)
        if slot not in self.pickup_slots:
            raise RuntimeError("return-book source slot %d is not calibrated" % slot)
        try:
            return self.pickup_slots[slot]
        except KeyError as error:
            raise RuntimeError("book-stand pickup slot %d is not calibrated" % slot) from error

    def _pickup_from_stand(
        self,
        task: WorkflowTask,
        request: ReturnBookRequest,
        pickup: BookStandPickupPlan,
    ) -> Dict[str, Any]:
        """Open, drive onto the slot, close with contact check, retreat to debug."""

        return pickup_book_from_stand(
            self.arm,
            self.engine,
            task,
            self.plan,
            int(request.source_slot.value),
            pickup,
        )
