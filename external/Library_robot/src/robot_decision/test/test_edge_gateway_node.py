import queue
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
from sensor_msgs.msg import Imu
from std_msgs.msg import String

from robot_decision.bookarm_client import BookArmHealth
from robot_decision.edge_gateway_node import EdgeGatewayNode
from robot_decision.edge_gateway_protocol import GatewayCommand
from robot_decision.map_registry import RegisteredMap


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[Twist] = []

    def publish(self, message: Twist) -> None:
        self.messages.append(message)


class FakeGoalHandle:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel_goal_async(self):
        self.cancelled = True
        return SimpleNamespace(add_done_callback=lambda callback: callback(None))


def test_gateway_rejects_an_unreliable_amcl_candidate() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node.max_position_variance = 0.5
    node.max_yaw_variance = 0.35
    node._latest_pose = (1.0, 2.0, 0.3)
    node._latest_pose_received_at = 1.0

    message = PoseWithCovarianceStamped()
    message.header.frame_id = "map"
    message.pose.pose.orientation.w = 1.0
    message.pose.covariance[0] = 0.6
    message.pose.covariance[7] = 0.1
    message.pose.covariance[35] = 0.1

    node._handle_amcl_pose(message)

    assert node._latest_pose == (1.0, 2.0, 0.3)
    assert node._latest_pose_received_at is None


def test_gateway_refreshes_a_verified_manual_pose_from_localization_heartbeat(monkeypatch) -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node._latest_pose = (0.2, -0.3, 0.4)
    node._latest_pose_received_at = 1.0
    node._manual_localization_active = True
    node._localization_status = "manual_initializing"
    node.map_min_x = -1.0
    node.map_max_x = 1.0
    node.map_min_y = -1.0
    node.map_max_y = 1.0
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 10.0)

    node._handle_localization_status(String(data="localized"))

    assert node._localization_status == "localized"
    assert node._latest_pose_received_at == 10.0

    node._handle_localization_status(String(data="manual_required"))

    assert node._manual_localization_active is False


def test_gateway_rejects_an_amcl_pose_outside_the_active_map() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node.max_position_variance = 0.5
    node.max_yaw_variance = 0.35
    node.map_min_x = -1.0
    node.map_max_x = 1.0
    node.map_min_y = -1.0
    node.map_max_y = 1.0
    node._latest_pose = (0.0, 0.0, 0.0)
    node._latest_pose_received_at = 1.0
    node._localization_status = "localized"
    node.get_logger = lambda: SimpleNamespace(warn=lambda _message: None)

    message = PoseWithCovarianceStamped()
    message.header.frame_id = "map"
    message.pose.pose.position.x = 0.0
    message.pose.pose.position.y = -1.01
    message.pose.pose.orientation.w = 1.0
    message.pose.covariance[0] = 0.01
    message.pose.covariance[7] = 0.01
    message.pose.covariance[35] = 0.01

    node._handle_amcl_pose(message)

    assert node._latest_pose is None
    assert node._latest_pose_received_at is None
    assert node._localization_status == "manual_required"


def test_gateway_blocks_navigation_when_current_pose_is_outside_active_map(monkeypatch) -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    command = GatewayCommand("CMD-NAV", "LR-01", "navigate", {}, datetime.now(UTC) + timedelta(minutes=1))
    node._manual_command_deadline = None
    node._bookarm_test_command = None
    node._latest_imu_received_at = 9.0
    node.imu_stale_timeout_seconds = 2.0
    node._localization_status = "localized"
    node._latest_pose = (0.0, -1.01, 0.0)
    node._latest_pose_received_at = 9.0
    node.pose_stale_timeout_seconds = 5.0
    node.map_min_x = -1.0
    node.map_max_x = 1.0
    node.map_min_y = -1.0
    node.map_max_y = 1.0
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 10.0)

    node._start_navigation(command)

    assert events == [
        ("command_rejected", command, {"reason": "robot localization is not stable; navigation is blocked"})
    ]


def test_gateway_blocks_navigation_when_imu_feedback_is_stale(monkeypatch) -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    command = GatewayCommand("CMD-NAV", "LR-01", "navigate", {}, datetime.now(UTC) + timedelta(minutes=1))
    node._manual_command_deadline = None
    node._bookarm_test_command = None
    node._latest_imu_received_at = 7.0
    node.imu_stale_timeout_seconds = 2.0
    node._localization_status = "localized"
    node._latest_pose = (0.0, 0.0, 0.0)
    node._latest_pose_received_at = 9.0
    node.pose_stale_timeout_seconds = 5.0
    node.map_min_x = -1.0
    node.map_max_x = 1.0
    node.map_min_y = -1.0
    node.map_max_y = 1.0
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 10.0)

    node._start_navigation(command)

    assert events == [
        ("command_rejected", command, {"reason": "IMU feedback is unavailable or stale; navigation is blocked"})
    ]


def test_gateway_navigation_gate_passes_with_fresh_imu_feedback(monkeypatch) -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    command = GatewayCommand("CMD-NAV", "LR-01", "navigate", {}, datetime.now(UTC) + timedelta(minutes=1))
    node._manual_command_deadline = None
    node._bookarm_test_command = None
    node._latest_imu_received_at = 9.5
    node.imu_stale_timeout_seconds = 2.0
    node._localization_status = "localized"
    node._latest_pose = (0.0, -1.01, 0.0)
    node._latest_pose_received_at = 9.0
    node.pose_stale_timeout_seconds = 5.0
    node.map_min_x = -1.0
    node.map_max_x = 1.0
    node.map_min_y = -1.0
    node.map_max_y = 1.0
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 10.0)

    node._start_navigation(command)

    assert events == [
        ("command_rejected", command, {"reason": "robot localization is not stable; navigation is blocked"})
    ]


