"""Validation shared by the MQTT callback and the ROS 2 Edge Gateway node."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SUPPORTED_COMMANDS = frozenset(
    {
        "navigate",
        "cruise",
        "switch_map",
        "set_initial_pose",
        "auto_localize",
        "fine_tune",
        "manual_jog",
        "bookarm_test_start",
        "bookarm_test_continue",
        "bookarm_test_cancel",
        "lift_move",
        "mechanism_home",
        "cancel",
        "stop",
        "emergency_stop",
    }
)


@dataclass(frozen=True)
class GatewayCommand:
    command_id: str
    robot_id: str
    command: str
    payload: dict[str, Any]
    expires_at: datetime


@dataclass(frozen=True)
class NavigationGoal:
    map_id: str
    annotation_id: str
    x: float
    y: float
    yaw: float


@dataclass(frozen=True)
class InitialPose:
    """An operator-confirmed pose used to initialize AMCL on the active map."""

    map_id: str
    x: float
    y: float
    yaw: float


@dataclass(frozen=True)
class FineTuneTarget:
    """A low-speed precise-alignment target in the frame of the active map.

    ``map_id`` is ``None`` when the control station targets whichever map is
    currently loaded; the node still rejects any explicit ID that is not the
    active vehicle map.
    """

    map_id: str | None
    x: float
    y: float
    yaw: float


@dataclass(frozen=True)
class MapSwitch:
    """A request to select one of the robot-local staged maps."""

    map_id: str


@dataclass(frozen=True)
class CruiseWaypoint:
    annotation_id: str
    label: str
    x: float
    y: float
    yaw: float


@dataclass(frozen=True)
class CruiseRoute:
    map_id: str
    loop_count: int
    waypoints: tuple[CruiseWaypoint, ...]


@dataclass(frozen=True)
class ManualJog:
    linear: float
    angular: float
    duration_ms: int


@dataclass(frozen=True)
class LiftMove:
    """A bounded maintenance lift adjustment in centimetres."""

    distance_cm: float


@dataclass(frozen=True)
class MechanismHome:
    """A recovery request that must not move the mobile base."""

    task_id: str
    book_secured: bool


@dataclass(frozen=True)
class BookArmTestSession:
    """A maintenance-only, step-confirmed MotorStudio workflow."""

    session_id: str
    workflow_mode: str


def parse_gateway_command(raw_payload: bytes, expected_robot_id: str, now: datetime | None = None) -> GatewayCommand:
    """Parse a platform command without trusting the MQTT payload."""
    if len(raw_payload) > 64 * 1024:
        raise ValueError("command payload exceeds 64 KiB")
    try:
        document = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("command payload is not valid UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise ValueError("command payload must be an object")

    command_id = _read_identifier(document, "commandId")
    robot_id = _read_robot_id(document, "robotId")
    if robot_id != expected_robot_id:
        raise ValueError("command robotId does not match this gateway")
    command = document.get("command")
    if command not in _SUPPORTED_COMMANDS:
        raise ValueError("command is not permitted on the Edge Gateway")
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("command payload field must be an object")
    if document.get("safetyLevel") != "high":
        raise ValueError("command safetyLevel must be high")

    expires_at = _parse_timestamp(document.get("expiresAt"), "expiresAt")
    current_time = now or datetime.now(UTC)
    if expires_at <= current_time:
        raise ValueError("command has expired")
    return GatewayCommand(
        command_id=command_id,
        robot_id=robot_id,
        command=command,
        payload=payload,
        expires_at=expires_at,
    )


def parse_navigation_goal(
    payload: dict[str, Any],
    allowed_map_ids: set[str],
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
    max_abs_yaw: float,
) -> NavigationGoal:
    """Validate a map-frame target against the map explicitly enabled on the robot."""
    if not allowed_map_ids:
        raise ValueError("gateway has no allowed map IDs configured")
    map_id = _read_identifier(payload, "mapId")
    if map_id not in allowed_map_ids:
        raise ValueError("mapId is not enabled on this robot")
    annotation_id = _read_identifier(payload, "annotationId")
    if payload.get("frameId") != "map":
        raise ValueError("navigation frameId must be map")
    x = _read_finite_number(payload, "x")
    y = _read_finite_number(payload, "y")
    yaw = _read_finite_number(payload, "yaw")
    if not min_x <= x <= max_x or not min_y <= y <= max_y:
        raise ValueError("navigation target is outside configured map bounds")
    if abs(yaw) > max_abs_yaw:
        raise ValueError("navigation yaw is outside the permitted range")
    return NavigationGoal(map_id=map_id, annotation_id=annotation_id, x=x, y=y, yaw=yaw)


def parse_initial_pose(
    payload: dict[str, Any],
    allowed_map_ids: set[str],
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
    max_abs_yaw: float,
) -> InitialPose:
    """Validate a map-frame AMCL initialization pose from a trusted operator."""
    if not allowed_map_ids:
        raise ValueError("gateway has no allowed map IDs configured")
    map_id = _read_identifier(payload, "mapId")
    if map_id not in allowed_map_ids:
        raise ValueError("mapId is not enabled on this robot")
    if payload.get("frameId") != "map":
        raise ValueError("initial pose frameId must be map")
    x = _read_finite_number(payload, "x")
    y = _read_finite_number(payload, "y")
    yaw = _read_finite_number(payload, "yaw")
    if not min_x <= x <= max_x or not min_y <= y <= max_y:
        raise ValueError("initial pose is outside configured map bounds")
    if abs(yaw) > max_abs_yaw:
        raise ValueError("initial pose yaw is outside the permitted range")
    return InitialPose(map_id=map_id, x=x, y=y, yaw=yaw)


def parse_fine_tune(
    payload: dict[str, Any],
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
    max_abs_yaw: float,
) -> FineTuneTarget:
    """Validate a low-speed fine-tune target; mapId defaults to the active map."""
    map_id_value = payload.get("mapId")
    if map_id_value is None:
        map_id = None
    elif isinstance(map_id_value, str) and _IDENTIFIER_PATTERN.fullmatch(map_id_value):
        map_id = map_id_value
    else:
        raise ValueError("精调 mapId 必须是 1-80 位标识符")
    x = _read_fine_tune_coordinate(payload, "x")
    y = _read_fine_tune_coordinate(payload, "y")
    yaw_value = payload.get("yaw", 0.0)
    if isinstance(yaw_value, bool) or not isinstance(yaw_value, (int, float)):
        raise ValueError("精调目标 yaw 必须是有限数值")
    yaw = float(yaw_value)
    if not math.isfinite(yaw):
        raise ValueError("精调目标 yaw 必须是有限数值")
    if not min_x <= x <= max_x or not min_y <= y <= max_y:
        raise ValueError("精调目标超出当前地图边界")
    if abs(yaw) > max_abs_yaw:
        raise ValueError("精调目标 yaw 超出允许范围")
    return FineTuneTarget(map_id=map_id, x=x, y=y, yaw=yaw)


def _read_fine_tune_coordinate(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"精调目标缺少有效的 {key} 坐标")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"精调目标 {key} 坐标必须是有限数值")
    return number


def parse_map_switch(payload: dict[str, Any], allowed_map_ids: set[str]) -> MapSwitch:
    """Accept only an ID registered locally by the vehicle maintainer."""

    if set(payload) != {"mapId"}:
        raise ValueError("map switch payload must contain only mapId")
    map_id = _read_identifier(payload, "mapId")
    if map_id not in allowed_map_ids:
        raise ValueError("mapId is not enabled on this robot")
    return MapSwitch(map_id=map_id)


def parse_auto_localize(payload: dict[str, Any], active_map_id: str) -> str:
    """Validate the optional auto-localize target map; default is the active map."""

    if not payload:
        return active_map_id
    if set(payload) != {"mapId"}:
        raise ValueError("auto localize payload must contain only mapId")
    map_id = _read_identifier(payload, "mapId")
    if map_id != active_map_id:
        raise ValueError("auto localize mapId is not the active vehicle map")
    return map_id


def parse_cruise_route(
    payload: dict[str, Any],
    allowed_map_ids: set[str],
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
    max_abs_yaw: float,
) -> CruiseRoute:
    """Validate the ordered local route that the gateway will execute."""
    if payload.get("frameId") != "map":
        raise ValueError("cruise frameId must be map")
    map_id = _read_identifier(payload, "mapId")
    if map_id not in allowed_map_ids:
        raise ValueError("mapId is not enabled on this robot")
    loop_count = payload.get("loopCount")
    if isinstance(loop_count, bool) or not isinstance(loop_count, int) or not 1 <= loop_count <= 10:
        raise ValueError("cruise loopCount must be an integer from 1 to 10")
    raw_waypoints = payload.get("waypoints")
    if not isinstance(raw_waypoints, list) or not 2 <= len(raw_waypoints) <= 50:
        raise ValueError("cruise must contain 2 to 50 waypoints")

    waypoints: list[CruiseWaypoint] = []
    for raw_waypoint in raw_waypoints:
        if not isinstance(raw_waypoint, dict):
            raise ValueError("cruise waypoint must be an object")
        annotation_id = _read_identifier(raw_waypoint, "annotationId")
        label = raw_waypoint.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 120:
            raise ValueError("cruise waypoint label must be a non-empty string")
        x = _read_finite_number(raw_waypoint, "x")
        y = _read_finite_number(raw_waypoint, "y")
        yaw = _read_finite_number(raw_waypoint, "yaw")
        if not min_x <= x <= max_x or not min_y <= y <= max_y:
            raise ValueError("cruise waypoint is outside configured map bounds")
        if abs(yaw) > max_abs_yaw:
            raise ValueError("cruise waypoint yaw is outside the permitted range")
        waypoints.append(CruiseWaypoint(annotation_id, label.strip(), x, y, yaw))
    return CruiseRoute(map_id=map_id, loop_count=loop_count, waypoints=tuple(waypoints))


def parse_manual_jog(payload: dict[str, Any]) -> ManualJog:
    """Validate one bounded manual velocity request; authorization is node state."""
    linear = _read_finite_number(payload, "linear")
    angular = _read_finite_number(payload, "angular")
    duration_ms = payload.get("durationMs")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or not 100 <= duration_ms <= 750:
        raise ValueError("manual jog duration must be 100 to 750 ms")
    return ManualJog(linear=linear, angular=angular, duration_ms=duration_ms)


def parse_lift_move(payload: dict[str, Any]) -> LiftMove:
    """Bound direct maintenance moves; the local worker remains authoritative."""
    if payload.get("acknowledgeSafety") is not True:
        raise ValueError("lift move requires acknowledgeSafety=true")
    distance_cm = _read_finite_number(payload, "distanceCm")
    if distance_cm == 0.0 or abs(distance_cm) > 10.0:
        raise ValueError("lift move distance must be greater than 0 and no more than 10 cm")
    return LiftMove(distance_cm=distance_cm)


def parse_mechanism_home(payload: dict[str, Any]) -> MechanismHome:
    """Accept only an explicitly acknowledged stationary recovery request."""

    if payload.get("acknowledgeSafety") is not True:
        raise ValueError("mechanism home requires acknowledgeSafety=true")
    if payload.get("acknowledgeBookSecured") is not True:
        raise ValueError("mechanism home requires acknowledgeBookSecured=true")
    if payload.get("baseMustRemainStationary") is not True:
        raise ValueError("mechanism home requires baseMustRemainStationary=true")
    return MechanismHome(task_id=_read_identifier(payload, "taskId"), book_secured=True)


def parse_bookarm_test_start(payload: dict[str, Any]) -> BookArmTestSession:
    if payload.get("acknowledgeSafety") is not True:
        raise ValueError("book-arm maintenance test requires acknowledgeSafety=true")
    session_id = _read_identifier(payload, "sessionId")
    workflow_mode = payload.get("workflowMode")
    if workflow_mode not in {"takeout", "putback", "putback2", "tail_putback"}:
        raise ValueError("book-arm maintenance workflow mode is not permitted")
    if payload.get("stepMode") != "manual":
        raise ValueError("book-arm maintenance test must use manual step mode")
    return BookArmTestSession(session_id=session_id, workflow_mode=workflow_mode)


def parse_bookarm_test_session_id(payload: dict[str, Any]) -> str:
    return _read_identifier(payload, "sessionId")


def _read_identifier(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{key} must be a 1-80 character identifier")
    return value


def _read_robot_id(document: dict[str, Any], key: str) -> str:
    value = _read_identifier(document, key)
    if not value.startswith("LR-"):
        raise ValueError(f"{key} must start with LR-")
    return value


def _read_finite_number(document: dict[str, Any], key: str) -> float:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{key} must be finite")
    return number


def _parse_timestamp(value: Any, key: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{key} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{key} must include a timezone")
    return parsed.astimezone(UTC)
