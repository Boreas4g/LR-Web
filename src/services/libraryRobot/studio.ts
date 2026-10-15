// Base path of the on-robot spine-studio service (FastAPI). It shares the
// takeout service's dev proxy: /bookarm-api/ is rewritten to the service's /api/ prefix.
const STUDIO_API_BASE = '/bookarm-api/v1/studio';

export type LiftHeightView = {
  height_mm: number;
};

export type LiftMoveResult = {
  target_height_mm: number;
  measured_height_mm: number;
};

export type RodMoveResult = {
  target_angle_deg: number;
  measured_angle_deg: number;
};

export type ArmActionResult = {
  ok: boolean;
  enabled: boolean;
  was_enabled?: boolean;
  detail?: unknown;
};

export type StudioCapture = {
  id: string;
  width: number;
  height: number;
  frame_number: number;
  created_at: string;
};

export type StudioSpine = {
  library_id: string;
  index: number;
  confidence: number;
  area: number;
  bbox_xyxy: number[];
  polygon_xy: number[][];
};

export type SegmentResult = {
  capture_id: string;
  spine_count: number;
  spines: StudioSpine[];
  /** 导入/分割自检：特征不足时 ok=false 并给出 detail。 */
  selfCheck?: SelfCheck;
};

export type LibraryItem = {
  id: string;
  capture_id: string;
  spine_index: number;
  confidence: number;
  created_at: string;
  /** Operator-supplied name; present on manually segmented templates. */
  name?: string;
  /** 导入自检：特征不足时 ok=false 并给出 detail（建议重拍）。 */
  selfCheck?: SelfCheck;
};

/** 录入自检结果：ok=false 表示该书脊图特征不足，识别成功率低，建议重拍。 */
export type SelfCheck = {
  ok: boolean;
  detail?: string;
};

/** verify-all 返回的单条书脊库条目自检结果（id/name 与书脊库一致）。 */
export type LibraryVerifyItem = {
  id: string;
  name?: string;
  ok: boolean;
  detail?: string;
};

export type SegmentOptions = {
  conf?: number;
  imgsz?: number;
};

export type ManualSegmentOptions = {
  name: string;
  polygon: [number, number][];
  pad?: number;
};

/** Carries the HTTP status so callers can distinguish e.g. 409 (service busy). */
export class StudioApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'StudioApiError';
    this.status = status;
  }
}

function detailText(body: unknown, status: number) {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    // FastAPI validation failures (422) return detail as an array of issues.
    if (Array.isArray(detail)) {
      const text = detail
        .map((item) =>
          item && typeof item === 'object' && 'msg' in item
            ? String((item as { msg: unknown }).msg)
            : String(item),
        )
        .join('；');
      if (text) return text;
    }
  }
  return `请求失败（${status}）`;
}

async function studioRequest<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);

  const response = await fetch(`${STUDIO_API_BASE}${path}`, {
    ...options,
    headers,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new StudioApiError(
      response.status,
      detailText(body, response.status),
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function getLiftHeight(): Promise<LiftHeightView> {
  return studioRequest('/lift/height', { cache: 'no-store' });
}

export async function moveLift(
  heightMm: number,
  speedLevel: number,
): Promise<LiftMoveResult> {
  return studioRequest('/lift/move', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ height_mm: heightMm, speed_level: speedLevel }),
  });
}

export async function createCapture(): Promise<StudioCapture> {
  return studioRequest('/captures', { method: 'POST' });
}

export async function segmentCapture(
  id: string,
  options: SegmentOptions = {},
): Promise<SegmentResult> {
  return studioRequest(`/captures/${encodeURIComponent(id)}/segment`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(options),
  });
}

/** Store one hand-drawn spine polygon as a named library template. */
export async function manualSegmentCapture(
  id: string,
  options: ManualSegmentOptions,
): Promise<StudioSpine & { selfCheck?: SelfCheck }> {
  return studioRequest(
    `/captures/${encodeURIComponent(id)}/manual-segment`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(options),
    },
  );
}

export async function listLibrary(): Promise<{ items: LibraryItem[] }> {
  return studioRequest('/library', { cache: 'no-store' });
}

/** Run the on-robot recognition self-check over the whole spine library. */
export async function verifyLibrary(): Promise<LibraryVerifyItem[]> {
  return studioRequest('/library/verify-all', { method: 'POST' });
}

