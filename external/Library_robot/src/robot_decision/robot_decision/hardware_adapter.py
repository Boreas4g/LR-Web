"""Semantic, robot-local hardware boundary for the Edge Gateway.

The control station sends only approved high-level commands.  This adapter
converts them into the small set of local MotorStudio calls that exist on the
vehicle, and never treats a REST response or worker receipt as physical proof.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from robot_decision.bookarm_client import BookArmApiError, BookArmClient, BookArmHealth


HARDWARE_CAPABILITIES_SCHEMA_VERSION = "lr-vehicle-hardware-v1"
DEFAULT_HARDWARE_PROFILE = "LR-01-edulite-a3-v1"


def _result(
    accepted: bool,
    physical_completion_verified: bool,
    reason: str,
    **details: Any,
) -> dict[str, Any]:
    """Use one conservative result shape for every semantic operation."""

    return {
        "accepted": bool(accepted),
        "physicalCompletionVerified": bool(physical_completion_verified),
        "reason": str(reason)[:300],
        **details,
    }


def conservative_hardware_capabilities(profile: str = DEFAULT_HARDWARE_PROFILE) -> dict[str, Any]:
    """Build the static claim used before any live local hardware access."""

    return {
        "schemaVersion": HARDWARE_CAPABILITIES_SCHEMA_VERSION,
        "profile": profile,
        "navigation": True,
        "mapSwitching": True,
        "manualJog": True,
        "softwareStop": True,
        "liftRelativeMotion": True,
        "liftHomeSensor": False,
        "liftAbsolutePositionFeedback": False,
        "liftTargetArrivalFeedback": False,
        "armMotion": True,
        "armFeedback": True,
        "armHomeFeedback": False,
        "rodMotion": True,
        "cameraCapture": True,
        "bookHandoffFeedback": False,
        "bookWorkflowModes": [],
    }


class HardwareAdapter:
    """Expose only local, evidence-backed hardware semantics to the Gateway."""

    def __init__(
        self,
        bookarm_client: BookArmClient,
        *,
        profile: str = DEFAULT_HARDWARE_PROFILE,
        base_is_stationary: Callable[[], bool] | None = None,
    ) -> None:
        cleaned_profile = str(profile).strip()
        if not cleaned_profile or len(cleaned_profile) > 80:
            raise ValueError("hardware profile must be a non-empty value up to 80 characters")
        self._bookarm_client = bookarm_client
        self._profile = cleaned_profile
        self._base_is_stationary = base_is_stationary or (lambda: False)

    def capabilities(self) -> dict[str, Any]:
        """Return static capabilities justified by the current local hardware API.

        The WebUI has controlled relative lift, arm, rod, and camera paths, but
        it does not expose independent lift homing, actual mm position, arrival,
        arm home, or book handoff feedback.  Those claims must stay false until
        their physical sensors are integrated into this adapter.
        """

        reader = getattr(self._bookarm_client, "capabilities", None)
        if callable(reader):
            value = reader()
            if isinstance(value, dict):
                return value
        return conservative_hardware_capabilities(self._profile)

    def bookarm_health(self) -> BookArmHealth:
        """Read the existing local health endpoint without issuing movement."""

        return self._bookarm_client.health()

    def health(self) -> dict[str, Any]:
        """Return a dynamic health snapshot with explicit unknown-safe fields."""

        health = self.bookarm_health()
        return {
            "armReady": health.arm_ready,
            "armConnected": health.arm_connected,
            "armFeedbackHealthy": health.arm_feedback_healthy,
            "armCanFps": health.arm_can_fps,
            "armFeedbackReason": health.arm_feedback_reason,
            "armEnabled": health.arm_enabled,
            "armState": health.arm_state,
            "rodReady": health.rod_ready,
            "liftReady": health.lift_ready,
            "liftAbsolutePositioningReady": health.lift_absolute_positioning_ready,
            "mechanismHomeReady": False,
            "cameraReady": health.camera_ready,
            "liftPositionMm": None,
            "liftHomed": False,
            "liftFaultCode": None,
            "armHomed": False,
            "armFaultCode": None,
            "rodFaultCode": None,
            "cameraFaultCode": None,
            "bookArmMessage": health.message,
        }

    def base_is_stationary(self) -> bool:
        """Require the Gateway's fresh encoder-backed stationary predicate."""

        try:
            return bool(self._base_is_stationary())
        except Exception:
            return False

    def move_lift_relative(self, distance_cm: float) -> dict[str, Any]:
        """Request a bounded relative move, never claiming physical arrival."""

        if not self.capabilities()["liftRelativeMotion"]:
            return _result(False, False, "relative lift motion is not accepted for this hardware profile")
        if not self.base_is_stationary():
            return _result(False, False, "base wheel feedback is not fresh and stationary")
        health = self.bookarm_health()
        if not health.lift_ready:
            return _result(False, False, health.message)
        try:
            receipt = self._bookarm_client.move_lift(float(distance_cm))
        except BookArmApiError as error:
            self.safe_stop()
            return _result(False, False, str(error))
        # The current endpoint uses relative pulse/driver control only.  A
        # completed worker job is a driver receipt, not an encoder measurement.
        return _result(
            True,
            False,
            "lift driver acknowledged the relative command; physical arrival is unverified",
            distanceCm=float(distance_cm),
            driverAcknowledged=True,
            driverResult=receipt,
        )

    def move_lift_absolute(self, target_mm: float, tolerance_mm: float) -> dict[str, Any]:
        """Reject unsupported layer positioning instead of estimating from pulses."""

        del target_mm, tolerance_mm
        return _result(
            False,
            False,
            "lift home sensor, absolute position feedback, or target-arrival feedback is unavailable",
        )

    def home_lift(self) -> dict[str, Any]:
        """Reject until a physical home switch is provided by the local service."""

        return _result(False, False, "lift home sensor is unavailable")

    def home_arm(self) -> dict[str, Any]:
        """Reject GUI zero pose as a substitute for verified arm home feedback."""

        return _result(False, False, "arm safe-home feedback is unavailable")

    def start_book_workflow(self, mode: str, context: dict[str, Any]) -> dict[str, Any]:
        """Start only a mode independently accepted for automatic operation."""

        modes = self.capabilities()["bookWorkflowModes"]
        if mode not in modes:
            return _result(False, False, "book workflow mode has not passed physical handoff acceptance")
        try:
            sdk_starter = getattr(
                self._bookarm_client,
                "start_take_book" if mode == "takeout" else "start_return_book",
                None,
            )
            if callable(sdk_starter) and mode in {"takeout", "putback"}:
                workflow = sdk_starter(context)
            else:
                workflow = self._bookarm_client.start_workflow(mode, auto=True)
        except BookArmApiError as error:
            self.safe_stop()
            return _result(False, False, str(error))
        return _result(True, False, "book workflow accepted; physical handoff is pending", workflow=workflow)

    def prepare_book_workflow(self, context: dict[str, Any]) -> BookArmHealth:
        """Run backend-specific non-motion checks before the base navigates."""

        sdk_preflight = getattr(self._bookarm_client, "prepare_book_workflow", None)
        if not callable(sdk_preflight):
            sdk_preflight = getattr(self._bookarm_client, "prepare_take_book", None)
        if callable(sdk_preflight):
            return sdk_preflight(context)
        health = self.bookarm_health()
        if not (health.lift_ready and health.camera_ready and health.rod_ready):
            raise BookArmApiError(health.message)
        return self._bookarm_client.prepare_arm()

    def workflow_state(self, workflow_id: str) -> dict[str, Any]:
        try:
            workflow = self._bookarm_client.workflow(workflow_id)
        except BookArmApiError as error:
            return _result(False, False, str(error))
        return _result(True, False, "workflow state read", workflow=workflow)

    def continue_workflow(self, workflow_id: str) -> dict[str, Any]:
        try:
            workflow = self._bookarm_client.continue_workflow(workflow_id)
        except BookArmApiError as error:
            self.safe_stop()
            return _result(False, False, str(error))
        return _result(True, False, "workflow step accepted; physical handoff is unverified", workflow=workflow)

    def cancel_workflow(self, workflow_id: str) -> dict[str, Any]:
        try:
            workflow = self._bookarm_client.cancel_workflow(workflow_id)
        except BookArmApiError as error:
            self.safe_stop()
            return _result(False, False, str(error))
        stop_result = self.safe_stop()
        return _result(
            True,
            False,
            "workflow cancellation requested",
            workflow=workflow,
            safeStopAccepted=stop_result["accepted"],
        )

    def safe_stop(self) -> dict[str, Any]:
        """Request local arm cancellation and lift stop without opening a network port."""

        errors: list[str] = []
        try:
            result = self._bookarm_client.safe_stop()
        except BookArmApiError as error:
            errors.append(str(error))
        if errors:
            return _result(False, False, "; ".join(errors))
        return _result(
            True,
            False,
            "local arm-cancel and lift-stop requests submitted",
            backendResult=result if isinstance(result, dict) else None,
        )

    def emergency_stop(self) -> dict[str, Any]:
        """Request the stronger local arm stop and also stop the lift path."""

        stop_result = self.safe_stop()
        try:
            self._bookarm_client.emergency_stop()
        except BookArmApiError as error:
            return _result(False, False, str(error), safeStopAccepted=stop_result["accepted"])
        return _result(True, False, "local emergency stop requested", safeStopAccepted=stop_result["accepted"])
