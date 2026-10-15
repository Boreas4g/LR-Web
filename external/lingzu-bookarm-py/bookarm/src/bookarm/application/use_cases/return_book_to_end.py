"""Registered extension point for the future end-position return workflow."""

from __future__ import annotations

from bookarm.api.requests import ReturnBookToEndRequest
from bookarm.api.results import WorkflowResult, WorkflowTask
from bookarm.application.task_manager import TaskManager
from bookarm.application.workflow_engine import WorkflowEngine
from bookarm.domain.models.task import WorkflowKind


class ReturnBookToEndUseCase:
    def __init__(self, task_manager: TaskManager) -> None:
        self.task_manager = task_manager
        self.engine = WorkflowEngine(task_manager)

    def create(self, request: ReturnBookToEndRequest) -> WorkflowTask:
        request = None
        return self.task_manager.create(WorkflowKind.RETURN_BOOK_TO_END)

    def execute(self, task: WorkflowTask, request: ReturnBookToEndRequest) -> WorkflowResult:
        request = None
        return self.engine.unsupported(
            task,
            "return_book_to_end is registered but its end-placement plan has not been defined",
        )