/** Upload a local spine image into the on-robot image store; returns its entry. */
export async function importLibraryImage(
  file: File,
  name = '',
): Promise<LibraryItem> {
  const form = new FormData();
  form.append('image', file);
  form.append('source', 'local-upload');
  if (name) form.append('name', name);
  return studioRequest('/library', { method: 'POST', body: form });
}

export async function deleteLibraryItem(id: string): Promise<void> {
  return studioRequest(`/library/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
}

function captureImagePath(id: string) {
  return `${STUDIO_API_BASE}/captures/${encodeURIComponent(id)}/image`;
}

function libraryImagePath(id: string) {
  return `${STUDIO_API_BASE}/library/${encodeURIComponent(id)}/image`;
}

// <img> tags cannot send custom headers, so images are fetched and handed out
// as blob URLs, like getProtectedImage in ./index.
async function getProtectedImage(url: string, label: string): Promise<string> {
  // Debug images are overwritten in place on every run; bypass the HTTP cache
  // so the page always shows the latest attempt.
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new StudioApiError(
      response.status,
      body ? detailText(body, response.status) : `${label}（${response.status}）`,
    );
  }
  return URL.createObjectURL(await response.blob());
}

export async function captureImageUrl(id: string): Promise<string> {
  return getProtectedImage(captureImagePath(id), '无法读取拍摄图像');
}

export async function libraryImageUrl(id: string): Promise<string> {
  return getProtectedImage(libraryImagePath(id), '无法读取书脊图像');
}

export type VisionDebugMeta = {
  kind?: string;
  matched?: boolean;
  template?: string;
  timestamp?: string;
  confidence?: number;
  frame_number?: number;
  pixel_uv?: number[];
  camera_point_m?: number[];
  base_point_m?: number[];
  [key: string]: unknown;
};

export async function getVisionDebug(): Promise<VisionDebugMeta> {
  return studioRequest('/vision-debug', { cache: 'no-store' });
}

export async function visionDebugImageUrl(
  kind: 'raw' | 'annotated' | 'gap_raw' | 'gap_overlay',
): Promise<string> {
  return getProtectedImage(
    `${STUDIO_API_BASE}/vision-debug/image?kind=${kind}`,
    '无法读取视觉调试图像',
  );
}

export type VisionDebugGapMeta = {
  /** false when only the spine matched; partial fields plus `reason` are set. */
  gap_found?: boolean;
  /** Why no gap was reported (spine matched but no gap, etc.). */
  reason?: string;
  gap_method?: string;
  select?: string;
  selected_side?: 'left' | 'right';
  left_gap_width_px?: number | null;
  right_gap_width_px?: number | null;
  gap_width_px?: number;
  gap_center_uv?: number[];
  scan_row_v?: number;
  scan_height_ratio?: number;
  scan_band_px?: number;
  depth_gap_mm?: number;
  reference_depth_m?: number;
  gap_depth_m?: number;
  y1_m?: number;
  spine_bbox_xyxy?: number[];
  spine_confidence?: number;
  camera_point_m?: number[];
  base_point_m?: number[];
  timestamp?: string;
  [key: string]: unknown;
};

export async function getVisionDebugGap(): Promise<VisionDebugGapMeta> {
  return studioRequest('/vision-debug/gap', { cache: 'no-store' });
}

/** Capture one frame and run spine-match + depth-gap analysis on it. */
export async function runVisionDebug(
  values: {
    templateId?: string;
    select?: 'largest' | 'nearest';
    scanHeightRatio?: number;
    scanBandPx?: number;
    depthGapMm?: number;
  } = {},
): Promise<VisionDebugGapMeta> {
  return studioRequest('/vision-debug/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...(values.templateId ? { template_id: values.templateId } : {}),
      ...(values.select ? { select: values.select } : {}),
      ...(values.scanHeightRatio !== undefined
        ? { scan_height_ratio: values.scanHeightRatio }
        : {}),
      ...(values.scanBandPx !== undefined
        ? { scan_band_px: values.scanBandPx }
        : {}),
      ...(values.depthGapMm !== undefined
        ? { depth_gap_mm: values.depthGapMm }
        : {}),
    }),
  });
}

export async function resetArmToDebugPose(
  durationS = 3.0,
): Promise<{ ok: boolean; joints_rad: number[] }> {
  return studioRequest('/arm/debug-pose', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ duration_s: durationS }),
  });
}

export async function enableArm(): Promise<ArmActionResult> {
  return studioRequest('/arm/enable', { method: 'POST' });
}

export async function disableArm(
  durationS = 3.0,
): Promise<ArmActionResult> {
  return studioRequest('/arm/disable', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ duration_s: durationS }),
  });
}

export type ArmJointsView = {
  enabled: boolean;
  joints_rad: number[];
  joints_deg: number[];
  tcp?: unknown;
};

export async function getArmJoints(): Promise<ArmJointsView> {
  return studioRequest('/arm/joints');
}

export async function moveArmJoints(
  jointsDeg: number[],
  durationS = 3.0,
): Promise<ArmJointsView> {
  return studioRequest('/arm/joints/move', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ joints_deg: jointsDeg, duration_s: durationS }),
  });
}

export async function moveArmToZero(
  durationS = 3.0,
): Promise<ArmJointsView> {
  return studioRequest('/arm/zero', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ duration_s: durationS }),
  });
}

export type ArmZeroCalibrationResult = {
  ok: boolean;
  motor: number;
  results: Record<string, boolean>;
};

export async function calibrateArmZero(
  motor: number,
): Promise<ArmZeroCalibrationResult> {
  return studioRequest('/arm/zero-calibration', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ motor, confirm: true }),
  });
}

export type ArmFaultClearResult = {
  results: Record<string, boolean>;
  fault_codes: Record<string, number | null>;
};

/** Clear latched motor fault codes (operator recovery; re-enable afterwards). */
export async function clearArmFaults(): Promise<ArmFaultClearResult> {
  return studioRequest('/arm/faults/clear', { method: 'POST' });
}

export async function moveRod(
  targetDeg: number,
  options: {
    speed?: number;
    acceleration?: number;
    toleranceDeg?: number;
    timeoutS?: number;
  } = {},
): Promise<RodMoveResult> {
  return studioRequest('/rod/move', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      target_deg: targetDeg,
      speed: options.speed ?? 1000,
      acceleration: options.acceleration ?? 50,
      tolerance_deg: options.toleranceDeg ?? 1.5,
      timeout_s: options.timeoutS ?? 12,
    }),
  });
}

export type ThermalLockout = {
  message: string;
  reason: string;
  motor_id: number;
  temperature_c: number;
  limit_c: number;
  /** true = 已安全归位后失能；false = 归位失败直接失能。 */
  safe_posed: boolean;
  detail: string | null;
};

export type ArmThermalStatus = {
  limit_c: number;
  temperatures_c: Record<string, number>;
  lockout: ThermalLockout | null;
};

export async function getArmThermal(): Promise<ArmThermalStatus> {
  return studioRequest('/arm/thermal', { cache: 'no-store' });
}

export async function clearArmThermalLockout(): Promise<ArmThermalStatus> {
  return studioRequest('/arm/thermal-lockout/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
}

export type TcpPoseView = {
  x_mm: number;
  y_mm: number;
  z_mm: number;
  rx_deg: number;
  ry_deg: number;
  rz_deg: number;
};

export type HardwareStatus = {
  can: { interface: string; exists: boolean; up: boolean };
  arm: {
    wired: boolean;
    health?: {
      enabled: boolean;
      healthy_feedback: boolean;
      reason: string | null;
      can_fps: number;
      control_loop_running: boolean;
      joint_feedback: Record<
        string,
        { age_s: number | null; mode_state: number | null; fault_code: number | null }
      > | null;
    } | null;
    health_error: string | null;
    thermal?: ArmThermalStatus | null;
    pose?: {
      values_rad: number[];
      /** TCP pose from Pinocchio FK; null when FK is unavailable. */
      tcp?: TcpPoseView | null;
    } | null;
    pose_error?: string | null;
    /** Gripper (joint 7) carry hold state; see the two-stage grip logic. */
    gripper_carry_armed?: boolean;
    gripper_carry_derated?: boolean;
    gripper_thermal_warning?: string | null;
  };
  lift: {
    connected: boolean;
    height_mm: number | null;
    error: string | null;
    /** Serial auto-reconnects after USB re-enumeration (when wired). */
    reconnects?: number | null;
  };
  rod: {
    wired: boolean;
    connected?: boolean;
    angle_deg?: number | null;
    error?: string | null;
    reconnects?: number | null;
  };
  camera: {
    present: boolean | null;
    /** Auto pipeline rebuilds after USB frame timeouts (when camera is wired). */
    pipeline_recoveries?: number;
    pipeline_last_recovery_at?: number | null;
  };
  devices: Record<string, boolean>;
};

export async function getHardwareStatus(): Promise<HardwareStatus> {
  return studioRequest('/hardware/status', { cache: 'no-store' });
}

export type JogAxis = 'x' | 'y' | 'z' | 'rx' | 'ry' | 'rz';

export type CartesianJogResult = {
  ok: boolean;
  axis: JogAxis;
  /** Clamped step actually applied (mm for x/y/z, deg for rx/ry/rz). */
  delta_applied: number;
  duration_s: number;
  tcp: TcpPoseView;
};

export async function cartesianJog(options: {
  axis: JogAxis;
  delta: number;
  durationS?: number;
}): Promise<CartesianJogResult> {
  return studioRequest('/arm/cartesian-jog', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      axis: options.axis,
      delta: options.delta,
      duration_s: options.durationS ?? 2.0,
    }),
  });
}

/** Live D435-to-base extrinsic: 6-DOF pose plus source bookkeeping. */
export type CameraTransformView = TcpPoseView & {
  matrix: number[][];
  source: string;
  /** True when the user override file (camera_to_base.json) is in effect. */
  override: boolean;
};

export async function getCameraTransform(): Promise<CameraTransformView> {
  return studioRequest('/camera-transform', { cache: 'no-store' });
}

/** Validates, persists and hot-swaps the extrinsic; pose fields only, the
 * backend composes the 4x4 matrix (single Euler convention). */
export async function saveCameraTransform(
  pose: TcpPoseView,
): Promise<CameraTransformView> {
  return studioRequest('/camera-transform', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(pose),
  });
}

/** Deletes the user override and falls back to the packaged example. */
export async function resetCameraTransform(): Promise<CameraTransformView> {
  return studioRequest('/camera-transform/reset', { method: 'POST' });
}

// ---- 识别后校验调试（书脊倾斜判定 / 车辆居中脉冲对齐）----

export type VisionSpineCheckResult = {
  matched: boolean;
  detail?: string;
  tilt_deg?: number | null;
  tilt_max_deg?: number;
  tilt_ok?: boolean;
  center_x_ratio?: number | null;
  center_band?: [number, number];
  in_band?: boolean | null;
  polygon_uv?: [number, number][];
  frame_width_px?: number;
  frame_height_px?: number;
  capture_id?: string;
  confidence?: number;
};

export type VisionAlignRunResult = {
  matched: boolean;
  detail?: string;
  tilt_deg?: number | null;
  tilt_max_deg?: number;
  aligned?: boolean;
  align_skipped?: string;
  align_moves?: number;
  align_travel_m?: number;
  align_center_x_final?: number;
  align_pulses?: { direction: string; duration_ms: number }[];
  final_center_x_ratio?: number | null;
  final_capture_id?: string;
};

export type VehicleAlignStatus = {
  ok: boolean;
  moving: boolean;
  out_linear: number;
  travel_m_est: number;
  travel_locked: boolean;
  pulse_speed: number;
  accel: number;
};

/** 拍照匹配书脊模板并计算倾斜角/居中位置（不动车）。 */
export async function visionDebugSpineCheck(
  libraryId: string,
): Promise<VisionSpineCheckResult> {
  return studioRequest('/vision-debug/spine-check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ library_id: libraryId }),
  });
}

/** 完整实测：倾斜判定 + 车辆缓速脉冲对齐（车辆会动！）。 */
export async function visionDebugAlignRun(
  libraryId: string,
): Promise<VisionAlignRunResult> {
  return studioRequest('/vision-debug/align-run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ library_id: libraryId }),
  });
}

/** 车端缓速脉冲服务状态（bookarm 透传）。 */
export async function getVehicleAlignStatus(): Promise<VehicleAlignStatus> {
  return studioRequest('/vision-debug/vehicle-status', { cache: 'no-store' });
}
