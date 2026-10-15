from types import SimpleNamespace

from robot_decision.bookarm_client import BookArmApiError
from robot_decision.hardware_adapter import HardwareAdapter


def _health(*, lift_ready: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        arm_ready=True,
        arm_connected=True,
        arm_feedback_healthy=True,
        arm_can_fps=80.0,
        arm_feedback_reason="healthy",
        arm_enabled=True,
        arm_state="ready",
        rod_ready=True,
        lift_ready=lift_ready,
        camera_ready=True,
        message="ready" if lift_ready else "lift unavailable",
    )


def test_current_profile_declares_only_evidence_backed_capabilities() -> None:
    client = SimpleNamespace(health=lambda: _health())
    adapter = HardwareAdapter(client, profile="LR-01-edulite-a3-v1", base_is_stationary=lambda: True)

    capabilities = adapter.capabilities()

    assert capabilities["schemaVersion"] == "lr-vehicle-hardware-v1"
    assert capabilities["liftRelativeMotion"] is True
    assert capabilities["liftHomeSensor"] is False
    assert capabilities["liftAbsolutePositionFeedback"] is False
    assert capabilities["liftTargetArrivalFeedback"] is False
    assert capabilities["armHomeFeedback"] is False
    assert capabilities["bookHandoffFeedback"] is False
    assert capabilities["bookWorkflowModes"] == []


def test_relative_lift_requires_stationary_encoder_evidence() -> None:
    client = SimpleNamespace(health=lambda: _health(), move_lift=lambda _distance: None)
    adapter = HardwareAdapter(client, base_is_stationary=lambda: False)

    result = adapter.move_lift_relative(1.0)

    assert result == {
        "accepted": False,
        "physicalCompletionVerified": False,
        "reason": "base wheel feedback is not fresh and stationary",
    }


def test_relative_lift_receipt_never_claims_physical_arrival() -> None:
    client = SimpleNamespace(
        health=lambda: _health(),
        move_lift=lambda _distance: {"accepted": True, "physical_completion_verified": True},
    )
    adapter = HardwareAdapter(client, base_is_stationary=lambda: True)

    result = adapter.move_lift_relative(-2.5)

    assert result["accepted"] is True
    assert result["physicalCompletionVerified"] is False
    assert result["driverAcknowledged"] is True


def test_lift_failure_requests_the_local_stop_chain() -> None:
    stops: list[bool] = []

    def move_lift(_distance: float) -> None:
        raise BookArmApiError("driver timeout")

    client = SimpleNamespace(
        health=lambda: _health(),
        move_lift=move_lift,
        safe_stop=lambda: stops.append(True),
    )
    adapter = HardwareAdapter(client, base_is_stationary=lambda: True)

    result = adapter.move_lift_relative(1.0)

    assert stops == [True]
    assert result["accepted"] is False
    assert result["physicalCompletionVerified"] is False
    assert result["reason"] == "driver timeout"


def test_unsupported_absolute_and_automatic_book_operations_are_rejected() -> None:
    client = SimpleNamespace(health=lambda: _health())
    adapter = HardwareAdapter(client, base_is_stationary=lambda: True)

    absolute = adapter.move_lift_absolute(680.0, 5.0)
    workflow = adapter.start_book_workflow("takeout", {})

    assert absolute["accepted"] is False
    assert absolute["physicalCompletionVerified"] is False
    assert workflow["accepted"] is False
    assert workflow["physicalCompletionVerified"] is False


def test_sdk_capabilities_and_context_are_delegated_without_reinterpreting_height() -> None:
    contexts = []
    capabilities = {
        "schemaVersion": "lr-vehicle-hardware-v1",
        "profile": "sdk",
        "bookWorkflowModes": ["takeout"],
    }
    client = SimpleNamespace(
        capabilities=lambda: capabilities,
        start_take_book=lambda context: contexts.append(context) or {
            "id": "BA-1",
            "active": True,
            "waiting": True,
            "step_index": 0,
            "step_count": 6,
        },
    )
    adapter = HardwareAdapter(client, base_is_stationary=lambda: True)
    context = {"targetHeightMm": 620.0, "spineTemplatePath": "/private/spine.jpg"}

    result = adapter.start_book_workflow("takeout", context)

    assert adapter.capabilities() is capabilities
    assert result["accepted"] is True
    assert contexts == [context]


def test_sdk_health_preserves_absolute_lift_readiness() -> None:
    health = _health()
    health.lift_absolute_positioning_ready = True
    adapter = HardwareAdapter(SimpleNamespace(health=lambda: health))

    snapshot = adapter.health()

    assert snapshot["liftAbsolutePositioningReady"] is True


def test_sdk_return_mode_uses_left_neighbor_context_and_return_entrypoint() -> None:
    contexts = []
    client = SimpleNamespace(
        capabilities=lambda: {"bookWorkflowModes": ["takeout", "putback"]},
        start_return_book=lambda context: contexts.append(context) or {"id": "BA-RETURN-1"},
    )
    adapter = HardwareAdapter(client, base_is_stationary=lambda: True)

    result = adapter.start_book_workflow(
        "putback",
        {
            "operation": "return",
            "targetHeightMm": 620.0,
            "leftNeighborSpineTemplatePath": "/private/left.jpg",
            "sourceSlot": 2,
        },
    )

    assert result["accepted"] is True
    assert contexts[0]["sourceSlot"] == 2
    assert contexts[0]["leftNeighborSpineTemplatePath"] == "/private/left.jpg"
