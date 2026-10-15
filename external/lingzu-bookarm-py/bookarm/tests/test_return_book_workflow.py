"""Return-book workflow tests using only in-memory device fakes."""

from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from bookarm.api.requests import ReturnBookRequest, ReturnBookStage
from bookarm.application.task_manager import TaskManager
from bookarm.application.use_cases.return_book import ReturnBookUseCase
from bookarm.domain.models.book import BookIdentity, BookTarget, SpineTemplate
from bookarm.domain.models.lift import LiftPositionResult
from bookarm.domain.models.pose import BasePose, JointPose
from bookarm.domain.models.shelf import BookStandPickupPlan, BookStandSlot
from bookarm.domain.models.task import TaskState
from bookarm.domain.plans.return_book import ReturnBookMotionPlan


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
    """Synchronous spine locator returning one fixed base-frame target book."""

    def __init__(self, fail=False) -> None:
        self.fail = fail
        self.calls = []
        self.target = BookTarget(
            raw_pose=BasePose(-0.35, -0.07, 0.23, 0.0, 0.0, 0.0),
            grasp_pose=BasePose(-0.35, -0.07, 0.23, 0.0, 0.0, 0.0),
            pixel_uv=(320, 240),
            confidence=0.9,
            capture_id="capture-return-1",
        )

    def locate_book(self, template):
        self.calls.append(template)
        if self.fail:
            raise RuntimeError("simulated spine detection failure")
        return self.target


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
    "vision_retarget",
    "post_vision_align",
    "book_stand_pickup",
    "insert_target",
    "reachability_check",
    "shelf_insert",
]


def make_request(
    *,
    slot=BookStandSlot.SLOT_1,
    stop_after=ReturnBookStage.SHELF_INSERT,
    speed_level=2,
):
    return ReturnBookRequest(
        book=BookIdentity("return-book-1"),
        target_height_mm=800.0,
        left_neighbor_spine_template=SpineTemplate(Path("left-neighbor.jpeg")),
        source_slot=slot,
        lift_speed_level=speed_level,
        stop_after_stage=stop_after,
    )


class ReturnBookWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = ReturnBookMotionPlan()
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
        use_case = ReturnBookUseCase(
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

    def test_runs_all_seven_stages_in_order(self):
        use_case, arm, vision, lift, reservation = self.build()

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual([stage.name for stage in task.result.stages], ALL_STAGES)
        self.assertEqual(lift.commands, [(800.0, 2)])
        # The reference book is located once, synchronously, before the pickup.
        self.assertEqual(vision.calls, [make_request().left_neighbor_spine_template])
        self.assertTrue(reservation.released)
        # Slot pickup entered and left through the collision-checked pre_grasp.
        self.assertIn(("move_to_pose_by_move_j", self.pre_grasp, 4.0), arm.calls)
        self.assertIn(("move_l", self.grasp, 2.0), arm.calls)
        self.assertIn(("move_l", self.pre_grasp, 2.5), arm.calls)
        self.assertEqual(
            arm.calls[-1], ("move_j", self.plan.debug_joint, self.plan.debug_duration_s)
        )
        # The rod is never touched by the return flow.
        self.assertEqual(self.rod.calls, [])

    def test_insert_target_computes_the_chain_with_z_clamp_and_searched_x(self):
        use_case, arm, vision, _lift, _reservation = self.build()

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        # The recognition details surface on the vision stage…
        vision_stage = next(stage for stage in task.result.stages if stage.name == "vision_retarget")
        self.assertEqual(
            vision_stage.details["insert_book_target_grasp_base_pose"],
            vision.target.grasp_pose.values(),
        )
        self.assertEqual(vision_stage.details["capture_id"], "capture-return-1")
        # …and the insert stage carries the computed target: Y1 = target
        # grasp y (-0.07) + default 4 cm offset = -0.03; z (0.23) is below
        # the plan's reachability floor and is clamped to 0.25.
        insert_stage = next(stage for stage in task.result.stages if stage.name == "insert_target")
        self.assertAlmostEqual(insert_stage.details["y1_m"], -0.03)
        self.assertAlmostEqual(insert_stage.details["target_y_offset_cm"], 4.0)
        self.assertAlmostEqual(insert_stage.details["min_insert_z_m"], 0.25)
        self.assertIs(insert_stage.details["insert_z_clamped"], True)
        self.assertAlmostEqual(insert_stage.details["insert_target_base_pose"][2], 0.25)
        # The pre-insert x is IK-searched below the plan's -0.20 m cap (the
        # fake arm reaches everything, so the cap itself wins) and reported
        # on the stage.
        self.assertAlmostEqual(insert_stage.details["pre_insert_x_m"], -0.20)
        # Pre-insert flies MovePByJ to the searched x with y/z chained off
        # the insert pose; the final insert is a short linear move.
        # Push x-5; retract x+10 after release.
        pose_moves = [
            call
            for call in arm.calls
            if isinstance(call, tuple) and call[0] == "move_to_pose_by_move_j"
        ]
        pre_insert = pose_moves[-1][1]
        self.assertAlmostEqual(pre_insert.x_m, -0.20)
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
        release = [call for call in arm.calls if call[0] == "open_gripper"][-1]
        self.assertEqual(release[1], 50.0)
        self.assertEqual(release[2].open_deg, 50.0)
        self.assertEqual(
            arm.calls[-1], ("move_j", self.plan.debug_joint, self.plan.debug_duration_s)
        )

    def test_insert_target_honours_plan_y_offset(self):
        use_case, arm, _vision, _lift, _reservation = self.build()
        use_case.plan = replace(self.plan, target_y_offset_cm=7.5)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        insert_stage = next(stage for stage in task.result.stages if stage.name == "insert_target")
        self.assertAlmostEqual(insert_stage.details["y1_m"], 0.005)
        move_l_poses = [call[1] for call in arm.calls if call[0] == "move_l"]
        self.assertAlmostEqual(move_l_poses[-3].y_m, 0.005)

    def test_hot_swapped_plan_applies_to_the_next_run(self):
        use_case, arm, _vision, _lift, _reservation = self.build()
        use_case.plan = replace(self.plan, push_x_offset_cm=-8.0)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        move_l_poses = [call[1] for call in arm.calls if call[0] == "move_l"]
        push = move_l_poses[-2]
        self.assertAlmostEqual(push.x_m, -0.43)

    def test_push_applies_plan_z_offset(self):
        # 推回书架除 x 偏移外支持 z 偏移（末尾还书专用）；回抽以推回位为基准，
        # 跟随同一个 z。
        use_case, arm, _vision, _lift, _reservation = self.build()
        use_case.plan = replace(self.plan, push_z_offset_cm=-2.0)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        move_l_poses = [call[1] for call in arm.calls if call[0] == "move_l"]
        insert, push, retract = move_l_poses[-3:]
        self.assertAlmostEqual(push.z_m, insert.z_m - 0.02)
        self.assertAlmostEqual(retract.z_m, push.z_m)

    def test_push_applies_plan_rpy_offset(self):
        # 推回书架支持姿态偏移（末尾还书专用，相对插入姿态）；回抽位置仍以
        # 推回位为基准，但姿态固定为 retract_rpy_deg，不随推回姿态变化。
        use_case, arm, _vision, _lift, _reservation = self.build()
        use_case.plan = replace(self.plan, push_rpy_deg=(5.0, -10.0, 2.0))

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        move_l_poses = [call[1] for call in arm.calls if call[0] == "move_l"]
        insert, push, retract = move_l_poses[-3:]
        self.assertAlmostEqual(push.rx_rad, insert.rx_rad + math.radians(5.0))
        self.assertAlmostEqual(push.ry_rad, insert.ry_rad - math.radians(10.0))
        self.assertAlmostEqual(push.rz_rad, insert.rz_rad + math.radians(2.0))
        self.assertAlmostEqual(retract.x_m, push.x_m + 0.10)
        self.assertAlmostEqual(retract.rx_rad, math.radians(90.0))
        self.assertAlmostEqual(retract.ry_rad, 0.0)
        self.assertAlmostEqual(retract.rz_rad, math.radians(90.0))

    def test_retract_attitude_is_fixed_regardless_of_insert_and_push(self):
        # 回抽姿态恒为 retract_rpy_deg=(90,0,90)：即使 insert_rpy_deg 与
        # push_rpy_deg 都取非默认值也不受影响。
        use_case, arm, _vision, _lift, _reservation = self.build()
        use_case.plan = replace(
            self.plan,
            insert_rpy_deg=(80.0, 5.0, 70.0),
            push_rpy_deg=(5.0, -10.0, 2.0),
        )

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        move_l_poses = [call[1] for call in arm.calls if call[0] == "move_l"]
        _insert, push, retract = move_l_poses[-3:]
        self.assertAlmostEqual(retract.x_m, push.x_m + 0.10)
        self.assertAlmostEqual(retract.y_m, push.y_m)
        self.assertAlmostEqual(retract.z_m, push.z_m)
        self.assertAlmostEqual(retract.rx_rad, math.radians(90.0))
        self.assertAlmostEqual(retract.ry_rad, 0.0)
        self.assertAlmostEqual(retract.rz_rad, math.radians(90.0))

    def test_push_defaults_to_the_insert_attitude(self):
        # 缺省 push_rpy_deg=(0,0,0) 时推回位姿态与插入位一致（旧 profile 行为不变）。
        use_case, arm, _vision, _lift, _reservation = self.build()

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        move_l_poses = [call[1] for call in arm.calls if call[0] == "move_l"]
        insert, push, _retract = move_l_poses[-3:]
        self.assertAlmostEqual(push.rx_rad, insert.rx_rad)
        self.assertAlmostEqual(push.ry_rad, insert.ry_rad)
        self.assertAlmostEqual(push.rz_rad, insert.rz_rad)

    def test_vision_failure_fails_before_the_pickup_without_recovery(self):
        use_case, arm, _vision, _lift, reservation = self.build(vision=FakeVision(fail=True))

        task = self.run_case(use_case, make_request())

        # Arm parked at debug, no book held: a plain failure, no recovery.
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(
            [stage.name for stage in task.result.stages],
            ["lift_move", "arm_debug_pose", "vision_retarget"],
        )
        self.assertIn("simulated spine detection failure", task.result.error)
        self.assertNotIn(("move_to_pose_by_move_j", self.pre_grasp, 4.0), arm.calls)
        self.assertTrue(reservation.released)

    def test_unreachable_x_search_fails_at_insert_target_with_recovery(self):
        arm = FakeArm(enabled=True, unreachable_labels={"pre_insert_x_search"})
        use_case, arm, _vision, _lift, reservation = self.build(arm=arm)

        task = self.run_case(use_case, make_request())

        # The pickup completed, so the arm holds the book: recovery required.
        self.assertEqual(task.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(task.result.stages[-1].name, "insert_target")
        self.assertIn("预插入位无可达 x", task.result.error)
        self.assertIn(("move_l", self.pre_grasp, 2.5), arm.calls)
        self.assertTrue(reservation.released)

    def test_arm_debug_stage_exactly_uses_enable_zero_debug_sequence(self):
        use_case, arm, _vision, _lift, _reservation = self.build(slots={})

        task = self.run_case(
            use_case,
            make_request(stop_after=ReturnBookStage.ARM_DEBUG_POSE),
        )

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(
            arm.calls,
            [
                "is_enabled",
                "enable",
                ("move_j", self.plan.zero_joint, self.plan.zero_duration_s),
                ("move_j", self.plan.debug_joint, self.plan.debug_duration_s),
            ],
        )
        self.assertEqual(self.rod.calls, [])

    def test_stop_after_lift_does_not_require_slot_or_start_arm(self):
        use_case, arm, _vision, lift, reservation = self.build(slots={})

        task = self.run_case(
            use_case,
            make_request(
                slot=BookStandSlot.SLOT_4,
                stop_after=ReturnBookStage.LIFT_MOVE,
                speed_level=3,
            ),
        )

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(lift.commands, [(800.0, 3)])
        self.assertEqual(arm.calls, [])
        self.assertTrue(reservation.released)

    def test_slot_four_is_rejected_before_any_hardware_command(self):
        use_case, arm, vision, lift, reservation = self.build(slots={})

        task = self.run_case(use_case, make_request(slot=BookStandSlot.SLOT_4))

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertIn("slot 4 is not calibrated", task.result.error)
        self.assertEqual(lift.commands, [])
        self.assertEqual(arm.calls, [])
        self.assertEqual(vision.calls, [])
        self.assertFalse(reservation.released)

    @patch("bookarm.domain.policies.gripper.GRASP_CONTACT_CHECK_ENABLED", True)
    def test_empty_stand_pickup_requires_recovery_before_any_retreat(self):
        use_case, arm, _vision, _lift, reservation = self.build(
            arm=FakeArm(enabled=True, close_reasons=["timeout"])
        )

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(task.result.stages[-1].name, "book_stand_pickup")
        self.assertIn(
            "gripper closed without contact (empty slot or misalignment)",
            task.result.error,
        )
        # Only the drive onto the slot ran; the retreat never followed an
        # empty close.
        move_l_calls = [call for call in arm.calls if call[0] == "move_l"]
        self.assertEqual(move_l_calls, [("move_l", self.grasp, 2.0)])
        self.assertTrue(reservation.released)

    def test_stand_pickup_reaching_its_target_counts_as_a_pickup(self):
        use_case, arm, _vision, _lift, _reservation = self.build(
            arm=FakeArm(enabled=True, close_reasons=["target_reached"])
        )

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)

    def test_reservation_conflict_fails_before_any_motion(self):
        use_case, arm, vision, lift, _reservation = self.build(
            reservation=FakeReservation(available=False)
        )

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertIn("reserved", task.result.error)
        self.assertEqual(lift.commands, [])
        self.assertEqual(arm.calls, [])
        self.assertEqual(vision.calls, [])

    def test_cancel_before_the_stand_close_skips_the_close(self):
        manager = TaskManager()
        holder = {}

        def on_move_l():
            # move_l #1 drives onto the slot; the close is the next sub-step.
            holder["count"] = holder.get("count", 0) + 1
            if holder["count"] == 1:
                manager.cancel(holder["task_id"])

        arm = FakeArm(enabled=True, move_l_hook=on_move_l)
        use_case = ReturnBookUseCase(
            task_manager=manager,
            reservation=FakeReservation(),
            lift=FakeLift(),
            vision=FakeVision(),
            arm=arm,
            rod=FakeRod(),
            pickup_slots={1: self.pickup},
            plan=self.plan,
        )
        request = make_request()
        task = use_case.create(request)
        holder["task_id"] = task.id.value

        use_case.execute(task, request)

        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertEqual(task.result.error, "workflow cancelled")
        self.assertEqual(task.result.stages[-1].name, "book_stand_pickup")
        closes = [call for call in arm.calls if call[0] == "close_gripper_monitored"]
        self.assertEqual(closes, [])

    def test_cancel_during_shelf_insert_skips_the_release(self):
        manager = TaskManager()
        holder = {}

        def on_move_l():
            # move_l #1/#2: slot grasp and retreat; #3: the insert approach.
            holder["count"] = holder.get("count", 0) + 1
            if holder["count"] == 3:
                manager.cancel(holder["task_id"])

        arm = FakeArm(enabled=True, move_l_hook=on_move_l)
        use_case = ReturnBookUseCase(
            task_manager=manager,
            reservation=FakeReservation(),
            lift=FakeLift(),
            vision=FakeVision(),
            arm=arm,
            rod=FakeRod(),
            pickup_slots={1: self.pickup},
            plan=self.plan,
        )
        request = make_request()
        task = use_case.create(request)
        holder["task_id"] = task.id.value

        use_case.execute(task, request)

        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertEqual(task.result.error, "workflow cancelled")
        self.assertEqual(task.result.stages[-1].name, "shelf_insert")
        # The only open is the pickup open; the release never ran.
        opens = [call[1] for call in arm.calls if call[0] == "open_gripper"]
        self.assertEqual(opens, [50.0])


class ReturnBookReachabilityTests(unittest.TestCase):
    """Fail-fast insert-chain reachability precheck after the re-target."""

    def setUp(self) -> None:
        self.plan = ReturnBookMotionPlan()
        self.pre_grasp = BasePose(-0.1, -0.3, 0.3, 1.5, 0.0, 3.0)
        self.grasp = BasePose(-0.1, -0.45, 0.25, 1.5, 0.0, 3.0)
        self.pickup = BookStandPickupPlan(self.pre_grasp, self.grasp, 4.0, 2.0, 2.5)

    def build(self, arm):
        manager = TaskManager()
        return ReturnBookUseCase(
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
    def _pose_moves(arm):
        return [call for call in arm.calls if isinstance(call, tuple) and call[0] == "move_to_pose_by_move_j"]

    @staticmethod
    def _move_l_calls(arm):
        return [call for call in arm.calls if isinstance(call, tuple) and call[0] == "move_l"]

    def test_all_reachable_preserves_the_execution_order(self):
        arm = FakeArm(enabled=True)
        use_case = self.build(arm)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        checks = self._checks(arm)
        # One search probe at insert_target (fake arm: first candidate wins),
        # then the four-pose preflight at reachability_check; shelf_insert
        # reuses the cached poses and probes nothing again.
        self.assertEqual(
            [call[1] for call in checks],
            ["pre_insert_x_search", "pre_insert", "insert", "push", "retract"],
        )
        # Seeded from the debug pose (the stand pickup parks the arm there);
        # the search probe uses the same seed, then each solved pose seeds
        # the next one.
        self.assertEqual(checks[0][3], self.plan.debug_joint)
        self.assertEqual(checks[1][3], self.plan.debug_joint)
        for call in checks[2:]:
            self.assertEqual(call[3], arm.check_solution)
        # Probing never moved the arm: the probed poses are exactly the
        # executed ones and every check precedes the insert chain.
        pre_insert_call = self._pose_moves(arm)[-1]
        insert_call, push_call, retract_call = self._move_l_calls(arm)[-3:]
        self.assertEqual(checks[0][2], pre_insert_call[1])
        self.assertEqual(checks[1][2], pre_insert_call[1])
        self.assertEqual(checks[2][2], insert_call[1])
        self.assertEqual(checks[3][2], push_call[1])
        self.assertEqual(checks[4][2], retract_call[1])
        self.assertLess(arm.calls.index(checks[-1]), arm.calls.index(pre_insert_call))
        self.assertEqual(
            task.result.payload["reachability_checked_poses"],
            ["pre_insert", "insert", "push", "retract"],
        )

    def test_unreachable_pose_fails_before_the_insert_chain(self):
        arm = FakeArm(enabled=True, unreachable_labels={"push"})
        use_case = self.build(arm)

        task = self.run_case(use_case, make_request())

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.result.stages[-1].name, "reachability_check")
        self.assertIn("arm pose unreachable (push)", task.result.error)
        self.assertIn("IK did not converge", task.result.error)
        # The x search passed (fake arm reaches every candidate); the
        # preflight then failed at push and the insert chain never started.
        self.assertEqual(
            [call[1] for call in self._checks(arm)],
            ["pre_insert_x_search", "pre_insert", "insert", "push"],
        )
        # Only the stand-pickup motions ran; the insert chain never started.
        self.assertEqual(self._pose_moves(arm), [("move_to_pose_by_move_j", self.pre_grasp, 4.0)])
        self.assertEqual(
            self._move_l_calls(arm),
            [("move_l", self.grasp, 2.0), ("move_l", self.pre_grasp, 2.5)],
        )
        self.assertEqual(arm.calls[-1][0], "check_pose_reachable")


if __name__ == "__main__":
    unittest.main()
