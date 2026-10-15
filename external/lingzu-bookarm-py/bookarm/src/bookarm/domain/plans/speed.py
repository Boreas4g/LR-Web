"""Three-level speed presets shared by the take/return/mid-return plans.

One ``speed_level`` uniformly scales every speed-coupled parameter of a
motion plan: all arm-motion ``*_duration_s`` fields, the rod speed word and
the gripper open/close speeds plus the gripper timing fields that track the
motion speed (command lead, stall window, contact hold). Everything else —
contact-judgement constants, torque/effort limits, tolerances, loop periods —
is deliberately left untouched so grasp safety semantics never change with
the level.

Scaling is multiplicative off the tuned base values (the values stored in a
profile document), so ``standard`` (all factors 1.0) is an exact identity:
a profile without ``speed_level`` behaves exactly as before. Levels:

- ``slow``: first-run / fine execution. Longer motions, gentler rod and
  gripper speeds, wider contact-judgement windows.
- ``standard``: the calibrated defaults; identical to legacy behaviour.
- ``fast``: removes the perceived pauses between steps. Gripper close runs
  ~3.6x the legacy 16.7 deg/s, arm motions take 0.6x the time, the rod word
  rises to 1.8x. Contact force is still bounded by the unchanged effort
  limit; only the approach speed and the settle windows change.

Level justifications:

- Arm durations: the trajectory executor always runs the full ``duration_s``
  (devices/arm/service.py waits on each trajectory point's ``time_s``), so a
  uniform scale is the only lever short of re-planning trajectories. 0.6x
  keeps the longest joint move at 1.8 s, well inside the MoveL/MoveJ
  velocity/acceleration guards (the executor re-derives and re-clamps them).
- Rod speed word: the firmware (SMS/STS-class serial servo, ``WritePosEx``)
  accepts a raw speed word; 1000 is the calibrated default and 1800 stays
  comfortably below the servo's several-thousand word range.
- Gripper speeds: joint 7 runs MIT-mode frames whose torque is capped by
  ``effort`` regardless of approach speed, and the URDF velocity limit
  (50 rad/s motor-side) is orders above; the unit suite already exercises
  the monitored close at 720 deg/s. 3.6x is therefore a tracking change,
  not a safety change.
- ``command_lead_s`` scales inversely with the speed scale so the command's
  degree lead over feedback (speed x lead) stays in the same few-degree band;
  the ``stall_lead_threshold_deg`` reachability check in the service keeps
  passing at every level.
- ``stall_time_s``/``contact_hold_duration_s`` are the settle windows the
  user perceives as pauses after contact; they shrink on ``fast`` and widen
  on ``slow`` but are never removed — they are the book-present judgement.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, TypeVar, Union

from bookarm.domain.plans.mid_return import MidReturnMotionPlan
from bookarm.domain.plans.return_book import ReturnBookMotionPlan
from bookarm.domain.plans.take_book import GripperCommand, TakeBookMotionPlan


SPEED_LEVELS = ("slow", "standard", "fast")
DEFAULT_SPEED_LEVEL = "standard"


@dataclass(frozen=True)
class SpeedProfile:
    """Multipliers applied on top of the tuned base values for one level."""

    duration_scale: float = 1.0
    rod_speed_scale: float = 1.0
    gripper_speed_scale: float = 1.0
    gripper_command_lead_scale: float = 1.0
    gripper_stall_time_scale: float = 1.0
    gripper_contact_hold_scale: float = 1.0


SPEED_PROFILES: Mapping[str, SpeedProfile] = {
    "slow": SpeedProfile(
        duration_scale=1.5,
        rod_speed_scale=0.7,
        gripper_speed_scale=0.7,
        gripper_command_lead_scale=1.6,
        gripper_stall_time_scale=1.3,
        gripper_contact_hold_scale=1.2,
    ),
    "standard": SpeedProfile(),
    "fast": SpeedProfile(
        duration_scale=0.6,
        rod_speed_scale=1.8,
        gripper_speed_scale=3.6,
        gripper_command_lead_scale=0.6,
        gripper_stall_time_scale=0.85,
        gripper_contact_hold_scale=0.6,
    ),
}


AnyMotionPlan = Union[TakeBookMotionPlan, ReturnBookMotionPlan, MidReturnMotionPlan]
PlanT = TypeVar("PlanT", TakeBookMotionPlan, ReturnBookMotionPlan, MidReturnMotionPlan)

# Every arm-motion duration field per plan type; slot-calibration durations
# (book-stand poses) are intentionally not part of the speed level.
_TAKE_DURATION_FIELDS = (
    "zero_duration_s",
    "pregrasp_duration_s",
    "approach_duration_s",
    "fine_grasp_duration_s",
    "post_grasp_duration_s",
    "debug_duration_s",
)
_RETURN_DURATION_FIELDS = (
    "zero_duration_s",
    "debug_duration_s",
    "pregrasp_duration_s",
    "pre_insert_duration_s",
    "insert_duration_s",
    "push_duration_s",
    "retract_duration_s",
)
_MID_RETURN_DURATION_FIELDS = (
    "zero_duration_s",
    "debug_duration_s",
    "pregrasp_duration_s",
    "approach_duration_s",
    "fine_grasp_duration_s",
    "push_in_duration_s",
    "re_push_duration_s",
    "lower_duration_s",
    "shift_duration_s",
    "carry_forward_duration_s",
    "forward_duration_s",
    "pre_insert_duration_s",
    "insert_duration_s",
    "push_duration_s",
    "retract_duration_s",
)


def validate_speed_level(value: object) -> str:
    """Return the normalized level or raise ``ValueError`` for bad input."""

    if not isinstance(value, str) or value not in SPEED_LEVELS:
        raise ValueError(
            "speed_level must be one of %s; got %r" % ("/".join(SPEED_LEVELS), value)
        )
    return value


def _duration_fields(plan: AnyMotionPlan) -> tuple:
    if isinstance(plan, TakeBookMotionPlan):
        return _TAKE_DURATION_FIELDS
    if isinstance(plan, MidReturnMotionPlan):
        return _MID_RETURN_DURATION_FIELDS
    if isinstance(plan, ReturnBookMotionPlan):
        return _RETURN_DURATION_FIELDS
    raise TypeError("unsupported motion plan type: %s" % type(plan).__name__)


def _scale_gripper(gripper: GripperCommand, profile: SpeedProfile, direction: float) -> GripperCommand:
    """Scale (direction=1) or un-scale (direction=-1) the speed-coupled fields."""

    def factor(scale: float) -> float:
        return scale if direction > 0 else 1.0 / scale

    return replace(
        gripper,
        open_speed_deg_s=round(gripper.open_speed_deg_s * factor(profile.gripper_speed_scale), 6),
        close_speed_deg_s=round(gripper.close_speed_deg_s * factor(profile.gripper_speed_scale), 6),
        command_lead_s=round(gripper.command_lead_s * factor(profile.gripper_command_lead_scale), 6),
        stall_time_s=round(gripper.stall_time_s * factor(profile.gripper_stall_time_scale), 6),
        contact_hold_duration_s=round(
            gripper.contact_hold_duration_s * factor(profile.gripper_contact_hold_scale), 6
        ),
    )


def _rescale(plan: PlanT, direction: float) -> PlanT:
    """Scale (direction=1) or un-scale (direction=-1) one plan by its level.

    Values round to 6 decimals both ways so apply-then-remove returns the
    exact tuned base values; the ``standard`` level short-circuits to an
    untouched plan so legacy behaviour is bit-identical.
    """

    level = validate_speed_level(getattr(plan, "speed_level", DEFAULT_SPEED_LEVEL))
    if level == DEFAULT_SPEED_LEVEL:
        return plan
    profile = SPEED_PROFILES[level]

    def factor(scale: float) -> float:
        return scale if direction > 0 else 1.0 / scale

    duration_factor = factor(profile.duration_scale)
    updates = {}
    for name in _duration_fields(plan):
        scaled = round(getattr(plan, name) * duration_factor, 6)
        # Never let a level push a motion below the executor's floor budget.
        updates[name] = max(0.5, scaled) if direction > 0 else scaled
    rod_factor = factor(profile.rod_speed_scale)
    if hasattr(plan, "rod_speed"):
        # Rod-less plans (return-book) have no rod word to scale.
        updates["rod_speed"] = int(round(float(plan.rod_speed) * rod_factor))
    updates["gripper"] = _scale_gripper(plan.gripper, profile, direction)
    return replace(plan, **updates)


def apply_speed_to_gripper(command: GripperCommand, speed_level: str) -> GripperCommand:
    """Scale the speed-coupled fields of any gripper command for one level.

    ``apply_speed_level`` already rescales ``plan.gripper``; this helper is
    for commands built outside the plan (the take-book opens deliberately
    use the hardware-verified default command shape, so only the level may
    adjust their speeds). ``standard`` returns the command untouched.
    """

    level = validate_speed_level(speed_level)
    if level == DEFAULT_SPEED_LEVEL:
        return command
    return _scale_gripper(command, SPEED_PROFILES[level], 1.0)


def apply_speed_level(plan: PlanT) -> PlanT:
    """Effective plan: base values scaled by the plan's ``speed_level``.

    The profile loaders call this after validation, so use cases, tuning
    sessions and previews always execute the level-adjusted values while the
    persisted profile keeps the tuned base values plus the level name.
    """

    return _rescale(plan, 1.0)


def remove_speed_scaling(plan: PlanT) -> PlanT:
    """Base plan: undo :func:`apply_speed_level`, keeping ``speed_level``.

    The tuning API stores this as its canonical profile document so merging
    a patch and re-validating never applies the level twice.
    """

    return _rescale(plan, -1.0)
