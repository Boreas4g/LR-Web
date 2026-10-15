"""HTTP safety-stop endpoints: immediate stop chain and token enforcement."""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

from bookarm.application.task_manager import TaskManager
from bookarm.application.workflow_engine import WorkflowEngine
from bookarm.domain.models.task import TaskState, WorkflowKind
from bookarm.service.app import create_app
from bookarm.service.runner import FakeWorkflowRunner
from bookarm.service.safety import SafetyController


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
TASKS_URL = "/api/v1/takeout/tasks"
STOP_URL = "/api/v1/safety/stop"
EMERGENCY_URL = "/api/v1/safety/emergency-stop"


class _MockArm:
    def __init__(self) -> None:
        self.interrupt_calls = 0
        self.emergency_calls = 0
        self.emergency_result = True
        self.error: Exception | None = None

    def interrupt_motion(self) -> None:
        self.interrupt_calls += 1
        if self.error is not None:
            raise self.error

    def emergency_stop(self) -> bool:
        self.emergency_calls += 1
        if self.error is not None:
            raise self.error
        return self.emergency_result


class _MockLift:
    def __init__(self) -> None:
        self.emergency_calls = 0
        self.emergency_result = True
        self.error: Exception | None = None

    def emergency_stop(self) -> bool:
        self.emergency_calls += 1
        if self.error is not None:
            raise self.error
        return self.emergency_result


def _create_task(client: TestClient, **form):
    data = {"target_height_mm": "300"}
    data.update({key: str(value) for key, value in form.items()})
    return client.post(
        TASKS_URL,
        data=data,
        files={"spine_image": ("book1.png", PNG_BYTES, "image/png")},
    )


