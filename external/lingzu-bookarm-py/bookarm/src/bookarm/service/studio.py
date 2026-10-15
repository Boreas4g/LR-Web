"""Book-spine segmentation workbench behind the ``/api/v1/studio`` routes.

The studio lets an operator jog the lift, photograph the shelf with the D435
camera, segment the photo into individual book spines and keep the crops in a
persistent library. Workflow submissions can then reference a library image
by ``template_id`` instead of uploading a fresh file.

``RealStudioBackend`` drives the shared camera and lift, and runs the YOLO
segmentation script lazily so deployments without ultralytics keep every
other endpoint working. ``FakeStudioBackend`` simulates the whole loop for
tests and demonstrations. Both serialize camera/lift actions against workflow
execution through the runner's busy lock.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import gc
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple
import uuid

import cv2
import numpy as np

from bookarm.devices.arm.service import ArmService
from bookarm.devices.camera.d435_device import D435Frame
from bookarm.devices.camera.depth_gap import DepthGapLocator
from bookarm.devices.camera.matcher import BookSpineMatcher, SpineMatch
from bookarm.devices.camera.service import D435BookSpineLocator
from bookarm.devices.camera.transform import (
    CameraToBaseTransform,
    matrix_to_pose6,
    pose6_to_matrix,
)
from bookarm.devices.lift.service import AimLiftService
from bookarm.devices.rod.service import RodMotorService
from bookarm.domain.models.book import SpineTemplate
from bookarm.domain.models.pose import BasePose, JointPose


# Recommended inference parameters for the book-spine segmentation model.
SEGMENT_IOU = 0.6
SEGMENT_PAD = 8
SEGMENT_RETINA_MASKS = False
SEGMENT_MAX_DET = 30
SEGMENT_DEVICE = "cpu"

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
MANUAL_NAME_MAX = 60
MANUAL_POLYGON_MAX_POINTS = 40


def _manual_template_name(name: Any) -> str:
    """Clean one operator-supplied template name; raise ValueError if unusable."""

    if not isinstance(name, str) or not name.strip():
        raise ValueError("manual segmentation name must be a non-empty string")
    cleaned = "".join(
        ch if ch.isalnum() or ch in "._-" else "_" for ch in name.strip()
    ).strip("._-")
    if not cleaned:
        raise ValueError("manual segmentation name has no usable characters")
    return cleaned[:MANUAL_NAME_MAX]


def _manual_polygon_points(polygon: Any, width: int, height: int) -> List[List[float]]:
    """Validate one hand-drawn polygon against the capture dimensions."""

    if not isinstance(polygon, (list, tuple)) or not polygon:
        raise ValueError("manual segmentation polygon must be a non-empty array")
    if len(polygon) < 3:
        raise ValueError("manual segmentation polygon needs at least 3 points")
    if len(polygon) > MANUAL_POLYGON_MAX_POINTS:
        raise ValueError(
            "manual segmentation polygon supports at most %d points" % MANUAL_POLYGON_MAX_POINTS
        )
    points: List[List[float]] = []
    for point in polygon:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("manual segmentation polygon points must be [x, y] pairs")
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError) as error:
            raise ValueError("manual segmentation polygon coordinates must be numbers") from error
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("manual segmentation polygon coordinates must be finite")
        if not (0.0 <= x < width and 0.0 <= y < height):
            raise ValueError(
                "manual segmentation point (%.1f, %.1f) is outside the capture %dx%d"
                % (x, y, width, height)
            )
        points.append([x, y])
    return points


def _manual_masked_crop(
    image_bgr: np.ndarray, polygon: List[List[float]], pad: int
) -> Tuple[np.ndarray, Tuple[int, int, int, int], int]:
    """Crop the polygon region with a transparent background and padding."""

    height, width = image_bgr.shape[:2]
    polygon_np = np.asarray(polygon, dtype=np.float32)
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.fillPoly(mask, [np.round(polygon_np).astype(np.int32)], 255)
    area = int(cv2.countNonZero(mask))
    if area <= 0:
        raise ValueError("manual segmentation polygon encloses no pixels")
    x, y, w, h = cv2.boundingRect(np.round(polygon_np).astype(np.int32))
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(width, x + w + pad)
    y2 = min(height, y + h + pad)
    if x2 <= x1 or y2 <= y1:
        raise ValueError("manual segmentation polygon is outside the capture")
    crop = image_bgr[y1:y2, x1:x2]
    rgba = cv2.cvtColor(crop, cv2.COLOR_BGR2BGRA)
    rgba[:, :, 3] = mask[y1:y2, x1:x2]
    return rgba, (x1, y1, x2, y2), area


class StudioBusyError(RuntimeError):
    """Raised when a studio action collides with a running workflow task."""


class SegmentationUnavailableError(RuntimeError):
    """Raised when the YOLO segmentation stack cannot be loaded."""


class ArmUnavailableError(RuntimeError):
    """Raised when arm control is not wired into the studio backend."""


class ArmNotEnabledError(RuntimeError):
    """Raised when an interactive arm motion requires an enabled arm."""


class CartesianJogError(ValueError):
    """Raised when a cartesian jog request cannot be executed safely."""


class ArmJointMoveError(ValueError):
    """Raised when a joint-space target cannot be executed safely."""


class ArmEnabledError(RuntimeError):
    """Raised when an operation requires a disabled arm but it is enabled."""


def is_kinematics_error(error: BaseException) -> bool:
    """IK convergence failures are unreachable-target input, not hardware faults.

    The arm stack reports them as ``RuntimeError`` ("arm inverse kinematics
    did not converge", "MovePByJ inverse kinematics failed ..."), the same
    type it uses for rejected feedback and fault codes, so interactive
    endpoints classify by message before mapping to 422 vs 503.
    """

    return "inverse kinematics" in str(error).lower()


class CameraTransformError(ValueError):
    """Raised when a camera-to-base calibration update is invalid."""


# Cartesian jog safety envelope (base frame): per-step clamps and workspace box.
JOG_POSITION_STEP_LIMIT_M = 0.1
JOG_ROTATION_STEP_LIMIT_RAD = math.radians(30.0)
JOG_WORKSPACE_RADIUS_M = 0.6
JOG_WORKSPACE_Z_MIN_M = -0.1
JOG_WORKSPACE_Z_MAX_M = 0.8
JOG_DURATION_MIN_S = 0.5
JOG_DURATION_MAX_S = 10.0

# Camera calibration override file, next to the packaged example.
_CONFIG_CALIBRATION_DIR = Path(__file__).resolve().parents[3] / "config" / "calibration"
DEFAULT_CAMERA_TRANSFORM_PATH = _CONFIG_CALIBRATION_DIR / "camera_to_base.json"
DEFAULT_CAMERA_TRANSFORM_EXAMPLE_PATH = _CONFIG_CALIBRATION_DIR / "camera_to_base.example.json"

# Camera extrinsic validation: rotation must stay near-orthonormal (R@R.T ≈ I)
# and the camera must plausibly sit within two metres of the base.
CAMERA_TRANSFORM_ORTHO_ATOL = 1e-2
CAMERA_TRANSFORM_TRANSLATION_LIMIT_M = 2.0


def _tcp_view(pose: BasePose) -> Dict[str, Any]:
    """Serialise a base-frame TCP pose in mm/deg for the HTTP views."""

    return {
        "x_mm": pose.x_m * 1000.0,
        "y_mm": pose.y_m * 1000.0,
        "z_mm": pose.z_m * 1000.0,
        "rx_deg": math.degrees(pose.rx_rad),
        "ry_deg": math.degrees(pose.ry_rad),
        "rz_deg": math.degrees(pose.rz_rad),
    }


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    """Write one JSON document atomically (temp file in place + os.replace)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temp_name, path)


