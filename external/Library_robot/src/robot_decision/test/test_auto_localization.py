from datetime import UTC, datetime, timedelta
import json
import math
import time
from types import SimpleNamespace

import numpy as np
import pytest
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import OccupancyGrid
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from geometry_msgs.msg import TransformStamped
from tf2_msgs.msg import TFMessage

from robot_decision.auto_localization_node import AutoLocalizationNode
from robot_decision.pose_utils import yaw_to_quaternion
from robot_decision.scan_pose_matcher import normalize_angle

from std_srvs.srv import Trigger

from test_scan_pose_matcher import (
    MAP_ORIGIN_X,
    MAP_ORIGIN_Y,
    MAP_RESOLUTION,
    MAP_SIZE,
    U_ROOM_WALLS,
    draw_wall,
    raycast_scan,
)


def _set_map_bounds(node: AutoLocalizationNode) -> None:
    node.map_min_x = -2.0
    node.map_max_x = 2.0
    node.map_min_y = -2.0
    node.map_max_y = 2.0
    node._map_bounds_valid = True


def test_read_saved_pose_requires_current_map_and_fresh_timestamp(tmp_path) -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node.state_file = tmp_path / "pose.json"
    node.active_map_id = "MAP-01"
    node.max_saved_pose_age_seconds = 60.0
    _set_map_bounds(node)
    node.state_file.write_text(
        json.dumps(
            {
                "mapId": "MAP-01",
                "x": 1.2,
                "y": -0.3,
                "yaw": 0.5,
                "savedAt": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    assert node._read_saved_pose() == (1.2, -0.3, 0.0, 0.5)

    node.state_file.write_text(
        json.dumps(
            {
                "mapId": "MAP-01",
                "x": 1.2,
                "y": -0.3,
                "yaw": 0.5,
                "savedAt": (datetime.now(UTC) - timedelta(seconds=61)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    assert node._read_saved_pose() is None


def test_mark_localized_saves_a_confirmed_manual_pose() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _set_map_bounds(node)
    saved: list[tuple[float, float, float]] = []
    statuses: list[str] = []
    node._save_pose = lambda x, y, yaw: saved.append((x, y, yaw))
    node._set_status = statuses.append
    node._publish_initial_pose = lambda *_args: (_ for _ in ()).throw(AssertionError("must not republish"))

    node._mark_localized(1.2, -0.3, 0.5)

    assert saved == [(1.2, -0.3, 0.5)]
    assert statuses == ["localized"]


def test_later_stable_amcl_pose_recovers_after_manual_confirmation_prompt() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node._status = "manual_required"
    node._pose_seeded_for_map = True
    node.max_position_variance = 0.5
    node.max_yaw_variance = 0.35
    node.min_stable_samples = 1
    node._stable_samples = 0
    _set_map_bounds(node)
    node.get_logger = lambda: SimpleNamespace(info=lambda _message: None)
    localized: list[tuple[float, float, float]] = []
    node._mark_localized = lambda x, y, yaw, **_kwargs: localized.append((x, y, yaw))

    message = PoseWithCovarianceStamped()
    message.header.frame_id = "map"
    message.pose.pose.position.x = 1.2
    message.pose.pose.position.y = -0.3
    message.pose.pose.orientation.w = 1.0
    message.pose.covariance[0] = 0.1
    message.pose.covariance[7] = 0.1
    message.pose.covariance[35] = 0.1

    node._handle_amcl_pose(message)

    assert localized == [(1.2, -0.3, 0.0)]


def test_amcl_pose_is_ignored_until_the_filter_is_reseeded_for_the_active_map() -> None:
    """A confident pose carried over from the old map must never localize the robot."""

    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node._status = "manual_required"
    node._pose_seeded_for_map = False
    node.max_position_variance = 0.5
    node.max_yaw_variance = 0.35
    node.min_stable_samples = 1
    node._stable_samples = 0
    _set_map_bounds(node)
    node._mark_localized = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("an unseeded AMCL pose must not mark the robot localized")
    )

    message = PoseWithCovarianceStamped()
    message.header.frame_id = "map"
    message.pose.pose.position.x = 1.2
    message.pose.pose.position.y = -0.3
    message.pose.pose.orientation.w = 1.0
    message.pose.covariance[0] = 0.01
    message.pose.covariance[7] = 0.01
    message.pose.covariance[35] = 0.01

    node._handle_amcl_pose(message)

    assert node._stable_samples == 0


def test_localized_node_becomes_manual_required_when_amcl_covariance_is_unreliable() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node._status = "localized"
    node._pose_seeded_for_map = True
    node.max_position_variance = 0.5
    node.max_yaw_variance = 0.35
    node._stable_samples = 3
    node._last_stable_pose_at = 1.0
    _set_map_bounds(node)
    node.get_logger = lambda: SimpleNamespace(warn=lambda _message: None)
    statuses: list[str] = []
    node._set_status = statuses.append

    message = PoseWithCovarianceStamped()
    message.header.frame_id = "map"
    message.pose.pose.orientation.w = 1.0
    message.pose.covariance[0] = 0.6
    message.pose.covariance[7] = 0.1
    message.pose.covariance[35] = 0.1

    node._handle_amcl_pose(message)

    assert statuses == ["manual_required"]


def test_map_switch_discards_old_pose_and_restarts_localization() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node.active_map_id = "MAP-01"
    node._stable_samples = 3
    node._manual_initial_pose = (1.0, 2.0, 0.0)
    node._last_stable_pose_at = 1.0
    node._global_request_sent = True
    node._status = "localized"
    _set_map_bounds(node)
    node._map_switch_timer = None
    node._map_message = None
    node._map_received_at = None
    statuses = []
    node._set_status = statuses.append
    node.create_timer = lambda _period, _callback: SimpleNamespace(cancel=lambda: None)
    node.get_logger = lambda: SimpleNamespace(info=lambda _message: None)

    node._handle_map_switch(String(data="MAP-02"))

    assert node.active_map_id == "MAP-02"
    assert node._stable_samples == 0
    assert node._manual_initial_pose is None
    assert node._last_stable_pose_at is None
    assert node._global_request_sent is False
    assert node._pose_seeded_for_map is False
    assert statuses == ["starting"]


def test_map_switch_keeps_a_just_loaded_map() -> None:
    """The Gateway loads the new map before publishing the switch notice.

    The freshly received map must survive the switch handler, otherwise pose
    requests wait for a re-publication that never comes (LR-01, 2026-09-03).
    """

    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node.active_map_id = "MAP-01"
    node._stable_samples = 3
    node._manual_initial_pose = (1.0, 2.0, 0.0)
    node._last_stable_pose_at = 1.0
    node._global_request_sent = True
    node._status = "localized"
    _set_map_bounds(node)
    node._map_switch_timer = None
    fresh_map = OccupancyGrid()
    node._map_message = fresh_map
    node._map_received_at = time.monotonic()
    node._set_status = lambda _status: None
    node.create_timer = lambda _period, _callback: SimpleNamespace(cancel=lambda: None)
    node.get_logger = lambda: SimpleNamespace(info=lambda _message: None)

    node._handle_map_switch(String(data="MAP-02"))

    assert node._map_message is fresh_map
    assert node._pose_seeded_for_map is False


def test_auto_localization_rejects_a_precise_amcl_pose_outside_active_map() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node._status = "manual_required"
    node._pose_seeded_for_map = True
    node.max_position_variance = 0.5
    node.max_yaw_variance = 0.35
    node.min_stable_samples = 1
    node._stable_samples = 0
    node._manual_initial_pose = None
    node._map_bounds_rejection_logged = False
    _set_map_bounds(node)
    statuses: list[str] = []
    node._set_status = statuses.append
    node.get_logger = lambda: SimpleNamespace(info=lambda _message: None, warn=lambda _message: None)
    node._mark_localized = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("out-of-bounds pose must not be marked localized")
    )

    message = PoseWithCovarianceStamped()
    message.header.frame_id = "map"
    message.pose.pose.position.x = 0.0
    message.pose.pose.position.y = -2.01
    message.pose.pose.orientation.w = 1.0
    message.pose.covariance[0] = 0.01
    message.pose.covariance[7] = 0.01
    message.pose.covariance[35] = 0.01

    node._handle_amcl_pose(message)

    assert node._stable_samples == 0
    assert statuses == ["manual_required"]


def test_manual_pose_remains_localized_only_with_a_fresh_amcl_transform(monkeypatch) -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node._status = "localized"
    node._manual_pose_accepted = True
    node._last_map_to_odom_transform_at = 9.0
    node._last_stable_pose_at = 1.0
    node.pose_stale_timeout_seconds = 5.0
    statuses: list[str] = []
    node._publish_status = lambda: statuses.append("localized")
    node._set_status = lambda status: statuses.append(status)
    node.get_logger = lambda: SimpleNamespace(warn=lambda _message: None)
    monkeypatch.setattr("robot_decision.auto_localization_node.time.monotonic", lambda: 10.0)

    node._watchdog()

    assert node._last_stable_pose_at == 10.0
    assert statuses == ["localized"]

    node._last_map_to_odom_transform_at = 1.0
    node._watchdog()

    assert node._manual_pose_accepted is False
    assert statuses[-1] == "manual_required"


def test_tf_handler_accepts_only_the_amcl_map_to_odom_transform(monkeypatch) -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node._last_map_to_odom_transform_at = None
    message = TFMessage()
    unrelated = TransformStamped()
    unrelated.header.frame_id = "map"
    unrelated.child_frame_id = "base_link"
    map_to_odom = TransformStamped()
    map_to_odom.header.frame_id = "/map"
    map_to_odom.child_frame_id = "/odom"
    message.transforms = [unrelated, map_to_odom]
    monkeypatch.setattr("robot_decision.auto_localization_node.time.monotonic", lambda: 42.0)

    node._handle_tf(message)

    assert node._last_map_to_odom_transform_at == 42.0


def _make_map_and_scan(
    truth_x: float, truth_y: float, truth_yaw: float
) -> tuple[OccupancyGrid, LaserScan]:
    grid = np.zeros((MAP_SIZE, MAP_SIZE), dtype=np.int16)
    draw_wall(grid, 1.0, -2.0, 1.0, 2.0)
    map_message = OccupancyGrid()
    map_message.info.resolution = MAP_RESOLUTION
    map_message.info.width = MAP_SIZE
    map_message.info.height = MAP_SIZE
    map_message.info.origin.position.x = MAP_ORIGIN_X
    map_message.info.origin.position.y = MAP_ORIGIN_Y
    map_message.data = grid.astype(np.int8).flatten().tolist()
    angle_min, angle_increment, ranges = raycast_scan(
        truth_x, truth_y, truth_yaw, [((1.0, -2.0), (1.0, 2.0))]
    )
    scan_message = LaserScan()
    scan_message.angle_min = angle_min
    scan_message.angle_increment = angle_increment
    scan_message.ranges = ranges.tolist()
    return map_message, scan_message


def _stub_manual_pose_node(node: AutoLocalizationNode) -> None:
    node._status = "manual_required"
    node.smart_seed_enabled = True
    node.smart_seed_min_score = 0.25
    node.smart_seed_window_m = 1.0
    node.smart_seed_step_m = 0.05
    node.smart_seed_yaw_window_deg = 60.0
    node.smart_seed_yaw_step_deg = 5.0
    node.smart_seed_fine_window_m = 0.1
    node.smart_seed_fine_step_m = 0.02
    node.smart_seed_fine_yaw_deg = 6.0
    node.smart_seed_fine_yaw_step_deg = 2.0
    node._recent_republished_initial_poses = []
    node._stable_samples = 0
    node._manual_pose_accepted = False
    node._initial_pose_timers = []
    _set_map_bounds(node)
    node.get_logger = lambda: SimpleNamespace(info=lambda _message: None, warn=lambda _message: None)
    node._set_status = lambda _status: None
    node.create_timer = lambda _period, _callback: SimpleNamespace(cancel=lambda: None)


def _make_seed_message(x: float, y: float, yaw: float) -> PoseWithCovarianceStamped:
    message = PoseWithCovarianceStamped()
    message.header.frame_id = "map"
    message.pose.pose.position.x = x
    message.pose.pose.position.y = y
    message.pose.pose.orientation = yaw_to_quaternion(yaw)
    return message


@pytest.mark.parametrize("previous_status", ["localized", "restoring_saved_pose"])
def test_new_manual_seed_is_verified_even_after_localization(previous_status) -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _stub_manual_pose_node(node)
    node._status = previous_status
    node._pose_seeded_for_map = True
    node._set_status = lambda status: setattr(node, "_status", status)
    checked = []
    node._refine_manual_seed = lambda x, y, yaw: checked.append((x, y, yaw)) or (0.4, 0.6, 0.2)
    node.create_timer = lambda *_args: pytest.fail("publication must not start an acceptance timer")

    node._handle_manual_initial_pose(_make_seed_message(0.3, 0.5, 0.1))

    assert checked == [(0.3, 0.5, pytest.approx(0.1))]
    assert node._manual_initial_pose == (0.4, 0.6, 0.2)
    assert node._status == "manual_initializing"
    assert not node._manual_pose_accepted


def test_rejected_manual_seed_cannot_be_accepted_via_raw_amcl_covariance() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _stub_manual_pose_node(node)
    node._status = "localized"
    node._pose_seeded_for_map = True
    node._set_status = lambda status: setattr(node, "_status", status)
    node._refine_manual_seed = lambda *_args: None
    message = _make_seed_message(0.3, 0.5, 0.1)

    node._handle_manual_initial_pose(message)
    for _ in range(5):
        node._handle_amcl_pose(message)

    assert node._status == "manual_required"
    assert not node._pose_seeded_for_map


def test_amcl_convergence_does_not_reset_the_particle_filter() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _stub_manual_pose_node(node)
    node._status = "manual_initializing"
    node._pose_seeded_for_map = True
    node.max_position_variance = 0.5
    node.max_yaw_variance = 0.35
    node.min_stable_samples = 3
    node._set_status = lambda status: setattr(node, "_status", status)
    saved = []
    node._save_pose = lambda *pose: saved.append(pose)
    node._publish_initial_pose = lambda *_args: pytest.fail("convergence must not reseed AMCL")
    message = _make_seed_message(0.3, 0.5, 0.1)
    message.pose.covariance[0] = 0.02
    message.pose.covariance[7] = 0.02
    message.pose.covariance[35] = 0.01

    node._handle_amcl_pose(message)
    node._handle_amcl_pose(message)
    assert node._status == "manual_initializing"
    node._handle_amcl_pose(message)
    assert node._status == "localized"
    assert saved == [(0.3, 0.5, pytest.approx(0.1))]


def test_queued_pose_and_broad_initial_prior_do_not_count_as_convergence() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _stub_manual_pose_node(node)
    node._status = "manual_initializing"
    node._pose_seeded_for_map = True
    node._initial_pose_seeded_at_ns = 10_000_000_000
    node.max_position_variance = 0.5
    node.max_yaw_variance = 0.35
    node.initialization_max_position_variance = 0.04
    node.initialization_max_yaw_variance = 0.02
    node.min_stable_samples = 3
    node._mark_localized = lambda *_args: pytest.fail("initial pose is not converged")
    message = _make_seed_message(0.3, 0.5, 0.1)
    message.header.stamp.sec = 9
    for _ in range(5):
        node._handle_amcl_pose(message)
    assert node._stable_samples == 0
    message.header.stamp.sec = 11
    message.pose.covariance[0] = message.pose.covariance[7] = 0.25
    message.pose.covariance[35] = 0.068
    for _ in range(5):
        node._handle_amcl_pose(message)
    assert node._stable_samples == 0


def test_manual_initial_pose_is_rejected_when_refinement_is_unavailable() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _stub_manual_pose_node(node)
    node._map_message = None
    node._scan_message = None
    node._scan_received_at = None
    warnings: list[str] = []
    node.get_logger = lambda: SimpleNamespace(info=lambda _message: None, warn=warnings.append)
    statuses: list[str] = []
    node._set_status = statuses.append
    node._publish_initial_pose = lambda *_args: (_ for _ in ()).throw(
        AssertionError("must not republish without a refined pose")
    )

    node._handle_manual_initial_pose(_make_seed_message(1.0, 0.5, 0.3))

    assert getattr(node, "_manual_initial_pose", None) is None
    assert statuses == ["manual_initializing", "manual_required"]
    assert len(warnings) == 1


def test_manual_initial_pose_republishes_the_refined_seed_once() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _stub_manual_pose_node(node)
    truth_x, truth_y, truth_yaw = 0.2, 0.3, 0.4
    node._map_message, node._scan_message = _make_map_and_scan(truth_x, truth_y, truth_yaw)
    node._scan_received_at = time.monotonic()
    published: list[tuple[float, float, float]] = []
    node._publish_initial_pose = lambda x, y, _z, yaw: published.append((x, y, yaw))
    seed_x = truth_x + 0.3
    seed_y = truth_y - 0.25
    seed_yaw = truth_yaw + math.radians(15.0)

    node._handle_manual_initial_pose(_make_seed_message(seed_x, seed_y, seed_yaw))

    assert len(published) == 1
    refined_x, refined_y, refined_yaw = published[0]
    assert abs(refined_x - truth_x) < 0.05
    assert abs(refined_y - truth_y) < 0.05
    assert abs(math.degrees(refined_yaw - truth_yaw)) < 3.0
    assert node._manual_initial_pose == (refined_x, refined_y, refined_yaw)

    # The node's own refined republication must not be refined a second time.
    node._handle_manual_initial_pose(_make_seed_message(refined_x, refined_y, refined_yaw))

    assert len(published) == 1
    # The echo pose round-trips through a quaternion, so compare yaw loosely.
    assert node._manual_initial_pose is not None
    assert node._manual_initial_pose[0] == refined_x
    assert node._manual_initial_pose[1] == refined_y
    assert abs(node._manual_initial_pose[2] - refined_yaw) < 1e-9


def test_manual_initial_pose_is_rejected_when_refined_pose_is_out_of_bounds() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _stub_manual_pose_node(node)
    truth_x, truth_y, truth_yaw = 0.2, 0.3, 0.4
    node._map_message, node._scan_message = _make_map_and_scan(truth_x, truth_y, truth_yaw)
    node._scan_received_at = time.monotonic()
    # The bounds admit the rough seed but not the refined true pose.
    node.map_min_x = 0.5
    node.map_max_x = 2.0
    node.map_min_y = -2.0
    node.map_max_y = 2.0
    statuses: list[str] = []
    node._set_status = statuses.append
    node._publish_initial_pose = lambda *_args: (_ for _ in ()).throw(
        AssertionError("must not republish an out-of-bounds refined pose")
    )
    seed_x = truth_x + 0.4
    seed_y = truth_y - 0.35
    seed_yaw = truth_yaw + math.radians(15.0)

    node._handle_manual_initial_pose(_make_seed_message(seed_x, seed_y, seed_yaw))

    assert getattr(node, "_manual_initial_pose", None) is None
    assert statuses == ["manual_initializing", "manual_required"]


def _make_room_map_and_scan(
    truth_x: float, truth_y: float, truth_yaw: float
) -> tuple[OccupancyGrid, LaserScan]:
    grid = np.zeros((MAP_SIZE, MAP_SIZE), dtype=np.int16)
    for (x0, y0), (x1, y1) in U_ROOM_WALLS:
        draw_wall(grid, x0, y0, x1, y1)
    map_message = OccupancyGrid()
    map_message.info.resolution = MAP_RESOLUTION
    map_message.info.width = MAP_SIZE
    map_message.info.height = MAP_SIZE
    map_message.info.origin.position.x = MAP_ORIGIN_X
    map_message.info.origin.position.y = MAP_ORIGIN_Y
    map_message.data = grid.astype(np.int8).flatten().tolist()
    angle_min, angle_increment, ranges = raycast_scan(truth_x, truth_y, truth_yaw, U_ROOM_WALLS)
    scan_message = LaserScan()
    scan_message.angle_min = angle_min
    scan_message.angle_increment = angle_increment
    scan_message.ranges = ranges.tolist()
    return map_message, scan_message


def _stub_auto_localize_node(node: AutoLocalizationNode) -> None:
    _stub_manual_pose_node(node)
    node.auto_localize_coarse_step_m = 0.1
    node.auto_localize_coarse_yaw_step_deg = 45.0
    node.auto_localize_top_k = 5
    node.auto_localize_time_budget_s = 10.0
    node._auto_localize_running = False
    node._global_request_sent = False
    node._manual_initial_pose = None
    node.get_logger = lambda: SimpleNamespace(
        info=lambda _message: None,
        warn=lambda _message: None,
        error=lambda _message: None,
    )


def test_auto_localize_rejects_without_a_map_or_a_fresh_scan() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _stub_auto_localize_node(node)
    node._map_message = None
    node._scan_message = None
    node._scan_received_at = None
    node._request_global_localization = lambda: (_ for _ in ()).throw(
        AssertionError("must not spray particles without a match attempt")
    )
    node._publish_initial_pose = lambda *_args: (_ for _ in ()).throw(AssertionError("must not seed AMCL"))

    response = Trigger.Response()
    assert node._handle_auto_localize(Trigger.Request(), response) is response
    assert response.success is False
    assert "地图" in response.message

    node._map_message, node._scan_message = _make_room_map_and_scan(0.2, 0.3, 0.4)
    node._scan_received_at = time.monotonic() - 3.0

    response = Trigger.Response()
    node._handle_auto_localize(Trigger.Request(), response)

    assert response.success is False
    assert "扫描" in response.message


def test_auto_localize_seeds_amcl_with_the_matched_pose() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _stub_auto_localize_node(node)
    truth_x, truth_y, truth_yaw = 0.2, 0.3, 0.4
    node._map_message, node._scan_message = _make_room_map_and_scan(truth_x, truth_y, truth_yaw)
    node._scan_received_at = time.monotonic()
    published: list[tuple[float, float, float]] = []
    node._publish_initial_pose = lambda x, y, _z, yaw: published.append((x, y, yaw))
    statuses: list[str] = []
    node._set_status = statuses.append

    response = Trigger.Response()
    node._handle_auto_localize(Trigger.Request(), response)

    assert response.success is True
    assert len(published) == 1
    x, y, yaw = published[0]
    assert abs(x - truth_x) < 0.06
    assert abs(y - truth_y) < 0.06
    assert abs(math.degrees(normalize_angle(yaw - truth_yaw))) < 3.0
    assert node._manual_initial_pose == (x, y, yaw)
    assert statuses == ["manual_initializing"]
    assert f"|pose={x:.6f},{y:.6f},{yaw:.6f}" in response.message


def test_auto_localize_sprays_particles_when_the_match_is_not_trustworthy() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _stub_auto_localize_node(node)
    node._map_message, node._scan_message = _make_room_map_and_scan(0.2, 0.3, 0.4)
    # A scan without any valid ray can never produce a trustworthy match.
    node._scan_message.ranges = [math.inf] * len(node._scan_message.ranges)
    node._scan_received_at = time.monotonic()
    node._publish_initial_pose = lambda *_args: (_ for _ in ()).throw(AssertionError("must not seed AMCL"))
    sprayed: list[bool] = []
    node._request_global_localization = lambda: sprayed.append(True) or True

    response = Trigger.Response()
    node._handle_auto_localize(Trigger.Request(), response)

    assert response.success is False
    assert sprayed == [True]
    assert "撒粒子" in response.message


def test_auto_localize_rejects_while_a_previous_request_is_running() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _stub_auto_localize_node(node)
    node._auto_localize_running = True
    node._map_message = None
    node._scan_message = None
    node._scan_received_at = None

    response = Trigger.Response()
    node._handle_auto_localize(Trigger.Request(), response)

    assert response.success is False
    assert "正在执行" in response.message



def test_startup_pose_requests_wait_for_an_active_amcl_with_a_map(monkeypatch) -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node._status = "starting"
    node._map_message = None
    node._amcl_lifecycle_state = "active"
    node._startup_timer = SimpleNamespace(cancel=lambda: None)
    calls: list[object] = []
    future = SimpleNamespace(add_done_callback=lambda _callback: None)
    node.initial_pose_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True,
        call_async=lambda request: calls.append(request) or future,
    )
    node._read_saved_pose = lambda: (1.0, 2.0, 0.0, 0.5)
    statuses: list[str] = []
    node._set_status = statuses.append
    node.get_logger = lambda: SimpleNamespace(info=lambda _message: None)
    monkeypatch.setattr(
        "robot_decision.auto_localization_node.make_initial_pose",
        lambda *_args, **_kwargs: PoseWithCovarianceStamped(),
    )

    # No map yet: AMCL must not be called even though the service answers.
    node._begin_startup_localization()
    assert calls == []

    # Map received but AMCL only configured (inactive): still no call.
    node._map_message = OccupancyGrid()
    node._amcl_lifecycle_state = "inactive"
    node._begin_startup_localization()
    assert calls == []

    # Active AMCL + current-session map: the saved pose is restored.
    node._amcl_lifecycle_state = "active"
    node._begin_startup_localization()
    assert len(calls) == 1
    assert statuses == ["restoring_saved_pose"]


def test_global_localization_is_gated_until_amcl_is_ready() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node._global_request_sent = False
    node._map_message = None
    node._amcl_lifecycle_state = "active"
    sent: list[object] = []
    future = SimpleNamespace(add_done_callback=lambda _callback: None)
    node.global_localization_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True,
        call_async=lambda request: sent.append(request) or future,
    )
    statuses: list[str] = []
    node._set_status = statuses.append
    node.get_logger = lambda: SimpleNamespace(info=lambda _message: None, warn=lambda _message: None)

    assert node._request_global_localization() is False

    node._map_message = OccupancyGrid()
    node._amcl_lifecycle_state = "inactive"
    assert node._request_global_localization() is False
    assert sent == []
    assert statuses == []

    node._amcl_lifecycle_state = "active"
    assert node._request_global_localization() is True
    assert len(sent) == 1
    assert statuses == ["global_localizing"]


def test_map_switch_requires_a_fresh_map_before_relocalization() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node.active_map_id = "MAP-01"
    node._stable_samples = 3
    node._manual_initial_pose = (1.0, 2.0, 0.0)
    node._last_stable_pose_at = 1.0
    node._global_request_sent = True
    node._status = "localized"
    _set_map_bounds(node)
    node._map_switch_timer = None
    node._map_message = OccupancyGrid()
    node._map_received_at = None
    node._set_status = lambda _status: None
    node.create_timer = lambda _period, _callback: SimpleNamespace(cancel=lambda: None)
    node.get_logger = lambda: SimpleNamespace(info=lambda _message: None)

    node._handle_map_switch(String(data="MAP-02"))

    assert node._map_message is None


def test_watchdog_degrades_a_stuck_startup_to_manual_required() -> None:
    from rclpy.time import Time

    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node._status = "starting"
    node.startup_amcl_timeout_seconds = 120.0
    node._phase_started_at = Time(nanoseconds=1_000_000_000)
    statuses: list[str] = []
    node._set_status = statuses.append
    node.get_logger = lambda: SimpleNamespace(warn=lambda _message: None)

    node.get_clock = lambda: SimpleNamespace(now=lambda: Time(nanoseconds=200_000_000_000))
    node._watchdog()
    assert statuses == ["manual_required"]

    # Within the readiness budget the startup wait must continue.
    statuses.clear()
    node.get_clock = lambda: SimpleNamespace(now=lambda: Time(nanoseconds=30_000_000_000))
    node._watchdog()
    assert statuses == []


def test_saved_pose_failure_reenters_the_gated_startup_loop() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node._status = "restoring_saved_pose"
    node._global_request_sent = True
    node._map_message = None
    node._amcl_lifecycle_state = None
    node.global_localization_client = SimpleNamespace(wait_for_service=lambda timeout_sec: False)
    resets: list[bool] = []
    node._startup_timer = SimpleNamespace(reset=lambda: resets.append(True))
    statuses: list[str] = []
    node._set_status = statuses.append
    node.get_logger = lambda: SimpleNamespace(error=lambda _message: None, warn=lambda _message: None)

    class _FailedFuture:
        @staticmethod
        def result() -> None:
            raise RuntimeError("transport lost")

    node._handle_saved_pose_response(_FailedFuture())

    assert statuses == ["starting"]
    assert resets == [True]
    assert node._global_request_sent is False


def test_amcl_lifecycle_state_response_updates_readiness() -> None:
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node._amcl_state_query_pending = True
    future = SimpleNamespace(result=lambda: SimpleNamespace(current_state=SimpleNamespace(label="active")))

    node._handle_amcl_lifecycle_state_response(future)

    assert node._amcl_lifecycle_state == "active"
    assert node._amcl_state_query_pending is False


def test_auto_localized_pose_also_holds_on_tf_heartbeat(monkeypatch) -> None:
    """A motionless auto-localized robot must not flap to manual_required."""

    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    node._status = "localized"
    node._manual_pose_accepted = False
    node._last_map_to_odom_transform_at = 9.0
    # /amcl_pose has been silent well beyond the stale window, as happens
    # whenever the robot does not move.
    node._last_stable_pose_at = 1.0
    node.pose_stale_timeout_seconds = 5.0
    statuses: list[str] = []
    node._publish_status = lambda: statuses.append("localized")
    node._set_status = statuses.append
    node.get_logger = lambda: SimpleNamespace(warn=lambda _message: None)
    monkeypatch.setattr("robot_decision.auto_localization_node.time.monotonic", lambda: 10.0)

    node._watchdog()

    # Fresh map->odom heartbeat: still localized despite the silent pose topic.
    assert statuses == ["localized"]
    assert node._last_stable_pose_at == 10.0

    node._last_map_to_odom_transform_at = 1.0
    statuses.clear()
    node._watchdog()

    assert statuses == ["manual_required"]
    assert node._manual_pose_accepted is False


def test_manual_initial_pose_is_rejected_when_the_scan_does_not_match() -> None:
    """A random click away from the real robot must not become 'localized'."""
    node = AutoLocalizationNode.__new__(AutoLocalizationNode)
    _stub_manual_pose_node(node)
    truth_x, truth_y, truth_yaw = 0.2, 0.3, 0.4
    node._map_message, node._scan_message = _make_map_and_scan(truth_x, truth_y, truth_yaw)
    node._scan_received_at = time.monotonic()
    statuses: list[str] = []
    node._set_status = statuses.append
    node._publish_initial_pose = lambda *_args: (_ for _ in ()).throw(
        AssertionError("a rejected pose must not be republished")
    )

    # The scan was taken at (0.2, 0.3); a click at the far corner cannot match.
    node._handle_manual_initial_pose(_make_seed_message(-1.2, -1.2, 0.0))

    assert getattr(node, "_manual_initial_pose", None) is None
    assert statuses == ["manual_initializing", "manual_required"]
