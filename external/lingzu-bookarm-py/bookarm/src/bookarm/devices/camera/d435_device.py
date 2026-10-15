"""Direct pyrealsense2 pipeline owner for one D435 camera."""

from __future__ import annotations

from dataclasses import dataclass
import sys
import threading
import time
from typing import Any, Dict, Optional

import numpy as np


@dataclass(frozen=True)
class D435Frame:
    color_bgr: np.ndarray
    depth_m: np.ndarray
    fx: float
    fy: float
    ppx: float
    ppy: float
    timestamp_ms: float
    frame_number: int

    # D435 depth holes on dark or glossy spines routinely exceed a 5px
    # window, so the neighborhood search widens through these radii (after
    # the caller-requested one) before giving up. Candidates stay sorted by
    # pixel distance, so escalation still prefers the closest valid depth.
    DEPTH_SEARCH_RADII = (15, 35, 60)

    def point_at_pixel(self, u: int, v: int, search_radius: int = 5) -> tuple[float, float, float]:
        height, width = self.depth_m.shape
        if not 0 <= int(u) < width or not 0 <= int(v) < height:
            raise ValueError("pixel (%d, %d) is outside frame %dx%d" % (u, v, width, height))
        u, v = int(u), int(v)
        radii = [max(0, int(search_radius))]
        radii.extend(radius for radius in self.DEPTH_SEARCH_RADII if radius > radii[0])
        for radius in radii:
            candidates = []
            for row in range(max(0, v - radius), min(height, v + radius + 1)):
                for column in range(max(0, u - radius), min(width, u + radius + 1)):
                    depth = float(self.depth_m[row, column])
                    if np.isfinite(depth) and depth > 0.0:
                        candidates.append(((column - u) ** 2 + (row - v) ** 2, column, row, depth))
            if candidates:
                _distance, column, row, depth = min(candidates, key=lambda item: item[0])
                return (
                    (float(column) - self.ppx) * depth / self.fx,
                    (float(row) - self.ppy) * depth / self.fy,
                    depth,
                )
        raise RuntimeError("no valid D435 depth near pixel (%d, %d)" % (u, v))

    def point_cloud(self, max_depth_m: float, stride: int = 1) -> np.ndarray:
        """Generate an Nx3 camera-frame point cloud for diagnostics or recording."""

        stride = max(1, int(stride))
        rows, columns = np.mgrid[0:self.depth_m.shape[0]:stride, 0:self.depth_m.shape[1]:stride]
        depths = self.depth_m[::stride, ::stride]
        valid = np.isfinite(depths) & (depths > 0.0) & (depths <= float(max_depth_m))
        u = columns[valid].astype(float)
        v = rows[valid].astype(float)
        z = depths[valid].astype(float)
        return np.column_stack(((u - self.ppx) * z / self.fx, (v - self.ppy) * z / self.fy, z))


