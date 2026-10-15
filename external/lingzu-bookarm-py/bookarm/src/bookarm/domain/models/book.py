"""Book identity, supplied spine image, and D435 target result."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .pose import BasePose


@dataclass(frozen=True)
class BookIdentity:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("book identifier must not be empty")


@dataclass(frozen=True)
class SpineTemplate:
    path: Path

    def __post_init__(self) -> None:
        if not str(self.path).strip():
            raise ValueError("book spine template path must not be empty")


@dataclass(frozen=True)
class BookTarget:
    """Vision output in the robot-base coordinate frame."""

    raw_pose: BasePose
    grasp_pose: BasePose
    pixel_uv: Optional[Tuple[int, int]] = None
    confidence: Optional[float] = None
    capture_id: Optional[str] = None
    # 书脊四边形 4 角点像素坐标（识别后置校验用：倾斜判定与居中判定）；
    # 轴对齐兜底分支同样给 4 角点（此时无倾斜信息，按 aspect 粗判）
    polygon_uv: Optional[Tuple[Tuple[float, float], ...]] = None
    frame_width_px: Optional[int] = None
    frame_height_px: Optional[int] = None


@dataclass(frozen=True)
class ShelfPlacementTarget:
    """External-shelf placement point derived from a neighboring book spine."""

    pose: BasePose
    neighbor_reference_uv: Tuple[int, int]
    target_pixel_uv: Tuple[int, int]
    neighbor_polygon_uv: Optional[Tuple[Tuple[float, float], ...]] = None
    confidence: Optional[float] = None
    capture_id: Optional[str] = None
