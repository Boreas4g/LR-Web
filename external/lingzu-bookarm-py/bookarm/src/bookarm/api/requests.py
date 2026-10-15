"""Versioned request contracts consumed by upper-level applications."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from bookarm.domain.models.book import BookIdentity, SpineTemplate
from bookarm.domain.models.shelf import BookStandSlot, ShelfLocation


class TakeBookStage(str, Enum):
    """Named take-book workflow stage that can be used as a safe test cutoff."""

    LIFT_MOVE = "lift_move"
    VISION_TARGET = "vision_target"
    POST_VISION_ALIGN = "post_vision_align"
    ARM_DEBUG_POSE = "arm_debug_pose"
    BOOK_GRASP = "book_grasp"
    BOOK_STAND_PLACE = "book_stand_place"
    ARM_STANDBY = "arm_standby"


class ReturnBookStage(str, Enum):
    """Named return-book stages available as supervised test cutoffs."""

    LIFT_MOVE = "lift_move"
    ARM_DEBUG_POSE = "arm_debug_pose"
    VISION_RETARGET = "vision_retarget"
    POST_VISION_ALIGN = "post_vision_align"
    BOOK_STAND_PICKUP = "book_stand_pickup"
    INSERT_TARGET = "insert_target"
    REACHABILITY_CHECK = "reachability_check"
    SHELF_INSERT = "shelf_insert"


class MidReturnStage(str, Enum):
    """Named mid-return stages available as supervised test cutoffs."""

    LIFT_MOVE = "lift_move"
    ARM_DEBUG_POSE = "arm_debug_pose"
    VISION_TARGET = "vision_target"
    POST_VISION_ALIGN = "post_vision_align"
    BOOK_GRASP = "book_grasp"
    BOOK_PUT_BACK = "book_put_back"
    BOOK_STAND_PICKUP = "book_stand_pickup"
    VISION_RETARGET = "vision_retarget"
    POST_VISION_RETARGET_ALIGN = "post_vision_retarget_align"
    INSERT_TARGET = "insert_target"
    SHELF_INSERT = "shelf_insert"


@dataclass(frozen=True)
class TakeBookRequest:
    book: BookIdentity
    target_height_mm: float
    spine_template: SpineTemplate
    destination_slot: BookStandSlot
    lift_speed_level: int = 1
    stop_after_stage: TakeBookStage = TakeBookStage.ARM_STANDBY


@dataclass(frozen=True)
class ReturnBookRequest:
    book: BookIdentity
    target_height_mm: float
    # The reference book to identify by its spine; the returned book is
    # inserted beside it (insert Y = its grasp y + the plan's y offset).
    left_neighbor_spine_template: SpineTemplate
    source_slot: BookStandSlot
    lift_speed_level: int = 1
    stop_after_stage: ReturnBookStage = ReturnBookStage.SHELF_INSERT


@dataclass(frozen=True)
class MidReturnBookRequest:
    book: BookIdentity
    target_height_mm: float
    left_neighbor_spine_template: SpineTemplate
    source_slot: BookStandSlot
    lift_speed_level: int = 1
    stop_after_stage: MidReturnStage = MidReturnStage.SHELF_INSERT
    # Stress-test shortcut: when set (6 values: x/y/z in metres, rx/ry/rz in
    # radians), skip the neighbor grasp+put-back and the fresh re-target; pick
    # up from the stand slot and run the insert chain (tuning steps 24-29)
    # anchored on this pose, e.g. the take-out flow's recognized grasp pose.
    injected_grasp_pose: Optional[Sequence[float]] = None


@dataclass(frozen=True)
class ReturnBookToEndRequest:
    book: BookIdentity
    source_slot: BookStandSlot
    destination: ShelfLocation
