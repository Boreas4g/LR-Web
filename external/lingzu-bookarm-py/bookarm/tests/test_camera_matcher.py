"""Tests for configurable book-spine grasp-point placement."""

from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

import cv2
import numpy as np

from bookarm.devices.camera.matcher import BookSpineMatcher


class BookSpineMatcherPointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.polygon = np.array(((20, 10), (40, 10), (40, 70), (20, 70)), dtype=float)

    def test_pick_point_supports_supported_top_down_positions(self) -> None:
        self.assertEqual(BookSpineMatcher.pick_point(self.polygon, 1.0 / 6.0), (30, 20))
        self.assertEqual(BookSpineMatcher.pick_point(self.polygon, 1.0 / 5.0), (30, 22))
        self.assertEqual(BookSpineMatcher.pick_point(self.polygon, 2.0 / 5.0), (30, 34))
        self.assertEqual(BookSpineMatcher.pick_point(self.polygon, 1.0 / 4.0), (30, 25))
        self.assertEqual(BookSpineMatcher.pick_point(self.polygon, 1.0 / 3.0), (30, 30))
        self.assertEqual(BookSpineMatcher.pick_point(self.polygon, 1.0 / 2.0), (30, 40))
        self.assertEqual(BookSpineMatcher.pick_point(self.polygon, 2.0 / 3.0), (30, 50))

    def test_pick_point_defaults_to_one_sixth(self) -> None:
        self.assertEqual(
            BookSpineMatcher.pick_point(self.polygon),
            BookSpineMatcher.pick_point(self.polygon, BookSpineMatcher.TOP_DOWN_ONE_SIXTH),
        )

    def test_pick_point_rejects_invalid_top_down_position(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 0.0 and 1.0"):
            BookSpineMatcher.pick_point(self.polygon, 1.1)

    def test_pick_point_supports_one_tenth_from_top(self) -> None:
        self.assertEqual(
            BookSpineMatcher.pick_point(
                self.polygon,
                BookSpineMatcher.TOP_DOWN_ONE_TENTH,
            ),
            (30, 16),
        )


def _textured_spine(width: int = 180, height: int = 1440, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = rng.integers(90, 160, (height, width), dtype=np.uint8)
    image = cv2.GaussianBlur(image, (5, 5), 0)
    for _ in range(60):
        x, y = rng.integers(0, width - 40), rng.integers(0, height - 80)
        w, h = rng.integers(8, 40), rng.integers(8, 80)
        cv2.rectangle(image, (x, y), (x + w, y + h), int(rng.integers(0, 255)), -1)
    for y in range(0, height, 10):
        cv2.line(image, (0, y), (width, y), 20, 1)
    return image


def _frame_with(template: np.ndarray, scale: float) -> tuple[np.ndarray, tuple[float, float], float]:
    """Plant the template at (500, 120) of a 1280x720 frame; return center+height."""

    height, width = template.shape
    small = cv2.resize(
        template,
        (max(8, int(width * scale)), max(8, int(height * scale))),
        interpolation=cv2.INTER_AREA,
    )
    rng = np.random.default_rng(3)
    frame = rng.integers(100, 140, (720, 1280), dtype=np.uint8)
    sh, sw = small.shape
    y0, x0 = 120, 500
    frame[y0 : y0 + sh, x0 : x0 + sw] = small
    frame = cv2.GaussianBlur(frame, (3, 3), 0)
    center = (x0 + sw / 2.0, y0 + sh / 2.0)
    return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR), center, float(sh)


def _write_image(directory: str, name: str, image: np.ndarray) -> Path:
    path = Path(directory) / name
    cv2.imwrite(str(path), image)
    return path


class BookSpineMatcherMatchTests(unittest.TestCase):
    def test_multiscale_match_places_polygon_on_the_planted_spine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            template = _textured_spine()
            frame, (cx, cy), planted_height = _frame_with(template, 1.0 / 6.0)
            path = _write_image(directory, "spine.png", template)

            match = BookSpineMatcher(path).match(frame)

            self.assertIsNotNone(match)
            assert match is not None
            polygon = match.polygon_uv.reshape(4, 2)
            self.assertAlmostEqual(float(polygon[:, 0].mean()), cx, delta=30.0)
            self.assertAlmostEqual(float(polygon[:, 1].mean()), cy, delta=30.0)
            polygon_height = abs(float(polygon[2, 1] - polygon[0, 1]))
            self.assertAlmostEqual(polygon_height, planted_height, delta=planted_height * 0.25)

    def test_clahe_recovers_a_low_contrast_match(self) -> None:
        spine = np.full((1400, 200), 140, dtype=np.uint8)
        y = 80
        for char in ["书", "脊", "测", "试", "封", "面", "出", "版", "社"]:
            cv2.putText(
                spine, char, (30, y), cv2.FONT_HERSHEY_SIMPLEX, 1.6, 40, 3, cv2.LINE_AA
            )
            y += 150

        def degrade(image: np.ndarray, keep: float) -> np.ndarray:
            return np.clip(
                image.astype(np.float32) * keep + 128 * (1 - keep), 0, 255
            ).astype(np.uint8)

        template = degrade(spine, 0.20)
        height, width = template.shape
        small = degrade(
            cv2.resize(spine, (width // 3, height // 3), interpolation=cv2.INTER_AREA),
            0.15,
        )
        frame = np.full((720, 1280), 128, dtype=np.uint8)
        frame[120 : 120 + small.shape[0], 500 : 500 + small.shape[1]] = small
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        with tempfile.TemporaryDirectory() as directory:
            path = _write_image(directory, "spine.png", template)

            self.assertIsNone(BookSpineMatcher(path, use_clahe=False).match(frame))
            self.assertIsNotNone(BookSpineMatcher(path, use_clahe=True).match(frame))

    def test_correlation_fallback_locates_a_featureless_spine(self) -> None:
        rng = np.random.default_rng(5)
        template = np.zeros((900, 160), dtype=np.uint8)
        for row in range(900):
            template[row, :] = int(60 + 140 * row / 900)
        template = cv2.GaussianBlur(template, (9, 9), 0)
        # One small textured patch keeps template init happy; far too few
        # features for the SIFT pipeline itself to match.
        template[30:90, 30:90] = rng.integers(0, 255, (60, 60), dtype=np.uint8)
        height, width = template.shape
        small = cv2.resize(
            template, (width // 3, height // 3), interpolation=cv2.INTER_AREA
        )
        frame = np.full((720, 1280), 110, dtype=np.uint8)
        frame[150 : 150 + small.shape[0], 600 : 600 + small.shape[1]] = small
        frame = cv2.GaussianBlur(frame, (5, 5), 0)
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        with tempfile.TemporaryDirectory() as directory:
            path = _write_image(directory, "spine.png", template)

            self.assertIsNone(
                BookSpineMatcher(path, use_correlation_fallback=False).match(frame)
            )
            match = BookSpineMatcher(path).match(frame)

            self.assertIsNotNone(match)
            assert match is not None
            polygon = match.polygon_uv.reshape(4, 2)
            self.assertAlmostEqual(float(polygon[:, 0].mean()), 626.5, delta=15.0)
            self.assertAlmostEqual(float(polygon[:, 1].mean()), 300.0, delta=15.0)

    def test_blank_template_raises_insufficient_features(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_image(directory, "blank.png", np.zeros((200, 100), dtype=np.uint8))
            with self.assertRaisesRegex(ValueError, "insufficient visual features"):
                BookSpineMatcher(path)

    def test_missing_template_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            BookSpineMatcher(Path("/nonexistent/spine-template.png"))


if __name__ == "__main__":
    unittest.main()
