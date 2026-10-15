"""Mid-return workflow tests using only in-memory device fakes."""

from __future__ import annotations

import math
from pathlib import Path
import unittest
from unittest.mock import patch

from bookarm.api.requests import MidReturnBookRequest, MidReturnStage
from bookarm.application.task_manager import TaskManager
from bookarm.application.use_cases.mid_return import MidReturnBookUseCase
from bookarm.domain.models.book import BookIdentity, BookTarget, SpineTemplate
from bookarm.domain.models.lift import LiftPositionResult
from bookarm.domain.models.pose import BasePose, JointPose
from bookarm.domain.models.shelf import BookStandPickupPlan, BookStandSlot
from bookarm.domain.models.task import TaskState
from bookarm.domain.plans.mid_return import MidReturnMotionPlan


class FakeReservation:
    def __init__(self, *, available=True) -> None:
        self.available = available
        self.purpose = None
        self.released = False

    def acquire(self, purpose):
        if not self.available:
            return False
        self.purpose = purpose
        return True

    def release(self):
        self.released = True


class FakeLift:
    def __init__(self) -> None:
        self.commands = []

    def move_to_height_mm(self, target_height_mm, *, speed_level):
        self.commands.append((float(target_height_mm), int(speed_level)))
        return LiftPositionResult(float(target_height_mm), float(target_height_mm), 10, 100, 100)


class FakeVision:
    """First-round spine locator returning one fixed base-frame target."""

    def __init__(self, fail=False, retarget=None) -> None:
        self.fail = fail
        self.calls = []
        self.target = BookTarget(
            raw_pose=BasePose(-0.35, -0.07, 0.23, 0.0, 0.0, 0.0),
            grasp_pose=BasePose(-0.35, -0.07, 0.23, 0.0, 0.0, 0.0),
            pixel_uv=(320, 240),
            confidence=0.9,
            capture_id="capture-mid-1",
        )
        # Returned on the second and later locate (the post-pickup retarget).
        self.retarget = retarget or self.target

    def locate_book(self, template):
        self.calls.append(template)
        if self.fail:
            raise RuntimeError("simulated spine detection failure")
        return self.target if len(self.calls) == 1 else self.retarget


class FakeArm:
    def __init__(
        self, *, enabled=False, joints=None, close_reasons=None, move_l_hook=None,
        unreachable_labels=(), check_solution=None,
    ) -> None:
        self.enabled = enabled
        self.joints = joints or JointPose.from_values((0, 0, 0, 0, 0, 0))
        self.calls = []
        self.close_reasons = list(close_reasons) if close_reasons else []
        self.move_l_hook = move_l_hook
        self.unreachable_labels = set(unreachable_labels)
        self.check_solution = check_solution or JointPose.from_values((0.11, 0.12, 0.13, 0.14, 0.15, 0.16))

    def is_enabled(self):
        self.calls.append("is_enabled")
        return self.enabled

    def enable(self):
        self.calls.append("enable")
        self.enabled = True

    def read_joint_pose(self):
        return self.joints

    def move_j(self, target, duration_s):
        self.calls.append(("move_j", target, duration_s))
        self.joints = target

    def move_to_pose_by_move_j(self, target, duration_s):
        self.calls.append(("move_to_pose_by_move_j", target, duration_s))

    def move_l(self, target, duration_s):
        self.calls.append(("move_l", target, duration_s))
        if self.move_l_hook is not None:
            self.move_l_hook()

    def check_pose_reachable(self, target, seed=None, *, label):
        self.calls.append(("check_pose_reachable", label, target, seed))
        if label in self.unreachable_labels:
            raise RuntimeError("arm pose unreachable (%s): IK did not converge" % label)
        return self.check_solution

    def close_gripper_monitored(self, command):
        self.calls.append(("close_gripper_monitored", command))
        reason = self.close_reasons.pop(0) if self.close_reasons else "object_contact"
        return {"reason": reason}

    def open_gripper(self, target_deg, command=None):
        self.calls.append(("open_gripper", float(target_deg), command))