def _poll_terminal(client: TestClient, task_id: str, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        view = client.get("%s/%s" % (TASKS_URL, task_id)).json()
        if view["state"] in {"succeeded", "failed", "cancelled", "unsupported", "recovery_required"}:
            return view
        time.sleep(0.01)
    raise AssertionError("task %s did not reach a terminal state" % task_id)


class SafetyServiceTests(unittest.TestCase):
    def setUp(self):
        self._upload = tempfile.TemporaryDirectory()
        self.addCleanup(self._upload.cleanup)
        self.arm = _MockArm()
        self.lift = _MockLift()

    def _client(self, runner, *, token=None, arm=None, lift=None) -> TestClient:
        safety = SafetyController(runner, arm=arm, lift=lift)
        return TestClient(
            create_app(runner, upload_dir=Path(self._upload.name), token=token, safety=safety)
        )

    def _gated_runner(self, stage: str):
        """Return a runner blocked inside one stage plus its release control."""

        entered = threading.Event()
        gate = threading.Event()

        def hook(name: str) -> None:
            if name == stage:
                entered.set()
                gate.wait(timeout=10.0)

        runner = FakeWorkflowRunner(stage_delay_s=0.01, stage_hook=hook)
        self.addCleanup(runner.shutdown)
        return runner, entered, gate

    def test_stop_without_active_task_is_idempotent(self):
        runner = FakeWorkflowRunner(stage_delay_s=0.01)
        self.addCleanup(runner.shutdown)
        client = self._client(runner, arm=self.arm, lift=self.lift)

        response = client.post(STOP_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"accepted": True, "armStopped": True, "liftStopped": True, "taskInterrupted": False},
        )
        self.assertEqual(self.arm.interrupt_calls, 1)
        self.assertEqual(self.arm.emergency_calls, 0)
        self.assertEqual(self.lift.emergency_calls, 1)

    def test_stop_interrupts_running_task_and_halts_devices(self):
        runner, entered, gate = self._gated_runner("lift_move")
        client = self._client(runner, arm=self.arm, lift=self.lift)
        created = _create_task(client)
        self.assertEqual(created.status_code, 201)
        task_id = created.json()["id"]
        self.assertTrue(entered.wait(timeout=5.0))

        response = client.post(STOP_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"accepted": True, "armStopped": True, "liftStopped": True, "taskInterrupted": True},
        )
        self.assertEqual(self.arm.interrupt_calls, 1)
        self.assertEqual(self.lift.emergency_calls, 1)
        gate.set()
        view = _poll_terminal(client, task_id)
        self.assertEqual(view["state"], "cancelled")
        self.assertIn("interrupted", view["error"])

    def test_emergency_stop_disables_arm_and_interrupts_task(self):
        runner, entered, gate = self._gated_runner("vision_target")
        client = self._client(runner, arm=self.arm, lift=self.lift)
        created = _create_task(client)
        self.assertEqual(created.status_code, 201)
        task_id = created.json()["id"]
        self.assertTrue(entered.wait(timeout=5.0))

        response = client.post(EMERGENCY_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"accepted": True, "armStopped": True, "liftStopped": True, "taskInterrupted": True},
        )
        self.assertEqual(self.arm.interrupt_calls, 0)
        self.assertEqual(self.arm.emergency_calls, 1)
        self.assertEqual(self.lift.emergency_calls, 1)
        gate.set()
        view = _poll_terminal(client, task_id)
        self.assertEqual(view["state"], "cancelled")
        self.assertIn("interrupted", view["error"])

    def test_stop_without_attached_devices_reports_stopped(self):
        runner = FakeWorkflowRunner(stage_delay_s=0.01)
        self.addCleanup(runner.shutdown)
        client = self._client(runner)

        response = client.post(EMERGENCY_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"accepted": True, "armStopped": True, "liftStopped": True, "taskInterrupted": False},
        )

    def test_arm_stop_failure_is_reported_without_failing_the_request(self):
        runner = FakeWorkflowRunner(stage_delay_s=0.01)
        self.addCleanup(runner.shutdown)
        self.arm.error = RuntimeError("CAN send failed")
        client = self._client(runner, arm=self.arm, lift=self.lift)

        response = client.post(STOP_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"accepted": True, "armStopped": False, "liftStopped": True, "taskInterrupted": False},
        )

    def test_arm_emergency_result_false_is_reported(self):
        runner = FakeWorkflowRunner(stage_delay_s=0.01)
        self.addCleanup(runner.shutdown)
        self.arm.emergency_result = False
        client = self._client(runner, arm=self.arm, lift=self.lift)

        response = client.post(EMERGENCY_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], True)
        self.assertEqual(response.json()["armStopped"], False)

    def test_safety_routes_enforce_the_bearer_token(self):
        runner = FakeWorkflowRunner(stage_delay_s=0.01)
        self.addCleanup(runner.shutdown)
        client = self._client(runner, token="secret", arm=self.arm, lift=self.lift)

        self.assertEqual(client.post(STOP_URL).status_code, 401)
        self.assertEqual(
            client.post(STOP_URL, headers={"Authorization": "Bearer wrong"}).status_code, 401
        )
        self.assertEqual(
            client.post(EMERGENCY_URL, headers={"Authorization": "Bearer secret"}).status_code, 200
        )


class SafetyInterruptFlagTests(unittest.TestCase):
    def test_task_manager_interrupt_is_idempotent_for_terminal_tasks(self):
        manager = TaskManager()
        task = manager.create(WorkflowKind.TAKE_BOOK)
        manager.cancel(task.id.value)

        interrupted = manager.interrupt(task.id.value)

        self.assertEqual(interrupted.state, TaskState.CANCELLED)
        self.assertTrue(manager.is_interrupted(task.id.value))

    def test_engine_does_not_start_an_interrupted_task(self):
        manager = TaskManager()
        engine = WorkflowEngine(manager)
        task = manager.create(WorkflowKind.TAKE_BOOK)
        manager.interrupt(task.id.value)
        actions = []

        result = engine.run(task, (("stage", lambda: actions.append(1) or {}, False),))

        self.assertEqual(result.state, TaskState.CANCELLED)
        self.assertIn("interrupted", result.error)
        self.assertEqual(actions, [])

    def test_engine_stops_at_next_checkpoint_after_interrupt(self):
        manager = TaskManager()
        engine = WorkflowEngine(manager)
        task = manager.create(WorkflowKind.TAKE_BOOK)

        def first_stage():
            manager.interrupt(task.id.value)
            return {"done": True}

        result = engine.run(
            task,
            (
                ("first", first_stage, False),
                ("second", lambda: {}, False),
            ),
        )

        self.assertEqual(result.state, TaskState.CANCELLED)
        self.assertIn("interrupted", result.error)
        self.assertEqual([stage.name for stage in result.stages], ["first"])


if __name__ == "__main__":
    unittest.main()
