#!/usr/bin/env python3

"""Bridge trusted MQTT fleet commands into safe local navigation and teleoperation.

This node is deliberately a robot-local boundary. It validates command identity,
expiry, map identity, and pose bounds before it creates any ROS action or velocity.
Manual velocity remains local, bounded, and short-lived; the physical emergency
stop remains independent of this software path.
"""

from __future__ import annotations

import json
import math
import os
import queue
import ssl
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import paho.mqtt.client as mqtt
import rclpy
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, Twist
from nav2_msgs.action import NavigateToPose
from nav2_msgs.srv import LoadMap
from rclpy.action import ActionClient
from rcl_interfaces.msg import Parameter as ParameterMsg
from rcl_interfaces.msg import ParameterType, ParameterValue
from rcl_interfaces.srv import SetParameters
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import Imu
from std_msgs.msg import Float32MultiArray, String
from std_srvs.srv import Trigger

from robot_decision.bookarm_client import BookArmApiError, BookArmClient, BookArmHealth
from robot_decision.bookarm_sdk_client import BookArmSdkClient
from robot_decision.bookarm_takeout_client import BookArmTakeoutClient
from robot_decision.hardware_adapter import (
    DEFAULT_HARDWARE_PROFILE,
    HardwareAdapter,
    conservative_hardware_capabilities,
)
from robot_decision.edge_gateway_protocol import (
    BookArmTestSession,
    FineTuneTarget,
    GatewayCommand,
    MapSwitch,
    CruiseRoute,
    LiftMove,
    MechanismHome,
    NavigationGoal,
    parse_gateway_command,
    parse_auto_localize,
    parse_bookarm_test_session_id,
    parse_bookarm_test_start,
    parse_cruise_route,
    parse_fine_tune,
    parse_initial_pose,
    parse_lift_move,
    parse_mechanism_home,
    parse_manual_jog,
    parse_map_switch,
    parse_navigation_goal,
)
from robot_decision.map_registry import (
    RegisteredMap,
    active_map_id,
    cancel_pending_switch,
    commit_map_switch,
    load_map_registry,
    stage_pending_switch,
)
from robot_decision.pose_utils import make_initial_pose


_TELEMETRY_SCHEMA_VERSION = "lr-robot-telemetry-v1"
# Fixed manual-jog envelope enforced by the gateway on every command; the
# control station applies the same caps before sending.
_MANUAL_JOG_MAX_LINEAR_SPEED = 0.3
_MANUAL_JOG_MAX_ANGULAR_SPEED = 0.8
_MANUAL_JOG_MAX_DURATION_MS = 750
# Continuous manual drive mirrors the mapping agent: while a jog lease is
# live a 20 Hz tick ramps the output toward the latest target (0.5 m/s² and
# 1.5 rad/s²); watchdog expiry or stale chassis feedback cuts to zero
# immediately without ramping, preserving the hard-stop semantics.
_MANUAL_DRIVE_STEP_LINEAR_MPS = 0.025
_MANUAL_DRIVE_STEP_ANGULAR_RPS = 0.075


def _ramp_toward(current: float, target: float, max_step: float) -> float:
    delta = target - current
    if abs(delta) <= max_step:
        return target
    return current + math.copysign(max_step, delta)


def _auto_localize_pose_from_message(message: str) -> tuple[float, float, float] | None:
    """Extract the machine-readable pose tail appended by the localization node.

    The Trigger service only carries a text message, so a successful response
    ends with ``|pose=<x>,<y>,<yaw>``.  Anything malformed simply yields no
    pose; the event is still published without one.
    """

    marker = "|pose="
    if marker not in message:
        return None
    try:
        values = tuple(float(part) for part in message.rsplit(marker, 1)[1].split(","))
    except ValueError:
        return None
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        return None
    return values[0], values[1], values[2]


