"""Offline tests for the public-SDK take-book script."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bookarm.api.results import StageResult, WorkflowResult, WorkflowTask
from bookarm.domain.models.task import StageStatus, TaskId, TaskState, WorkflowKind


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "api" / "take_book_test.py"
_SPEC = importlib.util.spec_from_file_location("bookarm_take_book_api_script", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
take_book_test = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(take_book_test)


class FakeSdk:
    def __init__(self) -> None:
        self.request = None
        self.task = WorkflowTask(TaskId("take-book-test"), WorkflowKind.TAKE_BOOK)
        self.result = WorkflowResult(self.task.id, self.task.kind, TaskState.SUCCEEDED, [])

    def take_book(self, request):
        self.request = request
        return self.task

    def wait(self, task_id):
        self.waited_task_id = task_id
        return self.result

    def disable_arm(self, *, confirmed, duration_s=3.0):
        if confirmed is not True:
            raise ValueError("explicit confirmation is required")
        self.arm_disable_requested = True
        self.arm_disable_duration_s = duration_s
        return {"disable_command_sent": True}


class FakeRuntime:
    def __init__(self) -> None:
        self.sdk = FakeSdk()
        self.entered = False
        self.exited = False

        self.started = False
        self.stopped = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_):
        self.exited = True

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class TakeBookApiScriptTests(unittest.TestCase):
    def test_arm_config_converts_offset_and_gravity_mapping_keys_to_motor_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "arm.json"
            path.write_text(json.dumps({
                "joint_offsets": {"1": 0.1, "7": -0.2},
                "gravity_joint_scale": {"2": 1.5},
            }), encoding="utf-8")

            config = take_book_test.load_config(path, take_book_test.ArmDeviceConfig)

        assert config is not None
        self.assertEqual(config.joint_offsets[1], 0.1)
        self.assertEqual(config.joint_offsets[7], -0.2)
        self.assertEqual(config.gravity_joint_scale[2], 1.5)

    def test_requires_confirmation_before_creating_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "book.png"
            slots = root / "slots.json"
            calibration = root / "camera.json"
            for path in (template, slots, calibration):
                path.write_text("{}", encoding="utf-8")
            with (
                patch.object(take_book_test, "create_bookarm_runtime") as factory,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(take_book_test.main([
                    "--template", str(template),
                    "--height-mm", "300",
                    "--slot", "1",
                    "--book-stand-slots", str(slots),
                    "--camera-calibration", str(calibration),
                ]), 2)
        factory.assert_not_called()

    def test_calls_public_sdk_with_height_and_speed_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "book.png"
            slots = root / "slots.json"
            calibration = root / "camera.json"
            for path in (template, slots, calibration):
                path.write_text("{}", encoding="utf-8")
            runtime = FakeRuntime()
            with (
                patch.object(take_book_test, "create_bookarm_runtime", return_value=runtime) as factory,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = take_book_test.main([
                    "--template", str(template),
                    "--height-mm", "300",
                    "--speed-level", "3",
                    "--slot", "2",
                    "--book-stand-slots", str(slots),
                    "--camera-calibration", str(calibration),
                    "--confirm",
                ])

        self.assertEqual(code, 0)
        self.assertTrue(runtime.started)
        self.assertTrue(runtime.stopped)
        self.assertEqual(runtime.sdk.request.target_height_mm, 300.0)
        self.assertEqual(runtime.sdk.request.lift_speed_level, 3)
        self.assertEqual(runtime.sdk.request.destination_slot.value, 2)
        self.assertEqual(factory.call_args.kwargs["book_stand_slots_path"], slots)

    def test_lift_only_requires_only_height_speed_and_confirmation(self) -> None:
        runtime = FakeRuntime()
        with (
            patch.object(take_book_test, "create_bookarm_runtime", return_value=runtime) as factory,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = take_book_test.main([
                "--height-mm", "300",
                "--speed-level", "2",
                "--stop-after", "lift_move",
                "--confirm",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(runtime.sdk.request.stop_after_stage.value, "lift_move")
        self.assertEqual(factory.call_args.kwargs["take_book_stop_after_stage"].value, "lift_move")

    def test_visualize_passes_non_blocking_camera_display_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "book.png"
            calibration = root / "camera.json"
            for path in (template, calibration):
                path.write_text("{}", encoding="utf-8")
            runtime = FakeRuntime()
            with (
                patch.object(take_book_test, "create_bookarm_runtime", return_value=runtime) as factory,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = take_book_test.main([
                    "--height-mm", "300",
                    "--stop-after", "vision_target",
                    "--template", str(template),
                    "--camera-calibration", str(calibration),
                    "--visualize",
                    "--visualize-duration-ms", "17",
                    "--confirm",
                ])

        self.assertEqual(code, 0)
        camera_config = factory.call_args.kwargs["camera_config"]
        self.assertTrue(camera_config.visualize)
        self.assertEqual(camera_config.visualize_duration_ms, 17)

    def test_grasp_point_from_top_passes_camera_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "book.png"
            calibration = root / "camera.json"
            for path in (template, calibration):
                path.write_text("{}", encoding="utf-8")
            runtime = FakeRuntime()
            with (
                patch.object(take_book_test, "create_bookarm_runtime", return_value=runtime) as factory,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = take_book_test.main([
                    "--height-mm", "300",
                    "--stop-after", "vision_target",
                    "--template", str(template),
                    "--camera-calibration", str(calibration),
                    "--grasp-point-from-top", "1/5",
                    "--confirm",
                ])

        self.assertEqual(code, 0)
        camera_config = factory.call_args.kwargs["camera_config"]
        self.assertEqual(camera_config.grasp_point_from_top_ratio, 1.0 / 5.0)

    def test_visualize_requires_a_vision_stage(self) -> None:
        with (
            patch.object(take_book_test, "create_bookarm_runtime") as factory,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = take_book_test.main([
                "--height-mm", "300",
                "--stop-after", "lift_move",
                "--visualize",
                "--confirm",
            ])

        self.assertEqual(code, 2)
        factory.assert_not_called()

    def test_step_book_grasp_passes_the_interactive_step_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "book.png"
            calibration = root / "camera.json"
            for path in (template, calibration):
                path.write_text("{}", encoding="utf-8")
            runtime = FakeRuntime()
            with (
                patch.object(take_book_test, "create_bookarm_runtime", return_value=runtime) as factory,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = take_book_test.main([
                    "--height-mm", "300",
                    "--stop-after", "book_grasp",
                    "--template", str(template),
                    "--camera-calibration", str(calibration),
                    "--step-book-grasp",
                    "--confirm",
                ])

        self.assertEqual(code, 0)
        callback = factory.call_args.kwargs["grasp_step_approval"]
        self.assertIs(callback, take_book_test.confirm_book_grasp_step)
        reporter = factory.call_args.kwargs["grasp_step_reporter"]
        self.assertIs(reporter, take_book_test.report_book_grasp_step)

    def test_step_book_grasp_requires_the_book_grasp_stage_or_later(self) -> None:
        with (
            patch.object(take_book_test, "create_bookarm_runtime") as factory,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = take_book_test.main([
                "--height-mm", "300",
                "--stop-after", "arm_debug_pose",
                "--step-book-grasp",
                "--confirm",
            ])

        self.assertEqual(code, 2)
        factory.assert_not_called()

    def test_book_grasp_step_confirmation_rejects_noninteractive_input(self) -> None:
        error = io.StringIO()
        with patch.object(take_book_test.sys.stdin, "isatty", return_value=False), contextlib.redirect_stderr(error):
            self.assertFalse(take_book_test.confirm_book_grasp_step("open_gripper"))

        self.assertIn("未发送该动作", error.getvalue())

    def test_arm_disable_requires_an_interactive_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "book.png"
            calibration = root / "camera.json"
            template.write_text("image", encoding="utf-8")
            calibration.write_text("{}", encoding="utf-8")
            runtime = FakeRuntime()
            with (
                patch.object(take_book_test, "create_bookarm_runtime", return_value=runtime),
                patch.object(take_book_test.sys.stdin, "isatty", return_value=True),
                patch("builtins.input", return_value="y"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = take_book_test.main([
                    "--height-mm", "300",
                    "--stop-after", "arm_debug_pose",
                    "--template", str(template),
                    "--camera-calibration", str(calibration),
                    "--confirm",
                ])

        self.assertEqual(code, 0)
        self.assertTrue(runtime.sdk.arm_disable_requested)
        self.assertTrue(runtime.stopped)

    def test_failed_workflow_is_printed_before_arm_disable_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "book.png"
            calibration = root / "camera.json"
            template.write_text("image", encoding="utf-8")
            calibration.write_text("{}", encoding="utf-8")
            runtime = FakeRuntime()
            runtime.sdk.result = WorkflowResult(
                runtime.sdk.task.id,
                runtime.sdk.task.kind,
                TaskState.RECOVERY_REQUIRED,
                [StageResult("book_grasp", StageStatus.FAILED, "pregrasp MoveJ failed")],
                "pregrasp MoveJ failed",
            )
            events = []
            with (
                patch.object(take_book_test, "create_bookarm_runtime", return_value=runtime),
                patch.object(
                    take_book_test,
                    "print_result",
                    side_effect=lambda result: events.append(("result", result.error)),
                ),
                patch.object(
                    take_book_test,
                    "confirm_arm_disable",
                    side_effect=lambda _runtime: events.append(("disable_confirmation", None)),
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = take_book_test.main([
                    "--height-mm", "300",
                    "--stop-after", "book_grasp",
                    "--template", str(template),
                    "--camera-calibration", str(calibration),
                    "--step-book-grasp",
                    "--confirm",
                ])

        self.assertEqual(code, 1)
        self.assertEqual(
            events,
            [("result", "pregrasp MoveJ failed"), ("disable_confirmation", None)],
        )
