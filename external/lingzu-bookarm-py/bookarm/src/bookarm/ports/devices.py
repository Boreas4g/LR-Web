"""Hardware-neutral interfaces; implementations live in infrastructure."""

from __future__ import annotations

from typing import Optional, Protocol

from bookarm.domain.models.book import BookTarget, ShelfPlacementTarget, SpineTemplate
from bookarm.domain.models.lift import LiftPositionResult
from bookarm.domain.models.pose import BasePose, JointPose
from bookarm.domain.plans.take_book import GripperCommand


class ReservationPort(Protocol):
    def acquire(self, purpose: str) -> bool:
        ...

    def release(self) -> None:
        ...


class LiftPort(Protocol):
    def move_to_height_mm(
        self,
        target_height_mm: float,
        *,
        speed_level: int,
    ) -> LiftPositionResult:
        """Move at an AIM speed level and return after its feedback confirms arrival."""
        ...


class VisionPort(Protocol):
    def locate_book(self, template: SpineTemplate) -> BookTarget:
        ...

    def locate_shelf_placement(
        self,
        left_neighbor_template: SpineTemplate,
        *,
        reference_point_from_top_ratio: float,
    ) -> ShelfPlacementTarget:
        """Locate a horizontal-center reference point on a known neighboring spine."""
        ...


class ArmPort(Protocol):
    def is_enabled(self) -> bool:
        """Return the physical Motor-mode state, or raise if it is unknown."""
        ...

    def enable(self) -> None:
        ...

    def disable_at_safe_pose(self, duration_s: float = 3.0) -> dict[str, object]:
        """Move to the checked disable pose before sending motor disable frames."""
        ...

    def move_j(self, target: JointPose, duration_s: float) -> None:
        ...

    def read_joint_pose(self) -> JointPose:
        ...

    def move_to_pose_by_move_j(self, target: BasePose, duration_s: float) -> None:
        ...

    def move_l(self, target: BasePose, duration_s: float) -> None:
        ...

    def check_pose_reachable(
        self, target: BasePose, seed: Optional[JointPose], *, label: str
    ) -> JointPose:
        """Solve IK for ``target`` without moving; return the solution for chain seeding.

        Implementations must use the same solver, fallback seeds, and joint-limit
        validation as motion execution, so a passing precheck means the later
        motion-time solve passes too. ``seed`` of None reads the live joint
        feedback. An unreachable target raises RuntimeError carrying
        "arm pose unreachable (<label>)" and the solver reason.
        """
        ...

    def check_pose_reachable_offworker(
        self, target: BasePose, seed: JointPose, *, label: str
    ) -> JointPose:
        """Same judgment as :meth:`check_pose_reachable`, but safe to call from
        a non-worker thread with an explicit seed: pure read-only kinematics,
        no live feedback read, no motion, never queued behind a running motion.
        """
        ...

    def open_gripper(
        self, target_deg: float, command: Optional[GripperCommand] = None
    ) -> None:
        ...

    def close_gripper_monitored(self, command: GripperCommand) -> dict[str, object]:
        ...


class RodPort(Protocol):
    def move_to_deg(
        self,
        target_deg: float,
        speed: int,
        acceleration: int,
        tolerance_deg: float,
        timeout_s: float,
    ) -> None:
        ...


class PlacementPlanPort(Protocol):
    def get_book_stand_slot(self, slot: int):
        """Return a calibrated SlotPlacementPlan for slots 1 through 4."""
        ...
