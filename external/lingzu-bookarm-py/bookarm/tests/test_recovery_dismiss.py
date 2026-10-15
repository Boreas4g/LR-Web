"""HTTP dismiss-recovery endpoint: operator takeover confirmation."""

from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

from bookarm.application.task_manager import JsonTaskStore, TaskManager
from bookarm.domain.models.task import TaskState, WorkflowKind
from bookarm.service.app import create_app
from bookarm.service.runner import FakeWorkflowRunner


DISMISS_URL = "/api/v1/tasks/%s/dismiss-recovery"
TASK_URL = "/api/v1/takeout/tasks/%s"


class DismissRecoveryServiceTests(unittest.TestCase):
    def setUp(self):
        self._upload = tempfile.TemporaryDirectory()
        self.addCleanup(self._upload.cleanup)
        self.manager = TaskManager()
        self.runner = FakeWorkflowRunner(stage_delay_s=0.01, task_manager=self.manager)
        self.addCleanup(self.runner.shutdown)
        self.client = TestClient(
            create_app(self.runner, upload_dir=Path(self._upload.name))
        )

    def _seed(self, state: TaskState):
        task = self.manager.create(WorkflowKind.TAKE_BOOK)
        task.state = state
        self.manager.save(task)
        return task

    def test_dismiss_erases_a_recovery_required_task(self):
        task = self._seed(TaskState.RECOVERY_REQUIRED)

        response = self.client.post(DISMISS_URL % task.id.value)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], task.id.value)
        self.assertEqual(body["state"], "recovery_required")
        # Dismissal is a removal: the id is gone for good.
        self.assertEqual(self.client.get(TASK_URL % task.id.value).status_code, 404)
        self.assertEqual(self.client.post(DISMISS_URL % task.id.value).status_code, 404)

    def test_dismiss_rejects_tasks_in_any_other_state(self):
        for state in (
            TaskState.QUEUED,
            TaskState.RUNNING,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.UNSUPPORTED,
        ):
            task = self._seed(state)

            response = self.client.post(DISMISS_URL % task.id.value)

            self.assertEqual(response.status_code, 409, state)
            # Refused dismissals never erase the record.
            self.assertEqual(self.client.get(TASK_URL % task.id.value).status_code, 200)

    def test_dismiss_of_an_unknown_task_is_404(self):
        self.assertEqual(self.client.post(DISMISS_URL % "nope").status_code, 404)

    def test_dismiss_enforces_the_bearer_token(self):
        manager = TaskManager()
        runner = FakeWorkflowRunner(stage_delay_s=0.01, task_manager=manager)
        self.addCleanup(runner.shutdown)
        client = TestClient(
            create_app(runner, upload_dir=Path(self._upload.name), token="secret")
        )
        task = manager.create(WorkflowKind.TAKE_BOOK)
        task.state = TaskState.RECOVERY_REQUIRED
        manager.save(task)

        self.assertEqual(client.post(DISMISS_URL % task.id.value).status_code, 401)
        response = client.post(
            DISMISS_URL % task.id.value,
            headers={"Authorization": "Bearer secret"},
        )
        self.assertEqual(response.status_code, 200)

    def test_dismiss_removes_the_task_from_the_json_journal(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        journal = Path(directory.name) / "runtime" / "tasks.json"
        manager = TaskManager(JsonTaskStore(journal))
        runner = FakeWorkflowRunner(stage_delay_s=0.01, task_manager=manager)
        self.addCleanup(runner.shutdown)
        client = TestClient(create_app(runner, upload_dir=Path(self._upload.name)))
        task = manager.create(WorkflowKind.TAKE_BOOK)
        task.state = TaskState.RECOVERY_REQUIRED
        manager.save(task)

        response = client.post(DISMISS_URL % task.id.value)

        self.assertEqual(response.status_code, 200)
        with self.assertRaises(KeyError):
            TaskManager(JsonTaskStore(journal)).get(task.id.value)


if __name__ == "__main__":
    unittest.main()
