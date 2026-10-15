"""Vision port that owns D435 capture, matching, point cloud, and transform."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import uuid
from typing import Any, Optional, Sequence, Tuple

import numpy as np

from bookarm.devices.camera.d435_device import D435Device, D435Frame
from bookarm.devices.camera.matcher import BookSpineMatcher
from bookarm.devices.camera.transform import CameraToBaseTransform
from bookarm.devices.camera.visualization import _match_overlay, display_book_spine_detection
from bookarm.domain.models.book import BookTarget, ShelfPlacementTarget, SpineTemplate
from bookarm.domain.models.pose import BasePose
from bookarm.runtime.command_queue import DeviceCommandWorker


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return [_jsonable(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class D435BookSpineLocatorConfig:
    serial: str = "262322073961"
    calibration_path: Path = Path("config/calibration/camera_to_base.json")
    width: int = 1280
    height: int = 720
    depth_width: int = 1280
    depth_height: int = 720
    fps: int = 30
    warmup_frames: int = 30
    frame_timeout_ms: int = 5000
    max_attempts: int = 10
    max_depth_m: float = 2.0
    point_cloud_stride: int = 1
    grasp_point_from_top_ratio: float = BookSpineMatcher.TOP_DOWN_ONE_SIXTH
    visualize: bool = False
    visualize_duration_ms: int = 2500
    # When set, every locate call persists latest_raw.jpg / latest_annotated.jpg
    # / latest.json there so headless deployments can review what was matched.
    debug_capture_dir: Optional[Path] = None

    def __post_init__(self) -> None:
        BookSpineMatcher.validate_top_down_ratio(self.grasp_point_from_top_ratio)


class D435BookSpineLocator:
    """定位书脊的 D435 独占控制器；可选显示实际参与定位的图像。"""

    def __init__(self, config: D435BookSpineLocatorConfig, camera: Optional[D435Device] = None) -> None:
        self.config = config
        self._camera = camera or D435Device(
            serial=config.serial,
            width=config.width,
            height=config.height,
            depth_width=config.depth_width,
            depth_height=config.depth_height,
            fps=config.fps,
        )
        self._transform: Optional[CameraToBaseTransform] = None
        self._worker = DeviceCommandWorker("camera")
        self._visualization_enabled = bool(config.visualize)

    def start(self) -> None:
        self._worker.start()
        self._worker.call(self._load_calibration)

    def stop(self) -> None:
        if not self._worker.started:
            return
        try:
            self._worker.call(self._camera.stop, timeout_s=2.0)
        finally:
            self._worker.stop()

    def locate_book(self, template: SpineTemplate) -> BookTarget:
        timeout_s = (float(self.config.frame_timeout_ms) / 1000.0) * (self.config.max_attempts + 2)
        if self._visualization_enabled:
            timeout_s += 2.0 * float(self.config.visualize_duration_ms) / 1000.0
        return self._worker.call(lambda: self._locate_book(template), timeout_s=max(1.0, timeout_s))

    def locate_shelf_placement(
        self,
        left_neighbor_template: SpineTemplate,
        *,
        reference_point_from_top_ratio: float,
    ) -> ShelfPlacementTarget:
        """Locate an exact horizontal-center point on the supplied neighboring spine."""

        timeout_s = (float(self.config.frame_timeout_ms) / 1000.0) * (self.config.max_attempts + 2)
        if self._visualization_enabled:
            timeout_s += 2.0 * float(self.config.visualize_duration_ms) / 1000.0
        return self._worker.call(
            lambda: self._locate_shelf_placement(
                left_neighbor_template,
                reference_point_from_top_ratio=reference_point_from_top_ratio,
            ),
            timeout_s=max(1.0, timeout_s),
        )

    def capture_photo(self) -> Tuple[np.ndarray, int]:
        """Capture one color frame for interactive use outside a workflow.

        Runs the same start → warmup → capture sequence as book location on
        the camera command worker, and returns the BGR image together with
        the device's frame number.
        """

        timeout_s = (float(self.config.frame_timeout_ms) / 1000.0) * 2
        return self._worker.call(self._capture_photo, timeout_s=max(1.0, timeout_s))

    def capture_frame(self) -> D435Frame:
        """Capture one synchronized color+depth frame for interactive vision.

        Same start → warmup → capture sequence as :meth:`capture_photo`, but
        returns the full frame so callers can deproject pixels with
        ``frame.point_at_pixel``.
        """

        timeout_s = (float(self.config.frame_timeout_ms) / 1000.0) * 2
        return self._worker.call(self._capture_frame, timeout_s=max(1.0, timeout_s))

    def transform_to_base(self, point_camera_m: Tuple[float, float, float]) -> Tuple[float, float, float]:
        """Map one camera-frame point into the arm base frame."""

        return self._worker.call(lambda: self._transform_to_base(point_camera_m), timeout_s=1.0)

    def calibration_matrix(self) -> list:
        """Return the live 4x4 camera-to-base matrix (nested lists)."""

        return self._worker.call(self._calibration_matrix, timeout_s=1.0)

    def set_calibration(self, matrix: Sequence[Sequence[float]]) -> None:
        """Hot-swap the live camera-to-base transform (not persisted here)."""

        self._worker.call(lambda: self._set_calibration(matrix), timeout_s=1.0)

    def _calibration_matrix(self) -> list:
        if self._transform is None:
            raise RuntimeError("camera calibration is not loaded")
        return self._transform.matrix.tolist()

    def _set_calibration(self, matrix: Sequence[Sequence[float]]) -> None:
        self._transform = CameraToBaseTransform(matrix)

    def pipeline_recovery_status(self) -> Dict[str, Any]:
        """Auto-recovery counters of the owned D435 pipeline (read-only ints)."""

        return self._camera.pipeline_recovery_status()

    def _capture_frame(self) -> D435Frame:
        self._camera.start()
        self._camera.warmup(self.config.warmup_frames, self.config.frame_timeout_ms)
        return self._camera.capture(self.config.frame_timeout_ms)

    def _transform_to_base(self, point_camera_m: Tuple[float, float, float]) -> Tuple[float, float, float]:
        if self._transform is None:
            raise RuntimeError("camera calibration is not loaded")
        return self._transform.transform_point(point_camera_m)

    def _capture_photo(self) -> Tuple[np.ndarray, int]:
        self._camera.start()
        self._camera.warmup(self.config.warmup_frames, self.config.frame_timeout_ms)
        frame = self._camera.capture(self.config.frame_timeout_ms)
        return frame.color_bgr, frame.frame_number

    def _load_calibration(self) -> None:
        self._transform = CameraToBaseTransform.from_json(Path(self.config.calibration_path))

    def _locate_book(self, template: SpineTemplate) -> BookTarget:
        if self._transform is None:
            raise RuntimeError("camera calibration is not loaded")
        matcher = BookSpineMatcher(Path(template.path))
        self._camera.start()
        self._camera.warmup(self.config.warmup_frames, self.config.frame_timeout_ms)
        for _attempt in range(max(1, int(self.config.max_attempts))):
            frame = self._camera.capture(self.config.frame_timeout_ms)
            match = matcher.match(frame.color_bgr)
            if match is None:
                continue
            pixel = matcher.pick_point(match.polygon_uv, self.config.grasp_point_from_top_ratio)
            try:
                camera_point = frame.point_at_pixel(*pixel)
            except (RuntimeError, ValueError):
                # Depth holes vary frame to frame; retry with a fresh capture
                # instead of aborting the whole locate on one bad frame.
                continue
            if camera_point[2] > float(self.config.max_depth_m):
                continue
            base_point = self._transform.transform_point(camera_point)
            pose = BasePose(base_point[0], base_point[1], base_point[2], 0.0, 0.0, 0.0)
            self._visualize_detection(frame.color_bgr, Path(template.path), match)
            self._persist_debug_detection(
                frame.color_bgr,
                Path(template.path),
                match,
                kind="take_book",
                extra={
                    "matched": True,
                    "pixel_uv": pixel,
                    "camera_point_m": camera_point,
                    "base_point_m": base_point,
                    "confidence": float(match.confidence),
                    "frame_number": frame.frame_number,
                },
            )
            polygon = tuple(
                (float(point[0]), float(point[1])) for point in match.polygon_uv.reshape(4, 2)
            )
            return BookTarget(
                raw_pose=pose,
                grasp_pose=pose,
                pixel_uv=pixel,
                confidence=float(match.confidence),
                capture_id="d435-%s-%d" % (uuid.uuid4().hex, frame.frame_number),
                polygon_uv=polygon,
                frame_width_px=int(frame.color_bgr.shape[1]),
                frame_height_px=int(frame.color_bgr.shape[0]),
            )
        if self._visualization_enabled:
            self._visualize_detection(frame.color_bgr, Path(template.path), None)
        self._persist_debug_detection(
            frame.color_bgr,
            Path(template.path),
            match,
            kind="take_book",
            extra={"matched": False, "frame_number": frame.frame_number},
        )
        raise RuntimeError("book spine was not detected within %d D435 captures" % self.config.max_attempts)

    def _locate_shelf_placement(
        self,
        left_neighbor_template: SpineTemplate,
        *,
        reference_point_from_top_ratio: float,
    ) -> ShelfPlacementTarget:
        if self._transform is None:
            raise RuntimeError("camera calibration is not loaded")
        matcher = BookSpineMatcher(Path(left_neighbor_template.path))
        self._camera.start()
        self._camera.warmup(self.config.warmup_frames, self.config.frame_timeout_ms)
        for _attempt in range(max(1, int(self.config.max_attempts))):
            frame = self._camera.capture(self.config.frame_timeout_ms)
            match = matcher.match(frame.color_bgr)
            if match is None:
                continue
            reference_pixel = matcher.pick_point(
                match.polygon_uv,
                reference_point_from_top_ratio,
            )
            try:
                camera_point = frame.point_at_pixel(*reference_pixel)
            except (RuntimeError, ValueError):
                continue
            if camera_point[2] > float(self.config.max_depth_m):
                continue
            base_point = self._transform.transform_point(camera_point)
            pose = BasePose(base_point[0], base_point[1], base_point[2], 0.0, 0.0, 0.0)
            self._visualize_detection(
                frame.color_bgr,
                Path(left_neighbor_template.path),
                match,
                marker_pixel_uv=reference_pixel,
            )
            polygon = tuple(
                (float(point[0]), float(point[1])) for point in match.polygon_uv.reshape(4, 2)
            )
            self._persist_debug_detection(
                frame.color_bgr,
                Path(left_neighbor_template.path),
                match,
                kind="return_book",
                marker_pixel_uv=reference_pixel,
                extra={
                    "matched": True,
                    "pixel_uv": reference_pixel,
                    "camera_point_m": camera_point,
                    "base_point_m": base_point,
                    "confidence": float(match.confidence),
                    "frame_number": frame.frame_number,
                },
            )
            return ShelfPlacementTarget(
                pose=pose,
                neighbor_reference_uv=reference_pixel,
                target_pixel_uv=reference_pixel,
                neighbor_polygon_uv=polygon,
                confidence=float(match.confidence),
                capture_id="d435-%s-%d" % (uuid.uuid4().hex, frame.frame_number),
            )
        if self._visualization_enabled:
            self._visualize_detection(frame.color_bgr, Path(left_neighbor_template.path), None)
        self._persist_debug_detection(
            frame.color_bgr,
            Path(left_neighbor_template.path),
            match,
            kind="return_book",
            extra={"matched": False, "frame_number": frame.frame_number},
        )
        raise RuntimeError(
            "left-neighbor book spine or its offset depth was not detected within %d D435 captures"
            % self.config.max_attempts
        )

    def _visualize_detection(
        self,
        image_bgr,
        template_path: Path,
        match,
        marker_pixel_uv=None,
    ) -> None:
        """Show diagnostics when requested, without letting GUI failure alter motion."""

        if not self._visualization_enabled:
            return
        try:
            display_book_spine_detection(
                image_bgr,
                template_path,
                match,
                duration_ms=self.config.visualize_duration_ms,
                grasp_point_from_top_ratio=self.config.grasp_point_from_top_ratio,
                marker_pixel_uv=marker_pixel_uv,
            )
        except Exception as error:
            self._visualization_enabled = False
            print(
                "[WARN] book-spine visualization is unavailable; continuing without it: %s" % error,
                file=sys.stderr,
            )

    def _persist_debug_detection(
        self,
        image_bgr,
        template_path: Path,
        match,
        *,
        kind: str,
        marker_pixel_uv=None,
        extra: Optional[dict] = None,
    ) -> None:
        """Persist capture/match diagnostics to disk; never alter location flow."""

        directory = self.config.debug_capture_dir
        if directory is None:
            return
        try:
            import cv2

            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            overlay, details = _match_overlay(
                image_bgr,
                match,
                self.config.grasp_point_from_top_ratio,
                marker_pixel_uv,
            )
            cv2.imwrite(str(directory / "latest_raw.jpg"), image_bgr)
            cv2.imwrite(str(directory / "latest_annotated.jpg"), overlay)
            metadata = {
                "kind": kind,
                "template": str(template_path),
                "timestamp": datetime.now(UTC).isoformat(),
                "image_width": int(image_bgr.shape[1]),
                "image_height": int(image_bgr.shape[0]),
            }
            metadata.update({key: _jsonable(value) for key, value in details.items()})
            if extra:
                metadata.update({key: _jsonable(value) for key, value in extra.items()})
            (directory / "latest.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as error:
            print(
                "[WARN] book-spine debug persistence failed; continuing without it: %s" % error,
                file=sys.stderr,
            )
