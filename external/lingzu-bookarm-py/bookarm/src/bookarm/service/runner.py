"""Serial take/return task runners: real SDK execution and a hardware-free fake.

Hardware is exclusive, so both runners serialize submissions on one daemon
worker thread and reject concurrent submissions with ``RunnerBusyError``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from bookarm.api.requests import (
    MidReturnBookRequest,
    MidReturnStage,
    ReturnBookRequest,
    ReturnBookStage,
    TakeBookRequest,
    TakeBookStage,
)
from bookarm.api.results import WorkflowResult, WorkflowTask
from bookarm.application.task_manager import TaskManager
from bookarm.application.workflow_engine import WorkflowEngine, WorkflowStep
from bookarm.domain.models.task import TaskState, WorkflowKind
from bookarm.domain.plans.mid_return import MidReturnMotionPlan
from bookarm.domain.plans.return_book import ReturnBookMotionPlan
from bookarm.domain.plans.take_book import TakeBookMotionPlan
from bookarm.runtime.bookarm_runtime import BookArmRuntime


class RunnerBusyError(RuntimeError):
    """Raised when a workflow task is submitted while another one runs."""


class WorkflowRunner(ABC):
    """Serial executor behind the take-out and return HTTP APIs."""

    @property
    @abstractmethod
    def mode(self) -> str:
        """Human-readable runner mode reported by the health endpoint."""

    @property
    @abstractmethod
    def started(self) -> bool:
        """Whether the runner is ready to execute workflow requests."""

    @property
    def busy_lock(self) -> threading.Lock:
        """The non-blocking lock serializing workflow execution.

        Interactive hardware actions (the spine studio) acquire the same lock
        so they can never overlap a running workflow task.
        """

        return self._busy

    @abstractmethod
    def submit_take(self, request: TakeBookRequest) -> WorkflowTask:
        """Queue one take-book request; raise RunnerBusyError when busy."""

    @abstractmethod
    def submit_return(self, request: ReturnBookRequest) -> WorkflowTask:
        """Queue one return-book request; raise RunnerBusyError when busy."""

    @abstractmethod
    def submit_mid_return(self, request: MidReturnBookRequest) -> WorkflowTask:
        """Queue one mid-return request; raise RunnerBusyError when busy."""

    def submit(self, request: TakeBookRequest) -> WorkflowTask:
        """Backward-compatible alias for ``submit_take``."""

        return self.submit_take(request)

    @abstractmethod
    def status(self, task_id: str) -> WorkflowTask:
        """Return the live task view; raise KeyError for unknown task ids."""

    @abstractmethod
    def cancel(self, task_id: str) -> WorkflowTask:
        """Request cancellation at the next stage boundary."""

    @abstractmethod
    def dismiss_recovery(self, task_id: str) -> WorkflowTask:
        """Erase a recovery_required task after operator takeover.

        Returns the dismissed task's final view.  Raises KeyError for unknown
        task ids and RuntimeError when the task is not recovery_required.
        """

    @abstractmethod
    def interrupt_active(self) -> Optional[WorkflowTask]:
        """Interrupt the in-flight task after a safety stop, if one is running.

        Returns the interrupted task, or ``None`` when no task is active so
        callers can stay idempotent.
        """

    def cancel_active(self) -> Optional[WorkflowTask]:
        """Request cancellation of the in-flight task, if one is running.

        Used by tuning run-steps that preempt the workflow: cancellation
        takes effect at the next workflow stage boundary, so callers must
        still wait for the busy lock. Returns the task, or ``None`` when no
        task is active so callers can stay idempotent.
        """

        task_id = self._active_task_id
        if task_id is None:
            return None
        try:
            return self.cancel(task_id)
        except (KeyError, RuntimeError):
            return None

    def shutdown(self) -> None:
        """Release runner resources; runners without resources may do nothing."""


def _fail_task(task_manager: TaskManager, task: WorkflowTask, error: Exception) -> None:
    """Persist a terminal failure when execution escapes the workflow engine."""

    task.state = TaskState.FAILED
    task.result = WorkflowResult(task.id, task.kind, task.state, list(task.stages), str(error), {})
    task_manager.save(task)


class SdkWorkflowRunner(WorkflowRunner):
    """Execute take-book and return-book requests on one started runtime."""

    def __init__(
        self,
        runtime: BookArmRuntime,
        task_manager: TaskManager,
        *,
        shutdown_timeout_s: float = 10.0,
    ) -> None:
        """``task_manager`` must be the instance injected into the runtime.

        ``shutdown_timeout_s`` bounds how long :meth:`shutdown` waits for an
        in-flight workflow worker; a device call that never returns (e.g. a
        hung CAN write) must not suspend ``systemctl stop`` forever.
        """

        self._runtime = runtime
        self._tasks = task_manager
        self._busy = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._active_task_id: Optional[str] = None
        self._shutdown_timeout_s = float(shutdown_timeout_s)

    @property
    def mode(self) -> str:
        return "real"

    @property
    def started(self) -> bool:
        return self._runtime.started

    def submit_take(self, request: TakeBookRequest) -> WorkflowTask:
        # BookArmSDK.take_book would create a second task; executing the
        # take-book use case directly keeps the task returned by submit.
        return self._submit(
            WorkflowKind.TAKE_BOOK,
            request,
            self._runtime.sdk._take_book.execute,
            "takeout-sdk-task",
        )

    def submit_return(self, request: ReturnBookRequest) -> WorkflowTask:
        # Same private-use-case shortcut as submit_take, for return_book.
        return self._submit(
            WorkflowKind.RETURN_BOOK,
            request,
            self._runtime.sdk._return_book.execute,
            "return-sdk-task",
        )

    def submit_mid_return(self, request: MidReturnBookRequest) -> WorkflowTask:
        use_case = self._runtime.sdk._mid_return
        if use_case is None:
            raise RuntimeError("mid-return workflow is not wired into this SDK instance")
        return self._submit(
            WorkflowKind.MID_RETURN_BOOK,
            request,
            use_case.execute,
            "midreturn-sdk-task",
        )

    def _submit(
        self,
        kind: WorkflowKind,
        request: Any,
        execute: Callable[[WorkflowTask, Any], Any],
        thread_name: str,
    ) -> WorkflowTask:
        if not self._runtime.started:
            raise RuntimeError("bookarm runtime is not started")
        if not self._busy.acquire(blocking=False):
            raise RunnerBusyError("a bookarm workflow task is already running")
        try:
            task = self._tasks.create(kind)
            self._active_task_id = task.id.value
            self._worker = threading.Thread(
                target=self._execute,
                args=(task, request, execute),
                name=thread_name,
                daemon=True,
            )
            self._worker.start()
            return task
        except Exception:
            self._active_task_id = None
            self._busy.release()
            raise

    def _execute(
        self,
        task: WorkflowTask,
        request: Any,
        execute: Callable[[WorkflowTask, Any], Any],
    ) -> None:
        try:
            execute(task, request)
        except Exception as error:
            _fail_task(self._tasks, task, error)
        finally:
            self._active_task_id = None
            self._busy.release()

    def status(self, task_id: str) -> WorkflowTask:
        return self._tasks.get(str(task_id))

    def cancel(self, task_id: str) -> WorkflowTask:
        task = self._tasks.get(str(task_id))
        try:
            return self._tasks.cancel(str(task_id))
        except RuntimeError:
            # Terminal tasks cannot be cancelled; report their final view.
            return task

    def dismiss_recovery(self, task_id: str) -> WorkflowTask:
        return self._tasks.dismiss_recovery(str(task_id))

    def interrupt_active(self) -> Optional[WorkflowTask]:
        task_id = self._active_task_id
        if task_id is None:
            return None
        try:
            return self._tasks.interrupt(task_id)
        except KeyError:
            return None

    def shutdown(self) -> None:
        worker = self._worker
        if worker is None or not worker.is_alive():
            return
        # Closing CAN, serial or camera handles under a still-running
        # workflow can leave a physical operation half-owned.  Wait for its
        # own bounded device timeouts, but only up to the configured budget:
        # a stuck device call (e.g. a hung CAN write) must not suspend
        # `systemctl stop` forever.  When the budget expires the worker is
        # abandoned; it is a daemon thread, so the process still exits and
        # JsonTaskStore records the interrupted task as recovery_required on
        # restart.  If the exit path ever starts blocking on that thread
        # again (a non-daemon worker or an atexit join), escalate to
        # os._exit(1) here as the last resort instead of hanging the stop.
        worker.join(timeout=self._shutdown_timeout_s)
        if worker.is_alive():
            print(
                "bookarm workflow worker did not finish within %.1fs during "
                "shutdown; abandoning the stuck thread and exiting "
                "(interrupted task restarts as recovery_required)"
                % self._shutdown_timeout_s,
                file=sys.stderr,
            )


class FakeWorkflowRunner(WorkflowRunner):
    """Simulate the take-book and return-book stages without any hardware."""

    TAKE_STAGE_PLAN: Tuple[Tuple[str, bool], ...] = (
        (TakeBookStage.LIFT_MOVE.value, False),
        (TakeBookStage.VISION_TARGET.value, False),
        (TakeBookStage.ARM_DEBUG_POSE.value, True),
        (TakeBookStage.BOOK_GRASP.value, True),
        (TakeBookStage.BOOK_STAND_PLACE.value, True),
        (TakeBookStage.ARM_STANDBY.value, True),
    )
    RETURN_STAGE_PLAN: Tuple[Tuple[str, bool], ...] = (
        (ReturnBookStage.LIFT_MOVE.value, False),
        (ReturnBookStage.ARM_DEBUG_POSE.value, True),
        (ReturnBookStage.VISION_RETARGET.value, False),
        (ReturnBookStage.BOOK_STAND_PICKUP.value, True),
        (ReturnBookStage.INSERT_TARGET.value, True),
        (ReturnBookStage.REACHABILITY_CHECK.value, False),
        (ReturnBookStage.SHELF_INSERT.value, True),
    )
    MID_RETURN_STAGE_PLAN: Tuple[Tuple[str, bool], ...] = (
        (MidReturnStage.LIFT_MOVE.value, False),
        (MidReturnStage.ARM_DEBUG_POSE.value, True),
        (MidReturnStage.VISION_TARGET.value, False),
        (MidReturnStage.BOOK_GRASP.value, True),
        (MidReturnStage.BOOK_PUT_BACK.value, True),
        (MidReturnStage.BOOK_STAND_PICKUP.value, True),
        (MidReturnStage.VISION_RETARGET.value, True),
        (MidReturnStage.INSERT_TARGET.value, False),
        (MidReturnStage.SHELF_INSERT.value, True),
    )
    # Backward-compatible name from the take-out-only service version.
    STAGE_PLAN = TAKE_STAGE_PLAN

    def __init__(
        self,
        *,
        stage_delay_s: float = 2.0,
        stage_hook: Optional[Callable[[str], None]] = None,
        task_manager: Optional[TaskManager] = None,
    ) -> None:
        """``stage_hook`` runs at every stage entry so tests can gate progress."""

        self._stage_delay_s = float(stage_delay_s)
        self._stage_hook = stage_hook
        self._tasks = task_manager or TaskManager()
        self._engine = WorkflowEngine(self._tasks)
        self._busy = threading.Lock()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._active_task_id: Optional[str] = None
        self._started = True
        # Live motion plans the tuning API reads and hot-swaps; the simulated
        # workflow stages themselves do not consume them.
        self.take_plan = TakeBookMotionPlan()
        self.return_plan = ReturnBookMotionPlan()
        self.mid_return_plan = MidReturnMotionPlan()

    @property
    def mode(self) -> str:
        return "fake"

    @property
    def started(self) -> bool:
        return self._started

    def submit_take(self, request: TakeBookRequest) -> WorkflowTask:
        return self._submit(WorkflowKind.TAKE_BOOK, request, "takeout-fake-task")

    def submit_return(self, request: ReturnBookRequest) -> WorkflowTask:
        return self._submit(WorkflowKind.RETURN_BOOK, request, "return-fake-task")

    def submit_mid_return(self, request: MidReturnBookRequest) -> WorkflowTask:
        return self._submit(WorkflowKind.MID_RETURN_BOOK, request, "midreturn-fake-task")

    def _submit(self, kind: WorkflowKind, request: Any, thread_name: str) -> WorkflowTask:
        if not self._started:
            raise RuntimeError("fake workflow runner is shut down")
        if not self._busy.acquire(blocking=False):
            raise RunnerBusyError("a bookarm workflow task is already running")
        try:
            task = self._tasks.create(kind)
            self._active_task_id = task.id.value
            self._worker = threading.Thread(
                target=self._execute,
                args=(task, request),
                name=thread_name,
                daemon=True,
            )
            self._worker.start()
            return task
        except Exception:
            self._active_task_id = None
            self._busy.release()
            raise

    def _execute(self, task: WorkflowTask, request: Any) -> None:
        try:
            self._engine.run(task, self._steps(task, request))
        except Exception as error:
            _fail_task(self._tasks, task, error)
        finally:
            self._active_task_id = None
            self._busy.release()

    def _steps(self, task: WorkflowTask, request: Any) -> Tuple[WorkflowStep, ...]:
        if isinstance(request, MidReturnBookRequest):
            plan = self.MID_RETURN_STAGE_PLAN
            label = "mid-return"
        elif isinstance(request, ReturnBookRequest):
            plan = self.RETURN_STAGE_PLAN
            label = "return-book"
        else:
            plan = self.TAKE_STAGE_PLAN
            label = "take-book"
        names = [name for name, _recovery in plan]
        try:
            stop_index = names.index(request.stop_after_stage.value)
        except ValueError as error:
            raise ValueError(
                "unsupported %s stop stage: %s" % (label, request.stop_after_stage)
            ) from error
        return tuple(
            (name, self._stage_action(name, task, request), recovery_required)
            for name, recovery_required in plan[: stop_index + 1]
        )

    def _stage_action(
        self, stage: str, task: WorkflowTask, request: Any
    ) -> Callable[[], Dict[str, Any]]:
        def action() -> Dict[str, Any]:
            if self._stage_hook is not None:
                self._stage_hook(stage)
            deadline = time.monotonic() + self._stage_delay_s
            while not self._stop_event.is_set() and task.state is not TaskState.CANCELLED:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                time.sleep(min(0.02, remaining))
            return self._stage_details(stage, request)

        return action

    @staticmethod
    def _stage_details(stage: str, request: Any) -> Dict[str, Any]:
        details: Dict[str, Any] = {"fake": True, "book": request.book.value}
        if stage == TakeBookStage.LIFT_MOVE.value:
            # lift_move opens both workflows with the same lift command.
            details.update(
                {
                    "lift_target_height_mm": request.target_height_mm,
                    "lift_speed_level": request.lift_speed_level,
                }
            )
        if isinstance(request, MidReturnBookRequest):
            if stage in (MidReturnStage.VISION_TARGET.value, MidReturnStage.VISION_RETARGET.value):
                details["left_neighbor_spine_template"] = request.left_neighbor_spine_template.path
            elif stage == MidReturnStage.BOOK_STAND_PICKUP.value:
                details["book_stand_source_slot"] = int(request.source_slot.value)
        elif isinstance(request, ReturnBookRequest):
            if stage == ReturnBookStage.BOOK_STAND_PICKUP.value:
                details["book_stand_source_slot"] = int(request.source_slot.value)
            elif stage == ReturnBookStage.VISION_RETARGET.value:
                details["left_neighbor_spine_template"] = request.left_neighbor_spine_template.path
        else:
            if stage == TakeBookStage.VISION_TARGET.value:
                details["spine_template"] = request.spine_template.path
            elif stage == TakeBookStage.BOOK_STAND_PLACE.value:
                details["book_stand_slot"] = int(request.destination_slot.value)
        return details

    def status(self, task_id: str) -> WorkflowTask:
        return self._tasks.get(str(task_id))

    def cancel(self, task_id: str) -> WorkflowTask:
        task = self._tasks.get(str(task_id))
        try:
            return self._tasks.cancel(str(task_id))
        except RuntimeError:
            # Terminal tasks cannot be cancelled; report their final view.
            return task

    def dismiss_recovery(self, task_id: str) -> WorkflowTask:
        return self._tasks.dismiss_recovery(str(task_id))

    def interrupt_active(self) -> Optional[WorkflowTask]:
        task_id = self._active_task_id
        if task_id is None:
            return None
        try:
            return self._tasks.interrupt(task_id)
        except KeyError:
            return None

    def shutdown(self) -> None:
        self._started = False
        self._stop_event.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=1.0)


# Backward-compatible names from the take-out-only service version.
TakeoutRunner = WorkflowRunner
SdkTakeoutRunner = SdkWorkflowRunner
FakeTakeoutRunner = FakeWorkflowRunner
