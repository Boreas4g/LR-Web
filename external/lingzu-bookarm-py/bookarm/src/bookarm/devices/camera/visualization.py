"""Optional OpenCV HighGUI diagnostics for the book-spine locator."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

from bookarm.devices.camera.matcher import BookSpineMatcher, SpineMatch


def graphical_display_available() -> bool:
    """Return whether the current process has a graphical display target."""

    return os.name == "nt" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _resize_for_display(image: np.ndarray, max_width: int, max_height: int) -> np.ndarray:
    height, width = image.shape[:2]
    scale = min(1.0, float(max_width) / width, float(max_height) / height)
    if scale == 1.0:
        return image.copy()
    return cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)


def _labeled_display_image(image: np.ndarray, label: str, width: int, height: int) -> np.ndarray:
    canvas = np.full((height + 40, width, 3), 245, dtype=np.uint8)
    resized = _resize_for_display(image, width, height)
    image_height, image_width = resized.shape[:2]
    x = (width - image_width) // 2
    y = 40 + (height - image_height) // 2
    canvas[y : y + image_height, x : x + image_width] = resized
    cv2.putText(canvas, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2, cv2.LINE_AA)
    return canvas


def _match_overlay(
    image_bgr: np.ndarray,
    match: Optional[SpineMatch],
    grasp_point_from_top_ratio: float,
    marker_pixel_uv: Optional[tuple[int, int]] = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    overlay = image_bgr.copy()
    if match is None:
        cv2.putText(
            overlay,
            "No matching book spine found",
            (24, 48),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (0, 0, 255),
            2,
            cv2.LINE_AA,
        )
        return overlay, {"matched": False}

    polygon = np.asarray(match.polygon_uv, dtype=np.int32).reshape(-1, 1, 2)
    grasp_pixel = BookSpineMatcher.pick_point(match.polygon_uv, grasp_point_from_top_ratio)
    display_pixel = marker_pixel_uv or grasp_pixel
    cv2.polylines(overlay, [polygon], True, (0, 255, 0), 4, cv2.LINE_AA)
    cv2.circle(overlay, display_pixel, 8, (0, 0, 255), -1, cv2.LINE_AA)
    cv2.putText(
        overlay,
        "Book spine match: %.3f" % match.confidence,
        (24, 48),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (0, 255, 0),
        2,
        cv2.LINE_AA,
    )
    return overlay, {
        "matched": True,
        "confidence": float(match.confidence),
        "polygon_uv": match.polygon_uv,
        "grasp_pixel_uv": grasp_pixel,
        "display_pixel_uv": display_pixel,
    }


def display_book_spine_detection(
    image_bgr: np.ndarray,
    template_path: Path,
    match: Optional[SpineMatch],
    *,
    duration_ms: int,
    grasp_point_from_top_ratio: float = BookSpineMatcher.TOP_DOWN_ONE_SIXTH,
    marker_pixel_uv: Optional[tuple[int, int]] = None,
) -> dict[str, Any]:
    """Automatically show capture/template comparison and the matching overlay.

    This function never waits for a user key press. ``duration_ms`` applies to
    each of the two views so the caller can continue deterministically.
    """

    duration = int(duration_ms)
    if duration <= 0:
        raise ValueError("book-spine visualization duration must be greater than zero")
    if not graphical_display_available():
        raise RuntimeError("no graphical display is available for book-spine visualization")

    template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
    if template is None:
        raise RuntimeError("cannot read book-spine template image: %s" % template_path)

    panel_width, panel_height = 800, 600
    comparison = np.hstack(
        (
            _labeled_display_image(image_bgr, "D435 capture", panel_width, panel_height),
            _labeled_display_image(template, "Target book-spine template", panel_width, panel_height),
        )
    )
    overlay, details = _match_overlay(
        image_bgr,
        match,
        grasp_point_from_top_ratio,
        marker_pixel_uv,
    )
    window_name = "BookArm book-spine detection"
    window_created = False
    try:
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        window_created = True
        cv2.imshow(window_name, comparison)
        cv2.waitKey(duration)
        cv2.imshow(window_name, overlay)
        cv2.waitKey(duration)
        return {
            "comparison_shape": comparison.shape,
            "template_shape": template.shape,
            "display_duration_ms": duration,
            **details,
        }
    finally:
        if window_created:
            try:
                cv2.destroyWindow(window_name)
            except cv2.error:
                # A user may close the window before automatic cleanup.
                pass
