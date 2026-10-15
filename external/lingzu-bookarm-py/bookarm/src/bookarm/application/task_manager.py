"""Thread-safe task registry used by all bookarm workflows."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import uuid
from enum import Enum
from typing import Any, Dict, Optional

from bookarm.api.results import StageResult, WorkflowResult, WorkflowTask
from bookarm.domain.models.task import StageStatus, TaskId, TaskState, WorkflowKind
from bookarm.ports.task_store import TaskStore


class InMemoryTaskStore:
    """Default store; replace with SQLite through the same TaskStore port."""

    def __init__(self) -> None:
        self._tasks: Dict[str, WorkflowTask] = {}
        self._lock = threading.RLock()

    def save(self, task: WorkflowTask) -> None:
        with self._lock:
            self._tasks[task.id.value] = task

    def get(self, task_id: TaskId) -> WorkflowTask:
        with self._lock:
            try:
                return self._tasks[task_id.value]
            except KeyError as error:
                raise KeyError("unknown bookarm task: %s" % task_id.value) from error

    def delete(self, task_id: TaskId) -> None:
        with self._lock:
            try:
                del self._tasks[task_id.value]
            except KeyError as error:
                raise KeyError("unknown bookarm task: %s" % task_id.value) from error


def _jsonable(value: Any) -> Any:
    """Encode task details without allowing a runtime object into the journal."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Workflow detail dictionaries are part of the HTTP contract.  Preserve a
    # useful, serializable value rather than making a completed hardware task
    # impossible to record because a future adapter returned an exotic type.
    return str(value)


def _task_to_record(task: WorkflowTask) -> dict[str, Any]:
    result = task.result
    return {
        "id": task.id.value,
        "kind": task.kind.value,
        "state": task.state.value,
        "stages": [
            {
                "name": stage.name,
                "status": stage.status.value,
                "message": stage.message,
                "details": _jsonable(stage.details),
            }
            for stage in task.stages
        ],
        "result": (
            {
                "state": result.state.value,
                "error": result.error,
                "payload": _jsonable(result.payload),
            }
            if result is not None
            else None
        ),
    }


def _task_from_record(record: Any) -> WorkflowTask:
    """Validate and restore one task record from the on-disk journal."""

    if not isinstance(record, dict):
        raise ValueError("task record must be an object")
    task_id = record.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task record has no id")
    stages_value = record.get("stages", [])
    if not isinstance(stages_value, list):
        raise ValueError("task %s stages must be a list" % task_id)
    stages: list[StageResult] = []
    for stage in stages_value:
        if not isinstance(stage, dict):
            raise ValueError("task %s contains an invalid stage" % task_id)
        details = stage.get("details", {})
        if not isinstance(details, dict):
            raise ValueError("task %s stage details must be an object" % task_id)
        stages.append(
            StageResult(
                str(stage["name"]),
                StageStatus(str(stage["status"])),
                str(stage.get("message", "")),
                details,
            )
        )
    task = WorkflowTask(
        id=TaskId(task_id),
        kind=WorkflowKind(str(record["kind"])),
        state=TaskState(str(record["state"])),
        stages=stages,
    )
    result = record.get("result")
    if result is not None:
        if not isinstance(result, dict):
            raise ValueError("task %s result must be an object" % task_id)
        payload = result.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("task %s result payload must be an object" % task_id)
        task.result = WorkflowResult(
            task.id,
            task.kind,
            TaskState(str(result.get("state", task.state.value))),
            list(stages),
            result.get("error"),
            payload,
        )
    return task