class FakeRod:
    def __init__(self) -> None:
        self.calls = []

    def move_to_deg(self, target_deg, speed, acceleration, tolerance_deg, timeout_s):
        self.calls.append((target_deg, speed, acceleration, tolerance_deg, timeout_s))


ALL_STAGES = [
    "lift_move",
    "arm_debug_pose",
    "vision_target",
    "post_vision_align",
    "book_grasp",
    "book_put_back",
    "book_stand_pickup",
    "vision_retarget",
    "post_vision_retarget_align",
    "insert_target",
    "reachability_check",
    "shelf_insert",
]


def make_request(
    *,
    slot=BookStandSlot.SLOT_1,
    stop_after=MidReturnStage.SHELF_INSERT,
    speed_level=2,
):
    return MidReturnBookRequest(
        book=BookIdentity("mid-return-book-1"),
        target_height_mm=800.0,
        left_neighbor_spine_template=SpineTemplate(Path("left-neighbor.jpeg")),
        source_slot=slot,
        lift_speed_level=speed_level,
        stop_after_stage=stop_after,
    )


class MidReturnBookWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = MidReturnMotionPlan()
        self.pre_grasp = BasePose(-0.1, -0.3, 0.3, 1.5, 0.0, 3.0)
        self.grasp = BasePose(-0.1, -0.45, 0.25, 1.5, 0.0, 3.0)
        self.pickup = BookStandPickupPlan(self.pre_grasp, self.grasp, 4.0, 2.0, 2.5)

    def build(self, *, arm=None, vision=None, slots=None, reservation=None):
        manager = TaskManager()
        reservation = reservation or FakeReservation()
        lift = FakeLift()
        arm = arm or FakeArm()
        vision = vision or FakeVision()
        rod = FakeRod()
        self.rod = rod
        use_case = MidReturnBookUseCase(
            task_manager=manager,
            reservation=reservation,
            lift=lift,
            vision=vision,
            arm=arm,
            rod=rod,
            pickup_slots={1: self.pickup} if slots is None else slots,
            plan=self.plan,
        )
        return use_case, arm, vision, lift, reservation

    def run_case(self, use_case, request):
        task = use_case.create(request)
        use_case.execute(task, request)
        return task

    def test_runs_all_nine_stages_in_order(self):
        use_case, arm, vision, lift, reservation = self.build()

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual([stage.name for stage in task.result.stages], ALL_STAGES)
        self.assertEqual(lift.commands, [(800.0, 2)])
        # The target book is located twice: once for the grasp chain and once
        # after the slot pickup for the insert chain.
        self.assertEqual(
            vision.calls,
            [
                make_request().left_neighbor_spine_template,
                make_request().left_neighbor_spine_template,
            ],
        )
        self.assertTrue(reservation.released)
        # Slot pickup entered and left through the collision-checked pre_grasp.
        self.assertIn(("move_to_pose_by_move_j", self.pre_grasp, 4.0), arm.calls)
        self.assertIn(("move_l", self.grasp, 2.0), arm.calls)
        self.assertIn(("move_l", self.pre_grasp, 2.5), arm.calls)
        self.assertEqual(
            arm.calls[-1], ("move_j", self.plan.debug_joint, self.plan.debug_duration_s)
        )

    def test_grasp_chain_and_put_back_motion_sequence(self):
        use_case, arm, _vision, _lift, _reservation = self.build()

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        # Fine grasp: vision x +1 cm; the put-back chain anchors on it.
        fine_x = -0.35 + 0.01
        # move_l order: approach, push in, re-push, lower, shift, carry,
        # forward, slot grasp, slot retreat, insert, push, retract.
        move_l_poses = [call[1] for call in arm.calls if call[0] == "move_l"]
        self.assertAlmostEqual(move_l_poses[0].x_m, -0.35)
        self.assertAlmostEqual(move_l_poses[1].x_m, fine_x - 0.05)
        # The re-push drives the fingers deeper; the clear-out anchors on it.
        self.assertAlmostEqual(move_l_poses[2].x_m, fine_x - 0.07)
        self.assertAlmostEqual(move_l_poses[3].x_m, fine_x - 0.07)
        self.assertAlmostEqual(move_l_poses[3].z_m, 0.23 - 0.05)
        self.assertAlmostEqual(move_l_poses[4].y_m, -0.07 + 0.05)
        # Carry stages the camera view; forward chains off the carry pose.
        self.assertAlmostEqual(move_l_poses[5].x_m, fine_x - 0.02)
        self.assertAlmostEqual(move_l_poses[6].x_m, fine_x - 0.02 + 0.10)
        self.assertAlmostEqual(move_l_poses[6].z_m, 0.23 - 0.05)
        # Rod: home, hook, home. Opens: pickup, partial, full (spreads the
        # gap), partial again (after the shift re-grip), pickup, release;
        # closes: grasp, re-grip, pickup.
        self.assertEqual([call[0] for call in self.rod.calls], [0.0, -70.0, 0.0])
        opens = [call[1] for call in arm.calls if call[0] == "open_gripper"]
        self.assertEqual(opens, [50.0, 80.0, 50.0, 80.0, 50.0, 50.0])
        closes = [call for call in arm.calls if call[0] == "close_gripper_monitored"]
        self.assertEqual(len(closes), 3)

    def test_insert_target_derives_from_book_target_without_gap_scan(self):
        use_case, arm, _vision, _lift, _reservation = self.build()

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        # Y1 = target grasp y (-0.07) + default 4 cm offset = -0.03.
        insert_stage = next(stage for stage in task.result.stages if stage.name == "insert_target")
        self.assertAlmostEqual(insert_stage.details["y1_m"], -0.03)
        self.assertAlmostEqual(insert_stage.details["target_y_offset_cm"], 4.0)
        # Pre-insert flies MovePByJ to the pinned base-frame staging x with
        # y/z chained off the insert pose; the final insert is a short linear
        # move. Push x-5; retract x+10 after release.
        pose_moves = [
            call
            for call in arm.calls
            if isinstance(call, tuple) and call[0] == "move_to_pose_by_move_j"
        ]
        # z=0.23 is below the plan's reachability floor and is clamped to 0.25.
        pre_insert = pose_moves[-1][1]
        self.assertAlmostEqual(pre_insert.x_m, -0.33)
        self.assertAlmostEqual(pre_insert.y_m, -0.03)
        self.assertAlmostEqual(pre_insert.z_m, 0.25)
        self.assertAlmostEqual(pre_insert.rx_rad, math.radians(95.0))
        move_l_poses = [call[1] for call in arm.calls if call[0] == "move_l"]
        insert, push, retract = move_l_poses[-3:]
        self.assertAlmostEqual(insert.x_m, -0.35)
        self.assertAlmostEqual(insert.y_m, -0.03)
        self.assertAlmostEqual(insert.z_m, 0.25)
        self.assertAlmostEqual(insert.rx_rad, math.radians(95.0))
        self.assertAlmostEqual(push.x_m, -0.40)
        self.assertAlmostEqual(push.y_m, -0.03)
        self.assertAlmostEqual(retract.x_m, -0.30)
        # 中位还书没有 retract_rpy_deg：回抽继续继承推回位（=插入位）姿态。
        self.assertAlmostEqual(retract.rx_rad, push.rx_rad)
        self.assertAlmostEqual(retract.ry_rad, push.ry_rad)
        self.assertAlmostEqual(retract.rz_rad, push.rz_rad)
        release = [call for call in arm.calls if call[0] == "open_gripper"][-1]
        self.assertEqual(release[1], 50.0)

    def test_injected_grasp_pose_runs_shortened_insert_flow(self):
        injected = (-0.35, -0.07, 0.23, 0.0, 0.0, 0.0)
        request = MidReturnBookRequest(
            book=BookIdentity("mid-return-book-1"),
            target_height_mm=800.0,
            left_neighbor_spine_template=SpineTemplate(Path("left-neighbor.jpeg")),
            source_slot=BookStandSlot.SLOT_1,
            lift_speed_level=2,
            injected_grasp_pose=injected,
        )
        use_case, arm, vision, lift, reservation = self.build()

        task = self.run_case(use_case, request)

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        # Shortened flow: skip neighbor grasp/put-back and the fresh re-target.
        self.assertEqual(
            [stage.name for stage in task.result.stages],
            [
                "lift_move",
                "arm_debug_pose",
                "book_stand_pickup",
                "insert_target",
                "reachability_check",
                "shelf_insert",
            ],
        )
        self.assertEqual(vision.calls, [])
        self.assertEqual(lift.commands, [(800.0, 2)])
        # The insert anchor is the injected pose: Y1 = injected y + 4 cm, and
        # its z (0.23) is clamped up to the 0.25 reachability floor.
        insert_stage = next(stage for stage in task.result.stages if stage.name == "insert_target")
        self.assertAlmostEqual(insert_stage.details["y1_m"], -0.03)
        self.assertAlmostEqual(insert_stage.details["insert_target_base_pose"][2], 0.25)
        self.assertIs(insert_stage.details["insert_z_clamped"], True)
        # Slot pickup still uses the calibrated plan, and the flow ends at debug.
        self.assertIn(("move_to_pose_by_move_j", self.pre_grasp, 4.0), arm.calls)
        self.assertEqual(
            arm.calls[-1], ("move_j", self.plan.debug_joint, self.plan.debug_duration_s)
        )
        self.assertTrue(reservation.released)

    def test_injected_grasp_pose_rejects_malformed_values(self):
        from bookarm.application.use_cases.mid_return import _injected_book_target

        with self.assertRaises(RuntimeError):
            _injected_book_target((0.1, 0.2, 0.3, 0.0, 0.0))
        with self.assertRaises(RuntimeError):
            _injected_book_target((0.1, 0.2, float("nan"), 0.0, 0.0, 0.0))

    def test_insert_chain_anchors_on_the_post_pickup_retarget(self):
        shifted = BookTarget(            raw_pose=BasePose(-0.35, -0.05, 0.23, 0.0, 0.0, 0.0),
            grasp_pose=BasePose(-0.35, -0.05, 0.23, 0.0, 0.0, 0.0),
            pixel_uv=(330, 240),
            confidence=0.9,
            capture_id="capture-mid-2",
        )
        use_case, arm, _vision, _lift, _reservation = self.build(
            vision=FakeVision(retarget=shifted)
        )

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        insert_stage = next(stage for stage in task.result.stages if stage.name == "insert_target")
        # y1 follows the fresh recognition (-0.05 + 4 cm), not the prepare one.
        self.assertAlmostEqual(insert_stage.details["y1_m"], -0.01)
        move_l_poses = [call[1] for call in arm.calls if call[0] == "move_l"]
        self.assertAlmostEqual(move_l_poses[-3].y_m, -0.01)

    def test_put_back_rod_move_uses_the_independent_putback_position(self):
        use_case, _arm, _vision, _lift, _reservation = self.build()
        from dataclasses import replace

        use_case.plan = replace(self.plan, rod_putback_deg=15.0)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        # Rod sequence: grasp-chain home, hook (rod_grasp_deg), put-back move.
        self.assertEqual(self.rod.calls[0][0], 0.0)
        self.assertEqual(self.rod.calls[1][0], -70.0)
        self.assertEqual(self.rod.calls[2][0], 15.0)

    def test_insert_target_honours_plan_y_offset(self):
        use_case, arm, _vision, _lift, _reservation = self.build()
        from dataclasses import replace

        use_case.plan = replace(self.plan, target_y_offset_cm=7.5)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        insert_stage = next(stage for stage in task.result.stages if stage.name == "insert_target")
        self.assertAlmostEqual(insert_stage.details["y1_m"], 0.005)
        move_l_poses = [call[1] for call in arm.calls if call[0] == "move_l"]
        self.assertAlmostEqual(move_l_poses[-3].y_m, 0.005)

    def test_hot_swapped_plan_applies_to_the_next_run(self):
        use_case, arm, _vision, _lift, _reservation = self.build()
        from dataclasses import replace

        use_case.plan = replace(self.plan, push_x_offset_cm=-8.0)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        move_l_poses = [call[1] for call in arm.calls if call[0] == "move_l"]
        push = move_l_poses[-2]
        self.assertAlmostEqual(push.x_m, -0.43)

    def test_stop_after_stage_truncates_the_workflow(self):
        use_case, arm, _vision, lift, reservation = self.build(slots={})

        task = self.run_case(
            use_case,
            make_request(stop_after=MidReturnStage.BOOK_GRASP),
        )

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(
            [stage.name for stage in task.result.stages],
            ["lift_move", "arm_debug_pose", "vision_target", "post_vision_align", "book_grasp"],
        )
        self.assertEqual(lift.commands, [(800.0, 2)])
        self.assertTrue(reservation.released)

    def test_stop_after_lift_does_not_require_slot_or_start_arm(self):
        use_case, arm, _vision, lift, reservation = self.build(slots={})

        task = self.run_case(
            use_case,
            make_request(slot=BookStandSlot.SLOT_4, stop_after=MidReturnStage.LIFT_MOVE),
        )

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(lift.commands, [(800.0, 2)])
        self.assertEqual(arm.calls, [])
        self.assertTrue(reservation.released)

    def test_slot_four_is_rejected_before_any_hardware_command(self):
        use_case, arm, _vision, lift, reservation = self.build(slots={})

        task = self.run_case(use_case, make_request(slot=BookStandSlot.SLOT_4))

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertIn("slot 4 is not calibrated", task.result.error)
        self.assertEqual(lift.commands, [])
        self.assertEqual(arm.calls, [])
        self.assertFalse(reservation.released)

    def test_reservation_conflict_fails_before_any_motion(self):
        use_case, arm, _vision, lift, _reservation = self.build(
            reservation=FakeReservation(available=False)
        )

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertIn("reserved", task.result.error)
        self.assertEqual(lift.commands, [])
        self.assertEqual(arm.calls, [])

    def test_vision_failure_keeps_arm_untouched(self):
        use_case, arm, _vision, _lift, reservation = self.build(vision=FakeVision(fail=True))

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.result.stages[-1].name, "vision_target")
        self.assertIn("simulated spine detection failure", task.result.error)
        self.assertTrue(reservation.released)