def _validate_camera_transform_payload(payload: Mapping[str, Any]) -> list:
    """Return a validated 4x4 nested-list matrix from a PUT payload.

    Accepts either ``{"matrix": 4x4}`` or the 6-DOF fields ``x_mm/y_mm/z_mm``
    plus ``rx_deg/ry_deg/rz_deg`` (roll-pitch-yaw, composed in the backend so
    both ends share one Euler convention). The rotation must stay
    near-orthonormal and the translation within ±2 m per axis.
    """

    if not isinstance(payload, Mapping):
        raise CameraTransformError("camera transform update must be a JSON object")
    if "matrix" in payload:
        try:
            matrix = np.asarray(payload["matrix"], dtype=float)
        except (TypeError, ValueError) as error:
            raise CameraTransformError("matrix must be a numeric 4x4 array") from error
        if matrix.shape != (4, 4):
            raise CameraTransformError(
                "matrix must be 4x4, got shape %s" % (matrix.shape,)
            )
    else:
        fields = ("x_mm", "y_mm", "z_mm", "rx_deg", "ry_deg", "rz_deg")
        missing = [name for name in fields if name not in payload]
        if missing:
            raise CameraTransformError(
                "camera transform update requires 'matrix' or 6-DOF fields; missing: %s"
                % ", ".join(missing)
            )
        try:
            values = {name: float(payload[name]) for name in fields}
        except (TypeError, ValueError) as error:
            raise CameraTransformError("6-DOF fields must be numeric") from error
        matrix = np.asarray(
            pose6_to_matrix(
                values["x_mm"] / 1000.0,
                values["y_mm"] / 1000.0,
                values["z_mm"] / 1000.0,
                math.radians(values["rx_deg"]),
                math.radians(values["ry_deg"]),
                math.radians(values["rz_deg"]),
            ),
            dtype=float,
        )
    if not np.all(np.isfinite(matrix)):
        raise CameraTransformError("matrix contains non-finite values")
    if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1e-9):
        raise CameraTransformError("matrix must be homogeneous (last row [0, 0, 0, 1])")
    rotation = matrix[:3, :3]
    if not np.allclose(
        rotation @ rotation.T, np.eye(3), atol=CAMERA_TRANSFORM_ORTHO_ATOL
    ):
        raise CameraTransformError(
            "rotation part must be near-orthonormal (R·Rᵀ ≈ I, atol %.3g)"
            % CAMERA_TRANSFORM_ORTHO_ATOL
        )
    if not np.all(np.abs(matrix[:3, 3]) <= CAMERA_TRANSFORM_TRANSLATION_LIMIT_M):
        raise CameraTransformError(
            "translation must be within ±%.1f m per axis"
            % CAMERA_TRANSFORM_TRANSLATION_LIMIT_M
        )
    return matrix.tolist()


@dataclass(frozen=True)
class CaptureRecord:
    """One stored shelf photo taken through the studio."""

    id: str
    path: Path
    width: int
    height: int
    frame_number: int
    created_at: str


@dataclass(frozen=True)
class LibraryRecord:
    """One segmented book-spine crop kept in the persistent library."""

    id: str
    capture_id: str
    spine_index: int
    confidence: float
    created_at: str
    name: str = ""


@dataclass(frozen=True)
class SegmentedSpine:
    """One freshly segmented spine, already copied into the library."""

    library_id: str
    index: int
    confidence: float
    area: int
    bbox_xyxy: Tuple[int, int, int, int]
    polygon_xy: Tuple[Tuple[float, float], ...]


@dataclass(frozen=True)
class SegmentResult:
    """All spines segmented from one capture."""

    capture_id: str
    spines: Tuple[SegmentedSpine, ...]


@dataclass(frozen=True)
class TemplateSelfCheck:
    """Recognizability verdict for one stored library template."""

    ok: bool
    detail: str
    feature_count: int = 0