class JsonTaskStore:
    """Crash-resilient task journal with atomic writes.

    A book-arm task can leave a book or an arm outside its verified safe pose.
    Therefore a daemon restart must not erase an in-flight task or silently
    present it as successful.  The journal is intentionally small and uses a
    replace-on-success write, so it has no extra server dependency and remains
    usable before PostgreSQL or a network service comes up.
    """

    VERSION = 1
    RECOVERY_MESSAGE = (
        "service restarted before this task reached a terminal state; "
        "inspect the hardware and perform operator recovery before continuing"
    )

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._tasks = self._load()
        if self._mark_interrupted_tasks():
            self._write()

    def save(self, task: WorkflowTask) -> None:
        with self._lock:
            self._tasks[task.id.value] = task
            self._write()

    def get(self, task_id: TaskId) -> WorkflowTask:
        with self._lock:
            try:
                return self._tasks[task_id.value]
            except KeyError as error:
                raise KeyError("unknown bookarm task: %s" % task_id.value) from error

    def delete(self, task_id: TaskId) -> None:
        with self._lock:
            try:
                del self._tasks[task_id.value]
            except KeyError as error:
                raise KeyError("unknown bookarm task: %s" % task_id.value) from error
            self._write()

    def _load(self) -> Dict[str, WorkflowTask]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != self.VERSION:
                raise ValueError("unsupported journal format")
            records = raw.get("tasks", [])
            if not isinstance(records, list):
                raise ValueError("tasks must be a list")
            tasks: Dict[str, WorkflowTask] = {}
            for record in records:
                task = _task_from_record(record)
                if task.id.value in tasks:
                    raise ValueError("duplicate task id")
                tasks[task.id.value] = task
            if len(tasks) != len(records):
                raise ValueError("duplicate task id")
            return tasks
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise RuntimeError("cannot load bookarm task journal %s: %s" % (self.path, error)) from error

    def _mark_interrupted_tasks(self) -> bool:
        changed = False
        for task in self._tasks.values():
            if task.state not in {TaskState.QUEUED, TaskState.RUNNING}:
                continue
            task.state = TaskState.RECOVERY_REQUIRED
            task.result = WorkflowResult(
                task.id,
                task.kind,
                TaskState.RECOVERY_REQUIRED,
                list(task.stages),
                self.RECOVERY_MESSAGE,
                {},
            )
            changed = True
        return changed

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "tasks": [_task_to_record(task) for task in self._tasks.values()],
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % self.path.name,
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                json.dump(payload, temporary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.path)
            # Ensure the rename itself survives a power loss where the host
            # filesystem supports syncing directories.
            try:
                directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
            except (AttributeError, OSError):
                return
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise


TERMINAL_STATES = {
    TaskState.SUCCEEDED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.RECOVERY_REQUIRED,
    TaskState.UNSUPPORTED,
}


class TaskManager:
    def __init__(self, store: Optional[TaskStore] = None) -> None:
        self.store = store or InMemoryTaskStore()
        self._interrupt_lock = threading.Lock()
        self._interrupted_ids: set[str] = set()

    def create(self, kind: WorkflowKind) -> WorkflowTask:
        task = WorkflowTask(id=TaskId(uuid.uuid4().hex), kind=kind)
        self.store.save(task)
        return task

    def get(self, task_id: str) -> WorkflowTask:
        return self.store.get(TaskId(str(task_id)))

    def save(self, task: WorkflowTask) -> None:
        self.store.save(task)

    def cancel(self, task_id: str) -> WorkflowTask:
        task = self.get(task_id)
        if task.state in TERMINAL_STATES:
            raise RuntimeError("terminal task cannot be cancelled")
        task.state = TaskState.CANCELLED
        self.save(task)
        return task

    def interrupt(self, task_id: str) -> WorkflowTask:
        """Flag a task as interrupted by a safety stop and cancel it.

        Unlike ``cancel`` this never rejects a terminal task: a safety stop
        must stay idempotent even when the workflow already finished.  The
        flag survives state writes, so the workflow engine can distinguish an
        interruption from a plain cancellation at every stage checkpoint.
        """

        task = self.get(task_id)
        with self._interrupt_lock:
            self._interrupted_ids.add(task.id.value)
        if task.state not in TERMINAL_STATES:
            task.state = TaskState.CANCELLED
            self.save(task)
        return task

    def is_interrupted(self, task_id: str) -> bool:
        with self._interrupt_lock:
            return str(task_id) in self._interrupted_ids

    def dismiss_recovery(self, task_id: str) -> WorkflowTask:
        """Remove a recovery_required task from the journal after takeover.

        The control station calls this once an operator confirms the hardware
        was inspected and taken over manually.  Dismissal erases the record:
        the task id is gone for good and later lookups raise KeyError.  Any
        other state is rejected so a live or honestly finished task can never
        be erased through this path.
        """

        task = self.get(task_id)
        if task.state is not TaskState.RECOVERY_REQUIRED:
            raise RuntimeError(
                "only a recovery_required task can be dismissed; task %s is %s"
                % (task.id.value, task.state.value)
            )
        self.store.delete(task.id)
        with self._interrupt_lock:
            self._interrupted_ids.discard(task.id.value)
        return task
