// Base path of the on-robot tuning service (FastAPI). It shares the takeout
// service's dev proxy: /bookarm-api/ is rewritten to the service's /api/ prefix.
const TUNING_API_BASE = '/bookarm-api/v1/tuning';

/** Tuning flow discriminator: take-book / return-book / mid-return motion plan. */
export type TuningKind = 'take' | 'return' | 'midreturn';

/** Unified motion-speed preset of a motion plan. One level scales the arm
 * motion durations, the rod speed word and the gripper open/close speeds
 * (plus the speed-coupled gripper timing) together; `standard` is the
 * calibrated default and matches the legacy behaviour exactly. */
export type SpeedLevel = 'slow' | 'standard' | 'fast';

export const DEFAULT_SPEED_LEVEL: SpeedLevel = 'standard';

/** Selector options in display order; labels are Chinese for the debug UI. */
export const SPEED_LEVEL_OPTIONS: { value: SpeedLevel; label: string }[] = [
  { value: 'slow', label: '慢速（精细/首次执行）' },
  { value: 'standard', label: '标准（当前标定值）' },
  { value: 'fast', label: '快速（消除停顿间隔）' },
];

/** Reads the level out of a profile; anything unexpected means standard. */
export function normalizeSpeedLevel(profile: TuningProfile): SpeedLevel {
  const raw = profile.speed_level;
  return raw === 'slow' || raw === 'fast' ? raw : DEFAULT_SPEED_LEVEL;
}

/** The motion plan profile is a flat JSON object; nested objects (gripper)
 * appear as plain values. Kept open-ended so the panel can edit any field. */
export type TuningProfile = Record<string, unknown>;

export type TuningSessionView = {
  step_index: number;
  steps: string[];
  has_target: boolean;
  slot: number;
};

export type TuningPrepareResult = {
  ok: boolean;
  target_base_pose: number[];
  confidence: number;
};

export type TuningStepRunResult = {
  step_index: number;
  step_name: string;
  ok: boolean;
  /** Backend returns a step-specific JSON object (pose, gap info, ...). */
  detail?: unknown;
};

/** Carries the HTTP status so callers can distinguish e.g. 409 (service busy). */
export class TuningApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'TuningApiError';
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

async function tuningRequest<T>(
  kind: TuningKind,
  path: string,
  options: RequestInit = {},
): Promise<T> {
  return tuningApiRequest<T>(`${TUNING_API_BASE}/${kind}${path}`, options);
}

/** Same request pipeline for tuning endpoints not scoped to one kind. */
async function tuningApiRequest<T>(
  url: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);

  const response = await fetch(url, {
    ...options,
    headers,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new TuningApiError(
      response.status,
      detailText(body, response.status),
    );
  }
  return (await response.json()) as T;
}

