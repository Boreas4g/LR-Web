"""Independent device-stack tests using in-memory transports and devices."""

from __future__ import annotations

from decimal import Decimal
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import call, patch

from bookarm.devices.arm.motion import joint_trajectory
from bookarm.devices.arm.kinematics import PinocchioKinematics
from bookarm.devices.arm.protocol import CommType, MotorFeedback, RunMode
from bookarm.devices.arm.service import ArmDeviceConfig, ArmService, BOOK_DEBUG_JOINT, DISABLE_JOINT, ZERO_JOINT
from bookarm.devices.arm.transport import SocketCanTransport
from bookarm.devices.camera.d435_device import D435Device, D435Frame
from bookarm.devices.camera.transform import (
    CameraToBaseTransform,
    matrix_to_pose6,
    pose6_to_matrix,
)
from bookarm.devices.lift.service import AimLiftService, LiftDeviceConfig, LiftPositioningError
from bookarm.devices.rod.service import RodDeviceConfig, RodMotorService
from bookarm.domain.models.lift import LiftPositionResult
from bookarm.domain.models.pose import BasePose, JointPose
from bookarm.domain.plans.take_book import GripperCommand


class FakeArmTransport:
    def __init__(self):
        self.connected = False
        self.enabled = []
        self.feedback_enabled = []
        self.disabled = []
        self.run_modes = []
        self.can_timeouts = []
        self.pp_velocities = []
        self.pp_accelerations = []
        self.commands = []
        self._feedback = {
            index: MotorFeedback(index, 0.0, 0.0, 0.0, 25.0, 0, __import__("time").time())
            for index in range(1, 8)
        }

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    def enable_motor(self, motor_id):
        self.enabled.append(motor_id)
        item = self._feedback[motor_id]
        self._feedback[motor_id] = MotorFeedback(
            item.motor_id, item.position_rad, item.velocity_rad_s, item.torque_nm,
            item.temperature_c, item.fault_code, __import__("time").time(), 2,
        )
        return True

    def disable_motor(self, motor_id, clear_fault=False):
        self.disabled.append(motor_id)
        item = self._feedback[motor_id]
        fault_code = 0 if clear_fault else item.fault_code
        self._feedback[motor_id] = MotorFeedback(
            item.motor_id, item.position_rad, item.velocity_rad_s, item.torque_nm,
            item.temperature_c, fault_code, __import__("time").time(), 0,
        )
        return True

    def enable_feedback(self, motor_id):
        self.feedback_enabled.append(motor_id)
        return True

    def set_run_mode(self, motor_id, mode):
        self.run_modes.append((motor_id, mode))
        return True

    def set_can_timeout(self, motor_id, timeout_s):
        self.can_timeouts.append((motor_id, timeout_s))
        return True

    def set_pp_velocity(self, motor_id, velocity_rad_s):
        self.pp_velocities.append((motor_id, velocity_rad_s))
        return True

    def set_pp_acceleration(self, motor_id, acceleration_rad_s2):
        self.pp_accelerations.append((motor_id, acceleration_rad_s2))
        return True

    def send_motion(self, motor_id, position_rad, velocity_rad_s, kp, kd, torque_nm):
        self.commands.append((motor_id, position_rad, velocity_rad_s, kp, kd, torque_nm))
        previous = self._feedback[motor_id]
        self._feedback[motor_id] = MotorFeedback(
            motor_id, position_rad, velocity_rad_s, torque_nm, 25.0, 0,
            __import__("time").time(), previous.mode_state,
        )
        return True

    def feedback(self):
        now = __import__("time").time()
        for motor_id, item in tuple(self._feedback.items()):
            self._feedback[motor_id] = MotorFeedback(
                item.motor_id, item.position_rad, item.velocity_rad_s, item.torque_nm,
                item.temperature_c, item.fault_code, now, item.mode_state,
            )
        return dict(self._feedback)

    def can_fps(self):
        return 100.0

    def emergency_stop(self):
        for motor_id in range(1, 8):
            self.disable_motor(motor_id)
        return True


class ThermalAwareTransport(FakeArmTransport):
    """Preserve injected temperatures across motion-feedback rewrites."""

    def send_motion(self, motor_id, position_rad, velocity_rad_s, kp, kd, torque_nm):
        previous = self._feedback[motor_id]
        self.commands.append((motor_id, position_rad, velocity_rad_s, kp, kd, torque_nm))
        self._feedback[motor_id] = MotorFeedback(
            motor_id, position_rad, velocity_rad_s, torque_nm,
            previous.temperature_c, 0, time.time(), previous.mode_state,
        )
        return True

    def set_temperature(self, motor_id: int, celsius: float) -> None:
        item = self._feedback[motor_id]
        self._feedback[motor_id] = MotorFeedback(
            item.motor_id, item.position_rad, item.velocity_rad_s, item.torque_nm,
            celsius, item.fault_code, time.time(), item.mode_state,
        )


class StalledGripperTransport(FakeArmTransport):
    """Record gripper commands while keeping its feedback at the initial angle."""

    def send_motion(self, motor_id, position_rad, velocity_rad_s, kp, kd, torque_nm):
        self.commands.append((motor_id, position_rad, velocity_rad_s, kp, kd, torque_nm))
        if motor_id != 7:
            previous = self._feedback[motor_id]
            self._feedback[motor_id] = MotorFeedback(
                motor_id, position_rad, velocity_rad_s, torque_nm, 25.0, 0,
                __import__("time").time(), previous.mode_state,
            )

    def feedback(self):
        item = self._feedback[7]
        self._feedback[7] = MotorFeedback(
            item.motor_id, item.position_rad, item.velocity_rad_s, item.torque_nm,
            item.temperature_c, item.fault_code, __import__("time").time(), item.mode_state,
        )
        return super().feedback()


class NoGripperFeedbackRefreshTransport(FakeArmTransport):
    """Keep the gripper's timestamp stale after its feedback refresh request."""

    def enable_feedback(self, motor_id):
        super().enable_feedback(motor_id)
        return True

    def feedback(self):
        feedback = super().feedback()
        if len(self.feedback_enabled) > 14:
            item = feedback[7]
            feedback[7] = MotorFeedback(
                item.motor_id, item.position_rad, item.velocity_rad_s, item.torque_nm,
                item.temperature_c, item.fault_code, 0.0, item.mode_state,
            )
            self._feedback[7] = feedback[7]
        return feedback


class NoArmFeedbackRefreshTransport(FakeArmTransport):
    """Keep 1--6 feedback stale after the post-enable feedback subscription."""

    def __init__(self):
        super().__init__()
        self.drop_arm_feedback_after_request = False

    def enable_feedback(self, motor_id):
        super().enable_feedback(motor_id)
        if self.drop_arm_feedback_after_request and motor_id in range(1, 7):
            item = self._feedback[motor_id]
            self._feedback[motor_id] = MotorFeedback(
                item.motor_id, item.position_rad, item.velocity_rad_s, item.torque_nm,
                item.temperature_c, item.fault_code, 0.0, item.mode_state,
            )
        return True

    def feedback(self):
        if not self.drop_arm_feedback_after_request:
            return super().feedback()
        feedback = dict(self._feedback)
        for motor_id in range(1, 7):
            item = feedback[motor_id]
            feedback[motor_id] = MotorFeedback(
                item.motor_id, item.position_rad, item.velocity_rad_s, item.torque_nm,
                item.temperature_c, item.fault_code, 0.0, item.mode_state,
            )
            self._feedback[motor_id] = feedback[motor_id]
        return feedback


class FailingMotionTransport(FakeArmTransport):
    """Simulate a CAN write failure without changing the enabled state."""

    def send_motion(self, motor_id, position_rad, velocity_rad_s, kp, kd, torque_nm):
        self.commands.append((motor_id, position_rad, velocity_rad_s, kp, kd, torque_nm))
        return False


class FakeAimDrive:
    def __init__(self):
        self.enabled_modbus = False
        self.enabled_output = False
        self.closed = False
        self.absolute_position = 0

    def enable_modbus(self):
        self.enabled_modbus = True

    def enable_output(self):
        self.enabled_output = True

    def disable_output(self):
        self.enabled_output = False

    def disable_modbus(self):
        self.enabled_modbus = False

    def is_modbus_enabled(self):
        return self.enabled_modbus

    def is_output_enabled(self):
        return self.enabled_output

    def assert_motion_ready(self):
        if not self.enabled_modbus or not self.enabled_output:
            raise RuntimeError("drive is not motion ready")

    def close(self):
        self.closed = True

    def read_absolute_position(self):
        return self.absolute_position


class FakeLiftPlatform:
    def __init__(self, drive):
        self.drive = drive
        self.height_cm = Decimal("1")
        self.moves = []

    def height_to_absolute_position(self, height_cm):
        if not 1.0 <= float(height_cm) <= 170.0:
            raise ValueError("lift height must be in the range 1..170 cm")
        return int(round(float(height_cm) * 100))

    def speed_profile(self, speed):
        if speed not in (1, 2, 3):
            raise ValueError("lift speed must be 1, 2, or 3")

    def move_to_height(self, height_cm, speed, wait, timeout_s, tolerance_pulses, check_ready):
        self.moves.append((float(height_cm), speed, wait, timeout_s, tolerance_pulses, check_ready))
        self.height_cm = Decimal(str(height_cm))
        self.drive.absolute_position = int(round(float(height_cm) * 100))
        return self.drive.absolute_position

    def read_height_cm(self):
        return self.height_cm


class FakeRodSerial:
    def __init__(self):
        self.is_open = True
        self.last_target_rad = 0.0
        self.messages = []

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def write(self, data):
        message = json.loads(data.decode("utf-8"))
        self.messages.append(message)
        if message["T"] == RodMotorService.WRITE_ANGLE:
            self.last_target_rad = message["rad"]

    def flush(self):
        pass

    def readline(self):
        return json.dumps({"ok": True, "rad": self.last_target_rad}).encode("utf-8") + b"\n"

    def close(self):
        self.is_open = False


class FlakyRodSerial(FakeRodSerial):
    """Dies with EIO on the first ``fail_writes`` writes, then works."""

    def __init__(self, fail_writes=0):
        super().__init__()
        self.fail_writes = fail_writes

    def write(self, data):
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise OSError(5, "Input/output error")
        super().write(data)


class FlakyAimDrive(FakeAimDrive):
    """Dies with EIO on the first ``fail_reads`` position reads, then works."""

    def __init__(self, fail_reads=0):
        super().__init__()
        self.fail_reads = fail_reads

    def read_absolute_position(self):
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise OSError(5, "Input/output error")
        return super().read_absolute_position()


class StoppableAimDrive(FakeAimDrive):
    """Fake drive that records software emergency-stop sequences."""

    def __init__(self, fail_reads=0):
        super().__init__()
        self.fail_reads = fail_reads
        self.emergency_stops = 0

    def emergency_stop(self):
        self.emergency_stops += 1


class HangingLiftPlatform(FakeLiftPlatform):
    """Platform whose move starts but never reaches the target by itself."""

    def move_to_height(self, height_cm, speed, wait, timeout_s, tolerance_pulses, check_ready):
        self.moves.append((float(height_cm), speed, wait, timeout_s, tolerance_pulses, check_ready))
        return int(round(float(height_cm) * 100))


