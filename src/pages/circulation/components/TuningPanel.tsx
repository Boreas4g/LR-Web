import {
  AimOutlined,
  CaretRightOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  RollbackOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Empty,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
  theme,
} from 'antd';
import { type ReactNode, useEffect, useRef, useState } from 'react';
import { listBooks } from '@/services/libraryRobot';
import {
  type ArmThermalStatus,
  clearArmThermalLockout,
  getArmThermal,
  getHardwareStatus,
  getLiftHeight,
  type HardwareStatus,
  moveLift,
} from '@/services/libraryRobot/studio';
import {
  type BookStandGroup,
  type BookStandSlotsDocument,
  DEFAULT_SPEED_LEVEL,
  executeBookStandSlot,
  getAllTuningStepPreviews,
  getBookStandSlots,
  getTuningProfile,
  getTuningSession,
  normalizeSpeedLevel,
  prepareTuningSession,
  SPEED_LEVEL_OPTIONS,
  saveBookStandSlots,
  saveTuningProfile,
  type TuningKind,
  type TuningProfile,
  type TuningSessionView,
  type TuningStepMotion,
  tuningArmDebugPose,
  tuningArmDisable,
  tuningArmEnable,
  tuningStepRun,
} from '@/services/libraryRobot/tuning';
import type { Book } from '@/services/libraryRobot/types';
import { errorMessage } from '@/utils/errorText';

// Shared panel for the takeout/return motion-plan tuning pages. The pages only
// supply the step list and the profile-field grouping; all session/profile
// logic lives here so both flows behave identically.

export type TuningFieldDef = {
  /** Dot path into the profile JSON, e.g. `pregrasp_offset_cm` or
   * `gripper.open_deg`. Fields missing from the loaded profile are hidden. */
  path: string;
  label: string;
  /** Unit suffix shown with the editor, e.g. cm / deg / s / deg·s⁻¹。 */
  unit?: string;
  /** Component labels for array values, e.g. ['x','y','z'] or 6 joint names. */
  components?: string[];
  /** Display = stored × scale, stored = display ÷ scale. Used to show radian
   * joint values as degrees. */
  scale?: number;
  precision?: number;
  min?: number;
  max?: number;
  step?: number;
  /** Editor kind. Defaults to 'number'; 'select' edits string fields. */
  input?: 'number' | 'select';
  /** Options for `input: 'select'`. */
  options?: { value: string; label: string }[];
  /** Value used when the loaded profile lacks this key: the editor still
   * renders (scalar or per-component array) and the first edit adds the key
   * to the patch. Without it, missing keys hide the editor. */
  fallback?: number | number[];
};

export type TuningStepDef = {
  /** Step title in execution order; index-aligned with the backend session. */
  title: string;
  hint?: string;
  fields: TuningFieldDef[];
};

export type TuningPanelProps = {
  kind: TuningKind;
  pageTitle: string;
  pageContent: string;
  steps: TuningStepDef[];
  /** Slot numbers offered by the prepare dialog. */
  slotValues: number[];
  /** Optional PageContainer header tabs (flow switcher on the merged page). */
  tabList?: { key: string; tab: ReactNode }[];
  tabActiveKey?: string;
  onTabChange?: (key: string) => void;
};

const RAD_TO_DEG = 180 / Math.PI;
/** Scale for radian-stored joint poses shown as degrees. */
export const JOINT_SCALE = RAD_TO_DEG;

/** Reads the HTTP status carried by the tuning ApiError class. */
function apiErrorStatus(error: unknown) {
  if (error && typeof error === 'object' && 'status' in error) {
    const status = (error as { status: unknown }).status;
    if (typeof status === 'number') return status;
  }
  return undefined;
}

