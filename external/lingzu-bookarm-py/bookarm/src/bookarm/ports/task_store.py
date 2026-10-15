"""Persistence boundary for resumable/auditable workflow tasks."""

from __future__ import annotations

from typing import Protocol

from bookarm.api.results import WorkflowTask
from bookarm.domain.models.task import TaskId


class TaskStore(Protocol):
    def save(self, task: WorkflowTask) -> None:
        ...

    def get(self, task_id: TaskId) -> WorkflowTask:
        ...

    def delete(self, task_id: TaskId) -> None:
        """Remove one task permanently; raise KeyError for unknown ids."""
        ...
