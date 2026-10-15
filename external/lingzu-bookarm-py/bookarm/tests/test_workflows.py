"""Workflow tests using fakes; no GUI, camera, or robot hardware is required."""

from __future__ import annotations

import math
import unittest
from pathlib import Path
from unittest.mock import patch

from bookarm.api.requests import ReturnBookToEndRequest, TakeBookRequest, TakeBookStage
from bookarm.api.sdk import BookArmSDK
from bookarm.application.task_manager import TaskManager
from bookarm.application.use_cases.return_book import ReturnBookUseCase
from bookarm.application.use_cases.return_book_to_end import ReturnBookToEndUseCase
from bookarm.application.use_cases.take_book import TakeBookUseCase
from bookarm.application.workflow_engine import WorkflowEngine
from bookarm.domain.models.book import BookIdentity, BookTarget, SpineTemplate
from bookarm.domain.models.lift import LiftPositionResult
from bookarm.domain.models.pose import BasePose, JointPose
from bookarm.domain.models.shelf import BookStandSlot, ShelfLocation, SlotPlacementPlan
from bookarm.domain.plans.take_book import GripperCommand, TakeBookMotionPlan
from bookarm.domain.models.task import StageStatus, TaskState, WorkflowKind


class FakeReservation:
    def __init__(self, available=True):
        self.available = available
        self.released = False

    def acquire(self, purpose):
        self.purpose = purpose
        return self.available

    def release(self):
        self.released = True


class FakeLift:
    def __init__(self, measured=None):
        self.measured = measured
        self.commands = []

    def move_to_height_mm(self, target_height_mm, *, speed_level):
        self.commands.append((float(target_height_mm), int(speed_level)))
        if self.measured is None:
            self.measured = float(target_height_mm)
        if float(target_height_mm) != float(self.measured):
            raise RuntimeError("[timeout] lift failed to reach %.3f mm; last measured %.3f mm" % (
                float(target_height_mm), float(self.measured),
            ))
        return LiftPositionResult(
            target_height_mm=float(target_height_mm),
            measured_height_mm=float(self.measured),
            tolerance_pulses=10,
            controller_target_pulses=100,
            controller_measured_pulses=100,
        )


class FakeVision:
    def locate_book(self, template):
        self.template = template
        return BookTarget(
            raw_pose=BasePose(0.2, 0.1, 0.3, 0.0, 0.0, 0.0),
            grasp_pose=BasePose(0.21, 0.1, 0.3, 0.0, 0.0, 0.0),
            pixel_uv=(12, 34),
            confidence=0.95,
            capture_id="capture-1",
        )


class FakeArm:
    def __init__(
        self,
        fail_at=None,
        enabled=False,
        joints=None,
        update_after_move=True,
        close_reason="object_contact",
        move_l_hook=None,
        unreachable_labels=(),
        check_solution=None,
    ):
        self.joints = joints or JointPose.from_values((0, 0, 0, 0, 0, 0))
        self.calls = []
        self.fail_at = fail_at
        self.enabled = enabled
        self.update_after_move = update_after_move
        self.close_reason = close_reason
        self.move_l_hook = move_l_hook
        self.unreachable_labels = set(unreachable_labels)
        # Fixed solution echoed by the reachability probe so tests can verify
        # that one check's solution seeds the next.
        self.check_solution = check_solution or JointPose.from_values((0.11, 0.12, 0.13, 0.14, 0.15, 0.16))

    def is_enabled(self):
        self.calls.append("is_enabled")
        return self.enabled

    def enable(self):
        self.calls.append("enable")
        self.enabled = True

    def move_j(self, target, duration_s):
        if self.fail_at == "move_j":
            raise RuntimeError("simulated arm joint motion failure")
        if self.update_after_move:
            self.joints = target
        self.calls.append(("move_j", target, duration_s))

    def read_joint_pose(self):
        return self.joints

    def disable_at_safe_pose(self, duration_s=3.0):
        self.calls.append(("disable_at_safe_pose", float(duration_s)))
        self.enabled = False
        return {"disable_command_sent": True}

    def move_to_pose_by_move_j(self, target, duration_s):
        if self.fail_at == "move_to_pose_by_move_j":
            raise RuntimeError("simulated arm MovePByJ failure")
        self.calls.append(("move_to_pose_by_move_j", target, duration_s))

    def move_l(self, target, duration_s):
        if self.fail_at == "move_l":
            raise RuntimeError("simulated arm motion failure")
        self.calls.append(("move_l", target, duration_s))
        if self.move_l_hook is not None:
            self.move_l_hook()

    def check_pose_reachable(self, target, seed=None, *, label):
        self.calls.append(("check_pose_reachable", label, target, seed))
        if label in self.unreachable_labels:
            raise RuntimeError("arm pose unreachable (%s): IK did not converge" % label)
        return self.check_solution

    def open_gripper(self, target_deg, command=None):
        self.calls.append(("open_gripper", float(target_deg), command))

    def close_gripper_monitored(self, command):
        self.calls.append("close_gripper")
        return {"reason": self.close_reason}


