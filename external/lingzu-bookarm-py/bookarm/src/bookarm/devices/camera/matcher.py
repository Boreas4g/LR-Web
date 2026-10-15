"""OpenCV template matching for book spines, independent of the old GUI."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Optional, Sequence, Tuple

import cv2
import numpy as np


@dataclass(frozen=True)
class SpineMatch:
    polygon_uv: np.ndarray
    confidence: float


@dataclass(frozen=True)
class _TemplateVariant:
    """One scaled, contrast-enhanced view of the template with its features."""

    scale: float
    enhanced: np.ndarray
    keypoints: tuple
    descriptors: np.ndarray
    width: int
    height: int


# Phone close-up templates are 6-10x larger than a spine in the D435 frame;
# SIFT's scale invariance breaks down at those ratios because the small-print
# features that dominate the template simply do not exist in the frame. A
# downscale pyramid restores the overlap: every variant is matched
# independently and the best verified homography wins. The pyramid is dense
# in the 0.10-0.50 band where real shelf spines land.
DEFAULT_TEMPLATE_SCALES = (
    1.0, 0.7, 0.55, 0.45, 0.4, 0.35, 0.3, 0.27, 0.24, 0.21, 0.18, 0.16, 0.14,
    0.12, 0.10,
)
# Small variants carry few keypoints but are exactly where thin spines match;
# only drop variants too small for SIFT to form any keypoint at all.
MIN_TEMPLATE_SIDE_PX = 12
MIN_CORRELATION_SIDE_PX = 10
# Verification gates, tuned for precision over recall: a match must survive
# geometry (convexity, size, aspect) AND photometry (NCC of the warped patch).
RANSAC_REPROJ_THRESHOLD_PX = 3.0
RANSAC_MAX_ITERS = 10000
RANSAC_CONFIDENCE = 0.995
# Fixed seed makes RANSAC deterministic: identical inputs, identical verdict.
RANSAC_RNG_SEED = 12345
MIN_INLIERS = 5
MIN_INLIER_RATIO = 0.40
MAX_FRAME_AREA_RATIO = 0.15
ASPECT_TOLERANCE_MIN = 0.4
ASPECT_TOLERANCE_MAX = 2.5
MIN_NCC = 0.45
# A candidate below the strict NCC gate is still accepted when at least one
# other scale independently lands on the same spot: specular glare can wash
# a real spine's patch below the strict gate, and unrelated false candidates
# almost never agree on one location across scales.
RELAXED_MIN_NCC = 0.30
CONSENSUS_DX_PX = 30.0
CONSENSUS_DY_PX = 50.0
# Correlation fallback (featureless spines): multi-scale TM_CCOEFF_NORMED
# sweep used only when the SIFT pipeline finds no verified match. The bar is
# deliberately high: light-colored low-texture spines score 0.6-0.7 against
# ANY pale surface (white books, shelf panels, walls), so only near-identical
# appearances may pass, and only when independent scales agree (see below).
MIN_CORRELATION = 0.75
# Horizontal region of interest (fractions of frame width). The camera looks
# at the shelf head-on, so books always land in the middle band; the frame
# edges hold spine-printed packaging and cabling that SIFT can confuse with
# real books. Measured on live frames: books span ~0.41-0.82, edge clutter
# sits below ~0.24 and above ~0.89.
DEFAULT_ROI_X = (0.30, 0.86)


def _rootsift(descriptors: np.ndarray) -> np.ndarray:
    """L1-normalise + sqrt (Hellinger kernel); measurably better SIFT matches."""

    root = descriptors.astype(np.float32)
    root /= np.linalg.norm(root, axis=1, ord=1, keepdims=True) + 1e-7
    return np.sqrt(root)


def _is_convex(polygon: np.ndarray) -> bool:
    """All four corners turn the same way (rejects twisted homographies)."""

    points = polygon.reshape(4, 2).astype(np.float64)
    sign = 0.0
    for index in range(4):
        a, b, c = points[index], points[(index + 1) % 4], points[(index + 2) % 4]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if cross == 0.0:
            return False
        current = 1.0 if cross > 0 else -1.0
        if sign == 0.0:
            sign = current
        elif current != sign:
            return False
    return True


class BookSpineMatcher:
    """Feature/homography matcher selected to tolerate camera perspective.

    Frame features are only detected inside a horizontal region of interest
    (the shelf band), so spine-printed packaging and cabling at the frame
    edges cannot produce matches. Every candidate homography is verified
    geometrically (convexity, ROI, frame-area and aspect-ratio bounds) and
    photometrically (normalized cross-correlation between the template
    variant and the warped frame patch); a candidate with weak photometry is
    still accepted when independent scales agree on the same location. The
    pipeline trades computation for precision: wrong books and degenerate
    matches are rejected instead of reported.
    """

    # Fractions measured down from the matched spine's top edge.
    TOP_DOWN_ONE_TENTH = 1.0 / 10.0
    TOP_DOWN_ONE_SIXTH = 1.0 / 6.0
    TOP_DOWN_ONE_FIFTH = 1.0 / 5.0
    TOP_DOWN_TWO_FIFTHS = 2.0 / 5.0
    TOP_DOWN_ONE_QUARTER = 1.0 / 4.0
    TOP_DOWN_ONE_THIRD = 1.0 / 3.0
    TOP_DOWN_ONE_HALF = 1.0 / 2.0
    TOP_DOWN_TWO_THIRDS = 2.0 / 3.0

    def __init__(
        self,
        template_path: Path,
        ratio_test: float = 0.80,
        min_matches: int = 6,
        *,
        scales: Sequence[float] = DEFAULT_TEMPLATE_SCALES,
        use_clahe: bool = True,
        min_inliers: int = MIN_INLIERS,
        min_inlier_ratio: float = MIN_INLIER_RATIO,
        min_ncc: float = MIN_NCC,
        use_correlation_fallback: bool = True,
        min_correlation: float = MIN_CORRELATION,
        roi_x: Tuple[float, float] = DEFAULT_ROI_X,
    ) -> None:
        template = cv2.imread(str(Path(template_path)), cv2.IMREAD_GRAYSCALE)
        if template is None or template.size == 0:
            raise FileNotFoundError("unable to read book spine template: %s" % template_path)
        roi_lo, roi_hi = float(roi_x[0]), float(roi_x[1])
        if not 0.0 <= roi_lo < roi_hi <= 1.0:
            raise ValueError("roi_x must satisfy 0 <= lo < hi <= 1")
        self._roi_x = (roi_lo, roi_hi)
        self._template_aspect = float(template.shape[1]) / float(template.shape[0])
        self._ratio_test = float(ratio_test)
        self._min_matches = max(4, int(min_matches))
        self._min_inliers = max(4, int(min_inliers))
        self._min_inlier_ratio = float(min_inlier_ratio)
        self._min_ncc = float(min_ncc)
        self._use_correlation_fallback = bool(use_correlation_fallback)
        self._min_correlation = float(min_correlation)
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)) if use_clahe else None
        self._detector, self._norm = self._make_detector()
        self._variants: list[_TemplateVariant] = []
        self._correlation_variants: list[_TemplateVariant] = []
        for scale in sorted({float(value) for value in scales if value > 0.0}, reverse=True):
            if scale == 1.0:
                variant_image = template
            else:
                variant_image = cv2.resize(
                    template, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
                )
            height, width = variant_image.shape[:2]
            enhanced = self._enhance(variant_image)
            keypoints, descriptors = self._detector.detectAndCompute(enhanced, None)
            variant = _TemplateVariant(
                scale=float(scale),
                enhanced=enhanced,
                keypoints=tuple(keypoints) if descriptors is not None else (),
                descriptors=_rootsift(descriptors) if descriptors is not None else np.empty((0, 128), dtype=np.float32),
                width=int(width),
                height=int(height),
            )
            # The NCC sweep does not need keypoints: weak-texture variants that
            # SIFT skips are exactly the ones the fallback exists for.
            if min(height, width) >= MIN_CORRELATION_SIDE_PX:
                self._correlation_variants.append(variant)
            if min(height, width) < MIN_TEMPLATE_SIDE_PX:
                continue
            if descriptors is None or len(keypoints) < 4:
                continue
            self._variants.append(variant)
        if not self._variants:
            raise ValueError("book spine template has insufficient visual features")

    @property
    def template_feature_count(self) -> int:
        """Keypoints kept across the usable template scale variants.

        Exposed for the library self-check: a successfully built matcher with
        a healthy feature budget is very likely to be found at take-out time.
        """

        return sum(len(variant.keypoints) for variant in self._variants)

    def match(self, image_bgr: np.ndarray) -> Optional[SpineMatch]:
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        self._frame_enhanced = self._enhance(gray)
        frame_height, frame_width = gray.shape
        roi_x0 = int(round(self._roi_x[0] * frame_width))
        roi_x1 = int(round(self._roi_x[1] * frame_width))
        # Detect inside the horizontal ROI only: edge clutter (spine-printed
        # packaging, cables) cannot anchor a false homography, and the SIFT
        # feature budget is spent on the shelf region where books live.
        # Keypoint coordinates are shifted back to full-frame coordinates so
        # homographies and NCC warps stay in the caller's frame.
        roi_enhanced = self._frame_enhanced[:, roi_x0:roi_x1]
        keypoints, descriptors = self._detector.detectAndCompute(roi_enhanced, None)
        if keypoints:
            keypoints = list(keypoints)
            for keypoint in keypoints:
                keypoint.pt = (keypoint.pt[0] + roi_x0, keypoint.pt[1])
        candidates: list[Tuple[int, float, int, np.ndarray]] = []
        if descriptors is not None and len(keypoints) >= 4:
            frame_descriptors = _rootsift(descriptors)
            for variant in self._variants:
                candidate = self._match_variant(variant, keypoints, frame_descriptors, image_bgr.shape)
                if candidate is not None:
                    candidates.append(candidate)
        best = self._verify_candidates(candidates)
        if best is not None:
            inliers, _ncc, good_count, polygon = best
            return SpineMatch(polygon_uv=polygon, confidence=float(inliers) / float(good_count))
        if self._use_correlation_fallback:
            return self._correlation_fallback(image_bgr.shape, roi_enhanced, roi_x0)
        return None

    def _verify_candidates(
        self,
        candidates: list[Tuple[int, float, int, np.ndarray]],
    ) -> Optional[Tuple[int, float, int, np.ndarray]]:
        """Photometric acceptance with a cross-scale consensus fallback.

        A lone candidate must pass the strict NCC gate. A weaker candidate
        (down to RELAXED_MIN_NCC) is accepted only when another scale's
        candidate lands on the same spot — specular glare washes intensity
        NCC out on glossy spines, while false candidates rarely cluster.
        """

        strict = [candidate for candidate in candidates if candidate[1] >= self._min_ncc]
        if strict:
            return max(strict, key=lambda candidate: (candidate[0], candidate[1]))
        supported = []
        for candidate in candidates:
            if candidate[1] < RELAXED_MIN_NCC:
                continue
            centroid_x = float(candidate[3][:, 0].mean())
            centroid_y = float(candidate[3][:, 1].mean())
            for other in candidates:
                if other is candidate:
                    continue
                if (abs(float(other[3][:, 0].mean()) - centroid_x) <= CONSENSUS_DX_PX
                        and abs(float(other[3][:, 1].mean()) - centroid_y) <= CONSENSUS_DY_PX):
                    supported.append(candidate)
                    break
        if not supported:
            return None
        return max(supported, key=lambda candidate: candidate[1])

    def _correlation_fallback(
        self,
        frame_shape: tuple[int, ...],
        roi_enhanced: np.ndarray,
        roi_x0: int,
    ) -> Optional[SpineMatch]:
        """Multi-scale NCC template sweep for feature-poor spines.

        SIFT needs stable keypoints; glossy wraps and fine print on smooth
        covers leave it nothing to hold. A normalized cross-correlation sweep
        only asks that the pixels look alike, so it still locates such spines —
        at the price of an axis-aligned (no perspective) polygon.
        """

        _frame_height, frame_width = frame_shape[:2]
        peaks: list[Tuple[float, Tuple[int, int], _TemplateVariant]] = []
        for variant in self._correlation_variants:
            if variant.width >= frame_width or variant.height >= roi_enhanced.shape[0]:
                continue
            if variant.width >= roi_enhanced.shape[1]:
                continue
            result = cv2.matchTemplate(
                roi_enhanced, variant.enhanced, cv2.TM_CCOEFF_NORMED
            )
            _min_val, peak, _min_loc, loc = cv2.minMaxLoc(result)
            peaks.append((float(peak), (int(loc[0]), int(loc[1])), variant))
        # A lone correlation peak is not trusted: flat white stretches (shelf
        # uprights, wall) correlate strongly with light-colored featureless
        # spines. A peak is accepted only when another scale's peak — itself
        # reasonably strong — lands on the same spot.
        supporter_floor = self._min_correlation - 0.15
        best: Optional[Tuple[float, Tuple[int, int], _TemplateVariant]] = None
        for peak, loc, variant in peaks:
            if peak < self._min_correlation:
                continue
            center_x = loc[0] + variant.width / 2.0
            center_y = loc[1] + variant.height / 2.0
            consensual = False
            for other_peak, other_loc, other_variant in peaks:
                if other_variant is variant or other_peak < supporter_floor:
                    continue
                if (abs(other_loc[0] + other_variant.width / 2.0 - center_x) <= CONSENSUS_DX_PX
                        and abs(other_loc[1] + other_variant.height / 2.0 - center_y) <= CONSENSUS_DY_PX):
                    consensual = True
                    break
            if consensual and (best is None or peak > best[0]):
                best = (peak, loc, variant)
        if best is None:
            return None
        peak, (x, y), variant = best
        x += roi_x0
        polygon = np.float32(
            [
                [x, y],
                [x + variant.width - 1, y],
                [x + variant.width - 1, y + variant.height - 1],
                [x, y + variant.height - 1],
            ]
        )
        return SpineMatch(polygon_uv=polygon, confidence=peak)

    def _enhance(self, gray: np.ndarray) -> np.ndarray:
        """CLAHE contrast boost; weakly textured spines gain usable features."""

        if self._clahe is None:
            return gray
        return self._clahe.apply(gray)

    def _match_variant(
        self,
        variant: _TemplateVariant,
        frame_keypoints,
        frame_descriptors: np.ndarray,
        frame_shape: tuple[int, ...],
    ) -> Optional[Tuple[int, float, int, np.ndarray]]:
        """Match one template variant; return (inliers, ncc, good, polygon)."""

        pairs = cv2.BFMatcher(self._norm, crossCheck=False).knnMatch(
            variant.descriptors, frame_descriptors, k=2
        )
        good = [first for pair in pairs if len(pair) == 2 for first, second in [pair]
                if first.distance < self._ratio_test * second.distance]
        if len(good) < self._min_matches:
            return None
        source = np.float32([variant.keypoints[item.queryIdx].pt for item in good]).reshape(-1, 1, 2)
        destination = np.float32([frame_keypoints[item.trainIdx].pt for item in good]).reshape(-1, 1, 2)
        # Seed RANSAC for determinism: the same frame and template must yield
        # the same verdict every run, otherwise borderline matches flicker.
        cv2.setRNGSeed(RANSAC_RNG_SEED)
        homography, inlier_mask = cv2.findHomography(
            source, destination, cv2.RANSAC, RANSAC_REPROJ_THRESHOLD_PX,
            maxIters=RANSAC_MAX_ITERS, confidence=RANSAC_CONFIDENCE,
        )
        if homography is None or inlier_mask is None:
            return None
        inliers = int(inlier_mask.ravel().sum())
        if inliers < self._min_inliers or inliers / len(good) < self._min_inlier_ratio:
            return None
        corners = np.float32(
            [
                [0, 0],
                [variant.width - 1, 0],
                [variant.width - 1, variant.height - 1],
                [0, variant.height - 1],
            ]
        ).reshape(-1, 1, 2)
        polygon = cv2.perspectiveTransform(corners, homography).reshape(4, 2)
        if not self._plausible_polygon(polygon, frame_shape):
            return None
        ncc = self._ncc(homography, variant)
        # NCC acceptance happens in match(): strict for lone candidates,
        # relaxed when independent scales agree on the same spot.
        return inliers, ncc, len(good), polygon

    def _plausible_polygon(self, polygon: np.ndarray, shape: tuple[int, ...]) -> bool:
        """Convex, in-ROI, in-bounds, sanely sized and shaped like the template."""

        height, width = shape[:2]
        if not np.all(np.isfinite(polygon)):
            return False
        if not _is_convex(polygon):
            return False
        centroid_x = float(polygon[:, 0].mean())
        if not self._roi_x[0] * width <= centroid_x <= self._roi_x[1] * width:
            return False
        if not (np.all(polygon[:, 0] >= -width) and np.all(polygon[:, 0] <= 2 * width)
                and np.all(polygon[:, 1] >= -height) and np.all(polygon[:, 1] <= 2 * height)):
            return False
        area = abs(float(cv2.contourArea(polygon.astype(np.float32))))
        if area < 80.0 or area > MAX_FRAME_AREA_RATIO * float(width * height):
            return False
        poly_width = (abs(polygon[1, 0] - polygon[0, 0]) + abs(polygon[2, 0] - polygon[3, 0])) / 2.0
        poly_height = (abs(polygon[3, 1] - polygon[0, 1]) + abs(polygon[2, 1] - polygon[1, 1])) / 2.0
        if poly_width <= 0.0 or poly_height <= 0.0:
            return False
        aspect = poly_width / poly_height
        if aspect < self._template_aspect * ASPECT_TOLERANCE_MIN:
            return False
        if aspect > self._template_aspect * ASPECT_TOLERANCE_MAX:
            return False
        return True

    def _ncc(self, homography: np.ndarray, variant: _TemplateVariant) -> float:
        """Photometric check: does the warped frame patch look like the template?"""

        warped = cv2.warpPerspective(
            self._frame_enhanced, np.linalg.inv(homography), (variant.width, variant.height)
        )
        patch = warped.astype(np.float32).ravel()
        reference = variant.enhanced.astype(np.float32).ravel()
        patch -= patch.mean()
        reference -= reference.mean()
        denom = float(np.sqrt((patch * patch).sum() * (reference * reference).sum()))
        if denom <= 0.0:
            return 0.0
        return float((patch * reference).sum() / denom)

    @staticmethod
    def pick_point(
        polygon_uv: np.ndarray,
        top_down_ratio: float = TOP_DOWN_ONE_SIXTH,
    ) -> tuple[int, int]:
        """Return the horizontal center at ``top_down_ratio`` from spine top to bottom."""

        points = np.asarray(polygon_uv, dtype=float).reshape(4, 2)
        ratio = BookSpineMatcher.validate_top_down_ratio(top_down_ratio)
        top_left, top_right, _bottom_right, bottom_left = points
        left = top_left * (1.0 - ratio) + bottom_left * ratio
        right = top_right * (1.0 - ratio) + _bottom_right * ratio
        point = (left + right) * 0.5
        return (int(round(float(point[0]))), int(round(float(point[1]))))

    @staticmethod
    def validate_top_down_ratio(value: float) -> float:
        """Validate a normalized point position from the spine top edge."""

        try:
            ratio = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("book-spine top-down ratio must be a number") from error
        if not math.isfinite(ratio) or not 0.0 <= ratio <= 1.0:
            raise ValueError("book-spine top-down ratio must be between 0.0 and 1.0")
        return ratio

    @staticmethod
    def _make_detector():
        if hasattr(cv2, "SIFT_create"):
            # A generous feature cap and a low contrast threshold keep
            # keypoints on dark or weakly textured spines from being crowded
            # out by high-response background text.
            return (
                cv2.SIFT_create(nfeatures=12000, contrastThreshold=0.02),
                cv2.NORM_L2,
            )
        return cv2.ORB_create(nfeatures=12000), cv2.NORM_HAMMING