def _run_capturing(action):
    try:
        action()
        return None
    except Exception as error:
        return error


class DeviceStackTests(unittest.TestCase):
    def test_book_debug_joint_matches_documented_degrees(self):
        expected_degrees = (0.0, 10.0, -65.0, 50.0, 0.0, 0.0)
        actual_degrees = tuple(math.degrees(value) for value in BOOK_DEBUG_JOINT.values_rad)
        for actual, expected in zip(actual_degrees, expected_degrees):
            self.assertAlmostEqual(actual, expected, places=9)

    def test_arm_config_defaults_to_calibrated_tcp_offset(self):
        self.assertEqual(
            ArmDeviceConfig().tcp_offset,
            (0.0, 0.01, -0.14, 0.0, 0.0, 0.0),
        )

    def test_arm_config_uses_calibrated_gripper_limits(self):
        lower, upper = ArmDeviceConfig().joint_limits[7]

        self.assertEqual(lower, 0.0)
        self.assertAlmostEqual(math.degrees(upper), 108.5, places=9)

    def test_arm_transport_parses_motor_mode_from_feedback_identifier(self):
        transport = SocketCanTransport()
        # Type 2 feedback: mode uses bits 23--22 and faults use bits 21--16.
        can_id = (2 << 24) | (2 << 22) | (3 << 16) | (1 << 8) | 0xFD
        transport._parse(can_id, bytes(8))

        feedback = transport.feedback()[1]
        self.assertEqual(feedback.mode_state, 2)
        self.assertEqual(feedback.fault_code, 3)

    def test_arm_transport_encodes_run_mode_as_uint32_parameter_write(self):
        transport = SocketCanTransport()

        with patch.object(transport, "_send") as send:
            transport.set_run_mode(7, RunMode.MOTION_CONTROL)

        expected_id = (int(CommType.WRITE_PARAM) << 24) | (0xFD << 8) | 7
        send.assert_called_once_with(expected_id, b"\x05\x70\x00\x00\x00\x00\x00\x00")

    def test_arm_service_owns_transport_and_confirms_joint_arrival(self):
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=50.0, final_tolerance_rad=0.01), transport=transport
        )
        service.start()
        try:
            service.enable()
            target = JointPose.from_values((0.1, 0.2, -0.2, 0.0, 0.0, 0.0))
            service.move_j(target, 0.04)

            self.assertEqual(service.read_joint_pose(), target)
            self.assertEqual(transport.enabled, list(range(1, 8)))
            self.assertEqual(transport.disabled, [7])
            self.assertEqual(transport.run_modes, [(7, RunMode.MOTION_CONTROL)])
            self.assertEqual(transport.can_timeouts, [(7, 0.0)])
            self.assertEqual(transport.feedback_enabled[:14], list(range(1, 8)) * 2)
            self.assertTrue(set(transport.feedback_enabled[14:]).issubset(set(range(1, 7))))
            self.assertGreaterEqual(len(transport.commands), 6)
        finally:
            service.stop()
        self.assertFalse(transport.connected)

    def test_arm_enable_starts_persistent_six_axis_hold_loop(self):
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=200.0, gravity_feedforward_ratio=0.0), transport=transport
        )
        service.start()
        try:
            service.enable()
            time.sleep(0.06)
            hold_commands = list(transport.commands)
            self.assertGreaterEqual(len(hold_commands), 24)
            self.assertTrue(all(command[0] in range(1, 7) for command in hold_commands))
            self.assertTrue(all(command[1:3] == (0.0, 0.0) for command in hold_commands))

            service.move_j(JointPose.from_values((0.1, 0.0, 0.0, 0.0, 0.0, 0.0)), 0.04)
            command_count_after_motion = len(transport.commands)
            time.sleep(0.04)
            self.assertGreater(len(transport.commands), command_count_after_motion)
            self.assertTrue(all(command[0] in range(1, 7) for command in transport.commands))
        finally:
            service.close()

        self.assertFalse(service._control_running.is_set())
        self.assertEqual(transport.disabled, [7])

    def test_arm_enable_clears_latched_motor_fault(self):
        transport = FakeArmTransport()
        # 夹爪锁存过流故障（fault 16）且处于 RESET：只发使能帧无法恢复，
        # 使能路径必须先失能并清故障才能成功。
        item = transport._feedback[7]
        transport._feedback[7] = MotorFeedback(
            item.motor_id, item.position_rad, item.velocity_rad_s, item.torque_nm,
            item.temperature_c, 16, __import__("time").time(), 0,
        )
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=200.0, gravity_feedforward_ratio=0.0), transport=transport
        )
        service.start()
        try:
            service.enable()
            self.assertTrue(service.is_enabled())
            self.assertEqual(transport.feedback()[7].fault_code, 0)
            self.assertEqual(transport.feedback()[7].mode_state, 2)
        finally:
            service.close()

    def test_is_enabled_adopts_arm_left_enabled_by_previous_owner(self):
        transport = FakeArmTransport()
        for motor_id in range(1, 8):
            transport.enable_motor(motor_id)
        transport.enabled.clear()
        transport.feedback_enabled.clear()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=200.0, gravity_feedforward_ratio=0.0), transport=transport
        )
        service.start()
        try:
            self.assertTrue(service.is_enabled())
            self.assertTrue(service._control_running.is_set())
            time.sleep(0.06)
            hold_commands = [command for command in transport.commands if command[0] in range(1, 7)]
            self.assertGreaterEqual(len(hold_commands), 12)
            self.assertTrue(all(command[1:3] == (0.0, 0.0) for command in hold_commands))
            # Adoption only starts the hold loop; it never re-sends enable frames.
            self.assertEqual(transport.enabled, [])
        finally:
            service.close()

        self.assertFalse(service._control_running.is_set())

    def test_move_l_retries_waypoint_solve_without_limit_margin(self):
        # Deep-shelf push targets are only solvable within 0.05 rad of a joint
        # limit: _move_l must retry a failed waypoint solve with margin 0
        # (validate_joint_target still enforces the true URDF limits).
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=200.0, gravity_feedforward_ratio=0.0), transport=transport
        )
        margins = []

        class BoundaryKinematics:
            def forward_kinematics(self, joints):
                return BasePose(0.0, 0.0, 0.10, 0.0, 0.0, 0.0)

            def ik_step(self, waypoint, seed, **kwargs):
                return seed, 1.0  # always force the full-solve branch

            def solve_ik(self, target, seed, **kwargs):
                margin = kwargs.get("joint_limit_margin", 0.05)
                margins.append(margin)
                if margin > 0.0:
                    raise RuntimeError("arm inverse kinematics did not converge")
                return seed

            def compute_gravity(self, joints):
                return (0.0,) * 6

        service.start()
        try:
            service.enable()
            service._kinematics = BoundaryKinematics()
            service.move_l(BasePose(0.0, 0.0, 0.12, 0.0, 0.0, 0.0), 0.06)
            self.assertTrue(margins)
            self.assertIn(0.0, margins)
        finally:
            service.close()

    def test_move_p_by_j_retries_ik_without_limit_margin(self):
        # The same boundary rescue as _move_l: a fresh solve that fails inside
        # the conservative margin is retried once with margin 0.
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=200.0, gravity_feedforward_ratio=0.0), transport=transport
        )
        margins = []

        class BoundaryKinematics:
            def solve_ik(self, target, seed, **kwargs):
                margin = kwargs.get("joint_limit_margin", 0.05)
                margins.append(margin)
                if margin > 0.0:
                    raise RuntimeError("arm inverse kinematics did not converge")
                return seed

            def compute_gravity(self, joints):
                return (0.0,) * 6

        service.start()
        try:
            service.enable()
            service._kinematics = BoundaryKinematics()
            service.move_to_pose_by_move_j(BasePose(-0.40, -0.10, 0.25, 0.0, 0.0, 0.0), 0.05)
            self.assertEqual(margins, [0.05, 0.0])
        finally:
            service.close()

    def test_check_pose_reachable_uses_the_motion_solver_and_sends_no_motion(self):
        # The fail-fast precheck must mirror MovePByJ's solve exactly — same
        # known-good fallback seeds — while sending no arm motion at all.
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=200.0, gravity_feedforward_ratio=0.0), transport=transport
        )
        calls = []
        solution = JointPose.from_values((0.2, 0.1, -0.1, 0.05, 0.0, 0.0))

        class RecordingKinematics:
            def solve_ik(self, target, seed, **kwargs):
                calls.append((target, seed, kwargs))
                return solution

            def compute_gravity(self, joints):
                return (0.0,) * 6

        service.start()
        try:
            service._kinematics = RecordingKinematics()
            seed = JointPose.from_values((0.1, 0.2, -0.3, 0.4, 0.5, -0.6))
            target = BasePose(-0.35, -0.07, 0.25, 0.0, 0.0, 0.0)

            result = service.check_pose_reachable(target, seed, label="arm_pregrasp")

            self.assertEqual(result, solution)
            self.assertEqual(len(calls), 1)
            _target, used_seed, kwargs = calls[0]
            self.assertEqual(used_seed, seed)
            self.assertEqual(kwargs["fallback_seeds"], (ZERO_JOINT, BOOK_DEBUG_JOINT))
            self.assertEqual(transport.commands, [])
        finally:
            service.close()

    def test_check_pose_reachable_retries_without_margin_then_raises_a_labelled_error(self):
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=200.0, gravity_feedforward_ratio=0.0), transport=transport
        )
        margins = []

        class FailingKinematics:
            def solve_ik(self, target, seed, **kwargs):
                margins.append(kwargs.get("joint_limit_margin", 0.05))
                raise RuntimeError("arm inverse kinematics did not converge")

            def compute_gravity(self, joints):
                return (0.0,) * 6

        service.start()
        try:
            service._kinematics = FailingKinematics()
            seed = JointPose.from_values((0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
            target = BasePose(5.0, 5.0, 5.0, 0.0, 0.0, 0.0)

            with self.assertRaises(RuntimeError) as ctx:
                service.check_pose_reachable(target, seed, label="arm_fine_grasp")

            self.assertIn("arm pose unreachable (arm_fine_grasp): IK did not converge", str(ctx.exception))
            self.assertIn("5.0", str(ctx.exception))
            self.assertEqual(margins, [0.05, 0.0])
            self.assertEqual(transport.commands, [])
        finally:
            service.close()

    def test_check_pose_reachable_rejects_a_solution_outside_the_joint_limits(self):
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=200.0, gravity_feedforward_ratio=0.0), transport=transport
        )

        class OutOfLimitKinematics:
            def solve_ik(self, target, seed, **kwargs):
                return JointPose.from_values((99.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            def compute_gravity(self, joints):
                return (0.0,) * 6

        service.start()
        try:
            service._kinematics = OutOfLimitKinematics()
            with self.assertRaises(RuntimeError) as ctx:
                service.check_pose_reachable(
                    BasePose(0.0, 0.0, 0.1, 0.0, 0.0, 0.0),
                    JointPose.from_values((0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
                    label="probe_pose",
                )
            self.assertIn("arm pose unreachable (probe_pose)", str(ctx.exception))
            self.assertIn("outside", str(ctx.exception))
        finally:
            service.close()

    def test_check_pose_reachable_defaults_to_the_live_feedback_seed(self):
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=200.0, gravity_feedforward_ratio=0.0), transport=transport
        )
        seeds = []

        class SeedRecordingKinematics:
            def solve_ik(self, target, seed, **kwargs):
                seeds.append(seed)
                return seed

            def compute_gravity(self, joints):
                return (0.0,) * 6

        service.start()
        try:
            service.enable()
            service._kinematics = SeedRecordingKinematics()

            result = service.check_pose_reachable(
                BasePose(0.0, 0.0, 0.1, 0.0, 0.0, 0.0), None, label="probe"
            )

            self.assertEqual(seeds, [service.read_joint_pose()])
            self.assertEqual(result, seeds[0])
        finally:
            service.close()

    def test_thermal_watchdog_stops_overheated_arm_and_latches_until_cooled(self):
        transport = ThermalAwareTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=200.0,
                gravity_feedforward_ratio=0.0,
                thermal_limit_c=75.0,
                thermal_check_interval_s=0.05,
                thermal_trip_sustained_s=0.0,
                thermal_safe_pose_duration_s=0.05,
            ),
            transport=transport,
        )

        service.start()
        try:
            service.enable()
            self.assertTrue(service._control_running.is_set())

            transport.set_temperature(3, 90.0)
            deadline = time.time() + 3.0
            while service._thermal_lockout is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(service._thermal_lockout)
            # The arm parked at the collapse pose before being disabled.
            self.assertTrue(service._thermal_lockout["safe_posed"])
            self.assertEqual(sorted(set(transport.disabled)), [1, 2, 3, 4, 5, 6, 7])
            self.assertFalse(service._control_running.is_set())
            with self.assertRaises(RuntimeError):
                service.enable()

            # Still hot: clearing is refused.
            with self.assertRaises(RuntimeError):
                service.clear_thermal_lockout()

            transport.set_temperature(3, 40.0)
            status = service.clear_thermal_lockout()
            self.assertIsNone(status["lockout"])
            self.assertEqual(status["temperatures_c"][3], 40.0)
        finally:
            service.close()

    def test_thermal_trip_parks_at_collapse_pose_before_disabling(self):
        class OrderRecordingTransport(ThermalAwareTransport):
            def __init__(self):
                super().__init__()
                self.events = []

            def send_motion(self, motor_id, position_rad, velocity_rad_s, kp, kd, torque_nm):
                self.events.append(("motion", motor_id, position_rad))
                return super().send_motion(motor_id, position_rad, velocity_rad_s, kp, kd, torque_nm)

            def disable_motor(self, motor_id, clear_fault=False):
                self.events.append(("disable", motor_id, None))
                return super().disable_motor(motor_id, clear_fault=clear_fault)

        transport = OrderRecordingTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=200.0,
                gravity_feedforward_ratio=0.0,
                thermal_limit_c=75.0,
                thermal_check_interval_s=0.05,
                thermal_trip_sustained_s=0.0,
                thermal_safe_pose_duration_s=0.05,
            ),
            transport=transport,
        )

        service.start()
        try:
            service.enable()
            # The enable path briefly disables joint 7 while configuring run
            # modes; only record the trip sequence itself from here on.
            transport.events.clear()
            transport.set_temperature(2, 88.0)
            deadline = time.time() + 3.0
            while service._thermal_lockout is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(service._thermal_lockout)
            lockout = service._thermal_lockout
            self.assertTrue(lockout["safe_posed"])
            self.assertEqual(lockout["motor_id"], 2)

            kinds = [kind for kind, _motor_id, _position in transport.events]
            self.assertIn("motion", kinds)
            self.assertIn("disable", kinds)
            # Every park motion command precedes the first disable frame.
            self.assertLess(kinds.index("motion"), kinds.index("disable"))
            last_motion = max(index for index, kind in enumerate(kinds) if kind == "motion")
            first_disable = kinds.index("disable")
            self.assertLess(last_motion, first_disable)
            # The last arm-joint targets sent before disabling are the collapse pose.
            final_targets = {}
            for kind, motor_id, position in transport.events:
                if kind == "motion" and motor_id in range(1, 7):
                    final_targets[motor_id] = position
            for motor_id, expected in enumerate(DISABLE_JOINT.values_rad, start=1):
                # -180° wraps to +180° through the trajectory generator. The
                # park succeeds when the final targets land within the joint
                # arrival tolerance (0.08 rad) of the collapse pose: the last
                # emitted setpoint may legitimately stop one trajectory step
                # short of the exact target.
                error = (final_targets[motor_id] - expected + math.pi) % (2 * math.pi) - math.pi
                self.assertAlmostEqual(error, 0.0, delta=0.08)
        finally:
            service.close()

    def test_thermal_trip_falls_back_to_direct_disable_when_park_fails(self):
        transport = ThermalAwareTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=200.0,
                gravity_feedforward_ratio=0.0,
                thermal_limit_c=75.0,
                thermal_check_interval_s=0.05,
                thermal_trip_sustained_s=0.0,
                thermal_safe_pose_duration_s=0.05,
            ),
            transport=transport,
        )

        service.start()
        try:
            service.enable()
            with patch.object(service, "_move_j", side_effect=RuntimeError("park boom")):
                transport.set_temperature(4, 91.0)
                deadline = time.time() + 3.0
                while service._thermal_lockout is None and time.time() < deadline:
                    time.sleep(0.05)

            lockout = service._thermal_lockout
            self.assertIsNotNone(lockout)
            self.assertFalse(lockout["safe_posed"])
            self.assertIn("park boom", lockout["detail"])
            # The fallback still cut power on every motor and stopped the hold loop.
            self.assertEqual(sorted(set(transport.disabled)), [1, 2, 3, 4, 5, 6, 7])
            self.assertFalse(service._control_running.is_set())
        finally:
            service.close()

    def test_thermal_trip_with_partially_enabled_arm_disables_directly(self):
        transport = ThermalAwareTransport()
        # Physically half-enabled without the service knowing: the checked
        # park sequence must refuse and fall back to the direct stop.
        for motor_id in (1, 2, 3):
            transport.enable_motor(motor_id)
        transport.enabled.clear()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=200.0,
                gravity_feedforward_ratio=0.0,
                thermal_limit_c=75.0,
                thermal_check_interval_s=0.05,
                thermal_trip_sustained_s=0.0,
                thermal_safe_pose_duration_s=0.05,
            ),
            transport=transport,
        )

        service.start()
        try:
            transport.set_temperature(1, 95.0)
            deadline = time.time() + 3.0
            while service._thermal_lockout is None and time.time() < deadline:
                time.sleep(0.05)

            lockout = service._thermal_lockout
            self.assertIsNotNone(lockout)
            self.assertFalse(lockout["safe_posed"])
            self.assertEqual(sorted(set(transport.disabled)), [1, 2, 3, 4, 5, 6, 7])
        finally:
            service.close()

    def test_thermal_trip_requires_sustained_over_limit(self):
        transport = ThermalAwareTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=200.0,
                gravity_feedforward_ratio=0.0,
                thermal_limit_c=75.0,
                thermal_check_interval_s=0.05,
                thermal_trip_sustained_s=0.3,
                thermal_safe_pose_duration_s=0.05,
            ),
            transport=transport,
        )
        service.start()
        try:
            service.enable()
            transport.set_temperature(3, 90.0)
            # Below the sustained budget the spike alone must not latch a trip.
            time.sleep(0.18)
            self.assertIsNone(service._thermal_lockout)
            self.assertIsNotNone(service._over_limit_since)
            deadline = time.time() + 3.0
            while service._thermal_lockout is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(service._thermal_lockout)
            self.assertEqual(service._thermal_lockout["motor_id"], 3)
        finally:
            service.close()

    def test_thermal_spike_does_not_trip(self):
        transport = ThermalAwareTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=200.0,
                gravity_feedforward_ratio=0.0,
                thermal_limit_c=75.0,
                thermal_check_interval_s=0.05,
                thermal_trip_sustained_s=0.4,
                thermal_safe_pose_duration_s=0.05,
            ),
            transport=transport,
        )
        service.start()
        try:
            service.enable()
            transport.set_temperature(2, 95.0)
            time.sleep(0.15)
            transport.set_temperature(2, 40.0)
            time.sleep(0.6)
            self.assertIsNone(service._thermal_lockout)
            self.assertIsNone(service._over_limit_since)
        finally:
            service.close()

    def test_thermal_lockout_auto_releases_below_release_threshold(self):
        transport = ThermalAwareTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=200.0,
                gravity_feedforward_ratio=0.0,
                thermal_limit_c=75.0,
                thermal_check_interval_s=0.05,
                thermal_trip_sustained_s=0.0,
                thermal_safe_pose_duration_s=0.05,
                thermal_release_c=50.0,
            ),
            transport=transport,
        )
        service.start()
        try:
            service.enable()
            transport.set_temperature(3, 90.0)
            deadline = time.time() + 3.0
            while service._thermal_lockout is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(service._thermal_lockout)

            transport.set_temperature(3, 40.0)
            deadline = time.time() + 3.0
            while service._thermal_lockout is not None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNone(service._thermal_lockout)
            self.assertIsNone(service.thermal_status()["lockout"])
        finally:
            service.close()

    def test_clear_thermal_lockout_stops_busy_worker_first(self):
        transport = ThermalAwareTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=200.0,
                gravity_feedforward_ratio=0.0,
                thermal_limit_c=75.0,
                thermal_check_interval_s=0.05,
                thermal_trip_sustained_s=0.0,
            ),
            transport=transport,
        )
        service.start()
        try:
            service.enable()
            service._thermal_lockout = {
                "message": "thermal lockout: test",
                "reason": "thermal lockout: test",
                "motor_id": 3,
                "temperature_c": 91.0,
                "limit_c": 75.0,
                "safe_posed": True,
                "detail": None,
            }
            busy = threading.Thread(
                target=lambda: service._worker.call(lambda: time.sleep(1.0), timeout_s=10.0),
                daemon=True,
            )
            busy.start()
            time.sleep(0.1)
            started = time.monotonic()
            status = service.clear_thermal_lockout()
            self.assertLess(time.monotonic() - started, 25.0)
            self.assertIsNone(status["lockout"])
            busy.join(timeout=5.0)
        finally:
            service.close()

    def test_gripper_offline_does_not_block_arm_motion_or_safe_disable(self):
        transport = ThermalAwareTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=50.0, gravity_feedforward_ratio=0.0),
            transport=transport,
        )
        service.start()
        try:
            service.enable()
            # Gripper motor drops out of Motor mode (thermal firmware torque cut).
            item = transport._feedback[7]
            transport._feedback[7] = MotorFeedback(
                item.motor_id, item.position_rad, item.velocity_rad_s, item.torque_nm,
                item.temperature_c, item.fault_code, item.timestamp_s, 0,
            )
            # Arm body is reported and usable with the gripper offline.
            self.assertTrue(service.is_enabled())
            self.assertFalse(service._gripper_enabled)
            service.move_to_zero(0.04)

            with self.assertRaisesRegex(RuntimeError, "夹爪电机未使能"):
                service.open_gripper(30.0, GripperCommand(open_speed_deg_s=720.0))

            result = service.disable_at_safe_pose(0.04)
            self.assertFalse(result["already_disabled"])
            self.assertTrue(result["gripper_was_offline"])
            self.assertEqual(sorted(set(transport.disabled)), [1, 2, 3, 4, 5, 6, 7])
        finally:
            service.close()

    def test_gripper_overheat_warns_and_pauses_carry_without_arm_lockout(self):
        transport = ThermalAwareTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=200.0,
                gravity_feedforward_ratio=0.0,
                thermal_limit_c=90.0,
                thermal_check_interval_s=0.05,
                thermal_trip_sustained_s=0.0,
                thermal_safe_pose_duration_s=0.05,
                gripper_thermal_limit_c=70.0,
            ),
            transport=transport,
        )
        service.start()
        try:
            service.enable()
            command = GripperCommand(
                close_deg=20.0, close_speed_deg_s=720.0, timeout_s=1.0,
                monitor_period_s=0.01, target_tolerance_deg=0.1, stall_tolerance_deg=0.05,
                stall_time_s=0.05, min_monitor_s=0.0, hold_margin_deg=0.1,
                command_lead_s=0.1, stall_lead_threshold_deg=0.5,
            )
            result = service.close_gripper_monitored(command)
            self.assertTrue(result["carry_hold"])

            transport.set_temperature(7, 72.0)
            deadline = time.time() + 2.0
            while service._gripper_thermal_warning is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(service._gripper_thermal_warning)
            # Carry hold paused, but the arm body is not locked out.
            self.assertIsNone(service._gripper_carry)
            self.assertIsNone(service._thermal_lockout)

            # An arm joint over the arm limit still trips the full lockout.
            transport.set_temperature(3, 95.0)
            deadline = time.time() + 3.0
            while service._thermal_lockout is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(service._thermal_lockout)
        finally:
            service.close()

    def test_gripper_carry_derates_torque_when_hot_and_recovers_with_hysteresis(self):
        transport = ThermalAwareTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=200.0,
                gravity_feedforward_ratio=0.0,
                gripper_derate_onset_c=60.0,
                gripper_derate_release_c=55.0,
            ),
            transport=transport,
        )
        service.start()
        try:
            service.enable()
            command = GripperCommand(
                close_deg=20.0, close_speed_deg_s=720.0, timeout_s=1.0,
                monitor_period_s=0.01, target_tolerance_deg=0.1, stall_tolerance_deg=0.05,
                stall_time_s=0.05, min_monitor_s=0.0, hold_margin_deg=0.1,
                command_lead_s=0.1, stall_lead_threshold_deg=0.5,
                carry_effort=0.4, carry_lead_deg=1.0,
            )
            service.close_gripper_monitored(command)
            carry_target = math.radians(20.0 + 1.0)

            def carry_efforts():
                return [
                    item[5]
                    for item in transport.commands
                    if item[0] == 7 and math.isclose(item[1], carry_target, abs_tol=1e-9)
                ]

            time.sleep(0.2)
            self.assertTrue(carry_efforts())
            self.assertTrue(all(effort == 0.4 for effort in carry_efforts()))

            transport.set_temperature(7, 62.0)
            time.sleep(0.25)
            hot = carry_efforts()
            self.assertIn(0.2, hot)
            self.assertTrue(service._gripper_carry_derated)

            # 58 C sits inside the hysteresis band: the derated torque holds.
            transport.set_temperature(7, 58.0)
            time.sleep(0.25)
            self.assertTrue(service._gripper_carry_derated)

            transport.set_temperature(7, 54.0)
            time.sleep(0.25)
            self.assertFalse(service._gripper_carry_derated)
            cool = carry_efforts()
            self.assertEqual(cool[-1], 0.4)
        finally:
            service.close()

    def test_telemetry_logger_writes_joint_csv_rows(self):
        transport = FakeArmTransport()
        with tempfile.TemporaryDirectory() as tmp:
            service = ArmService(
                ArmDeviceConfig(
                    telemetry_interval_s=0.05,
                    telemetry_dir=Path(tmp),
                ),
                transport=transport,
            )
            service.start()
            try:
                time.sleep(0.3)
            finally:
                service.close()
            path = service.telemetry_path
            self.assertIsNotNone(path)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines[0].split(",")), 22)
            self.assertGreaterEqual(len(lines), 3)
            row = lines[1].split(",")
            self.assertEqual(row[1], "0.000")
            self.assertEqual(row[2], "25.0")

    def test_telemetry_logger_deletes_files_older_than_retention(self):
        transport = FakeArmTransport()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            now = time.time()
            expired = directory / "telemetry_20200101_000000.csv"
            fresh = directory / "telemetry_20200102_000000.csv"
            # Bench-test captures share the directory and must survive cleanup.
            bench = directory / "motor1_bench_20200101_000000.csv"
            for path in (expired, fresh, bench):
                path.write_text("timestamp,j1_torque_nm\n", encoding="utf-8")
            os.utime(expired, (now - 8 * 86400.0, now - 8 * 86400.0))
            os.utime(fresh, (now - 1 * 86400.0, now - 1 * 86400.0))
            os.utime(bench, (now - 30 * 86400.0, now - 30 * 86400.0))
            service = ArmService(
                ArmDeviceConfig(
                    telemetry_interval_s=0.05,
                    telemetry_dir=directory,
                    telemetry_retention_days=7.0,
                ),
                transport=transport,
            )
            service.start()
            try:
                self.assertFalse(expired.exists())
                self.assertTrue(fresh.exists())
                self.assertTrue(bench.exists())
                self.assertTrue(service.telemetry_path.exists())
            finally:
                service.close()

    def test_telemetry_logger_keeps_all_files_when_retention_is_zero(self):
        transport = FakeArmTransport()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            now = time.time()
            expired = directory / "telemetry_20200101_000000.csv"
            expired.write_text("timestamp,j1_torque_nm\n", encoding="utf-8")
            os.utime(expired, (now - 30 * 86400.0, now - 30 * 86400.0))
            service = ArmService(
                ArmDeviceConfig(
                    telemetry_interval_s=0.05,
                    telemetry_dir=directory,
                    telemetry_retention_days=0.0,
                ),
                transport=transport,
            )
            service.start()
            try:
                self.assertTrue(expired.exists())
            finally:
                service.close()

    def test_arm_hold_loop_renews_feedback_subscriptions_while_idle(self):
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=100.0,
                feedback_keepalive_interval_s=0.02,
                gravity_feedforward_ratio=0.0,
            ),
            transport=transport,
        )
        service.start()
        try:
            service.enable()
            initial_request_count = len(transport.feedback_enabled)
            time.sleep(0.08)
        finally:
            service.close()

        keepalive_ids = transport.feedback_enabled[initial_request_count:]
        self.assertGreaterEqual(len(keepalive_ids), 12)
        self.assertTrue(all(motor_id in range(1, 7) for motor_id in keepalive_ids))

    def test_arm_motion_refreshes_stale_feedback_before_publishing_target(self):
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=100.0, gravity_feedforward_ratio=0.0), transport=transport
        )
        service.start()
        try:
            service.enable()
            time.sleep(0.40)
            command_count_before = len(transport.commands)
            service.move_j(JointPose.from_values((0.1, 0.0, 0.0, 0.0, 0.0, 0.0)), 0.04)
        finally:
            service.close()

        self.assertGreater(len(transport.commands), command_count_before)

    def test_arm_motion_never_publishes_new_target_when_feedback_refresh_fails(self):
        transport = NoArmFeedbackRefreshTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=100.0,
                feedback_keepalive_interval_s=0.2,
                enable_feedback_timeout_s=0.04,
                gravity_feedforward_ratio=0.0,
            ),
            transport=transport,
        )
        service.start()
        try:
            service.enable()
            transport.drop_arm_feedback_after_request = True
            command_count_before = len(transport.commands)
            with self.assertRaisesRegex(RuntimeError, "feedback refresh failed before motion command"):
                service.move_j(JointPose.from_values((0.1, 0.0, 0.0, 0.0, 0.0, 0.0)), 0.04)
        finally:
            service.close()

        new_commands = transport.commands[command_count_before:]
        self.assertTrue(new_commands)
        self.assertTrue(all(abs(command[1]) < 1e-9 for command in new_commands if command[0] == 1))

    def test_arm_motion_resends_target_once_after_transient_feedback_stall(self):
        transport = NoArmFeedbackRefreshTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=100.0,
                feedback_keepalive_interval_s=0.02,
                enable_feedback_timeout_s=1.0,
                gravity_feedforward_ratio=0.0,
            ),
            transport=transport,
        )
        service.start()
        try:
            service.enable()
            time.sleep(0.05)

            def pulse_stall() -> None:
                # 运动起步时反馈健康；0.10s 后开始停帧（执行中途冻结），停 0.6s
                # （超过默认 0.35s 新鲜度上限）后恢复。
                time.sleep(0.10)
                transport.drop_arm_feedback_after_request = True
                time.sleep(0.60)
                transport.drop_arm_feedback_after_request = False

            threading.Thread(target=pulse_stall, daemon=True).start()
            target = JointPose.from_values((0.2, 0.1, -0.1, 0.05, 0.0, 0.0))
            command_count_before = len(transport.commands)
            service.move_j(target, 0.8)
            measured = service.read_joint_pose()
        finally:
            service.close()

        streamed = [command for command in transport.commands[command_count_before:] if command[0] == 1]
        # 单条 0.8s@100Hz 轨迹约 81 帧；明显超过即证明中途冻结失败后重发了同一目标。
        self.assertGreater(len(streamed), 90)
        self.assertIsNone(service._arm_feedback_error)
        for actual, expected in zip(measured.values_rad, target.values_rad):
            self.assertAlmostEqual(actual, expected, places=2)

    def test_arm_motion_resend_happens_exactly_once_when_stall_persists(self):
        transport = NoArmFeedbackRefreshTransport()
        service = ArmService(
            ArmDeviceConfig(
                control_rate_hz=100.0,
                feedback_keepalive_interval_s=0.2,
                enable_feedback_timeout_s=0.04,
                gravity_feedforward_ratio=0.0,
            ),
            transport=transport,
        )
        service.start()
        try:
            service.enable()
            transport.drop_arm_feedback_after_request = True
            refresh_calls = 0
            real_refresh = service._refresh_arm_feedback

            def counting_refresh():
                nonlocal refresh_calls
                refresh_calls += 1
                return real_refresh()

            service._refresh_arm_feedback = counting_refresh
            with self.assertRaisesRegex(RuntimeError, "feedback refresh failed before motion command"):
                service.move_j(JointPose.from_values((0.1, 0.0, 0.0, 0.0, 0.0, 0.0)), 0.04)
        finally:
            service.close()

        self.assertEqual(refresh_calls, 2)

    def test_arm_motion_never_resends_non_stall_errors(self):
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=100.0, gravity_feedforward_ratio=0.0),
            transport=transport,
        )
        service.start()
        try:
            service.enable()
            calls = 0

            def dead_loop(_target, _duration_s):
                nonlocal calls
                calls += 1
                raise RuntimeError("arm 200 Hz control loop stopped: simulated")

            service._move_j = dead_loop
            with self.assertRaisesRegex(RuntimeError, "control loop stopped"):
                service.move_j(JointPose.from_values((0.1, 0.0, 0.0, 0.0, 0.0, 0.0)), 0.04)
        finally:
            service.close()

        self.assertEqual(calls, 1)

    def test_arm_health_reports_per_joint_feedback_age_and_keepalive_state(self):
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=100.0, gravity_feedforward_ratio=0.0), transport=transport
        )
        service.start()
        try:
            service.enable()
            health = service.health()
        finally:
            service.close()

        self.assertTrue(health["control_loop_running"])
        self.assertEqual(health["feedback_keepalive_interval_s"], 0.1)
        self.assertIsNotNone(health["last_arm_feedback_request_s"])
        self.assertEqual(set(health["joint_feedback"]), {str(motor_id) for motor_id in range(1, 8)})
        self.assertTrue(all(
            health["joint_feedback"][str(motor_id)]["age_s"] is not None
            for motor_id in range(1, 8)
        ))

    def test_arm_hold_loop_stops_before_explicit_safe_pose_disable(self):
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=100.0, gravity_feedforward_ratio=0.0), transport=transport
        )
        service.start()
        try:
            service.enable()
            result = service.disable_at_safe_pose(0.04)
            command_count_after_disable = len(transport.commands)
            time.sleep(0.04)
        finally:
            service.close()

        self.assertTrue(result["disable_command_sent"])
        self.assertFalse(service._control_running.is_set())
        self.assertEqual(len(transport.commands), command_count_after_disable)
        self.assertEqual(transport.disabled, [7] + list(range(1, 8)))

    def test_arm_service_close_preserves_motor_enable_state(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        service.start()
        service.enable()

        service.close()

        self.assertEqual(transport.disabled, [7])
        self.assertFalse(transport.connected)

    def test_arm_service_stop_preserves_motor_enable_state(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        service.start()
        service.enable()

        service.stop()

        self.assertEqual(transport.disabled, [7])
        self.assertFalse(transport.connected)

    def test_arm_disable_moves_to_safe_pose_confirms_feedback_then_disables(self):
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=50.0, final_tolerance_rad=0.01), transport=transport
        )
        service.start()
        try:
            service.enable()
            result = service.disable_at_safe_pose(0.04)
        finally:
            service.close()

        self.assertTrue(result["disable_command_sent"])
        self.assertTrue(result["moved_to_disable_pose"])
        self.assertEqual(transport.disabled, [7] + list(range(1, 8)))
        self.assertGreaterEqual(len(transport.commands), 6)
        self.assertTrue(all(
            abs(actual - expected) <= 0.01
            for actual, expected in zip(result["measured_joint_pose_rad"], DISABLE_JOINT.values_rad)
        ))

    def test_arm_disable_never_sends_disable_when_safe_pose_motion_fails(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        service.start()
        try:
            service.enable()
            with patch.object(service, "_move_j", side_effect=RuntimeError("safe-pose motion failed")):
                with self.assertRaisesRegex(RuntimeError, "safe-pose motion failed"):
                    service.disable_at_safe_pose(0.04)
        finally:
            service.close()

        self.assertEqual(transport.disabled, [7])

    def test_arm_disable_never_sends_disable_when_final_pose_feedback_is_out_of_tolerance(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        service.start()
        try:
            service.enable()
            with patch.object(service, "_move_j", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "final joint error"):
                    service.disable_at_safe_pose(0.04)
        finally:
            service.close()

        self.assertEqual(transport.disabled, [7])

    def test_arm_disable_never_sends_disable_when_motor_mode_changes_at_safe_pose(self):
        transport = FakeArmTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=50.0, final_tolerance_rad=0.01), transport=transport
        )
        all_enabled = {
            "all_enabled": True,
            "arm_enabled": True,
            "gripper_enabled": True,
            "joint_enabled": {str(motor_id): True for motor_id in range(1, 8)},
            "reason": None,
        }
        mode_changed = {
            "all_enabled": False,
            "arm_enabled": False,
            "gripper_enabled": True,
            "joint_enabled": {str(motor_id): motor_id != 1 for motor_id in range(1, 8)},
            "reason": "one or more joints are not in Motor mode",
        }
        service.start()
        try:
            service.enable()
            with patch.object(service, "_enable_status", side_effect=(all_enabled, mode_changed)):
                with self.assertRaisesRegex(RuntimeError, "state changed while moving"):
                    service.disable_at_safe_pose(0.04)
        finally:
            service.close()

        self.assertEqual(transport.disabled, [7])

    def test_arm_disable_reports_fully_disabled_without_sending_frames(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        service.start()
        try:
            result = service.disable_at_safe_pose()
        finally:
            service.close()

        self.assertTrue(result["already_disabled"])
        self.assertFalse(result["disable_command_sent"])
        self.assertEqual(transport.disabled, [])

    def test_arm_disable_rejects_partial_or_unknown_motor_state_without_disabling(self):
        partial_transport = FakeArmTransport()
        partial_transport.enable_motor(1)
        partial_service = ArmService(ArmDeviceConfig(), transport=partial_transport)
        partial_service.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "partially enabled"):
                partial_service.disable_at_safe_pose()
        finally:
            partial_service.close()
        self.assertEqual(partial_transport.disabled, [])

        unknown_transport = FakeArmTransport()
        for motor_id in range(1, 8):
            unknown_transport.enable_motor(motor_id)
        # An unknown ARM joint still refuses; an unknown gripper (joint 7) is
        # tolerated by the park-and-disable sequence now.
        del unknown_transport._feedback[3]
        unknown_service = ArmService(
            ArmDeviceConfig(enable_feedback_timeout_s=0.01), transport=unknown_transport
        )
        unknown_service.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "cannot determine enable state"):
                unknown_service.disable_at_safe_pose()
        finally:
            unknown_service.close()
        self.assertEqual(unknown_transport.disabled, [])

    def test_arm_enable_status_reports_mode_bits_without_enabling_motors(self):
        transport = FakeArmTransport()
        for motor_id in range(1, 8):
            item = transport._feedback[motor_id]
            transport._feedback[motor_id] = MotorFeedback(
                item.motor_id, item.position_rad, item.velocity_rad_s, item.torque_nm,
                item.temperature_c, item.fault_code, item.timestamp_s, 2,
            )
        service = ArmService(ArmDeviceConfig(), transport=transport)
        service.start()
        try:
            status = service.enable_status()
            self.assertTrue(service.is_enabled())
        finally:
            service.close()

        self.assertTrue(status["all_enabled"])
        self.assertEqual(status["joint_modes"], {str(index): "motor" for index in range(1, 8)})
        # Two status reads renew their own subscriptions; afterwards only the
        # adopted hold loop's 1--6 keepalive renewals may appear.
        self.assertEqual(transport.feedback_enabled[:14], list(range(1, 8)) * 2)
        self.assertTrue(
            all(motor_id in range(1, 7) for motor_id in transport.feedback_enabled[14:])
        )
        self.assertEqual(transport.enabled, [])

    def test_arm_service_uses_aligned_default_pd_gains(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        service.start()
        try:
            service.enable()
            service.move_j(JointPose.from_values((0.1, 0.0, 0.0, 0.0, 0.0, 0.0)), 0.04)
        finally:
            service.stop()

        self.assertTrue(transport.commands)
        self.assertEqual(transport.commands[0][3:5], (80.0, 4.0))

    def test_arm_motion_uses_sdk_coordinate_mapping_velocity_and_gravity(self):
        transport = FakeArmTransport()
        config = ArmDeviceConfig(
            joint_offsets={1: 0.1, 2: 0.2, 3: 0.3, 4: 0.4, 5: 0.5, 6: 0.6, 7: 0.7},
            gravity_feedforward_ratio=0.5,
            gravity_joint_scale={1: 2.0, 2: 1.0, 3: 0.5, 4: 1.0, 5: 1.5, 6: 1.0},
            velocity_limit_rad_s=0.25,
        )
        service = ArmService(config, transport=transport)
        target = JointPose.from_values((0.4, 0.5, -0.6, -0.7, 0.8, -0.9))
        velocities = (0.4, -0.3, 0.2, -0.1, 0.0, 0.5)

        with patch.object(service, "_gravity_torques", return_value=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0)):
            service._send_arm_target(target, velocities)

        expected = (
            (-0.3, -0.25, -1.0), (0.7, -0.25, 1.0), (0.9, -0.2, -0.75),
            (-0.3, -0.1, 2.0), (-0.3, 0.0, -3.75), (-0.3, 0.25, 3.0),
        )
        self.assertEqual([item[0] for item in transport.commands], list(range(1, 7)))
        self.assertTrue(all(item[3:5] == (80.0, 4.0) for item in transport.commands))
        for command, (position, velocity, torque) in zip(transport.commands, expected):
            self.assertAlmostEqual(command[1], position)
            self.assertAlmostEqual(command[2], velocity)
            self.assertAlmostEqual(command[5], torque)

    def test_arm_motion_reports_can_send_failure(self):
        transport = FailingMotionTransport()
        service = ArmService(ArmDeviceConfig(gravity_feedforward_ratio=0.0), transport=transport)

        with self.assertRaisesRegex(RuntimeError, "send motion target for motor 1"):
            service._send_arm_target(JointPose.from_values((0.1,) * 6), (0.0,) * 6)

    def test_gripper_motion_applies_offset_and_reports_can_send_failure(self):
        transport = FailingMotionTransport()
        service = ArmService(
            ArmDeviceConfig(joint_offsets={1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0, 6: 0.0, 7: 0.4}),
            transport=transport,
        )

        with self.assertRaisesRegex(RuntimeError, "send gripper motion target"):
            service._send_gripper_target(transport, 0.2, GripperCommand(), 0.0)

        self.assertEqual(len(transport.commands), 1)
        motor_id, position, velocity, kp, kd, torque = transport.commands[0]
        self.assertEqual((motor_id, velocity, kp, kd, torque), (7, 0.0, 40.0, 3.0, 0.0))
        self.assertAlmostEqual(position, 0.6)

    def test_joint_trajectory_is_synchronized_bounded_and_ends_exactly_at_target(self):
        start = JointPose.from_values((0.0,) * 6)
        target = JointPose.from_values((1.4, -0.8, 0.2, -0.1, 0.0, 0.5))
        points = list(joint_trajectory(
            start, target, duration_s=0.1, rate_hz=200.0,
            max_velocity_rad_s=0.7, max_acceleration_rad_s2=2.0, max_jerk_rad_s3=8.0,
        ))

        self.assertGreater(points[-1].time_s, 0.1)
        self.assertEqual(points[-1].position, target)
        self.assertEqual(points[-1].velocities_rad_s, (0.0,) * 6)
        self.assertTrue(all(
            abs(value) <= 0.7 + 1e-9
            for point in points for value in point.velocities_rad_s
        ))
        self.assertTrue(all(
            abs(value) <= 2.0 + 1e-9
            for point in points for value in point.accelerations_rad_s2
        ))

    def test_arm_service_moves_to_built_in_poses(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(control_rate_hz=50.0), transport=transport)
        service.start()
        try:
            service.enable()
            service.move_to_zero(0.04)
            self.assertEqual(service.read_joint_pose(), ZERO_JOINT)

            service.move_to_book_debug(0.04)
            # The real control loop samples asynchronously.  A very short
            # test trajectory may be observed one control tick before the
            # exact final setpoint, while still well inside the configured
            # arrival tolerance.  Test the physical contract, not scheduler
            # timing or bit-for-bit floating point equality.
            for actual, expected in zip(
                service.read_joint_pose().values_rad,
                BOOK_DEBUG_JOINT.values_rad,
            ):
                self.assertAlmostEqual(actual, expected, places=3)

            service.move_to_disable_pose(0.04)
            for actual, expected in zip(service.read_joint_pose().values_rad, DISABLE_JOINT.values_rad):
                self.assertAlmostEqual(actual, expected, places=9)
        finally:
            service.stop()

    def test_arm_service_built_in_pose_helpers_forward_durations(self):
        service = ArmService(ArmDeviceConfig())

        with patch.object(service, "move_j") as move_j:
            service.move_to_zero()
            service.move_to_book_debug(1.5)
            service.move_to_disable_pose(2.5)

        self.assertEqual(
            move_j.call_args_list,
            [call(ZERO_JOINT, 3.0), call(BOOK_DEBUG_JOINT, 1.5), call(DISABLE_JOINT, 2.5)],
        )

    def test_arm_service_tcp_pose_matches_direct_kinematics(self):
        service = ArmService(ArmDeviceConfig())
        joints = JointPose.from_values((0.1, -0.2, 0.3, -0.4, 0.5, -0.6))

        pose = service.tcp_pose(joints)

        expected = PinocchioKinematics(
            service.config.urdf_path,
            service.config.tcp_offset,
            joint_limits=service.config.joint_limits,
        ).forward_kinematics(joints)
        self.assertEqual(len(pose.values()), 6)
        for actual, wanted in zip(pose.values(), expected.values()):
            self.assertAlmostEqual(actual, wanted, places=9)

    def test_open_gripper_accepts_explicit_angle_without_command(self):
        service = ArmService(ArmDeviceConfig())

        with (
            patch.object(service._worker, "call", side_effect=lambda action, timeout_s: action()),
            patch.object(service, "_move_gripper") as move_gripper,
        ):
            service.open_gripper(42.5)

        target_deg, command = move_gripper.call_args.args[:2]
        self.assertEqual(target_deg, 42.5)
        self.assertIsInstance(command, GripperCommand)
        self.assertFalse(move_gripper.call_args.kwargs["monitored"])

    def test_gripper_close_progressively_reaches_target(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        command = GripperCommand(
            close_deg=20.0,
            close_speed_deg_s=720.0,
            timeout_s=1.0,
            monitor_period_s=0.01,
            target_tolerance_deg=0.1,
            stall_tolerance_deg=0.05,
            stall_time_s=0.05,
            min_monitor_s=0.0,
            hold_margin_deg=0.1,
            command_lead_s=0.1,
            stall_lead_threshold_deg=0.5,
        )
        service.start()
        try:
            service.enable()
            result = service.close_gripper_monitored(command)
        finally:
            service.close()

        gripper_commands = [item[1] for item in transport.commands if item[0] == 7]
        target_rad = math.radians(command.close_deg)
        self.assertEqual(result["reason"], "target_reached")
        self.assertGreaterEqual(len(gripper_commands), 3)
        self.assertGreater(gripper_commands[0], 0.0)
        self.assertLess(gripper_commands[0], target_rad)
        self.assertTrue(all(left <= right for left, right in zip(gripper_commands, gripper_commands[1:])))
        # The control loop may already have re-sent the low-torque carry frame.
        carry_rad = target_rad + math.radians(command.carry_lead_deg)
        self.assertTrue(
            math.isclose(gripper_commands[-1], target_rad, abs_tol=1e-9)
            or math.isclose(gripper_commands[-1], carry_rad, abs_tol=1e-9)
        )

    def test_gripper_open_progressively_reaches_and_confirms_target(self):
        transport = FakeArmTransport()
        item = transport._feedback[7]
        transport._feedback[7] = MotorFeedback(
            7, math.radians(80.0), item.velocity_rad_s, item.torque_nm,
            item.temperature_c, item.fault_code, item.timestamp_s, item.mode_state,
        )
        service = ArmService(ArmDeviceConfig(), transport=transport)
        command = GripperCommand(
            open_deg=5.0,
            open_speed_deg_s=720.0,
            timeout_s=1.0,
            monitor_period_s=0.01,
            target_tolerance_deg=0.1,
            stall_tolerance_deg=0.05,
            stall_time_s=0.05,
            min_monitor_s=0.0,
            hold_margin_deg=0.1,
            command_lead_s=0.1,
            stall_lead_threshold_deg=0.5,
        )
        target_deg = 30.0
        service.start()
        try:
            service.enable()
            service.open_gripper(target_deg, command)
        finally:
            service.close()

        gripper_commands = [item[1] for item in transport.commands if item[0] == 7]
        target_rad = math.radians(target_deg)
        self.assertGreaterEqual(len(gripper_commands), 3)
        self.assertTrue(all(left >= right for left, right in zip(gripper_commands, gripper_commands[1:])))
        self.assertAlmostEqual(gripper_commands[-1], target_rad, places=9)

    def test_gripper_command_refreshes_feedback_without_reenabling_motors(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        command = GripperCommand(open_deg=30.0, open_speed_deg_s=720.0)
        service.start()
        try:
            service.enable()
            enabled_before = list(transport.enabled)
            disabled_before = list(transport.disabled)
            service.open_gripper(command.open_deg, command)
        finally:
            service.close()

        self.assertEqual(transport.enabled, enabled_before)
        self.assertEqual(transport.disabled, disabled_before)
        self.assertIn(7, transport.feedback_enabled)

    def test_gripper_never_sends_motion_when_feedback_refresh_fails(self):
        transport = NoGripperFeedbackRefreshTransport()
        service = ArmService(
            ArmDeviceConfig(enable_feedback_timeout_s=0.01), transport=transport
        )
        service.start()
        try:
            service.enable()
            gripper_commands_before = [item for item in transport.commands if item[0] == 7]
            with self.assertRaisesRegex(RuntimeError, "feedback refresh failed.*no gripper motion was sent"):
                service.open_gripper(30.0, GripperCommand(timeout_s=0.1))
        finally:
            service.close()

        self.assertEqual(
            [item for item in transport.commands if item[0] == 7], gripper_commands_before
        )

    def test_gripper_open_reports_no_movement_instead_of_succeeding_silently(self):
        transport = StalledGripperTransport()
        item = transport._feedback[7]
        transport._feedback[7] = MotorFeedback(
            7, math.radians(80.0), item.velocity_rad_s, item.torque_nm,
            item.temperature_c, item.fault_code, item.timestamp_s, item.mode_state,
        )
        service = ArmService(ArmDeviceConfig(), transport=transport)
        command = GripperCommand(
            open_deg=30.0,
            open_speed_deg_s=720.0,
            timeout_s=0.1,
            monitor_period_s=0.01,
            target_tolerance_deg=0.1,
            stall_tolerance_deg=0.05,
            stall_time_s=0.02,
            hold_margin_deg=0.5,
            command_lead_s=0.1,
            stall_lead_threshold_deg=0.5,
        )
        service.start()
        try:
            service.enable()
            with self.assertRaisesRegex(RuntimeError, "gripper open failed.*(object_contact|timeout)"):
                service.open_gripper(command.open_deg, command)
        finally:
            service.close()

    def test_gripper_close_object_contact_sends_repeated_tighter_hold_commands(self):
        transport = StalledGripperTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        command = GripperCommand(
            close_deg=80.0,
            close_speed_deg_s=720.0,
            timeout_s=1.0,
            monitor_period_s=0.01,
            target_tolerance_deg=0.1,
            stall_tolerance_deg=0.05,
            stall_time_s=0.03,
            hold_margin_deg=0.5,
            command_lead_s=0.1,
            stall_lead_threshold_deg=0.5,
            contact_hold_offset_deg=2.0,
            contact_hold_duration_s=0.05,
            contact_hold_rate_hz=100.0,
        )
        service.start()
        try:
            service.enable()
            result = service.close_gripper_monitored(command)
        finally:
            service.close()

        gripper_commands = [item[1] for item in transport.commands if item[0] == 7]
        contact_offset_rad = math.radians(command.contact_hold_offset_deg)
        self.assertEqual(result["reason"], "object_contact")
        self.assertGreater(len(gripper_commands), 1)
        self.assertAlmostEqual(float(result["final_angle_rad"]), 0.0, places=9)
        self.assertAlmostEqual(float(result["command_angle_rad"]), contact_offset_rad, places=9)
        # The control loop may already have re-sent the low-torque carry frame.
        carry_rad = contact_offset_rad + math.radians(command.carry_lead_deg)
        self.assertTrue(
            math.isclose(gripper_commands[-1], contact_offset_rad, abs_tol=1e-9)
            or math.isclose(gripper_commands[-1], carry_rad, abs_tol=1e-9)
        )
        self.assertTrue(result["contact_hold_applied"])
        self.assertEqual(result["contact_hold_offset_deg"], 2.0)
        self.assertEqual(result["contact_hold_duration_s"], 0.05)
        self.assertEqual(result["contact_hold_rate_hz"], 100.0)
        self.assertGreaterEqual(result["contact_hold_command_count"], 2)

    def test_gripper_carry_hold_repeats_low_torque_frames_after_contact(self):
        transport = StalledGripperTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=200.0), transport=transport
        )
        command = GripperCommand(
            close_deg=80.0,
            close_speed_deg_s=720.0,
            timeout_s=1.0,
            monitor_period_s=0.01,
            target_tolerance_deg=0.1,
            stall_tolerance_deg=0.05,
            stall_time_s=0.03,
            hold_margin_deg=0.5,
            command_lead_s=0.1,
            stall_lead_threshold_deg=0.5,
            contact_hold_offset_deg=2.0,
            contact_hold_duration_s=0.05,
            contact_hold_rate_hz=100.0,
            carry_effort=0.2,
            carry_lead_deg=1.0,
        )
        service.start()
        try:
            service.enable()
            result = service.close_gripper_monitored(command)
            self.assertEqual(result["reason"], "object_contact")
            self.assertTrue(result["carry_hold"])
            expected_target = math.radians(
                command.contact_hold_offset_deg + command.carry_lead_deg
            )
            self.assertAlmostEqual(result["carry_target_angle_rad"], expected_target, places=9)
            self.assertAlmostEqual(
                service._gripper_carry["target_rad"], expected_target, places=9
            )
            self.assertEqual(service._gripper_carry["effort"], 0.2)
            # 25 Hz carry frames from the control loop, not the closing effort.
            time.sleep(0.3)
        finally:
            service.close()

        carry_frames = [
            item
            for item in transport.commands
            if item[0] == 7
            and math.isclose(item[1], expected_target, abs_tol=1e-9)
            and item[5] == 0.2
        ]
        self.assertGreaterEqual(len(carry_frames), 3)
        self.assertTrue(
            all(item[3] == command.kp and item[4] == command.kd for item in carry_frames)
        )

    def test_gripper_open_and_disable_clear_the_carry_hold(self):
        transport = StalledGripperTransport()
        service = ArmService(
            ArmDeviceConfig(control_rate_hz=200.0), transport=transport
        )
        command = GripperCommand(
            close_deg=80.0,
            close_speed_deg_s=720.0,
            timeout_s=1.0,
            monitor_period_s=0.01,
            target_tolerance_deg=0.1,
            stall_tolerance_deg=0.05,
            stall_time_s=0.03,
            hold_margin_deg=0.5,
            command_lead_s=0.1,
            stall_lead_threshold_deg=0.5,
            contact_hold_offset_deg=2.0,
            contact_hold_duration_s=0.05,
            contact_hold_rate_hz=100.0,
        )
        service.start()
        try:
            service.enable()
            service.close_gripper_monitored(command)
            self.assertIsNotNone(service._gripper_carry)

            # A new open supersedes the carry hold immediately.
            service.open_gripper(0.0, command)
            self.assertIsNone(service._gripper_carry)

            # Re-arm, then the checked disable sequence clears it too.
            service.close_gripper_monitored(command)
            self.assertIsNotNone(service._gripper_carry)
            service.disable_at_safe_pose(0.05)
            self.assertIsNone(service._gripper_carry)
        finally:
            service.close()

    def test_gripper_close_timeout_does_not_arm_the_carry_hold(self):
        transport = StalledGripperTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        command = GripperCommand(
            close_deg=30.0,
            close_speed_deg_s=720.0,
            timeout_s=0.05,
            monitor_period_s=0.01,
            target_tolerance_deg=0.1,
            stall_tolerance_deg=0.05,
            stall_time_s=0.02,
            min_monitor_s=1.0,
            hold_margin_deg=0.5,
            command_lead_s=0.1,
            stall_lead_threshold_deg=0.5,
        )
        service.start()
        try:
            service.enable()
            result = service.close_gripper_monitored(command)
            self.assertEqual(result["reason"], "timeout")
            self.assertNotIn("carry_hold", result)
            self.assertIsNone(service._gripper_carry)
        finally:
            service.close()

    def test_gripper_close_timeout_holds_near_feedback_when_contact_is_not_yet_eligible(self):
        transport = StalledGripperTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        command = GripperCommand(
            close_deg=30.0,
            close_speed_deg_s=720.0,
            timeout_s=0.05,
            monitor_period_s=0.01,
            target_tolerance_deg=0.1,
            stall_tolerance_deg=0.05,
            stall_time_s=0.01,
            hold_margin_deg=0.5,
            command_lead_s=0.1,
            min_monitor_s=0.1,
        )
        service.start()
        try:
            service.enable()
            result = service.close_gripper_monitored(command)
        finally:
            service.close()

        gripper_commands = [item[1] for item in transport.commands if item[0] == 7]
        hold_rad = math.radians(command.hold_margin_deg)
        self.assertEqual(result["reason"], "timeout")
        self.assertAlmostEqual(float(result["final_angle_rad"]), 0.0, places=9)
        self.assertAlmostEqual(float(result["command_angle_rad"]), hold_rad, places=9)
        self.assertAlmostEqual(gripper_commands[-1], hold_rad, places=9)

    def test_gripper_close_default_contact_threshold_is_reachable(self):
        transport = StalledGripperTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        command = GripperCommand(
            close_deg=80.0,
            close_speed_deg_s=720.0,
            timeout_s=1.0,
            monitor_period_s=0.01,
            min_monitor_s=0.0,
            start_boost_s=0.0,
            stall_time_s=0.03,
        )
        service.start()
        try:
            service.enable()
            result = service.close_gripper_monitored(command)
        finally:
            service.close()

        self.assertEqual(result["reason"], "object_contact")

    def test_gripper_close_uses_motion_based_timeout(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        command = GripperCommand(
            close_deg=20.0,
            close_speed_deg_s=10.0,
            timeout_s=10.0,
            close_timeout_margin_s=1.5,
        )
        service.start()
        try:
            service.enable()
            result = service.close_gripper_monitored(command)
        finally:
            service.close()

        self.assertAlmostEqual(result["expected_travel_s"], 2.0, places=5)
        self.assertAlmostEqual(result["effective_timeout_s"], 3.5, places=5)

    def test_aim_lift_service_matches_move_height_sdk_defaults(self):
        drive = FakeAimDrive()
        platform = FakeLiftPlatform(drive)
        service = AimLiftService(LiftDeviceConfig(), platform=platform, drive=drive)
        service.start()
        try:
            result = service.move_to_height_mm(300.0, speed_level=2)

            self.assertIsInstance(result, LiftPositionResult)
            # The SDK move is issued without its own wait: the service runs the
            # same 45 s / 10-pulse arrival wait itself so a safety halt is felt
            # within one poll cycle.
            self.assertEqual(platform.moves[0][:3], (30.0, 2, False))
            self.assertEqual(platform.moves[0][3:5], (45.0, 10))
            self.assertEqual(platform.moves[0][-1], True)
            self.assertAlmostEqual(service.read_lift_height_mm(), 300.0)
            self.assertEqual(result.controller_measured_pulses, 3000)
            self.assertEqual(result.tolerance_pulses, 10)
            self.assertTrue(drive.enabled_modbus)
            self.assertTrue(drive.enabled_output)
        finally:
            service.stop()
        self.assertTrue(drive.closed)
        self.assertFalse(drive.enabled_modbus)
        self.assertFalse(drive.enabled_output)

    def test_aim_lift_service_rejects_targets_outside_calibrated_range(self):
        drive = FakeAimDrive()
        platform = FakeLiftPlatform(drive)
        service = AimLiftService(LiftDeviceConfig(), platform=platform, drive=drive)
        service.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "invalid_target.*1..170"):
                service.move_to_height_mm(5.0, speed_level=1)
            self.assertEqual(platform.moves, [])
            self.assertFalse(drive.enabled_output)
        finally:
            service.stop()

    def test_aim_lift_service_rejects_invalid_speed_before_enabling(self):
        drive = FakeAimDrive()
        platform = FakeLiftPlatform(drive)
        service = AimLiftService(LiftDeviceConfig(), platform=platform, drive=drive)
        service.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "invalid_speed"):
                service.move_to_height_mm(300.0, speed_level=4)
            self.assertEqual(platform.moves, [])
            self.assertFalse(drive.enabled_output)
        finally:
            service.stop()

    def test_aim_lift_service_drives_simulated_aim_absolute_position(self):
        from aim_sdk import AimDrive, LiftPlatform, SimulatedAimDevice, SimulatedTransport

        device = SimulatedAimDevice(address=1)
        drive = AimDrive.from_transport(SimulatedTransport(device), address=1)
        service = AimLiftService(LiftDeviceConfig(), platform=LiftPlatform(drive), drive=drive)
        service.start()
        try:
            result = service.move_to_height_mm(300.0, speed_level=2)

            self.assertAlmostEqual(result.measured_height_mm, 300.0, places=2)
            self.assertEqual(result.controller_target_pulses, result.controller_measured_pulses)
            self.assertGreater(result.controller_target_pulses, 0)
        finally:
            service.stop()

    def test_rod_service_rejects_targets_outside_safe_angle_range(self):
        service = RodMotorService(RodDeviceConfig())

        for target_deg in (-100.1, 150.1, float("nan")):
            with self.assertRaisesRegex(ValueError, "between -100.0 and 150.0"):
                service.move_to_deg(target_deg, 1000, 50, 1.5, 1.0)

    def test_rod_service_accepts_safe_angle_range_endpoints(self):
        serial_port = FakeRodSerial()
        service = RodMotorService(RodDeviceConfig(), serial_port=serial_port)
        service.start()
        try:
            service.move_to_deg(-100.0, 1000, 50, 1.5, 1.0)
            service.move_to_deg(150.0, 1000, 50, 1.5, 1.0)
        finally:
            service.stop()

        self.assertEqual(
            [message["rad"] for message in serial_port.messages if message["T"] == RodMotorService.WRITE_ANGLE],
            [math.radians(-100.0), math.radians(150.0)],
        )

    def test_rod_reconnects_once_after_eio_and_completes_the_move(self):
        created = []

        def factory():
            port = FlakyRodSerial(fail_writes=1 if not created else 0)
            created.append(port)
            return port

        service = RodMotorService(RodDeviceConfig(), serial_factory=factory)
        service.reconnect_settle_s = 0.0
        service.start()
        try:
            service.move_to_deg(10.0, 1000, 50, 1.5, 2.0)
        finally:
            service.stop()

        self.assertEqual(service.reconnects, 1)
        self.assertEqual(len(created), 2)
        self.assertTrue(created[0].is_open is False)
        self.assertAlmostEqual(created[-1].last_target_rad, math.radians(10.0))

    def test_rod_reports_unrecovered_when_reconnect_also_fails(self):
        def factory():
            return FlakyRodSerial(fail_writes=99)

        service = RodMotorService(RodDeviceConfig(), serial_factory=factory)
        service.reconnect_settle_s = 0.0
        service.start()
        try:
            with self.assertRaisesRegex(OSError, "auto-reconnect failed once"):
                service.move_to_deg(10.0, 1000, 50, 1.5, 1.0)
        finally:
            service.stop()

        self.assertEqual(service.reconnects, 1)

    def test_lift_reconnects_once_after_eio_and_completes_the_move(self):
        created = []

        def factory():
            drive = FlakyAimDrive(fail_reads=1 if not created else 0)
            created.append(drive)
            return drive, FakeLiftPlatform(drive)

        service = AimLiftService(LiftDeviceConfig(), drive_factory=factory)
        service.reconnect_settle_s = 0.0
        service.start()
        try:
            result = service.move_to_height_mm(300.0, speed_level=2)
        finally:
            service.stop()

        self.assertEqual(service.reconnects, 1)
        self.assertTrue(created[0].closed)
        self.assertAlmostEqual(result.measured_height_mm, 300.0)

    def test_lift_reports_unrecovered_when_reconnect_also_fails(self):
        def factory():
            drive = FlakyAimDrive(fail_reads=99)
            return drive, FakeLiftPlatform(drive)

        service = AimLiftService(LiftDeviceConfig(), drive_factory=factory)
        service.reconnect_settle_s = 0.0
        service.start()
        try:
            with self.assertRaisesRegex(LiftPositioningError, "auto-reconnect failed once"):
                service.move_to_height_mm(300.0, speed_level=2)
        finally:
            service.stop()

        self.assertEqual(service.reconnects, 1)

    def test_lift_non_communication_errors_do_not_reconnect(self):
        def factory():
            drive = FakeAimDrive()
            return drive, FakeLiftPlatform(drive)

        service = AimLiftService(LiftDeviceConfig(), drive_factory=factory)
        service.reconnect_settle_s = 0.0
        service.start()
        try:
            # Out-of-range height is a validation error, not a serial failure.
            with self.assertRaisesRegex(LiftPositioningError, "invalid_target"):
                service.move_to_height_mm(5.0, speed_level=2)
        finally:
            service.stop()

        self.assertEqual(service.reconnects, 0)

    def test_arm_emergency_stop_disables_all_motors_without_queueing(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        service.start()
        try:
            service.enable()
            self.assertTrue(service.is_enabled())
            disabled_before = list(transport.disabled)

            stopped = service.emergency_stop()

            self.assertTrue(stopped)
            self.assertEqual(set(transport.disabled[len(disabled_before):]), set(range(1, 8)))
            self.assertFalse(service.is_enabled())
            with self.assertRaisesRegex(RuntimeError, "arm is not enabled"):
                service.move_j(ZERO_JOINT, 0.1)
            # The arm recovers only through an explicit re-enable.
            service.enable()
            self.assertTrue(service.is_enabled())
        finally:
            service.stop()

    def test_arm_emergency_stop_aborts_in_flight_motion(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        service.start()
        try:
            service.enable()
            errors = []
            motion = threading.Thread(
                target=lambda: errors.append(
                    _run_capturing(lambda: service.move_j(BOOK_DEBUG_JOINT, 30.0))
                ),
                daemon=True,
            )
            motion.start()
            time.sleep(0.3)

            stopped = service.emergency_stop()
            motion.join(timeout=5.0)

            self.assertTrue(stopped)
            self.assertFalse(motion.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RuntimeError)
            self.assertRegex(str(errors[0]), "interrupted|control loop")
        finally:
            service.stop()

    def test_arm_motion_recovers_from_interrupt_with_new_command(self):
        transport = FakeArmTransport()
        service = ArmService(ArmDeviceConfig(), transport=transport)
        service.start()
        try:
            service.enable()
            service.interrupt_motion()

            # A stale interrupt must not brick later commanded motion.
            service.move_j(BOOK_DEBUG_JOINT, 0.5)

            self.assertEqual(
                service.read_joint_pose().values_rad, BOOK_DEBUG_JOINT.values_rad
            )
        finally:
            service.stop()

    def test_lift_emergency_stop_aborts_in_flight_move(self):
        drive = StoppableAimDrive()
        platform = HangingLiftPlatform(drive)
        service = AimLiftService(LiftDeviceConfig(), platform=platform, drive=drive)
        service.start()
        try:
            errors = []
            mover = threading.Thread(
                target=lambda: errors.append(
                    _run_capturing(lambda: service.move_to_height_mm(300.0, speed_level=2))
                ),
                daemon=True,
            )
            mover.start()
            deadline = time.monotonic() + 2.0
            while not platform.moves and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(platform.moves)

            self.assertTrue(service.emergency_stop())
            mover.join(timeout=5.0)

            self.assertFalse(mover.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], LiftPositioningError)
            self.assertEqual(errors[0].code, "interrupted")
            self.assertGreaterEqual(drive.emergency_stops, 1)
        finally:
            service.stop()

    def test_lift_move_recovers_after_emergency_stop(self):
        drive = StoppableAimDrive()
        platform = FakeLiftPlatform(drive)
        service = AimLiftService(LiftDeviceConfig(), platform=platform, drive=drive)
        service.start()
        try:
            self.assertTrue(service.emergency_stop())

            result = service.move_to_height_mm(300.0, speed_level=2)

            self.assertEqual(result.controller_measured_pulses, 3000)
        finally:
            service.stop()

    def test_lift_emergency_stop_without_drive_returns_false(self):
        service = AimLiftService(LiftDeviceConfig())

        self.assertFalse(service.emergency_stop())

    def test_d435_frame_deprojects_nearest_valid_depth(self):
        import numpy as np

        frame = D435Frame(
            color_bgr=np.zeros((3, 3, 3), dtype=np.uint8),
            depth_m=np.array([[0.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 0.0]], dtype=float),
            fx=100.0, fy=100.0, ppx=1.0, ppy=1.0, timestamp_ms=0.0, frame_number=1,
        )

        self.assertEqual(frame.point_at_pixel(0, 0, search_radius=1), (0.0, 0.0, 2.0))
        self.assertEqual(frame.point_cloud(max_depth_m=2.0).shape, (1, 3))

    def test_d435_frame_escalates_search_radius_past_depth_hole(self):
        import numpy as np

        # 41x41 frame: one valid pixel 20 left of center, outside the default
        # radius-5 window but inside the first escalation step (15 -> 35).
        depth_m = np.zeros((41, 41), dtype=float)
        depth_m[20, 0] = 2.0
        frame = D435Frame(
            color_bgr=np.zeros((41, 41, 3), dtype=np.uint8),
            depth_m=depth_m,
            fx=100.0, fy=100.0, ppx=1.0, ppy=1.0, timestamp_ms=0.0, frame_number=1,
        )

        self.assertEqual(frame.point_at_pixel(20, 20), (-0.02, 0.38, 2.0))

    def test_d435_frame_raises_when_no_valid_depth_anywhere(self):
        import numpy as np

        frame = D435Frame(
            color_bgr=np.zeros((9, 9, 3), dtype=np.uint8),
            depth_m=np.zeros((9, 9), dtype=float),
            fx=100.0, fy=100.0, ppx=1.0, ppy=1.0, timestamp_ms=0.0, frame_number=1,
        )

        with self.assertRaisesRegex(RuntimeError, "no valid D435 depth"):
            frame.point_at_pixel(4, 4)

    def test_camera_transform_maps_camera_point_to_base(self):
        transform = CameraToBaseTransform([
            [0.0, 0.0, -1.0, 0.15],
            [1.0, 0.0, 0.0, -0.29],
            [0.0, -1.0, 0.0, 0.17],
            [0.0, 0.0, 0.0, 1.0],
        ])

        self.assertEqual(
            tuple(round(value, 6) for value in transform.transform_point((0.1, 0.2, 0.3))),
            (-0.15, -0.19, -0.03),
        )

    def test_camera_pose6_round_trips_through_matrix_helpers(self):
        composed = pose6_to_matrix(
            0.152, -0.248, 0.165,
            math.radians(-89.5), math.radians(0.2), math.radians(90.1),
        )

        x_m, y_m, z_m, rx, ry, rz = matrix_to_pose6(composed)
        recomposed = pose6_to_matrix(x_m, y_m, z_m, rx, ry, rz)

        self.assertAlmostEqual(x_m, 0.152)
        self.assertAlmostEqual(y_m, -0.248)
        self.assertAlmostEqual(z_m, 0.165)
        self.assertAlmostEqual(rx, math.radians(-89.5))
        self.assertAlmostEqual(ry, math.radians(0.2))
        self.assertAlmostEqual(rz, math.radians(90.1))
        for actual_row, expected_row in zip(recomposed, composed):
            for actual, expected in zip(actual_row, expected_row):
                self.assertAlmostEqual(actual, expected, places=12)

    def test_d435_recovers_frames_after_rebuilding_pipeline_twice(self):
        device = _RecoverableD435([["timeout"], ["timeout"], ["ok"]])

        device.start()
        device.warmup(1, 100)

        status = device.pipeline_recovery_status()
        self.assertEqual(status["pipeline_recoveries"], 2)
        self.assertIsNotNone(status["last_recovery_at"])
        self.assertEqual(len(device.pipelines), 3)
        self.assertEqual(device.pipelines[0].stop_calls, 1)
        self.assertEqual(device.pipelines[1].stop_calls, 1)
        self.assertEqual(sum(pipeline.wait_calls for pipeline in device.pipelines), 3)

    def test_d435_raises_after_three_pipeline_rebuilds(self):
        device = _RecoverableD435([["timeout"], ["timeout"], ["timeout"], ["timeout"]])

        device.start()
        with self.assertRaisesRegex(RuntimeError, "auto-rebuilt 3 times"):
            device.warmup(1, 100)

        self.assertEqual(device.pipeline_recovery_status()["pipeline_recoveries"], 3)
        self.assertEqual(len(device.pipelines), 4)
        self.assertEqual(
            sum(pipeline.wait_calls for pipeline in device.pipelines), 4
        )

    def test_d435_non_timeout_errors_do_not_trigger_rebuild(self):
        device = _RecoverableD435([["boom"]])

        device.start()
        with self.assertRaisesRegex(RuntimeError, "some other failure"):
            device.warmup(1, 100)

        self.assertEqual(device.pipeline_recovery_status()["pipeline_recoveries"], 0)
        self.assertEqual(len(device.pipelines), 1)

    def test_d435_rebuilds_when_frame_number_stops_advancing(self):
        device = _RecoverableD435(
            [[("frame", 100), ("frame", 100), ("frame", 100)], [("frame", 101)]]
        )

        device.start()
        device.warmup(4, 100)

        self.assertEqual(device.pipeline_recovery_status()["pipeline_recoveries"], 1)
        self.assertEqual(len(device.pipelines), 2)
        self.assertEqual(device.pipelines[0].stop_calls, 1)

    def test_d435_does_not_rebuild_when_frame_numbers_advance(self):
        device = _RecoverableD435(
            [[("frame", 100), ("frame", 101), ("frame", 102), ("frame", 103)]]
        )

        device.start()
        device.warmup(4, 100)

        self.assertEqual(device.pipeline_recovery_status()["pipeline_recoveries"], 0)
        self.assertEqual(len(device.pipelines), 1)

    def test_d435_raises_after_three_rebuilds_on_stale_frames(self):
        stuck = [("frame", 100), ("frame", 100), ("frame", 100)]
        device = _RecoverableD435([stuck, stuck, stuck, stuck])

        device.start()
        with self.assertRaisesRegex(RuntimeError, "auto-rebuilt 3 times"):
            device.warmup(12, 100)

        self.assertEqual(device.pipeline_recovery_status()["pipeline_recoveries"], 3)
        self.assertEqual(len(device.pipelines), 4)


class _FakeRsFrame:
    def __init__(self, number: int) -> None:
        self._number = number

    def get_frame_number(self) -> int:
        return self._number


class _FakeRsFrameset:
    def __init__(self, number: int) -> None:
        self._color = _FakeRsFrame(number)

    def get_color_frame(self) -> _FakeRsFrame:
        return self._color


class _FakeRsPipeline:
    """Scripted wait_for_frames outcomes; 'timeout' raises like librealsense."""

    def __init__(self, script):
        self.script = list(script)
        self.wait_calls = 0
        self.stop_calls = 0
        self._next_frame_number = 0

    def wait_for_frames(self, timeout_ms):
        self.wait_calls += 1
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "timeout":
            raise RuntimeError("Frame didn't arrive within 5000")
        if outcome == "boom":
            raise RuntimeError("some other failure")
        if isinstance(outcome, tuple) and outcome[0] == "frame":
            return _FakeRsFrameset(int(outcome[1]))
        self._next_frame_number += 1
        return _FakeRsFrameset(self._next_frame_number)

    def stop(self):
        self.stop_calls += 1


class _RecoverableD435(D435Device):
    """D435Device whose pipeline factory hands out scripted fakes."""

    def __init__(self, scripts):
        super().__init__(serial="fake")
        self.recovery_settle_s = 0.0
        self._scripts = list(scripts)
        self.pipelines = []

    def _open_pipeline(self):
        pipeline = _FakeRsPipeline(self._scripts.pop(0) if self._scripts else ["ok"])
        self.pipelines.append(pipeline)
        self._pipeline = pipeline
        self._profile = object()
        self._rs = None
        self._align = None
        self._depth_scale = 0.001


if __name__ == "__main__":
    unittest.main()
