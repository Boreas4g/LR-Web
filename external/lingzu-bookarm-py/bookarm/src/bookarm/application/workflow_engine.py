"""Reusable task lifecycle runner for present and future use cases."""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from bookarm.api.results import StageResult, WorkflowResult, WorkflowTask
from bookarm.application.task_manager import TaskManager
from bookarm.domain.models.task import StageStatus, TaskState


# The boolean says that a failed stage may leave the robot carrying a book or
# outside a verified safe pose. Such failures require operator recovery.
WorkflowStep = Tuple[str, Callable[[], Dict[str, Any]], bool]


class WorkflowCancelled(RuntimeError):
    """Abort a stage action because the task was cancelled or interrupted."""


class WorkflowEngine:
    """Run named steps and persist a terminal task result after every stage."""

    def __init__(self, task_manager: TaskManager) -> None:
        self.task_manager = task_manager

    def raise_if_cancelled(self, task: WorkflowTask) -> None:
        """Cooperative cancellation checkpoint for multi-motion stage actions.

        Mirrors the stage-boundary checks in ``run`` so a long chain of
        arm/gripper motions stops between sub-steps instead of running to its
        end; the messages match the boundary path on purpose.
        """

        if self.task_manager.is_interrupted(task.id.value):
            raise WorkflowCancelled("workflow interrupted by safety stop")
        if task.state == TaskState.CANCELLED:
            raise WorkflowCancelled("workflow cancelled")

    def run(self, task: WorkflowTask, steps: Iterable[WorkflowStep]) -> WorkflowResult:
        if self.task_manager.is_interrupted(task.id.value):
            # A safety stop that landed before the worker entered must not be
            # resurrected into RUNNING by this assignment.
            return self._terminal(task, TaskState.CANCELLED, "workflow interrupted by safety stop", {})
        task.state = TaskState.RUNNING
        self.task_manager.save(task)
        payload: Dict[str, Any] = {}
        for name, action, recovery_required in steps:
            if self.task_manager.is_interrupted(task.id.value):
                return self._terminal(
                    task, TaskState.CANCELLED, "workflow interrupted by safety stop", payload
                )
            if task.state == TaskState.CANCELLED:
                return self._terminal(task, TaskState.CANCELLED, "workflow cancelled", payload)
            task.stages.append(StageResult(name, StageStatus.RUNNING, "stage running"))
            self.task_manager.save(task)
            try:
                details = dict(action() or {})
            except WorkflowCancelled as error:
                # The stage stays recorded as running: it was in flight when
                # the cancel landed, and CANCELLED is not a stage failure.
                return self._terminal(task, TaskState.CANCELLED, str(error), payload)
            except Exception as error:
                task.stages[-1] = StageResult(name, StageStatus.FAILED, str(error))
                state = TaskState.RECOVERY_REQUIRED if recovery_required else TaskState.FAILED
                return self._terminal(task, state, str(error), payload)
            task.stages[-1] = StageResult(name, StageStatus.SUCCEEDED, "stage complete", details)
            payload.update(details)
            self.task_manager.save(task)
        return self._terminal(task, TaskState.SUCCEEDED, None, payload)

    def unsupported(self, task: WorkflowTask, reason: str) -> WorkflowResult:
        return self._terminal(task, TaskState.UNSUPPORTED, reason, {})

    def reject(self, task: WorkflowTask, stage: str, reason: str) -> WorkflowResult:
        """Persist a safe pre-motion rejection with its responsible stage."""

        task.stages.append(StageResult(stage, StageStatus.FAILED, reason))
        return self._terminal(task, TaskState.FAILED, reason, {})

    def _terminal(
        self,
        task: WorkflowTask,
        state: TaskState,
        error: Optional[str],
        payload: Dict[str, Any],
    ) -> WorkflowResult:
        task.state = state
        result = WorkflowResult(task.id, task.kind, state, list(task.stages), error, dict(payload))
        task.result = result
        self.task_manager.save(task)
        return result