class D435Device:
    """Create, use, and close one RealSense pipeline from a single owner thread."""

    # USB 扰动后管线经常永久卡死；帧超时后自动重建管线的次数上限。
    MAX_PIPELINE_REBUILDS = 3
    # wait_for_frames 每次阻塞等下一帧，健康管线的 frame_number 必然递增；
    # 连续 3 次取到同一（或回退的）帧号才判定管线卡死，排除连拍边缘误伤。
    STALE_FRAME_LIMIT = 3

    def __init__(
        self,
        *,
        serial: str,
        width: int = 1280,
        height: int = 720,
        depth_width: int = 1280,
        depth_height: int = 720,
        fps: int = 30,
        align_depth_to_color: bool = True,
    ) -> None:
        self.serial = str(serial)
        self.width = int(width)
        self.height = int(height)
        self.depth_width = int(depth_width)
        self.depth_height = int(depth_height)
        self.fps = int(fps)
        self.align_depth_to_color = bool(align_depth_to_color)
        self._rs: Optional[Any] = None
        self._pipeline: Optional[Any] = None
        self._profile: Optional[Any] = None
        self._align: Optional[Any] = None
        self._depth_scale: Optional[float] = None
        # Guards pipeline pointer swaps during a recovery rebuild; normally the
        # owning camera worker is the only caller, so the lock is uncontended.
        self._pipeline_lock = threading.Lock()
        # Pause after stop() so a flaky USB link settles before the restart.
        self.recovery_settle_s = 0.5
        self._pipeline_recoveries = 0
        self._last_recovery_at: Optional[float] = None
        # Stale-frame tracking: consecutive reads of a non-advancing frame number.
        self._last_frame_number: Optional[int] = None
        self._stale_frame_streak = 0
        # Consecutive rebuilds without one successful fresh frame; a good read
        # resets it, so a persistently stuck camera still errors out.
        self._consecutive_rebuilds = 0

    @property
    def started(self) -> bool:
        return self._profile is not None

    def pipeline_recovery_status(self) -> Dict[str, Any]:
        """Auto-recovery counters for hardware status views."""

        return {
            "pipeline_recoveries": self._pipeline_recoveries,
            "last_recovery_at": self._last_recovery_at,
        }

    def start(self) -> None:
        if self._profile is not None:
            return
        with self._pipeline_lock:
            self._open_pipeline()

    def _open_pipeline(self) -> None:
        try:
            import pyrealsense2 as rs
        except ImportError as error:
            raise RuntimeError("bookarm camera requires pyrealsense2") from error
        pipeline = rs.pipeline()
        config = rs.config()
        config.enable_device(self.serial)
        config.enable_stream(rs.stream.depth, self.depth_width, self.depth_height, rs.format.z16, self.fps)
        config.enable_stream(rs.stream.color, self.width, self.height, rs.format.bgr8, self.fps)
        try:
            profile = pipeline.start(config)
        except Exception as error:
            raise RuntimeError("failed to start D435 serial %s: %s" % (self.serial, error)) from error
        self._rs = rs
        self._pipeline = pipeline
        self._profile = profile
        self._align = rs.align(rs.stream.color) if self.align_depth_to_color else None
        sensor = profile.get_device().first_depth_sensor()
        self._depth_scale = float(sensor.get_depth_scale())

    def _rebuild_pipeline(self, reason: str = "frame timeout") -> None:
        """Stop and restart the pipeline with the same configuration."""

        with self._pipeline_lock:
            print(
                "[D435] %s; rebuilding pipeline (attempt %d/%d)"
                % (reason, self._pipeline_recoveries + 1, self.MAX_PIPELINE_REBUILDS),
                file=sys.stderr,
            )
            try:
                if self._pipeline is not None:
                    self._pipeline.stop()
            finally:
                self._pipeline = None
                self._profile = None
                self._align = None
                self._depth_scale = None
                self._rs = None
            # Frame numbers restart from zero on a fresh pipeline; without a
            # reset the stale-frame detector would misfire on every new frame.
            self._last_frame_number = None
            self._stale_frame_streak = 0
            time.sleep(self.recovery_settle_s)
            self._open_pipeline()
            self._pipeline_recoveries += 1
            self._consecutive_rebuilds += 1
            self._last_recovery_at = time.time()

    def stop(self) -> None:
        with self._pipeline_lock:
            try:
                if self._profile is not None and self._pipeline is not None:
                    self._pipeline.stop()
            finally:
                self._pipeline = None
                self._profile = None
                self._align = None
                self._depth_scale = None
                self._rs = None

    def _wait_frames(self, timeout_ms: int) -> Any:
        """wait_for_frames with automatic pipeline rebuild on frame timeouts."""

        while True:
            with self._pipeline_lock:
                pipeline = self._pipeline
            if pipeline is None:
                raise RuntimeError("D435 is not started")
            try:
                frames = pipeline.wait_for_frames(int(timeout_ms))
            except RuntimeError as error:
                if "didn't arrive" not in str(error):
                    raise
                self._recover_or_raise(
                    "frame timeout",
                    "D435 frame timeout persisted; pipeline auto-rebuilt %d times: %s"
                    % (self._consecutive_rebuilds, error),
                )
                continue
            if self._is_stale_frameset(frames):
                self._recover_or_raise(
                    "stale frame (%d consecutive reads)" % self._stale_frame_streak,
                    "D435 kept returning the same frame; pipeline auto-rebuilt %d times"
                    % self._consecutive_rebuilds,
                )
                continue
            return frames

    def _recover_or_raise(self, log_reason: str, error_message: str) -> None:
        """Rebuild the pipeline, or raise once the rebuild budget is spent.

        The budget counts consecutive rebuilds without a successful fresh
        frame, so it applies across warmup/capture calls, not just within one.
        """

        with self._pipeline_lock:
            spent = self._consecutive_rebuilds
        if spent >= self.MAX_PIPELINE_REBUILDS:
            raise RuntimeError(error_message)
        self._rebuild_pipeline(log_reason)

    def _is_stale_frameset(self, frames: Any) -> bool:
        """Track non-advancing frame numbers; true once the streak hits the limit."""

        frame_number: Optional[int] = None
        try:
            color_frame = frames.get_color_frame()
            if color_frame:
                frame_number = int(color_frame.get_frame_number())
        except Exception:
            frame_number = None
        if frame_number is None:
            return False
        with self._pipeline_lock:
            if (
                self._last_frame_number is not None
                and frame_number <= self._last_frame_number
            ):
                self._stale_frame_streak += 1
            else:
                self._stale_frame_streak = 1
                # A genuinely advancing frame means the pipeline is healthy
                # again; only then does the consecutive-rebuild budget clear.
                # (Frame numbers restart after a rebuild, so the first read of
                # a new pipeline is deliberately neutral.)
                if self._last_frame_number is not None:
                    self._consecutive_rebuilds = 0
            self._last_frame_number = frame_number
            return self._stale_frame_streak >= self.STALE_FRAME_LIMIT

    def warmup(self, frame_count: int, timeout_ms: int) -> None:
        self._require_started()
        for _ in range(max(0, int(frame_count))):
            self._wait_frames(timeout_ms)

    def capture(self, timeout_ms: int) -> D435Frame:
        self._require_started()
        frames = self._wait_frames(timeout_ms)
        if self._align is not None:
            frames = self._align.process(frames)
        depth_frame = frames.get_depth_frame()
        color_frame = frames.get_color_frame()
        if not depth_frame or not color_frame:
            raise RuntimeError("D435 did not provide synchronized depth and color frames")
        profile = color_frame.profile.as_video_stream_profile()
        intrinsics = profile.intrinsics
        assert self._depth_scale is not None
        return D435Frame(
            color_bgr=np.asanyarray(color_frame.get_data()).copy(),
            depth_m=np.asanyarray(depth_frame.get_data()).astype(np.float32) * self._depth_scale,
            fx=float(intrinsics.fx),
            fy=float(intrinsics.fy),
            ppx=float(intrinsics.ppx),
            ppy=float(intrinsics.ppy),
            timestamp_ms=float(color_frame.get_timestamp()),
            frame_number=int(color_frame.get_frame_number()),
        )

    def _require_started(self) -> None:
        if self._profile is None:
            raise RuntimeError("D435 is not started")
