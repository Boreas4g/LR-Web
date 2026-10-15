"""CLI safety behavior tests for the direct arm utility."""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "arm" / "arm_move.py"
_SPEC = importlib.util.spec_from_file_location("bookarm_arm_move_test", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
arm_move = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(arm_move)


class FakeArmService:
    instances: list["FakeArmService"] = []

    def __init__(self, config: object) -> None:
        self.started = False
        self.closed = False
        self.disable_requested = False
        FakeArmService.instances.append(self)

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def disable_at_safe_pose(self, duration_s: float = 3.0) -> dict[str, object]:
        self.disable_requested = True
        self.disable_duration_s = duration_s
        return {"disable_command_sent": True}

    def enable(self) -> None:
        pass

    def move_to_pose_by_move_j(self, target, duration_s: float) -> None:
        self.target = target
        self.duration_s = duration_s

    def read_joint_pose(self):
        return type("Pose", (), {"values_rad": (0.0,) * 6})()

    def health(self) -> dict[str, object]:
        return {"healthy_feedback": True}

    def enable_status(self) -> dict[str, object]:
        return {"all_enabled": True}


class ArmMoveSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeArmService.instances.clear()

    def test_normal_exit_closes_transport_without_stopping_motors(self) -> None:
        with patch.object(arm_move, "ArmService", FakeArmService), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(arm_move.main(["health"]), 0)

        arm = FakeArmService.instances[0]
        self.assertTrue(arm.closed)
        self.assertFalse(arm.disable_requested)

    def test_enable_prompts_and_stops_when_user_confirms(self) -> None:
        with (
            patch.object(arm_move, "ArmService", FakeArmService),
            patch.object(arm_move.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="y") as prompt,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(arm_move.main(["enable", "--confirm"]), 0)

        arm = FakeArmService.instances[0]
        prompt.assert_called_once()
        self.assertTrue(arm.disable_requested)
        self.assertTrue(arm.closed)

    def test_enable_keeps_motors_enabled_when_user_declines(self) -> None:
        with patch.object(arm_move, "ArmService", FakeArmService), contextlib.redirect_stdout(io.StringIO()):
            with patch.object(arm_move.sys.stdin, "isatty", return_value=True), patch(
                "builtins.input", return_value="n"
            ):
                self.assertEqual(arm_move.main(["enable", "--confirm"]), 0)

        arm = FakeArmService.instances[0]
        self.assertFalse(arm.disable_requested)
        self.assertTrue(arm.closed)

    def test_noninteractive_enable_keeps_motors_enabled(self) -> None:
        with patch.object(arm_move, "ArmService", FakeArmService), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(arm_move.main(["enable", "--confirm"]), 0)

        arm = FakeArmService.instances[0]
        self.assertFalse(arm.disable_requested)
        self.assertTrue(arm.closed)

    def test_disable_is_an_explicit_user_action(self) -> None:
        with patch.object(arm_move, "ArmService", FakeArmService), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(arm_move.main(["disable", "--confirm"]), 0)

        arm = FakeArmService.instances[0]
        self.assertTrue(arm.disable_requested)
        self.assertTrue(arm.closed)

    def test_disable_requires_confirmation(self) -> None:
        with patch.object(arm_move, "ArmService", FakeArmService), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(arm_move.main(["disable"]), 2)

        self.assertEqual(FakeArmService.instances, [])

    def test_is_enabled_is_read_only_and_needs_no_confirmation(self) -> None:
        output = io.StringIO()
        with patch.object(arm_move, "ArmService", FakeArmService), contextlib.redirect_stdout(output):
            self.assertEqual(arm_move.main(["is-enabled"]), 0)

        arm = FakeArmService.instances[0]
        self.assertIn('"all_enabled": true', output.getvalue())
        self.assertFalse(arm.disable_requested)
        self.assertTrue(arm.closed)

    def test_move_p_by_j_calls_sdk_pose_by_move_j(self) -> None:
        output = io.StringIO()
        with (
            patch.object(arm_move, "ArmService", FakeArmService),
            patch.object(arm_move.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="n"),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(arm_move.main([
                "move-p-by-j",
                "--pose", "0.3", "-0.2", "0.25", "0.1", "0.2", "-0.3",
                "--duration", "2.5",
                "--confirm",
            ]), 0)

        arm = FakeArmService.instances[0]
        self.assertEqual(arm.target.values(), (0.3, -0.2, 0.25, 0.1, 0.2, -0.3))
        self.assertEqual(arm.duration_s, 2.5)
        self.assertFalse(arm.disable_requested)
        self.assertIn("[OK] move-p-by-j: 逆解及 MoveJ 运动完成。", output.getvalue())

    def test_move_p_by_j_prints_the_ik_failure_reason(self) -> None:
        class FailingArmService(FakeArmService):
            def move_to_pose_by_move_j(self, target, duration_s: float) -> None:
                raise RuntimeError("arm inverse kinematics did not converge")

        error = io.StringIO()
        with (
            patch.object(arm_move, "ArmService", FailingArmService),
            patch.object(arm_move.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="n"),
            contextlib.redirect_stderr(error),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(arm_move.main([
                "move-p-by-j",
                "--pose", "0.3", "-0.2", "0.25", "0.1", "0.2", "-0.3",
                "--duration", "2.5",
                "--confirm",
            ]), 1)

        self.assertIn(
            "[FAIL] move-p-by-j: arm inverse kinematics did not converge",
            error.getvalue(),
        )

    def test_move_p_by_j_prints_unexpected_sdk_errors(self) -> None:
        class PinocchioBindingError(Exception):
            pass

        class FailingArmService(FakeArmService):
            def move_to_pose_by_move_j(self, target, duration_s: float) -> None:
                raise PinocchioBindingError("SE3 expects a NumPy translation vector")

        error = io.StringIO()
        with (
            patch.object(arm_move, "ArmService", FailingArmService),
            patch.object(arm_move.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="n"),
            contextlib.redirect_stderr(error),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(arm_move.main([
                "move-p-by-j",
                "--pose", "0.3", "-0.2", "0.25", "0.1", "0.2", "-0.3",
                "--duration", "2.5",
                "--confirm",
            ]), 1)

        self.assertIn("[FAIL] move-p-by-j: SE3 expects a NumPy translation vector", error.getvalue())
