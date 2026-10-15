"""Shutdown resilience tests for the hardware-backed workflow runner."""

from __future__ import annotations

import contextlib
import io
import threading
import time
import unittest

from bookarm.application.task_manager import TaskManager
from bookarm.service.runner import SdkWorkflowRunner


class _StubUseCase:
    def __init__(self, execute):
        self.execute = execute


class _StubSdk:
    def __init__(self, execute):
        self._take_book = _StubUseCase(execute)


class _StubRuntime:
    """Started runtime whose take-book use case runs a scripted execute."""

    def __init__(self, execute):
        self.started = True
        self.sdk = _StubSdk(execute)


class SdkWorkflowRunnerShutdownTests(unittest.TestCase):
    def stuck_runner(self, timeout_s: float) -> SdkWorkflowRunner:
        """Return a runner whose worker hangs inside the device operation."""

        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck_execute(task, request):
            entered.set()
            release.wait()

        runner = SdkWorkflowRunner(
            _StubRuntime(stuck_execute),
            TaskManager(),
            shutdown_timeout_s=timeout_s,
        )
        runner.submit_take(None)
        self.assertTrue(entered.wait(timeout=5.0))
        return runner

    def test_shutdown_abandons_stuck_worker_after_timeout(self):
        runner = self.stuck_runner(timeout_s=0.2)

        stderr = io.StringIO()
        started = time.monotonic()
        with contextlib.redirect_stderr(stderr):
            runner.shutdown()
        elapsed = time.monotonic() - started

        # The hung thread is reported and abandoned, not joined forever.
        self.assertLess(elapsed, 5.0)
        self.assertIn("did not finish", stderr.getvalue())
        self.assertTrue(runner._worker.is_alive())

    def test_shutdown_joins_finished_worker_without_error(self):
        finished = threading.Event()

        def quick_execute(task, request):
            finished.set()

        runner = SdkWorkflowRunner(
            _StubRuntime(quick_execute),
            TaskManager(),
            shutdown_timeout_s=5.0,
        )
        runner.submit_take(None)
        self.assertTrue(finished.wait(timeout=5.0))

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            runner.shutdown()

        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(runner._worker.is_alive())

    def test_shutdown_timeout_defaults_to_ten_seconds(self):
        runner = SdkWorkflowRunner(_StubRuntime(lambda task, request: None), TaskManager())
        self.assertEqual(runner._shutdown_timeout_s, 10.0)


if __name__ == "__main__":
    unittest.main()