class FakeRod:
    def __init__(self):
        self.calls = []

    def move_to_deg(self, target_deg, speed, acceleration, tolerance_deg, timeout_s):
        self.calls.append(float(target_deg))


def _slots():
    pose = BasePose(0.4, 0.0, 0.2, 0.0, 0.0, 0.0)
    plan = SlotPlacementPlan(pose, pose, pose, pose)
    return {1: plan, 2: plan, 3: plan, 4: plan}


def _request(slot=BookStandSlot.SLOT_1, speed_level=1, stop_after=TakeBookStage.ARM_STANDBY):
    return TakeBookRequest(
        book=BookIdentity("book-42"),
        target_height_mm=123.0,
        spine_template=SpineTemplate(Path("book.png")),
        destination_slot=slot,
        lift_speed_level=speed_level,
        stop_after_stage=stop_after,
    )


def _sdk(lift=None, slots=None, arm=None, plan=None, grasp_step_approval=None, grasp_step_reporter=None):
    manager = TaskManager()
    reservation = FakeReservation()
    arm = arm or FakeArm()
    take = TakeBookUseCase(
        task_manager=manager,
        reservation=reservation,
        lift=lift or FakeLift(),
        vision=FakeVision(),
        arm=arm,
        rod=FakeRod(),
        slots=_slots() if slots is None else slots,
        plan=plan,
        grasp_step_approval=grasp_step_approval,
        grasp_step_reporter=grasp_step_reporter,
    )
    return BookArmSDK(
        task_manager=manager,
        take_book=take,
        return_book=ReturnBookUseCase(
            task_manager=manager,
            reservation=reservation,
            lift=lift or FakeLift(),
            vision=FakeVision(),
            arm=arm,
            rod=FakeRod(),
            pickup_slots={},
        ),
        return_book_to_end=ReturnBookToEndUseCase(manager),
    ), arm, reservation