class MidReturnReachabilityTests(unittest.TestCase):
    """Fail-fast insert-chain reachability precheck after the fresh re-target."""

    def setUp(self) -> None:
        self.plan = MidReturnMotionPlan()
        self.pre_grasp = BasePose(-0.1, -0.3, 0.3, 1.5, 0.0, 3.0)
        self.grasp = BasePose(-0.1, -0.45, 0.25, 1.5, 0.0, 3.0)
        self.pickup = BookStandPickupPlan(self.pre_grasp, self.grasp, 4.0, 2.0, 2.5)

    def build(self, arm):
        manager = TaskManager()
        return MidReturnBookUseCase(
            task_manager=manager,
            reservation=FakeReservation(),
            lift=FakeLift(),
            vision=FakeVision(),
            arm=arm,
            rod=FakeRod(),
            pickup_slots={1: self.pickup},
            plan=self.plan,
        )

    def run_case(self, use_case, request):
        task = use_case.create(request)
        use_case.execute(task, request)
        return task

    @staticmethod
    def _checks(arm):
        return [call for call in arm.calls if isinstance(call, tuple) and call[0] == "check_pose_reachable"]

    @staticmethod
    def _move_l_calls(arm):
        return [call for call in arm.calls if isinstance(call, tuple) and call[0] == "move_l"]

    def test_all_reachable_preserves_the_execution_order(self):
        arm = FakeArm()
        use_case = self.build(arm)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        checks = self._checks(arm)
        self.assertEqual([call[1] for call in checks], ["pre_insert", "insert", "push", "retract"])
        # Seeded from the debug pose (the pickup parks the arm there); each
        # solved pose seeds the next one.
        self.assertEqual(checks[0][3], self.plan.debug_joint)
        for call in checks[1:]:
            self.assertEqual(call[3], arm.check_solution)
        # Probing ran no motion; the insert chain still executes in order.
        insert, push, retract = [call[1] for call in self._move_l_calls(arm)][-3:]
        self.assertAlmostEqual(insert.x_m, -0.35)
        self.assertAlmostEqual(push.x_m, -0.40)
        self.assertAlmostEqual(retract.x_m, -0.30)
        self.assertEqual(len(self._move_l_calls(arm)), 12)
        self.assertEqual(
            task.result.payload["reachability_checked_poses"],
            ["pre_insert", "insert", "push", "retract"],
        )

    def test_unreachable_pose_fails_before_the_insert_chain(self):
        arm = FakeArm(unreachable_labels={"push"})
        use_case = self.build(arm)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.result.stages[-1].name, "reachability_check")
        self.assertIn("arm pose unreachable (push)", task.result.error)
        self.assertIn("IK did not converge", task.result.error)
        self.assertEqual([call[1] for call in self._checks(arm)], ["pre_insert", "insert", "push"])
        # Only the nine pre-insert move_l motions ran; nothing moved after the
        # failed check.
        self.assertEqual(len(self._move_l_calls(arm)), 9)
        self.assertEqual(arm.calls[-1][0], "check_pose_reachable")

    def test_injected_flow_unreachable_fails_before_insert_motions(self):
        injected = (-0.35, -0.07, 0.23, 0.0, 0.0, 0.0)
        request = MidReturnBookRequest(
            book=BookIdentity("mid-return-book-1"),
            target_height_mm=800.0,
            left_neighbor_spine_template=SpineTemplate(Path("left-neighbor.jpeg")),
            source_slot=BookStandSlot.SLOT_1,
            lift_speed_level=2,
            injected_grasp_pose=injected,
        )
        arm = FakeArm(unreachable_labels={"insert"})
        use_case = self.build(arm)

        task = self.run_case(use_case, request)

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.result.stages[-1].name, "reachability_check")
        self.assertIn("arm pose unreachable (insert)", task.result.error)
        # Only the stand-pickup move_l pair ran.
        self.assertEqual(len(self._move_l_calls(arm)), 2)
        self.assertEqual([call[1] for call in self._checks(arm)], ["pre_insert", "insert"])


