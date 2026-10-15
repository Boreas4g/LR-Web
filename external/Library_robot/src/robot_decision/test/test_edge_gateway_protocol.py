from datetime import UTC, datetime, timedelta

import pytest

from robot_decision.edge_gateway_protocol import (
    parse_cruise_route,
    parse_gateway_command,
    parse_auto_localize,
    parse_fine_tune,
    parse_initial_pose,
    parse_manual_jog,
    parse_map_switch,
    parse_navigation_goal,
    parse_bookarm_test_session_id,
    parse_bookarm_test_start,
    parse_lift_move,
    parse_mechanism_home,
)



def test_parse_navigation_command() -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=1)
    command = parse_gateway_command(
        (
            '{"commandId":"CMD-001","robotId":"LR-01","command":"navigate",'
            '"payload":{"mapId":"MAP-01","annotationId":"ANN-01","frameId":"map",'
            '"x":1.2,"y":-0.8,"yaw":1.57},"expiresAt":"'
            + expires_at.isoformat()
            + '","safetyLevel":"high"}'
        ).encode(),
        "LR-01",
    )

    goal = parse_navigation_goal(
        command.payload,
        {"MAP-01"},
        min_x=-2.0,
        max_x=2.0,
        min_y=-2.0,
        max_y=2.0,
        max_abs_yaw=6.284,
    )

    assert goal.map_id == "MAP-01"
    assert goal.x == 1.2


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"not json", "not valid UTF-8 JSON"),
        (
            b'{"commandId":"CMD-001","robotId":"LR-02","command":"navigate",'
            b'"payload":{},"expiresAt":"2099-01-01T00:00:00+00:00","safetyLevel":"high"}',
            "does not match",
        ),
        (
            b'{"commandId":"CMD-001","robotId":"LR-01","command":"navigate",'
            b'"payload":{},"expiresAt":"2000-01-01T00:00:00+00:00","safetyLevel":"high"}',
            "expired",
        ),
    ],
)
def test_rejects_unsafe_commands(payload: bytes, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_gateway_command(payload, "LR-01")


def test_rejects_target_on_other_map_or_outside_bounds() -> None:
    with pytest.raises(ValueError, match="not enabled"):
        parse_navigation_goal(
            {"mapId": "MAP-OTHER", "annotationId": "ANN-01", "frameId": "map", "x": 0, "y": 0, "yaw": 0},
            {"MAP-01"},
            min_x=-1,
            max_x=1,
            min_y=-1,
            max_y=1,
            max_abs_yaw=6.284,
        )
    with pytest.raises(ValueError, match="outside configured"):
        parse_navigation_goal(
            {"mapId": "MAP-01", "annotationId": "ANN-01", "frameId": "map", "x": 2, "y": 0, "yaw": 0},
            {"MAP-01"},
            min_x=-1,
            max_x=1,
            min_y=-1,
            max_y=1,
            max_abs_yaw=6.284,
        )


def test_parse_web_confirmed_initial_pose() -> None:
    pose = parse_initial_pose(
        {"mapId": "MAP-01", "frameId": "map", "x": 0.5, "y": -0.5, "yaw": 1.57},
        {"MAP-01"},
        min_x=-1,
        max_x=1,
        min_y=-1,
        max_y=1,
        max_abs_yaw=6.284,
    )
    assert pose.map_id == "MAP-01"
    assert pose.yaw == 1.57


def test_map_switch_only_accepts_a_registered_id_and_no_extra_payload() -> None:
    assert parse_map_switch({"mapId": "MAP-02"}, {"MAP-01", "MAP-02"}).map_id == "MAP-02"
    with pytest.raises(ValueError, match="not enabled"):
        parse_map_switch({"mapId": "MAP-OTHER"}, {"MAP-01"})
    with pytest.raises(ValueError, match="only mapId"):
        parse_map_switch({"mapId": "MAP-01", "yamlPath": "/tmp/unsafe.yaml"}, {"MAP-01"})


def test_parse_cruise_route_preserves_operator_selected_order() -> None:
    route = parse_cruise_route(
        {
            "mapId": "MAP-01",
            "frameId": "map",
            "loopCount": 2,
            "waypoints": [
                {"annotationId": "ANN-SECOND", "label": "第二点", "x": 0.5, "y": 0.2, "yaw": 0},
                {"annotationId": "ANN-FIRST", "label": "第一点", "x": -0.5, "y": -0.2, "yaw": 1.57},
            ],
        },
        {"MAP-01"},
        min_x=-1,
        max_x=1,
        min_y=-1,
        max_y=1,
        max_abs_yaw=6.284,
    )

    assert route.loop_count == 2
    assert [waypoint.annotation_id for waypoint in route.waypoints] == ["ANN-SECOND", "ANN-FIRST"]


def test_manual_jog_parses_a_bounded_lease_free_velocity_request() -> None:
    jog = parse_manual_jog({"linear": 0.2, "angular": -0.4, "durationMs": 500})
    assert jog.linear == 0.2
    assert jog.angular == -0.4
    assert jog.duration_ms == 500

    with pytest.raises(ValueError, match="duration"):
        parse_manual_jog({"linear": 0.1, "angular": 0, "durationMs": 1000})
    with pytest.raises(ValueError, match="duration"):
        parse_manual_jog({"linear": 0.1, "angular": 0, "durationMs": 50})


def test_lift_move_requires_explicit_acknowledgement_and_has_a_small_bound() -> None:
    assert parse_lift_move({"distanceCm": -2.5, "acknowledgeSafety": True}).distance_cm == -2.5
    with pytest.raises(ValueError, match="acknowledgeSafety"):
        parse_lift_move({"distanceCm": 1.0})
    with pytest.raises(ValueError, match="no more than 10"):
        parse_lift_move({"distanceCm": 10.1, "acknowledgeSafety": True})


def test_mechanism_home_requires_all_stationary_recovery_acknowledgements() -> None:
    home = parse_mechanism_home(
        {
            "taskId": "TSK-RECOVERY",
            "acknowledgeSafety": True,
            "acknowledgeBookSecured": True,
            "baseMustRemainStationary": True,
        }
    )

    assert home.task_id == "TSK-RECOVERY"
    assert home.book_secured is True

    with pytest.raises(ValueError, match="acknowledgeBookSecured"):
        parse_mechanism_home(
            {"taskId": "TSK-RECOVERY", "acknowledgeSafety": True, "baseMustRemainStationary": True}
        )
    with pytest.raises(ValueError, match="baseMustRemainStationary"):
        parse_mechanism_home(
            {"taskId": "TSK-RECOVERY", "acknowledgeSafety": True, "acknowledgeBookSecured": True}
        )


def test_bookarm_maintenance_test_requires_manual_mode_and_safety_acknowledgement() -> None:
    session = parse_bookarm_test_start(
        {
            "sessionId": "MNT-001",
            "workflowMode": "takeout",
            "stepMode": "manual",
            "acknowledgeSafety": True,
        }
    )
    assert session.session_id == "MNT-001"
    assert session.workflow_mode == "takeout"
    assert parse_bookarm_test_session_id({"sessionId": "MNT-001"}) == "MNT-001"

    with pytest.raises(ValueError, match="acknowledgeSafety"):
        parse_bookarm_test_start(
            {"sessionId": "MNT-001", "workflowMode": "takeout", "stepMode": "manual"}
        )
    with pytest.raises(ValueError, match="manual step mode"):
        parse_bookarm_test_start(
            {
                "sessionId": "MNT-001",
                "workflowMode": "takeout",
                "stepMode": "auto",
                "acknowledgeSafety": True,
            }
        )


def test_parse_auto_localize_command_defaults_to_the_active_map() -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=1)
    command = parse_gateway_command(
        (
            '{"commandId":"CMD-AL-001","robotId":"LR-01","command":"auto_localize",'
            '"payload":{},"expiresAt":"'
            + expires_at.isoformat()
            + '","safetyLevel":"high"}'
        ).encode(),
        "LR-01",
    )

    assert command.command == "auto_localize"
    assert parse_auto_localize(command.payload, "MAP-01") == "MAP-01"
    assert parse_auto_localize({"mapId": "MAP-01"}, "MAP-01") == "MAP-01"

    with pytest.raises(ValueError, match="not the active vehicle map"):
        parse_auto_localize({"mapId": "MAP-02"}, "MAP-01")
    with pytest.raises(ValueError, match="must contain only mapId"):
        parse_auto_localize({"mapId": "MAP-01", "x": 1.0}, "MAP-01")