def test_gateway_telemetry_reports_fresh_imu_feedback(monkeypatch) -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node._latest_pose = None
    node._latest_pose_received_at = None
    node.pose_stale_timeout_seconds = 5.0
    node._localization_status = "starting"
    node._localization_scan_mode = "dual"
    node._chassis_feedback_healthy = True
    node._chassis_status_received_at = 9.0
    node.chassis_status_timeout_seconds = 2.5
    node._latest_imu_received_at = None
    node.imu_stale_timeout_seconds = 2.0
    node.active_map_id = "MAP-01"
    node._telemetry_boot_id = "BOOT-TEST"
    node._action_client = SimpleNamespace(server_is_ready=lambda: True)
    telemetry = []
    node._publish_telemetry_frame = telemetry.append
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 10.0)

    node._publish_telemetry()

    assert telemetry[0]["imuReady"] is False

    node._handle_imu_data(Imu())

    assert node._latest_imu_received_at == 10.0

    node._publish_telemetry()

    assert telemetry[1]["imuReady"] is True


def test_gateway_stale_pose_is_not_published_as_live(monkeypatch) -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node._latest_pose = (1.0, 2.0, 0.3)
    node._latest_pose_received_at = 1.0
    node.pose_stale_timeout_seconds = 5.0
    node._localization_status = "localized"
    node._localization_scan_mode = "dual"
    node._chassis_feedback_healthy = True
    node._chassis_status_received_at = 9.0
    node.chassis_status_timeout_seconds = 2.5
    node.active_map_id = "MAP-01"
    node._telemetry_boot_id = "BOOT-TEST"
    node._action_client = SimpleNamespace(server_is_ready=lambda: True)
    telemetry = []
    node._publish_telemetry_frame = telemetry.append
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 10.0)

    node._publish_telemetry()

    assert telemetry[0] | {"hardwareCapabilities": None} == {
        "schemaVersion": "lr-robot-telemetry-v1",
        "bootId": "BOOT-TEST",
        "mapId": "MAP-01",
        "frameId": "map",
        "nav2Ready": True,
        "chassisReady": True,
        "imuReady": False,
        "availableMapIds": [],
        "localizationAvailable": False,
        "localizationStatus": "manual_required",
        "localizationScanMode": "dual",
        "hardwareCapabilities": None,
    }
    assert telemetry[0]["hardwareCapabilities"]["schemaVersion"] == "lr-vehicle-hardware-v1"


def test_gateway_does_not_publish_old_map_pose_while_manual_confirmation_is_required(monkeypatch) -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node._latest_pose = (1.0, 2.0, 0.3)
    node._latest_pose_received_at = 9.0
    node.pose_stale_timeout_seconds = 5.0
    node._localization_status = "manual_required"
    node._localization_scan_mode = "dual"
    node._chassis_feedback_healthy = True
    node._chassis_status_received_at = 9.0
    node.chassis_status_timeout_seconds = 2.5
    node.active_map_id = "MAP-02"
    node._telemetry_boot_id = "BOOT-TEST"
    node._action_client = SimpleNamespace(server_is_ready=lambda: True)
    telemetry = []
    node._publish_telemetry_frame = telemetry.append
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 10.0)

    node._publish_telemetry()

    assert telemetry[0]["localizationAvailable"] is False
    assert not {"x", "y", "yaw"}.intersection(telemetry[0])


def test_gateway_telemetry_includes_non_actuating_arm_feedback_diagnostics(monkeypatch) -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node._latest_pose = None
    node._latest_pose_received_at = None
    node.pose_stale_timeout_seconds = 5.0
    node._localization_status = "manual_required"
    node._localization_scan_mode = "dual"
    node._chassis_feedback_healthy = True
    node._chassis_status_received_at = 9.0
    node.chassis_status_timeout_seconds = 2.5
    node.active_map_id = "MAP-01"
    node._telemetry_boot_id = "BOOT-TEST"
    node._action_client = SimpleNamespace(server_is_ready=lambda: True)
    node._bookarm_health = BookArmHealth(
        available=True,
        arm_ready=False,
        lift_ready=True,
        camera_ready=True,
        rod_ready=True,
        message="机械臂本机通信已打开，但 CAN 反馈未就绪",
        arm_connected=True,
        arm_feedback_healthy=False,
        arm_can_fps=0.0,
        arm_feedback_reason="未收到 CAN 反馈帧",
        arm_enabled=False,
        arm_state="fault",
    )
    telemetry = []
    node._publish_telemetry_frame = telemetry.append
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 10.0)

    node._publish_telemetry()

    assert telemetry[0]["armConnected"] is True
    assert telemetry[0]["armFeedbackHealthy"] is False
    assert telemetry[0]["armEnabled"] is False
    assert telemetry[0]["armState"] == "fault"
    assert telemetry[0]["armCanFps"] == 0.0
    assert telemetry[0]["armFeedbackReason"] == "未收到 CAN 反馈帧"


def test_gateway_telemetry_always_includes_conservative_hardware_capabilities(monkeypatch) -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node._latest_pose = None
    node._latest_pose_received_at = None
    node.pose_stale_timeout_seconds = 5.0
    node._localization_status = "manual_required"
    node._localization_scan_mode = "dual"
    node._chassis_feedback_healthy = True
    node._chassis_status_received_at = 9.0
    node.chassis_status_timeout_seconds = 2.5
    node.active_map_id = "MAP-01"
    node._telemetry_boot_id = "BOOT-TEST"
    node._action_client = SimpleNamespace(server_is_ready=lambda: True)
    node._bookarm_health = BookArmHealth(False, False, False, False, False, "unavailable")
    telemetry = []
    node._publish_telemetry_frame = telemetry.append
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 10.0)

    node._publish_telemetry()

    capabilities = telemetry[0]["hardwareCapabilities"]
    assert capabilities["schemaVersion"] == "lr-vehicle-hardware-v1"
    assert capabilities["liftHomeSensor"] is False
    assert capabilities["liftAbsolutePositionFeedback"] is False
    assert capabilities["liftTargetArrivalFeedback"] is False
    assert capabilities["armHomeFeedback"] is False
    assert capabilities["bookHandoffFeedback"] is False
    assert capabilities["bookWorkflowModes"] == []


