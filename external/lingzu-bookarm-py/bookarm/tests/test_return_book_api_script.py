"""Offline tests for the public-SDK return-book script."""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bookarm.api.results import WorkflowResult, WorkflowTask
from bookarm.domain.models.task import TaskId, TaskState, WorkflowKind


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "api" / "return_book_test.py"
SPEC = importlib.util.spec_from_file_location("bookarm_return_book_api_script", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
return_book_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(return_book_test)


class FakeSdk:
    def __init__(self) -> None:
        self.request = None
        self.task = WorkflowTask(TaskId("return-book-test"), WorkflowKind.RETURN_BOOK)
        self.result = WorkflowResult(self.task.id, self.task.kind, TaskState.SUCCEEDED, [])

    def return_book(self, request):
        self.request = request
        return self.task

    def wait(self, task_id):
        self.waited_task_id = task_id
        return self.result

    def disable_arm(self, *, confirmed, duration_s=3.0):
        self.arm_disable_requested = confirmed
        return {"disable_command_sent": True}


class FakeRuntime:
    def __init__(self) -> None:
        self.sdk = FakeSdk()
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class ReturnBookApiScriptTests(unittest.TestCase):
    def test_requires_confirmation_before_creating_runtime(self):
        with (
            patch.object(return_book_test, "create_return_book_runtime") as factory,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = return_book_test.main(["--height-mm", "300"])

        self.assertEqual(code, 2)
        factory.assert_not_called()

    def test_lift_only_needs_height_speed_and_confirmation(self):
        runtime = FakeRuntime()
        with (
            patch.object(return_book_test, "create_return_book_runtime", return_value=runtime) as factory,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = return_book_test.main([
                "--height-mm", "300",
                "--speed-level", "3",
                "--stop-after", "lift_move",
                "--confirm",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(runtime.sdk.request.target_height_mm, 300.0)
        self.assertEqual(runtime.sdk.request.lift_speed_level, 3)
        self.assertEqual(runtime.sdk.request.stop_after_stage.value, "lift_move")
        self.assertEqual(factory.call_args.kwargs["stop_after_stage"].value, "lift_move")
        self.assertTrue(runtime.started)
        self.assertTrue(runtime.stopped)

    def test_full_request_passes_slot_and_left_neighbor_template(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "left-neighbor.jpeg"
            slots = root / "pickup-slots.json"
            calibration = root / "camera.json"
            for path in (template, slots, calibration):
                path.write_text("{}", encoding="utf-8")
            runtime = FakeRuntime()
            with (
                patch.object(return_book_test, "create_return_book_runtime", return_value=runtime) as factory,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = return_book_test.main([
                    "--height-mm", "300",
                    "--slot", "2",
                    "--left-neighbor-template", str(template),
                    "--book-stand-pickup-slots", str(slots),
                    "--camera-calibration", str(calibration),
                    "--confirm",
                ])

        self.assertEqual(code, 0)
        self.assertEqual(runtime.sdk.request.source_slot.value, 2)
        self.assertEqual(runtime.sdk.request.left_neighbor_spine_template.path, template)
        self.assertEqual(factory.call_args.kwargs["book_stand_pickup_slots_path"], slots)

    def test_arm_disable_requires_a_separate_interactive_yes(self):
        runtime = FakeRuntime()
        with (
            patch.object(return_book_test, "create_return_book_runtime", return_value=runtime),
            patch.object(return_book_test.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="y"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = return_book_test.main([
                "--height-mm", "300",
                "--stop-after", "arm_debug_pose",
                "--confirm",
            ])

        self.assertEqual(code, 0)
        self.assertTrue(runtime.sdk.arm_disable_requested)
        self.assertTrue(runtime.stopped)


if __name__ == "__main__":
    unittest.main()
