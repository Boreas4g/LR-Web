"""Persistence and process-lock tests that never touch BookArm hardware."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bookarm.api.results import StageResult
from bookarm.application.task_manager import JsonTaskStore, TaskManager
from bookarm.domain.models.task import StageStatus, TaskId, TaskState, WorkflowKind
from bookarm.runtime.reservation import HardwareProcessLock


class JsonTaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "runtime" / "tasks.json"

    def test_persists_terminal_task_and_json_details(self) -> None:
        manager = TaskManager(JsonTaskStore(self.path))
        task = manager.create(WorkflowKind.TAKE_BOOK)
        task.state = TaskState.SUCCEEDED
        task.stages.append(
            StageResult("lift_move", StageStatus.SUCCEEDED, "done", {"height": 800.0})
        )
        manager.save(task)

        restored = TaskManager(JsonTaskStore(self.path)).get(task.id.value)

        self.assertEqual(restored.state, TaskState.SUCCEEDED)
        self.assertEqual(restored.stages[0].details, {"height": 800.0})
        self.assertTrue(self.path.is_file())

    def test_marks_interrupted_task_as_recovery_required_after_restart(self) -> None:
        manager = TaskManager(JsonTaskStore(self.path))
        task = manager.create(WorkflowKind.RETURN_BOOK)
        task.state = TaskState.RUNNING
        task.stages.append(StageResult("book_stand_pickup", StageStatus.RUNNING, "stage running"))
        manager.save(task)

        recovered = TaskManager(JsonTaskStore(self.path)).get(task.id.value)

        self.assertEqual(recovered.state, TaskState.RECOVERY_REQUIRED)
        self.assertIsNotNone(recovered.result)
        self.assertIn("service restarted", recovered.result.error or "")
        self.assertEqual(recovered.stages[-1].status, StageStatus.RUNNING)
        with self.assertRaisesRegex(RuntimeError, "terminal task"):
            TaskManager(JsonTaskStore(self.path)).cancel(task.id.value)

    def test_rejects_a_corrupt_journal_instead_of_silently_losing_recovery_state(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "cannot load bookarm task journal"):
            JsonTaskStore(self.path)

    def test_delete_removes_the_task_from_the_journal_durably(self) -> None:
        manager = TaskManager(JsonTaskStore(self.path))
        kept = manager.create(WorkflowKind.TAKE_BOOK)
        dropped = manager.create(WorkflowKind.RETURN_BOOK)
        manager.store.delete(dropped.id)

        reloaded = TaskManager(JsonTaskStore(self.path))
        self.assertEqual(reloaded.get(kept.id.value).id, kept.id)
        with self.assertRaises(KeyError):
            reloaded.get(dropped.id.value)

    def test_delete_of_an_unknown_task_raises(self) -> None:
        manager = TaskManager(JsonTaskStore(self.path))

        with self.assertRaises(KeyError):
            manager.store.delete(TaskId("no-such-task"))


class DismissRecoveryTests(unittest.TestCase):
    def test_dismiss_erases_only_a_recovery_required_task(self) -> None:
        manager = TaskManager()
        task = manager.create(WorkflowKind.RETURN_BOOK)

        with self.assertRaisesRegex(RuntimeError, "recovery_required"):
            manager.dismiss_recovery(task.id.value)
        self.assertEqual(manager.get(task.id.value).state, TaskState.QUEUED)

        task.state = TaskState.RECOVERY_REQUIRED
        manager.save(task)
        dismissed = manager.dismiss_recovery(task.id.value)

        self.assertEqual(dismissed.id, task.id)
        with self.assertRaises(KeyError):
            manager.get(task.id.value)

    def test_dismiss_of_an_unknown_task_raises_key_error(self) -> None:
        manager = TaskManager()

        with self.assertRaises(KeyError):
            manager.dismiss_recovery("no-such-task")

    def test_dismiss_clears_the_safety_interrupt_flag(self) -> None:
        manager = TaskManager()
        task = manager.create(WorkflowKind.TAKE_BOOK)
        task.state = TaskState.RECOVERY_REQUIRED
        manager.save(task)
        manager.interrupt(task.id.value)
        self.assertTrue(manager.is_interrupted(task.id.value))

        manager.dismiss_recovery(task.id.value)

        self.assertFalse(manager.is_interrupted(task.id.value))

    def test_in_memory_store_delete_removes_the_task(self) -> None:
        manager = TaskManager()
        task = manager.create(WorkflowKind.TAKE_BOOK)

        manager.store.delete(task.id)

        with self.assertRaises(KeyError):
            manager.get(task.id.value)


class HardwareProcessLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "runtime" / "hardware.lock"

    def test_rejects_a_second_live_owner_and_releases_cleanly(self) -> None:
        first = HardwareProcessLock(self.path)
        second = HardwareProcessLock(self.path)
        first.acquire()
        self.addCleanup(first.release)

        with self.assertRaisesRegex(RuntimeError, "already reserved"):
            second.acquire()

        first.release()
        second.acquire()
        self.addCleanup(second.release)
        self.assertTrue(second.held)
