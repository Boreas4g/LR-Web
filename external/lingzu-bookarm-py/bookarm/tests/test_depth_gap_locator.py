"""Depth-map gap locator tests with synthetic D435 frames."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from bookarm.devices.camera.depth_gap import DepthGapLocator
from bookarm.devices.camera.d435_device import D435Frame
from bookarm.devices.camera.matcher import SpineMatch
from bookarm.domain.models.book import SpineTemplate


WIDTH, HEIGHT = 640, 480
FX = FY = 600.0
PPX, PPY = 320.0, 240.0
# Spine bbox in every scene: x 200..260, y 100..400.
BBOX = (200, 100, 260, 400)
BOOK_DEPTH_M = 0.50


def make_frame(depth_m: np.ndarray) -> D435Frame:
    return D435Frame(
        color_bgr=np.zeros((HEIGHT, WIDTH, 3), np.uint8),
        depth_m=depth_m.astype(np.float32),
        fx=FX,
        fy=FY,
        ppx=PPX,
        ppy=PPY,
        timestamp_ms=0.0,
        frame_number=1,
    )


class FakeCamera:
    """capture_frame + identity camera-to-base transform."""

    def __init__(self, frame: D435Frame) -> None:
        self._frame = frame

    def capture_frame(self) -> D435Frame:
        return self._frame

    def transform_to_base(self, point_camera_m):
        return tuple(float(value) for value in point_camera_m)


class StubMatcher:
    def __init__(self, match) -> None:
        self._match = match

    def match(self, image_bgr):
        return self._match


def spine_match():
    x0, y0, x1, y1 = BBOX
    return SpineMatch(
        polygon_uv=np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=float),
        confidence=0.9,
    )


_DEFAULT_MATCH = object()


def build_locator(depth_m, match=_DEFAULT_MATCH, **kwargs) -> DepthGapLocator:
    return DepthGapLocator(
        FakeCamera(make_frame(depth_m)),
        matcher_factory=lambda _path: StubMatcher(spine_match() if match is _DEFAULT_MATCH else match),
        **kwargs,
    )


def locate(locator: DepthGapLocator, **overrides):
    params = {
        "template": SpineTemplate(Path("spine.png")),
        "select": "largest",
        "reference_uv": (230, 200),
        "scan_height_ratio": 0.67,
        "scan_band_px": 5,
        "depth_gap_mm": 15.0,
    }
    params.update(overrides)
    return locator.locate_gap(**params)


def base_scene() -> np.ndarray:
    """Books at 0.50 m; left gap 20 px deeper at 0.62 m; right gap 11 px hole."""

    depth = np.full((HEIGHT, WIDTH), BOOK_DEPTH_M, dtype=np.float32)
    depth[:, 170:190] = 0.62
    depth[:, 261:272] = 0.0
    return depth


class DepthGapLocatorTests(unittest.TestCase):
    def test_finds_both_gaps_and_selects_the_wider_left_side(self):
        with tempfile.TemporaryDirectory() as directory:
            locator = build_locator(base_scene(), debug_dir=Path(directory))

            gap = locate(locator)

            self.assertEqual(gap.side, "left")
            self.assertEqual(gap.gap_width_px, 20.0)
            self.assertEqual(gap.left_gap_width_px, 20.0)
            self.assertEqual(gap.right_gap_width_px, 11.0)
            # Run midpoint of columns 170..189 on the scan row.
            self.assertEqual(gap.pixel_uv, (180, 199))
            self.assertEqual(gap.scan_row_v, 199)
            self.assertAlmostEqual(gap.reference_depth_m, BOOK_DEPTH_M)
            self.assertAlmostEqual(gap.gap_depth_m, 0.62)
            # Deprojection runs at the book-cover depth through the identity
            # transform; the deeper slot reading stays a diagnostic only.
            self.assertAlmostEqual(gap.base_pose.x_m, (180 - PPX) * BOOK_DEPTH_M / FX)
            self.assertAlmostEqual(gap.base_pose.y_m, (199 - PPY) * BOOK_DEPTH_M / FY)
            self.assertAlmostEqual(gap.base_pose.z_m, BOOK_DEPTH_M)
            self.assertEqual(gap.spine_bbox_xyxy, BBOX)
            # Debug artifacts land in the debug dir.
            self.assertTrue(Path(gap.raw_image_path).is_file())
            self.assertTrue(Path(gap.overlay_path).is_file())
            self.assertTrue((Path(directory) / locator.META_NAME).is_file())

    def test_nearest_select_picks_the_gap_closer_to_the_reference_pixel(self):
        locator = build_locator(base_scene())

        # Reference near the right gap (center 266) beats the left one (180).
        gap = locate(locator, select="nearest", reference_uv=(300, 200))

        self.assertEqual(gap.side, "right")
        self.assertEqual(gap.gap_width_px, 11.0)
        self.assertEqual(gap.pixel_uv, (266, 199))
        # The right gap is a pure depth hole: it deprojects at the cover depth.
        self.assertAlmostEqual(gap.gap_depth_m, BOOK_DEPTH_M)

    def test_scan_height_ratio_moves_the_scan_row(self):
        locator = build_locator(base_scene())

        gap = locate(locator, scan_height_ratio=0.5)

        self.assertEqual(gap.scan_row_v, 250)
        self.assertEqual(gap.pixel_uv[1], 250)

    def test_depth_gap_threshold_gates_shallow_dips(self):
        depth = base_scene()
        depth[:, 261:272] = BOOK_DEPTH_M + 0.008  # 8 mm dip, below the 15 mm jump
        locator = build_locator(depth)

        gap = locate(locator)
        self.assertEqual(gap.side, "left")
        self.assertIsNone(gap.right_gap_width_px)

        sensitive = locate(locator, depth_gap_mm=5.0)
        self.assertEqual(sensitive.side, "left")  # still wider
        self.assertEqual(sensitive.right_gap_width_px, 11.0)

    def test_single_sided_open_shelf_end_is_accepted(self):
        depth = base_scene()
        depth[:, 170:190] = BOOK_DEPTH_M  # close the left gap
        locator = build_locator(depth)

        gap = locate(locator)

        self.assertEqual(gap.side, "right")
        self.assertIsNone(gap.left_gap_width_px)
        self.assertEqual(gap.right_gap_width_px, 11.0)

    def test_no_gap_raises_a_clear_error(self):
        depth = np.full((HEIGHT, WIDTH), BOOK_DEPTH_M, dtype=np.float32)
        locator = build_locator(depth)

        with self.assertRaisesRegex(RuntimeError, "未找到深度突变或空洞的缝隙"):
            locate(locator)

    def test_gap_info_is_json_serializable_for_the_tuning_session(self):
        # Regression: the tuning session API ships gap.info() verbatim; a
        # DepthGap without info() 500'd both the gap step and the session view.
        locator = build_locator(base_scene())

        info = locate(locator).info()

        self.assertEqual(json.loads(json.dumps(info, ensure_ascii=False)), info)
        self.assertEqual(len(info["gap_base_pose"]), 6)
        self.assertEqual(info["gap_pixel_uv"], [180, 199])
        self.assertEqual(info["gap_width_px"], 20.0)
        self.assertEqual(info["selected_side"], "left")
        self.assertEqual(info["left_gap_width_px"], 20.0)
        self.assertEqual(info["right_gap_width_px"], 11.0)
        self.assertEqual(info["spine_bbox_xyxy"], [200, 100, 260, 400])
        self.assertEqual(info["scan_row_v"], 199)
        self.assertAlmostEqual(info["gap_depth_m"], 0.62)
        self.assertAlmostEqual(info["reference_depth_m"], BOOK_DEPTH_M)
        self.assertEqual(info["method"], "depth")
        self.assertIsNone(info["raw_image_path"])
        self.assertIsNone(info["overlay_path"])

    def test_unmatched_spine_raises_a_clear_error(self):
        locator = build_locator(base_scene(), match=None)

        with self.assertRaisesRegex(RuntimeError, "未在当前画面匹配到参照书脊"):
            locate(locator)

    def test_spine_without_valid_depth_raises_a_clear_error(self):
        depth = base_scene()
        x0, y0, x1, y1 = BBOX
        depth[y0 : y1 + 1, x0 : x1 + 1] = 0.0
        locator = build_locator(depth)

        with self.assertRaisesRegex(RuntimeError, "没有有效深度"):
            locate(locator)


def locate_debug(locator: DepthGapLocator, **overrides):
    params = {
        "template": SpineTemplate(Path("spine.png")),
        "select": "largest",
        "reference_uv": (230, 200),
        "scan_height_ratio": 0.67,
        "scan_band_px": 5,
        "depth_gap_mm": 15.0,
    }
    params.update(overrides)
    return locator.locate_gap_debug(**params)


class DepthGapDebugTests(unittest.TestCase):
    """Interactive studio variant: partial results instead of errors."""

    def test_success_reports_gap_found(self):
        with tempfile.TemporaryDirectory() as directory:
            locator = build_locator(base_scene(), debug_dir=Path(directory))

            meta = locate_debug(locator)

            self.assertTrue(meta["gap_found"])
            self.assertEqual(meta["selected_side"], "left")
            self.assertEqual(meta["gap_width_px"], 20.0)
            self.assertEqual(meta["spine_bbox_xyxy"], list(BBOX))

    def test_no_gap_returns_partial_metadata_with_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            depth = np.full((HEIGHT, WIDTH), BOOK_DEPTH_M, dtype=np.float32)
            locator = build_locator(depth, debug_dir=Path(directory))

            meta = locate_debug(locator)

            self.assertFalse(meta["gap_found"])
            self.assertIn("未找到深度突变或空洞的缝隙", meta["reason"])
            self.assertEqual(meta["spine_bbox_xyxy"], list(BBOX))
            self.assertAlmostEqual(meta["spine_confidence"], 0.9)
            self.assertAlmostEqual(meta["reference_depth_m"], BOOK_DEPTH_M)
            self.assertTrue((Path(directory) / locator.RAW_IMAGE_NAME).is_file())
            self.assertTrue((Path(directory) / locator.OVERLAY_NAME).is_file())
            self.assertTrue((Path(directory) / locator.META_NAME).is_file())

    def test_no_spine_match_still_raises_but_keeps_the_raw_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            locator = build_locator(base_scene(), match=None, debug_dir=Path(directory))

            with self.assertRaisesRegex(RuntimeError, "未在当前画面匹配到参照书脊"):
                locate_debug(locator)

            self.assertTrue((Path(directory) / locator.RAW_IMAGE_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
