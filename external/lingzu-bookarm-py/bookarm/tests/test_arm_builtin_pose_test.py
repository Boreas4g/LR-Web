"""Safety and dispatch tests for the SDK built-in pose utility."""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "arm" / "arm_builtin_pose_test.py"
_SPEC = importlib.util.spec_from_file_location("bookarm_arm_builtin_pose_test", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
arm_builtin_pose_test = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(arm_builtin_pose_test)


class FakeArmService:
    instances: list["FakeArmService"] = []

    def __init__(self, config: object) -> None:
        self.events: list[object] = []
        FakeArmService.instances.append(self)

    def start(self) -> None:
        self.events.append("start")

    def enable(self) -> None:
        self.events.append("enable")

    def move_to_zero(self, duration_s: float) -> None:
        self.events.append(("move_to_zero", duration_s))

    def move_to_book_debug(self, duration_s: float) -> None:
        self.events.append(("move_to_book_debug", duration_s))

    def move_to_disable_pose(self, duration_s: float) -> None:
        self.events.append(("move_to_disable_pose", duration_s))

    def read_joint_pose(self):
        self.events.append("read_joint_pose")
        return type("Pose", (), {"values_rad": (0.0,) * 6})()

    def disable_at_safe_pose(self, duration_s: float = 3.0) -> dict[str, object]:
        self.events.append(("disable_at_safe_pose", duration_s))
        return {"disable_command_sent": True}

    def interrupt_motion(self) -> None:
        self.events.append("interrupt_motion")

    def close(self) -> None:
        self.events.append("close")


class ArmBuiltInPoseTestTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeArmService.instances.clear()

    def test_requires_confirmation_before_creating_service(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(arm_builtin_pose_test.main(["zero"]), 2)
        self.assertEqual(FakeArmService.instances, [])

    def test_zero_dispatches_to_sdk_helper_and_only_disables_with_confirmation(self) -> None:
        with (
            patch.object(arm_builtin_pose_test, "ArmService", FakeArmService),
            patch.object(arm_builtin_pose_test.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="y"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(arm_builtin_pose_test.main(["zero", "--duration", "1.5", "--confirm"]), 0)

        arm = FakeArmService.instances[0]
        self.assertEqual(
            arm.events,
            ["start", "enable", ("move_to_zero", 1.5), "read_joint_pose", ("disable_at_safe_pose", 3.0), "close"],
        )

    def test_book_debug_keeps_motors_enabled_when_user_declines(self) -> None:
        with (
            patch.object(arm_builtin_pose_test, "ArmService", FakeArmService),
            patch.object(arm_builtin_pose_test.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="n"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(arm_builtin_pose_test.main(["book-debug", "--confirm"]), 0)

        arm = FakeArmService.instances[0]
        self.assertIn(("move_to_book_debug", 3.0), arm.events)
        self.assertNotIn(("disable_at_safe_pose", 3.0), arm.events)
        self.assertEqual(arm.events[-1], "close")

    def test_disable_pose_dispatches_to_sdk_helper(self) -> None:
        with (
            patch.object(arm_builtin_pose_test, "ArmService", FakeArmService),
            patch.object(arm_builtin_pose_test.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="n"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(arm_builtin_pose_test.main(["disable-pose", "--duration", "2.5", "--confirm"]), 0)

        arm = FakeArmService.instances[0]
        self.assertIn(("move_to_disable_pose", 2.5), arm.events)
        self.assertNotIn(("disable_at_safe_pose", 3.0), arm.events)

    def test_noninteractive_input_never_disables_motors(self) -> None:
        with (
            patch.object(arm_builtin_pose_test, "ArmService", FakeArmService),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(arm_builtin_pose_test.main(["zero", "--confirm"]), 0)

        arm = FakeArmService.instances[0]
        self.assertNotIn(("disable_at_safe_pose", 3.0), arm.events)