def test_gateway_requires_recent_encoder_feedback_for_manual_control(monkeypatch) -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node._chassis_feedback_healthy = True
    node._chassis_status_received_at = 1.0
    node.chassis_status_timeout_seconds = 2.5
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 4.0)

    assert node._chassis_ready() is False


def test_map_switch_rejects_when_the_base_is_not_confirmed_stationary() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    command = GatewayCommand(
        "CMD-MAP", "LR-01", "switch_map", {"mapId": "MAP-02"}, datetime.now(UTC) + timedelta(minutes=1)
    )
    node._registered_maps = {"MAP-02": RegisteredMap("MAP-02", __file__, -1, 1, -1, 1)}
    node.active_map_id = "MAP-01"
    node._manual_command_deadline = None
    node._bookarm_test_command = None
    node._active_command = None
    node._pending_navigation_command = None
    node._base_is_stationary = lambda: False
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))

    node._switch_map(command)

    assert events == [
        ("command_rejected", command, {"reason": "base wheel feedback is not fresh and stationary"})
    ]


def test_gateway_refreshes_newly_synchronized_maps_without_changing_active_map(tmp_path) -> None:
    active_yaml = tmp_path / "active.yaml"
    target_yaml = tmp_path / "target.yaml"
    active_yaml.write_text("image: active.pgm\n", encoding="utf-8")
    target_yaml.write_text("image: target.pgm\n", encoding="utf-8")
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        """version: 1
maps:
  MAP-01:
    yamlPath: %s
    bounds: {minX: -1, maxX: 1, minY: -1, maxY: 1}
  MAP-02:
    yamlPath: %s
    bounds: {minX: -2, maxX: 2, minY: -2, maxY: 2}
""" % (active_yaml, target_yaml),
        encoding="utf-8",
    )
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node.map_registry_file = str(registry)
    node.active_map_id = "MAP-01"
    node._registered_maps = {"MAP-01": RegisteredMap("MAP-01", active_yaml, -1, 1, -1, 1)}
    node.allowed_map_ids = {"MAP-01"}
    node._apply_registered_map = EdgeGatewayNode._apply_registered_map.__get__(node)
    logs = []
    node.get_logger = lambda: SimpleNamespace(info=logs.append, warn=logs.append)

    node._refresh_registered_maps()

    assert node.active_map_id == "MAP-01"
    assert node.allowed_map_ids == {"MAP-01", "MAP-02"}
    assert set(node._registered_maps) == {"MAP-01", "MAP-02"}


def test_map_switch_success_clears_old_pose_and_publishes_localization_reset(monkeypatch, tmp_path) -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    command = GatewayCommand(
        "CMD-MAP", "LR-01", "switch_map", {"mapId": "MAP-02"}, datetime.now(UTC) + timedelta(minutes=1)
    )
    target = RegisteredMap("MAP-02", tmp_path / "map.yaml", -2, 3, -4, 5)
    node._pending_map_switch_command = command
    node._registered_maps = {"MAP-02": target}
    node._latest_pose = (1.0, 2.0, 0.0)
    node._latest_pose_received_at = 1.0
    node._localization_status = "localized"
    node.map_state_file = str(tmp_path / "active-map.json")
    node._apply_registered_map = EdgeGatewayNode._apply_registered_map.__get__(node)
    node._map_switch_publisher = SimpleNamespace(publish=lambda message: published.append(message.data))
    node.get_logger = lambda: SimpleNamespace(warn=lambda _message: None)
    events = []
    published = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    future = SimpleNamespace(result=lambda: SimpleNamespace(result=0))
    monkeypatch.setattr(
        "robot_decision.edge_gateway_node.LoadMap",
        SimpleNamespace(Response=SimpleNamespace(RESULT_SUCCESS=0)),
    )

    node._handle_map_switch_result(command, "MAP-01", target, future)

    assert node.active_map_id == "MAP-02"
    assert node._latest_pose is None
    assert node._localization_status == "manual_required"
    assert published == ["MAP-02"]
    assert events == [("map_switch_succeeded", command, {"mapId": "MAP-02", "localizationRequired": True})]


def test_cancelling_cruise_cancels_the_existing_nav2_goal() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    command = GatewayCommand("CMD-001", "LR-01", "cruise", {}, datetime.now(UTC) + timedelta(minutes=1))
    goal_handle = FakeGoalHandle()
    events = []
    node._active_goal_handle = goal_handle
    node._active_command = command
    node._pending_navigation_command = None
    node._active_cruise_route = object()
    node._cruise_waypoint_index = 1
    node._cruise_loop_index = 0
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))

    assert node._cancel_active_navigation("manual control takeover") is True
    assert goal_handle.cancelled is True
    assert node._active_goal_handle is None
    assert node._active_command is None
    assert events == [("cruise_cancelled", command, {"reason": "manual control takeover"})]


def test_manual_drive_tick_zeroes_velocity_at_the_jog_deadline(monkeypatch) -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    publisher = FakePublisher()
    node._manual_command_deadline = 5.0
    node._manual_velocity_publisher = publisher
    node._manual_target_linear = 0.2
    node._manual_target_angular = 0.5
    node._manual_output_linear = 0.2
    node._manual_output_angular = 0.5
    node._chassis_ready = lambda: True
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 6.0)

    node._manual_drive_tick()

    assert publisher.messages[-1].linear.x == 0.0
    assert publisher.messages[-1].angular.z == 0.0
    assert node._manual_command_deadline is None
    assert node._manual_target_linear == 0.0
    assert node._manual_output_linear == 0.0


def test_manual_drive_tick_zeroes_velocity_when_chassis_feedback_is_stale() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    publisher = FakePublisher()
    node._manual_command_deadline = 5.0
    node._manual_velocity_publisher = publisher
    node._manual_target_linear = 0.2
    node._manual_target_angular = 0.0
    node._manual_output_linear = 0.2
    node._manual_output_angular = 0.0
    node._chassis_ready = lambda: False

    node._manual_drive_tick()

    assert publisher.messages[-1].linear.x == 0.0
    assert publisher.messages[-1].angular.z == 0.0
    assert node._manual_command_deadline is None


