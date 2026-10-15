"""Tests for the non-blocking book-spine HighGUI diagnostic view."""

from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from bookarm.devices.camera import visualization
from bookarm.devices.camera.matcher import SpineMatch
from bookarm.devices.camera.service import D435BookSpineLocator, D435BookSpineLocatorConfig
from bookarm.domain.models.book import SpineTemplate


class CameraVisualizationTests(unittest.TestCase):
    def test_display_switches_automatically_from_comparison_to_match_overlay(self) -> None:
        image = np.zeros((16, 24, 3), dtype=np.uint8)
        template = np.zeros((8, 6, 3), dtype=np.uint8)
        match = SpineMatch(
            polygon_uv=np.array(((2, 2), (12, 2), (12, 14), (2, 14)), dtype=float),
            confidence=0.9,
        )
        with (
            patch.dict(os.environ, {"DISPLAY": ":0"}, clear=False),
            patch.object(visualization.cv2, "imread", return_value=template),
            patch.object(visualization.cv2, "namedWindow") as named_window,
            patch.object(visualization.cv2, "imshow") as imshow,
            patch.object(visualization.cv2, "waitKey", return_value=-1) as wait_key,
            patch.object(visualization.cv2, "destroyWindow") as destroy_window,
        ):
            details = visualization.display_book_spine_detection(
                image,
                Path("template.jpg"),
                match,
                duration_ms=17,
            )

        named_window.assert_called_once()
        self.assertEqual(imshow.call_count, 2)
        self.assertEqual(wait_key.call_args_list[0].args, (17,))
        self.assertEqual(wait_key.call_args_list[1].args, (17,))
        destroy_window.assert_called_once()
        self.assertTrue(details["matched"])
        self.assertEqual(details["grasp_pixel_uv"], (7, 4))

    def test_display_requires_a_positive_automatic_duration(self) -> None:
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            visualization.display_book_spine_detection(
                np.zeros((2, 2, 3), dtype=np.uint8),
                Path("template.jpg"),
                None,
                duration_ms=0,
            )

    def test_locator_timeout_includes_both_automatic_views(self) -> None:
        locator = D435BookSpineLocator(
            D435BookSpineLocatorConfig(
                frame_timeout_ms=1000,
                max_attempts=3,
                visualize=True,
                visualize_duration_ms=250,
            )
        )
        locator._worker.start()
        try:
            with patch.object(locator._worker, "call", wraps=locator._worker.call) as worker_call:
                with patch.object(locator, "_locate_book", side_effect=RuntimeError("expected")):
                    with self.assertRaisesRegex(RuntimeError, "expected"):
                        locator.locate_book(SpineTemplate(Path("template.jpg")))
        finally:
            locator._worker.stop()

        self.assertEqual(worker_call.call_args.kwargs["timeout_s"], 5.5)


if __name__ == "__main__":
    unittest.main()