class TakeBookWorkflowTests(unittest.TestCase):
    def test_successfully_runs_the_seven_required_stages(self):
        sdk, arm, reservation = _sdk()

        task = sdk.take_book(_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(
            [stage.name for stage in task.result.stages],
            [
                "lift_move",
                "vision_target",
                "post_vision_align",
                "arm_debug_pose",
                "reachability_check",
                "book_grasp",
                "book_stand_place",
                "arm_standby",
            ],
        )
        self.assertTrue(reservation.released)
        self.assertIn("close_gripper", arm.calls)
        self.assertEqual(task.result.payload["lift_speed_level"], 1)
        self.assertFalse(task.result.payload["arm_was_enabled"])
        self.assertTrue(task.result.payload["arm_moved_to_zero"])
        self.assertTrue(task.result.payload["arm_moved_to_debug_pose"])
        self.assertEqual(sdk.wait(task.id).state, TaskState.SUCCEEDED)

    def test_debug_pose_stage_skips_motion_when_enabled_arm_is_already_at_debug_pose(self):
        debug_joint = TakeBookMotionPlan().debug_joint
        arm = FakeArm(enabled=True, joints=debug_joint)
        sdk, arm, _reservation = _sdk(arm=arm, slots={})

        task = sdk.take_book(_request(stop_after=TakeBookStage.ARM_DEBUG_POSE))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual([stage.name for stage in task.result.stages], ["lift_move", "vision_target", "post_vision_align", "arm_debug_pose"])
        self.assertEqual(arm.calls, ["is_enabled"])
        self.assertTrue(task.result.payload["arm_was_enabled"])
        self.assertFalse(task.result.payload["arm_moved_to_zero"])
        self.assertFalse(task.result.payload["arm_moved_to_debug_pose"])

    def test_debug_pose_stage_moves_enabled_arm_directly_to_debug_pose(self):
        plan = TakeBookMotionPlan()
        arm = FakeArm(enabled=True)
        sdk, arm, _reservation = _sdk(arm=arm, slots={})

        task = sdk.take_book(_request(stop_after=TakeBookStage.ARM_DEBUG_POSE))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(
            arm.calls,
            ["is_enabled", ("move_j", plan.debug_joint, plan.debug_duration_s)],
        )
        self.assertTrue(task.result.payload["arm_was_enabled"])
        self.assertFalse(task.result.payload["arm_moved_to_zero"])
        self.assertTrue(task.result.payload["arm_moved_to_debug_pose"])

    def test_debug_pose_stage_enables_disabled_arm_then_visits_zero_and_debug_pose(self):
        plan = TakeBookMotionPlan()
        arm = FakeArm(enabled=False)
        sdk, arm, _reservation = _sdk(arm=arm, slots={})

        task = sdk.take_book(_request(stop_after=TakeBookStage.ARM_DEBUG_POSE))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(
            arm.calls,
            [
                "is_enabled",
                "enable",
                ("move_j", plan.zero_joint, plan.zero_duration_s),
                ("move_j", plan.debug_joint, plan.debug_duration_s),
            ],
        )
        self.assertFalse(task.result.payload["arm_was_enabled"])
        self.assertTrue(task.result.payload["arm_moved_to_zero"])
        self.assertTrue(task.result.payload["arm_moved_to_debug_pose"])

    def test_debug_pose_failure_blocks_book_grasp_and_requires_recovery(self):
        arm = FakeArm(enabled=True, update_after_move=False)
        sdk, arm, reservation = _sdk(arm=arm)

        task = sdk.take_book(_request())

        self.assertEqual(task.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(task.result.stages[-1].name, "arm_debug_pose")
        self.assertNotIn("open_gripper", arm.calls)
        self.assertTrue(reservation.released)

    def test_lift_feedback_timeout_fails_at_lift_stage(self):
        sdk, _arm, reservation = _sdk(lift=FakeLift(measured=0.0))

        task = sdk.take_book(_request())

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.result.stages[-1].name, "lift_move")
        self.assertIn("failed to reach", task.result.error)
        self.assertTrue(reservation.released)

    def test_lift_stage_passes_requested_height_and_speed_level(self):
        lift = FakeLift()
        sdk, _arm, _reservation = _sdk(lift=lift)

        task = sdk.take_book(_request(speed_level=3))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(lift.commands, [(123.0, 3)])

    def test_stop_after_lift_runs_no_other_stage_or_destination_validation(self):
        lift = FakeLift()
        sdk, arm, reservation = _sdk(lift=lift, slots={})

        task = sdk.take_book(_request(stop_after=TakeBookStage.LIFT_MOVE))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual([stage.name for stage in task.result.stages], ["lift_move"])
        self.assertEqual(lift.commands, [(123.0, 1)])
        self.assertEqual(arm.calls, [])
        self.assertTrue(reservation.released)

    def test_missing_slot_is_rejected_before_any_motion(self):
        sdk, arm, reservation = _sdk(slots={1: _slots()[1]})

        task = sdk.take_book(_request(slot=BookStandSlot.SLOT_4))

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual([stage.name for stage in task.result.stages], ["book_stand_place"])
        self.assertEqual(arm.calls, [])
        self.assertFalse(reservation.released)

    def test_book_stand_place_returns_to_pre_place_then_debug_pose(self):
        pre_place = BasePose(0.3, -0.2, 0.4, 1.0, 0.0, 1.5)
        place = BasePose(0.2, -0.3, 0.25, 1.2, 0.0, 1.5)
        lower_place = BasePose(0.2, -0.3, 0.23, 1.2, 0.0, 1.5)
        distinct_retreat = BasePose(0.9, 0.8, 0.7, 0.0, 0.0, 0.0)
        placement = SlotPlacementPlan(pre_place, place, lower_place, distinct_retreat, 3.0, 2.0, 1.5, 2.5)
        plan = TakeBookMotionPlan(
            debug_joint=JointPose.from_values((0.1, 0.2, -0.3, 0.4, 0.5, -0.6))
        )
        sdk, arm, _reservation = _sdk(
            arm=FakeArm(enabled=True),
            slots={1: placement},
            plan=plan,
        )

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_STAND_PLACE))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        move_l_calls = [call for call in arm.calls if isinstance(call, tuple) and call[0] == "move_l"]
        self.assertEqual(
            move_l_calls[-3:],
            [("move_l", place, 2.0), ("move_l", lower_place, 1.5), ("move_l", pre_place, 2.5)],
        )
        self.assertNotIn(("move_l", distinct_retreat, 2.5), arm.calls)
        self.assertIn(("open_gripper", 50.0, GripperCommand(open_deg=50.0)), arm.calls)
        self.assertIn(("move_j", plan.debug_joint, plan.debug_duration_s), arm.calls)
        self.assertTrue(task.result.payload["returned_to_debug_pose"])

    def test_grasp_motion_failure_requires_operator_recovery(self):
        sdk, _arm, reservation = _sdk(arm=FakeArm(fail_at="move_l"))

        task = sdk.take_book(_request())

        self.assertEqual(task.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(task.result.stages[-1].name, "book_grasp")
        self.assertTrue(reservation.released)

    def test_book_grasp_step_mode_confirms_each_internal_action_in_order(self):
        approvals = []
        expected_steps = [
            "open_gripper",
            "rod_home",
            "arm_pregrasp",
            "arm_approach",
            "rod_grasp",
            "arm_fine_grasp",
            "close_gripper",
            "arm_post_grasp",
            "arm_return_debug",
        ]
        sdk, arm, _reservation = _sdk(
            arm=FakeArm(enabled=True),
            slots={},
            grasp_step_approval=lambda step: approvals.append(step) is None or True,
        )

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_GRASP))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(approvals, expected_steps)
        self.assertEqual(task.result.payload["grasp_steps_completed"], expected_steps)
        self.assertTrue(task.result.payload["grasp_step_mode"])
        self.assertIn("close_gripper", arm.calls)

    def test_book_grasp_opens_gripper_through_arm_port_at_fifty_degrees(self):
        profile_command = GripperCommand(open_deg=5.0, open_speed_deg_s=1.0, kp=99.0, kd=4.0)
        sdk, arm, _reservation = _sdk(
            arm=FakeArm(enabled=True),
            slots={},
            plan=TakeBookMotionPlan(gripper=profile_command),
        )

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_GRASP))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        calls = [call for call in arm.calls if isinstance(call, tuple) and call[0] == "open_gripper"]
        self.assertEqual(len(calls), 1)
        _, target_deg, command = calls[0]
        self.assertEqual(target_deg, 50.0)
        self.assertIsNotNone(command)
        self.assertEqual(command.open_deg, 50.0)
        self.assertEqual(command, GripperCommand(open_deg=50.0))

    def test_book_grasp_open_speed_follows_the_plan_speed_level(self):
        from dataclasses import replace

        from bookarm.domain.plans.speed import apply_speed_level

        plan = apply_speed_level(replace(TakeBookMotionPlan(), speed_level="fast"))
        sdk, arm, _reservation = _sdk(arm=FakeArm(enabled=True), slots={}, plan=plan)

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_GRASP))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        calls = [call for call in arm.calls if isinstance(call, tuple) and call[0] == "open_gripper"]
        self.assertEqual(len(calls), 1)
        _, target_deg, command = calls[0]
        self.assertEqual(target_deg, 50.0)
        # The verified-script command shape keeps its kp/kd/effort defaults;
        # only the speed-coupled fields follow the fast level.
        self.assertEqual(command.open_speed_deg_s, 60.12)
        self.assertEqual(command.kp, 40.0)
        self.assertEqual(command.kd, 3.0)
        self.assertEqual(command.effort, 0.5)

    def test_book_grasp_uses_move_p_by_j_for_fine_grasp(self):
        sdk, arm, _reservation = _sdk(arm=FakeArm(enabled=True), slots={})

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_GRASP))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        motion_calls = [call for call in arm.calls if isinstance(call, tuple)]
        pregrasp = next(call for call in motion_calls if call[0] == "move_to_pose_by_move_j")
        fine_grasp = [call for call in motion_calls if call[0] == "move_to_pose_by_move_j"][1]
        self.assertEqual(pregrasp[2], TakeBookMotionPlan().pregrasp_duration_s)
        self.assertEqual(fine_grasp[2], TakeBookMotionPlan().fine_grasp_duration_s)
        plan = TakeBookMotionPlan()
        fine_offset = plan.fine_grasp_offset_cm
        self.assertAlmostEqual(fine_grasp[1].x_m, 0.21 + fine_offset[0] / 100.0)
        self.assertAlmostEqual(fine_grasp[1].y_m, 0.1 + fine_offset[1] / 100.0)
        self.assertAlmostEqual(fine_grasp[1].z_m, 0.3 + fine_offset[2] / 100.0)
        self.assertEqual(fine_grasp[1].rx_rad, math.radians(plan.fine_grasp_rpy_deg[0]))

    def test_book_grasp_uses_gripper_ready_after_arm_enable_without_reenabling(self):
        sdk, arm, _reservation = _sdk(arm=FakeArm(enabled=True), slots={})

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_GRASP))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertNotIn("enable", arm.calls)

    def test_book_grasp_step_reporter_records_started_and_completed_events(self):
        events = []
        sdk, _arm, _reservation = _sdk(
            arm=FakeArm(enabled=True),
            slots={},
            grasp_step_approval=lambda _step: True,
            grasp_step_reporter=lambda step, event, error: events.append((step, event, error)),
        )

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_GRASP))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(
            events,
            [
                (step, event, None)
                for step in task.result.payload["grasp_steps_completed"]
                for event in ("started", "completed")
            ],
        )

    def test_book_grasp_step_reporter_records_failure_before_workflow_stops(self):
        events = []
        sdk, _arm, _reservation = _sdk(
            arm=FakeArm(fail_at="move_to_pose_by_move_j", enabled=True),
            slots={},
            grasp_step_approval=lambda _step: True,
            grasp_step_reporter=lambda step, event, error: events.append((step, event, error)),
        )

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_GRASP))

        self.assertEqual(task.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(events[-2], ("arm_pregrasp", "started", None))
        self.assertEqual(events[-1][0:2], ("arm_pregrasp", "failed"))
        self.assertIn("simulated arm MovePByJ failure", events[-1][2])
        self.assertIn("book-grasp step arm_pregrasp failed", task.result.error)

    def test_book_grasp_step_mode_stops_before_declined_action(self):
        approvals = []

        def approve_until_approach(step):
            approvals.append(step)
            return step != "arm_approach"

        sdk, arm, reservation = _sdk(
            arm=FakeArm(enabled=True),
            slots={},
            grasp_step_approval=approve_until_approach,
        )

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_GRASP))

        self.assertEqual(task.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(approvals, ["open_gripper", "rod_home", "arm_pregrasp", "arm_approach"])
        self.assertNotIn("close_gripper", arm.calls)
        self.assertEqual(task.result.stages[-1].name, "book_grasp")
        self.assertIn("operator declined", task.result.error)
        self.assertTrue(reservation.released)

    def test_return_to_end_workflow_is_explicitly_unsupported(self):
        sdk, _arm, _reservation = _sdk()
        book = BookIdentity("book-42")
        location = ShelfLocation("A", 1, 1)

        end = sdk.return_book_to_end(ReturnBookToEndRequest(book, BookStandSlot.SLOT_1, location))

        self.assertEqual(end.state, TaskState.UNSUPPORTED)

    def test_public_disable_uses_the_checked_arm_sdk_with_the_hardware_reservation(self):
        sdk, arm, reservation = _sdk(arm=FakeArm(enabled=True))

        result = sdk.disable_arm(confirmed=True, duration_s=2.5)

        self.assertTrue(result["disable_command_sent"])
        self.assertEqual(arm.calls, [("disable_at_safe_pose", 2.5)])
        self.assertEqual(reservation.purpose, "disable_arm")
        self.assertTrue(reservation.released)


class TakeBookReachabilityTests(unittest.TestCase):
    """Fail-fast arm reachability precheck after vision, before any grasp motion."""

    @staticmethod
    def _checks(arm):
        return [call for call in arm.calls if isinstance(call, tuple) and call[0] == "check_pose_reachable"]

    @staticmethod
    def _grasp_motions(arm):
        return [
            call for call in arm.calls
            if isinstance(call, tuple) and call[0] in ("move_to_pose_by_move_j", "move_l", "open_gripper")
        ]

    def test_all_reachable_preserves_the_execution_order(self):
        plan = TakeBookMotionPlan()
        arm = FakeArm(enabled=True)
        sdk, arm, _reservation = _sdk(arm=arm)

        task = sdk.take_book(_request())

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        checks = self._checks(arm)
        self.assertEqual(
            [call[1] for call in checks],
            [
                "arm_pregrasp", "arm_approach", "arm_fine_grasp", "arm_post_grasp",
                "stand_pre_place", "stand_place", "stand_lower_place",
            ],
        )
        # The chain starts at the debug joint pose; each solved pose seeds the next.
        self.assertEqual(checks[0][3], plan.debug_joint)
        for call in checks[1:]:
            self.assertEqual(call[3], arm.check_solution)
        # Probing never moved the arm: every check ran before the first grasp action.
        first_grasp_action = next(
            index for index, call in enumerate(arm.calls)
            if isinstance(call, tuple) and call[0] == "open_gripper"
        )
        self.assertLess(arm.calls.index(checks[-1]), first_grasp_action)
        self.assertIn("close_gripper", arm.calls)

    def test_unreachable_pose_fails_the_task_before_any_grasp_motion(self):
        arm = FakeArm(enabled=True, unreachable_labels={"arm_fine_grasp"})
        sdk, arm, reservation = _sdk(arm=arm)

        task = sdk.take_book(_request())

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.result.stages[-1].name, "reachability_check")
        self.assertIn("arm pose unreachable (arm_fine_grasp)", task.result.error)
        self.assertIn("IK did not converge", task.result.error)
        # The chain stops at the failed pose; the arm was only parked at debug.
        self.assertEqual(
            [call[1] for call in self._checks(arm)],
            ["arm_pregrasp", "arm_approach", "arm_fine_grasp"],
        )
        self.assertEqual(self._grasp_motions(arm), [])
        self.assertNotIn("close_gripper", arm.calls)
        self.assertTrue(reservation.released)

    def test_unreachable_stand_pose_fails_with_the_placement_chain(self):
        arm = FakeArm(enabled=True, unreachable_labels={"stand_place"})
        sdk, arm, _reservation = _sdk(arm=arm)

        task = sdk.take_book(_request())

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.result.stages[-1].name, "reachability_check")
        self.assertIn("arm pose unreachable (stand_place)", task.result.error)
        # The placement chain is probed after the grasp chain; the failure
        # stops the chain, so stand_lower_place is never checked.
        self.assertEqual(
            [call[1] for call in self._checks(arm)],
            [
                "arm_pregrasp", "arm_approach", "arm_fine_grasp", "arm_post_grasp",
                "stand_pre_place", "stand_place",
            ],
        )
        self.assertEqual(self._grasp_motions(arm), [])

    def test_grasp_only_stop_checks_no_placement_poses(self):
        arm = FakeArm(enabled=True)
        sdk, arm, _reservation = _sdk(arm=arm, slots={})

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_GRASP))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        checks = [call[1] for call in self._checks(arm)]
        self.assertEqual(checks, ["arm_pregrasp", "arm_approach", "arm_fine_grasp", "arm_post_grasp"])
        self.assertEqual(task.result.payload["reachability_checked_poses"], checks)

    def test_stopping_at_debug_pose_skips_the_reachability_check(self):
        arm = FakeArm(enabled=True)
        sdk, arm, _reservation = _sdk(arm=arm, slots={})

        task = sdk.take_book(_request(stop_after=TakeBookStage.ARM_DEBUG_POSE))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertNotIn("reachability_check", [stage.name for stage in task.result.stages])
        self.assertEqual(self._checks(arm), [])