function getPath(source: Record<string, unknown>, path: string): unknown {
  let current: unknown = source;
  for (const key of path.split('.')) {
    if (!current || typeof current !== 'object') return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

function setPath(
  target: Record<string, unknown>,
  path: string,
  value: unknown,
) {
  const keys = path.split('.');
  let current = target;
  for (const key of keys.slice(0, -1)) {
    const next = current[key];
    if (!next || typeof next !== 'object' || Array.isArray(next)) {
      current[key] = {};
    }
    current = current[key] as Record<string, unknown>;
  }
  current[keys[keys.length - 1]] = value;
}

/** Top-level diff between the last applied profile and the edited draft. */
function computePatch(
  base: TuningProfile,
  edited: TuningProfile,
): TuningProfile {
  const changed: TuningProfile = {};
  for (const key of Object.keys(edited)) {
    if (JSON.stringify(base[key]) !== JSON.stringify(edited[key])) {
      changed[key] = edited[key];
    }
  }
  return changed;
}

/** Debounce before an edit is hot-applied to the live plan (persist=false). */
const APPLY_DEBOUNCE_MS = 600;

/** Profile field behind the action-bar speed-level selector. */
const SPEED_LEVEL_FIELD: TuningFieldDef = {
  path: 'speed_level',
  label: '运动速度档位',
  input: 'select',
  options: SPEED_LEVEL_OPTIONS,
};

const MOTION_METHOD_LABELS: Record<string, string> = {
  move_j: 'MoveJ',
  move_l: 'MoveL',
  move_p_by_j: 'MovePByJ',
};

/** One-line summary of the motion data a step is about to send. */
function previewText(motion: TuningStepMotion): string {
  const method = MOTION_METHOD_LABELS[motion.method] ?? motion.method;
  const tail = motion.continues ? '（本步后续还有动作）' : '';
  if (motion.type === 'joints') {
    return `${method} 关节 [${motion.joints_deg
      .map((value) => value.toFixed(1))
      .join(', ')}]° · ${motion.duration_s}s${tail}`;
  }
  return (
    `${method} TCP X ${motion.x_mm.toFixed(1)} · Y ${motion.y_mm.toFixed(1)} · ` +
    `Z ${motion.z_mm.toFixed(1)} mm ｜ Rx ${motion.rx_deg.toFixed(1)}° · ` +
    `Ry ${motion.ry_deg.toFixed(1)}° · Rz ${motion.rz_deg.toFixed(1)}° · ` +
    `${motion.duration_s}s${tail}`
  );
}

/** Compact one-line summary of a step-run detail payload for toasts. */
function stepDetailSummary(detail: unknown): string {
  if (!detail || typeof detail !== 'object') {
    return typeof detail === 'string' ? detail : '';
  }
  const data = detail as Record<string, unknown>;
  const parts: string[] = [];
  if (typeof data.y1_m === 'number') {
    parts.push(`Y1 = ${(data.y1_m * 100).toFixed(1)} cm`);
  }
  if (typeof data.selected_side === 'string') {
    parts.push(data.selected_side === 'left' ? '左侧缝' : '右侧缝');
  }
  if (typeof data.gap_width_px === 'number') {
    parts.push(`宽 ${data.gap_width_px.toFixed(0)} px`);
  }
  if (typeof data.open_deg === 'number') parts.push(`张开 ${data.open_deg}°`);
  if (typeof data.rod_deg === 'number') parts.push(`拨杆 ${data.rod_deg}°`);
  return parts.join(' · ');
}

type SlotPoseField = {
  key: string | null;
  label: string;
  durationKey: string;
};

/** Pose rows edited per slot; the null-key row edits a duration only. */
const SLOT_POSE_FIELDS: Record<BookStandGroup, SlotPoseField[]> = {
  place: [
    { key: 'pre_place', label: '预放置', durationKey: 'pre_place_duration_s' },
    { key: 'place', label: '放置', durationKey: 'place_duration_s' },
    {
      key: 'lower_place',
      label: '下放',
      durationKey: 'lower_place_duration_s',
    },
    { key: 'retreat', label: '撤回', durationKey: 'retreat_duration_s' },
  ],
  preplace: [
    { key: 'pre_grasp', label: '预抓取', durationKey: 'pre_grasp_duration_s' },
    { key: 'grasp', label: '抓取', durationKey: 'grasp_duration_s' },
    { key: null, label: '撤回（仅时长）', durationKey: 'retreat_duration_s' },
  ],
};

const SLOT_GROUP_TITLES: Record<BookStandGroup, string> = {
  place: '书立放置槽位（book_stand_slots.json）',
  preplace: '书立取回槽位（book_stand_pickup_slots.json）',
};

const SLOT_COMPONENTS: { label: string; angle: boolean }[] = [
  { label: 'X', angle: false },
  { label: 'Y', angle: false },
  { label: 'Z', angle: false },
  { label: 'Rx', angle: true },
  { label: 'Ry', angle: true },
  { label: 'Rz', angle: true },
];

/** Avoids float noise when converting deg input back to stored radians. */
function round6(value: number) {
  return Math.round(value * 1_000_000) / 1_000_000;
}

export default function TuningPanel({
  kind,
  pageTitle,
  pageContent,
  steps,
  slotValues,
  tabList,
  tabActiveKey,
  onTabChange,
}: TuningPanelProps) {
  const { message } = App.useApp();
  const { token } = theme.useToken();
  const [profile, setProfile] = useState<TuningProfile>();
  const [draft, setDraft] = useState<TuningProfile>();
  const [profileError, setProfileError] = useState<string>();
  const [session, setSession] = useState<TuningSessionView>();
  const [pending, setPending] = useState<string>();
  const applyTimerRef = useRef(0);
  /** Guards against out-of-order apply responses after edits or kind switches. */
  const applySeqRef = useRef(0);
  const editRef = useRef<{
    profile?: TuningProfile;
    draft?: TuningProfile;
  }>({});
  const [stepPreviews, setStepPreviews] = useState<
    { motion: TuningStepMotion | null; reason: string | null }[]
  >([]);
  /** Book-stand slots: last applied document and the edited copy (file units). */
  const [slotsApplied, setSlotsApplied] = useState<BookStandSlotsDocument>();
  const [slotsDraft, setSlotsDraft] = useState<BookStandSlotsDocument>();
  const [slotsError, setSlotsError] = useState<string>();
  const slotsTimerRef = useRef(0);
  const slotsSeqRef = useRef(0);
  const slotsEditRef = useRef<{
    applied?: BookStandSlotsDocument;
    draft?: BookStandSlotsDocument;
  }>({});
  const [prepareOpen, setPrepareOpen] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [prepareSlot, setPrepareSlot] = useState<number>(1);
  const [prepareBookId, setPrepareBookId] = useState<string>();
  const [books, setBooks] = useState<Book[]>([]);
  const [booksLoading, setBooksLoading] = useState(false);
  const [booksLoaded, setBooksLoaded] = useState(false);
  const [thermal, setThermal] = useState<ArmThermalStatus>();
  const [armPose, setArmPose] = useState<HardwareStatus['arm']['pose']>();
  const [armEnabled, setArmEnabled] = useState<boolean>();
  const [liftHeight, setLiftHeight] = useState<number>();
  const [liftTarget, setLiftTarget] = useState<number>(800);
  const [liftSpeed, setLiftSpeed] = useState<number>(2);
  const [liftMoving, setLiftMoving] = useState(false);

  const actionBusy = pending !== undefined;
  const speedLevel = draft ? normalizeSpeedLevel(draft) : DEFAULT_SPEED_LEVEL;

  editRef.current = { profile, draft };
  slotsEditRef.current = { applied: slotsApplied, draft: slotsDraft };
  /** Take places books into the stand; return/mid-return pick up from it. */
  const slotsGroup: BookStandGroup = kind === 'take' ? 'place' : 'preplace';

  const refreshPreview = async () => {
    try {
      const view = await getAllTuningStepPreviews(kind);
      setStepPreviews(
        view.steps.map((entry) => ({
          motion: entry.motion ?? null,
          reason: entry.reason ?? null,
        })),
      );
    } catch {
      // 预览加载失败保持旧值，不干扰调参主流程。
    }
  };

  const refreshSlots = async () => {
    try {
      const view = await getBookStandSlots();
      const document = view[slotsGroup];
      setSlotsApplied(structuredClone(document));
      setSlotsDraft(structuredClone(document));
      setSlotsError(undefined);
    } catch (error) {
      setSlotsError(errorMessage(error, '无法读取书立槽位标定。'));
    }
  };

  const refreshThermal = async () => {
    try {
      setThermal(await getArmThermal());
    } catch {
      // 温度看板静默失败，避免干扰调参主流程。
    }
    try {
      const status = await getHardwareStatus();
      setArmPose(status.arm.pose ?? null);
      setArmEnabled(status.arm.health?.enabled ?? undefined);
    } catch {
      // 位姿看板同样静默失败。
    }
  };

  async function clearLockout() {
    setPending('thermal-clear');
    try {
      await clearArmThermalLockout();
      message.success('温度保护锁定已解除。');
      await refreshThermal();
    } catch (error) {
      // 后端会拒绝未冷却的解锁并说明原因（通常仍有轴超过阈值）。
      reportError(error, '解除锁定失败。');
    } finally {
      setPending(undefined);
    }
  }

  const refreshLiftHeight = async () => {
    try {
      const { height_mm } = await getLiftHeight();
      setLiftHeight(height_mm);
    } catch {
      // 同上，静默。
    }
  };

  async function moveLiftNow() {
    setLiftMoving(true);
    try {
      const result = await moveLift(liftTarget, liftSpeed);
      setLiftHeight(result.measured_height_mm);
      message.success(
        `升降台已到位：${result.measured_height_mm.toFixed(1)} mm。`,
      );
    } catch (error) {
      message.error(errorMessage(error, '升降台移动失败。'));
    } finally {
      setLiftMoving(false);
    }
  }

  useEffect(() => {
    getTuningProfile(kind)
      .then((loaded) => {
        setProfile(loaded);
        setDraft(structuredClone(loaded));
      })
      .catch((error: unknown) =>
        setProfileError(errorMessage(error, '无法读取运动计划参数。')),
      );
    refreshSession();
    void refreshThermal();
    void refreshLiftHeight();
    void refreshSlots();
    // 温度与位姿看板每秒自动刷新；升降台高度跟随手动刷新。
    const thermalTimer = window.setInterval(() => void refreshThermal(), 1_000);
    return () => {
      window.clearInterval(thermalTimer);
      // 切换流程或卸载时丢弃未发的热生效请求及其迟到响应。
      window.clearTimeout(applyTimerRef.current);
      applySeqRef.current += 1;
      window.clearTimeout(slotsTimerRef.current);
      slotsSeqRef.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind]);

  useEffect(() => {
    if (!prepareOpen || booksLoaded) return;
    void refreshBooks();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prepareOpen, booksLoaded]);

  async function refreshSession() {
    try {
      setSession(await getTuningSession(kind));
    } catch (error) {
      message.error(errorMessage(error, '无法读取步骤进度。'));
    }
    void refreshPreview();
  }

  async function refreshBooks() {
    setBooksLoading(true);
    try {
      setBooks(await listBooks());
      setBooksLoaded(true);
    } catch (error) {
      message.error(errorMessage(error, '无法读取书籍管理。'));
    } finally {
      setBooksLoading(false);
    }
  }

  function reportError(error: unknown, fallback: string) {
    if (apiErrorStatus(error) === 409) {
      message.warning('服务正忙：请等待当前动作结束后再试。');
    } else {
      message.error(errorMessage(error, fallback));
    }
  }

  async function runAction(
    key: string,
    action: () => Promise<void>,
    fallback: string,
  ) {
    setPending(key);
    try {
      await action();
    } catch (error) {
      reportError(error, fallback);
    } finally {
      setPending(undefined);
    }
  }

  function updateField(
    def: TuningFieldDef,
    displayValue: number,
    index?: number,
  ) {
    const stored = def.scale ? round6(displayValue / def.scale) : displayValue;
    setDraft((previous) => {
      if (!previous) return previous;
      const next = structuredClone(previous);
      if (def.components && index !== undefined) {
        const raw = getPath(next, def.path);
        const vector = Array.isArray(raw) ? [...raw] : [];
        vector[index] = stored;
        setPath(next, def.path, vector);
      } else {
        setPath(next, def.path, stored);
      }
      return next;
    });
    scheduleApply();
  }

  function updateSelectField(def: TuningFieldDef, value: string) {
    setDraft((previous) => {
      if (!previous) return previous;
      const next = structuredClone(previous);
      setPath(next, def.path, value);
      return next;
    });
    scheduleApply();
  }

  async function autoApply(pendingPatch: TuningProfile) {
    const seq = ++applySeqRef.current;
    try {
      // persist=true：防抖合并后直接热生效并落盘，一次完成。
      const result = await saveTuningProfile(kind, pendingPatch);
      if (seq !== applySeqRef.current) return;
      setProfile(structuredClone(result));
      void refreshPreview();
    } catch (error) {
      if (seq !== applySeqRef.current) return;
      reportError(
        error,
        '参数自动保存失败：修改未生效且未写入文件，已保留输入。',
      );
    }
  }

  async function autoApplySlots(document: BookStandSlotsDocument) {
    const seq = ++slotsSeqRef.current;
    try {
      const result = await saveBookStandSlots(slotsGroup, document);
      if (seq !== slotsSeqRef.current) return;
      setSlotsApplied(structuredClone(result));
      // 槽位热替换后，书立相关步骤的待发送位置随之变化。
      void refreshPreview();
    } catch (error) {
      if (seq !== slotsSeqRef.current) return;
      reportError(
        error,
        '槽位自动保存失败：修改未生效且未写入文件，已保留输入。',
      );
    }
  }

  function scheduleSlotsApply() {
    window.clearTimeout(slotsTimerRef.current);
    slotsTimerRef.current = window.setTimeout(() => {
      const { applied, draft: edited } = slotsEditRef.current;
      if (!applied || !edited) return;
      if (JSON.stringify(applied) === JSON.stringify(edited)) return;
      void autoApplySlots(structuredClone(edited));
    }, APPLY_DEBOUNCE_MS);
  }

  function updateSlotPose(
    slotId: string,
    poseKey: string,
    component: number,
    angle: boolean,
    displayValue: number,
  ) {
    const stored = angle ? displayValue / RAD_TO_DEG : displayValue / 1000;
    setSlotsDraft((previous) => {
      if (!previous) return previous;
      const next = structuredClone(previous);
      const slot = next.slots[slotId] ?? {};
      next.slots[slotId] = slot;
      const pose = Array.isArray(slot[poseKey])
        ? [...(slot[poseKey] as number[])]
        : [0, 0, 0, 0, 0, 0];
      pose[component] = round6(stored);
      slot[poseKey] = pose;
      return next;
    });
    scheduleSlotsApply();
  }

  function updateSlotDuration(
    slotId: string,
    durationKey: string,
    value: number,
  ) {
    setSlotsDraft((previous) => {
      if (!previous) return previous;
      const next = structuredClone(previous);
      const slot = next.slots[slotId] ?? {};
      next.slots[slotId] = slot;
      slot[durationKey] = value;
      return next;
    });
    scheduleSlotsApply();
  }

  async function executeSlotPose(
    slotId: string,
    action: string,
    label: string,
  ) {
    const key = `slot-exec-${slotId}-${action}`;
    setPending(key);
    try {
      const rawDuration =
        slotsDraft?.slots[slotId]?.[
          SLOT_POSE_FIELDS[slotsGroup].find((field) => field.key === action)
            ?.durationKey ?? ''
        ];
      await executeBookStandSlot(slotsGroup, {
        slotId,
        action,
        ...(typeof rawDuration === 'number' ? { durationS: rawDuration } : {}),
      });
      message.success(`槽位 ${slotId}「${label}」已到位。`);
    } catch (error) {
      // 409 可能是未使能或服务正忙，服务端 detail 更有信息量。
      if (apiErrorStatus(error) === 409) {
        message.warning(errorMessage(error, '操作冲突。'));
      } else {
        reportError(error, '槽位执行失败。');
      }
    } finally {
      setPending(undefined);
    }
  }

  function scheduleApply() {
    window.clearTimeout(applyTimerRef.current);
    applyTimerRef.current = window.setTimeout(() => {
      const { profile: base, draft: edited } = editRef.current;
      if (!base || !edited) return;
      const pendingPatch = computePatch(base, edited);
      if (Object.keys(pendingPatch).length > 0) void autoApply(pendingPatch);
    }, APPLY_DEBOUNCE_MS);
  }

  async function prepare() {
    setPreparing(true);
    try {
      const prepareBook = books.find((book) => book.id === prepareBookId);
      const result = await prepareTuningSession(kind, {
        slot: prepareSlot,
        ...(prepareBook ? { template_id: prepareBook.templateId } : {}),
      });
      message.success(
        `目标识别成功：置信度 ${result.confidence.toFixed(3)}，目标基座位姿 [${result.target_base_pose
          .map((value) => value.toFixed(3))
          .join(', ')}]。`,
      );
      setPrepareOpen(false);
      await refreshSession();
    } catch (error) {
      reportError(error, '目标识别失败。');
    } finally {
      setPreparing(false);
    }
  }

  function runStep(index: number) {
    void runAction(
      `run-${index}`,
      async () => {
        const result = await tuningStepRun(kind, index);
        if (result.ok) {
          const summary = stepDetailSummary(result.detail);
          message.success(
            `步骤「${result.step_name}」执行成功。${summary}`,
            2.5,
          );
        } else {
          message.error(
            `步骤「${result.step_name}」执行失败。${result.detail ?? ''}`,
          );
        }
        await refreshSession();
      },
      '步骤执行失败。',
    );
  }

  function fieldEditor(def: TuningFieldDef) {
    if (!profile || !draft) {
      return null;
    }
    const raw = getPath(draft, def.path) ?? def.fallback;
    if (raw === undefined) {
      return null;
    }
    if (def.input === 'select') {
      const value = typeof raw === 'string' ? raw : undefined;
      return (
        <div key={def.path} style={{ marginBottom: 8 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {def.label}
          </Typography.Text>
          <Select
            value={value}
            options={def.options}
            style={{ width: '100%' }}
            onChange={(next) => updateSelectField(def, next)}
          />
        </div>
      );
    }
    if (def.components) {
      const vector = Array.isArray(raw) ? raw : [];
      return (
        <div key={def.path} style={{ marginBottom: 8 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {def.label}
            {def.unit ? `（${def.unit}）` : ''}
          </Typography.Text>
          <div>
            <Space.Compact block>
              {def.components.map((component, index) => {
                const stored = vector[index];
                const display =
                  typeof stored === 'number'
                    ? round6(stored * (def.scale ?? 1))
                    : undefined;
                return (
                  <InputNumber
                    key={component}
                    addonBefore={component}
                    value={display}
                    precision={def.precision}
                    min={def.min}
                    max={def.max}
                    step={def.step}
                    style={{ width: '100%' }}
                    onChange={(value) => {
                      if (typeof value === 'number')
                        updateField(def, value, index);
                    }}
                  />
                );
              })}
            </Space.Compact>
          </div>
        </div>
      );
    }
    const display =
      typeof raw === 'number' ? round6(raw * (def.scale ?? 1)) : undefined;
    return (
      <div key={def.path} style={{ marginBottom: 8 }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {def.label}
        </Typography.Text>
        <InputNumber
          value={display}
          addonAfter={def.unit}
          precision={def.precision}
          min={def.min}
          max={def.max}
          step={def.step}
          style={{ width: '100%' }}
          onChange={(value) => {
            if (typeof value === 'number') updateField(def, value);
          }}
        />
      </div>
    );
  }

  return (
    <PageContainer
      title={pageTitle}
      content={pageContent}
      tabList={tabList}
      tabActiveKey={tabActiveKey}
      onTabChange={onTabChange}
    >
      {profileError ? (
        <Alert
          showIcon
          type="error"
          title={`无法连接${pageTitle}服务`}
          description={`${profileError} 请确认机器人本机的受管 Unix socket 服务已启动；开发环境经 /bookarm-api 代理转发。`}
          style={{ marginBottom: 16 }}
        />
      ) : null}
      {/* 操作条与温度位姿看板合并吸顶：调参全程可点使能/失能。 */}
      <div
        style={{
          position: 'sticky',
          top: 0,
          zIndex: 10,
          background: token.colorBgLayout,
          borderBottom: `1px solid ${token.colorBorderSecondary}`,
          marginBottom: 16,
          paddingBottom: 8,
        }}
      >
        <Card size="small" style={{ marginBottom: 8 }}>
          <div
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: 8,
              alignItems: 'center',
              justifyContent: 'space-between',
            }}
          >
            <Space wrap>
              <Popconfirm
                title="运动到调试位？"
                description="机械臂将使能（如需要）并运动到调试位。请确认运动范围内无人、周边无干涉且实体急停可触达。"
                okText="确认运动"
                cancelText="再检查一下"
                okButtonProps={{
                  danger: true,
                  loading: pending === 'debug-pose',
                }}
                onConfirm={() =>
                  void runAction(
                    'debug-pose',
                    async () => {
                      await tuningArmDebugPose(kind);
                      message.success('机械臂已运动到调试位。');
                    },
                    '运动到调试位失败。',
                  )
                }
              >
                <Button
                  icon={<RollbackOutlined />}
                  loading={pending === 'debug-pose'}
                  disabled={actionBusy && pending !== 'debug-pose'}
                >
                  运动到调试位
                </Button>
              </Popconfirm>
              <Button
                icon={<ThunderboltOutlined />}
                loading={pending === 'enable'}
                disabled={actionBusy && pending !== 'enable'}
                onClick={() =>
                  void runAction(
                    'enable',
                    async () => {
                      await tuningArmEnable(kind);
                      message.success('机械臂已使能。');
                      void refreshThermal();
                    },
                    '使能失败。',
                  )
                }
              >
                使能
              </Button>
              <Popconfirm
                title="失能机械臂？"
                description="失能前请确认机械臂已停靠在调试位或待机位等安全位姿：失能后关节失去保持力矩，臂体可能在重力作用下缓慢下垂。请确认实体急停可触达。"
                okText="确认失能"
                cancelText="再检查一下"
                okButtonProps={{ danger: true, loading: pending === 'disable' }}
                onConfirm={() =>
                  void runAction(
                    'disable',
                    async () => {
                      await tuningArmDisable(kind);
                      message.success('机械臂已失能。');
                      void refreshThermal();
                    },
                    '失能失败。',
                  )
                }
              >
                <Button
                  danger
                  icon={<PoweroffOutlined />}
                  loading={pending === 'disable'}
                  disabled={actionBusy && pending !== 'disable'}
                >
                  失能
                </Button>
              </Popconfirm>
              <Button
                icon={<AimOutlined />}
                disabled={actionBusy}
                onClick={() => setPrepareOpen(true)}
              >
                准备（识别目标）
              </Button>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                修改自动保存并实时生效
              </Typography.Text>
            </Space>
            <div style={{ textAlign: 'right' }}>
              <Space wrap size={8}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  运动速度档位
                </Typography.Text>
                <Select
                  size="small"
                  style={{ width: 200 }}
                  value={speedLevel}
                  options={SPEED_LEVEL_OPTIONS}
                  disabled={!draft}
                  onChange={(value) =>
                    updateSelectField(SPEED_LEVEL_FIELD, value)
                  }
                />
                {session ? (
                  <Tag color={session.has_target ? 'success' : 'default'}>
                    {session.has_target
                      ? `已识别目标（格口 ${session.slot}）`
                      : '未识别目标'}
                  </Tag>
                ) : null}
              </Space>
              <Typography.Text
                type="secondary"
                style={{ display: 'block', fontSize: 12, marginTop: 4 }}
              >
                档位对机械臂动作时长、拨杆速度与夹爪开/闭速度同时生效；页面参数为基准（标准档）数值，实际动作按档位缩放。
              </Typography.Text>
            </div>
          </div>
        </Card>

        {/* 温度位姿看板随操作条吸顶；升降台不吸顶，在下方随内容滚动。 */}
        <Card
          size="small"
          title="各轴温度与位姿（每秒自动刷新）"
          extra={
            <Button
              icon={<ReloadOutlined />}
              size="small"
              type="text"
              onClick={() => void refreshThermal()}
            />
          }
        >
          {thermal?.lockout ? (
            <Alert
              showIcon
              type="error"
              title={`温度保护锁定：${thermal.lockout.safe_posed ? '已安全归位后失能' : '归位失败直接失能'}（${thermal.lockout.message}）`}
              style={{ marginBottom: 8 }}
              action={
                <Popconfirm
                  title="解除温度保护锁定？"
                  description="仅在确认机械臂已冷却（各轴温度低于阈值）且现场安全后操作；未冷却时后端会拒绝并说明原因。"
                  okText="确认解除"
                  cancelText="再等等"
                  okButtonProps={{
                    danger: true,
                    loading: pending === 'thermal-clear',
                  }}
                  onConfirm={() => void clearLockout()}
                >
                  <Button
                    size="small"
                    danger
                    loading={pending === 'thermal-clear'}
                    disabled={actionBusy && pending !== 'thermal-clear'}
                  >
                    解除锁定
                  </Button>
                </Popconfirm>
              }
            />
          ) : null}
          <Space wrap size={8}>
            {Array.from({ length: 7 }, (_, index) => {
              const joint = index + 1;
              const value = thermal?.temperatures_c?.[String(joint)];
              const angleRad = armPose?.values_rad?.[joint - 1];
              const danger =
                value !== undefined &&
                thermal !== undefined &&
                value >= thermal.limit_c;
              const warn =
                value !== undefined &&
                thermal !== undefined &&
                value >= thermal.limit_c - 15 &&
                !danger;
              return (
                <Tag
                  key={joint}
                  color={
                    value === undefined
                      ? 'default'
                      : danger
                        ? 'error'
                        : warn
                          ? 'warning'
                          : 'success'
                  }
                  style={{ fontSize: 13, padding: '4px 10px' }}
                >
                  轴{joint}{' '}
                  {value === undefined ? '--' : `${value.toFixed(0)}°C`}
                  {angleRad === undefined
                    ? ''
                    : ` · ${(angleRad * RAD_TO_DEG).toFixed(1)}°`}
                </Tag>
              );
            })}
          </Space>
          {armPose?.tcp ? (
            <Typography.Text
              type="secondary"
              style={{ display: 'block', fontSize: 12, marginTop: 8 }}
            >
              TCP：X {armPose.tcp.x_mm.toFixed(1)} mm · Y{' '}
              {armPose.tcp.y_mm.toFixed(1)} mm · Z {armPose.tcp.z_mm.toFixed(1)}{' '}
              mm　|　Rx {armPose.tcp.rx_deg.toFixed(1)}° · Ry{' '}
              {armPose.tcp.ry_deg.toFixed(1)}° · Rz{' '}
              {armPose.tcp.rz_deg.toFixed(1)}°
            </Typography.Text>
          ) : null}
        </Card>
      </div>

      {/* 升降台不吸顶，随页面内容滚动。 */}
      <div style={{ maxWidth: 460, marginBottom: 16 }}>
        <Card
          size="small"
          title="升降台高度"
          extra={
            <Button
              icon={<ReloadOutlined />}
              size="small"
              type="text"
              onClick={() => void refreshLiftHeight()}
            />
          }
        >
          <Space wrap align="center">
            <Typography.Text>
              当前：
              <Typography.Text strong>
                {liftHeight === undefined
                  ? '--'
                  : `${liftHeight.toFixed(0)} mm`}
              </Typography.Text>
            </Typography.Text>
            <InputNumber
              addonAfter="mm"
              max={1700}
              min={10}
              onChange={(value) => setLiftTarget(Number(value))}
              precision={0}
              size="small"
              value={liftTarget}
            />
            <Select
              onChange={(value) => setLiftSpeed(value)}
              options={[
                { value: 1, label: '1 慢速' },
                { value: 2, label: '2 中速' },
                { value: 3, label: '3 快速' },
              ]}
              size="small"
              style={{ width: 90 }}
              value={liftSpeed}
            />
            <Popconfirm
              title={`移动升降台到 ${liftTarget} mm？`}
              okText="移动"
              cancelText="取消"
              onConfirm={() => void moveLiftNow()}
            >
              <Button loading={liftMoving} size="small" type="primary">
                移动
              </Button>
            </Popconfirm>
          </Space>
        </Card>
      </div>

      {!profile && !profileError ? (
        <Card>
          <Spin />{' '}
          <Typography.Text type="secondary">正在读取参数…</Typography.Text>
        </Card>
      ) : null}
      {profile ? (
        <Row gutter={[16, 16]}>
          {steps.map((step, index) => {
            const editors = step.fields
              .map((def) => fieldEditor(def))
              .filter((editor) => editor !== null);
            return (
              <Col xs={24} md={12} xxl={8} key={step.title}>
                <Card
                  size="small"
                  title={`${index + 1}. ${step.title}`}
                  style={{ height: '100%' }}
                >
                  {step.hint ? (
                    <Typography.Paragraph
                      type="secondary"
                      style={{ fontSize: 12 }}
                    >
                      {step.hint}
                    </Typography.Paragraph>
                  ) : null}
                  {editors.length ? (
                    editors
                  ) : (
                    <Typography.Paragraph
                      type="secondary"
                      style={{ fontSize: 12 }}
                    >
                      该步骤没有可调参数。
                    </Typography.Paragraph>
                  )}
                  {(() => {
                    const entry = stepPreviews[index];
                    if (!entry || (!entry.motion && !entry.reason)) return null;
                    return (
                      <div style={{ marginBottom: 8 }}>
                        <Typography.Text
                          type="secondary"
                          style={{ display: 'block', fontSize: 12 }}
                        >
                          待发送位置数据：
                        </Typography.Text>
                        {entry.motion ? (
                          <Typography.Text
                            code
                            style={{ fontSize: 12, whiteSpace: 'normal' }}
                          >
                            {previewText(entry.motion)}
                          </Typography.Text>
                        ) : (
                          <Typography.Text
                            type="secondary"
                            style={{ fontSize: 12 }}
                          >
                            暂不可预览：{entry.reason}
                          </Typography.Text>
                        )}
                      </div>
                    );
                  })()}
                  <Button
                    block
                    size="small"
                    loading={pending === `run-${index}`}
                    disabled={actionBusy && pending !== `run-${index}`}
                    onClick={() => runStep(index)}
                  >
                    执行此步
                  </Button>
                </Card>
              </Col>
            );
          })}
        </Row>
      ) : null}
      {profile ? (
        <Card
          size="small"
          title={SLOT_GROUP_TITLES[slotsGroup]}
          style={{ marginBottom: 16 }}
        >
          {slotsError && !slotsApplied ? (
            <Alert showIcon type="error" title={slotsError} />
          ) : null}
          {!slotsApplied && !slotsError ? (
            <Space>
              <Spin />
              <Typography.Text type="secondary">
                正在读取槽位标定…
              </Typography.Text>
            </Space>
          ) : null}
          {slotsApplied && slotsDraft ? (
            <>
              {armEnabled !== true ? (
                <Typography.Text
                  type="secondary"
                  style={{ display: 'block', fontSize: 12, marginBottom: 4 }}
                >
                  机械臂未使能时「执行」不可用；请先在顶部操作条使能。
                </Typography.Text>
              ) : null}
              {Object.keys(slotsDraft.slots)
                .sort((a, b) => Number(a) - Number(b))
                .map((slotId) => (
                  <div key={slotId} style={{ marginBottom: 10 }}>
                    <Typography.Text strong style={{ fontSize: 12 }}>
                      槽位 {slotId}
                    </Typography.Text>
                    {SLOT_POSE_FIELDS[slotsGroup].map(
                      ({ key, label, durationKey }) => (
                        <Space
                          key={label}
                          wrap
                          size={4}
                          style={{ display: 'flex', marginTop: 4 }}
                        >
                          <Typography.Text
                            type="secondary"
                            style={{ fontSize: 12, width: 96 }}
                          >
                            {label}
                          </Typography.Text>
                          {key
                            ? SLOT_COMPONENTS.map(
                                (component, componentIndex) => {
                                  const raw = slotsDraft.slots[slotId]?.[key];
                                  const vector = Array.isArray(raw)
                                    ? (raw as number[])
                                    : [];
                                  const stored =
                                    typeof vector[componentIndex] === 'number'
                                      ? vector[componentIndex]
                                      : 0;
                                  const display = component.angle
                                    ? stored * RAD_TO_DEG
                                    : stored * 1000;
                                  return (
                                    <InputNumber
                                      key={component.label}
                                      addonBefore={component.label}
                                      size="small"
                                      precision={1}
                                      step={component.angle ? 0.5 : 1}
                                      value={Number(display.toFixed(1))}
                                      onChange={(value) =>
                                        updateSlotPose(
                                          slotId,
                                          key,
                                          componentIndex,
                                          component.angle,
                                          Number(value ?? 0),
                                        )
                                      }
                                      style={{ width: 108 }}
                                    />
                                  );
                                },
                              )
                            : null}
                          <InputNumber
                            addonBefore="时长"
                            addonAfter="s"
                            size="small"
                            min={0.5}
                            precision={1}
                            step={0.5}
                            value={
                              typeof slotsDraft.slots[slotId]?.[durationKey] ===
                              'number'
                                ? (slotsDraft.slots[slotId][
                                    durationKey
                                  ] as number)
                                : 0
                            }
                            onChange={(value) =>
                              updateSlotDuration(
                                slotId,
                                durationKey,
                                Number(value ?? 0),
                              )
                            }
                            style={{ width: 130 }}
                          />
                          {key ? (
                            <Button
                              size="small"
                              icon={<CaretRightOutlined />}
                              loading={pending === `slot-exec-${slotId}-${key}`}
                              disabled={
                                armEnabled !== true ||
                                (actionBusy &&
                                  pending !== `slot-exec-${slotId}-${key}`)
                              }
                              onClick={() =>
                                void executeSlotPose(slotId, key, label)
                              }
                            >
                              执行
                            </Button>
                          ) : null}
                        </Space>
                      ),
                    )}
                  </div>
                ))}
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                位姿单位 mm/deg（存储为 m/rad），编辑自动保存并实时生效。
              </Typography.Text>
            </>
          ) : null}
        </Card>
      ) : null}
      {profileError && !profile ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="参数加载失败，请确认服务已启动后刷新页面。"
        />
      ) : null}

      <Modal
        title="准备：识别目标"
        open={prepareOpen}
        okText="开始识别"
        cancelText="取消"
        confirmLoading={preparing}
        onOk={() => void prepare()}
        onCancel={() => setPrepareOpen(false)}
        destroyOnHidden={false}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          选择书立格口后由视觉识别抓取/放置目标；不选择书籍时使用服务默认模板或实时拍摄。
        </Typography.Paragraph>
        <div style={{ marginBottom: 12 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            书立格口
          </Typography.Text>
          <Select
            value={prepareSlot}
            style={{ width: '100%' }}
            onChange={(value) => setPrepareSlot(value)}
            options={slotValues.map((value) => ({
              value,
              label: `${value} 号格口`,
            }))}
          />
        </div>
        <div>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            参照书籍（书籍管理，可选）
          </Typography.Text>
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            loading={booksLoading}
            placeholder="不选择书籍"
            style={{ width: '100%' }}
            value={prepareBookId}
            onChange={(value) => setPrepareBookId(value)}
            options={books.map((book) => ({
              value: book.id,
              label: `${book.name}（高度 ${book.heightMm} mm）`,
            }))}
          />
        </div>
      </Modal>
    </PageContainer>
  );
}
