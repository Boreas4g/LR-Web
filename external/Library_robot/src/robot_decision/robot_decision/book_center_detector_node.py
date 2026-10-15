#!/usr/bin/env python3

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
from cv_bridge import CvBridge, CvBridgeError
import numpy as np
import rclpy
from ament_index_python.packages import get_package_share_directory
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import Bool, Int32, String


DEFAULT_TARGET_IMAGE = ""

FULL_FRAME_MAX_SIDE = 960
ROI_FRAME_MAX_SIDE = 960
TARGET_MAX_SIDE = 1000
CENTER_SMOOTHING_ALPHA = 0.25
ROI_EXPAND_RATIO = 0.45
ROI_MIN_PAD = 60
ROI_REACQUIRE_AFTER_MISSES = 3
ORB_FEATURES = 1600
SIFT_FEATURES = 2500
ORB_RATIO_TEST = 0.82
SIFT_RATIO_TEST = 0.72
MIN_GOOD_MATCHES = 18
MIN_INLIERS = 12


@dataclass
class FeatureBackend:
    name: str
    detector: object
    matcher: object
    ratio_test: float
    min_good_matches: int
    min_inliers: int
    target_keypoints: list
    target_descriptors: np.ndarray
    target_corners: np.ndarray


@dataclass
class MatchResult:
    polygon: Optional[np.ndarray]
    good_count: int
    inlier_count: int
    match_confidence: float
    backend_name: str
    search_scope: str
    accepted: bool = False


def resize_keep_aspect(image: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    h, w = image.shape[:2]
    scale = min(float(max_side) / float(max(h, w)), 1.0)
    if scale >= 1.0:
        return image, 1.0
    resized = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return resized, scale


def clamp_rect(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    frame_width: int,
    frame_height: int,
) -> tuple[int, int, int, int]:
    x0_i = max(0, min(int(x0), frame_width - 1))
    y0_i = max(0, min(int(y0), frame_height - 1))
    x1_i = max(x0_i + 1, min(int(x1), frame_width))
    y1_i = max(y0_i + 1, min(int(y1), frame_height))
    return x0_i, y0_i, x1_i, y1_i


def polygon_to_roi(polygon: np.ndarray, frame_shape: tuple[int, ...]) -> tuple[int, int, int, int]:
    h, w = frame_shape[:2]
    points = polygon.reshape(-1, 2).astype(np.float32)
    left_x = float(points[:, 0].min())
    right_x = float(points[:, 0].max())
    top_y = float(points[:, 1].min())
    bottom_y = float(points[:, 1].max())
    box_w = max(right_x - left_x, 1.0)
    box_h = max(bottom_y - top_y, 1.0)
    pad_x = max(int(box_w * ROI_EXPAND_RATIO), ROI_MIN_PAD)
    pad_y = max(int(box_h * ROI_EXPAND_RATIO), ROI_MIN_PAD)
    return clamp_rect(left_x - pad_x, top_y - pad_y, right_x + pad_x, bottom_y + pad_y, w, h)


def build_feature_backend(
    name: str,
    detector: object,
    norm_type: int,
    ratio_test: float,
    target_image: np.ndarray,
) -> FeatureBackend:
    gray = cv2.cvtColor(target_image, cv2.COLOR_BGR2GRAY)
    keypoints, descriptors = detector.detectAndCompute(gray, None)
    if descriptors is None or len(keypoints) < MIN_GOOD_MATCHES:
        raise RuntimeError("%s target image has too few features" % name)

    h, w = target_image.shape[:2]
    corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
    return FeatureBackend(
        name=name,
        detector=detector,
        matcher=cv2.BFMatcher(norm_type),
        ratio_test=ratio_test,
        min_good_matches=MIN_GOOD_MATCHES,
        min_inliers=MIN_INLIERS,
        target_keypoints=keypoints,
        target_descriptors=descriptors,
        target_corners=corners,
    )


def match_target(
    frame: np.ndarray,
    backend: FeatureBackend,
    search_rect: Optional[tuple[int, int, int, int]] = None,
) -> MatchResult:
    if search_rect is None:
        search_frame = frame
        offset_x = 0
        offset_y = 0
        search_scope = "FULL"
    else:
        x0, y0, x1, y1 = search_rect
        search_frame = frame[y0:y1, x0:x1]
        offset_x = x0
        offset_y = y0
        search_scope = "ROI"

    max_side = ROI_FRAME_MAX_SIDE if search_rect is not None else FULL_FRAME_MAX_SIDE
    frame_small, frame_scale = resize_keep_aspect(search_frame, max_side)
    gray = cv2.cvtColor(frame_small, cv2.COLOR_BGR2GRAY)
    frame_keypoints, frame_descriptors = backend.detector.detectAndCompute(gray, None)

    if frame_descriptors is None or len(frame_keypoints) < backend.min_good_matches:
        return MatchResult(None, 0, 0, 0.0, backend.name, search_scope)

    raw_matches = backend.matcher.knnMatch(backend.target_descriptors, frame_descriptors, k=2)
    good_matches = []
    for pair in raw_matches:
        if len(pair) < 2:
            continue
        first, second = pair
        if first.distance < backend.ratio_test * second.distance:
            good_matches.append(first)

    if len(good_matches) < backend.min_good_matches:
        return MatchResult(None, len(good_matches), 0, 0.0, backend.name, search_scope)

    src_pts = np.float32(
        [backend.target_keypoints[match.queryIdx].pt for match in good_matches]
    ).reshape(-1, 1, 2)
    dst_pts = np.float32(
        [frame_keypoints[match.trainIdx].pt for match in good_matches]
    ).reshape(-1, 1, 2)
    matrix, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)

    if matrix is None or mask is None:
        return MatchResult(None, len(good_matches), 0, 0.0, backend.name, search_scope)

    inliers = int(mask.ravel().sum())
    match_confidence = inliers / max(len(good_matches), 1)
    if inliers < backend.min_inliers:
        return MatchResult(None, len(good_matches), inliers, match_confidence, backend.name, search_scope)

    projected = cv2.perspectiveTransform(backend.target_corners, matrix)
    projected = projected / frame_scale
    if offset_x or offset_y:
        projected[:, 0, 0] += offset_x
        projected[:, 0, 1] += offset_y

    return MatchResult(
        projected.astype(np.int32),
        len(good_matches),
        inliers,
        match_confidence,
        backend.name,
        search_scope,
    )