class MidReturnEmptyGraspTests(unittest.TestCase):
    """A monitored close that times out met no book; carrying on is unsafe."""

    def setUp(self) -> None:
        self.plan = MidReturnMotionPlan()
        self.pre_grasp = BasePose(-0.1, -0.3, 0.3, 1.5, 0.0, 3.0)
        self.grasp = BasePose(-0.1, -0.45, 0.25, 1.5, 0.0, 3.0)
        self.pickup = BookStandPickupPlan(self.pre_grasp, self.grasp, 4.0, 2.0, 2.5)

    def build(self, arm):
        manager = TaskManager()
        use_case = MidReturnBookUseCase(
            task_manager=manager,
            reservation=FakeReservation(),
            lift=FakeLift(),
            vision=FakeVision(),
            arm=arm,
            rod=FakeRod(),
            pickup_slots={1: self.pickup},
            plan=self.plan,
        )
        return use_case

    def run_case(self, use_case, request):
        task = use_case.create(request)
        use_case.execute(task, request)
        return task

    def close_calls(self, arm):
        return [call for call in arm.calls if call[0] == "close_gripper_monitored"]

    @patch("bookarm.domain.policies.gripper.GRASP_CONTACT_CHECK_ENABLED", True)
    def test_empty_grasp_of_the_neighbor_book_requires_recovery(self):
        arm = FakeArm(close_reasons=["timeout"])
        use_case = self.build(arm)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(task.result.stages[-1].name, "book_grasp")
        self.assertIn(
            "gripper closed without contact (empty slot or misalignment)",
            task.result.error,
        )
        self.assertEqual(len(self.close_calls(arm)), 1)

    @patch("bookarm.domain.policies.gripper.GRASP_CONTACT_CHECK_ENABLED", True)
    def test_empty_regrip_after_the_gap_spread_requires_recovery(self):
        arm = FakeArm(close_reasons=["object_contact", "timeout"])
        use_case = self.build(arm)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(task.result.stages[-1].name, "book_put_back")
        self.assertIn(
            "gripper closed without contact (book lost between the put-back release and re-grip)",
            task.result.error,
        )
        # The stand-slot pickup close never ran after the lost book.
        self.assertEqual(len(self.close_calls(arm)), 2)

    @patch("bookarm.domain.policies.gripper.GRASP_CONTACT_CHECK_ENABLED", True)
    def test_empty_stand_pickup_requires_recovery(self):
        arm = FakeArm(close_reasons=["object_contact", "object_contact", "timeout"])
        use_case = self.build(arm)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(task.result.stages[-1].name, "book_stand_pickup")
        self.assertIn(
            "gripper closed without contact (empty slot or misalignment)",
            task.result.error,
        )
        # The arm never retreated from the slot with an empty gripper.
        move_l_after_close = arm.calls.index(self.close_calls(arm)[-1])
        self.assertNotIn(
            ("move_l", self.pre_grasp, 2.5),
            arm.calls[move_l_after_close:],
        )