function jsonOptions(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

export async function getTuningProfile(
  kind: TuningKind,
): Promise<TuningProfile> {
  return tuningRequest(kind, '/profile', { cache: 'no-store' });
}

/** Sends a partial profile; the service merges, validates and hot-applies it. */
export async function saveTuningProfile(
  kind: TuningKind,
  patch: TuningProfile,
): Promise<TuningProfile> {
  return tuningRequest(kind, '/profile', jsonOptions('PUT', patch));
}

/** Hot-applies a patch into the live plan without touching the profile file
 * (persist=false); used for edit-as-you-type tuning. */
export async function applyTuningProfile(
  kind: TuningKind,
  patch: TuningProfile,
): Promise<TuningProfile> {
  return tuningRequest(kind, '/profile?persist=false', jsonOptions('PUT', patch));
}

/** Persists the current live plan to the on-robot profile file. */
export async function persistTuningProfile(
  kind: TuningKind,
): Promise<TuningProfile> {
  return tuningRequest(kind, '/profile/save', { method: 'POST' });
}

export async function prepareTuningSession(
  kind: TuningKind,
  body: { slot: number; template_id?: string },
): Promise<TuningPrepareResult> {
  return tuningRequest(kind, '/session/prepare', jsonOptions('POST', body));
}

export async function getTuningSession(
  kind: TuningKind,
): Promise<TuningSessionView> {
  return tuningRequest(kind, '/session', { cache: 'no-store' });
}

export async function tuningStepNext(
  kind: TuningKind,
): Promise<{ ok: boolean }> {
  return tuningRequest(kind, '/session/step/next', { method: 'POST' });
}

export async function tuningStepPrevious(
  kind: TuningKind,
): Promise<{ ok: boolean }> {
  return tuningRequest(kind, '/session/step/previous', { method: 'POST' });
}

export async function resetTuningSession(
  kind: TuningKind,
): Promise<{ ok: boolean }> {
  return tuningRequest(kind, '/session/step/reset', { method: 'POST' });
}

export async function tuningStepRun(
  kind: TuningKind,
  index: number,
): Promise<TuningStepRunResult> {
  return tuningRequest(kind, '/session/step/run', jsonOptions('POST', { index }));
}

export async function tuningArmEnable(
  kind: TuningKind,
): Promise<{ ok: boolean }> {
  return tuningRequest(kind, '/arm/enable', { method: 'POST' });
}

export async function tuningArmDisable(
  kind: TuningKind,
): Promise<{ ok: boolean }> {
  return tuningRequest(kind, '/arm/disable', { method: 'POST' });
}

export async function tuningArmDebugPose(
  kind: TuningKind,
): Promise<{ ok: boolean }> {
  return tuningRequest(kind, '/arm/debug-pose', { method: 'POST' });
}

/** Motion data the current step would send if executed now (null when the
 * step has no arm motion or the target is not prepared yet). */
export type TuningStepMotion =
  | {
      type: 'tcp';
      method: string;
      duration_s: number;
      x_mm: number;
      y_mm: number;
      z_mm: number;
      rx_deg: number;
      ry_deg: number;
      rz_deg: number;
      continues: boolean;
    }
  | {
      type: 'joints';
      method: string;
      duration_s: number;
      joints_deg: number[];
      continues: boolean;
    };

export type TuningStepPreview = {
  step_index: number;
  step_name: string | null;
  motion: TuningStepMotion | null;
  /** Why motion is null (runtime dependency not ready), when applicable. */
  reason?: string | null;
};

export type TuningAllStepsPreview = {
  step_index: number;
  steps: TuningStepPreview[];
};

export async function getAllTuningStepPreviews(
  kind: TuningKind,
): Promise<TuningAllStepsPreview> {
  return tuningRequest(kind, '/session/step/preview?all=1', {
    cache: 'no-store',
  });
}

/** Book-stand slot calibration document (file shape; poses in m/rad). */
export type BookStandSlotsDocument = {
  description?: string;
  slots: Record<string, Record<string, unknown>>;
};

export type BookStandGroup = 'place' | 'preplace';

export type BookStandSlotsView = Record<
  BookStandGroup,
  BookStandSlotsDocument
>;

export async function getBookStandSlots(): Promise<BookStandSlotsView> {
  return tuningApiRequest(`${TUNING_API_BASE}/book-stand/slots`, {
    cache: 'no-store',
  });
}

/** Hot-swaps one slot group without persisting (persist=false). */
export async function applyBookStandSlots(
  group: BookStandGroup,
  document: BookStandSlotsDocument,
): Promise<BookStandSlotsDocument> {
  return tuningApiRequest(
    `${TUNING_API_BASE}/book-stand/slots/${group}?persist=false`,
    jsonOptions('PUT', document),
  );
}

/** Validates, hot-swaps and persists one slot group in one shot. */
export async function saveBookStandSlots(
  group: BookStandGroup,
  document: BookStandSlotsDocument,
): Promise<BookStandSlotsDocument> {
  return tuningApiRequest(
    `${TUNING_API_BASE}/book-stand/slots/${group}`,
    jsonOptions('PUT', document),
  );
}

export type BookStandExecuteResult = {
  ok: boolean;
  group: BookStandGroup;
  slot_id: string;
  action: string;
  method: string;
  duration_s: number;
  pose: number[];
};

/** Moves the arm to one calibrated slot pose from the live group. */
export async function executeBookStandSlot(
  group: BookStandGroup,
  options: { slotId: string; action: string; durationS?: number },
): Promise<BookStandExecuteResult> {
  return tuningApiRequest(
    `${TUNING_API_BASE}/book-stand/slots/${group}/execute`,
    jsonOptions('POST', {
      slot_id: options.slotId,
      action: options.action,
      ...(options.durationS !== undefined
        ? { duration_s: options.durationS }
        : {}),
    }),
  );
}

/** Persists the current live slot group to its calibration file. */
export async function persistBookStandSlots(
  group: BookStandGroup,
): Promise<BookStandSlotsDocument> {
  return tuningApiRequest(
    `${TUNING_API_BASE}/book-stand/slots/${group}/save`,
    { method: 'POST' },
  );
}
