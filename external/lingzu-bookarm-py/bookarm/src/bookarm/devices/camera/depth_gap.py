"""Depth-map shelf-gap location for the mid-return workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import cv2
import numpy as np

from bookarm.devices.camera.matcher import BookSpineMatcher
from bookarm.domain.models.book import SpineTemplate
from bookarm.domain.models.pose import BasePose


@dataclass(frozen=True)
class DepthGap:
    """One shelf gap found beside the matched spine on the depth scan band."""

    base_pose: BasePose
    pixel_uv: Tuple[int, int]
    side: str  # "left" | "right"
    gap_width_px: float
    left_gap_width_px: Optional[float]
    right_gap_width_px: Optional[float]
    spine_bbox_xyxy: Tuple[int, int, int, int]
    scan_row_v: int
    gap_depth_m: float
    reference_depth_m: float
    raw_image_path: Optional[str] = None
    overlay_path: Optional[str] = None

    def info(self) -> Dict[str, Any]:
        """JSON-ready view consumed by the tuning session API and frontend."""

        return {
            "gap_base_pose": list(self.base_pose.values()),
            "gap_pixel_uv": [int(self.pixel_uv[0]), int(self.pixel_uv[1])],
            "gap_width_px": float(self.gap_width_px),
            "selected_side": self.side,
            "left_gap_width_px": self.left_gap_width_px,
            "right_gap_width_px": self.right_gap_width_px,
            "spine_bbox_xyxy": [int(value) for value in self.spine_bbox_xyxy],
            "scan_row_v": int(self.scan_row_v),
            "gap_depth_m": float(self.gap_depth_m),
            "reference_depth_m": float(self.reference_depth_m),
            "raw_image_path": self.raw_image_path,
            "overlay_path": self.overlay_path,
            "method": "depth",
        }


def _is_gap_column(depth_m: float, gap_depth_min_m: float) -> bool:
    """A gap column is a depth hole (invalid) or a surface deeper than the jump."""

    return not np.isfinite(depth_m) or depth_m > gap_depth_min_m


def _first_gap_run(
    column_depth_m: np.ndarray, start: int, step: int, gap_depth_min_m: float
) -> Optional[Tuple[int, int]]:
    """First contiguous gap run scanning outward from the spine edge.

    Cover-depth columns between the edge and the seam are skipped; the run
    returns as inclusive (lo, hi) pixel columns.
    """

    width = int(column_depth_m.shape[0])
    u = int(start)
    while 0 <= u < width and not _is_gap_column(float(column_depth_m[u]), gap_depth_min_m):
        u += step
    if not 0 <= u < width:
        return None
    run_start = u
    while 0 <= u < width and _is_gap_column(float(column_depth_m[u]), gap_depth_min_m):
        u += step
    run_end = u - step
    return (min(run_start, run_end), max(run_start, run_end))


def _choose_side(
    left: Optional[Tuple[int, int]],
    right: Optional[Tuple[int, int]],
    select: str,
    reference_uv: Optional[Tuple[int, int]],
) -> Tuple[str, Tuple[int, int]]:
    if left is None:
        return "right", right  # type: ignore[return-value]
    if right is None:
        return "left", left
    if str(select) == "nearest" and reference_uv is not None:
        left_center = (left[0] + left[1]) / 2.0
        right_center = (right[0] + right[1]) / 2.0
        if abs(left_center - float(reference_uv[0])) <= abs(right_center - float(reference_uv[0])):
            return "left", left
        return "right", right
    # "largest": wider run wins; ties go left for determinism.
    if (left[1] - left[0]) >= (right[1] - right[0]):
        return "left", left
    return "right", right


def _bbox_of(polygon_uv: np.ndarray, width: int, height: int) -> Tuple[int, int, int, int]:
    points = np.asarray(polygon_uv, dtype=float).reshape(-1, 2)
    x0 = max(0, int(np.floor(points[:, 0].min())))
    y0 = max(0, int(np.floor(points[:, 1].min())))
    x1 = min(width - 1, int(np.ceil(points[:, 0].max())))
    y1 = min(height - 1, int(np.ceil(points[:, 1].max())))
    return x0, y0, x1, y1


REASON_NO_SPINE_MATCH = "未在当前画面匹配到参照书脊，请重新准备（识别目标）或检查模板"
REASON_NO_REFERENCE_DEPTH = "参照书脊扫描带内没有有效深度，请调整视角后重试"
REASON_NO_GAP = (
    "参照书脊两侧扫描带内未找到深度突变或空洞的缝隙，"
    "请调整视角或调小 depth_gap_mm 后重试"
)


class DepthGapLocator:
    """Locate the shelf gap beside a recognized spine on the D435 depth map.

    One horizontal scan band is sampled at ``scan_height_ratio`` measured up
    from the bottom of the matched spine bbox. Book covers sit near the
    reference depth (median over the bbox's band columns); a gap column has no
    valid depth or lies deeper than ``reference + depth_gap_mm``. Scanning
    outward from each spine edge yields the first contiguous gap run per side;
    the selected side (``largest`` width, or ``nearest`` to the reference
    pixel) contributes its run midpoint, deprojected at the book-cover
    reference depth — the slot's own deeper reading would only slide the point
    along the pixel ray and skew the base-frame y — and transformed into the
    arm base frame. Each call overwrites
    the debug artifacts under ``debug_dir`` so headless deployments can review
    the latest attempt, mirroring the locator's ``vision_debug`` convention.
    """

    RAW_IMAGE_NAME = "midreturn_gap_depth_raw.jpg"
    OVERLAY_NAME = "midreturn_gap_depth_overlay.jpg"
    META_NAME = "midreturn_gap_depth.json"

    def __init__(
        self,
        camera: Any,
        *,
        debug_dir: Optional[Path] = None,
        matcher_factory: Optional[Callable[[Path], Any]] = None,
    ) -> None:
        self._camera = camera
        self._debug_dir = Path(debug_dir) if debug_dir is not None else None
        self._matcher_factory = matcher_factory or BookSpineMatcher

    def locate_gap(
        self,
        *,
        template: SpineTemplate,
        select: str,
        reference_uv: Optional[Tuple[int, int]],
        scan_height_ratio: float,
        scan_band_px: int,
        depth_gap_mm: float,
    ) -> DepthGap:
        detection = self._detect(
            template=template,
            select=select,
            reference_uv=reference_uv,
            scan_height_ratio=scan_height_ratio,
            scan_band_px=scan_band_px,
            depth_gap_mm=depth_gap_mm,
        )
        if detection["match"] is None:
            raise RuntimeError("深度书缝识别失败：%s" % REASON_NO_SPINE_MATCH)
        if detection["reference_depth_m"] is None:
            raise RuntimeError("深度书缝识别失败：%s" % REASON_NO_REFERENCE_DEPTH)
        if detection["chosen"] is None:
            raise RuntimeError("深度书缝识别失败：%s" % REASON_NO_GAP)
        frame = detection["frame"]
        match = detection["match"]
        x0, y0, x1, y1 = detection["bbox"]
        center_v = detection["center_v"]
        left, right = detection["left"], detection["right"]
        side, run = detection["chosen"]
        reference_depth = detection["reference_depth_m"]
        gap_depth = detection["gap_depth_m"]
        run_lo, run_hi = run
        center_u = int(round((run_lo + run_hi) / 2.0))
        # Deproject the midpoint at the book-cover depth, never at the slot's
        # own reading: the gap's lateral position lives on the book plane, and
        # a deeper depth just slides the 3D point along the pixel ray, which
        # inflates the base-frame y — the only value consumed downstream —
        # by the depth ratio (x/z keep the first-round target anyway).
        camera_point = (
            (center_u - frame.ppx) * reference_depth / frame.fx,
            (center_v - frame.ppy) * reference_depth / frame.fy,
            reference_depth,
        )
        base_point = self._camera.transform_to_base(camera_point)
        return DepthGap(
            base_pose=BasePose(base_point[0], base_point[1], base_point[2], 0.0, 0.0, 0.0),
            pixel_uv=(center_u, center_v),
            side=side,
            gap_width_px=float(run_hi - run_lo + 1),
            left_gap_width_px=None if left is None else float(left[1] - left[0] + 1),
            right_gap_width_px=None if right is None else float(right[1] - right[0] + 1),
            spine_bbox_xyxy=(x0, y0, x1, y1),
            scan_row_v=center_v,
            gap_depth_m=gap_depth,
            reference_depth_m=reference_depth,
            raw_image_path=detection["raw_image_path"],
            overlay_path=detection["overlay_path"],
        )

    def locate_gap_debug(
        self,
        *,
        template: SpineTemplate,
        select: str,
        reference_uv: Optional[Tuple[int, int]],
        scan_height_ratio: float,
        scan_band_px: int,
        depth_gap_mm: float,
    ) -> Dict[str, Any]:
        """Interactive variant for the studio "capture and inspect" button.

        Returns the persisted metadata, including partial results when the
        spine matched but no gap was found (``gap_found`` false + ``reason``),
        so operators can review spine matching alone. Still raises when the
        reference spine itself is not in the frame.
        """

        detection = self._detect(
            template=template,
            select=select,
            reference_uv=reference_uv,
            scan_height_ratio=scan_height_ratio,
            scan_band_px=scan_band_px,
            depth_gap_mm=depth_gap_mm,
        )
        if detection["match"] is None:
            raise RuntimeError("深度书缝识别失败：%s" % REASON_NO_SPINE_MATCH)
        return detection["metadata"]

    def _detect(
        self,
        *,
        template: SpineTemplate,
        select: str,
        reference_uv: Optional[Tuple[int, int]],
        scan_height_ratio: float,
        scan_band_px: int,
        depth_gap_mm: float,
    ) -> Dict[str, Any]:
        """Shared detection: capture, match, scan, persist debug artifacts."""

        frame = self._camera.capture_frame()
        matcher = self._matcher_factory(Path(template.path))
        match = matcher.match(frame.color_bgr)
        base_metadata: Dict[str, Any] = {
            "gap_method": "depth",
            "select": str(select),
            "scan_height_ratio": float(scan_height_ratio),
            "scan_band_px": int(scan_band_px),
            "depth_gap_mm": float(depth_gap_mm),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if match is None:
            metadata = {**base_metadata, "gap_found": False, "reason": REASON_NO_SPINE_MATCH}
            raw_path, overlay_path = self._persist_debug(
                frame.color_bgr,
                match=None,
                bbox=None,
                center_v=None,
                left=None,
                right=None,
                chosen=None,
                metadata=metadata,
            )
            return {
                "frame": frame, "match": None, "metadata": metadata,
                "raw_image_path": raw_path, "overlay_path": overlay_path,
            }

        height, width = frame.depth_m.shape
        x0, y0, x1, y1 = _bbox_of(match.polygon_uv, width, height)
        center_v = int(round(y1 - float(scan_height_ratio) * (y1 - y0)))
        half_band = max(0, int(scan_band_px) // 2)
        row_lo = max(0, center_v - half_band)
        row_hi = min(height, center_v + half_band + 1)

        band = frame.depth_m[row_lo:row_hi, :]
        valid = np.isfinite(band) & (band > 0.0)
        counts = valid.sum(axis=0)
        means = np.where(
            counts > 0,
            np.where(valid, band, 0.0).sum(axis=0) / np.maximum(counts, 1),
            np.nan,
        )

        spine_metadata: Dict[str, Any] = {
            **base_metadata,
            "scan_row_v": center_v,
            "spine_bbox_xyxy": [x0, y0, x1, y1],
            "spine_confidence": float(match.confidence),
        }

        reference_columns = means[x0 : x1 + 1]
        reference_valid = reference_columns[np.isfinite(reference_columns)]
        if reference_valid.size == 0:
            metadata = {**spine_metadata, "gap_found": False, "reason": REASON_NO_REFERENCE_DEPTH}
            raw_path, overlay_path = self._persist_debug(
                frame.color_bgr,
                match=match,
                bbox=(x0, y0, x1, y1),
                center_v=center_v,
                left=None,
                right=None,
                chosen=None,
                metadata=metadata,
            )
            return {
                "frame": frame, "match": match, "bbox": (x0, y0, x1, y1),
                "center_v": center_v, "left": None, "right": None, "chosen": None,
                "reference_depth_m": None, "metadata": metadata,
                "raw_image_path": raw_path, "overlay_path": overlay_path,
            }
        reference_depth = float(np.median(reference_valid))
        gap_depth_min_m = reference_depth + float(depth_gap_mm) / 1000.0

        left = _first_gap_run(means, x0 - 1, -1, gap_depth_min_m)
        right = _first_gap_run(means, x1 + 1, 1, gap_depth_min_m)
        gap_metadata: Dict[str, Any] = {
            **spine_metadata,
            "reference_depth_m": reference_depth,
            "left_gap_width_px": None if left is None else float(left[1] - left[0] + 1),
            "right_gap_width_px": None if right is None else float(right[1] - right[0] + 1),
        }
        if left is None and right is None:
            metadata = {**gap_metadata, "gap_found": False, "reason": REASON_NO_GAP}
            chosen = None
        else:
            side, run = _choose_side(left, right, select, reference_uv)
            run_lo, run_hi = run
            center_u = int(round((run_lo + run_hi) / 2.0))
            run_depths = means[run_lo : run_hi + 1]
            run_valid = run_depths[np.isfinite(run_depths)]
            # Diagnostic only: how deep the see-through slot reads (back wall,
            # or the cover depth for a pure hole).
            gap_depth = float(np.median(run_valid)) if run_valid.size else reference_depth
            camera_point = (
                (center_u - frame.ppx) * reference_depth / frame.fx,
                (center_v - frame.ppy) * reference_depth / frame.fy,
                reference_depth,
            )
            base_point = self._camera.transform_to_base(camera_point)
            chosen = (side, run)
            metadata = {
                **gap_metadata,
                "gap_found": True,
                "gap_depth_m": gap_depth,
                "deproject_depth_m": reference_depth,
                "selected_side": side,
                "gap_width_px": float(run_hi - run_lo + 1),
                "gap_center_uv": [center_u, center_v],
                "y1_m": float(base_point[1]),
                "camera_point_m": [float(value) for value in camera_point],
                "base_point_m": [float(value) for value in base_point],
            }
        raw_path, overlay_path = self._persist_debug(
            frame.color_bgr,
            match=match,
            bbox=(x0, y0, x1, y1),
            center_v=center_v,
            left=left,
            right=right,
            chosen=chosen,
            metadata=metadata,
        )
        return {
            "frame": frame,
            "match": match,
            "bbox": (x0, y0, x1, y1),
            "center_v": center_v,
            "left": left,
            "right": right,
            "chosen": chosen,
            "reference_depth_m": reference_depth,
            "gap_depth_m": metadata.get("gap_depth_m", reference_depth),
            "metadata": metadata,
            "raw_image_path": raw_path,
            "overlay_path": overlay_path,
        }

    def _persist_debug(
        self,
        image_bgr: np.ndarray,
        *,
        match: Any,
        bbox: Optional[Tuple[int, int, int, int]],
        center_v: Optional[int],
        left: Optional[Tuple[int, int]],
        right: Optional[Tuple[int, int]],
        chosen: Optional[Tuple[str, Tuple[int, int]]],
        metadata: dict,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Persist raw frame, annotated overlay and metadata; never alter flow."""

        if self._debug_dir is None:
            return None, None
        try:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            raw_path = self._debug_dir / self.RAW_IMAGE_NAME
            overlay_path = self._debug_dir / self.OVERLAY_NAME
            overlay = image_bgr.copy()
            if match is not None and bbox is not None:
                x0, y0, x1, y1 = bbox
                polygon = np.asarray(match.polygon_uv, dtype=np.int32).reshape(-1, 2)
                cv2.polylines(overlay, [polygon], True, (0, 255, 0), 2)
                cv2.putText(
                    overlay,
                    "conf %.2f" % float(match.confidence),
                    (x0, max(16, y0 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 255, 0),
                    1,
                    cv2.LINE_AA,
                )
            if center_v is not None:
                cv2.line(overlay, (0, center_v), (overlay.shape[1] - 1, center_v), (255, 255, 0), 1)
                for run, color in ((left, (255, 0, 0)), (right, (0, 0, 255))):
                    if run is not None:
                        cv2.rectangle(
                            overlay, (run[0], center_v - 8), (run[1], center_v + 8), color, 1
                        )
            if chosen is not None and center_v is not None:
                _side, run = chosen
                center_u = int(round((run[0] + run[1]) / 2.0))
                cv2.drawMarker(
                    overlay, (center_u, center_v), (0, 255, 255), cv2.MARKER_CROSS, 16, 2
                )
            cv2.imwrite(str(raw_path), image_bgr)
            cv2.imwrite(str(overlay_path), overlay)
            (self._debug_dir / self.META_NAME).write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return str(raw_path), str(overlay_path)
        except Exception as error:
            import sys

            print(
                "[WARN] depth-gap debug persistence failed; continuing without it: %s" % error,
                file=sys.stderr,
            )
            return None, None