def match_with_fallback(
    frame: np.ndarray,
    backends: tuple[FeatureBackend, ...],
    search_rect: Optional[tuple[int, int, int, int]],
    match_conf_threshold: float,
) -> MatchResult:
    best_result: Optional[MatchResult] = None
    for backend in backends:
        result = match_target(frame, backend, search_rect)
        result.accepted = result.polygon is not None and result.match_confidence >= match_conf_threshold
        if result.accepted:
            return result
        if best_result is None:
            best_result = result
        elif result.polygon is not None and result.match_confidence > best_result.match_confidence:
            best_result = result
        elif best_result.polygon is None and result.inlier_count > best_result.inlier_count:
            best_result = result

    if best_result is None:
        search_scope = "ROI" if search_rect is not None else "FULL"
        return MatchResult(None, 0, 0, 0.0, "NONE", search_scope, False)

    return best_result


def update_stable_state(
    current_state: bool,
    candidate_state: bool,
    green_count: int,
    red_count: int,
    green_confirm_frames: int,
    red_confirm_frames: int,
) -> tuple[bool, int, int]:
    if candidate_state:
        green_count += 1
        red_count = 0
        if green_count >= green_confirm_frames:
            current_state = True
    else:
        red_count += 1
        green_count = 0
        if red_count >= red_confirm_frames:
            current_state = False

    return current_state, green_count, red_count


