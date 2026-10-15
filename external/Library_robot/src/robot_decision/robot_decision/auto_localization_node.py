#!/usr/bin/env python3

"""Restore a verified AMCL pose or safely request global localization at startup."""

from __future__ import annotations

import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from lifecycle_msgs.srv import GetState
from nav2_msgs.srv import SetInitialPose
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from std_srvs.srv import Empty, Trigger
from tf2_msgs.msg import TFMessage

from robot_decision.map_registry import load_map_registry
from robot_decision.pose_utils import make_initial_pose
from robot_decision.scan_pose_matcher import best_pose_in_window, best_pose_on_map, normalize_angle


class AutoLocalizationNode(Node):
    """Keep a per-map pose cache and expose a conservative localization state."""

    def __init__(self) -> None:
        super().__init__("auto_localization_node")
        self._declare_parameters()
        self.active_map_id = self._required_string("active_map_id")
        self.state_file = Path(str(self.get_parameter("state_file").value)).expanduser()
        configured_registry_file = str(self.get_parameter("map_registry_file").value).strip()
        self.map_registry_file = Path(configured_registry_file).expanduser() if configured_registry_file else None
        self.map_min_x = float(self.get_parameter("map_min_x").value)
        self.map_max_x = float(self.get_parameter("map_max_x").value)
        self.map_min_y = float(self.get_parameter("map_min_y").value)
        self.map_max_y = float(self.get_parameter("map_max_y").value)
        self.pose_topic = self._required_string("pose_topic")
        self.initial_pose_topic = self._required_string("initial_pose_topic")
        self.status_topic = self._required_string("status_topic")
        self.tf_topic = self._required_string("tf_topic")
        self.map_switch_topic = self._required_string("map_switch_topic")
        self.set_initial_pose_service = self._required_string("set_initial_pose_service")
        self.global_localization_service = self._required_string("global_localization_service")
        self.nomotion_update_service = self._required_string("nomotion_update_service")
        self.amcl_get_state_service = str(self.get_parameter("amcl_get_state_service").value)
        self.startup_amcl_timeout_seconds = float(self.get_parameter("startup_amcl_timeout_seconds").value)
        self.max_saved_pose_age_seconds = float(self.get_parameter("max_saved_pose_age_seconds").value)
        self.initialization_timeout_seconds = float(self.get_parameter("initialization_timeout_seconds").value)
        self.min_stable_samples = int(self.get_parameter("min_stable_samples").value)
        self.max_position_variance = float(self.get_parameter("max_position_variance").value)
        self.max_yaw_variance = float(self.get_parameter("max_yaw_variance").value)
        self.initialization_max_position_variance = float(
            self.get_parameter("initialization_max_position_variance").value
        )
        self.initialization_max_yaw_variance = float(
            self.get_parameter("initialization_max_yaw_variance").value
        )
        self.pose_stale_timeout_seconds = float(self.get_parameter("pose_stale_timeout_seconds").value)
        self.nomotion_update_period_seconds = float(
            self.get_parameter("nomotion_update_period_seconds").value
        )
        self.smart_seed_enabled = bool(self.get_parameter("smart_seed_enabled").value)
        self.smart_seed_min_score = float(self.get_parameter("smart_seed_min_score").value)
        self.smart_seed_window_m = float(self.get_parameter("smart_seed_window_m").value)
        self.smart_seed_step_m = float(self.get_parameter("smart_seed_step_m").value)
        self.smart_seed_yaw_window_deg = float(self.get_parameter("smart_seed_yaw_window_deg").value)
        self.smart_seed_yaw_step_deg = float(self.get_parameter("smart_seed_yaw_step_deg").value)
        self.smart_seed_fine_window_m = float(self.get_parameter("smart_seed_fine_window_m").value)
        self.smart_seed_fine_step_m = float(self.get_parameter("smart_seed_fine_step_m").value)
        self.smart_seed_fine_yaw_deg = float(self.get_parameter("smart_seed_fine_yaw_deg").value)
        self.smart_seed_fine_yaw_step_deg = float(self.get_parameter("smart_seed_fine_yaw_step_deg").value)
        self.auto_localize_service = self._required_string("auto_localize_service")
        self.auto_localize_coarse_step_m = float(self.get_parameter("auto_localize_coarse_step_m").value)
        self.auto_localize_coarse_yaw_step_deg = float(self.get_parameter("auto_localize_coarse_yaw_step_deg").value)
        self.auto_localize_top_k = int(self.get_parameter("auto_localize_top_k").value)
        self.auto_localize_time_budget_s = float(self.get_parameter("auto_localize_time_budget_s").value)
        self.scan_topic = self._required_string("scan_topic")
        self.map_topic = self._required_string("map_topic")
        if self.max_saved_pose_age_seconds <= 0 or self.initialization_timeout_seconds <= 0:
            raise RuntimeError("localization timeout parameters must be greater than zero")
        if self.startup_amcl_timeout_seconds <= 0:
            raise RuntimeError("startup AMCL timeout must be greater than zero")
        if (
            self.min_stable_samples < 1
            or self.max_position_variance <= 0
            or self.max_yaw_variance <= 0
            or self.initialization_max_position_variance <= 0
            or self.initialization_max_yaw_variance <= 0
            or self.pose_stale_timeout_seconds <= 0
            or self.nomotion_update_period_seconds <= 0
        ):
            raise RuntimeError("localization stability parameters are invalid")
        if self.smart_seed_enabled and (
            self.smart_seed_window_m <= 0
            or self.smart_seed_step_m <= 0
            or self.smart_seed_yaw_window_deg <= 0
            or self.smart_seed_yaw_step_deg <= 0
            or self.smart_seed_fine_window_m <= 0
            or self.smart_seed_fine_step_m <= 0
            or self.smart_seed_fine_yaw_deg <= 0
            or self.smart_seed_fine_yaw_step_deg <= 0
            or self.smart_seed_min_score < 0
        ):
            raise RuntimeError("smart seed search parameters are invalid")
        if (
            self.auto_localize_coarse_step_m <= 0
            or not 0 < self.auto_localize_coarse_yaw_step_deg <= 180.0
            or self.auto_localize_top_k < 1
            or self.auto_localize_time_budget_s <= 0
        ):
            raise RuntimeError("auto localize search parameters are invalid")
        self._map_bounds_valid = self._validate_map_bounds()
        if not self._map_bounds_valid:
            raise RuntimeError("active map bounds are invalid")
        if self.map_registry_file is not None:
            self._map_bounds_valid = self._load_registered_map_bounds(self.active_map_id)
            if not self._map_bounds_valid:
                raise RuntimeError("active map is not registered with valid bounds")

        status_qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self.status_publisher = self.create_publisher(String, self.status_topic, status_qos)
        self.initial_pose_publisher = self.create_publisher(
            PoseWithCovarianceStamped, self.initial_pose_topic, 10
        )
        self.create_subscription(PoseWithCovarianceStamped, self.pose_topic, self._handle_amcl_pose, 10)
        self.create_subscription(PoseWithCovarianceStamped, self.initial_pose_topic, self._handle_manual_initial_pose, 10)
        self.create_subscription(String, self.map_switch_topic, self._handle_map_switch, 10)
        self.create_subscription(
            TFMessage,
            self.tf_topic,
            self._handle_tf,
            QoSProfile(depth=10, durability=DurabilityPolicy.VOLATILE, reliability=ReliabilityPolicy.BEST_EFFORT),
        )
        map_qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self.create_subscription(OccupancyGrid, self.map_topic, self._handle_map, map_qos)
        self.create_subscription(LaserScan, self.scan_topic, self._handle_scan, qos_profile_sensor_data)
        self.initial_pose_client = self.create_client(SetInitialPose, self.set_initial_pose_service)
        self.global_localization_client = self.create_client(Empty, self.global_localization_service)
        self.nomotion_update_client = self.create_client(Empty, self.nomotion_update_service)
        self._amcl_state_client = self.create_client(GetState, self.amcl_get_state_service)
        self._auto_localize_service = self.create_service(
            Trigger, self.auto_localize_service, self._handle_auto_localize
        )

        self._status = "starting"
        self._stable_samples = 0
        self._manual_initial_pose: tuple[float, float, float] | None = None
        self._phase_started_at = self.get_clock().now()
        self._initial_request_sent = False
        self._global_request_sent = False
        self._last_stable_pose_at: float | None = None
        self._last_pose_saved_at = 0.0
        self._initial_pose_seeded_at_ns = 0
        self._last_map_to_odom_transform_at: float | None = None
        self._manual_pose_accepted = False
        self._map_bounds_rejection_logged = False
        self._nomotion_request_pending = False
        self._map_switch_timer = None
        self._map_message: OccupancyGrid | None = None
        self._map_received_at: float | None = None
        # AMCL keeps its old particle filter when the map reloads underneath
        # it, so a confident pose right after a map switch is the OLD map's
        # pose.  No AMCL pose may be trusted until this node re-seeds the
        # filter (global spray or a verified seed) for the active map.
        self._pose_seeded_for_map = True
        self._scan_message: LaserScan | None = None
        self._scan_received_at: float | None = None
        self._amcl_lifecycle_state: str | None = None
        self._amcl_state_query_pending = False
        self._auto_localize_running = False
        self._recent_republished_initial_poses: list[tuple[float, float, float, float]] = []
        self._startup_timer = self.create_timer(0.5, self._begin_startup_localization)
        self._watchdog_timer = self.create_timer(1.0, self._watchdog)
        self._amcl_state_poll_timer = self.create_timer(1.0, self._poll_amcl_lifecycle_state)
        self._nomotion_update_timer = self.create_timer(
            self.nomotion_update_period_seconds, self._request_nomotion_update
        )
        self._publish_status()

    def _declare_parameters(self) -> None:
        self.declare_parameter("active_map_id", "")
        self.declare_parameter("state_file", "~/.local/state/lr-robot/last_localization.json")
        # Bounds come from the approved local map registry in production. The
        # explicit values keep single-map development deployments deterministic.
        self.declare_parameter("map_registry_file", "")
        self.declare_parameter("map_min_x", 0.0)
        self.declare_parameter("map_max_x", 0.0)
        self.declare_parameter("map_min_y", 0.0)
        self.declare_parameter("map_max_y", 0.0)
        self.declare_parameter("pose_topic", "/amcl_pose")
        self.declare_parameter("initial_pose_topic", "/initialpose")
        self.declare_parameter("status_topic", "/localization/status")
        self.declare_parameter("tf_topic", "/tf")
        self.declare_parameter("map_switch_topic", "/localization/map_switch")
        self.declare_parameter("set_initial_pose_service", "/set_initial_pose")
        self.declare_parameter("global_localization_service", "/reinitialize_global_localization")
        self.declare_parameter("nomotion_update_service", "/request_nomotion_update")
        self.declare_parameter("amcl_get_state_service", "/amcl/get_state")
        # Bounds for waiting on AMCL activation + the current-session map
        # before the startup loop falls back to operator confirmation.
        self.declare_parameter("startup_amcl_timeout_seconds", 120.0)
        self.declare_parameter("max_saved_pose_age_seconds", 604800.0)
        self.declare_parameter("initialization_timeout_seconds", 30.0)
        self.declare_parameter("min_stable_samples", 3)
        self.declare_parameter("max_position_variance", 0.5)
        self.declare_parameter("max_yaw_variance", 0.35)
        self.declare_parameter("initialization_max_position_variance", 0.04)
        self.declare_parameter("initialization_max_yaw_variance", 0.02)
        self.declare_parameter("pose_stale_timeout_seconds", 5.0)
        self.declare_parameter("nomotion_update_period_seconds", 1.0)
        # Smart seed refinement: grid-match the latest scan near the
        # operator's clicked pose and seed AMCL with the best match.
        self.declare_parameter("smart_seed_enabled", True)
        self.declare_parameter("smart_seed_min_score", 0.25)
        self.declare_parameter("smart_seed_window_m", 1.5)
        self.declare_parameter("smart_seed_step_m", 0.05)
        self.declare_parameter("smart_seed_yaw_window_deg", 60.0)
        self.declare_parameter("smart_seed_yaw_step_deg", 5.0)
        self.declare_parameter("smart_seed_fine_window_m", 0.1)
        self.declare_parameter("smart_seed_fine_step_m", 0.02)
        self.declare_parameter("smart_seed_fine_yaw_deg", 6.0)
        self.declare_parameter("smart_seed_fine_yaw_step_deg", 2.0)
        # One-shot automatic localization: match the latest scan against all
        # free cells of the map and seed AMCL with the best match, or fall
        # back to AMCL global localization when the match is not trustworthy.
        self.declare_parameter("auto_localize_service", "/localization/auto_localize")
        self.declare_parameter("auto_localize_coarse_step_m", 0.1)
        self.declare_parameter("auto_localize_coarse_yaw_step_deg", 45.0)
        self.declare_parameter("auto_localize_top_k", 5)
        self.declare_parameter("auto_localize_time_budget_s", 20.0)
        self.declare_parameter("scan_topic", "/scan")
        self.declare_parameter("map_topic", "/map")

    def _required_string(self, name: str) -> str:
        value = str(self.get_parameter(name).value).strip()
        if not value:
            raise RuntimeError(f"{name} must be configured")
        return value

    def _handle_map_switch(self, message: String) -> None:
        """Forget the old map pose after the local Gateway has loaded a new map."""

        map_id = message.data.strip()
        if not map_id.startswith("MAP-") or map_id == self.active_map_id:
            return
        self.active_map_id = map_id
        self._map_bounds_valid = self._load_registered_map_bounds(map_id)
        self._stable_samples = 0
        self._manual_initial_pose = None
        self._manual_pose_accepted = False
        self._last_stable_pose_at = None
        self._last_map_to_odom_transform_at = None
        self._map_bounds_rejection_logged = False
        self._global_request_sent = False
        self._pose_seeded_for_map = False
        # The Gateway loads the new map (map_server republishes /map) BEFORE it
        # publishes the switch notification, so the fresh map is usually
        # already in hand.  Discard the cached map only when it predates the
        # switch; otherwise pose requests would wait for a re-publication that
        # never comes (observed on LR-01, 2026-09-03: 120 s startup timeout,
        # then the stale old-map pose was wrongly accepted as localized).
        map_age = None if self._map_received_at is None else time.monotonic() - self._map_received_at
        if map_age is None or map_age > 5.0:
            self._map_message = None
        self._set_status("starting" if self._map_bounds_valid else "manual_required")
        if not self._map_bounds_valid:
            self.get_logger().error(
                f"map switched to {map_id}, but its registered bounds are unavailable; localization is blocked"
            )
            return
        if self._map_switch_timer is not None:
            self._map_switch_timer.cancel()
        self._map_switch_timer = self.create_timer(0.5, self._begin_map_switch_localization)
        self.get_logger().info(f"map switched to {map_id}; discarding the old localization pose")

    def _begin_map_switch_localization(self) -> None:
        if self._status != "starting":
            self._map_switch_timer.cancel()
            return
        if self._request_global_localization():
            self._map_switch_timer.cancel()

    def _begin_startup_localization(self) -> None:
        if self._status != "starting":
            self._startup_timer.cancel()
            return
        if not self._amcl_ready_for_pose_requests():
            return
        if not self.initial_pose_client.wait_for_service(timeout_sec=0.1):
            return
        saved_pose = self._read_saved_pose()
        if saved_pose:
            self._set_status("restoring_saved_pose")
            request = SetInitialPose.Request()
            request.pose = make_initial_pose(self, "map", *saved_pose)
            self.initial_pose_client.call_async(request).add_done_callback(self._handle_saved_pose_response)
            self.get_logger().info("restoring the last verified AMCL pose")
        else:
            if not self._request_global_localization():
                return
        self._startup_timer.cancel()

    def _handle_saved_pose_response(self, future) -> None:
        try:
            future.result()
        except Exception as error:  # pragma: no cover - ROS transport failures are runtime-only.
            self.get_logger().error(f"saved pose recovery failed: {error}")
            self._global_request_sent = False
            if not self._request_global_localization():
                # AMCL is no longer ready (e.g. it crashed); re-enter the
                # gated startup loop instead of firing a one-shot retry.
                self._set_status("starting")
                self._startup_timer.reset()

    def _request_global_localization(self) -> bool:
        if self._global_request_sent:
            return True
        if not self._amcl_ready_for_pose_requests():
            return False
        if not self.global_localization_client.wait_for_service(timeout_sec=0.1):
            self.get_logger().warn("waiting for AMCL global localization service")
            return False
        self._set_status("global_localizing")
        self.global_localization_client.call_async(Empty.Request()).add_done_callback(self._handle_global_localization_response)
        self._global_request_sent = True
        # The uniform spray reinitializes AMCL's filter, so any pose it
        # reports afterwards belongs to the active map rather than being a
        # carry-over from the previous one.
        self._pose_seeded_for_map = True
        self.get_logger().info("no verified pose is available; requested AMCL global localization")
        return True

    def _handle_global_localization_response(self, future) -> None:
        try:
            future.result()
        except Exception as error:  # pragma: no cover - ROS transport failures are runtime-only.
            self.get_logger().error(f"global localization request failed: {error}")
            self._set_status("manual_required")

    def _poll_amcl_lifecycle_state(self) -> None:
        """Keep AMCL's lifecycle state fresh without blocking the executor."""

        if self._amcl_state_query_pending:
            return
        if not self._amcl_state_client.wait_for_service(timeout_sec=0.0):
            self._amcl_lifecycle_state = None
            return
        self._amcl_state_query_pending = True
        future = self._amcl_state_client.call_async(GetState.Request())
        future.add_done_callback(self._handle_amcl_lifecycle_state_response)

    def _handle_amcl_lifecycle_state_response(self, future) -> None:
        self._amcl_state_query_pending = False
        try:
            response = future.result()
        except Exception as error:  # pragma: no cover - ROS transport failures are runtime-only.
            self.get_logger().warn(f"AMCL lifecycle state query failed: {error}")
            self._amcl_lifecycle_state = None
            return
        self._amcl_lifecycle_state = response.current_state.label

    def _amcl_ready_for_pose_requests(self) -> bool:
        # AMCL already serves /set_initial_pose and
        # /reinitialize_global_localization while only configured — before
        # activation and before it holds a map. Pose requests in that window
        # crash AMCL on a null-map dereference (SIGSEGV observed on LR-01,
        # 2026-08-26). Proceed only once AMCL is active and the map of the
        # current session has arrived.
        return getattr(self, "_map_message", None) is not None and getattr(self, "_amcl_lifecycle_state", None) == "active"

    def _handle_manual_initial_pose(self, message: PoseWithCovarianceStamped) -> None:
        if message.header.frame_id != "map":
            return
        x = message.pose.pose.position.x
        y = message.pose.pose.position.y
        orientation = message.pose.pose.orientation
        yaw = math.atan2(
            2.0 * (orientation.w * orientation.z + orientation.x * orientation.y),
            1.0 - 2.0 * (orientation.y * orientation.y + orientation.z * orientation.z),
        )
        if not all(math.isfinite(value) for value in (x, y, yaw)):
            self.get_logger().warn("ignoring manual initial pose with invalid coordinates")
            return
        if not self._pose_is_within_active_map_bounds(x, y):
            self._reject_out_of_bounds_pose("manual initial pose")
            return
        # Ignore our own seeds, including startup restores. A new operator
        # correction must still be checked when the previous pose was localized.
        if self._is_republished_refined_pose(x, y, yaw):
            return
        self._set_status("manual_initializing")
        self._pose_seeded_for_map = False
        self._stable_samples = 0
        self._manual_initial_pose = None
        refined = self._refine_manual_seed(x, y, yaw)
        if refined is None:
            # AMCL also subscribes to the raw seed. Do not let its low initial
            # covariance make an unverified click look localized again.
            self._set_status("manual_required")
            return
        x, y, yaw = refined
        self._stable_samples = 0
        self._manual_initial_pose = (x, y, yaw)
        self._manual_pose_accepted = False
        # The web gateway already forwarded this seed to AMCL, so the filter
        # now belongs to the active map and its poses may be evaluated again.
        self._pose_seeded_for_map = True
        self._set_status("manual_initializing")
        self.get_logger().info("received a manually confirmed initial pose")
        # Only fresh, stable AMCL observations may finish initialization.
        # A timer firing after publication is not evidence of convergence.

    def _handle_map(self, message: OccupancyGrid) -> None:
        """Keep only the newest static map for manual seed refinement."""

        self._map_message = message
        self._map_received_at = time.monotonic()

    def _handle_scan(self, message: LaserScan) -> None:
        """Keep only the newest scan for manual seed refinement."""

        self._scan_message = message
        self._scan_received_at = time.monotonic()

    def _is_republished_refined_pose(self, x: float, y: float, yaw: float) -> bool:
        """Recognize our own refined-pose republications so they are not refined again."""

        now = time.monotonic()
        recent = [
            entry for entry in self._recent_republished_initial_poses if now - entry[3] <= 5.0
        ]
        self._recent_republished_initial_poses = recent
        return any(
            abs(entry[0] - x) < 1e-9
            and abs(entry[1] - y) < 1e-9
            and abs(normalize_angle(entry[2] - yaw)) < 1e-9
            for entry in recent
        )

    def _refine_manual_seed(self, x: float, y: float, yaw: float) -> tuple[float, float, float] | None:
        """Grid-match the latest scan near the operator seed before seeding AMCL.

        Returns the refined pose, or None when the match is not trustworthy
        enough to accept the seed. An operator click that the laser scan
        cannot confirm must be rejected, not silently seeded into AMCL.
        """

        if not self.smart_seed_enabled:
            # Verification is explicitly disabled by configuration; the raw
            # seed passes through unverified by operator choice.
            return x, y, yaw
        map_message = self._map_message
        scan_message = self._scan_message
        scan_age_seconds = (
            None if self._scan_received_at is None else time.monotonic() - self._scan_received_at
        )
        if map_message is None or scan_message is None or scan_age_seconds is None or scan_age_seconds > 2.0:
            self.get_logger().warn(
                "cannot verify the manual seed (no map or no fresh scan); rejecting it, please retry"
            )
            return None
        started = time.monotonic()
        try:
            info = map_message.info
            grid = np.asarray(map_message.data, dtype=np.int16).reshape((info.height, info.width))
            refined_x, refined_y, refined_yaw, ratio = best_pose_in_window(
                grid,
                float(info.resolution),
                float(info.origin.position.x),
                float(info.origin.position.y),
                scan_message.ranges,
                float(scan_message.angle_min),
                float(scan_message.angle_increment),
                x,
                y,
                yaw,
                window_m=self.smart_seed_window_m,
                step_m=self.smart_seed_step_m,
                yaw_window=math.radians(self.smart_seed_yaw_window_deg),
                yaw_step=math.radians(self.smart_seed_yaw_step_deg),
                fine_window_m=self.smart_seed_fine_window_m,
                fine_step_m=self.smart_seed_fine_step_m,
                fine_yaw_window=math.radians(self.smart_seed_fine_yaw_deg),
                fine_yaw_step=math.radians(self.smart_seed_fine_yaw_step_deg),
                time_budget_seconds=8.0,
            )
        except Exception as error:  # A matcher failure must not seed an unverified pose either.
            self.get_logger().warn(f"smart seed refinement failed: {error}; rejecting the manual seed")
            return None
        elapsed_seconds = time.monotonic() - started
        if ratio < self.smart_seed_min_score:
            self.get_logger().warn(
                f"manual seed rejected: laser scan does not match the map near "
                f"({x:.3f}, {y:.3f}, {math.degrees(yaw):.1f} deg) "
                f"(hit ratio {ratio:.2f} < {self.smart_seed_min_score:.2f})"
            )
            return None
        if not self._pose_is_within_active_map_bounds(refined_x, refined_y):
            self.get_logger().warn(
                f"smart seed match (ratio {ratio:.2f}) lies outside the active map bounds; "
                "rejecting the manual seed"
            )
            return None
        self.get_logger().info(
            f"smart seed refined ({x:.3f}, {y:.3f}, {math.degrees(yaw):.1f} deg) -> "
            f"({refined_x:.3f}, {refined_y:.3f}, {math.degrees(refined_yaw):.1f} deg); "
            f"ratio {ratio:.2f} in {elapsed_seconds:.2f}s"
        )
        # AMCL already received the raw seed from the web gateway, so republish
        # the refined pose as the seed it should actually converge to.  The
        # marker keeps our own republication from being refined once more.
        self._recent_republished_initial_poses.append((refined_x, refined_y, refined_yaw, time.monotonic()))
        self._publish_initial_pose(refined_x, refined_y, 0.0, refined_yaw)
        return refined_x, refined_y, refined_yaw

    def _handle_auto_localize(
        self, _request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        """Run one motionless whole-map scan match; never create any motion."""

        if self._auto_localize_running:
            response.success = False
            response.message = "自动定位正在执行，请等待本次完成后再触发"
            return response
        self._auto_localize_running = True
        try:
            return self._run_auto_localize(response)
        finally:
            self._auto_localize_running = False

    def _run_auto_localize(self, response: Trigger.Response) -> Trigger.Response:
        map_message = self._map_message
        scan_message = self._scan_message
        scan_age_seconds = (
            None if self._scan_received_at is None else time.monotonic() - self._scan_received_at
        )
        if map_message is None:
            response.success = False
            response.message = "自动定位失败：尚未收到地图，无法全图匹配"
            return response
        if scan_message is None or scan_age_seconds is None or scan_age_seconds > 2.0:
            response.success = False
            response.message = "自动定位失败：激光扫描不可用或超过 2 秒未更新"
            return response
        started = time.monotonic()
        try:
            info = map_message.info
            grid = np.asarray(map_message.data, dtype=np.int16).reshape((info.height, info.width))
            x, y, yaw, ratio = best_pose_on_map(
                grid,
                float(info.resolution),
                float(info.origin.position.x),
                float(info.origin.position.y),
                scan_message.ranges,
                float(scan_message.angle_min),
                float(scan_message.angle_increment),
                coarse_step_m=self.auto_localize_coarse_step_m,
                coarse_yaw_step=math.radians(self.auto_localize_coarse_yaw_step_deg),
                top_k=self.auto_localize_top_k,
                time_budget_seconds=self.auto_localize_time_budget_s,
            )
        except Exception as error:  # A matcher failure must never take localization down.
            self.get_logger().error(f"auto localize matching failed: {error}")
            response.success = False
            response.message = f"自动定位失败：匹配计算异常（{error}）"
            return response
        elapsed_seconds = time.monotonic() - started
        match_in_bounds = self._pose_is_within_active_map_bounds(x, y)
        if ratio >= self.smart_seed_min_score and not match_in_bounds:
            self.get_logger().warn(
                f"auto localize match (ratio {ratio:.2f}) lies outside the active map bounds"
            )
        if ratio < self.smart_seed_min_score or not match_in_bounds:
            reason = (
                f"全图匹配不可信（命中率 {ratio:.2f}）"
                if ratio < self.smart_seed_min_score
                else f"全图匹配结果超出当前地图边界（命中率 {ratio:.2f}）"
            )
            self._global_request_sent = False
            sprayed = self._request_global_localization()
            response.success = False
            if sprayed:
                response.message = f"{reason}，已撒粒子等待移动收敛"
            else:
                response.message = f"{reason}，且全局定位服务不可用，请手动指定初始位姿"
            return response
        self.get_logger().info(
            f"auto localize matched ({x:.3f}, {y:.3f}, {math.degrees(yaw):.1f} deg); "
            f"ratio {ratio:.2f} in {elapsed_seconds:.2f}s"
        )
        # Same seeding path as a refined manual click: republish the pose so
        # AMCL converges to it, then reuse the manual-pose acceptance flow.
        # The marker keeps our own republication from being refined once more.
        self._recent_republished_initial_poses.append((x, y, yaw, time.monotonic()))
        self._publish_initial_pose(x, y, 0.0, yaw)
        self._pose_seeded_for_map = True
        self._stable_samples = 0
        self._manual_initial_pose = (x, y, yaw)
        self._manual_pose_accepted = False
        self._set_status("manual_initializing")
        response.success = True
        response.message = (
            f"激光匹配完成，等待 AMCL 收敛：x={x:.2f} m, y={y:.2f} m, yaw={math.degrees(yaw):.1f} deg, "
            f"命中率 {ratio:.2f}，耗时 {elapsed_seconds:.1f} s"
            f"|pose={x:.6f},{y:.6f},{yaw:.6f}"
        )
        return response

    def _handle_tf(self, message: TFMessage) -> None:
        """Track AMCL's dynamic map-to-odom transform while the base is still."""

        for transform in message.transforms:
            if (
                transform.header.frame_id.lstrip("/") == "map"
                and transform.child_frame_id.lstrip("/") == "odom"
            ):
                self._last_map_to_odom_transform_at = time.monotonic()
                return

    def _handle_amcl_pose(self, message: PoseWithCovarianceStamped) -> None:
        if message.header.frame_id != "map":
            return
        stamp_ns = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec
        if stamp_ns < getattr(self, "_initial_pose_seeded_at_ns", 0):
            return
        if not self._pose_seeded_for_map:
            # After a map switch AMCL still reports the old map's pose with its
            # old (tight) covariance.  Trusting it marked the robot localized
            # at a wrong position on LR-01 (2026-09-03).  Ignore every pose
            # until the filter is re-seeded for the active map.
            return
        if self._status not in {
            "restoring_saved_pose",
            "global_localizing",
            "manual_initializing",
            # Keep evaluating AMCL after the initial search deadline. The UI
            # can request a manual pose meanwhile, but a later stable laser
            # match must restore autonomous realtime localization by itself.
            "manual_required",
            "localized",
        }:
            return
        x = message.pose.pose.position.x
        y = message.pose.pose.position.y
        orientation = message.pose.pose.orientation
        yaw = math.atan2(
            2.0 * (orientation.w * orientation.z + orientation.x * orientation.y),
            1.0 - 2.0 * (orientation.y * orientation.y + orientation.z * orientation.z),
        )
        covariance = message.pose.covariance
        position_limit = self.max_position_variance
        yaw_limit = self.max_yaw_variance
        if self._status != "localized":
            position_limit = min(position_limit, getattr(self, "initialization_max_position_variance", position_limit))
            yaw_limit = min(yaw_limit, getattr(self, "initialization_max_yaw_variance", yaw_limit))
        stable = (
            math.isfinite(x)
            and math.isfinite(y)
            and math.isfinite(yaw)
            and self._pose_is_within_active_map_bounds(x, y)
            and covariance[0] <= position_limit
            and covariance[7] <= position_limit
            and covariance[35] <= yaw_limit
        )
        self._stable_samples = self._stable_samples + 1 if stable else 0
        if self._status == "localized":
            if stable:
                self._last_stable_pose_at = time.monotonic()
                if self._last_stable_pose_at - getattr(self, "_last_pose_saved_at", 0.0) >= 1.0:
                    self._save_pose(x, y, yaw)
            else:
                self._set_status("manual_required")
                if not self._pose_is_within_active_map_bounds(x, y):
                    self._reject_out_of_bounds_pose("AMCL pose")
                else:
                    self.get_logger().warn("AMCL covariance is no longer reliable; navigation is blocked")
            return
        if self._stable_samples < self.min_stable_samples:
            if not self._pose_is_within_active_map_bounds(x, y):
                self._reject_out_of_bounds_pose("AMCL pose")
            return
        if self._mark_localized(x, y, yaw):
            self.get_logger().info("AMCL localization is stable; navigation can start")

    def _mark_localized(self, x: float, y: float, yaw: float) -> bool:
        if not self._pose_is_within_active_map_bounds(x, y):
            self._reject_out_of_bounds_pose("localization candidate")
            return False
        self._save_pose(x, y, yaw)
        # Navigation listens to the localization status. Republishing a pose
        # here resets AMCL's converged filter to a wide initial distribution.
        self._set_status("localized")
        self._last_stable_pose_at = time.monotonic()
        return True

    def _watchdog(self) -> None:
        if self._status == "localized":
            # AMCL publishes /amcl_pose only when the robot moves, but it keeps
            # publishing its dynamic map->odom transform on every scan. That
            # transform is the only liveness heartbeat a motionless robot has:
            # demoting on /amcl_pose staleness alone flapped a healthy,
            # stationary robot between localized and manual_required every few
            # seconds (observed on LR-01, 2026-08-26). Hold "localized" while
            # the heartbeat is fresh, regardless of how it was established.
            if self._map_to_odom_transform_is_fresh():
                self._last_stable_pose_at = time.monotonic()
                self._publish_status()
                return
            self._manual_pose_accepted = False
            self._set_status("manual_required")
            self.get_logger().warn(
                "AMCL map-to-odom transform timed out; navigation is blocked until localization recovers"
            )
            return
        if self._status == "starting":
            # Waiting for AMCL activation + the current-session map is normal
            # during bringup, but a stuck lifecycle (or a dead AMCL) must not
            # block here forever: fall back to operator confirmation.
            elapsed = (self.get_clock().now() - self._phase_started_at).nanoseconds / 1_000_000_000
            if elapsed >= self.startup_amcl_timeout_seconds:
                self._set_status("manual_required")
                self.get_logger().warn(
                    "AMCL did not become ready in time; awaiting operator initial pose confirmation"
                )
            return
        if self._status not in {"restoring_saved_pose", "global_localizing", "manual_initializing"}:
            return
        elapsed = (self.get_clock().now() - self._phase_started_at).nanoseconds / 1_000_000_000
        if elapsed < self.initialization_timeout_seconds:
            return
        if self._status == "restoring_saved_pose":
            self._global_request_sent = False
            self._request_global_localization()
            return
        self._set_status("manual_required")
        self.get_logger().warn(
            "automatic localization did not converge yet; awaiting a stable AMCL pose or operator confirmation"
        )

    def _request_nomotion_update(self) -> None:
        # "localized" is deliberately excluded: every forced update runs a
        # measurement re-weighting + resample, which is a pointless random
        # walk in feature-sparse spots and the main cause of stationary
        # drift. AMCL republishes map→odom TF on every scan regardless, so
        # TF freshness does not depend on these calls.
        if self._nomotion_request_pending or self._status not in {
            "restoring_saved_pose",
            "global_localizing",
            "manual_initializing",
            "manual_required",
        }:
            return
        if not self.nomotion_update_client.wait_for_service(timeout_sec=0.0):
            return
        self._nomotion_request_pending = True
        future = self.nomotion_update_client.call_async(Empty.Request())
        future.add_done_callback(self._handle_nomotion_update_response)

    def _handle_nomotion_update_response(self, future) -> None:
        self._nomotion_request_pending = False
        try:
            future.result()
        except Exception as error:  # pragma: no cover - ROS transport failures are runtime-only.
            self.get_logger().warn(f"AMCL no-motion update request failed: {error}")

    def _read_saved_pose(self) -> tuple[float, float, float, float] | None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("mapId") != self.active_map_id:
                return None
            saved_at = datetime.fromisoformat(str(data["savedAt"]).replace("Z", "+00:00"))
            if saved_at.tzinfo is None:
                return None
            age_seconds = (datetime.now(UTC) - saved_at.astimezone(UTC)).total_seconds()
            values = (data["x"], data["y"], data["yaw"])
            if age_seconds < 0 or age_seconds > self.max_saved_pose_age_seconds:
                return None
            if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in values):
                return None
            if not self._pose_is_within_active_map_bounds(float(values[0]), float(values[1])):
                return None
            return (float(values[0]), float(values[1]), 0.0, float(values[2]))
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _save_pose(self, x: float, y: float, yaw: float) -> None:
        payload = {
            "mapId": self.active_map_id,
            "x": x,
            "y": y,
            "yaw": yaw,
            "savedAt": datetime.now(UTC).isoformat(),
        }
        try:
            self.state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.state_file)
            self._last_pose_saved_at = time.monotonic()
        except OSError as error:  # Persisting is useful but must not block a valid localization.
            self.get_logger().error(f"failed to save verified pose: {error}")

    def _publish_initial_pose(self, x: float, y: float, z: float, yaw: float) -> None:
        pose = make_initial_pose(self, "map", x, y, z, yaw)
        # This path is used only after laser matching. Keep the verified
        # neighborhood instead of the operator's broad half-meter prior.
        pose.pose.covariance[0] = 0.04
        pose.pose.covariance[7] = 0.04
        pose.pose.covariance[35] = math.radians(5.0) ** 2
        self._initial_pose_seeded_at_ns = pose.header.stamp.sec * 1_000_000_000 + pose.header.stamp.nanosec
        self._recent_republished_initial_poses.append((x, y, yaw, time.monotonic()))
        # Reliable delivery is sufficient; repeated seeds reset AMCL again.
        self.initial_pose_publisher.publish(pose)

    def _validate_map_bounds(self) -> bool:
        min_x = float(getattr(self, "map_min_x", -math.inf))
        max_x = float(getattr(self, "map_max_x", math.inf))
        min_y = float(getattr(self, "map_min_y", -math.inf))
        max_y = float(getattr(self, "map_max_y", math.inf))
        return all(
            math.isfinite(value)
            for value in (min_x, max_x, min_y, max_y)
        ) and min_x < max_x and min_y < max_y

    def _load_registered_map_bounds(self, map_id: str) -> bool:
        """Refresh the active bounds from the root-managed map registry."""

        if not getattr(self, "map_registry_file", None):
            return self._validate_map_bounds()
        try:
            selected = load_map_registry(self.map_registry_file).get(map_id)
        except ValueError as error:
            self.get_logger().error(f"cannot load map registry for localization: {error}")
            return False
        if selected is None:
            return False
        self.map_min_x = selected.min_x
        self.map_max_x = selected.max_x
        self.map_min_y = selected.min_y
        self.map_max_y = selected.max_y
        return self._validate_map_bounds()

    def _pose_is_within_active_map_bounds(self, x: float, y: float) -> bool:
        return bool(
            getattr(self, "_map_bounds_valid", self._validate_map_bounds())
            and math.isfinite(x)
            and math.isfinite(y)
            and getattr(self, "map_min_x", -math.inf) <= x <= getattr(self, "map_max_x", math.inf)
            and getattr(self, "map_min_y", -math.inf) <= y <= getattr(self, "map_max_y", math.inf)
        )

    def _reject_out_of_bounds_pose(self, source: str) -> None:
        self._stable_samples = 0
        self._manual_initial_pose = None
        self._manual_pose_accepted = False
        self._set_status("manual_required")
        if not getattr(self, "_map_bounds_rejection_logged", False):
            self.get_logger().warn(f"{source} is outside the active map bounds; navigation is blocked")
            self._map_bounds_rejection_logged = True

    def _map_to_odom_transform_is_fresh(self) -> bool:
        last_transform = self._last_map_to_odom_transform_at
        return bool(
            last_transform is not None
            and time.monotonic() - last_transform <= self.pose_stale_timeout_seconds
        )

    def _set_status(self, status: str) -> None:
        if self._status == status:
            return
        self._status = status
        self._phase_started_at = self.get_clock().now()
        self._publish_status()

    def _publish_status(self) -> None:
        self.status_publisher.publish(String(data=self._status))


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = AutoLocalizationNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
