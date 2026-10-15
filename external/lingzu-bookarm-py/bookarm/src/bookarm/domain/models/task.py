"""Task lifecycle types shared by every bookarm workflow."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WorkflowKind(str, Enum):
    TAKE_BOOK = "take_book"
    RETURN_BOOK = "return_book"
    RETURN_BOOK_TO_END = "return_book_to_end"
    MID_RETURN_BOOK = "mid_return_book"


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"
    UNSUPPORTED = "unsupported"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class TaskId:
    value: str