def test_manual_jog_runs_lease_free_and_cancels_navigation_on_burst_start() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    publisher = FakePublisher()
    events = []
    takeovers = []
    node._manual_command_deadline = None
    node._manual_velocity_publisher = publisher
    node._manual_target_linear = 0.0
    node._manual_target_angular = 0.0
    node._manual_output_linear = 0.0
    node._manual_output_angular = 0.0
    node._chassis_ready = lambda: True
    node.manual_command_timeout_seconds = 1.0
    node._cancel_active_navigation = lambda reason: takeovers.append(reason) or True
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    command = GatewayCommand(
        "CMD-JOG",
        "LR-01",
        "manual_jog",
        {"linear": 0.2, "angular": 0.5, "durationMs": 500},
        datetime.now(UTC) + timedelta(minutes=1),
    )

    node._manual_jog(command)

    assert takeovers == ["manual control takeover"]
    # The jog frame only sets the target; the 20 Hz tick streams the velocity.
    assert publisher.messages == []
    assert node._manual_target_linear == 0.2
    assert node._manual_target_angular == 0.5
    assert node._manual_command_deadline is not None
    assert node._manual_control_active() is True
    assert events == [("manual_jog_accepted", command, {"durationMs": 500})]

    node._manual_drive_tick()

    # Output ramps toward the target (0.5 m/s², 1.5 rad/s² at 20 Hz).
    assert publisher.messages[-1].linear.x == pytest.approx(0.025)
    assert publisher.messages[-1].angular.z == pytest.approx(0.075)


def test_manual_jog_rejects_speeds_above_the_fixed_safety_limits() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    publisher = FakePublisher()
    events = []
    node._manual_command_deadline = None
    node._manual_velocity_publisher = publisher
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    command = GatewayCommand(
        "CMD-JOG",
        "LR-01",
        "manual_jog",
        {"linear": 0.31, "angular": 0.0, "durationMs": 500},
        datetime.now(UTC) + timedelta(minutes=1),
    )

    node._manual_jog(command)

    assert publisher.messages == []
    assert node._manual_command_deadline is None
    assert events == [("command_rejected", command, {"reason": "manual jog exceeds active safety limits"})]


def test_navigation_goal_uses_latest_available_transform() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    pose = node._pose_from_goal(SimpleNamespace(x=1.0, y=-0.5, yaw=0.25))

    assert pose.header.frame_id == "map"
    assert pose.header.stamp.sec == 0
    assert pose.header.stamp.nanosec == 0


def test_gateway_requires_client_certificate_for_mqtt_tls() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node.allowed_map_ids = {"MAP-01"}
    node.active_map_id = "MAP-01"
    node.map_min_x = -1.0
    node.map_max_x = 1.0
    node.map_min_y = -1.0
    node.map_max_y = 1.0
    node.max_abs_yaw = 6.284
    node.telemetry_period_seconds = 0.5
    node.pose_stale_timeout_seconds = 5.0
    node.max_position_variance = 0.5
    node.max_yaw_variance = 0.35
    node.manual_command_timeout_seconds = 1.0
    node.chassis_status_timeout_seconds = 2.5
    node.wheel_speed_timeout_seconds = 1.0
    node.base_stationary_speed_threshold_mps = 0.01
    node.mqtt_tls_enabled = True
    node.mqtt_ca_file = "/tmp/ca.crt"
    node.mqtt_client_cert_file = ""
    node.mqtt_client_key_file = ""
    node.mqtt_protocol_version = "3.1.1"
    node.get_logger = lambda: SimpleNamespace(warn=lambda _message: None)

    with pytest.raises(RuntimeError, match="client certificate"):
        node._validate_configuration()


def test_expired_command_is_rejected_before_it_can_start_a_robot_action() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    command = GatewayCommand(
        "CMD-EXPIRED",
        "LR-01",
        "mechanism_home",
        {"taskId": "TSK-RECOVERY"},
        datetime.now(UTC) - timedelta(seconds=1),
    )
    node._pending_commands = queue.Queue()
    node._pending_commands.put(command)
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    node._home_mechanisms = lambda _command: pytest.fail("expired mechanism home must not execute")

    node._process_command_queue()

    assert events == [
        ("command_rejected", command, {"reason": "command expired before execution"})
    ]


def test_mechanism_home_publishes_zero_base_velocity_before_stationary_check() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    command = GatewayCommand(
        "CMD-HOME",
        "LR-01",
        "mechanism_home",
        {
            "taskId": "TSK-RECOVERY",
            "acknowledgeSafety": True,
            "acknowledgeBookSecured": True,
            "baseMustRemainStationary": True,
        },
        datetime.now(UTC) + timedelta(minutes=1),
    )
    node._bookarm_test_command = None
    node._active_command = None
    node._pending_navigation_command = None
    node._manual_command_deadline = None
    zero_velocity_requests = []
    events = []
    node._publish_zero_manual_velocity = lambda: zero_velocity_requests.append(True)
    node._base_is_stationary = lambda: False
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))

    node._home_mechanisms(command)

    assert zero_velocity_requests == [True]
    assert events == [
        (
            "mechanism_home_failed",
            command,
            {"taskId": "TSK-RECOVERY", "reason": "base wheel feedback is not fresh and stationary"},
        )
    ]


