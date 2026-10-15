"""Transport-neutral task status and terminal result contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bookarm.domain.models.task import StageStatus, TaskId, TaskState, WorkflowKind


@dataclass(frozen=True)
class StageResult:
    name: str
    status: StageStatus
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowTask:
    id: TaskId
    kind: WorkflowKind
    state: TaskState = TaskState.QUEUED
    stages: List[StageResult] = field(default_factory=list)
    result: Optional["WorkflowResult"] = None


@dataclass(frozen=True)
class WorkflowResult:
    task_id: TaskId
    kind: WorkflowKind
    state: TaskState
    stages: List[StageResult]
    error: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