def test_parse_fine_tune_command_defaults_yaw_and_map_to_active() -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=1)
    command = parse_gateway_command(
        (
            '{"commandId":"CMD-FT-001","robotId":"LR-01","command":"fine_tune",'
            '"payload":{"x":1.2,"y":-0.8},"expiresAt":"'
            + expires_at.isoformat()
            + '","safetyLevel":"high"}'
        ).encode(),
        "LR-01",
    )

    assert command.command == "fine_tune"
    target = parse_fine_tune(command.payload, min_x=-2.0, max_x=2.0, min_y=-2.0, max_y=2.0, max_abs_yaw=6.284)

    assert target.map_id is None
    assert target.x == 1.2
    assert target.y == -0.8
    assert target.yaw == 0.0


def test_parse_fine_tune_accepts_explicit_yaw_and_map_id() -> None:
    target = parse_fine_tune(
        {"mapId": "MAP-01", "x": 0.5, "y": -0.5, "yaw": 1.57},
        min_x=-1,
        max_x=1,
        min_y=-1,
        max_y=1,
        max_abs_yaw=6.284,
    )

    assert target.map_id == "MAP-01"
    assert target.x == 0.5
    assert target.y == -0.5
    assert target.yaw == 1.57


def test_parse_fine_tune_rejects_missing_or_invalid_fields() -> None:
    bounds = {"min_x": -1.0, "max_x": 1.0, "min_y": -1.0, "max_y": 1.0, "max_abs_yaw": 6.284}
    with pytest.raises(ValueError, match="缺少有效的 x"):
        parse_fine_tune({"y": 0.5}, **bounds)
    with pytest.raises(ValueError, match="缺少有效的 y"):
        parse_fine_tune({"x": 0.5}, **bounds)
    with pytest.raises(ValueError, match="必须是有限数值"):
        parse_fine_tune({"x": float("nan"), "y": 0.5}, **bounds)
    with pytest.raises(ValueError, match="yaw 必须是有限数值"):
        parse_fine_tune({"x": 0.5, "y": 0.5, "yaw": "0.0"}, **bounds)
    with pytest.raises(ValueError, match="标识符"):
        parse_fine_tune({"mapId": 12, "x": 0.5, "y": 0.5}, **bounds)


def test_parse_fine_tune_rejects_out_of_bounds_target_and_excessive_yaw() -> None:
    with pytest.raises(ValueError, match="超出当前地图边界"):
        parse_fine_tune({"x": 2.0, "y": 0.0}, min_x=-1, max_x=1, min_y=-1, max_y=1, max_abs_yaw=6.284)
    with pytest.raises(ValueError, match="yaw 超出允许范围"):
        parse_fine_tune(
            {"x": 0.0, "y": 0.0, "yaw": 1.0}, min_x=-1, max_x=1, min_y=-1, max_y=1, max_abs_yaw=0.5
        )