def test_bookarm_maintenance_test_starts_in_manual_step_mode() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node._manual_command_deadline = None
    node._bookarm_test_command = None
    node._active_command = None
    node._pending_navigation_command = None
    node._bookarm_workflow_id = None
    node._bookarm_test_last_state = None
    health = SimpleNamespace(
        lift_ready=True,
        camera_ready=True,
        rod_ready=True,
        arm_ready=True,
        message="ready",
    )
    calls = []
    node._bookarm_client = SimpleNamespace(
        health=lambda: health,
        prepare_arm=lambda: health,
        start_workflow=lambda mode, auto: calls.append((mode, auto))
        or {
            "id": "workflow-001",
            "active": True,
            "waiting": False,
            "step_index": 0,
            "step_count": 16,
            "step_name": "Capture RGB-D point cloud",
            "message": "ready",
        },
    )
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    command = GatewayCommand(
        "CMD-MNT",
        "LR-01",
        "bookarm_test_start",
        {
            "sessionId": "MNT-001",
            "workflowMode": "takeout",
            "stepMode": "manual",
            "acknowledgeSafety": True,
        },
        datetime.now(UTC) + timedelta(minutes=1),
    )

    node._start_bookarm_test(command)

    assert calls == [("takeout", False)]
    assert node._bookarm_test_session.session_id == "MNT-001"
    assert events[0][0] == "bookarm_test_started"
    assert events[1][0] == "bookarm_test_workflow"
    assert events[1][2]["stepIndex"] == 0


def test_lift_command_reports_driver_acknowledgement_not_physical_completion() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node._bookarm_test_command = None
    node._active_command = None
    node._pending_navigation_command = None
    node._manual_command_deadline = None
    node._hardware = SimpleNamespace(
        move_lift_relative=lambda _distance: {
            "accepted": True,
            "physicalCompletionVerified": False,
            "reason": "lift driver acknowledged the relative command; physical arrival is unverified",
            "driverAcknowledged": True,
            "driverResult": {"completion": "driver_acknowledged"},
        }
    )
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    command = GatewayCommand(
        "CMD-LIFT",
        "LR-01",
        "lift_move",
        {"distanceCm": 1.0, "acknowledgeSafety": True},
        datetime.now(UTC) + timedelta(minutes=1),
    )

    node._move_lift(command)

    assert events == [
        (
            "lift_move_driver_acknowledged",
            command,
            {
                "distanceCm": 1.0,
                "result": {
                    "accepted": True,
                    "physicalCompletionVerified": False,
                    "reason": "lift driver acknowledged the relative command; physical arrival is unverified",
                    "driverAcknowledged": True,
                    "driverResult": {"completion": "driver_acknowledged"},
                },
                "physicalCompletionVerified": False,
                "warning": "升降台驱动器已接收命令，但没有位置反馈，未确认物理到位",
            },
        )
    ]


def test_emergency_stop_requests_local_hardware_stop_chain_before_cancelling_navigation() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    command = GatewayCommand(
        "CMD-ESTOP", "LR-01", "emergency_stop", {}, datetime.now(UTC) + timedelta(minutes=1)
    )
    stops: list[bool] = []
    events = []
    node._manual_command_deadline = None
    node._manual_velocity_publisher = FakePublisher()
    node._hardware = SimpleNamespace(emergency_stop=lambda: stops.append(True) or {"accepted": True})
    node._bookarm_test_command = None
    node._cancel_active_navigation = lambda _reason: False
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))

    node._cancel_navigation(command)

    assert stops == [True]
    assert events[0][0] == "emergency_stop_requested"
    assert events[1] == ("command_completed", command, {"result": "no active Nav2 goal"})


def test_bookarm_maintenance_cancel_requests_local_hardware_stop_chain() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    active_command = GatewayCommand(
        "CMD-TEST", "LR-01", "bookarm_test_start", {}, datetime.now(UTC) + timedelta(minutes=1)
    )
    command = GatewayCommand(
        "CMD-CANCEL", "LR-01", "bookarm_test_cancel", {"sessionId": "SESSION-01"}, datetime.now(UTC) + timedelta(minutes=1)
    )
    node._bookarm_test_session = SimpleNamespace(session_id="SESSION-01")
    node._bookarm_test_command = active_command
    node._bookarm_workflow_id = "workflow-001"
    calls: list[str] = []
    node._bookarm_client = SimpleNamespace(cancel_workflow=lambda workflow_id: calls.append(workflow_id))
    node._hardware = SimpleNamespace(safe_stop=lambda: calls.append("safe-stop") or {"accepted": True})
    node._clear_bookarm_test = lambda: calls.append("cleared")
    node._publish_event = lambda *_args, **_kwargs: None

    node._cancel_bookarm_test(command, "operator cancelled maintenance test")

    assert calls == ["workflow-001", "safe-stop", "cleared"]


def test_bookarm_maintenance_state_forwards_bounded_recognition_result() -> None:
    from robot_decision.edge_gateway_protocol import BookArmTestSession

    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    command = GatewayCommand(
        "CMD-MNT",
        "LR-01",
        "bookarm_test_start",
        {},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    node._bookarm_test_session = BookArmTestSession("MNT-001", "takeout")
    node._bookarm_test_command = command
    node._bookarm_workflow_id = "workflow-001"
    node._bookarm_test_last_state = None
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))

    node._publish_bookarm_test_state(
        command,
        {
            "active": True,
            "waiting": False,
            "step_index": 2,
            "step_count": 16,
            "step_name": "Solve target point",
            "message": "book detected",
            "recognition_result": {
                "confidence": 0.91,
                "good_count": 24,
                "inlier_count": 17,
                "pixel_uv": [640, 360],
                "target_point_m": [0.2, 0.1, 0.3],
            },
        },
    )

    assert events[0][0] == "bookarm_test_workflow"
    assert events[0][2]["recognitionResult"]["confidence"] == 0.91


def test_bookarm_maintenance_state_forwards_takeout_debug_preflight() -> None:
    from robot_decision.edge_gateway_protocol import BookArmTestSession

    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    command = GatewayCommand(
        "CMD-MNT",
        "LR-01",
        "bookarm_test_start",
        {},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    node._bookarm_test_session = BookArmTestSession("MNT-001", "takeout")
    node._bookarm_test_command = command
    node._bookarm_workflow_id = "workflow-001"
    node._bookarm_test_last_state = None
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))

    node._publish_bookarm_test_state(
        command,
        {
            "active": True,
            "waiting": False,
            "step_index": 0,
            "step_count": 16,
            "step_name": "Capture RGB-D point cloud",
            "message": "startup preparation required",
            "preflight_required": True,
            "preflight_complete": False,
            "preflight_state": "required",
            "preflight_name": "MoveJ to book debug pose",
        },
    )

    details = events[0][2]
    assert details["preflightRequired"] is True
    assert details["preflightComplete"] is False
    assert details["preflightState"] == "required"
    assert details["preflightName"] == "MoveJ to book debug pose"


