"""Shelf and book-stand destination models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .pose import BasePose


class BookStandSlot(int, Enum):
    SLOT_1 = 1
    SLOT_2 = 2
    SLOT_3 = 3
    SLOT_4 = 4
    SLOT_5 = 5


@dataclass(frozen=True)
class ShelfLocation:
    area: str
    row: int
    column: int


@dataclass(frozen=True)
class SlotPlacementPlan:
    """Validated pre-place, place, lower-place, and retreat poses for one destination."""

    pre_place: BasePose
    place: BasePose
    lower_place: BasePose
    retreat: BasePose
    pre_place_duration_s: float = 3.0
    place_duration_s: float = 2.0
    lower_place_duration_s: float = 2.0
    retreat_duration_s: float = 2.0


@dataclass(frozen=True)
class BookStandPickupPlan:
    """Calibrated poses for taking a book from one onboard book-stand slot."""

    pre_grasp: BasePose
    grasp: BasePose
    pre_grasp_duration_s: float = 3.0
    grasp_duration_s: float = 2.0
    retreat_duration_s: float = 2.0