class BookCenterDetectorNode(Node):
    def __init__(self) -> None:
        super().__init__("book_center_detector_node")

        self.declare_parameter("image_topic", "/camera/camera/color/image_raw")
        self.declare_parameter("target_image_path", DEFAULT_TARGET_IMAGE)
        self.declare_parameter("start_after_fine_tune", True)
        self.declare_parameter("fine_tune_done_topic", "/fine_tune/done")
        self.declare_parameter("lift_move_topic", "/lifttable/move_pulses")
        self.declare_parameter("lift_move_pulses", 4850 * 30)
        self.declare_parameter("lift_settle_sec", 12.0)
        self.declare_parameter("match_conf_threshold", 0.65)
        self.declare_parameter("center_tol_ratio", 0.03)
        self.declare_parameter("min_center_tol_px", 45.0)
        self.declare_parameter("green_confirm_frames", 2)
        self.declare_parameter("red_confirm_frames", 5)
        self.declare_parameter("publish_annotated_image", True)
        self.declare_parameter(
            "annotated_image_topic",
            "/book_center_detector/annotated_image",
        )
        self.declare_parameter("centered_topic", "/book_center_detector/centered")
        self.declare_parameter("status_topic", "/book_center_detector/status")

        self.image_topic = str(self.get_parameter("image_topic").value)
        self.target_image_path = self._resolve_target_image_path(
            str(self.get_parameter("target_image_path").value)
        )
        self.start_after_fine_tune = bool(self.get_parameter("start_after_fine_tune").value)
        self.fine_tune_done_topic = str(self.get_parameter("fine_tune_done_topic").value)
        self.lift_move_topic = str(self.get_parameter("lift_move_topic").value)
        self.lift_move_pulses = int(self.get_parameter("lift_move_pulses").value)
        self.lift_settle_sec = max(0.0, float(self.get_parameter("lift_settle_sec").value))
        self.match_conf_threshold = float(self.get_parameter("match_conf_threshold").value)
        self.center_tol_ratio = float(self.get_parameter("center_tol_ratio").value)
        self.min_center_tol_px = float(self.get_parameter("min_center_tol_px").value)
        self.green_confirm_frames = max(1, int(self.get_parameter("green_confirm_frames").value))
        self.red_confirm_frames = max(1, int(self.get_parameter("red_confirm_frames").value))
        self.publish_annotated_image = bool(self.get_parameter("publish_annotated_image").value)

        self.bridge = CvBridge()
        self.backends: tuple[FeatureBackend, ...] = ()
        self.target_image = self._load_target_image(self.target_image_path)
        self._build_backends()

        self.centered_pub = self.create_publisher(
            Bool,
            str(self.get_parameter("centered_topic").value),
            10,
        )
        self.lift_pub = self.create_publisher(Int32, self.lift_move_topic, 10)
        self.status_pub = self.create_publisher(
            String,
            str(self.get_parameter("status_topic").value),
            10,
        )
        self.annotated_pub = None
        if self.publish_annotated_image:
            self.annotated_pub = self.create_publisher(
                Image,
                str(self.get_parameter("annotated_image_topic").value),
                10,
            )

        self.image_sub = self.create_subscription(Image, self.image_topic, self._image_callback, 10)
        self.fine_tune_done_sub = self.create_subscription(
            Bool,
            self.fine_tune_done_topic,
            self._fine_tune_done_callback,
            10,
        )

        self.active = not self.start_after_fine_tune
        self.waiting_for_lift = False
        self.lift_timer = None
        self.stable_centered = False
        self.green_count = 0
        self.red_count = 0
        self.smoothed_book_center_x: Optional[float] = None
        self.roi_rect: Optional[tuple[int, int, int, int]] = None
        self.roi_miss_streak = 0

        self.get_logger().info(
            "book center detector ready: image_topic=%s target=%s start_after_fine_tune=%s"
            % (self.image_topic, self.target_image_path, self.start_after_fine_tune)
        )

    def _resolve_target_image_path(self, configured_path: str) -> str:
        configured_path = configured_path.strip()
        if configured_path:
            return str(Path(configured_path).expanduser())
        return str(Path(get_package_share_directory("robot_decision")) / "target" / "target2.jpg")

    def _fine_tune_done_callback(self, msg: Bool) -> None:
        if not msg.data:
            self.get_logger().warn("fine tune reported failure; book center detector remains inactive")
            return
        if self.active or self.waiting_for_lift:
            self.get_logger().info("fine tune done received, detector workflow is already active")
            return

        self.get_logger().info(
            "fine tune done received; moving lifttable pulses=%d"
            % self.lift_move_pulses
        )
        self._reset_detection_state()
        self.waiting_for_lift = True
        self.lift_pub.publish(Int32(data=self.lift_move_pulses))
        if self.lift_settle_sec <= 0.0:
            self._start_detection_after_lift()
            return
        self.lift_timer = self.create_timer(self.lift_settle_sec, self._handle_lift_settle_timer)

    def _handle_lift_settle_timer(self) -> None:
        if self.lift_timer is not None:
            self.lift_timer.cancel()
            self.lift_timer = None
        self._start_detection_after_lift()

    def _start_detection_after_lift(self) -> None:
        self.waiting_for_lift = False
        self.active = True
        self.get_logger().info("lifttable settle complete; book center detector is active")

    def _reset_detection_state(self) -> None:
        self.stable_centered = False
        self.green_count = 0
        self.red_count = 0
        self.smoothed_book_center_x = None
        self.roi_rect = None
        self.roi_miss_streak = 0

    def _load_target_image(self, path: str) -> np.ndarray:
        target_path = Path(path).expanduser()
        target = cv2.imread(str(target_path))
        if target is None:
            raise RuntimeError("failed to read target image: %s" % target_path)
        return resize_keep_aspect(target, TARGET_MAX_SIDE)[0]

    def _build_backends(self) -> None:
        backends = []
        for name, detector, norm_type, ratio_test in (
            ("ORB", cv2.ORB_create(nfeatures=ORB_FEATURES), cv2.NORM_HAMMING, ORB_RATIO_TEST),
            ("SIFT", cv2.SIFT_create(nfeatures=SIFT_FEATURES), cv2.NORM_L2, SIFT_RATIO_TEST),
        ):
            try:
                backend = build_feature_backend(
                    name,
                    detector,
                    norm_type,
                    ratio_test,
                    self.target_image,
                )
            except RuntimeError as exc:
                self.get_logger().warn(str(exc))
                continue
            backends.append(backend)

        if not backends:
            raise RuntimeError("target image has too few features for all matching backends")

        self.backends = tuple(backends)
        self.get_logger().info(
            "target features: %s"
            % ", ".join("%s=%d" % (b.name, len(b.target_keypoints)) for b in self.backends)
        )

    def _image_callback(self, msg: Image) -> None:
        if not self.active:
            return

        try:
            frame = self.bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
        except CvBridgeError as exc:
            self.get_logger().warn("failed to convert image: %s" % exc)
            return

        match_result = self._match_frame(frame)
        (
            candidate_centered,
            status,
            offset_px,
            tolerance_px,
            left_x,
            right_x,
        ) = self._evaluate_match(frame, match_result)

        self.stable_centered, self.green_count, self.red_count = update_stable_state(
            self.stable_centered,
            candidate_centered,
            self.green_count,
            self.red_count,
            self.green_confirm_frames,
            self.red_confirm_frames,
        )

        self.centered_pub.publish(Bool(data=self.stable_centered))
        self.status_pub.publish(String(data=status))

        if self.annotated_pub is not None:
            annotated = self._draw_annotation(
                frame.copy(),
                match_result,
                status,
                offset_px,
                tolerance_px,
                left_x,
                right_x,
            )
            annotated_msg = self.bridge.cv2_to_imgmsg(annotated, encoding="bgr8")
            annotated_msg.header = msg.header
            self.annotated_pub.publish(annotated_msg)

    def _match_frame(self, frame: np.ndarray) -> MatchResult:
        using_roi = self.roi_rect is not None and self.roi_miss_streak < ROI_REACQUIRE_AFTER_MISSES
        match_result = match_with_fallback(
            frame,
            self.backends,
            self.roi_rect if using_roi else None,
            self.match_conf_threshold,
        )

        if match_result.accepted:
            self.roi_rect = polygon_to_roi(match_result.polygon, frame.shape)
            self.roi_miss_streak = 0
        elif using_roi:
            self.roi_miss_streak += 1
            if self.roi_miss_streak >= ROI_REACQUIRE_AFTER_MISSES:
                match_result = match_with_fallback(
                    frame,
                    self.backends,
                    None,
                    self.match_conf_threshold,
                )
                if match_result.accepted:
                    self.roi_rect = polygon_to_roi(match_result.polygon, frame.shape)
                    self.roi_miss_streak = 0
                else:
                    self.roi_rect = None
                    self.roi_miss_streak = 0
        elif match_result.search_scope == "FULL" and not match_result.accepted:
            self.roi_rect = None

        return match_result

    def _evaluate_match(
        self,
        frame: np.ndarray,
        match_result: MatchResult,
    ) -> tuple[bool, str, float, float, Optional[float], Optional[float]]:
        h, w = frame.shape[:2]
        frame_center_x = w / 2.0
        tolerance_px = max(w * self.center_tol_ratio, self.min_center_tol_px)
        offset_px = 0.0
        left_x: Optional[float] = None
        right_x: Optional[float] = None

        if match_result.polygon is None:
            self.smoothed_book_center_x = None
            status = (
                "%s %s | NOT_FOUND | matches=%d inliers=%d"
                % (
                    match_result.backend_name,
                    match_result.search_scope,
                    match_result.good_count,
                    match_result.inlier_count,
                )
            )
            return False, status, offset_px, tolerance_px, left_x, right_x

        points = match_result.polygon.reshape(-1, 2).astype(np.float32)
        left_x = float(points[:, 0].min())
        right_x = float(points[:, 0].max())
        detected_center_x = (left_x + right_x) / 2.0
        if self.smoothed_book_center_x is None:
            self.smoothed_book_center_x = detected_center_x
        else:
            self.smoothed_book_center_x = (
                CENTER_SMOOTHING_ALPHA * detected_center_x
                + (1.0 - CENTER_SMOOTHING_ALPHA) * self.smoothed_book_center_x
            )

        offset_signed = self.smoothed_book_center_x - frame_center_x
        offset_px = abs(offset_signed)
        centered = offset_px <= tolerance_px
        candidate_centered = centered and match_result.match_confidence >= self.match_conf_threshold
        if match_result.match_confidence < self.match_conf_threshold:
            state = "LOW_MATCH"
        elif centered:
            state = "CENTERED"
        elif offset_signed < 0.0:
            state = "LEFT"
        else:
            state = "RIGHT"

        status = (
            "%s %s | %s | conf=%.1f%% offset=%.1fpx tol=%.1fpx "
            "matches=%d inliers=%d stable=%s"
            % (
                match_result.backend_name,
                match_result.search_scope,
                state,
                match_result.match_confidence * 100.0,
                offset_signed,
                tolerance_px,
                match_result.good_count,
                match_result.inlier_count,
                "true" if self.stable_centered else "false",
            )
        )
        return candidate_centered, status, offset_px, tolerance_px, left_x, right_x

    def _draw_annotation(
        self,
        frame: np.ndarray,
        match_result: MatchResult,
        status: str,
        offset_px: float,
        tolerance_px: float,
        left_x: Optional[float],
        right_x: Optional[float],
    ) -> np.ndarray:
        h, w = frame.shape[:2]
        center_x = w // 2
        tol = int(tolerance_px)
        guide_left = max(center_x - tol, 0)
        guide_right = min(center_x + tol, w - 1)
        color = (0, 255, 0) if self.stable_centered else (0, 0, 255)

        overlay = frame.copy()
        cv2.rectangle(overlay, (guide_left, 0), (guide_right, h), color, -1)
        frame = cv2.addWeighted(overlay, 0.08, frame, 0.92, 0)
        cv2.line(frame, (guide_left, 0), (guide_left, h), (220, 220, 220), 1)
        cv2.line(frame, (guide_right, 0), (guide_right, h), (220, 220, 220), 1)
        cv2.rectangle(frame, (center_x - 3, 0), (center_x + 3, h), color, -1)

        if left_x is not None and right_x is not None:
            cv2.rectangle(
                frame,
                (int(left_x), 0),
                (int(right_x), h - 1),
                (180, 180, 180),
                1,
            )

        if match_result.polygon is not None:
            cv2.polylines(frame, [match_result.polygon], True, color, 3, cv2.LINE_AA)
            for point in match_result.polygon.reshape(-1, 2):
                cv2.circle(frame, (int(point[0]), int(point[1])), 4, color, -1, cv2.LINE_AA)

        cv2.putText(frame, status[:115], (20, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA)
        cv2.putText(
            frame,
            "stable=%s candidate_frames=%d/%d miss_frames=%d/%d offset=%.1f"
            % (
                "true" if self.stable_centered else "false",
                self.green_count,
                self.green_confirm_frames,
                self.red_count,
                self.red_confirm_frames,
                offset_px,
            ),
            (20, 68),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        return frame


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = BookCenterDetectorNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