def _stub_auto_localize_gateway(node: EdgeGatewayNode) -> None:
    node._manual_command_deadline = None
    node._bookarm_test_command = None
    node._active_command = None
    node._pending_navigation_command = None
    node._pending_auto_localize_command = None
    node.active_map_id = "MAP-01"
    node._latest_pose = None
    node._manual_localization_active = False


class _ImmediateFuture:
    def __init__(self, response) -> None:
        self._response = response

    def add_done_callback(self, callback) -> None:
        callback(self)

    def result(self):
        return self._response


def test_auto_localize_command_calls_the_service_and_publishes_the_matched_pose() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    _stub_auto_localize_gateway(node)
    command = GatewayCommand(
        "CMD-AUTOLOC",
        "LR-01",
        "auto_localize",
        {},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    response = SimpleNamespace(
        success=True,
        message=(
            "自动定位成功：位姿 x=1.20 m, y=-0.30 m, yaw=28.6 deg, 命中率 0.87，耗时 1.2 s"
            "|pose=1.200000,-0.300000,0.500000"
        ),
    )
    node._auto_localize_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True,
        call_async=lambda _request: _ImmediateFuture(response),
    )

    node._auto_localize(command)

    assert events == [
        (
            "localization_initializing",
            command,
            {
                "mapId": "MAP-01",
                "message": "自动定位成功：位姿 x=1.20 m, y=-0.30 m, yaw=28.6 deg, 命中率 0.87，耗时 1.2 s",
                "x": 1.2,
                "y": -0.3,
                "yaw": 0.5,
            },
        )
    ]
    assert node._latest_pose == (1.2, -0.3, 0.5)
    assert node._manual_localization_active is True
    assert node._pending_auto_localize_command is None


def test_auto_localize_is_rejected_while_another_activity_is_active() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    _stub_auto_localize_gateway(node)
    command = GatewayCommand(
        "CMD-AUTOLOC",
        "LR-01",
        "auto_localize",
        {},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    node._active_command = command  # any active navigation blocks re-localization
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    node._auto_localize_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: pytest.fail("must not consult the service")
    )

    node._auto_localize(command)

    assert events == [
        ("command_rejected", command, {"reason": "another robot activity is still active"})
    ]
    assert node._pending_auto_localize_command is None


def test_auto_localize_rejects_a_request_for_another_map() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    _stub_auto_localize_gateway(node)
    command = GatewayCommand(
        "CMD-AUTOLOC-MAP",
        "LR-01",
        "auto_localize",
        {"mapId": "MAP-02"},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    node._auto_localize_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: pytest.fail("must not consult the service")
    )

    node._auto_localize(command)

    assert events == [
        ("command_rejected", command, {"reason": "auto localize mapId is not the active vehicle map"})
    ]


def test_auto_localize_forwards_the_failure_reason_from_the_service() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    _stub_auto_localize_gateway(node)
    command = GatewayCommand(
        "CMD-AUTOLOC-FAIL",
        "LR-01",
        "auto_localize",
        {},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    response = SimpleNamespace(
        success=False,
        message="全图匹配不可信（命中率 0.12），已撒粒子等待移动收敛",
    )
    node._auto_localize_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True,
        call_async=lambda _request: _ImmediateFuture(response),
    )

    node._auto_localize(command)

    assert events == [
        (
            "command_rejected",
            command,
            {"reason": "全图匹配不可信（命中率 0.12），已撒粒子等待移动收敛"},
        )
    ]
    assert node._latest_pose is None
    assert node._pending_auto_localize_command is None


def _fine_tune_gateway_node() -> tuple[EdgeGatewayNode, list]:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node._manual_command_deadline = None
    node._bookarm_test_command = None
    node._active_command = None
    node._pending_navigation_command = None
    node._pending_fine_tune_command = None
    node.book_shelf_fine_tune_enabled = True
    node._localization_is_reliable = lambda: True
    node._active_map_bounds = lambda: (-1.0, 1.0, -1.0, 1.0)
    node.active_map_id = "MAP-01"
    node.max_abs_yaw = 6.284
    node._latest_pose = None
    node._latest_pose_received_at = None
    node.pose_stale_timeout_seconds = 5.0
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    return node, events


def test_fine_tune_is_rejected_while_another_activity_is_active() -> None:
    node, events = _fine_tune_gateway_node()
    node._manual_command_deadline = 1.0
    command = GatewayCommand(
        "CMD-FT", "LR-01", "fine_tune", {"x": 0.5, "y": 0.5}, datetime.now(UTC) + timedelta(minutes=1)
    )

    node._start_fine_tune(command)

    assert events == [("command_rejected", command, {"reason": "another robot activity is still active"})]
    assert node._pending_fine_tune_command is None


def test_fine_tune_is_rejected_when_localization_is_unreliable() -> None:
    node, events = _fine_tune_gateway_node()
    node._localization_is_reliable = lambda: False
    command = GatewayCommand(
        "CMD-FT", "LR-01", "fine_tune", {"x": 0.5, "y": 0.5}, datetime.now(UTC) + timedelta(minutes=1)
    )

    node._start_fine_tune(command)

    assert events == [
        ("command_rejected", command, {"reason": "robot localization is not stable; fine tune is blocked"})
    ]
    assert node._pending_fine_tune_command is None


def test_fine_tune_rejects_a_target_on_another_map() -> None:
    node, events = _fine_tune_gateway_node()
    node._book_shelf_fine_tune_parameter_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: pytest.fail("must not consult the service")
    )
    command = GatewayCommand(
        "CMD-FT",
        "LR-01",
        "fine_tune",
        {"mapId": "MAP-02", "x": 0.5, "y": 0.5},
        datetime.now(UTC) + timedelta(minutes=1),
    )

    node._start_fine_tune(command)

    assert events == [("command_rejected", command, {"reason": "fine tune mapId is not the active vehicle map"})]
    assert node._pending_fine_tune_command is None


