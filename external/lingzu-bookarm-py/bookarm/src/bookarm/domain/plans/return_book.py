"""Calibrated motion plan for returning a book to a shelf beside a located reference book."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Tuple

from bookarm.domain.models.pose import JointPose
from bookarm.domain.plans.take_book import GripperCommand


@dataclass(frozen=True)
class ReturnBookMotionPlan:
    """Shared poses, offsets, and durations for the return-book workflow.

    The flow picks the book to be returned from the calibrated book-stand
    pickup slot, re-locates the reference book by its spine template, then
    runs the same insertion pipeline as the mid-return flow: insert Y1 =
    target grasp y + target_y_offset_cm, pre-insert staging, insert, push
    into the shelf, partial release, retract and back to the debug pose.

    All single-axis shifts are expressed as absolute poses chained off the
    fresh vision target, so re-running one step stays idempotent.
    """

    zero_joint: JointPose = field(
        default_factory=lambda: JointPose.from_values((0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
    )
    debug_joint: JointPose = field(
        default_factory=lambda: JointPose.from_values(
            tuple(math.radians(value) for value in (0.0, 10.0, -65.0, 50.0, 0.0, 0.0))
        )
    )
    joint_tolerance_rad: float = 0.08
    zero_duration_s: float = 3.0
    debug_duration_s: float = 3.0
    # Open the gripper this far before the book-slot pickup.
    pickup_open_deg: float = 50.0
    pregrasp_duration_s: float = 3.0
    # Insert Y1 = target grasp y + this offset (cm). The automated workflow
    # and the tuning session both read it from this plan, so it is tuned here
    # rather than passed per request.
    target_y_offset_cm: float = 4.0
    # Insert pose: x/z reused from the vision target and y=Y1, then shifted by
    # this base-frame offset; orientation is insert_rpy_deg. rx beyond ~95 deg
    # is outside the workspace at deep-shelf targets.
    insert_offset_cm: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    insert_rpy_deg: Tuple[float, float, float] = (95.0, 0.0, 90.0)
    # Reachability floor for the insert z (arm-base frame): at insert_rpy_deg
    # the wrist runs into joint limits below ~0.25 m, so targets lower than
    # this are clamped up to the reachable minimum instead of failing IK.
    min_insert_z_m: float = 0.25
    # Pre-insert staging pose, flown MovePByJ before the final MoveL approach
    # so the book never sweeps across the stand neighbours. Its x is computed
    # per target by an IK search for the numerically greatest reachable value
    # (see application.use_cases.mid_return.search_pre_insert_x), so the pose
    # hugs the outer reach boundary; y/z stay offsets from the insert pose
    # (i.e. from the fresh recognition).
    # 预插入位 x 上限（负值）；IK 搜索只在其以下取可达最大值，防止搜出过于
    # 贴近本体、姿态翻转的解（2026-10-04 现场翻臂事故）。
    pre_insert_x_max_m: float = -0.20
    pre_insert_y_offset_cm: float = 0.0
    pre_insert_z_offset_cm: float = 0.0
    pre_insert_duration_s: float = 3.0
    insert_duration_s: float = 3.0
    # Push the book into the shelf from the insert pose: x drives further into
    # the shelf (negative); z nudges the book up/down while pushing (末尾还书
    # 专用，中位还书没有这个旋钮，共享插入链按字段是否存在分支）。
    push_x_offset_cm: float = -5.0
    push_z_offset_cm: float = 0.0
    # Push-pose orientation offset relative to the insert pose's
    # insert_rpy_deg orientation (deg); (0, 0, 0) keeps the insert attitude
    # (末尾还书专用，中位还书没有这个旋钮，共享插入链按字段是否存在分支）。
    push_rpy_deg: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    push_duration_s: float = 2.0
    # Partially open the gripper to release the book (not the full stroke).
    release_open_deg: float = 50.0
    # Retract from the push pose: only x changes (positive x leaves the shelf).
    retract_x_offset_cm: float = 10.0
    # Retract-pose orientation, a fixed value that does NOT follow the
    # insert/push attitude (insert_rpy_deg / push_rpy_deg have no effect on
    # it) — 末尾还书专用，中位还书没有该字段，回抽继续继承推回位姿态。
    retract_rpy_deg: Tuple[float, float, float] = (90.0, 0.0, 90.0)
    retract_duration_s: float = 2.0
    # Unified speed preset ("slow"/"standard"/"fast"); see domain.plans.speed.
    speed_level: str = "standard"
    gripper: GripperCommand = field(default_factory=GripperCommand)
    # ---- 识别后置校验与车辆对齐（post-vision，取/还/中位共用语义）----
    # 书脊与画面竖直方向夹角超过该值判定不可执行（>60° 一般为倒伏/斜插）
    spine_tilt_max_deg: float = 60.0
    # 兜底识别（轴对齐矩形）无倾斜信息时是否判失败；默认放行并记日志
    spine_tilt_fail_on_unknown: bool = False
    # 画面正中区域宽度占比（水平方向 0.40 = 正中 40% 带宽）
    align_center_band_ratio: float = 0.40
    # 车辆脉冲缓移速度（升降杆高位，务必缓速）
    align_pulse_speed_mps: float = 0.03
    # 偏差像素→毫米的经验增益（脉冲时长折算用，现场标定）
    align_px_to_mm: float = 0.5
    align_pulse_min_ms: int = 100
    align_pulse_max_ms: int = 400
    # 每次脉冲后的稳定等待
    align_settle_ms: int = 600
    align_max_moves: int = 8
    align_max_travel_m: float = 0.20
    # 画面偏右需要前进=1，需要后退=-1（相机安装方向决定，现场标定）
    align_direction_sign: int = 1