class TakeBookEmptyGraspTests(unittest.TestCase):
    """A monitored close that times out met no book; carrying on is unsafe."""

    @patch("bookarm.domain.policies.gripper.GRASP_CONTACT_CHECK_ENABLED", True)
    def test_empty_close_fails_book_grasp_before_any_post_grasp_motion(self):
        arm = FakeArm(enabled=True, close_reason="timeout")
        sdk, arm, reservation = _sdk(arm=arm, slots={})

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_GRASP))

        self.assertEqual(task.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(task.result.stages[-1].name, "book_grasp")
        self.assertIn(
            "gripper closed without contact (empty slot or misalignment)",
            task.result.error,
        )
        # Neither the post-grasp lift nor the return to debug was commanded.
        self.assertEqual(arm.calls[-1], "close_gripper")
        self.assertTrue(reservation.released)

    def test_empty_close_is_ignored_while_the_contact_check_is_disabled(self):
        """默认关闭（GRASP_CONTACT_CHECK_ENABLED=False）：空夹也继续流程。"""

        arm = FakeArm(enabled=True, close_reason="timeout")
        sdk, arm, _reservation = _sdk(arm=arm, slots={})

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_GRASP))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(task.result.stages[-1].name, "book_grasp")

    def test_close_that_reaches_its_target_counts_as_a_grasp(self):
        sdk, arm, _reservation = _sdk(
            arm=FakeArm(enabled=True, close_reason="target_reached"),
            slots={},
        )

        task = sdk.take_book(_request(stop_after=TakeBookStage.BOOK_GRASP))

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertIn("close_gripper", arm.calls)