def test_fine_tune_rejects_an_invalid_payload_before_touching_the_service() -> None:
    node, events = _fine_tune_gateway_node()
    node._book_shelf_fine_tune_parameter_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: pytest.fail("must not consult the service")
    )
    command = GatewayCommand(
        "CMD-FT", "LR-01", "fine_tune", {"y": 0.5}, datetime.now(UTC) + timedelta(minutes=1)
    )

    node._start_fine_tune(command)

    assert events == [("command_rejected", command, {"reason": "精调目标缺少有效的 x 坐标"})]
    assert node._pending_fine_tune_command is None


def test_fine_tune_dispatches_target_and_completes_with_actual_pose(monkeypatch) -> None:
    node, events = _fine_tune_gateway_node()
    node._latest_pose = (0.49, 0.51, 0.01)
    node._latest_pose_received_at = 9.0
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 10.0)
    parameter_requests = []
    node._book_shelf_fine_tune_parameter_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True,
        call_async=lambda request: parameter_requests.append(request)
        or _ImmediateFuture(SimpleNamespace(results=[SimpleNamespace(successful=True, reason="")])),
    )
    start_requests = []
    node._book_shelf_fine_tune_start_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True,
        call_async=lambda request: start_requests.append(request)
        or _ImmediateFuture(
            SimpleNamespace(success=True, message="book shelf fine tune complete: x=0.010 y=0.010 yaw=0.010")
        ),
    )
    command = GatewayCommand(
        "CMD-FT",
        "LR-01",
        "fine_tune",
        {"x": 0.5, "y": 0.5, "yaw": 0.0},
        datetime.now(UTC) + timedelta(minutes=1),
    )

    node._start_fine_tune(command)

    assert len(parameter_requests) == 1
    parameters = {parameter.name: parameter.value for parameter in parameter_requests[0].parameters}
    assert parameters["target_task_id"].string_value == "CMD-FT"
    assert parameters["target_x"].double_value == 0.5
    assert parameters["target_y"].double_value == 0.5
    assert parameters["target_yaw"].double_value == 0.0
    assert len(start_requests) == 1
    assert node._pending_fine_tune_command is None
    assert events == [
        (
            "fine_tune_dispatching",
            command,
            {"mapId": "MAP-01", "targetX": 0.5, "targetY": 0.5, "targetYaw": 0.0},
        ),
        (
            "command_completed",
            command,
            {
                "mapId": "MAP-01",
                "targetX": 0.5,
                "targetY": 0.5,
                "targetYaw": 0.0,
                "message": "book shelf fine tune complete: x=0.010 y=0.010 yaw=0.010",
                "actualX": 0.49,
                "actualY": 0.51,
                "actualYaw": 0.01,
            },
        ),
    ]


def test_fine_tune_failure_forwards_the_service_reason() -> None:
    node, events = _fine_tune_gateway_node()
    node._book_shelf_fine_tune_parameter_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True,
        call_async=lambda _request: _ImmediateFuture(
            SimpleNamespace(results=[SimpleNamespace(successful=True, reason="")])
        ),
    )
    node._book_shelf_fine_tune_start_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True,
        call_async=lambda _request: _ImmediateFuture(
            SimpleNamespace(success=False, message="精调超时：90 秒内未到达目标")
        ),
    )
    command = GatewayCommand(
        "CMD-FT", "LR-01", "fine_tune", {"x": 0.5, "y": 0.5}, datetime.now(UTC) + timedelta(minutes=1)
    )

    node._start_fine_tune(command)

    assert node._pending_fine_tune_command is None
    assert events[-1] == ("command_rejected", command, {"reason": "精调超时：90 秒内未到达目标"})


def test_cancel_during_fine_tune_aborts_the_local_run() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    active_command = GatewayCommand(
        "CMD-FT", "LR-01", "fine_tune", {"x": 0.5, "y": 0.5}, datetime.now(UTC) + timedelta(minutes=1)
    )
    cancel_command = GatewayCommand(
        "CMD-CANCEL", "LR-01", "cancel", {}, datetime.now(UTC) + timedelta(minutes=1)
    )
    node._manual_command_deadline = None
    node._manual_velocity_publisher = FakePublisher()
    node._bookarm_test_command = None
    node._pending_fine_tune_command = active_command
    node._book_shelf_fine_tune_timer = None
    aborts = []
    node._book_shelf_fine_tune_abort_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True,
        call_async=lambda request: aborts.append(request),
    )
    stops = []
    node._hardware = SimpleNamespace(safe_stop=lambda: stops.append(True) or {"accepted": True})
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))

    node._cancel_navigation(cancel_command)

    assert len(aborts) == 1
    assert stops == [True]
    assert node._pending_fine_tune_command is None
    assert events == [
        ("fine_tune_cancelled", active_command, {"reason": "operator cancelled fine tune"}),
        ("command_completed", cancel_command, {"result": "fine tune cancellation requested"}),
    ]


def test_fine_tune_in_progress_rejects_new_commands_at_the_queue() -> None:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    active_command = GatewayCommand(
        "CMD-FT", "LR-01", "fine_tune", {"x": 0.5, "y": 0.5}, datetime.now(UTC) + timedelta(minutes=1)
    )
    queued_command = GatewayCommand(
        "CMD-NAV", "LR-01", "navigate", {}, datetime.now(UTC) + timedelta(minutes=1)
    )
    node._pending_commands = queue.Queue()
    node._pending_commands.put(queued_command)
    node._pending_map_switch_command = None
    node._pending_auto_localize_command = None
    node._pending_fine_tune_command = active_command
    events = []
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    node._start_navigation = lambda _command: pytest.fail("navigation must not start during fine tune")

    node._process_command_queue()

    assert events == [("command_rejected", queued_command, {"reason": "fine tune is still in progress"})]


class _FakeMqtt:
    def __init__(self, connected: bool = True) -> None:
        self._connected = connected
        self.reconnects = 0

    def is_connected(self) -> bool:
        return self._connected

    def reconnect(self) -> None:
        self.reconnects += 1


