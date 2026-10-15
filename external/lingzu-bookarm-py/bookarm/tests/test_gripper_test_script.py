"""Offline tests for the gripper SDK test script."""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gripper" / "gripper_test.py"
_SPEC = importlib.util.spec_from_file_location("bookarm_gripper_test_script", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
gripper_test = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gripper_test)


class FakeArmService:
    instances: list["FakeArmService"] = []
    close_reason = "target_reached"

    def __init__(self, config) -> None:
        self.config = config
        self.events = []
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.events.append("start")

    def enable(self) -> None:
        self.events.append("enable")

    def open_gripper(self, target_deg, command=None) -> None:
        self.target_deg = target_deg
        self.command = command
        self.events.append("open")

    def close_gripper_monitored(self, command):
        self.command = command
        self.events.append("close")
        return {"reason": self.close_reason, "final_angle_rad": 0.5}

    def interrupt_motion(self) -> None:
        self.events.append("interrupt")

    def close(self) -> None:
        self.events.append("disconnect")


class GripperTestScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeArmService.instances.clear()
        FakeArmService.close_reason = "target_reached"

    def test_open_uses_parameterized_target_angle(self) -> None:
        output = io.StringIO()
        with patch.object(gripper_test, "ArmService", FakeArmService), contextlib.redirect_stdout(output):
            code = gripper_test.main(["open", "--angle", "30", "--speed-deg-s", "20", "--confirm"])

        self.assertEqual(code, 0)
        arm = FakeArmService.instances[0]
        self.assertEqual(arm.target_deg, 30.0)
        self.assertEqual(arm.command.open_deg, 30.0)
        self.assertEqual(arm.command.open_speed_deg_s, 20.0)
        self.assertEqual(arm.events, ["start", "enable", "open", "disconnect"])
        self.assertIn("夹爪已打开到 30.00°", output.getvalue())

    def test_close_uses_parameterized_target_angle(self) -> None:
        with patch.object(gripper_test, "ArmService", FakeArmService), contextlib.redirect_stdout(io.StringIO()):
            code = gripper_test.main(["close", "--angle", "80", "--confirm"])

        self.assertEqual(code, 0)
        arm = FakeArmService.instances[0]
        self.assertEqual(arm.command.close_deg, 80.0)
        self.assertEqual(arm.events, ["start", "enable", "close", "disconnect"])

    def test_requires_confirmation_before_connecting(self) -> None:
        error = io.StringIO()
        with patch.object(gripper_test, "ArmService", FakeArmService), contextlib.redirect_stderr(error):
            code = gripper_test.main(["open", "--angle", "30"])

        self.assertEqual(code, 2)
        self.assertEqual(FakeArmService.instances, [])
        self.assertIn("--confirm", error.getvalue())

    def test_rejects_angle_outside_sdk_limit_before_connecting(self) -> None:
        error = io.StringIO()
        with patch.object(gripper_test, "ArmService", FakeArmService), contextlib.redirect_stderr(error):
            code = gripper_test.main(["close", "--angle", "109", "--confirm"])

        self.assertEqual(code, 2)
        self.assertEqual(FakeArmService.instances, [])
        self.assertIn("超出 SDK 限位", error.getvalue())

    def test_accepts_configured_maximum_angle(self) -> None:
        with patch.object(gripper_test, "ArmService", FakeArmService), contextlib.redirect_stdout(io.StringIO()):
            code = gripper_test.main(["close", "--angle", "108.5", "--confirm"])

        self.assertEqual(code, 0)
        self.assertEqual(FakeArmService.instances[0].command.close_deg, 108.5)

    def test_close_timeout_returns_failure(self) -> None:
        FakeArmService.close_reason = "timeout"
        with (
            patch.object(gripper_test, "ArmService", FakeArmService),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = gripper_test.main(["close", "--angle", "80", "--confirm"])

        self.assertEqual(code, 1)

    def test_close_object_contact_returns_success(self) -> None:
        FakeArmService.close_reason = "object_contact"
        output = io.StringIO()
        with patch.object(gripper_test, "ArmService", FakeArmService), contextlib.redirect_stdout(output):
            code = gripper_test.main(["close", "--angle", "80", "--confirm"])

        self.assertEqual(code, 0)
        self.assertIn("书本接触", output.getvalue())


if __name__ == "__main__":
    unittest.main()