def self_check_template(template_path: Path) -> TemplateSelfCheck:
    """Build the take-out matcher on one template as a recognizability check.

    BookSpineMatcher construction runs the exact feature-extraction pipeline
    that take-out matching relies on, so a template that builds here is
    recognizable; one that raises "insufficient visual features" would only
    surface the same error later, while the operator is waiting at the shelf.
    """

    try:
        matcher = BookSpineMatcher(Path(template_path))
    except ValueError as error:
        return TemplateSelfCheck(
            ok=False,
            detail="该图特征不足，取书时可能识别不到，建议重拍（%s）" % error,
        )
    except FileNotFoundError as error:
        return TemplateSelfCheck(
            ok=False,
            detail="模板图片无法读取，建议重拍（%s）" % error,
        )
    except Exception as error:  # OpenCV build/runtime failures stay non-fatal
        return TemplateSelfCheck(
            ok=False,
            detail="模板自检未能完成，取书时可能识别不到，建议重拍（%s）" % error,
        )
    return TemplateSelfCheck(
        ok=True,
        detail="特征提取自检通过（可用特征点 %d 个）" % matcher.template_feature_count,
        feature_count=matcher.template_feature_count,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StudioStore:
    """Filesystem layout and manifest for captures and the spine library.

    Layout under the configured root directory::

        captures/<id>.jpg        shelf photos taken through the studio
        segments/<capture_id>/   intermediate overlay/masked segmentation files
        library/<id>.png         rectified spine crops usable as templates
        library/manifest.json    id -> {capture_id, spine_index, confidence, created_at}
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self.captures_dir = self._root / "captures"
        self.segments_dir = self._root / "segments"
        self.library_dir = self._root / "library"
        self._manifest_path = self.library_dir / "manifest.json"
        self._lock = threading.Lock()
        self._captures: Dict[str, CaptureRecord] = {}
        self._library: Dict[str, LibraryRecord] = {}
        self._load_manifest()

    def add_capture(self, image_bgr: np.ndarray, frame_number: int) -> CaptureRecord:
        """Persist one BGR frame as JPEG and register its metadata."""

        capture_id = uuid.uuid4().hex
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        path = self.captures_dir / ("%s.jpg" % capture_id)
        if not cv2.imwrite(str(path), image_bgr):
            raise RuntimeError("failed to write studio capture: %s" % path)
        height, width = image_bgr.shape[:2]
        record = CaptureRecord(
            id=capture_id,
            path=path,
            width=int(width),
            height=int(height),
            frame_number=int(frame_number),
            created_at=_utc_now(),
        )
        with self._lock:
            self._captures[capture_id] = record
        return record

    def capture_image_path(self, capture_id: str) -> Path:
        """Resolve a capture photo, falling back to the filesystem.

        Capture metadata is in-memory only, so after a restart the photo is
        still served (and segmentable) from ``captures/<id>.jpg``.
        """

        self._validate_id(capture_id)
        with self._lock:
            record = self._captures.get(capture_id)
        path = record.path if record is not None else self.captures_dir / ("%s.jpg" % capture_id)
        if not path.is_file():
            raise KeyError("unknown capture: %s" % capture_id)
        return path

    def add_library_crop(
        self,
        source_path: Path,
        *,
        capture_id: str,
        spine_index: int,
        confidence: float,
        name: str = "",
    ) -> LibraryRecord:
        """Copy one segmented crop into the library and the manifest."""

        library_id = uuid.uuid4().hex
        self.library_dir.mkdir(parents=True, exist_ok=True)
        destination = self.library_dir / ("%s.png" % library_id)
        shutil.copyfile(source_path, destination)
        record = LibraryRecord(
            id=library_id,
            capture_id=str(capture_id),
            spine_index=int(spine_index),
            confidence=float(confidence),
            created_at=_utc_now(),
            name=str(name),
        )
        with self._lock:
            self._library[library_id] = record
            self._save_manifest()
        return record

    def add_library_image(self, image_bgr: np.ndarray, *, source: str, name: str = "") -> LibraryRecord:
        """Encode one externally supplied image as PNG and register it."""

        library_id = uuid.uuid4().hex
        self.library_dir.mkdir(parents=True, exist_ok=True)
        destination = self.library_dir / ("%s.png" % library_id)
        if not cv2.imwrite(str(destination), image_bgr):
            raise RuntimeError("failed to write library image: %s" % destination)
        record = LibraryRecord(
            id=library_id,
            capture_id=str(source),
            spine_index=0,
            confidence=1.0,
            created_at=_utc_now(),
            name=str(name),
        )
        with self._lock:
            self._library[library_id] = record
            self._save_manifest()
        return record

    def list_library(self) -> List[LibraryRecord]:
        """All library records, newest first."""

        with self._lock:
            records = list(self._library.values())
        records.sort(key=lambda record: (record.created_at, record.id), reverse=True)
        return records

    def library_image_path(self, library_id: str) -> Path:
        """Resolve a library crop; raise KeyError when unknown."""

        self._validate_id(library_id)
        with self._lock:
            known = library_id in self._library
        path = self.library_dir / ("%s.png" % library_id)
        if not known or not path.is_file():
            raise KeyError("unknown library image: %s" % library_id)
        return path

    def delete_library_item(self, library_id: str) -> None:
        """Remove one library crop from the manifest and the filesystem."""

        self._validate_id(library_id)
        with self._lock:
            if library_id not in self._library:
                raise KeyError("unknown library image: %s" % library_id)
            del self._library[library_id]
            self._save_manifest()
        (self.library_dir / ("%s.png" % library_id)).unlink(missing_ok=True)

    def _load_manifest(self) -> None:
        """Restore the library index, tolerating missing or corrupt files."""

        try:
            raw = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for library_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            try:
                record = LibraryRecord(
                    id=str(library_id),
                    capture_id=str(entry["capture_id"]),
                    spine_index=int(entry["spine_index"]),
                    confidence=float(entry["confidence"]),
                    created_at=str(entry["created_at"]),
                    name=str(entry.get("name", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if not (self.library_dir / ("%s.png" % record.id)).is_file():
                continue
            self._library[record.id] = record

    def _save_manifest(self) -> None:
        self.library_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            record.id: {
                "capture_id": record.capture_id,
                "spine_index": record.spine_index,
                "confidence": record.confidence,
                "created_at": record.created_at,
                "name": record.name,
            }
            for record in self._library.values()
        }
        self._manifest_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _validate_id(record_id: str) -> None:
        if not _ID_PATTERN.match(str(record_id)):
            raise KeyError("invalid studio resource id: %s" % record_id)

    def vision_debug_dir(self) -> Path:
        return self._root / "vision_debug"

    def vision_debug_meta(self) -> Dict[str, Any]:
        """Read the locator's latest persisted match metadata."""

        path = self.vision_debug_dir() / "latest.json"
        if not path.is_file():
            raise KeyError("no workflow vision capture has been recorded yet")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise KeyError("workflow vision capture metadata is unreadable") from error

    def vision_debug_gap_meta(self) -> Dict[str, Any]:
        """Read the latest depth-gap detection metadata."""

        path = self.vision_debug_dir() / DepthGapLocator.META_NAME
        if not path.is_file():
            raise KeyError("no depth-gap debug capture has been recorded yet")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise KeyError("depth-gap debug metadata is unreadable") from error

    def vision_debug_image_path(self, kind: str) -> Path:
        names = {
            "raw": "latest_raw.jpg",
            "annotated": "latest_annotated.jpg",
            "gap_raw": DepthGapLocator.RAW_IMAGE_NAME,
            "gap_overlay": DepthGapLocator.OVERLAY_NAME,
        }
        if kind not in names:
            raise ValueError(
                "vision debug image kind must be one of %s" % ", ".join(sorted(names))
            )
        path = self.vision_debug_dir() / names[kind]
        if not path.is_file():
            raise KeyError("no workflow vision capture has been recorded yet")
        return path

    def arm_thermal_status(self) -> Dict[str, Any]:
        raise ArmUnavailableError("arm thermal status is not available on this backend")

    def clear_arm_thermal_lockout(self) -> Dict[str, Any]:
        raise ArmUnavailableError("arm thermal lockout is not available on this backend")

    def hardware_status(self) -> Dict[str, Any]:
        raise ArmUnavailableError("hardware status is not available on this backend")


class StudioBackend(ABC):
    """Hardware-neutral operations behind the studio HTTP routes."""

    def __init__(self, store: StudioStore, busy_lock: Optional[threading.Lock] = None) -> None:
        """``busy_lock`` should be the workflow runner's lock when available."""

        self._store = store
        self._busy = busy_lock if busy_lock is not None else threading.Lock()
        # Self-check verdicts keyed by template path; the (mtime, size) stamp
        # invalidates an entry when the image file is replaced. Only the small
        # verdict is cached, never the matcher with its descriptor pyramid.
        self._self_check_lock = threading.Lock()
        self._self_check_cache: Dict[str, Tuple[Tuple[int, int], TemplateSelfCheck]] = {}

    @abstractmethod
    def read_height_mm(self) -> float:
        """Read the current lift height in millimetres."""

    @abstractmethod
    def move_to_height_mm(self, target_height_mm: float, *, speed_level: int) -> Tuple[float, float]:
        """Move the lift; return (target, measured) height in millimetres."""

    @abstractmethod
    def capture(self) -> CaptureRecord:
        """Photograph the shelf and store the frame as one capture."""

    @abstractmethod
    def segment_capture(self, capture_id: str, *, conf: float, imgsz: int) -> SegmentResult:
        """Segment one capture into library spines; raise KeyError if unknown."""

    def segment_capture_manual(
        self,
        capture_id: str,
        polygon: Any,
        *,
        name: str,
        pad: int = SEGMENT_PAD,
    ) -> SegmentedSpine:
        """Store one hand-drawn spine polygon as a named library template.

        No camera access or YOLO stack is needed: the crop comes from the
        stored capture JPEG, so this works on every backend. The masked crop
        (transparent outside the polygon) is kept next to the YOLO
        intermediates and copied into the library like any segmented spine.
        """

        image_path = self._store.capture_image_path(capture_id)
        image = cv2.imread(str(image_path))
        if image is None:
            raise RuntimeError("studio capture is unreadable: %s" % image_path)
        height, width = image.shape[:2]
        label = _manual_template_name(name)
        points = _manual_polygon_points(polygon, width, height)
        pad_value = int(pad)
        if not 0 <= pad_value <= 64:
            raise ValueError("manual segmentation pad must be between 0 and 64 pixels")
        crop, bbox, area = _manual_masked_crop(image, points, pad_value)
        output_dir = self._store.segments_dir / capture_id
        output_dir.mkdir(parents=True, exist_ok=True)
        crop_path = output_dir / ("%s_manual_%s.png" % (capture_id, label))
        if not cv2.imwrite(str(crop_path), crop):
            raise RuntimeError("failed to write manual spine crop: %s" % crop_path)
        record = self._store.add_library_crop(
            crop_path,
            capture_id=capture_id,
            spine_index=0,
            confidence=1.0,
            name=label,
        )
        return SegmentedSpine(
            library_id=record.id,
            index=0,
            confidence=1.0,
            area=area,
            bbox_xyxy=bbox,
            polygon_xy=tuple((float(x), float(y)) for x, y in points),
        )

    def capture_image_path(self, capture_id: str) -> Path:
        return self._store.capture_image_path(capture_id)

    def list_library(self) -> List[LibraryRecord]:
        return self._store.list_library()

    def import_library_image(self, image_bgr: np.ndarray, *, source: str, name: str = "") -> LibraryRecord:
        return self._store.add_library_image(image_bgr, source=source, name=name)

    def check_library_template(self, library_id: str) -> TemplateSelfCheck:
        """Self-check one stored template; verdicts cached by file mtime/size.

        Matcher construction is the expensive part (SIFT over a scale
        pyramid), so repeated checks of an unchanged image reuse the verdict.
        """

        path = self._store.library_image_path(library_id)
        try:
            stat = path.stat()
        except OSError as error:
            return TemplateSelfCheck(ok=False, detail="模板图片无法读取，建议重拍（%s）" % error)
        key = str(path)
        stamp = (int(stat.st_mtime_ns), int(stat.st_size))
        with self._self_check_lock:
            cached = self._self_check_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        result = self_check_template(path)
        with self._self_check_lock:
            # Deleted library entries must not pile up; the library stays far
            # below this bound in practice, so a full clear is cheap enough.
            if len(self._self_check_cache) >= 512:
                self._self_check_cache.clear()
            self._self_check_cache[key] = (stamp, result)
        return result

    def verify_library_templates(self) -> List[Dict[str, Any]]:
        """Self-check every library template for the library quality report."""

        report: List[Dict[str, Any]] = []
        for record in self._store.list_library():
            check = self.check_library_template(record.id)
            report.append(
                {
                    "id": record.id,
                    "name": record.name,
                    "ok": check.ok,
                    "detail": check.detail,
                }
            )
        return report

    def library_image_path(self, library_id: str) -> Path:
        return self._store.library_image_path(library_id)

    def delete_library_item(self, library_id: str) -> None:
        self._store.delete_library_item(library_id)

    def vision_debug_meta(self) -> Dict[str, Any]:
        return self._store.vision_debug_meta()

    def vision_debug_gap_meta(self) -> Dict[str, Any]:
        return self._store.vision_debug_gap_meta()

    def run_vision_debug(
        self,
        template_id: Optional[str],
        *,
        select: str,
        scan_height_ratio: float,
        scan_band_px: int,
        depth_gap_mm: float,
    ) -> Dict[str, Any]:
        """Capture one frame and run spine-match + depth-gap analysis on it."""

        raise ArmUnavailableError("vision debug run is not available on this backend")

    def vision_debug_image_path(self, kind: str) -> Path:
        return self._store.vision_debug_image_path(kind)

    @abstractmethod
    def reset_arm_to_debug_pose(self, duration_s: float) -> List[float]:
        """Enable the arm when needed, move it to the book-debug pose, return joints."""

    def move_rod_to_deg(
        self,
        target_deg: float,
        speed: int,
        acceleration: int,
        tolerance_deg: float,
        timeout_s: float,
    ) -> float:
        """Move the book-pushing rod to an absolute angle and return the measured angle."""
        raise ArmUnavailableError("rod control is not wired into this service instance")

    def arm_enable(self) -> Dict[str, Any]:
        """Enable the arm if needed; return the enable result."""
        raise ArmUnavailableError("arm control is not wired into this service instance")

    def arm_disable(self, duration_s: float = 3.0) -> Dict[str, Any]:
        """Move the arm to the safe pose and disable it; return the disable result."""
        raise ArmUnavailableError("arm control is not wired into this service instance")

    def cartesian_jog(self, *, axis: str, delta: float, duration_s: float) -> Dict[str, Any]:
        """Jog the arm TCP along one base-frame axis; return the new TCP pose."""
        raise ArmUnavailableError("arm control is not wired into this service instance")

    def arm_joints(self) -> Dict[str, Any]:
        """Return the current joint pose (rad and deg) plus the TCP pose."""
        raise ArmUnavailableError("arm control is not wired into this service instance")

    def move_arm_joints(self, joints_deg: List[float], duration_s: float) -> Dict[str, Any]:
        """Move the six joints to an absolute target in degrees; return the new pose."""
        raise ArmUnavailableError("arm control is not wired into this service instance")

    def move_arm_zero(self, duration_s: float) -> Dict[str, Any]:
        """Move the six joints to the built-in zero pose; return the new pose."""
        raise ArmUnavailableError("arm control is not wired into this service instance")

    def calibrate_arm_zero(self, motor: int) -> Dict[str, Any]:
        """Write the current pose as the encoder zero of one or all arm joints."""
        raise ArmUnavailableError("arm control is not wired into this service instance")

    def camera_transform_view(self) -> Dict[str, Any]:
        """Return the live camera-to-base matrix, its 6-DOF pose and source path."""
        raise ArmUnavailableError("camera calibration is not available on this backend")

    def update_camera_transform(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Validate, persist and hot-swap the camera-to-base calibration."""
        raise ArmUnavailableError("camera calibration is not available on this backend")

    def reset_camera_transform(self) -> Dict[str, Any]:
        """Delete the user calibration override and fall back to the example."""
        raise ArmUnavailableError("camera calibration is not available on this backend")

    def shutdown(self) -> None:
        """Release background resources; no-op for backends without any."""

    @contextmanager
    def _hold_busy(self) -> Iterator[None]:
        """Serialize one hardware action against workflow execution."""

        if not self._busy.acquire(blocking=False):
            raise StudioBusyError("a bookarm workflow or studio operation is already running")
        try:
            yield
        finally:
            self._busy.release()


class RealStudioBackend(StudioBackend):
    """Studio operations on the shared D435 camera and AIM lift.

    The YOLO weights and inference module are loaded lazily on the first
    segmentation request, so the rest of the service keeps working when
    ultralytics is not installed. Torch intra-op threads are capped via
    ``torch_threads`` so segmentation does not saturate every core, and the
    model is dropped from memory after ``model_idle_unload_s`` seconds of
    inactivity (``0`` keeps it resident forever).
    """

    def __init__(
        self,
        camera: D435BookSpineLocator,
        lift: AimLiftService,
        store: StudioStore,
        *,
        busy_lock: Optional[threading.Lock] = None,
        weights_path: Path,
        infer_path: Path,
        torch_threads: int = 2,
        model_idle_unload_s: float = 300.0,
        arm: Optional[ArmService] = None,
        rod: Optional[RodMotorService] = None,
        calibration_path: Path = DEFAULT_CAMERA_TRANSFORM_PATH,
        calibration_example_path: Path = DEFAULT_CAMERA_TRANSFORM_EXAMPLE_PATH,
        vehicle: Optional[Any] = None,
        plan_getter: Optional[Any] = None,
    ) -> None:
        super().__init__(store, busy_lock)
        self._vehicle = vehicle
        self._plan_getter = plan_getter
        self._camera = camera
        self._lift = lift
        self._arm = arm
        self._rod = rod
        self._calibration_path = Path(calibration_path)
        self._calibration_example_path = Path(calibration_example_path)
        self._weights_path = Path(weights_path)
        self._infer_path = Path(infer_path)
        self._torch_threads = int(torch_threads)
        self._model_idle_unload_s = float(model_idle_unload_s)
        self._model_lock = threading.Lock()
        self._segment_model: Any = None
        self._infer_module: Any = None
        self._idle_timer: Optional[threading.Timer] = None

    def read_height_mm(self) -> float:
        return float(self._lift.read_lift_height_mm())

    def move_to_height_mm(self, target_height_mm: float, *, speed_level: int) -> Tuple[float, float]:
        with self._hold_busy():
            result = self._lift.move_to_height_mm(target_height_mm, speed_level=speed_level)
        return float(result.target_height_mm), float(result.measured_height_mm)

    def capture(self) -> CaptureRecord:
        with self._hold_busy():
            image_bgr, frame_number = self._camera.capture_photo()
        return self._store.add_capture(image_bgr, frame_number)

    def run_vision_debug(
        self,
        template_id: Optional[str],
        *,
        select: str,
        scan_height_ratio: float,
        scan_band_px: int,
        depth_gap_mm: float,
    ) -> Dict[str, Any]:
        """Capture one frame and run spine-match + depth-gap analysis on it.

        The artifacts land in the shared vision-debug directory, so the latest
        run stays inspectable through the vision-debug routes until the next
        one (workflow or interactive) overwrites them.
        """

        if template_id is None:
            items = self._store.list_library()
            if not items:
                raise ValueError("template_id is required while the spine library is empty")
            template_id = items[0].id
        template_path = self._store.library_image_path(template_id)
        with self._hold_busy():
            locator = DepthGapLocator(self._camera, debug_dir=self._store.vision_debug_dir())
            return locator.locate_gap_debug(
                template=SpineTemplate(template_path),
                select=select,
                reference_uv=None,
                scan_height_ratio=scan_height_ratio,
                scan_band_px=scan_band_px,
                depth_gap_mm=depth_gap_mm,
            )

    # ---- 识别后校验调试（书脊倾斜 / 车辆居中对齐）----

    def vision_check_spine(self, library_id: str) -> Dict[str, Any]:
        """拍照 + SIFT 匹配 + 倾斜/居中几何计算（不动车），供调试页检查。"""

        from bookarm.application.post_vision import center_x_ratio, compute_spine_tilt_deg

        template_path = self._store.library_image_path(library_id)
        with self._hold_busy():
            try:
                target = self._camera.locate_book(SpineTemplate(template_path))
            except RuntimeError as error:
                if "not detected" in str(error):
                    return {"matched": False, "detail": str(error)}
                raise
        plan = self._plan_getter() if self._plan_getter is not None else None
        band = float(getattr(plan, "align_center_band_ratio", 0.40))
        tilt_max = float(getattr(plan, "spine_tilt_max_deg", 60.0))
        tilt = compute_spine_tilt_deg(target.polygon_uv)
        ratio = center_x_ratio(target)
        lo, hi = 0.5 - band / 2.0, 0.5 + band / 2.0
        return {
            "matched": True,
            "tilt_deg": None if tilt is None else round(tilt, 2),
            "tilt_max_deg": tilt_max,
            "tilt_ok": (tilt is None) or (tilt <= tilt_max + 1e-6),
            "center_x_ratio": None if ratio is None else round(ratio, 4),
            "center_band": [round(lo, 3), round(hi, 3)],
            "in_band": None if ratio is None else lo <= ratio <= hi,
            "polygon_uv": [list(point) for point in (target.polygon_uv or ())],
            "frame_width_px": target.frame_width_px,
            "frame_height_px": target.frame_height_px,
            "capture_id": target.capture_id,
            "confidence": target.confidence,
        }

    def vision_align_run(self, library_id: str) -> Dict[str, Any]:
        """倾斜判定 + 车辆居中脉冲对齐的完整实测（车辆会缓速移动！）。"""

        from bookarm.application.post_vision import (
            align_vehicle_to_center,
            center_x_ratio,
            check_spine_tilt,
        )

        if self._vehicle is None:
            raise ArmUnavailableError("车辆对齐服务未接线到本服务实例")
        template_path = self._store.library_image_path(library_id)
        if self._plan_getter is None:
            raise RuntimeError("plan_getter 未配置，无法读取当前作业阈值")
        plan = self._plan_getter()
        with self._hold_busy():
            try:
                target = self._camera.locate_book(SpineTemplate(template_path))
            except RuntimeError as error:
                if "not detected" in str(error):
                    return {"matched": False, "detail": str(error)}
                raise
            tilt = check_spine_tilt(target, plan)
            final, details = align_vehicle_to_center(
                self._camera, SpineTemplate(template_path), target, self._vehicle, plan
            )
        ratio = center_x_ratio(final)
        return {
            "matched": True,
            "tilt_deg": None if tilt is None else round(tilt, 2),
            "tilt_max_deg": float(plan.spine_tilt_max_deg),
            "final_center_x_ratio": None if ratio is None else round(ratio, 4),
            "final_capture_id": final.capture_id,
            **details,
        }

    def vehicle_align_status(self) -> Dict[str, Any]:
        """车端缓速脉冲服务状态透传。"""

        if self._vehicle is None:
            raise ArmUnavailableError("车辆对齐服务未接线到本服务实例")
        return self._vehicle.status()

    def reset_arm_to_debug_pose(self, duration_s: float) -> List[float]:
        if self._arm is None:
            raise ArmUnavailableError("arm control is not wired into this service instance")
        with self._hold_busy():
            # is_enabled() adopts the hold loop when a previous owner left the
            # motors enabled; enable() itself is safe to repeat.
            if not self._arm.is_enabled():
                self._arm.enable()
            self._arm.move_to_book_debug(float(duration_s))
            return [float(value) for value in self._arm.read_joint_pose().values_rad]

    def move_rod_to_deg(
        self,
        target_deg: float,
        speed: int,
        acceleration: int,
        tolerance_deg: float,
        timeout_s: float,
    ) -> float:
        if self._rod is None:
            raise ArmUnavailableError("rod control is not wired into this service instance")
        with self._hold_busy():
            self._rod.move_to_deg(
                float(target_deg),
                int(speed),
                int(acceleration),
                float(tolerance_deg),
                float(timeout_s),
            )
            return float(self._rod.read_angle_deg())

    def arm_enable(self) -> Dict[str, Any]:
        if self._arm is None:
            raise ArmUnavailableError("arm control is not wired into this service instance")
        with self._hold_busy():
            # 夹爪掉使能/带锁存故障时也要重跑完整使能序列（同 tuning 使能）。
            was_enabled = self._arm.is_enabled() and self._arm.is_gripper_ready()
            if not was_enabled:
                self._arm.enable()
        return {"ok": True, "enabled": True, "was_enabled": was_enabled}

    def arm_disable(self, duration_s: float = 3.0) -> Dict[str, Any]:
        if self._arm is None:
            raise ArmUnavailableError("arm control is not wired into this service instance")
        with self._hold_busy():
            detail = self._arm.disable_at_safe_pose(float(duration_s))
        return {"ok": True, "enabled": False, "detail": detail}

    def arm_thermal_status(self) -> Dict[str, Any]:
        if self._arm is None:
            raise ArmUnavailableError("arm control is not wired into this service instance")
        return self._arm.thermal_status()

    def clear_arm_thermal_lockout(self) -> Dict[str, Any]:
        if self._arm is None:
            raise ArmUnavailableError("arm control is not wired into this service instance")
        return self._arm.clear_thermal_lockout()

    def hardware_status(self) -> Dict[str, Any]:
        """Run live hardware checks and return one consolidated status view."""

        def _attempt(fn, error_key: str):
            try:
                return fn(), None
            except Exception as error:
                return None, str(error)

        # CAN interface link state from sysfs (no bus traffic needed).
        can_info: Dict[str, Any] = {"interface": "can0", "exists": False, "up": False}
        try:
            can_info["exists"] = True
            can_info["up"] = Path("/sys/class/net/can0/operstate").read_text().strip() == "up"
        except (FileNotFoundError, OSError):
            can_info["exists"] = False

        arm_view: Dict[str, Any] = {"wired": self._arm is not None}
        if self._arm is not None:
            health, health_error = _attempt(self._arm.health, "health")
            arm_view["health"] = health
            arm_view["health_error"] = health_error
            thermal, _ = _attempt(self._arm.thermal_status, "thermal")
            arm_view["thermal"] = thermal

            def _read_pose() -> Dict[str, Any]:
                pose = self._arm.read_joint_pose()
                view: Dict[str, Any] = {"values_rad": list(pose.values_rad)}
                try:
                    view["tcp"] = _tcp_view(self._arm.tcp_pose(pose))
                except Exception:
                    # FK must not fail the whole status view (e.g. URDF or
                    # pinocchio unavailable); the joint values still stand.
                    view["tcp"] = None
                return view

            pose, pose_error = _attempt(_read_pose, "pose")
            arm_view["pose"] = pose
            arm_view["pose_error"] = pose_error
            carry = getattr(self._arm, "gripper_carry_status", None)
            if callable(carry):
                carry_view = carry()
                arm_view["gripper_carry_armed"] = carry_view["armed"]
                arm_view["gripper_carry_derated"] = carry_view["derated"]
                arm_view["gripper_thermal_warning"] = carry_view["thermal_warning"]

        lift_view: Dict[str, Any] = {}
        # read_lift_height_mm reports 0 mm as power-on default on failure, so
        # connectivity must come from the explicit handle flag, not the read.
        lift_connected = getattr(self._lift, "connected", None)
        height, lift_error = _attempt(self._lift.read_lift_height_mm, "height")
        lift_view["connected"] = lift_connected if lift_connected is not None else lift_error is None
        lift_view["height_mm"] = height
        lift_view["error"] = (
            lift_error if lift_connected is not False else "lift not connected (port unreachable or unpowered)"
        )
        lift_view["reconnects"] = getattr(self._lift, "reconnects", None)

        rod_view: Dict[str, Any] = {"wired": self._rod is not None}
        if self._rod is not None:
            angle, rod_error = _attempt(self._rod.read_angle_deg, "angle")
            rod_view["connected"] = rod_error is None
            rod_view["angle_deg"] = angle
            rod_view["error"] = rod_error
            rod_view["reconnects"] = getattr(self._rod, "reconnects", None)

        camera_view: Dict[str, Any] = {"present": False}
        try:
            import pyrealsense2 as rs

            context = rs.context()
            camera_view["present"] = len(context.query_devices()) > 0
        except Exception:
            camera_view["present"] = None
        if self._camera is not None:
            recovery = self._camera.pipeline_recovery_status()
            camera_view["pipeline_recoveries"] = recovery["pipeline_recoveries"]
            camera_view["pipeline_last_recovery_at"] = recovery["last_recovery_at"]

        devices = {
            name: Path("/dev/%s" % name).exists()
            for name in ("rodmotor", "lift_port", "imu_port")
        }
        return {
            "can": can_info,
            "arm": arm_view,
            "lift": lift_view,
            "rod": rod_view,
            "camera": camera_view,
            "devices": devices,
        }

    def cartesian_jog(self, *, axis: str, delta: float, duration_s: float) -> Dict[str, Any]:
        """Jog the arm TCP along one base-frame axis with a straight MoveL.

        The step is clamped (position ≤ 100 mm, rotation ≤ 30°) and the target
        must stay inside the jog workspace box. MoveL precomputes and
        joint-limit-validates the whole path before any motion, so an IK or
        limit failure leaves the arm untouched.
        """

        if self._arm is None:
            raise ArmUnavailableError("arm control is not wired into this service instance")
        axes = ("x", "y", "z", "rx", "ry", "rz")
        if axis not in axes:
            raise CartesianJogError("axis must be one of %s" % "/".join(axes))
        if not math.isfinite(float(delta)):
            raise CartesianJogError("delta must be a finite number")
        if not math.isfinite(float(duration_s)):
            raise CartesianJogError("duration_s must be a finite number")
        duration = min(max(float(duration_s), JOG_DURATION_MIN_S), JOG_DURATION_MAX_S)
        if axis in ("x", "y", "z"):
            step = min(max(float(delta) / 1000.0, -JOG_POSITION_STEP_LIMIT_M), JOG_POSITION_STEP_LIMIT_M)
            applied = step * 1000.0
        else:
            step = min(
                max(math.radians(float(delta)), -JOG_ROTATION_STEP_LIMIT_RAD),
                JOG_ROTATION_STEP_LIMIT_RAD,
            )
            applied = math.degrees(step)
        if not self._arm.is_enabled():
            raise ArmNotEnabledError("机械臂未使能：请先在硬件调试页使能再进行 TCP 点动")
        with self._hold_busy():
            current = self._arm.tcp_pose(self._arm.read_joint_pose())
            values = list(current.values())
            values[axes.index(axis)] += step
            target = BasePose.from_values(values)
            radius = math.hypot(target.x_m, target.y_m)
            if radius > JOG_WORKSPACE_RADIUS_M or not (
                JOG_WORKSPACE_Z_MIN_M <= target.z_m <= JOG_WORKSPACE_Z_MAX_M
            ):
                raise CartesianJogError(
                    "target TCP (%.3f, %.3f, %.3f) m leaves the jog workspace "
                    "(horizontal radius ≤ %.2f m, z %.2f..%.2f m); no motion was sent"
                    % (
                        target.x_m,
                        target.y_m,
                        target.z_m,
                        JOG_WORKSPACE_RADIUS_M,
                        JOG_WORKSPACE_Z_MIN_M,
                        JOG_WORKSPACE_Z_MAX_M,
                    )
                )
            try:
                self._arm.move_l(target, duration)
            except (ValueError, RuntimeError) as error:
                # Parameter-range and unreachable-target failures are operator
                # input (422); hardware/driver RuntimeError stays 503.
                if isinstance(error, RuntimeError) and not is_kinematics_error(error):
                    raise
                raise CartesianJogError(
                    "TCP jog path validation failed; no motion was sent: %s" % error
                ) from error
            updated = self._arm.tcp_pose(self._arm.read_joint_pose())
        return {
            "ok": True,
            "axis": axis,
            "delta_applied": applied,
            "duration_s": duration,
            "tcp": _tcp_view(updated),
        }

    def arm_joints(self) -> Dict[str, Any]:
        if self._arm is None:
            raise ArmUnavailableError("arm control is not wired into this service instance")
        pose = self._arm.read_joint_pose()
        joints_rad = [float(value) for value in pose.values_rad]
        try:
            tcp = _tcp_view(self._arm.tcp_pose(pose))
        except Exception:
            tcp = None
        return {
            "enabled": bool(self._arm.is_enabled()),
            "joints_rad": joints_rad,
            "joints_deg": [math.degrees(value) for value in joints_rad],
            "tcp": tcp,
        }

    def move_arm_joints(self, joints_deg: List[float], duration_s: float) -> Dict[str, Any]:
        if self._arm is None:
            raise ArmUnavailableError("arm control is not wired into this service instance")
        if len(joints_deg) != 6 or not all(math.isfinite(float(value)) for value in joints_deg):
            raise ArmJointMoveError("joints_deg must contain six finite values")
        duration = min(max(float(duration_s), JOG_DURATION_MIN_S), JOG_DURATION_MAX_S)
        if not self._arm.is_enabled():
            raise ArmNotEnabledError("机械臂未使能：请先在硬件调试页使能再发送关节目标")
        target = JointPose.from_values([math.radians(float(value)) for value in joints_deg])
        with self._hold_busy():
            try:
                self._arm.move_j(target, duration)
            except (ValueError, RuntimeError) as error:
                # Joint-limit and unreachable-target failures are operator
                # input (422); hardware/driver RuntimeError stays 503.
                if isinstance(error, RuntimeError) and not is_kinematics_error(error):
                    raise
                raise ArmJointMoveError(
                    "joint target validation failed; no motion was sent: %s" % error
                ) from error
        return self.arm_joints()

    def move_arm_zero(self, duration_s: float) -> Dict[str, Any]:
        if self._arm is None:
            raise ArmUnavailableError("arm control is not wired into this service instance")
        duration = min(max(float(duration_s), JOG_DURATION_MIN_S), JOG_DURATION_MAX_S)
        if not self._arm.is_enabled():
            raise ArmNotEnabledError("机械臂未使能：请先在硬件调试页使能再回零位")
        with self._hold_busy():
            self._arm.move_to_zero(duration)
        return self.arm_joints()

    def calibrate_arm_zero(self, motor: int) -> Dict[str, Any]:
        if self._arm is None:
            raise ArmUnavailableError("arm control is not wired into this service instance")
        if self._arm.is_enabled():
            raise ArmEnabledError("机械臂仍处使能状态：请先安全失能，再执行零位校准")
        motor_num = 0xFF if int(motor) == 0 else int(motor)
        with self._hold_busy():
            results = self._arm.set_zero_position(motor_num)
        failed = [str(motor_id) for motor_id, ok in results.items() if not ok]
        if failed:
            raise RuntimeError("零位校准帧发送失败，电机：%s" % ", ".join(failed))
        return {
            "ok": True,
            "motor": int(motor),
            "results": {str(motor_id): bool(ok) for motor_id, ok in results.items()},
        }

    def clear_arm_faults(self) -> Dict[str, Any]:
        """Clear latched fault codes on all motors (operator recovery action)."""
        if self._arm is None:
            raise ArmUnavailableError("arm control is not wired into this service instance")
        with self._hold_busy():
            return self._arm.clear_motor_faults()

    def camera_transform_view(self) -> Dict[str, Any]:
        if self._camera is None:
            raise ArmUnavailableError("camera is not wired into this service instance")
        matrix = self._camera.calibration_matrix()
        x_m, y_m, z_m, rx_rad, ry_rad, rz_rad = matrix_to_pose6(matrix)
        override = self._calibration_path.is_file()
        return {
            "matrix": matrix,
            "x_mm": x_m * 1000.0,
            "y_mm": y_m * 1000.0,
            "z_mm": z_m * 1000.0,
            "rx_deg": math.degrees(rx_rad),
            "ry_deg": math.degrees(ry_rad),
            "rz_deg": math.degrees(rz_rad),
            "source": str(
                self._calibration_path if override else self._calibration_example_path
            ),
            "override": override,
        }

    def update_camera_transform(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if self._camera is None:
            raise ArmUnavailableError("camera is not wired into this service instance")
        matrix = _validate_camera_transform_payload(payload)
        # Persist first, then hot-swap: the live locator can only disagree with
        # the file if the process dies between the two steps.
        _write_json_atomic(
            self._calibration_path,
            {
                "description": (
                    "User-refined D435 camera-to-robot-base extrinsic "
                    "(p_B = T_B_C @ p_C), written by the studio camera-transform API."
                ),
                "matrix": matrix,
            },
        )
        self._camera.set_calibration(matrix)
        return self.camera_transform_view()

    def reset_camera_transform(self) -> Dict[str, Any]:
        if self._camera is None:
            raise ArmUnavailableError("camera is not wired into this service instance")
        example = CameraToBaseTransform.from_json(self._calibration_example_path)
        self._calibration_path.unlink(missing_ok=True)
        self._camera.set_calibration(example.matrix.tolist())
        return self.camera_transform_view()

    def segment_capture(self, capture_id: str, *, conf: float, imgsz: int) -> SegmentResult:
        try:
            image_path = self._store.capture_image_path(capture_id)
            module = self._load_infer_module()
            model = self._load_segment_model()
            _overlay_path, crops = module.segment_image(
                model,
                image_path,
                self._store.segments_dir,
                int(imgsz),
                float(conf),
                SEGMENT_IOU,
                SEGMENT_PAD,
                SEGMENT_RETINA_MASKS,
                SEGMENT_MAX_DET,
                device=SEGMENT_DEVICE,
                save_crops=True,
            )
            spines: List[SegmentedSpine] = []
            for crop in crops:
                crop_path = Path(crop.rectified_path)
                if not crop_path.is_file():
                    crop_path = Path(crop.masked_path)
                record = self._store.add_library_crop(
                    crop_path,
                    capture_id=capture_id,
                    spine_index=crop.index,
                    confidence=crop.confidence,
                )
                spines.append(
                    SegmentedSpine(
                        library_id=record.id,
                        index=int(crop.index),
                        confidence=float(crop.confidence),
                        area=int(crop.area),
                        bbox_xyxy=tuple(int(value) for value in crop.bbox_xyxy),
                        polygon_xy=tuple(
                            (float(point[0]), float(point[1])) for point in crop.polygon_xy
                        ),
                    )
                )
            return SegmentResult(capture_id=capture_id, spines=tuple(spines))
        finally:
            self._schedule_idle_unload()

    def _load_segment_model(self) -> Any:
        with self._model_lock:
            if self._segment_model is not None:
                return self._segment_model
            if not self._weights_path.is_file():
                raise SegmentationUnavailableError(
                    "书脊分割模型未安装，请联系维护人员部署 book_spine_inference_light（缺失权重：%s）"
                    % self._weights_path
                )
            try:
                import torch
            except ImportError as error:
                raise SegmentationUnavailableError(
                    "书脊分割功能不可用：缺少 torch 依赖，请联系维护人员检查 BookArm Python 环境"
                ) from error
            torch.set_num_threads(self._torch_threads)
            try:
                torch.set_num_interop_threads(1)
            except RuntimeError:
                # Interop threads can only be configured once, before any
                # parallel work starts; a reload after that point raises.
                pass
            try:
                from ultralytics import YOLO
            except ImportError as error:
                raise SegmentationUnavailableError(
                    "书脊分割功能不可用：缺少 ultralytics 依赖，请联系维护人员检查 BookArm Python 环境"
                ) from error
            model = YOLO(str(self._weights_path))
            model.to("cpu")
            self._segment_model = model
            return self._segment_model

    def _schedule_idle_unload(self) -> None:
        """Restart the idle timer that drops the model from memory."""

        if self._model_idle_unload_s <= 0:
            return
        with self._model_lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            timer = threading.Timer(self._model_idle_unload_s, self._unload_idle_model)
            timer.daemon = True
            timer.start()
            self._idle_timer = timer

    def _unload_idle_model(self) -> None:
        with self._model_lock:
            self._idle_timer = None
            if self._segment_model is None:
                return
            self._segment_model = None
        gc.collect()

    def shutdown(self) -> None:
        """Cancel the idle-unload timer so it cannot fire during teardown."""

        with self._model_lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None

    def _load_infer_module(self) -> Any:
        if self._infer_module is not None:
            return self._infer_module
        if not self._infer_path.is_file():
            raise SegmentationUnavailableError(
                "书脊分割模型未安装，请联系维护人员部署 book_spine_inference_light（缺失推理脚本：%s）"
                % self._infer_path
            )
        spec = importlib.util.spec_from_file_location("_bookarm_studio_infer", str(self._infer_path))
        if spec is None or spec.loader is None:
            raise SegmentationUnavailableError(
                "书脊分割推理模块无法加载，请联系维护人员检查 book_spine_inference_light：%s"
                % self._infer_path
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except ImportError as error:
            sys.modules.pop(spec.name, None)
            raise SegmentationUnavailableError(
                "书脊分割推理模块加载失败，请联系维护人员检查 book_spine_inference_light：%s"
                % error
            ) from error
        self._infer_module = module
        return module


class _FakeGapCamera:
    """Feed the real DepthGapLocator one synthetic color+depth frame."""

    def __init__(self, frame: D435Frame) -> None:
        self._frame = frame

    def capture_frame(self) -> D435Frame:
        return self._frame

    def transform_to_base(self, point_camera_m):
        return tuple(float(value) for value in point_camera_m)


class _StubSpineMatcher:
    """Fixed spine match standing in for template matching on fake frames."""

    def __init__(self, match: SpineMatch) -> None:
        self._match = match

    def match(self, image_bgr) -> SpineMatch:
        image_bgr = None
        return self._match


class FakeStudioBackend(StudioBackend):
    """Simulate the studio: in-memory lift, synthetic frames, rectangle crops."""

    IMAGE_WIDTH = 640
    IMAGE_HEIGHT = 480
    # (x1, y1, x2, y2, BGR color) spine blocks drawn on every fake capture.
    SPINE_BLOCKS: Tuple[Tuple[int, int, int, int, Tuple[int, int, int]], ...] = (
        (60, 90, 160, 420, (60, 90, 200)),
        (240, 70, 340, 420, (180, 120, 40)),
        (430, 110, 540, 400, (40, 160, 90)),
    )

    def __init__(
        self,
        store: StudioStore,
        *,
        busy_lock: Optional[threading.Lock] = None,
        initial_height_mm: float = 10.0,
    ) -> None:
        super().__init__(store, busy_lock)
        self._height_mm = float(initial_height_mm)
        self._height_lock = threading.Lock()
        self._rod_angle_deg = 0.0
        self._rod_lock = threading.Lock()
        self._capture_count = 0

    def read_height_mm(self) -> float:
        with self._height_lock:
            return self._height_mm

    def move_to_height_mm(self, target_height_mm: float, *, speed_level: int) -> Tuple[float, float]:
        with self._hold_busy():
            with self._height_lock:
                self._height_mm = float(target_height_mm)
        return float(target_height_mm), float(target_height_mm)

    def reset_arm_to_debug_pose(self, duration_s: float) -> List[float]:
        duration_s = None
        with self._hold_busy():
            # BOOK_DEBUG_JOINT in degrees: [0, 10, -65, 50, 0, 0].
            return [
                0.0,
                0.17453292519943295,
                -1.1344640137963142,
                0.8726646259971648,
                0.0,
                0.0,
            ]

    def arm_joints(self) -> Dict[str, Any]:
        joints_rad = self.reset_arm_to_debug_pose(0.0)
        return {
            "enabled": True,
            "joints_rad": joints_rad,
            "joints_deg": [math.degrees(value) for value in joints_rad],
            "tcp": None,
        }

    def move_arm_joints(self, joints_deg: List[float], duration_s: float) -> Dict[str, Any]:
        duration_s = None
        if len(joints_deg) != 6 or not all(math.isfinite(float(value)) for value in joints_deg):
            raise ArmJointMoveError("joints_deg must contain six finite values")
        with self._hold_busy():
            joints_rad = [math.radians(float(value)) for value in joints_deg]
            return {
                "enabled": True,
                "joints_rad": joints_rad,
                "joints_deg": [math.degrees(value) for value in joints_rad],
                "tcp": None,
            }

    def move_arm_zero(self, duration_s: float) -> Dict[str, Any]:
        return self.move_arm_joints([0.0] * 6, duration_s)

    def calibrate_arm_zero(self, motor: int) -> Dict[str, Any]:
        with self._hold_busy():
            motor_ids = list(range(1, 7)) if int(motor) == 0 else [int(motor)]
            return {"ok": True, "motor": int(motor), "results": {str(motor_id): True for motor_id in motor_ids}}

    def move_rod_to_deg(
        self,
        target_deg: float,
        speed: int,
        acceleration: int,
        tolerance_deg: float,
        timeout_s: float,
    ) -> float:
        speed = acceleration = tolerance_deg = timeout_s = None
        with self._hold_busy():
            with self._rod_lock:
                self._rod_angle_deg = float(target_deg)
            return self._rod_angle_deg

    def arm_enable(self) -> Dict[str, Any]:
        with self._hold_busy():
            return {"ok": True, "enabled": True, "was_enabled": True}

    def arm_disable(self, duration_s: float = 3.0) -> Dict[str, Any]:
        duration_s = None
        with self._hold_busy():
            return {"ok": True, "enabled": False, "detail": {"fake": True, "enabled": False}}

    def arm_thermal_status(self) -> Dict[str, Any]:
        return {"limit_c": 75.0, "temperatures_c": {}, "lockout": None}

    def clear_arm_thermal_lockout(self) -> Dict[str, Any]:
        return self.arm_thermal_status()

    def hardware_status(self) -> Dict[str, Any]:
        return {
            "can": {"interface": "can0", "exists": True, "up": True},
            "arm": {"wired": True, "health": None, "health_error": None, "thermal": self.arm_thermal_status()},
            "lift": {"connected": True, "height_mm": self.read_height_mm(), "error": None},
            "rod": {"wired": True, "connected": True, "angle_deg": self._rod_angle_deg, "error": None},
            "camera": {"present": True},
            "devices": {"rodmotor": True, "lift_port": True, "imu_port": True},
        }

    def capture(self) -> CaptureRecord:
        with self._hold_busy():
            frame_number = self._capture_count + 1
            image_bgr = self._render_frame(frame_number)
        self._capture_count = frame_number
        return self._store.add_capture(image_bgr, frame_number)

    def run_vision_debug(
        self,
        template_id: Optional[str],
        *,
        select: str,
        scan_height_ratio: float,
        scan_band_px: int,
        depth_gap_mm: float,
    ) -> Dict[str, Any]:
        """Run the real depth-gap algorithm on one synthetic shelf frame.

        The middle spine block is the matched reference; the strip to its
        left reads as a deeper shelf back, the strip to its right as a depth
        hole, so the wider right gap wins the default "largest" selection.
        """

        template_id = None
        with self._hold_busy():
            frame_number = self._capture_count + 1
            image_bgr = self._render_frame(frame_number)
            self._capture_count = frame_number
            depth_m = np.full((self.IMAGE_HEIGHT, self.IMAGE_WIDTH), 0.50, dtype=np.float32)
            depth_m[:, 160:240] = 0.62
            depth_m[:, 341:430] = 0.0
            frame = D435Frame(
                color_bgr=image_bgr,
                depth_m=depth_m,
                fx=600.0,
                fy=600.0,
                ppx=self.IMAGE_WIDTH / 2.0,
                ppy=self.IMAGE_HEIGHT / 2.0,
                timestamp_ms=0.0,
                frame_number=frame_number,
            )
            x1, y1, x2, y2, _color = self.SPINE_BLOCKS[1]
            match = SpineMatch(
                polygon_uv=np.array(
                    [[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=float
                ),
                confidence=0.99,
            )
            locator = DepthGapLocator(
                _FakeGapCamera(frame),
                debug_dir=self._store.vision_debug_dir(),
                matcher_factory=lambda _path: _StubSpineMatcher(match),
            )
            locator.locate_gap(
                template=SpineTemplate(Path("fake-spine.png")),
                select=select,
                reference_uv=None,
                scan_height_ratio=scan_height_ratio,
                scan_band_px=scan_band_px,
                depth_gap_mm=depth_gap_mm,
            )
        return self._store.vision_debug_gap_meta()

    def segment_capture(self, capture_id: str, *, conf: float, imgsz: int) -> SegmentResult:
        image_path = self._store.capture_image_path(capture_id)
        image = cv2.imread(str(image_path))
        if image is None:
            raise RuntimeError("studio capture is unreadable: %s" % image_path)
        output_dir = self._store.segments_dir / capture_id
        output_dir.mkdir(parents=True, exist_ok=True)
        spines: List[SegmentedSpine] = []
        for index, (x1, y1, x2, y2, _color) in enumerate(self.SPINE_BLOCKS, start=1):
            crop_path = output_dir / ("%s_fake_spine_%03d.png" % (capture_id, index))
            if not cv2.imwrite(str(crop_path), image[y1:y2, x1:x2]):
                raise RuntimeError("failed to write fake spine crop: %s" % crop_path)
            record = self._store.add_library_crop(
                crop_path,
                capture_id=capture_id,
                spine_index=index,
                confidence=0.99 - 0.01 * (index - 1),
            )
            spines.append(
                SegmentedSpine(
                    library_id=record.id,
                    index=index,
                    confidence=record.confidence,
                    area=int((x2 - x1) * (y2 - y1)),
                    bbox_xyxy=(x1, y1, x2, y2),
                    polygon_xy=(
                        (float(x1), float(y1)),
                        (float(x2), float(y1)),
                        (float(x2), float(y2)),
                        (float(x1), float(y2)),
                    ),
                )
            )
        return SegmentResult(capture_id=capture_id, spines=tuple(spines))

    def _render_frame(self, frame_number: int) -> np.ndarray:
        image = np.full((self.IMAGE_HEIGHT, self.IMAGE_WIDTH, 3), (232, 226, 218), dtype=np.uint8)
        for index, (x1, y1, x2, y2, color) in enumerate(self.SPINE_BLOCKS, start=1):
            cv2.rectangle(image, (x1, y1), (x2, y2), color, thickness=-1)
            cv2.putText(
                image,
                "spine %d" % index,
                (x1 + 6, y1 + 32),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
        cv2.putText(
            image,
            "capture %d" % frame_number,
            (12, self.IMAGE_HEIGHT - 14),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (80, 80, 80),
            2,
            cv2.LINE_AA,
        )
        return image