def _watchdog_node(command_age_s: float, reconnect_age_s: float, connected: bool = True):
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node._mqtt = _FakeMqtt(connected)
    node._last_command_received_at = 1_000.0 - command_age_s
    node._last_mqtt_reconnect_at = 1_000.0 - reconnect_age_s if reconnect_age_s >= 0 else 0.0
    logs = []
    node.get_logger = lambda: SimpleNamespace(
        warn=lambda message: logs.append(("warn", message)),
        error=lambda message: logs.append(("error", message)),
        info=lambda message: logs.append(("info", message)),
    )
    return node


def test_mqtt_watchdog_reconnects_when_command_intake_is_dead(monkeypatch) -> None:
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 1_000.0)
    node = _watchdog_node(command_age_s=120.0, reconnect_age_s=120.0)

    node._mqtt_subscription_watchdog()

    assert node._mqtt.reconnects == 1


def test_mqtt_watchdog_stays_quiet_when_commands_are_flowing(monkeypatch) -> None:
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 1_000.0)
    node = _watchdog_node(command_age_s=5.0, reconnect_age_s=120.0)

    node._mqtt_subscription_watchdog()

    assert node._mqtt.reconnects == 0


def test_mqtt_watchdog_debounces_reconnects(monkeypatch) -> None:
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 1_000.0)
    node = _watchdog_node(command_age_s=120.0, reconnect_age_s=10.0)

    node._mqtt_subscription_watchdog()

    assert node._mqtt.reconnects == 0


def test_mqtt_watchdog_skips_when_broker_link_is_down(monkeypatch) -> None:
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 1_000.0)
    node = _watchdog_node(command_age_s=120.0, reconnect_age_s=120.0, connected=False)

    node._mqtt_subscription_watchdog()

    assert node._mqtt.reconnects == 0


def test_gateway_refreshes_an_auto_restored_pose_from_localization_heartbeat(monkeypatch) -> None:
    """A startup saved-pose restore has no operator command; the localized
    heartbeat alone must keep its pose alive while the robot is parked."""
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node._latest_pose = (0.2, -0.3, 0.4)
    node._latest_pose_received_at = 1.0
    node._manual_localization_active = False
    node._localization_status = "restoring_saved_pose"
    node.map_min_x = -1.0
    node.map_max_x = 1.0
    node.map_min_y = -1.0
    node.map_max_y = 1.0
    monkeypatch.setattr("robot_decision.edge_gateway_node.time.monotonic", lambda: 10.0)

    node._handle_localization_status(String(data="localized"))

    assert node._localization_status == "localized"
    assert node._latest_pose_received_at == 10.0


def _stub_cancel_test_node(node: EdgeGatewayNode, events: list) -> EdgeGatewayNode:
    node._pending_fine_tune_command = None
    node._bookarm_test_command = None
    node._request_hardware_safe_stop = lambda emergency=False: None
    node._manual_command_deadline = None
    node._manual_velocity_publisher = FakePublisher()
    node._publish_event = lambda event, command=None, details=None: events.append((event, command, details))
    return node


def test_targeted_cancel_cancels_only_the_matching_navigation_command() -> None:
    node = _stub_cancel_test_node(EdgeGatewayNode.__new__(EdgeGatewayNode), events := [])
    active = GatewayCommand("CMD-NAV-2", "LR-01", "navigate", {}, datetime.now(UTC) + timedelta(minutes=1))
    goal_handle = FakeGoalHandle()
    node._active_goal_handle = goal_handle
    node._active_command = active
    node._pending_navigation_command = None
    node._active_cruise_route = None
    node._cruise_waypoint_index = 0
    node._cruise_loop_index = 0
    cancel = GatewayCommand(
        "CMD-CXL-1", "LR-01", "cancel", {"taskId": "TSK-NAV-2"}, datetime.now(UTC) + timedelta(minutes=1)
    )

    node._cancel_navigation(cancel)

    assert goal_handle.cancelled is True
    assert ("navigation_cancelled", active, {"reason": "operator cancelled navigation"}) in events
    assert ("navigation_cancelling", cancel, None) in events


def test_targeted_cancel_with_unknown_id_leaves_active_goal_untouched() -> None:
    node = _stub_cancel_test_node(EdgeGatewayNode.__new__(EdgeGatewayNode), events := [])
    active = GatewayCommand("CMD-NAV-2", "LR-01", "navigate", {}, datetime.now(UTC) + timedelta(minutes=1))
    goal_handle = FakeGoalHandle()
    node._active_goal_handle = goal_handle
    node._active_command = active
    node._pending_navigation_command = None
    cancel = GatewayCommand(
        "CMD-CXL-1", "LR-01", "cancel", {"taskId": "TSK-OLD-1"}, datetime.now(UTC) + timedelta(minutes=1)
    )

    node._cancel_navigation(cancel)

    assert goal_handle.cancelled is False
    assert node._active_command is active
    assert node._active_goal_handle is goal_handle
    assert events == [
        ("command_completed", cancel, {"result": "no matching active command", "targetCommandId": "CMD-OLD-1"})
    ]


def test_bare_cancel_keeps_legacy_broad_behavior() -> None:
    node = _stub_cancel_test_node(EdgeGatewayNode.__new__(EdgeGatewayNode), events := [])
    active = GatewayCommand("CMD-NAV-2", "LR-01", "navigate", {}, datetime.now(UTC) + timedelta(minutes=1))
    goal_handle = FakeGoalHandle()
    node._active_goal_handle = goal_handle
    node._active_command = active
    node._pending_navigation_command = None
    node._active_cruise_route = None
    node._cruise_waypoint_index = 0
    node._cruise_loop_index = 0
    cancel = GatewayCommand("CMD-CXL-1", "LR-01", "cancel", {}, datetime.now(UTC) + timedelta(minutes=1))

    node._cancel_navigation(cancel)

    assert goal_handle.cancelled is True
    assert ("navigation_cancelling", cancel, None) in events
