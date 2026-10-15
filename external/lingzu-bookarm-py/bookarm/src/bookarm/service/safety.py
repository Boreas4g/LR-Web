"""Immediate safety stop coordination behind the /api/v1/safety routes.

The controller fans one operator stop request out to every layer that can
halt motion: the in-flight workflow task is interrupted so the engine stops
issuing stage actions, the arm aborts its current motion (safe stop) or is
disabled in place (emergency stop), and the lift halts. Every step is
idempotent: with no task running and no device moving the request still
succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from bookarm.service.runner import WorkflowRunner


@dataclass(frozen=True)
class SafetyStopReport:
    """Outcome of one safety stop request across the stop chain."""

    accepted: bool
    armStopped: bool
    liftStopped: bool
    taskInterrupted: bool


class SafetyController:
    """Drive the immediate-stop chain for the HTTP safety endpoints."""

    def __init__(
        self,
        runner: WorkflowRunner,
        *,
        arm: Optional[Any] = None,
        lift: Optional[Any] = None,
    ) -> None:
        self._runner = runner
        self._arm = arm
        self._lift = lift

    def stop(self) -> SafetyStopReport:
        """Safe stop: interrupt the workflow and halt motion, motors stay on."""

        task = self._runner.interrupt_active()
        arm_stopped = self._stop_arm(emergency=False)
        lift_stopped = self._stop_lift()
        return SafetyStopReport(
            accepted=True,
            armStopped=arm_stopped,
            liftStopped=lift_stopped,
            taskInterrupted=task is not None,
        )

    def emergency_stop(self) -> SafetyStopReport:
        """Emergency stop: interrupt the workflow and disable the arm in place."""

        task = self._runner.interrupt_active()
        arm_stopped = self._stop_arm(emergency=True)
        lift_stopped = self._stop_lift()
        return SafetyStopReport(
            accepted=True,
            armStopped=arm_stopped,
            liftStopped=lift_stopped,
            taskInterrupted=task is not None,
        )

    def _stop_arm(self, *, emergency: bool) -> bool:
        arm = self._arm
        if arm is None:
            # No arm attached to this process (fake runner): no arm motion can
            # be ongoing, so the stopped state already holds.
            return True
        try:
            if emergency:
                return bool(arm.emergency_stop())
            arm.interrupt_motion()
        except Exception:
            return False
        return True

    def _stop_lift(self) -> bool:
        lift = self._lift
        if lift is None:
            return True
        try:
            return bool(lift.emergency_stop())
        except Exception:
            return False