class TakeBookSubStepCancellationTests(unittest.TestCase):
    """Cancel/interrupt checkpoints between the sub-steps of one stage."""

    def _use_case(self, manager, arm, *, approval=None, slots=None):
        return TakeBookUseCase(
            task_manager=manager,
            reservation=FakeReservation(),
            lift=FakeLift(),
            vision=FakeVision(),
            arm=arm,
            rod=FakeRod(),
            slots={} if slots is None else slots,
            grasp_step_approval=approval,
        )

    def test_cancel_between_grasp_sub_steps_stops_before_the_next_motion(self):
        manager = TaskManager()
        holder = {}

        def approve(step):
            if step == "arm_approach":
                manager.cancel(holder["task_id"])
            return True

        arm = FakeArm(enabled=True)
        use_case = self._use_case(manager, arm, approval=approve)
        request = _request(stop_after=TakeBookStage.BOOK_GRASP)
        task = use_case.create(request)
        holder["task_id"] = task.id.value
        rod = use_case.rod

        use_case.execute(task, request)

        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertEqual(task.result.error, "workflow cancelled")
        # The cancel landed inside book_grasp; that stage stays recorded as
        # running and no later sub-step motion was commanded.
        self.assertEqual(task.result.stages[-1].name, "book_grasp")
        self.assertEqual(task.result.stages[-1].status, StageStatus.RUNNING)
        self.assertNotIn("close_gripper", arm.calls)
        self.assertEqual(len(rod.calls), 1)

    def test_interrupt_between_grasp_sub_steps_reports_the_safety_stop(self):
        manager = TaskManager()
        holder = {}

        def approve(step):
            if step == "arm_approach":
                manager.interrupt(holder["task_id"])
            return True

        arm = FakeArm(enabled=True)
        use_case = self._use_case(manager, arm, approval=approve)
        request = _request(stop_after=TakeBookStage.BOOK_GRASP)
        task = use_case.create(request)
        holder["task_id"] = task.id.value

        use_case.execute(task, request)

        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertEqual(task.result.error, "workflow interrupted by safety stop")
        self.assertEqual(task.result.stages[-1].name, "book_grasp")
        self.assertNotIn("close_gripper", arm.calls)

    def test_cancel_inside_stand_place_chain_stops_at_the_next_motion(self):
        manager = TaskManager()
        holder = {}

        def on_move_l():
            # move_l #1 is the grasp-chain approach; #2 is the stand place.
            holder["count"] = holder.get("count", 0) + 1
            if holder["count"] == 2:
                manager.cancel(holder["task_id"])

        arm = FakeArm(enabled=True, move_l_hook=on_move_l)
        use_case = self._use_case(manager, arm, slots=_slots())
        request = _request()
        task = use_case.create(request)
        holder["task_id"] = task.id.value

        use_case.execute(task, request)

        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertEqual(task.result.error, "workflow cancelled")
        self.assertEqual(task.result.stages[-1].name, "book_stand_place")
        # The book was never released and the arm never retreated.
        opens = [call for call in arm.calls if isinstance(call, tuple) and call[0] == "open_gripper"]
        self.assertEqual(len(opens), 1)


class WorkflowEngineCancellationTests(unittest.TestCase):
    def test_cancelled_action_cancels_the_task_without_failing_the_stage(self):
        manager = TaskManager()
        engine = WorkflowEngine(manager)
        task = manager.create(WorkflowKind.TAKE_BOOK)
        later_motion = []

        def action():
            manager.cancel(task.id.value)
            engine.raise_if_cancelled(task)
            return {}

        result = engine.run(
            task,
            (
                ("carrying_stage", action, True),
                ("later_stage", lambda: later_motion.append(1) or {}, True),
            ),
        )

        self.assertEqual(result.state, TaskState.CANCELLED)
        self.assertEqual(result.error, "workflow cancelled")
        self.assertEqual([stage.name for stage in result.stages], ["carrying_stage"])
        self.assertEqual(result.stages[-1].status, StageStatus.RUNNING)
        self.assertEqual(later_motion, [])


if __name__ == "__main__":
    unittest.main()