class EdgeGatewayNode(Node):
    """Receive central commands from MQTT and execute only permitted Nav2 actions."""

    def __init__(self) -> None:
        super().__init__("edge_gateway_node")
        self._declare_parameters()
        self.robot_id = self._required_string_parameter("robot_id")
        self.mqtt_host = self._required_string_parameter("mqtt_host")
        self.mqtt_port = self._positive_int_parameter("mqtt_port")
        self.mqtt_protocol_version = self._required_string_parameter("mqtt_protocol_version")
        self.mqtt_tls_enabled = bool(self.get_parameter("mqtt_tls_enabled").value)
        self.mqtt_ca_file = str(self.get_parameter("mqtt_ca_file").value).strip()
        self.mqtt_client_cert_file = str(self.get_parameter("mqtt_client_cert_file").value).strip()
        self.mqtt_client_key_file = str(self.get_parameter("mqtt_client_key_file").value).strip()
        self.mqtt_username = str(self.get_parameter("mqtt_username").value)
        self.mqtt_password = str(self.get_parameter("mqtt_password").value)
        self.allowed_map_ids = {
            str(value).strip() for value in self.get_parameter("allowed_map_ids").value if str(value).strip()
        }
        self.map_min_x = float(self.get_parameter("map_min_x").value)
        self.map_max_x = float(self.get_parameter("map_max_x").value)
        self.map_min_y = float(self.get_parameter("map_min_y").value)
        self.map_max_y = float(self.get_parameter("map_max_y").value)
        self.max_abs_yaw = float(self.get_parameter("max_abs_yaw").value)
        self.command_queue_size = self._positive_int_parameter("command_queue_size")
        self.navigate_action_name = self._required_string_parameter("navigate_action_name")
        self.manual_cmd_vel_topic = self._required_string_parameter("manual_cmd_vel_topic")
        self.active_map_id = self._required_string_parameter("active_map_id")
        self.map_registry_file = str(self.get_parameter("map_registry_file").value).strip()
        self.map_state_file = str(self.get_parameter("map_state_file").value).strip()
        self.map_load_service = self._required_string_parameter("map_load_service")
        self.auto_localize_service = self._required_string_parameter("auto_localize_service")
        self.map_switch_topic = self._required_string_parameter("map_switch_topic")
        self.amcl_pose_topic = self._required_string_parameter("amcl_pose_topic")
        self.initial_pose_topic = self._required_string_parameter("initial_pose_topic")
        self.localization_status_topic = self._required_string_parameter("localization_status_topic")
        self.localization_scan_mode_topic = self._required_string_parameter("localization_scan_mode_topic")
        self.chassis_status_topic = self._required_string_parameter("chassis_status_topic")
        self.wheel_speed_topic = self._required_string_parameter("wheel_speed_topic")
        self.chassis_status_timeout_seconds = float(
            self.get_parameter("chassis_status_timeout_seconds").value
        )
        self.imu_stale_timeout_seconds = float(
            self.get_parameter("imu_stale_timeout_seconds").value
        )
        self.wheel_speed_timeout_seconds = float(
            self.get_parameter("wheel_speed_timeout_seconds").value
        )
        self.base_stationary_speed_threshold_mps = float(
            self.get_parameter("base_stationary_speed_threshold_mps").value
        )
        self.telemetry_period_seconds = float(self.get_parameter("telemetry_period_seconds").value)
        self.pose_stale_timeout_seconds = float(self.get_parameter("pose_stale_timeout_seconds").value)
        self.max_position_variance = float(self.get_parameter("max_position_variance").value)
        self.max_yaw_variance = float(self.get_parameter("max_yaw_variance").value)
        self.manual_command_timeout_seconds = float(
            self.get_parameter("manual_command_timeout_seconds").value
        )
        self.bookarm_api_url = self._required_string_parameter("bookarm_api_url")
        self.bookarm_backend = self._required_string_parameter("bookarm_backend")
        self.bookarm_timeout_seconds = float(self.get_parameter("bookarm_timeout_seconds").value)
        self.bookarm_rod_device = self._required_string_parameter("bookarm_rod_device")
        self.bookarm_lift_device = self._required_string_parameter("bookarm_lift_device")
        self.bookarm_takeout_socket = self._required_string_parameter("bookarm_takeout_socket")
        # An empty parameter falls back to the root-managed runtime.env value
        # so the shared BookArm token never lives in a Git-tracked YAML.
        self.bookarm_takeout_token = (
            str(self.get_parameter("bookarm_takeout_token").value).strip()
            or os.environ.get("LR_GATEWAY_BOOKARM_TOKEN", "").strip()
        )
        self.book_shelf_fine_tune_enabled = bool(
            self.get_parameter("book_shelf_fine_tune_enabled").value
        )
        self.book_shelf_fine_tune_parameter_service = self._required_string_parameter(
            "book_shelf_fine_tune_parameter_service"
        )
        self.book_shelf_fine_tune_start_service = self._required_string_parameter(
            "book_shelf_fine_tune_start_service"
        )
        self.book_shelf_fine_tune_abort_service = self._required_string_parameter(
            "book_shelf_fine_tune_abort_service"
        )
        self.book_shelf_fine_tune_settle_delay_seconds = float(
            self.get_parameter("book_shelf_fine_tune_settle_delay_seconds").value
        )
        self.hardware_profile = self._required_string_parameter("hardware_profile")
        self._registered_maps: dict[str, RegisteredMap] = {}
        self._configure_registered_maps()
        self._validate_configuration()

        self.command_topic = f"fleet/{self.robot_id}/command"
        self.event_topic = f"fleet/{self.robot_id}/event"
        self.telemetry_topic = f"fleet/{self.robot_id}/telemetry"
        self._telemetry_boot_id = f"BOOT-{uuid4().hex[:12].upper()}"
        self._telemetry_sequence = 0
        self._pending_commands: queue.Queue[GatewayCommand] = queue.Queue(maxsize=self.command_queue_size)
        self._seen_command_ids: OrderedDict[str, None] = OrderedDict()
        self._seen_lock = threading.Lock()
        self._active_command: GatewayCommand | None = None
        self._active_goal_handle = None
        self._pending_navigation_command: GatewayCommand | None = None
        self._pending_map_switch_command: GatewayCommand | None = None
        self._pending_auto_localize_command: GatewayCommand | None = None
        self._pending_fine_tune_command: GatewayCommand | None = None
        self._active_cruise_route: CruiseRoute | None = None
        self._cruise_waypoint_index = 0
        self._cruise_loop_index = 0
        self._manual_command_deadline: float | None = None
        self._manual_target_linear = 0.0
        self._manual_target_angular = 0.0
        self._manual_output_linear = 0.0
        self._manual_output_angular = 0.0
        self._bookarm_workflow_id: str | None = None
        self._book_shelf_fine_tune_timer = None
        self._bookarm_test_session: BookArmTestSession | None = None
        self._bookarm_test_command: GatewayCommand | None = None
        self._bookarm_test_last_state: tuple[object, ...] | None = None
        self._bookarm_health = BookArmHealth(False, False, False, False, False, "机械臂状态尚未读取")
        if self.bookarm_backend == "sdk":
            self._bookarm_client = BookArmSdkClient(self.bookarm_api_url, self.bookarm_timeout_seconds)
        elif self.bookarm_backend == "takeout":
            self._bookarm_client = BookArmTakeoutClient(
                self.bookarm_takeout_socket,
                self.bookarm_timeout_seconds,
                token=self.bookarm_takeout_token,
            )
        else:
            self._bookarm_client = BookArmClient(
                self.bookarm_api_url,
                self.bookarm_timeout_seconds,
                rod_device_path=self.bookarm_rod_device,
                lift_device_path=self.bookarm_lift_device,
            )
        self._hardware = HardwareAdapter(
            self._bookarm_client,
            profile=self.hardware_profile,
            base_is_stationary=self._base_is_stationary,
        )
        self._latest_pose: tuple[float, float, float] | None = None
        self._latest_pose_received_at: float | None = None
        self._manual_localization_active = False
        self._localization_status = "starting"
        self._localization_scan_mode = "unavailable"
        self._chassis_feedback_healthy = False
        self._chassis_status_received_at: float | None = None
        self._latest_imu_received_at: float | None = None
        self._latest_wheel_speeds: tuple[float, float, float, float] | None = None
        self._wheel_speeds_received_at: float | None = None
        self._action_client = ActionClient(self, NavigateToPose, self.navigate_action_name)
        self._book_shelf_fine_tune_parameter_client = self.create_client(
            SetParameters, self.book_shelf_fine_tune_parameter_service
        )
        self._book_shelf_fine_tune_start_client = self.create_client(
            Trigger, self.book_shelf_fine_tune_start_service
        )
        self._book_shelf_fine_tune_abort_client = self.create_client(
            Trigger, self.book_shelf_fine_tune_abort_service
        )
        self._map_load_client = self.create_client(LoadMap, self.map_load_service)
        self._auto_localize_client = self.create_client(Trigger, self.auto_localize_service)
        self._amcl_pose_subscription = self.create_subscription(
            PoseWithCovarianceStamped,
            self.amcl_pose_topic,
            self._handle_amcl_pose,
            10,
        )
        self._initial_pose_publisher = self.create_publisher(
            PoseWithCovarianceStamped,
            self.initial_pose_topic,
            10,
        )
        self._map_switch_publisher = self.create_publisher(String, self.map_switch_topic, 10)
        self._manual_velocity_publisher = self.create_publisher(Twist, self.manual_cmd_vel_topic, 10)
        self._localization_status_subscription = self.create_subscription(
            String,
            self.localization_status_topic,
            self._handle_localization_status,
            QoSProfile(
                depth=1,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
                reliability=ReliabilityPolicy.RELIABLE,
            ),
        )
        self._chassis_status_subscription = self.create_subscription(
            String,
            self.chassis_status_topic,
            self._handle_chassis_status,
            10,
        )
        self._imu_subscription = self.create_subscription(
            Imu,
            "/imu/data_raw",
            self._handle_imu_data,
            qos_profile_sensor_data,
        )
        self._wheel_speed_subscription = self.create_subscription(
            Float32MultiArray,
            self.wheel_speed_topic,
            self._handle_wheel_speeds,
            10,
        )
        self._localization_scan_mode_subscription = self.create_subscription(
            String,
            self.localization_scan_mode_topic,
            self._handle_localization_scan_mode,
            QoSProfile(
                depth=1,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
                reliability=ReliabilityPolicy.RELIABLE,
            ),
        )
        self._mqtt = self._create_mqtt_client()
        self._mqtt.loop_start()
        self._mqtt.connect_async(self.mqtt_host, self.mqtt_port, keepalive=30)
        self._queue_timer = self.create_timer(0.1, self._process_command_queue)
        self._telemetry_timer = self.create_timer(self.telemetry_period_seconds, self._publish_telemetry)
        self._last_command_received_at = time.monotonic()
        self._last_mqtt_reconnect_at = 0.0
        self._mqtt_watchdog_timer = self.create_timer(30.0, self._mqtt_subscription_watchdog)
        # The root-owned synchronization timer can add local maps while ROS is
        # online. Refreshing this registry never changes the active map.
        self._map_registry_refresh_timer = self.create_timer(2.0, self._refresh_registered_maps)
        self._manual_safety_timer = self.create_timer(0.05, self._manual_drive_tick)
        self._bookarm_health_timer = self.create_timer(2.0, self._refresh_bookarm_health)

        self.get_logger().info(
            f"Edge Gateway ready for {self.robot_id}; subscribing to {self.command_topic}, "
            f"Nav2 action {self.navigate_action_name}, pose topic {self.amcl_pose_topic}, "
            f"manual velocity topic {self.manual_cmd_vel_topic}"
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter("robot_id", "")
        self.declare_parameter("mqtt_host", "")
        self.declare_parameter("mqtt_port", 1883)
        self.declare_parameter("mqtt_protocol_version", "3.1.1")
        self.declare_parameter("mqtt_tls_enabled", False)
        self.declare_parameter("mqtt_ca_file", "")
        self.declare_parameter("mqtt_client_cert_file", "")
        self.declare_parameter("mqtt_client_key_file", "")
        self.declare_parameter("mqtt_username", "")
        self.declare_parameter("mqtt_password", "")
        # An empty Python list is inferred as BYTE_ARRAY by rclpy; declare an
        # explicit string-array default so YAML map IDs can override it safely.
        self.declare_parameter("allowed_map_ids", [""])
        self.declare_parameter("map_min_x", 0.0)
        self.declare_parameter("map_max_x", 0.0)
        self.declare_parameter("map_min_y", 0.0)
        self.declare_parameter("map_max_y", 0.0)
        self.declare_parameter("max_abs_yaw", math.tau)
        self.declare_parameter("command_queue_size", 10)
        self.declare_parameter("navigate_action_name", "navigate_to_pose")
        # Manual jog bypasses Nav2's smoother/collision monitor (operator drives
        # by sight); navigation keeps the full safety chain. See edge_gateway.yaml.
        self.declare_parameter("manual_cmd_vel_topic", "/cmd_vel")
        self.declare_parameter("active_map_id", "")
        # When configured, this root-managed registry is the only source of
        # maps selectable from MQTT. Empty preserves staged-upgrade support.
        self.declare_parameter("map_registry_file", "")
        self.declare_parameter("map_state_file", "~/.local/state/lr-robot/active_map.json")
        self.declare_parameter("map_load_service", "/map_server/load_map")
        # Motionless one-shot localization offered by the localization node.
        self.declare_parameter("auto_localize_service", "/localization/auto_localize")
        self.declare_parameter("map_switch_topic", "/localization/map_switch")
        self.declare_parameter("amcl_pose_topic", "/amcl_pose")
        self.declare_parameter("initial_pose_topic", "/initialpose")
        self.declare_parameter("localization_status_topic", "/localization/status")
        self.declare_parameter("localization_scan_mode_topic", "/localization/scan_mode")
        # The base controller publishes a JSON map of wheel feedback sources.
        # Gateway must see fresh encoder feedback before it permits manual motion.
        self.declare_parameter("chassis_status_topic", "/wheel_speed_source")
        self.declare_parameter("chassis_status_timeout_seconds", 2.5)
        # car_odometry integrates the IMU gyro z axis; a silent IMU dropout
        # degrades odometry without any other visible fault, so freshness is
        # tracked separately from chassis encoder feedback.
        self.declare_parameter("imu_stale_timeout_seconds", 2.0)
        # Feedback source health alone proves neither that the wheels are
        # stopped nor that an automatic recovery may hold the base in place.
        self.declare_parameter("wheel_speed_topic", "/wheel_speeds")
        self.declare_parameter("wheel_speed_timeout_seconds", 1.0)
        self.declare_parameter("base_stationary_speed_threshold_mps", 0.01)
        self.declare_parameter("telemetry_period_seconds", 0.5)
        self.declare_parameter("pose_stale_timeout_seconds", 5.0)
        self.declare_parameter("max_position_variance", 0.5)
        self.declare_parameter("max_yaw_variance", 0.35)
        self.declare_parameter("manual_command_timeout_seconds", 1.0)
        # The MotorStudio WebUI is deliberately loopback-only on the robot.
        # Its actual CAN/serial worker API is never exposed through MQTT.
        self.declare_parameter("bookarm_api_url", "http://127.0.0.1:18080/api/v1")
        self.declare_parameter("bookarm_backend", "legacy")
        self.declare_parameter("bookarm_timeout_seconds", 3.0)
        self.declare_parameter("bookarm_rod_device", "/dev/rodmotor")
        self.declare_parameter("bookarm_lift_device", "/dev/lift_port")
        # The takeout backend talks HTTP over the daemon's private Unix
        # socket; the Bearer token stays empty here and is injected from
        # LR_GATEWAY_BOOKARM_TOKEN (/etc/lr-robot/runtime.env) at runtime.
        self.declare_parameter("bookarm_takeout_socket", "/run/lr-bookarm/bookarm.sock")
        self.declare_parameter("bookarm_takeout_token", "")
        # Fine-tune commands set an approved dynamic target then invoke this
        # local-only service before any mechanism workflow can start.
        self.declare_parameter("book_shelf_fine_tune_enabled", False)
        self.declare_parameter(
            "book_shelf_fine_tune_parameter_service",
            "/book_shelf_fine_tune_node/set_parameters",
        )
        self.declare_parameter("book_shelf_fine_tune_start_service", "/book_shelf_fine_tune/start")
        self.declare_parameter("book_shelf_fine_tune_abort_service", "/book_shelf_fine_tune/abort")
        self.declare_parameter("book_shelf_fine_tune_settle_delay_seconds", 1.0)
        # This is an installed, evidence-backed vehicle profile rather than a
        # control-station setting. Unsupported feedback stays false in the
        # adapter regardless of the profile name.
        self.declare_parameter("hardware_profile", DEFAULT_HARDWARE_PROFILE)

    def _required_string_parameter(self, name: str) -> str:
        value = str(self.get_parameter(name).value).strip()
        if not value:
            raise RuntimeError(f"{name} must be configured")
        return value

    def _positive_int_parameter(self, name: str) -> int:
        value = int(self.get_parameter(name).value)
        if value <= 0:
            raise RuntimeError(f"{name} must be greater than zero")
        return value

    def _validate_configuration(self) -> None:
        bookarm_backend = getattr(self, "bookarm_backend", "legacy")
        if bookarm_backend not in {"legacy", "sdk", "takeout"}:
            raise RuntimeError("bookarm_backend must be legacy, sdk or takeout")
        if bookarm_backend == "takeout" and not getattr(self, "bookarm_takeout_token", ""):
            self.get_logger().warn(
                "bookarm_takeout_token is empty; this only works while the takeout "
                "service itself runs without a token"
            )
        if not self.allowed_map_ids:
            raise RuntimeError("allowed_map_ids must contain the map currently loaded by Nav2")
        if self.active_map_id not in self.allowed_map_ids:
            raise RuntimeError("active_map_id must be included in allowed_map_ids")
        if self.map_min_x >= self.map_max_x or self.map_min_y >= self.map_max_y:
            raise RuntimeError("configured map bounds are invalid")
        if self.max_abs_yaw <= 0.0:
            raise RuntimeError("max_abs_yaw must be greater than zero")
        if self.telemetry_period_seconds < 0.2:
            raise RuntimeError("telemetry_period_seconds must be at least 0.2 seconds")
        if self.pose_stale_timeout_seconds <= 0.0:
            raise RuntimeError("pose_stale_timeout_seconds must be greater than zero")
        if self.max_position_variance <= 0.0 or self.max_yaw_variance <= 0.0:
            raise RuntimeError("AMCL covariance limits must be greater than zero")
        if self.manual_command_timeout_seconds <= 0.0 or self.manual_command_timeout_seconds > 2.0:
            raise RuntimeError("manual_command_timeout_seconds must be from 0 to 2")
        if self.chassis_status_timeout_seconds <= 0.0 or self.chassis_status_timeout_seconds > 10.0:
            raise RuntimeError("chassis_status_timeout_seconds must be from 0 to 10")
        imu_stale_timeout_seconds = float(getattr(self, "imu_stale_timeout_seconds", 2.0))
        if imu_stale_timeout_seconds <= 0.0:
            raise RuntimeError("imu_stale_timeout_seconds must be greater than zero")
        if self.wheel_speed_timeout_seconds <= 0.0 or self.wheel_speed_timeout_seconds > 10.0:
            raise RuntimeError("wheel_speed_timeout_seconds must be from 0 to 10")
        if not 0.0 < self.base_stationary_speed_threshold_mps <= 0.1:
            raise RuntimeError("base_stationary_speed_threshold_mps must be from 0 to 0.1")
        bookarm_timeout_seconds = float(getattr(self, "bookarm_timeout_seconds", 3.0))
        if bookarm_timeout_seconds <= 0.0 or bookarm_timeout_seconds > 15.0:
            raise RuntimeError("bookarm_timeout_seconds must be from 0 to 15")
        fine_tune_settle_delay = float(
            getattr(self, "book_shelf_fine_tune_settle_delay_seconds", 1.0)
        )
        if not 0.0 <= fine_tune_settle_delay <= 10.0:
            raise RuntimeError("book_shelf_fine_tune_settle_delay_seconds must be from 0 to 10")
        if self.mqtt_tls_enabled and not self.mqtt_ca_file:
            raise RuntimeError("mqtt_ca_file is required when mqtt_tls_enabled is true")
        if self.mqtt_tls_enabled and not (self.mqtt_client_cert_file and self.mqtt_client_key_file):
            raise RuntimeError("mqtt client certificate and key are required when mqtt_tls_enabled is true")
        if bool(self.mqtt_client_cert_file) != bool(self.mqtt_client_key_file):
            raise RuntimeError("mqtt_client_cert_file and mqtt_client_key_file must be configured together")
        if self.mqtt_protocol_version not in {"3.1.1", "5"}:
            raise RuntimeError("mqtt_protocol_version must be 3.1.1 or 5")
        if not self.mqtt_tls_enabled:
            self.get_logger().warn("MQTT TLS is disabled; use this only on an isolated development network")

    def _configure_registered_maps(self) -> None:
        """Resolve a persisted local map selection before accepting MQTT commands."""

        if not self.map_registry_file:
            return
        registry_path = Path(self.map_registry_file).expanduser()
        state_path = Path(self.map_state_file).expanduser()
        if not registry_path.is_file():
            self.get_logger().warn(
                "map registry is not installed yet; automatic map switching remains unavailable"
            )
            return
        try:
            registered_maps = load_map_registry(registry_path)
            selected_map_id = active_map_id(state_path, self.active_map_id)
        except ValueError as error:
            raise RuntimeError(f"map registry configuration is invalid: {error}") from error
        selected = registered_maps.get(selected_map_id)
        if selected is None:
            raise RuntimeError("active_map_id is not registered in map_registry_file")
        self._registered_maps = registered_maps
        self.allowed_map_ids = set(registered_maps)
        self._apply_registered_map(selected)

    def _refresh_registered_maps(self) -> None:
        """Expose newly synchronized maps without restarting navigation."""

        if not self.map_registry_file:
            return
        try:
            registered_maps = load_map_registry(Path(self.map_registry_file).expanduser())
        except ValueError as error:
            self.get_logger().warn(f"map registry refresh ignored: {error}")
            return
        active = registered_maps.get(self.active_map_id)
        if active is None:
            self.get_logger().warn("map registry refresh ignored because it lacks the active map")
            return
        previous_ids = set(self._registered_maps)
        self._registered_maps = registered_maps
        self.allowed_map_ids = set(registered_maps)
        self._apply_registered_map(active)
        if set(registered_maps) != previous_ids:
            self.get_logger().info("local map registry refreshed: %s" % ", ".join(sorted(registered_maps)))

    def _apply_registered_map(self, selected: RegisteredMap) -> None:
        self.active_map_id = selected.map_id
        self.map_min_x = selected.min_x
        self.map_max_x = selected.max_x
        self.map_min_y = selected.min_y
        self.map_max_y = selected.max_y

    def _create_mqtt_client(self) -> mqtt.Client:
        callback_version = getattr(mqtt, "CallbackAPIVersion", None)
        protocol = mqtt.MQTTv5 if self.mqtt_protocol_version == "5" else mqtt.MQTTv311
        client_options = {"client_id": f"lr-edge-{self.robot_id}", "protocol": protocol}
        if callback_version is not None:
            client_options["callback_api_version"] = callback_version.VERSION2
        if protocol == mqtt.MQTTv311:
            client_options["clean_session"] = False
        client = mqtt.Client(**client_options)
        if self.mqtt_username:
            client.username_pw_set(self.mqtt_username, self.mqtt_password)
        if self.mqtt_tls_enabled:
            client.tls_set(
                ca_certs=self.mqtt_ca_file,
                certfile=self.mqtt_client_cert_file or None,
                keyfile=self.mqtt_client_key_file or None,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
            client.tls_insecure_set(False)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_mqtt_connect
        client.on_disconnect = self._on_mqtt_disconnect
        client.on_message = self._on_mqtt_message
        return client

    def _on_mqtt_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        if int(reason_code) != 0:
            self.get_logger().error(f"MQTT connection refused: {reason_code}")
            return
        result, _mid = client.subscribe(self.command_topic, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self.get_logger().error(f"MQTT subscribe failed with code {result}")
            return
        self.get_logger().info(f"MQTT connected; subscribed to {self.command_topic}")
        self._publish_event(
            "gateway_online",
            details={
                "allowedMapIds": sorted(self.allowed_map_ids),
            },
        )

    def _on_mqtt_disconnect(self, _client, _userdata, _rc, *arguments) -> None:
        # Paho MQTT 1.x passes ``rc``; 2.x passes flags, reason code, properties.
        reason_code = arguments[0] if arguments else _rc
        self.get_logger().warn(f"MQTT disconnected: {reason_code}")

    def _mqtt_subscription_watchdog(self) -> None:
        """Force an MQTT reconnect when command intake looks silently dead.

        The broker can silently drop a session's subscription while the TCP
        link stays healthy: telemetry keeps flowing out, but commands stop
        arriving. A reconnect re-issues the subscription via
        ``_on_mqtt_connect``. Only fires when no command has arrived for a
        while and never more than once per 90 seconds, so healthy periods
        are unaffected. clean_session=False keeps the session across the
        reconnect, and QoS-1 commands queue through the sub-second gap.
        """
        now = time.monotonic()
        if now - self._last_command_received_at < 90.0:
            return
        if now - self._last_mqtt_reconnect_at < 90.0:
            return
        if not self._mqtt.is_connected():
            return
        self._last_mqtt_reconnect_at = now
        self.get_logger().warn(
            "no commands received for 90 s; forcing MQTT reconnect to refresh the subscription"
        )
        try:
            self._mqtt.reconnect()
        except Exception as error:  # pragma: no cover - network failures are runtime-only.
            self.get_logger().error(f"MQTT watchdog reconnect failed: {error}")

    def _on_mqtt_message(self, _client, _userdata, message) -> None:
        if message.topic != self.command_topic:
            return
        self._last_command_received_at = time.monotonic()
        try:
            command = parse_gateway_command(message.payload, self.robot_id)
        except ValueError as error:
            self._publish_event("command_rejected", details={"reason": str(error)})
            return
        if not self._remember_command(command.command_id):
            self._publish_event("command_duplicate", command, {"reason": "commandId was already received"})
            return
        try:
            self._pending_commands.put_nowait(command)
        except queue.Full:
            self._publish_event("command_rejected", command, {"reason": "gateway command queue is full"})
            return
        self._publish_event("command_received", command)

    def _remember_command(self, command_id: str) -> bool:
        with self._seen_lock:
            if command_id in self._seen_command_ids:
                return False
            self._seen_command_ids[command_id] = None
            # QoS redeliveries only need a bounded idempotency cache on the robot.
            if len(self._seen_command_ids) > 512:
                self._seen_command_ids.popitem(last=False)
            return True

    def _process_command_queue(self) -> None:
        try:
            command = self._pending_commands.get_nowait()
        except queue.Empty:
            return
        if command.expires_at <= datetime.now(UTC):
            self._publish_event("command_rejected", command, {"reason": "command expired before execution"})
            return
        if self._pending_map_switch_command and command.command not in {"cancel", "stop", "emergency_stop"}:
            self._publish_event("command_rejected", command, {"reason": "map switch is still in progress"})
            return
        if self._pending_auto_localize_command and command.command not in {"cancel", "stop", "emergency_stop"}:
            self._publish_event(
                "command_rejected", command, {"reason": "auto localization is still in progress"}
            )
            return
        if self._pending_fine_tune_command and command.command not in {"cancel", "stop", "emergency_stop"}:
            self._publish_event("command_rejected", command, {"reason": "fine tune is still in progress"})
            return
        if command.command == "navigate":
            self._start_navigation(command)
            return
        if command.command == "set_initial_pose":
            self._set_initial_pose(command)
            return
        if command.command == "auto_localize":
            self._auto_localize(command)
            return
        if command.command == "fine_tune":
            self._start_fine_tune(command)
            return
        if command.command == "switch_map":
            self._switch_map(command)
            return
        if command.command == "cruise":
            self._start_cruise(command)
            return
        if command.command == "manual_jog":
            self._manual_jog(command)
            return
        if command.command == "bookarm_test_start":
            self._start_bookarm_test(command)
            return
        if command.command == "bookarm_test_continue":
            self._continue_bookarm_test(command)
            return
        if command.command == "bookarm_test_cancel":
            self._cancel_bookarm_test(command, "operator cancelled maintenance test")
            return
        if command.command == "lift_move":
            self._move_lift(command)
            return
        if command.command == "mechanism_home":
            self._home_mechanisms(command)
            return
        self._cancel_navigation(command)

    def _switch_map(self, command: GatewayCommand) -> None:
        """Load an already staged map and force localization to restart.

        Map data and paths never travel over MQTT.  The selected map must come
        from the root-managed local registry, and map loading runs only while
        all independent robot work is idle and encoder feedback proves the
        base is stationary.
        """

        if not self._registered_maps:
            self._publish_event(
                "command_rejected",
                command,
                {"reason": "automatic map switching is not configured on this vehicle"},
            )
            return
        try:
            switch: MapSwitch = parse_map_switch(command.payload, set(self._registered_maps))
        except ValueError as error:
            self._publish_event("command_rejected", command, {"reason": str(error)})
            return
        if switch.map_id == self.active_map_id:
            self._publish_event("command_rejected", command, {"reason": "requested map is already active"})
            return
        if (
            self._manual_control_active()
            or self._bookarm_test_command
            or self._active_command
            or self._pending_navigation_command
        ):
            self._publish_event("command_rejected", command, {"reason": "another robot activity is still active"})
            return
        if not self._base_is_stationary():
            self._publish_event(
                "command_rejected",
                command,
                {"reason": "base wheel feedback is not fresh and stationary"},
            )
            return
        if not self._map_load_client.wait_for_service(timeout_sec=0.2):
            self._publish_event("map_switch_failed", command, {"reason": "Nav2 map load service is unavailable"})
            return

        previous_map_id = self.active_map_id
        target = self._registered_maps[switch.map_id]
        state_path = Path(self.map_state_file).expanduser()
        try:
            # A reboot during LoadMap should restart with the requested target,
            # never silently revert to the old map's coordinate frame.
            stage_pending_switch(state_path, target.map_id, target.map_id)
        except ValueError as error:
            self._publish_event("map_switch_failed", command, {"reason": str(error)})
            return

        self._pending_map_switch_command = command
        request = LoadMap.Request()
        request.map_url = str(target.yaml_path)
        future = self._map_load_client.call_async(request)
        future.add_done_callback(
            lambda result: self._handle_map_switch_result(command, previous_map_id, target, result)
        )

    def _handle_map_switch_result(
        self,
        command: GatewayCommand,
        previous_map_id: str,
        target: RegisteredMap,
        future,
    ) -> None:
        if self._pending_map_switch_command is not command:
            return
        self._pending_map_switch_command = None
        try:
            response = future.result()
            loaded = response.result == LoadMap.Response.RESULT_SUCCESS
        except Exception as error:  # pragma: no cover - ROS transport failures are runtime-only.
            self._restore_previous_map_selection(command, previous_map_id, str(error))
            return
        if not loaded:
            self._restore_previous_map_selection(command, previous_map_id, "Nav2 rejected the staged map")
            return

        self._apply_registered_map(target)
        self._latest_pose = None
        self._latest_pose_received_at = None
        self._localization_status = "manual_required"
        try:
            commit_map_switch(Path(self.map_state_file).expanduser(), target.map_id)
        except ValueError as error:
            # The active ID was atomically staged before LoadMap.  Continue to
            # report the actual loaded map even if housekeeping could not drop
            # its pending marker, rather than lying that the old map is live.
            self.get_logger().warn(f"map switch state finalization failed: {error}")
        self._map_switch_publisher.publish(String(data=target.map_id))
        self._publish_event(
            "map_switch_succeeded",
            command,
            {
                "mapId": target.map_id,
                "localizationRequired": True,
            },
        )

    def _restore_previous_map_selection(
        self,
        command: GatewayCommand,
        previous_map_id: str,
        reason: str,
    ) -> None:
        if self._pending_map_switch_command is command:
            self._pending_map_switch_command = None
        try:
            cancel_pending_switch(Path(self.map_state_file).expanduser(), previous_map_id)
        except ValueError as error:
            self.get_logger().error(f"failed to restore map selection after LoadMap failure: {error}")
        self._publish_event("map_switch_failed", command, {"reason": str(reason)[:300]})

    def _active_map_bounds(self) -> tuple[float, float, float, float]:
        """Return the bounds belonging to the currently loaded map only."""

        return self.map_min_x, self.map_max_x, self.map_min_y, self.map_max_y

    def _set_initial_pose(self, command: GatewayCommand) -> None:
        """Forward a web-confirmed location to AMCL; this never moves the robot."""
        if self._active_command or self._pending_navigation_command:
            self._publish_event(
                "command_rejected",
                command,
                {"reason": "cannot change localization while a navigation goal is active"},
            )
            return
        try:
            min_x, max_x, min_y, max_y = self._active_map_bounds()
            initial_pose = parse_initial_pose(
                command.payload,
                {self.active_map_id},
                min_x,
                max_x,
                min_y,
                max_y,
                self.max_abs_yaw,
            )
        except ValueError as error:
            self._publish_event("command_rejected", command, {"reason": str(error)})
            return
        if initial_pose.map_id != self.active_map_id:
            self._publish_event(
                "command_rejected", command, {"reason": "initial pose mapId is not the active vehicle map"}
            )
            return
        self._initial_pose_publisher.publish(
            make_initial_pose(self, "map", initial_pose.x, initial_pose.y, 0.0, initial_pose.yaw)
        )
        # AMCL can legitimately wait for motion before it emits /amcl_pose.
        # Preserve the operator-confirmed pose for telemetry once localization
        # manager reports that AMCL accepted it.
        self._latest_pose = (initial_pose.x, initial_pose.y, initial_pose.yaw)
        self._manual_localization_active = True
        self._publish_event(
            "localization_initializing",
            command,
            {
                "mapId": initial_pose.map_id,
                "x": initial_pose.x,
                "y": initial_pose.y,
                "yaw": initial_pose.yaw,
            },
        )

    def _auto_localize(self, command: GatewayCommand) -> None:
        """Ask the localization node for one motionless whole-map scan match."""

        if (
            self._manual_control_active()
            or self._bookarm_test_command
            or self._active_command
            or self._pending_navigation_command
        ):
            self._publish_event("command_rejected", command, {"reason": "another robot activity is still active"})
            return
        try:
            map_id = parse_auto_localize(command.payload, self.active_map_id)
        except ValueError as error:
            self._publish_event("command_rejected", command, {"reason": str(error)})
            return
        if not self._auto_localize_client.wait_for_service(timeout_sec=1.0):
            self._publish_event(
                "command_rejected", command, {"reason": "auto localization service is unavailable"}
            )
            return
        self._pending_auto_localize_command = command
        # The whole-map match can take up to ~30 s; the done callback keeps
        # the Gateway main loop free for telemetry and stop commands.
        future = self._auto_localize_client.call_async(Trigger.Request())
        future.add_done_callback(lambda result: self._handle_auto_localize_result(command, map_id, result))

    def _handle_auto_localize_result(self, command: GatewayCommand, map_id: str, future) -> None:
        if self._pending_auto_localize_command is not command:
            return
        self._pending_auto_localize_command = None
        try:
            response = future.result()
        except Exception as error:  # pragma: no cover - ROS transport failures are runtime-only.
            self.get_logger().error(f"auto localization service call failed: {error}")
            self._publish_event(
                "command_rejected", command, {"reason": "auto localization service call failed"}
            )
            return
        if not response.success:
            self._publish_event(
                "command_rejected",
                command,
                {"reason": str(response.message)[:300] or "auto localization failed"},
            )
            return
        message = str(response.message)
        details: dict[str, Any] = {"mapId": map_id, "message": message.split("|pose=", 1)[0]}
        pose = _auto_localize_pose_from_message(message)
        if pose is not None:
            # Mirror the manual-click bookkeeping so telemetry shows the
            # matched pose until AMCL confirms the localization heartbeat.
            self._latest_pose = pose
            self._manual_localization_active = True
            details.update({"x": pose[0], "y": pose[1], "yaw": pose[2]})
        self._publish_event("localization_initializing", command, details)

    def _start_navigation(self, command: GatewayCommand) -> None:
        if self._manual_control_active() or self._bookarm_test_command:
            self._publish_event(
                "command_rejected", command, {"reason": "manual or book-arm maintenance control is active"}
            )
            return
        if not self._imu_ready():
            self._publish_event(
                "command_rejected",
                command,
                {"reason": "IMU feedback is unavailable or stale; navigation is blocked"},
            )
            return
        if not self._localization_is_reliable():
            self._publish_event(
                "command_rejected",
                command,
                {"reason": "robot localization is not stable; navigation is blocked"},
            )
            return
        try:
            min_x, max_x, min_y, max_y = self._active_map_bounds()
            goal = parse_navigation_goal(
                command.payload,
                {self.active_map_id},
                min_x,
                max_x,
                min_y,
                max_y,
                self.max_abs_yaw,
            )
        except ValueError as error:
            self._publish_event("command_rejected", command, {"reason": str(error)})
            return
        if goal.map_id != self.active_map_id:
            self._publish_event("command_rejected", command, {"reason": "navigation mapId is not the active vehicle map"})
            return
        if self._active_command or self._pending_navigation_command:
            self._publish_event("command_rejected", command, {"reason": "a navigation goal is already active"})
            return
        if not self._action_client.server_is_ready():
            self._publish_event("command_rejected", command, {"reason": "Nav2 NavigateToPose server is unavailable"})
            return

        self._pending_navigation_command = command
        ros_goal = NavigateToPose.Goal()
        ros_goal.pose = self._pose_from_goal(goal)
        future = self._action_client.send_goal_async(ros_goal, feedback_callback=self._navigation_feedback)
        future.add_done_callback(lambda result: self._handle_goal_response(command, goal, result))
        self._publish_event("navigation_dispatching", command, {"mapId": goal.map_id, "annotationId": goal.annotation_id})

    def _start_cruise(self, command: GatewayCommand) -> None:
        if self._manual_control_active() or self._bookarm_test_command:
            self._publish_event(
                "command_rejected", command, {"reason": "manual or book-arm maintenance control is active"}
            )
            return
        if not self._localization_is_reliable():
            self._publish_event(
                "command_rejected", command, {"reason": "robot localization is not stable; cruise is blocked"}
            )
            return
        if self._active_command or self._pending_navigation_command:
            self._publish_event(
                "command_rejected", command, {"reason": "a navigation goal is already active"}
            )
            return
        if not self._action_client.server_is_ready():
            self._publish_event(
                "command_rejected", command, {"reason": "Nav2 NavigateToPose server is unavailable"}
            )
            return
        try:
            min_x, max_x, min_y, max_y = self._active_map_bounds()
            route = parse_cruise_route(
                command.payload,
                {self.active_map_id},
                min_x,
                max_x,
                min_y,
                max_y,
                self.max_abs_yaw,
            )
        except ValueError as error:
            self._publish_event("command_rejected", command, {"reason": str(error)})
            return
        if route.map_id != self.active_map_id:
            self._publish_event("command_rejected", command, {"reason": "cruise mapId is not the active vehicle map"})
            return
        self._active_command = command
        self._active_cruise_route = route
        self._cruise_waypoint_index = 0
        self._cruise_loop_index = 0
        self._publish_event(
            "cruise_dispatching",
            command,
            {"mapId": route.map_id, "waypointCount": len(route.waypoints), "loopCount": route.loop_count},
        )
        self._send_next_cruise_waypoint()

    # ---- Book-arm maintenance ------------------------------------

    def _start_bookarm_test(self, command: GatewayCommand) -> None:
        if self._manual_control_active():
            self._publish_event("command_rejected", command, {"reason": "manual control is active"})
            return
        if (
            self._bookarm_test_command
            or self._active_command
            or self._pending_navigation_command
        ):
            self._publish_event(
                "command_rejected",
                command,
                {"reason": "another navigation, book task, or maintenance test is active"},
            )
            return
        if not self._hardware_capabilities()["armMotion"]:
            self._publish_event("command_rejected", command, {"reason": "arm motion is unavailable on this hardware profile"})
            return
        try:
            session = parse_bookarm_test_start(command.payload)
            health = self._hardware_bookarm_health()
            if not (health.lift_ready and health.camera_ready and health.rod_ready):
                raise BookArmApiError(health.message)
            health = self._bookarm_client.prepare_arm()
            if not health.arm_ready:
                raise BookArmApiError("机械臂连接或使能未确认")
            workflow = self._bookarm_client.start_workflow(session.workflow_mode, auto=False)
        except (ValueError, BookArmApiError) as error:
            self._publish_event("command_rejected", command, {"reason": str(error)})
            return
        self._bookarm_health = health
        self._bookarm_test_session = session
        self._bookarm_test_command = command
        self._bookarm_workflow_id = str(workflow["id"])
        self._bookarm_test_last_state = None
        self._publish_event(
            "bookarm_test_started",
            command,
            {
                "sessionId": session.session_id,
                "workflowId": self._bookarm_workflow_id,
                "workflowMode": session.workflow_mode,
                "stepMode": "manual",
            },
        )
        self._publish_bookarm_test_state(command, workflow)

    def _continue_bookarm_test(self, command: GatewayCommand) -> None:
        session = self._bookarm_test_session
        active_command = self._bookarm_test_command
        if not session or not active_command or not self._bookarm_workflow_id:
            self._publish_event("command_rejected", command, {"reason": "no book-arm maintenance test is active"})
            return
        try:
            session_id = parse_bookarm_test_session_id(command.payload)
        except ValueError as error:
            self._publish_event("command_rejected", command, {"reason": str(error)})
            return
        if session_id != session.session_id:
            self._publish_event("command_rejected", command, {"reason": "maintenance sessionId does not match"})
            return
        try:
            current = self._bookarm_client.workflow(self._bookarm_workflow_id)
            if current.get("waiting") is True:
                raise BookArmApiError("机械臂当前步骤仍在执行")
            if current.get("active") is not True:
                raise BookArmApiError("机械臂维护流程已经结束")
            workflow = self._bookarm_client.continue_workflow(self._bookarm_workflow_id)
        except BookArmApiError as error:
            self._publish_event("command_rejected", command, {"reason": str(error)})
            return
        self._publish_bookarm_test_state(active_command, workflow)
        self._publish_event(
            "command_completed",
            command,
            {"result": "book-arm maintenance step accepted", "sessionId": session.session_id},
        )

    def _cancel_bookarm_test(self, command: GatewayCommand, reason: str) -> None:
        session = self._bookarm_test_session
        active_command = self._bookarm_test_command
        if not session or not active_command:
            self._publish_event("command_rejected", command, {"reason": "no book-arm maintenance test is active"})
            return
        if command.command == "bookarm_test_cancel":
            try:
                session_id = parse_bookarm_test_session_id(command.payload)
            except ValueError as error:
                self._publish_event("command_rejected", command, {"reason": str(error)})
                return
            if session_id != session.session_id:
                self._publish_event("command_rejected", command, {"reason": "maintenance sessionId does not match"})
                return
        try:
            if command.command != "emergency_stop" and self._bookarm_workflow_id:
                self._bookarm_client.cancel_workflow(self._bookarm_workflow_id)
        except BookArmApiError as error:
            self.get_logger().warn(f"book-arm maintenance stop failed: {error}")
        self._request_hardware_safe_stop(emergency=command.command == "emergency_stop")
        self._publish_event(
            "bookarm_test_cancelled",
            active_command,
            {"sessionId": session.session_id, "reason": reason},
        )
        self._clear_bookarm_test()
        self._publish_event(
            "command_completed",
            command,
            {"result": "book-arm maintenance test stopped", "sessionId": session.session_id},
        )

    def _publish_bookarm_test_state(self, command: GatewayCommand, workflow: dict[str, Any]) -> None:
        session = self._bookarm_test_session
        if not session or self._bookarm_test_command is not command:
            return
        step_index = workflow.get("step_index")
        step_count = workflow.get("step_count")
        active = workflow.get("active")
        waiting = workflow.get("waiting")
        message = str(workflow.get("message", ""))[:240]
        if not isinstance(step_index, int) or not isinstance(step_count, int) or step_count <= 0:
            self._fail_bookarm_test(command, "机械臂维护工作流状态无效")
            return
        preflight_state = workflow.get("preflight_state")
        preflight_complete = workflow.get("preflight_complete")
        state_key = (
            step_index,
            bool(active),
            bool(waiting),
            message,
            preflight_state,
            bool(preflight_complete),
        )
        if self._bookarm_test_last_state == state_key:
            return
        self._bookarm_test_last_state = state_key
        details: dict[str, Any] = {
            "sessionId": session.session_id,
            "workflowId": self._bookarm_workflow_id,
            "workflowMode": session.workflow_mode,
            "stepIndex": step_index,
            "stepCount": step_count,
            "stepName": workflow.get("step_name"),
            "waiting": bool(waiting),
            "message": message,
        }
        capture_result = workflow.get("capture_result")
        recognition_result = workflow.get("recognition_result")
        if isinstance(capture_result, dict):
            details["captureResult"] = capture_result
        if isinstance(recognition_result, dict):
            details["recognitionResult"] = recognition_result
        if isinstance(workflow.get("preflight_required"), bool):
            details["preflightRequired"] = workflow["preflight_required"]
        if isinstance(preflight_complete, bool):
            details["preflightComplete"] = preflight_complete
        if isinstance(preflight_state, str):
            details["preflightState"] = preflight_state
        if isinstance(workflow.get("preflight_name"), str):
            details["preflightName"] = workflow["preflight_name"]
        if bool(active):
            self._publish_event("bookarm_test_workflow", command, details)
            return
        if step_index >= step_count and not workflow.get("safety_stop_state"):
            self._publish_event(
                "bookarm_test_succeeded",
                command,
                {**details, "message": message or "机械臂维护测试已完成"},
            )
            self._clear_bookarm_test()
            return
        self._fail_bookarm_test(command, message or "book-arm maintenance workflow stopped")

    def _fail_bookarm_test(self, command: GatewayCommand, reason: str) -> None:
        session = self._bookarm_test_session
        if session and self._bookarm_test_command is command:
            self._request_hardware_safe_stop()
            self._publish_event(
                "bookarm_test_failed",
                command,
                {"sessionId": session.session_id, "reason": str(reason)[:300]},
            )
            self._clear_bookarm_test()
            return
        self._publish_event("command_rejected", command, {"reason": str(reason)[:300]})

    def _clear_bookarm_test(self) -> None:
        self._bookarm_test_session = None
        self._bookarm_test_command = None
        self._bookarm_workflow_id = None
        self._bookarm_test_last_state = None

    @staticmethod
    def _string_parameter(name: str, value: str) -> ParameterMsg:
        parameter = ParameterMsg()
        parameter.name = name
        parameter.value = ParameterValue(type=ParameterType.PARAMETER_STRING, string_value=value)
        return parameter

    @staticmethod
    def _double_parameter(name: str, value: float) -> ParameterMsg:
        parameter = ParameterMsg()
        parameter.name = name
        parameter.value = ParameterValue(type=ParameterType.PARAMETER_DOUBLE, double_value=float(value))
        return parameter

    def _abort_book_shelf_fine_tune(self) -> None:
        timer = getattr(self, "_book_shelf_fine_tune_timer", None)
        if timer is not None:
            timer.cancel()
            self._book_shelf_fine_tune_timer = None
        client = getattr(self, "_book_shelf_fine_tune_abort_client", None)
        if client is None or not client.wait_for_service(timeout_sec=0.1):
            return
        try:
            client.call_async(Trigger.Request())
        except Exception as error:  # pragma: no cover - ROS transport failures are runtime-only.
            self.get_logger().warn(f"book shelf finetune abort request failed: {error}")

    # ---- Standalone shelf fine tune -------------------------------

    def _start_fine_tune(self, command: GatewayCommand) -> None:
        """Align precisely on an operator target through the local fine-tune service."""
        if (
            self._manual_control_active()
            or self._bookarm_test_command
            or self._active_command
            or self._pending_navigation_command
            or self._pending_fine_tune_command
        ):
            self._publish_event("command_rejected", command, {"reason": "another robot activity is still active"})
            return
        if not self._localization_is_reliable():
            self._publish_event(
                "command_rejected",
                command,
                {"reason": "robot localization is not stable; fine tune is blocked"},
            )
            return
        if not self.book_shelf_fine_tune_enabled:
            self._publish_event(
                "command_rejected",
                command,
                {"reason": "book shelf finetune is disabled on this vehicle"},
            )
            return
        try:
            min_x, max_x, min_y, max_y = self._active_map_bounds()
            target = parse_fine_tune(
                command.payload,
                min_x,
                max_x,
                min_y,
                max_y,
                self.max_abs_yaw,
            )
        except ValueError as error:
            self._publish_event("command_rejected", command, {"reason": str(error)})
            return
        if target.map_id is not None and target.map_id != self.active_map_id:
            self._publish_event(
                "command_rejected", command, {"reason": "fine tune mapId is not the active vehicle map"}
            )
            return
        parameter_client = self._book_shelf_fine_tune_parameter_client
        if not parameter_client.wait_for_service(timeout_sec=0.2):
            self._publish_event(
                "command_rejected",
                command,
                {"reason": "book shelf finetune parameter service is unavailable"},
            )
            return
        self._pending_fine_tune_command = command
        self._publish_event(
            "fine_tune_dispatching",
            command,
            {
                "mapId": target.map_id or self.active_map_id,
                "targetX": target.x,
                "targetY": target.y,
                "targetYaw": target.yaw,
            },
        )
        request = SetParameters.Request()
        request.parameters = [
            self._string_parameter("target_task_id", command.command_id),
            self._double_parameter("target_x", target.x),
            self._double_parameter("target_y", target.y),
            self._double_parameter("target_yaw", target.yaw),
        ]
        future = parameter_client.call_async(request)
        future.add_done_callback(lambda result: self._handle_fine_tune_target(command, target, result))

    def _handle_fine_tune_target(self, command: GatewayCommand, target: FineTuneTarget, future) -> None:
        if self._pending_fine_tune_command is not command:
            return
        try:
            response = future.result()
        except Exception as error:  # pragma: no cover - ROS transport failures are runtime-only.
            self._fail_fine_tune(command, f"book shelf finetune target update failed: {error}")
            return
        if not response.results or not all(result.successful for result in response.results):
            reasons = "; ".join(result.reason for result in response.results if result.reason)
            self._fail_fine_tune(command, f"book shelf finetune target was rejected: {reasons or 'no result'}")
            return
        start_client = self._book_shelf_fine_tune_start_client
        if not start_client.wait_for_service(timeout_sec=0.2):
            self._fail_fine_tune(command, "book shelf finetune start service is unavailable")
            return
        # The Trigger response only arrives when the low-speed run finishes,
        # so the done callback keeps the Gateway loop free for telemetry and
        # stop commands while the base is aligning.
        future = start_client.call_async(Trigger.Request())
        future.add_done_callback(lambda result: self._handle_fine_tune_result(command, target, result))

    def _handle_fine_tune_result(self, command: GatewayCommand, target: FineTuneTarget, future) -> None:
        if self._pending_fine_tune_command is not command:
            return
        self._pending_fine_tune_command = None
        try:
            response = future.result()
        except Exception as error:  # pragma: no cover - ROS transport failures are runtime-only.
            self.get_logger().error(f"fine tune service call failed: {error}")
            self._publish_event("command_rejected", command, {"reason": "fine tune service call failed"})
            return
        if not response.success:
            self._publish_event(
                "command_rejected",
                command,
                {"reason": str(response.message)[:300] or "fine tune failed"},
            )
            return
        details: dict[str, Any] = {
            "mapId": target.map_id or self.active_map_id,
            "targetX": target.x,
            "targetY": target.y,
            "targetYaw": target.yaw,
            "message": str(response.message)[:240],
        }
        pose = self._latest_pose
        if (
            pose is not None
            and self._latest_pose_received_at is not None
            and time.monotonic() - self._latest_pose_received_at <= self.pose_stale_timeout_seconds
        ):
            details.update({"actualX": pose[0], "actualY": pose[1], "actualYaw": pose[2]})
        self._publish_event("command_completed", command, details)

    def _fail_fine_tune(self, command: GatewayCommand, reason: str) -> None:
        if self._pending_fine_tune_command is command:
            self._pending_fine_tune_command = None
        self._publish_event("command_rejected", command, {"reason": str(reason)[:300]})

    def _cancel_fine_tune(self, command: GatewayCommand, reason: str) -> None:
        active_command = self._pending_fine_tune_command
        self._pending_fine_tune_command = None
        self._abort_book_shelf_fine_tune()
        self._request_hardware_safe_stop(emergency=command.command == "emergency_stop")
        if active_command is not None:
            self._publish_event("fine_tune_cancelled", active_command, {"reason": reason})
        self._publish_event("command_completed", command, {"result": "fine tune cancellation requested"})

    def _move_lift(self, command: GatewayCommand) -> None:
        if (
            self._bookarm_test_command
            or self._active_command
            or self._pending_navigation_command
            or self._manual_control_active()
        ):
            self._publish_event("command_rejected", command, {"reason": "cannot move lift while navigation or another control session is active"})
            return
        try:
            move: LiftMove = parse_lift_move(command.payload)
            result = self._hardware.move_lift_relative(move.distance_cm)
        except (ValueError, BookArmApiError) as error:
            self._publish_event("command_rejected", command, {"reason": str(error)})
            return
        if not result.get("accepted", False):
            self._publish_event("command_rejected", command, {"reason": str(result.get("reason", "lift move rejected"))})
            return
        verified = bool(result.get("physicalCompletionVerified"))
        event = "lift_move_completed" if verified else "lift_move_driver_acknowledged"
        self._publish_event(
            event,
            command,
            {
                "distanceCm": move.distance_cm,
                "result": result,
                "physicalCompletionVerified": verified,
                "warning": (
                    None
                    if verified
                    else "升降台驱动器已接收命令，但没有位置反馈，未确认物理到位"
                ),
            },
        )

    def _home_mechanisms(self, command: GatewayCommand) -> None:
        """Reject recovery until the physical home contract actually exists.

        The current lift can execute only relative pulse moves. Calling the
        arm's GUI zero-pose route would falsely imply that both mechanisms are
        referenced, so this path deliberately sends no motion command.
        """

        try:
            home: MechanismHome = parse_mechanism_home(command.payload)
        except ValueError as error:
            self._publish_event("mechanism_home_failed", command, {"reason": str(error)})
            return
        if (
            self._bookarm_test_command
            or self._active_command
            or self._pending_navigation_command
            or self._manual_control_active()
        ):
            self._publish_event(
                "mechanism_home_failed",
                command,
                {"taskId": home.task_id, "reason": "another robot activity is still active"},
            )
            return
        # Stop any lingering manual velocity before deciding that the base is
        # stationary. A recovery never issues a manual jog or a navigation
        # goal, so this is a one-way zero command rather than a chassis
        # movement request.
        self._publish_zero_manual_velocity()
        if not self._base_is_stationary():
            self._publish_event(
                "mechanism_home_failed",
                command,
                {"taskId": home.task_id, "reason": "base wheel feedback is not fresh and stationary"},
            )
            return
        try:
            health = self._hardware_bookarm_health()
        except BookArmApiError as error:
            self._publish_event("mechanism_home_failed", command, {"taskId": home.task_id, "reason": str(error)})
            return
        self._bookarm_health = health
        capabilities = self._hardware_capabilities()
        if not all(
            capabilities.get(name) is True
            for name in (
                "liftHomeSensor",
                "liftAbsolutePositionFeedback",
                "liftTargetArrivalFeedback",
                "armHomeFeedback",
            )
        ):
            self._publish_event(
                "mechanism_home_failed",
                command,
                {
                    "taskId": home.task_id,
                    "reason": "verified arm or lift home feedback is unavailable; automatic mechanism home is disabled",
                    "armHomeVerified": False,
                    "liftHomeVerified": False,
                    "baseStationaryVerified": True,
                    "bookSecuredAcknowledged": home.book_secured,
                },
            )
            return
        if not getattr(health, "mechanism_home_ready", False):
            self._publish_event(
                "mechanism_home_failed",
                command,
                {
                    "taskId": home.task_id,
                    "reason": "lift home switch or absolute position feedback is unavailable; automatic mechanism home is disabled",
                    "armHomeVerified": False,
                    "liftHomeVerified": False,
                    "baseStationaryVerified": True,
                    "bookSecuredAcknowledged": home.book_secured,
                },
            )
            return
        # Future hardware support must execute and independently verify both
        # home operations here before emitting mechanism_home_succeeded.
        self._publish_event(
            "mechanism_home_failed",
            command,
            {
                "taskId": home.task_id,
                "reason": "verified mechanism home is not implemented for this hardware profile",
                "bookSecuredAcknowledged": home.book_secured,
            },
        )

    def _refresh_bookarm_health(self) -> None:
        self._bookarm_health = self._hardware_bookarm_health()

    def _hardware_bookarm_health(self) -> BookArmHealth:
        """Read through the adapter, retaining safe test/upgrade fallback."""

        hardware = getattr(self, "_hardware", None)
        reader = getattr(hardware, "bookarm_health", None)
        if callable(reader):
            return reader()
        return self._bookarm_client.health()

    def _request_hardware_safe_stop(self, *, emergency: bool = False) -> None:
        """Request the local stop chain without changing a cancellation receipt."""

        hardware = getattr(self, "_hardware", None)
        stopper = getattr(hardware, "emergency_stop" if emergency else "safe_stop", None)
        if not callable(stopper):
            fallback_name = "emergency_stop" if emergency else "safe_stop"
            stopper = getattr(getattr(self, "_bookarm_client", None), fallback_name, None)
        if not callable(stopper):
            return
        try:
            result = stopper()
            if isinstance(result, dict) and not result.get("accepted", False):
                self.get_logger().warn(f"local hardware stop was not accepted: {result.get('reason', 'unknown')}")
        except BookArmApiError as error:
            self.get_logger().warn(f"local hardware stop failed: {error}")

    def _hardware_capabilities(self) -> dict[str, Any]:
        """Return a conservative capability contract during staged upgrades."""

        hardware = getattr(self, "_hardware", None)
        reader = getattr(hardware, "capabilities", None)
        if callable(reader):
            try:
                value = reader()
            except Exception as error:
                self.get_logger().warn(f"hardware capability read failed: {error}")
            else:
                if isinstance(value, dict):
                    return value
        return conservative_hardware_capabilities(
            str(getattr(self, "hardware_profile", DEFAULT_HARDWARE_PROFILE))
        )

    def _send_next_cruise_waypoint(self) -> None:
        command = self._active_command
        route = self._active_cruise_route
        if not command or not route:
            return
        if self._cruise_waypoint_index >= len(route.waypoints):
            self._cruise_waypoint_index = 0
            self._cruise_loop_index += 1
        if self._cruise_loop_index >= route.loop_count:
            self._publish_event("cruise_succeeded", command)
            self._clear_cruise()
            return
        if datetime.now(UTC) >= command.expires_at:
            self._publish_event("cruise_failed", command, {"reason": "cruise command expired"})
            self._clear_cruise()
            return
        waypoint = route.waypoints[self._cruise_waypoint_index]
        goal = NavigateToPose.Goal()
        goal.pose = self._pose_from_goal(
            NavigationGoal(route.map_id, waypoint.annotation_id, waypoint.x, waypoint.y, waypoint.yaw)
        )
        waypoint_index = self._cruise_waypoint_index
        loop_index = self._cruise_loop_index
        future = self._action_client.send_goal_async(goal, feedback_callback=self._navigation_feedback)
        future.add_done_callback(
            lambda result: self._handle_cruise_goal_response(
                command, route, waypoint_index, loop_index, result
            )
        )

    def _handle_cruise_goal_response(
        self,
        command: GatewayCommand,
        route: CruiseRoute,
        waypoint_index: int,
        loop_index: int,
        future,
    ) -> None:
        try:
            goal_handle = future.result()
        except Exception as error:  # pragma: no cover - ROS transport failures are runtime-only.
            if self._active_command is not command:
                return
            self._publish_event("cruise_failed", command, {"reason": str(error)})
            self._clear_cruise()
            return
        # A manual takeover or stop may have arrived while Nav2 was accepting
        # this waypoint. Cancel that late goal rather than allowing it to move
        # the robot after the cruise was cleared.
        if self._active_command is not command or self._active_cruise_route is not route:
            if goal_handle.accepted:
                goal_handle.cancel_goal_async()
            return
        if not goal_handle.accepted:
            self._publish_event("cruise_failed", command, {"reason": "Nav2 rejected cruise waypoint"})
            self._clear_cruise()
            return
        self._active_goal_handle = goal_handle
        waypoint = route.waypoints[waypoint_index]
        self._publish_event(
            "cruise_started",
            command,
            {
                "waypointIndex": waypoint_index + 1,
                "waypointCount": len(route.waypoints),
                "loopIndex": loop_index + 1,
                "loopCount": route.loop_count,
                "annotationId": waypoint.annotation_id,
            },
        )
        goal_handle.get_result_async().add_done_callback(
            lambda result: self._handle_cruise_goal_result(command, goal_handle, result)
        )

    def _handle_cruise_goal_result(self, command: GatewayCommand, goal_handle, future) -> None:
        if self._active_command is not command or self._active_goal_handle is not goal_handle:
            return
        self._active_goal_handle = None
        try:
            result = future.result()
        except Exception as error:  # pragma: no cover - ROS transport failures are runtime-only.
            self._publish_event("cruise_failed", command, {"reason": str(error)})
            self._clear_cruise()
            return
        if result.status == GoalStatus.STATUS_SUCCEEDED:
            self._cruise_waypoint_index += 1
            self._send_next_cruise_waypoint()
            return
        if result.status == GoalStatus.STATUS_CANCELED:
            self._publish_event("cruise_cancelled", command)
        else:
            self._publish_event("cruise_failed", command, {"nav2Status": int(result.status)})
        self._clear_cruise()

    def _clear_cruise(self) -> None:
        self._active_goal_handle = None
        self._active_command = None
        self._active_cruise_route = None
        self._cruise_waypoint_index = 0
        self._cruise_loop_index = 0

    def _pose_from_goal(self, goal: NavigationGoal) -> PoseStamped:
        pose = PoseStamped()
        pose.header.frame_id = "map"
        # Nav2 resolves a zero timestamp against the latest available TF. A
        # wall-clock stamp can become stale while MQTT/ROS queues a command,
        # leading to "extrapolation into the past" and a rejected goal.
        pose.header.stamp.sec = 0
        pose.header.stamp.nanosec = 0
        pose.pose.position.x = goal.x
        pose.pose.position.y = goal.y
        pose.pose.orientation.z = math.sin(goal.yaw / 2.0)
        pose.pose.orientation.w = math.cos(goal.yaw / 2.0)
        return pose

    def _handle_goal_response(self, command: GatewayCommand, goal: NavigationGoal, future) -> None:
        try:
            goal_handle = future.result()
        except Exception as error:  # ROS futures expose transport errors as exceptions.
            if self._pending_navigation_command is not command:
                return
            self._pending_navigation_command = None
            self._publish_event("navigation_failed", command, {"reason": str(error)})
            return
        # The operator can take over before Nav2 accepts an asynchronous goal.
        # Reject the late result locally and cancel it if Nav2 did accept it.
        if self._pending_navigation_command is not command:
            if goal_handle.accepted:
                goal_handle.cancel_goal_async()
            return
        self._pending_navigation_command = None
        if not goal_handle.accepted:
            self._publish_event("command_rejected", command, {"reason": "Nav2 rejected the navigation goal"})
            return
        self._active_command = command
        self._active_goal_handle = goal_handle
        self._publish_event(
            "navigation_started",
            command,
            {"mapId": goal.map_id, "annotationId": goal.annotation_id, "x": goal.x, "y": goal.y, "yaw": goal.yaw},
        )
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(lambda result: self._handle_navigation_result(command, result))

    def _navigation_feedback(self, _feedback) -> None:
        # Nav2 feedback is intentionally not mirrored at high frequency over MQTT.
        return

    def _handle_amcl_pose(self, message: PoseWithCovarianceStamped) -> None:
        if message.header.frame_id != "map":
            return
        covariance = message.pose.covariance
        if (
            covariance[0] > self.max_position_variance
            or covariance[7] > self.max_position_variance
            or covariance[35] > self.max_yaw_variance
        ):
            self._latest_pose_received_at = None
            return
        orientation = message.pose.pose.orientation
        yaw = math.atan2(
            2.0 * (orientation.w * orientation.z + orientation.x * orientation.y),
            1.0 - 2.0 * (orientation.y * orientation.y + orientation.z * orientation.z),
        )
        x = message.pose.pose.position.x
        y = message.pose.pose.position.y
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(yaw)):
            self._latest_pose = None
            self._latest_pose_received_at = None
            return
        if not self._pose_is_within_active_map_bounds(x, y):
            # Do not retain an old in-map sample after AMCL has reported a
            # precise but incompatible coordinate frame or map selection.
            self._latest_pose = None
            self._latest_pose_received_at = None
            self._localization_status = "manual_required"
            self.get_logger().warn("AMCL pose is outside the active map bounds; navigation is blocked")
            return
        self._latest_pose = (x, y, yaw)
        self._latest_pose_received_at = time.monotonic()

    def _handle_localization_status(self, message: String) -> None:
        status = message.data.strip()
        if status in {
            "starting",
            "restoring_saved_pose",
            "global_localizing",
            "manual_initializing",
            "localized",
            "manual_required",
        }:
            self._localization_status = status
        if status == "localized":
            pose = self._latest_pose
            if pose is not None and self._pose_is_within_active_map_bounds(pose[0], pose[1]):
                # The localization manager emits this heartbeat only while
                # AMCL's map->odom transform is fresh — for automatic startup
                # restores as well as operator-confirmed poses. Treat it as
                # the liveness signal for the last verified pose, or a parked
                # robot flips to manual_required seconds after every restore.
                self._latest_pose_received_at = time.monotonic()
        elif status in {"starting", "restoring_saved_pose", "global_localizing", "manual_required"}:
            self._manual_localization_active = False

    def _handle_localization_scan_mode(self, message: String) -> None:
        mode = message.data.strip()
        if mode in {"dual", "single_laser_1", "single_laser_2", "unavailable"}:
            self._localization_scan_mode = mode

    def _handle_chassis_status(self, message: String) -> None:
        """Accept only fresh, complete encoder feedback from the local base driver."""
        try:
            sources = json.loads(message.data)
        except json.JSONDecodeError:
            self._chassis_feedback_healthy = False
            self._chassis_status_received_at = time.monotonic()
            return
        self._chassis_feedback_healthy = (
            isinstance(sources, dict)
            and len(sources) == 4
            and all(source == "feedback" for source in sources.values())
        )
        self._chassis_status_received_at = time.monotonic()

    def _handle_imu_data(self, message: Imu) -> None:
        """Only freshness matters; the message body is consumed downstream."""

        del message
        # monotonic is immune to system clock steps on the vehicle computer.
        self._latest_imu_received_at = time.monotonic()

    def _handle_wheel_speeds(self, message: Float32MultiArray) -> None:
        """Keep only a complete, finite physical wheel-speed feedback sample."""

        if len(message.data) < 4:
            self._latest_wheel_speeds = None
            self._wheel_speeds_received_at = time.monotonic()
            return
        speeds = tuple(float(value) for value in message.data[:4])
        self._latest_wheel_speeds = speeds if all(math.isfinite(value) for value in speeds) else None
        self._wheel_speeds_received_at = time.monotonic()

    def _chassis_ready(self) -> bool:
        return bool(
            self._chassis_feedback_healthy
            and self._chassis_status_received_at is not None
            and time.monotonic() - self._chassis_status_received_at
            <= self.chassis_status_timeout_seconds
        )

    def _imu_ready(self) -> bool:
        """True only while fresh IMU samples keep the odometry yaw reliable."""

        received_at = getattr(self, "_latest_imu_received_at", None)
        return bool(
            received_at is not None
            and time.monotonic() - received_at
            <= float(getattr(self, "imu_stale_timeout_seconds", 2.0))
        )

    def _base_is_stationary(self) -> bool:
        """Require fresh physical wheel feedback below a conservative threshold."""

        return bool(
            self._chassis_ready()
            and self._latest_wheel_speeds is not None
            and self._wheel_speeds_received_at is not None
            and time.monotonic() - self._wheel_speeds_received_at <= self.wheel_speed_timeout_seconds
            and all(abs(speed) <= self.base_stationary_speed_threshold_mps for speed in self._latest_wheel_speeds)
        )

    def _pose_is_within_active_map_bounds(self, x: float, y: float) -> bool:
        """Accept only finite AMCL positions inside the active map extent."""

        min_x = float(getattr(self, "map_min_x", -math.inf))
        max_x = float(getattr(self, "map_max_x", math.inf))
        min_y = float(getattr(self, "map_min_y", -math.inf))
        max_y = float(getattr(self, "map_max_y", math.inf))
        return bool(
            math.isfinite(x)
            and math.isfinite(y)
            and math.isfinite(min_x)
            and math.isfinite(max_x)
            and math.isfinite(min_y)
            and math.isfinite(max_y)
            and min_x <= x <= max_x
            and min_y <= y <= max_y
        )

    def _localization_is_reliable(self) -> bool:
        pose = self._latest_pose
        return bool(
            self._localization_status == "localized"
            and pose is not None
            and self._latest_pose_received_at is not None
            and time.monotonic() - self._latest_pose_received_at <= self.pose_stale_timeout_seconds
            and self._pose_is_within_active_map_bounds(pose[0], pose[1])
        )

    def _publish_telemetry(self) -> None:
        pose_is_fresh = (
            self._latest_pose_received_at is not None
            and time.monotonic() - self._latest_pose_received_at <= self.pose_stale_timeout_seconds
        )
        localization_status = self._localization_status
        pose_is_in_bounds = bool(
            self._latest_pose and self._pose_is_within_active_map_bounds(*self._latest_pose[:2])
        )
        if localization_status == "localized" and (not pose_is_fresh or not pose_is_in_bounds):
            localization_status = "manual_required"
        localization_is_reliable = localization_status == "localized" and pose_is_fresh and pose_is_in_bounds
        registered_maps = getattr(self, "_registered_maps", {})
        allowed_map_ids = getattr(self, "allowed_map_ids", set())
        details: dict[str, Any] = {
            "schemaVersion": _TELEMETRY_SCHEMA_VERSION,
            "bootId": self._telemetry_boot_id,
            "mapId": self.active_map_id,
            "frameId": "map",
            "nav2Ready": self._action_client.server_is_ready(),
            "chassisReady": self._chassis_ready(),
            "imuReady": self._imu_ready(),
            "availableMapIds": sorted(registered_maps) if registered_maps else sorted(allowed_map_ids),
            "localizationAvailable": self._latest_pose is not None and localization_is_reliable,
            "localizationStatus": localization_status,
            "localizationScanMode": self._localization_scan_mode,
            "hardwareCapabilities": self._hardware_capabilities(),
        }
        hardware = getattr(self, "_hardware", None)
        health_reader = getattr(hardware, "health", None)
        if callable(health_reader):
            try:
                health = health_reader()
                if not isinstance(health, dict):
                    raise BookArmApiError("hardware adapter returned an invalid health snapshot")
                details.update(health)
            except BookArmApiError as error:
                details.update({
                    "armReady": False,
                    "rodReady": False,
                    "liftReady": False,
                    "liftAbsolutePositioningReady": False,
                    "mechanismHomeReady": False,
                    "cameraReady": False,
                    "bookArmMessage": str(error)[:240],
                })
        else:
            bookarm_health = getattr(self, "_bookarm_health", None)
            if isinstance(bookarm_health, BookArmHealth):
                details.update(
                    {
                        "armReady": bookarm_health.arm_ready,
                        "armConnected": bookarm_health.arm_connected,
                        "armFeedbackHealthy": bookarm_health.arm_feedback_healthy,
                        "armCanFps": bookarm_health.arm_can_fps,
                        "armFeedbackReason": bookarm_health.arm_feedback_reason,
                        "armEnabled": bookarm_health.arm_enabled,
                        "armState": bookarm_health.arm_state,
                        "liftReady": bookarm_health.lift_ready,
                        "liftAbsolutePositioningReady": False,
                        "mechanismHomeReady": False,
                        "cameraReady": bookarm_health.camera_ready,
                        "rodReady": bookarm_health.rod_ready,
                        "bookArmMessage": bookarm_health.message,
                    }
                )
        # An AMCL global-search candidate can be far from the real robot and
        # carries high covariance. Never render or dispatch it as a live pose.
        if self._latest_pose and pose_is_fresh and localization_status == "localized":
            x, y, yaw = self._latest_pose
            details.update({"x": x, "y": y, "yaw": yaw})
        self._publish_telemetry_frame(details)

    def _publish_telemetry_frame(self, details: dict[str, Any]) -> None:
        """Publish lossy state snapshots separately from durable workflow receipts."""

        if not self._mqtt.is_connected():
            return
        self._telemetry_sequence += 1
        payload = {
            "robotId": self.robot_id,
            "sequence": self._telemetry_sequence,
            "timestamp": datetime.now(UTC).isoformat(),
            **details,
        }
        result = self._mqtt.publish(self.telemetry_topic, json.dumps(payload), qos=0, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            self.get_logger().warn(f"failed to publish telemetry: MQTT result {result.rc}")

    def _handle_navigation_result(self, command: GatewayCommand, future) -> None:
        if self._active_command is not command:
            return
        self._active_goal_handle = None
        self._active_command = None
        try:
            result = future.result()
        except Exception as error:
            self._publish_event("navigation_failed", command, {"reason": str(error)})
            return
        if result.status == GoalStatus.STATUS_SUCCEEDED:
            self._publish_event("navigation_succeeded", command)
        elif result.status == GoalStatus.STATUS_CANCELED:
            self._publish_event("navigation_cancelled", command)
        else:
            self._publish_event("navigation_failed", command, {"nav2Status": int(result.status)})

    def _manual_control_active(self) -> bool:
        """A manual drive lease counts as active until its deadline is enforced to zero."""
        return self._manual_command_deadline is not None

    def _manual_jog(self, command: GatewayCommand) -> None:
        try:
            jog = parse_manual_jog(command.payload)
        except ValueError as error:
            self._publish_event("command_rejected", command, {"reason": str(error)})
            return
        if (
            abs(jog.linear) > _MANUAL_JOG_MAX_LINEAR_SPEED
            or abs(jog.angular) > _MANUAL_JOG_MAX_ANGULAR_SPEED
            or jog.duration_ms > _MANUAL_JOG_MAX_DURATION_MS
        ):
            self._publish_event("command_rejected", command, {"reason": "manual jog exceeds active safety limits"})
            return
        if command.expires_at <= datetime.now(UTC):
            self._publish_event("command_rejected", command, {"reason": "manual jog command has expired"})
            return
        if self._manual_command_deadline is None:
            self._cancel_active_navigation("manual control takeover")
        self._manual_target_linear = jog.linear
        self._manual_target_angular = jog.angular
        active_for_seconds = min(
            jog.duration_ms / 1000.0,
            self.manual_command_timeout_seconds,
        )
        self._manual_command_deadline = time.monotonic() + max(0.0, active_for_seconds)
        self._publish_event("manual_jog_accepted", command, {"durationMs": jog.duration_ms})

    def _manual_drive_tick(self) -> None:
        """20 Hz continuous manual drive: stream the ramped velocity while a
        jog lease is live; cut to zero (no ramp) on watchdog timeout or stale
        chassis feedback, mirroring the mapping agent's jog behaviour."""
        if self._manual_command_deadline is None:
            return
        if not self._chassis_ready() or time.monotonic() >= self._manual_command_deadline:
            self._stop_manual_drive()
            return
        self._manual_output_linear = _ramp_toward(
            self._manual_output_linear, self._manual_target_linear, _MANUAL_DRIVE_STEP_LINEAR_MPS
        )
        self._manual_output_angular = _ramp_toward(
            self._manual_output_angular, self._manual_target_angular, _MANUAL_DRIVE_STEP_ANGULAR_RPS
        )
        velocity = Twist()
        velocity.linear.x = self._manual_output_linear
        velocity.angular.z = self._manual_output_angular
        self._manual_velocity_publisher.publish(velocity)

    def _stop_manual_drive(self) -> None:
        self._manual_target_linear = 0.0
        self._manual_target_angular = 0.0
        self._manual_output_linear = 0.0
        self._manual_output_angular = 0.0
        self._publish_zero_manual_velocity()
        self._manual_command_deadline = None

    def _publish_zero_manual_velocity(self) -> None:
        self._manual_velocity_publisher.publish(Twist())

    def _cancel_navigation(self, command: GatewayCommand) -> None:
        # A cancel carrying a task/command id is targeted: it may only cancel
        # the matching command. Anything else would let a stale cancel kill an
        # unrelated navigation that started after it (supersede race).
        if command.command == "cancel" and self._cancel_target_id(command) is not None:
            self._cancel_targeted(command)
            return
        self._stop_manual_drive()
        if self._bookarm_test_command:
            self._cancel_bookarm_test(command, "operator stopped book-arm maintenance test")
            return
        if getattr(self, "_pending_fine_tune_command", None):
            self._cancel_fine_tune(command, "operator cancelled fine tune")
            return
        self._request_hardware_safe_stop(emergency=command.command == "emergency_stop")
        if command.command == "emergency_stop":
            self._publish_event(
                "emergency_stop_requested",
                command,
                {"warning": "Only the active Nav2 goal was cancelled; use the physical emergency stop for hazards."},
            )
        if not self._cancel_active_navigation("operator cancelled navigation"):
            self._publish_event("command_completed", command, {"result": "no active Nav2 goal"})
            return
        self._publish_event("navigation_cancelling", command)

    @staticmethod
    def _cancel_target_id(command: GatewayCommand) -> str | None:
        payload = command.payload if isinstance(command.payload, dict) else {}
        raw = payload.get("commandId") or payload.get("taskId")
        if not isinstance(raw, str) or not raw:
            return None
        if raw.startswith("TSK-"):
            return raw.replace("TSK-", "CMD-", 1)
        return raw

    def _cancel_targeted(self, command: GatewayCommand) -> None:
        target_id = self._cancel_target_id(command)
        pending_fine_tune = getattr(self, "_pending_fine_tune_command", None)
        if pending_fine_tune is not None and pending_fine_tune.command_id == target_id:
            self._cancel_fine_tune(command, "operator cancelled fine tune")
            return
        bookarm_test = getattr(self, "_bookarm_test_command", None)
        if bookarm_test is not None and bookarm_test.command_id == target_id:
            self._cancel_bookarm_test(command, "operator stopped book-arm maintenance test")
            return
        active = self._active_command
        pending = self._pending_navigation_command
        if (active is not None and active.command_id == target_id) or (
            pending is not None and pending.command_id == target_id
        ):
            self._request_hardware_safe_stop(emergency=False)
            if self._cancel_active_navigation("operator cancelled navigation"):
                self._publish_event("navigation_cancelling", command)
            return
        # No matching command: leave whatever is currently active untouched.
        self._publish_event(
            "command_completed",
            command,
            {"result": "no matching active command", "targetCommandId": target_id},
        )

    def _cancel_active_navigation(self, reason: str) -> bool:
        """Cancel the active or still-pending Nav2 work without stale callbacks reviving it."""
        goal_handle = self._active_goal_handle
        active_command = self._active_command
        pending_command = self._pending_navigation_command
        is_cruise = self._active_cruise_route is not None

        self._active_goal_handle = None
        self._active_command = None
        self._active_cruise_route = None
        self._cruise_waypoint_index = 0
        self._cruise_loop_index = 0
        self._pending_navigation_command = None

        if active_command:
            self._publish_event(
                "cruise_cancelled" if is_cruise else "navigation_cancelled",
                active_command,
                {"reason": reason},
            )
        elif pending_command:
            self._publish_event("navigation_cancelled", pending_command, {"reason": reason})

        if goal_handle:
            future = goal_handle.cancel_goal_async()
            future.add_done_callback(lambda _result: None)
        return bool(goal_handle or active_command or pending_command)

    def _publish_event(
        self,
        event: str,
        command: GatewayCommand | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        # Timers may fire before the asynchronous MQTT connection completes.
        if not self._mqtt.is_connected():
            return
        payload = {
            "eventId": f"EVT-{uuid4().hex[:12].upper()}",
            "event": event,
            "robotId": self.robot_id,
            "commandId": command.command_id if command else None,
            "timestamp": datetime.now(UTC).isoformat(),
            "details": details or {},
        }
        result = self._mqtt.publish(self.event_topic, json.dumps(payload), qos=1, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            self.get_logger().warn(f"failed to publish {event}: MQTT result {result.rc}")

    def destroy_node(self) -> bool:
        # A process restart must stop any still-active manual drive stream.
        if getattr(self, "_manual_command_deadline", None) is not None:
            self._stop_manual_drive()
        if getattr(self, "_bookarm_test_command", None):
            self._cancel_bookarm_test(self._bookarm_test_command, "robot runtime is stopping")
        pending_fine_tune = getattr(self, "_pending_fine_tune_command", None)
        if pending_fine_tune is not None:
            self._pending_fine_tune_command = None
            self._abort_book_shelf_fine_tune()
            self._publish_event(
                "fine_tune_cancelled", pending_fine_tune, {"reason": "robot runtime is stopping"}
            )
        self._mqtt.loop_stop()
        self._mqtt.disconnect()
        return super().destroy_node()


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = EdgeGatewayNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
