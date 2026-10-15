"""Strict readers for operator-maintained placement calibration."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from bookarm.domain.models.pose import BasePose
from bookarm.domain.models.shelf import BookStandPickupPlan, SlotPlacementPlan


def load_book_stand_pickup_slots(path: Path) -> Dict[int, BookStandPickupPlan]:
    """Load the three calibrated onboard slots used by return-book pickup."""

    source = _read_json(path, "book-stand pickup calibration")
    slots_value = source.get("slots")
    if not isinstance(slots_value, Mapping):
        raise ValueError("book-stand pickup calibration requires a slots object")
    slots: Dict[int, BookStandPickupPlan] = {}
    for number in (1, 2, 3, 4, 5):
        raw = slots_value.get(str(number), slots_value.get(number))
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            raise ValueError("book-stand pickup slot %d must be an object" % number)
        slots[number] = BookStandPickupPlan(
            pre_grasp=_pose(raw.get("pre_grasp"), "pickup slot %d pre_grasp" % number),
            grasp=_pose(raw.get("grasp"), "pickup slot %d grasp" % number),
            pre_grasp_duration_s=_positive_duration(
                raw.get("pre_grasp_duration_s", 3.0), "pre_grasp_duration_s"
            ),
            grasp_duration_s=_positive_duration(raw.get("grasp_duration_s", 2.0), "grasp_duration_s"),
            retreat_duration_s=_positive_duration(
                raw.get("retreat_duration_s", 2.0), "retreat_duration_s"
            ),
        )
    unexpected = set(str(key) for key in slots_value.keys()) - {"1", "2", "3", "4", "5"}
    if unexpected:
        raise ValueError(
            "book-stand pickup calibration has unsupported slots: %s"
            % ", ".join(sorted(unexpected))
        )
    if not slots:
        raise ValueError("book-stand pickup calibration contains no calibrated slots")
    return slots


def load_book_stand_slots(path: Path) -> Dict[int, SlotPlacementPlan]:
    """Load calibrated book-stand slots 1--4 from an operator-maintained file.

    A file may contain only the slots already calibrated. Requests for an
    omitted slot are rejected before any hardware command is sent.
    """

    source = _read_json(path, "book-stand calibration")
    slots_value = source.get("slots")
    if not isinstance(slots_value, Mapping):
        raise ValueError("book-stand calibration requires a slots object")
    slots: Dict[int, SlotPlacementPlan] = {}
    for number in (1, 2, 3, 4, 5):
        raw = slots_value.get(str(number), slots_value.get(number))
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            raise ValueError("book-stand slot %d must be an object" % number)
        slots[number] = SlotPlacementPlan(
            pre_place=_pose(raw.get("pre_place"), "slot %d pre_place" % number),
            place=_pose(raw.get("place"), "slot %d place" % number),
            lower_place=_pose(raw.get("lower_place"), "slot %d lower_place" % number),
            retreat=_pose(raw.get("retreat"), "slot %d retreat" % number),
            pre_place_duration_s=_positive_duration(raw.get("pre_place_duration_s", 3.0), "pre_place_duration_s"),
            place_duration_s=_positive_duration(raw.get("place_duration_s", 2.0), "place_duration_s"),
            lower_place_duration_s=_positive_duration(
                raw.get("lower_place_duration_s", 2.0), "lower_place_duration_s"
            ),
            retreat_duration_s=_positive_duration(raw.get("retreat_duration_s", 2.0), "retreat_duration_s"),
        )
    unexpected = set(str(key) for key in slots_value.keys()) - {"1", "2", "3", "4", "5"}
    if unexpected:
        raise ValueError("book-stand calibration has unsupported slots: %s" % ", ".join(sorted(unexpected)))
    if not slots:
        raise ValueError("book-stand calibration contains no calibrated slots")
    return slots


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as error:
        raise FileNotFoundError("%s file not found: %s" % (label, path)) from error
    except json.JSONDecodeError as error:
        raise ValueError("%s is not valid JSON: %s" % (label, error)) from error
    if not isinstance(value, Mapping):
        raise ValueError("%s root must be an object" % label)
    return value


def _pose(value: Any, label: str) -> BasePose:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 6:
        raise ValueError("%s must contain six calibrated values" % label)
    if any(item is None for item in value):
        raise ValueError("%s contains an uncalibrated value" % label)
    try:
        return BasePose.from_values(value)
    except (TypeError, ValueError) as error:
        raise ValueError("%s is invalid: %s" % (label, error)) from error


def _positive_duration(value: Any, name: str) -> float:
    duration = float(value)
    if not math.isfinite(duration) or duration <= 0.0:
        raise ValueError("%s must be a positive finite number" % name)
    return duration
