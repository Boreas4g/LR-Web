"""Public, transport-neutral facade for bookarm workflows."""

from __future__ import annotations

import time
from typing import Optional, Union

from bookarm.api.requests import (
    MidReturnBookRequest,
    ReturnBookRequest,
    ReturnBookToEndRequest,
    TakeBookRequest,
)
from bookarm.api.results import WorkflowResult, WorkflowTask
from bookarm.application.task_manager import TaskManager
from bookarm.application.use_cases.mid_return import MidReturnBookUseCase
from bookarm.application.use_cases.return_book import ReturnBookUseCase
from bookarm.application.use_cases.return_book_to_end import ReturnBookToEndUseCase
from bookarm.application.use_cases.take_book import TakeBookUseCase
from bookarm.domain.models.task import TaskId, TaskState


TaskReference = Union[str, TaskId]
_TERMINAL_STATES = frozenset({
    TaskState.SUCCEEDED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.UNSUPPORTED,
    TaskState.RECOVERY_REQUIRED,
})


class BookArmSDK:
    """The single public entry point used by upper-level applications.

    Workflows execute synchronously in this first version so an integration
    gets a deterministic terminal task before its call returns.  The task
    contracts and task store are intentionally retained so a queue-backed
    runner can be introduced without changing the API request shape.
    """

    def __init__(
        self,
        *,
        task_manager: TaskManager,
        take_book: TakeBookUseCase,
        return_book: ReturnBookUseCase,
        return_book_to_end: ReturnBookToEndUseCase,
        mid_return: Optional[MidReturnBookUseCase] = None,
    ) -> None:
        self._task_manager = task_manager
        self._take_book = take_book
        self._return_book = return_book
        self._return_book_to_end = return_book_to_end
        self._mid_return = mid_return

    def take_book(self, request: TakeBookRequest) -> WorkflowTask:
        task = self._take_book.create(request)
        self._take_book.execute(task, request)
        return task

    def return_book(self, request: ReturnBookRequest) -> WorkflowTask:
        task = self._return_book.create(request)
        self._return_book.execute(task, request)
        return task

    def mid_return_book(self, request: MidReturnBookRequest) -> WorkflowTask:
        if self._mid_return is None:
            raise RuntimeError("mid-return workflow is not wired into this SDK instance")
        task = self._mid_return.create(request)
        self._mid_return.execute(task, request)
        return task

    def return_book_to_end(self, request: ReturnBookToEndRequest) -> WorkflowTask:
        task = self._return_book_to_end.create(request)
        self._return_book_to_end.execute(task, request)
        return task

    def get_task(self, task_id: TaskReference) -> WorkflowTask:
        return self._task_manager.get(self._task_id_value(task_id))

    def wait(self, task_id: TaskReference, timeout_s: Optional[float] = None) -> WorkflowResult:
        """Return a terminal result, or raise ``TimeoutError`` if it is not ready."""

        deadline = None if timeout_s is None else time.monotonic() + max(0.0, float(timeout_s))
        while True:
            task = self.get_task(task_id)
            if task.state in _TERMINAL_STATES:
                if task.result is None:
                    raise RuntimeError("terminal task has no workflow result")
                return task.result
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("bookarm task did not finish before timeout")
            time.sleep(0.02)

    def cancel(self, task_id: TaskReference) -> WorkflowTask:
        """Request cancellation at the next workflow stage boundary.

        This does not stop an in-flight physical motion. An operator-confirmed
        disable operation owns that safety action.
        """

        return self._task_manager.cancel(self._task_id_value(task_id))

    def disable_arm(self, *, confirmed: bool, duration_s: float = 3.0) -> dict[str, object]:
        """Safely disable the arm only after the caller confirms operator approval.

        The operation first reaches and verifies the configured disable pose.
        It never sends a motor-disable frame when that positioning fails.
        """

        if confirmed is not True:
            raise ValueError("arm disable requires explicit operator confirmation")
        return self._take_book.disable_arm(duration_s)

    @staticmethod
    def _task_id_value(task_id: TaskReference) -> str:
        if isinstance(task_id, TaskId):
            return task_id.value
        return str(task_id)