class MidReturnSubStepCancellationTests(unittest.TestCase):
    """Cancel/interrupt checkpoints inside the grasp and put-back chains."""

    def setUp(self) -> None:
        self.plan = MidReturnMotionPlan()
        self.pre_grasp = BasePose(-0.1, -0.3, 0.3, 1.5, 0.0, 3.0)
        self.grasp = BasePose(-0.1, -0.45, 0.25, 1.5, 0.0, 3.0)
        self.pickup = BookStandPickupPlan(self.pre_grasp, self.grasp, 4.0, 2.0, 2.5)
        self.manager = TaskManager()

    def build(self, arm):
        return MidReturnBookUseCase(
            task_manager=self.manager,
            reservation=FakeReservation(),
            lift=FakeLift(),
            vision=FakeVision(),
            arm=arm,
            rod=FakeRod(),
            pickup_slots={1: self.pickup},
            plan=self.plan,
        )

    def test_cancel_during_put_back_leaves_the_rod_home(self):
        holder = {}

        def on_move_l():
            # move_l #1: grasp approach; #2: put-back push-in; #3: re-push.
            holder["count"] = holder.get("count", 0) + 1
            if holder["count"] == 3:
                self.manager.cancel(holder["task_id"])

        arm = FakeArm(move_l_hook=on_move_l)
        use_case = self.build(arm)
        request = make_request()
        task = use_case.create(request)
        holder["task_id"] = task.id.value

        use_case.execute(task, request)

        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertEqual(task.result.error, "workflow cancelled")
        self.assertEqual(task.result.stages[-1].name, "book_put_back")
        # Rod: home + hook from the grasp chain; the put-back move never ran.
        self.assertEqual(len(use_case.rod.calls), 2)

    def test_interrupt_during_grasp_chain_reports_the_safety_stop(self):
        holder = {}

        def on_move_l():
            holder["count"] = holder.get("count", 0) + 1
            if holder["count"] == 1:  # grasp-chain approach
                self.manager.interrupt(holder["task_id"])

        arm = FakeArm(move_l_hook=on_move_l)
        use_case = self.build(arm)
        request = make_request()
        task = use_case.create(request)
        holder["task_id"] = task.id.value

        use_case.execute(task, request)

        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertEqual(task.result.error, "workflow interrupted by safety stop")
        self.assertEqual(task.result.stages[-1].name, "book_grasp")
        # Only the rod home ran; the hook move never followed the interrupt.
        self.assertEqual(len(use_case.rod.calls), 1)


if __name__ == "__main__":
    unittest.main()

