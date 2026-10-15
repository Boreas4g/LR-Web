import { DeleteOutlined, PlusOutlined, SaveOutlined } from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd';
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';

import {
  cancelAnnotationNavigation,
  cancelCompleteReturn,
  cancelCompleteTakeout,
  createCompleteReturn,
  createCompleteTakeout,
  getMapAnnotations,
  getMapVersions,
  getRobotRealtimeState,
  getRobots,
  listBooks,
  navigateRobotToAnnotation,
  pollTasks,
  sendManualJog,
  subscribeRobotRealtime,
} from '@/services/libraryRobot';
import {
  createStressTestTemplate,
  deleteStressTestTemplate,
  listStressTestTemplates,
  type StressTestTemplate,
  type StressTestTemplateConfig,
  updateStressTestTemplate,
  validateStressTestActions,
} from '@/services/libraryRobot/stressTestTemplates';
import {
  disableArm,
  getHardwareStatus,
  type HardwareStatus,
  moveLift,
} from '@/services/libraryRobot/studio';
import type {
  Book,
  DispatchTask,
  MapAnnotation,
  MapVersion,
  RobotSummary,
} from '@/services/libraryRobot/types';
import { errorMessage } from '@/utils/errorText';

const TERMINAL_STATES = [
  'completed',
  'failed',
  'cancelled',
  'recovery_required',
];

/** Calibrated onboard book-stand slots (see the bookarm slot calibration). */
const BOOK_STAND_SLOTS = [1, 2, 3, 4, 5];

type Phase =
  | 'idle'
  | 'navigate-out'
  | 'takeout'
  | 'return'
  | 'park'
  | 'back-off';

const PHASE_META: Record<Phase, { text: string; color: string }> = {
  idle: { text: '空闲', color: 'default' },
  'navigate-out': { text: '导航到目标点', color: 'blue' },
  takeout: { text: '快速取书', color: 'gold' },
  return: { text: '中位还书（还到目标书左边）', color: 'gold' },
  park: { text: '收拢（失能+降台）', color: 'cyan' },
  'back-off': { text: '直退 1.5 米（不走导航）', color: 'geekblue' },
};

/** 收拢后升降台停放高度（mm）：还书结束、直退前降到该高度。 */
const PARK_LIFT_HEIGHT_MM = 10;

/** 每轮收尾的直退距离（m）：不走导航，开环点动直退，给下一轮导航留出余量。 */
const BACK_OFF_DISTANCE_M = 1.5;
/** 直退线速度（m/s，负值后退）；服务端手动点动安全上限为 0.3 m/s。 */
const BACK_OFF_LINEAR_SPEED = -0.2;
/** 直退点动租约与续发间隔：租约内持续后退，停发后车端看门狗约 1 s 内刹停。 */
const BACK_OFF_JOG_DURATION_MS = 700;
const BACK_OFF_JOG_INTERVAL_MS = 300;
/** 位姿停滞上限：开环直退时位姿超过该时长未更新即失败停车，避免盲开。 */
const BACK_OFF_POSE_STALE_MS = 3_000;
/** 直退总超时：按里程迟迟不到位（如被挡）判本轮失败。 */
const BACK_OFF_TIMEOUT_MS = 30_000;
/** 交还导航前的租约沉降：车端手动控制租约在最后一帧点动被接受后最多存活约
 * 1.65 s（命令过期 250 ms + 租约 ≤700 ms + 余量）；不等够就导航会被车端
 * manual control 联锁拒绝。 */
const BACK_OFF_LEASE_SETTLE_MS = 2_000;

/** 一键运行的默认压测任务（地图/点位/书籍按注册信息解析；速度档 3）。
 * 每轮 2 取 2 还：取「杜拉拉」→2 号槽、取「风洞」→1 号槽、还 1 号槽（还到
 * NET 左边）、还 2 号槽（还到风洞左边）。还 2 号槽以风洞为定位目标，依赖
 * 还 1 号槽先把风洞还回架上，顺序不可调换。 */
const DEFAULT_TASK = {
  mapId: 'MAP-BB36BE4791F2',
  mapName: 'aoxiao_zoulang',
  targetLabel: '书架点 4',
  takeouts: [
    { bookName: '杜拉拉', destinationSlot: 2 },
    { bookName: '风洞', destinationSlot: 1 },
  ],
  returns: [
    { sourceSlot: 1, referenceBookName: 'NET' },
    { sourceSlot: 2, referenceBookName: '风洞' },
  ],
  liftSpeedLevel: 3,
} as const;

const ROBOT_STATUS_TEXT: Record<RobotSummary['status'], string> = {
  idle: '空闲',
  navigating: '导航中',
  charging: '充电中',
  manual: '手动控制',
  fault: '故障',
  offline: '离线',
  busy: '机械臂作业中',
  mapping: '建图中',
};

const LOCALIZATION_TEXT: Record<string, string> = {
  starting: '启动中',
  restoring_saved_pose: '恢复位姿中',
  global_localizing: '全局定位中',
  manual_initializing: '手动初始化中',
  localized: '已定位',
  manual_required: '需手动定位',
  unknown: '未知',
};

const HEALTH_ITEMS: { key: keyof RobotSummary['health']; label: string }[] = [
  { key: 'navigation', label: '导航' },
  { key: 'chassis', label: '底盘' },
  { key: 'arm', label: '机械臂' },
  { key: 'rod', label: '推杆' },
  { key: 'imu', label: 'IMU' },
  { key: 'lift', label: '升降台' },
  { key: 'camera', label: '相机' },
];

type RoundLog = {
  round: number;
  result: 'success' | 'failed' | 'stopped';
  durationS: number;
  message: string;
  at: string;
};

/** 一次取书动作：把书取到书立的指定槽位。 */
type TakeoutAction = {
  book: Book;
  destinationSlot: number;
  liftHeightMm: number;
};

/** 一次中位还书动作：从书立槽位取回该书，识别目标书后还到其左边。 */
type ReturnAction = {
  sourceSlot: number;
  referenceBook: Book;
};

/** 取书动作表单行：书未选时只带槽位，校验在 buildConfig 完成。 */
type TakeoutRow = {
  bookId?: string;
  destinationSlot: number;
};

/** 还书动作表单行。 */
type ReturnRow = {
  sourceSlot: number;
  referenceBookId?: string;
};

type LoopConfig = {
  robotId: string;
  mapId: string;
  targetAnnotationId: string;
  takeouts: TakeoutAction[];
  returns: ReturnAction[];
  liftSpeedLevel: number;
  maxRounds: number | null;
  stopOnFailure: boolean;
};

function fmt(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

function fmtDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = String(Math.floor(total / 3600)).padStart(2, '0');
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
  const s = String(total % 60).padStart(2, '0');
  return `${h}:${m}:${s}`;
}

/** Poll a dispatch task until it reaches a terminal state; resolves on
 * completed, rejects on failed/cancelled/recovery_required or when `halted`
 * (page closed). */
function waitTaskTerminal(
  taskId: string,
  onUpdate: (task: DispatchTask) => void,
  halted: () => boolean,
): Promise<DispatchTask> {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      if (halted()) {
        reject(new Error('页面已关闭'));
        return;
      }
      try {
        const tasks = await pollTasks();
        const task = tasks.find((item) => item.id === taskId);
        if (task) {
          onUpdate(task);
          if (TERMINAL_STATES.includes(task.status)) {
            if (task.status === 'completed') {
              resolve(task);
            } else {
              reject(
                new Error(
                  task.failureReason ||
                    (task.status === 'cancelled'
                      ? '任务已取消'
                      : task.status === 'recovery_required'
                        ? '机械臂需人工确认恢复（可能夹着书停在半路）'
                        : '任务失败'),
                ),
              );
            }
            return;
          }
        }
      } catch {
        // 轮询失败保持重试。
      }
      // 500 ms 一拍：动作结束到页面感知最多差半秒，再大就拖慢循环节奏。
      window.setTimeout(() => void tick(), 500);
    };
    void tick();
  });
}

function sleepMs(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

/** 读取机器人最新位姿：优先实时通道（WebSocket），缺失时回退 REST。
 * 后端内存摘要每帧遥测都会刷新（WS 推送有节流），REST 反而更可靠。 */
async function readRobotPose(robotId: string) {
  const realtimePose = getRobotRealtimeState().robots.find(
    (item) => item.id === robotId,
  )?.pose;
  if (realtimePose) return realtimePose;
  try {
    const robots = await getRobots();
    return robots.find((item) => item.id === robotId)?.pose;
  } catch {
    return undefined;
  }
}

/** 直退收尾：不走导航，按固定线速度持续点动后退，以实时位姿累计里程，
 * 够 BACK_OFF_DISTANCE_M 即结束；位姿停滞/总超时/手动停止判失败或中止。 */
async function backOffStraight(
  robotId: string,
  halted: () => boolean,
): Promise<void> {
  // 起步位姿：实时通道/REST 任一读到即可，最多等 5 s（WS 抖动或单帧缺位姿时
  // 不立即判败）。
  const startedAt = Date.now();
  let startPose: Awaited<ReturnType<typeof readRobotPose>>;
  while (!startPose) {
    if (halted()) throw new Error('已手动停止');
    if (Date.now() - startedAt > 5_000) {
      throw new Error('直退失败：超过 5 s 读不到机器人位姿，请确认遥测正常');
    }
    startPose = await readRobotPose(robotId);
    if (!startPose) await sleepMs(BACK_OFF_JOG_INTERVAL_MS);
  }
  let travelledM = 0;
  // 以 capturedAt 变化判断位姿通道是否活着（比绝对时间更稳，不受时钟偏差影响）。
  let lastPoseStamp = startPose.capturedAt;
  let lastPoseChangeAt = Date.now();
  /** 最后一帧运动点动的发送时刻：决定手动控制租约的最晚存活点。 */
  let lastJogAt = 0;
  try {
    while (travelledM < BACK_OFF_DISTANCE_M) {
      if (halted()) throw new Error('已手动停止');
      if (Date.now() - startedAt > BACK_OFF_TIMEOUT_MS) {
        throw new Error(
          `直退超时：${Math.round(BACK_OFF_TIMEOUT_MS / 1000)} s 内仅移动 ${travelledM.toFixed(2)} m`,
        );
      }
      lastJogAt = Date.now();
      await sendManualJog(robotId, {
        linear: BACK_OFF_LINEAR_SPEED,
        angular: 0,
        durationMs: BACK_OFF_JOG_DURATION_MS,
      });
      await sleepMs(BACK_OFF_JOG_INTERVAL_MS);
      const pose = await readRobotPose(robotId);
      if (pose && pose.capturedAt !== lastPoseStamp) {
        lastPoseStamp = pose.capturedAt;
        lastPoseChangeAt = Date.now();
        travelledM = Math.hypot(pose.x - startPose.x, pose.y - startPose.y);
      } else if (Date.now() - lastPoseChangeAt > BACK_OFF_POSE_STALE_MS) {
        throw new Error('直退中止：位姿超过 3 s 未更新，为避免盲开已停车');
      }
    }
  } finally {
    // 零速度帧立即停车；即便丢失，车端看门狗也会在点动租约内刹停。
    await sendManualJog(robotId, {
      linear: 0,
      angular: 0,
      durationMs: 100,
    }).catch(() => undefined);
    // 等手动控制租约释放再交还导航；手动停止时立即返回（没有下一轮）。
    if (!halted() && lastJogAt > 0) {
      const settleLeft = BACK_OFF_LEASE_SETTLE_MS - (Date.now() - lastJogAt);
      if (settleLeft > 0) await sleepMs(settleLeft);
    }
  }
}

export default function StressTestPage() {
  const { message } = App.useApp();
  const [loadingOptions, setLoadingOptions] = useState(true);
  const [books, setBooks] = useState<Book[]>([]);
  const [maps, setMaps] = useState<MapVersion[]>([]);
  const [annotations, setAnnotations] = useState<MapAnnotation[]>([]);
  const [robots, setRobots] = useState<RobotSummary[]>([]);

  const [robotId, setRobotId] = useState<string>();
  const [mapId, setMapId] = useState<string>();
  const [targetAnnotationId, setTargetAnnotationId] = useState<string>();
  const [takeoutRows, setTakeoutRows] = useState<TakeoutRow[]>([
    { destinationSlot: 1 },
  ]);
  const [returnRows, setReturnRows] = useState<ReturnRow[]>([
    { sourceSlot: 1 },
  ]);
  const [templates, setTemplates] = useState<StressTestTemplate[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState<string>();
  const [saveModalOpen, setSaveModalOpen] = useState(false);
  const [saveTemplateName, setSaveTemplateName] = useState('');
  const [templateSaving, setTemplateSaving] = useState(false);
  const [liftSpeedLevel, setLiftSpeedLevel] = useState<number>(3);
  const [maxRounds, setMaxRounds] = useState<number | null>(null);
  const [stopOnFailure, setStopOnFailure] = useState(true);

  const [running, setRunning] = useState(false);
  const [phase, setPhase] = useState<Phase>('idle');
  /** 当前动作的简短说明（哪本书/哪个槽位），随阶段标签展示。 */
  const [phaseDetail, setPhaseDetail] = useState('');
  const [currentTask, setCurrentTask] = useState<DispatchTask>();
  const [round, setRound] = useState(0);
  const [successCount, setSuccessCount] = useState(0);
  const [failCount, setFailCount] = useState(0);
  const [logs, setLogs] = useState<RoundLog[]>([]);
  const [startedAt, setStartedAt] = useState<number>();
  const [finishedAt, setFinishedAt] = useState<number>();
  const [tick, setTick] = useState(Date.now());
  const [hardware, setHardware] = useState<HardwareStatus | null>(null);

  const stopRef = useRef(false);
  const unmountedRef = useRef(false);
  /** 应用模板/默认任务时跳过地图切换副作用的清空动作（点位由调用方直接回填）。 */
  const suppressMapResetRef = useRef(false);
  const currentTaskRef = useRef<{
    phase: Phase;
    taskId: string;
    robotId: string;
  } | null>(null);

  const realtimeState = useSyncExternalStore(
    subscribeRobotRealtime,
    getRobotRealtimeState,
    getRobotRealtimeState,
  );

  const robot =
    realtimeState.robots.find((item) => item.id === robotId) ??
    robots.find((item) => item.id === robotId);

  // 初始加载书籍/机器人/地图选项。
  useEffect(() => {
    (async () => {
      try {
        const [bookList, robotList, mapList] = await Promise.all([
          listBooks(),
          getRobots(),
          getMapVersions(),
        ]);
        setBooks(bookList);
        setMaps(mapList);
        setRobots(robotList);
        const first = robotList[0];
        if (first) setRobotId(first.id);
        const chosenMap =
          mapList.find((item) => item.id === first?.mapId) ?? mapList[0];
        if (chosenMap) setMapId(chosenMap.id);
      } catch (error) {
        message.error(errorMessage(error, '无法加载压测选项。'));
      } finally {
        setLoadingOptions(false);
      }
    })();
  }, [message]);

  // 模板列表单独加载：模板接口异常不影响压测主选项。
  useEffect(() => {
    (async () => {
      try {
        setTemplates(await listStressTestTemplates());
      } catch (error) {
        message.error(errorMessage(error, '无法加载压测模板列表。'));
      }
    })();
  }, [message]);

  // 地图切换后重载点位并清空已选点（应用模板/默认任务时跳过，由调用方回填）。
  useEffect(() => {
    if (!mapId) return;
    if (suppressMapResetRef.current) {
      suppressMapResetRef.current = false;
      return;
    }
    setTargetAnnotationId(undefined);
    (async () => {
      try {
        setAnnotations(await getMapAnnotations(mapId));
      } catch (error) {
        message.error(errorMessage(error, '无法加载地图点位。'));
      }
    })();
  }, [mapId, message]);

  // 机器人列表轮询（实时 WebSocket 断开时兜底）。
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const list = await getRobots();
        if (!cancelled) setRobots(list);
      } catch {
        // 轮询失败保持旧状态。
      }
    };
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const refreshHardware = useCallback(async () => {
    try {
      setHardware(await getHardwareStatus());
    } catch {
      setHardware(null);
    }
  }, []);

  // 机械臂/升降台等硬件状态轮询。
  useEffect(() => {
    void refreshHardware();
    const timer = window.setInterval(() => void refreshHardware(), 3000);
    return () => window.clearInterval(timer);
  }, [refreshHardware]);

  // 已运行时长的秒级刷新。
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setTick(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  // 页面关闭时停止循环（当前动作仍会走完或被取消，见顶部告警）。
  useEffect(
    () => () => {
      unmountedRef.current = true;
      stopRef.current = true;
    },
    [],
  );

  function pushLog(entry: RoundLog) {
    setLogs((prev) => [entry, ...prev].slice(0, 50));
  }

  /** 把取书/还书表单行解析成动作序列；返回字符串表示校验失败信息。 */
  function resolveActions():
    | { takeouts: TakeoutAction[]; returns: ReturnAction[] }
    | string {
    const shapeError = validateStressTestActions(takeoutRows, returnRows);
    if (shapeError) return shapeError;
    const takeouts: TakeoutAction[] = [];
    for (const [index, row] of takeoutRows.entries()) {
      const book = books.find((item) => item.id === row.bookId);
      if (!book) return `取书动作 ${index + 1}：请选择要取的书`;
      // 升降杆高度跟着书籍登记信息走。
      takeouts.push({
        book,
        destinationSlot: row.destinationSlot,
        liftHeightMm: book.heightMm,
      });
    }
    const bookBySlot = new Map(
      takeouts.map((takeout) => [takeout.destinationSlot, takeout.book]),
    );
    const returns: ReturnAction[] = [];
    for (const [index, row] of returnRows.entries()) {
      const referenceBook = books.find(
        (item) => item.id === row.referenceBookId,
      );
      if (!referenceBook) return `还书动作 ${index + 1}：请选择还书目标书`;
      const takenBook = bookBySlot.get(row.sourceSlot);
      if (takenBook && takenBook.id === referenceBook.id)
        return `还书动作 ${index + 1}：目标书与 ${row.sourceSlot} 号槽取入的书相同，无法作为定位目标`;
      returns.push({ sourceSlot: row.sourceSlot, referenceBook });
    }
    return { takeouts, returns };
  }

  function buildConfig(): LoopConfig | string {
    if (!robotId) return '请选择机器人';
    if (!mapId) return '请选择地图';
    if (!targetAnnotationId) return '请选择目标点（书架点）';
    const actions = resolveActions();
    if (typeof actions === 'string') return actions;
    if (robot && robot.status !== 'idle')
      return `机器人当前状态为「${ROBOT_STATUS_TEXT[robot.status]}」，需空闲才能开始压测`;
    if (robot && robot.localizationStatus !== 'localized')
      return '机器人尚未完成定位，请先在「地图建模」页完成定位';
    return {
      robotId,
      mapId,
      targetAnnotationId,
      takeouts: actions.takeouts,
      returns: actions.returns,
      liftSpeedLevel,
      maxRounds,
      stopOnFailure,
    };
  }

  /** 当前表单的任务定义（存模板用；不校验机器人状态，只存书籍/点位 id）。 */
  function buildTemplateConfig(): StressTestTemplateConfig | string {
    if (!mapId) return '请选择地图';
    if (!targetAnnotationId) return '请选择目标点（书架点）';
    const actions = resolveActions();
    if (typeof actions === 'string') return actions;
    return {
      mapId,
      targetAnnotationId,
      takeouts: actions.takeouts.map((takeout) => ({
        bookId: takeout.book.id,
        destinationSlot: takeout.destinationSlot,
      })),
      returns: actions.returns.map((action) => ({
        sourceSlot: action.sourceSlot,
        referenceBookId: action.referenceBook.id,
      })),
      liftSpeedLevel,
      maxRounds,
      stopOnFailure,
    };
  }

  function updateTakeoutRow(index: number, patch: Partial<TakeoutRow>) {
    setTakeoutRows((prev) =>
      prev.map((row, rowIndex) =>
        rowIndex === index ? { ...row, ...patch } : row,
      ),
    );
  }

  function addTakeoutRow() {
    setTakeoutRows((prev) => {
      const used = new Set(prev.map((row) => row.destinationSlot));
      const free = BOOK_STAND_SLOTS.find((slot) => !used.has(slot)) ?? 1;
      return [...prev, { destinationSlot: free }];
    });
  }

  function removeTakeoutRow(index: number) {
    setTakeoutRows((prev) => prev.filter((_, rowIndex) => rowIndex !== index));
  }

  function updateReturnRow(index: number, patch: Partial<ReturnRow>) {
    setReturnRows((prev) =>
      prev.map((row, rowIndex) =>
        rowIndex === index ? { ...row, ...patch } : row,
      ),
    );
  }

  function addReturnRow() {
    setReturnRows((prev) => {
      const used = new Set(prev.map((row) => row.sourceSlot));
      const free = takeoutRows.find((row) => !used.has(row.destinationSlot));
      return [...prev, { sourceSlot: free?.destinationSlot ?? 1 }];
    });
  }

  function removeReturnRow(index: number) {
    setReturnRows((prev) => prev.filter((_, rowIndex) => rowIndex !== index));
  }

  /** 应用模板：按当前库解析地图/点位/书籍 id 并回填表单；任一引用失效即报错不动表单。 */
  async function applyTemplate(template: StressTestTemplate) {
    const config = template.config;
    const map = maps.find((item) => item.id === config.mapId);
    if (!map) {
      message.error(`模板「${template.name}」：地图 ${config.mapId} 已不存在`);
      return;
    }
    let annotationList = annotations;
    if (map.id !== mapId) {
      try {
        annotationList = await getMapAnnotations(map.id);
      } catch (error) {
        message.error(errorMessage(error, '应用模板：无法加载地图点位'));
        return;
      }
    }
    const missing: string[] = [];
    const target = annotationList.find(
      (item) => item.id === config.targetAnnotationId,
    );
    if (!target) missing.push('目标点');
    const takeoutRowsNext: TakeoutRow[] = [];
    for (const [index, action] of config.takeouts.entries()) {
      if (books.some((item) => item.id === action.bookId)) {
        takeoutRowsNext.push({
          bookId: action.bookId,
          destinationSlot: action.destinationSlot,
        });
      } else {
        missing.push(`取书动作 ${index + 1} 的书`);
      }
    }
    const returnRowsNext: ReturnRow[] = [];
    for (const [index, action] of config.returns.entries()) {
      if (books.some((item) => item.id === action.referenceBookId)) {
        returnRowsNext.push({
          sourceSlot: action.sourceSlot,
          referenceBookId: action.referenceBookId,
        });
      } else {
        missing.push(`还书动作 ${index + 1} 的目标书`);
      }
    }
    if (missing.length > 0) {
      message.error(
        `模板「${template.name}」引用的${missing.join('、')}已不存在`,
      );
      return;
    }
    suppressMapResetRef.current = true;
    setMapId(map.id);
    setAnnotations(annotationList);
    setTargetAnnotationId(config.targetAnnotationId);
    setTakeoutRows(takeoutRowsNext);
    setReturnRows(returnRowsNext);
    setLiftSpeedLevel(config.liftSpeedLevel);
    setMaxRounds(config.maxRounds);
    setStopOnFailure(config.stopOnFailure);
    setSelectedTemplateId(template.id);
    message.success(`已应用模板「${template.name}」，确认无误后点开始压测`);
  }

  function openSaveModal() {
    const config = buildTemplateConfig();
    if (typeof config === 'string') {
      message.error(config);
      return;
    }
    const selected = templates.find((item) => item.id === selectedTemplateId);
    setSaveTemplateName(selected?.name ?? '');
    setSaveModalOpen(true);
  }

  async function saveTemplate() {
    const name = saveTemplateName.trim();
    if (!name) {
      message.error('请填写模板名称');
      return;
    }
    const config = buildTemplateConfig();
    if (typeof config === 'string') {
      message.error(config);
      return;
    }
    // 同名模板直接覆盖（模态框内已提示）。
    const existing = templates.find((item) => item.name === name);
    setTemplateSaving(true);
    try {
      const saved = existing
        ? await updateStressTestTemplate(existing.id, { name, config })
        : await createStressTestTemplate({ name, config });
      setTemplates(await listStressTestTemplates());
      setSelectedTemplateId(saved.id);
      setSaveModalOpen(false);
      message.success(`模板「${name}」已保存`);
    } catch (error) {
      message.error(errorMessage(error, '保存模板失败'));
    } finally {
      setTemplateSaving(false);
    }
  }

  async function removeTemplate() {
    const selected = templates.find((item) => item.id === selectedTemplateId);
    if (!selected) return;
    try {
      await deleteStressTestTemplate(selected.id);
      setTemplates(await listStressTestTemplates());
      setSelectedTemplateId(undefined);
      message.success(`模板「${selected.name}」已删除`);
    } catch (error) {
      message.error(errorMessage(error, '删除模板失败'));
    }
  }

  async function runPhase(
    phase: Exclude<Phase, 'idle'>,
    label: string,
    create: () => Promise<DispatchTask>,
  ): Promise<DispatchTask> {
    if (stopRef.current || unmountedRef.current) throw new Error('已手动停止');
    setPhase(phase);
    setPhaseDetail(label);
    setCurrentTask(undefined);
    const task = await create();
    currentTaskRef.current = { phase, taskId: task.id, robotId: task.robotId };
    setCurrentTask(task);
    try {
      return await waitTaskTerminal(
        task.id,
        setCurrentTask,
        () => unmountedRef.current,
      );
    } finally {
      currentTaskRef.current = null;
    }
  }

  async function runLoop(config: LoopConfig) {
    stopRef.current = false;
    setRunning(true);
    setPhase('idle');
    setPhaseDetail('');
    setCurrentTask(undefined);
    setRound(0);
    setSuccessCount(0);
    setFailCount(0);
    setLogs([]);
    const started = Date.now();
    setStartedAt(started);
    setFinishedAt(undefined);
    setTick(started);
    let roundNo = 0;
    try {
      while (!stopRef.current && !unmountedRef.current) {
        if (config.maxRounds !== null && roundNo >= config.maxRounds) break;
        roundNo += 1;
        setRound(roundNo);
        const roundStart = Date.now();
        /** 当前动作说明：失败时写进轮次日志，运行中随阶段标签展示。 */
        let actionLabel = '';
        try {
          actionLabel = '导航到目标点';
          await runPhase('navigate-out', actionLabel, () =>
            navigateRobotToAnnotation(config.robotId, {
              mapId: config.mapId,
              annotationId: config.targetAnnotationId,
            }),
          );
          // 槽位→该槽位里的书：还书时据此回填 bookId。
          const bookBySlot = new Map<number, Book>();
          const actionNotes: string[] = [];
          for (const takeout of config.takeouts) {
            actionLabel = `取「${takeout.book.name}」→ ${takeout.destinationSlot} 号槽`;
            await runPhase('takeout', actionLabel, () =>
              createCompleteTakeout({
                robotId: config.robotId,
                templateId: takeout.book.templateId,
                bookId: takeout.book.id,
                liftHeightMm: takeout.liftHeightMm,
                destinationSlot: takeout.destinationSlot,
                liftSpeedLevel: config.liftSpeedLevel,
                mapId: config.mapId,
                annotationId: config.targetAnnotationId,
                sendNavigation: false,
              }),
            );
            bookBySlot.set(takeout.destinationSlot, takeout.book);
            actionNotes.push(actionLabel);
          }
          // 中位还书：识别目标书，抓出左邻书晃缝后还到目标书左边。
          for (const returnAction of config.returns) {
            // 手动单取单还允许两槽位不同：查不到时退回第一个取书动作的书，
            // 与旧的单取单还行为一致（默认任务的槽位均能查到）。
            const takenBook =
              bookBySlot.get(returnAction.sourceSlot) ??
              config.takeouts[0]?.book;
            actionLabel = `还 ${returnAction.sourceSlot} 号槽（目标「${returnAction.referenceBook.name}」左边）`;
            await runPhase('return', actionLabel, () =>
              createCompleteReturn({
                robotId: config.robotId,
                templateId: returnAction.referenceBook.templateId,
                bookId: takenBook?.id,
                liftHeightMm: returnAction.referenceBook.heightMm,
                sourceSlot: returnAction.sourceSlot,
                liftSpeedLevel: config.liftSpeedLevel,
                mapId: config.mapId,
                sendNavigation: false,
                returnMode: 'mid',
              }),
            );
            actionNotes.push(actionLabel);
          }
          // 收拢：全部还书完成后，先失能机械臂、把升降台降到停放高度，再动车直退。
          actionLabel = '收拢（失能+降台）';
          setPhase('park');
          setPhaseDetail(actionLabel);
          setCurrentTask(undefined);
          try {
            await disableArm();
            await moveLift(PARK_LIFT_HEIGHT_MM, config.liftSpeedLevel);
          } catch (error) {
            throw new Error(errorMessage(error, '机械臂失能或升降台降下失败'));
          }
          // 收尾：不走导航，开环直退固定距离即结束本轮；下一轮直接导航到书架点。
          actionLabel = `直退 ${BACK_OFF_DISTANCE_M} 米（不走导航）`;
          setPhase('back-off');
          setPhaseDetail(actionLabel);
          setCurrentTask(undefined);
          await backOffStraight(
            config.robotId,
            () => stopRef.current || unmountedRef.current,
          );
          setSuccessCount((count) => count + 1);
          pushLog({
            round: roundNo,
            result: 'success',
            durationS: (Date.now() - roundStart) / 1000,
            message: actionNotes.join('；'),
            at: new Date().toLocaleTimeString(),
          });
        } catch (error) {
          const stopped = stopRef.current || unmountedRef.current;
          pushLog({
            round: roundNo,
            result: stopped ? 'stopped' : 'failed',
            durationS: (Date.now() - roundStart) / 1000,
            message: stopped
              ? '手动停止'
              : `${actionLabel ? `${actionLabel}：` : ''}${errorMessage(error, '本轮执行失败')}`,
            at: new Date().toLocaleTimeString(),
          });
          if (stopped) break;
          setFailCount((count) => count + 1);
          if (config.stopOnFailure) {
            message.error(
              errorMessage(error, `第 ${roundNo} 轮失败，压测已停止`),
            );
            break;
          }
        }
      }
    } finally {
      currentTaskRef.current = null;
      setCurrentTask(undefined);
      setPhase('idle');
      setPhaseDetail('');
      setRunning(false);
      setFinishedAt(Date.now());
    }
  }

  /** 一键运行默认任务：按 DEFAULT_TASK 解析地图/点位/书籍后开跑，
   * 同步把解析结果回填到表单。 */
  async function applyDefaultTask() {
    const map =
      maps.find((item) => item.id === DEFAULT_TASK.mapId) ??
      maps.find((item) => item.name === DEFAULT_TASK.mapName);
    if (!map) {
      message.error(
        `默认任务：地图 ${DEFAULT_TASK.mapName}（${DEFAULT_TASK.mapId}）不存在`,
      );
      return;
    }
    let annotations: MapAnnotation[];
    try {
      annotations = await getMapAnnotations(map.id);
    } catch (error) {
      message.error(errorMessage(error, '默认任务：无法加载地图点位'));
      return;
    }
    const target = annotations.find(
      (item) => item.label === DEFAULT_TASK.targetLabel,
    );
    if (!target) {
      message.error(`默认任务：目标点「${DEFAULT_TASK.targetLabel}」不存在`);
      return;
    }
    // 逐本解析 2 取 2 还涉及的书籍；缺哪本报哪本。
    const resolveBook = (name: string) =>
      books.find((item) => item.name.includes(name));
    const takeouts: TakeoutAction[] = [];
    for (const entry of DEFAULT_TASK.takeouts) {
      const book = resolveBook(entry.bookName);
      if (!book) {
        message.error(`默认任务：书籍「${entry.bookName}」不存在`);
        return;
      }
      takeouts.push({
        book,
        destinationSlot: entry.destinationSlot,
        liftHeightMm: book.heightMm,
      });
    }
    const bookBySlot = new Map(
      takeouts.map((takeout) => [takeout.destinationSlot, takeout.book]),
    );
    const returns: ReturnAction[] = [];
    for (const entry of DEFAULT_TASK.returns) {
      const referenceBook = resolveBook(entry.referenceBookName);
      if (!referenceBook) {
        message.error(
          `默认任务：还书目标书「${entry.referenceBookName}」不存在`,
        );
        return;
      }
      const takenBook = bookBySlot.get(entry.sourceSlot);
      if (!takenBook) {
        message.error(
          `默认任务：还 ${entry.sourceSlot} 号槽没有对应的取书动作`,
        );
        return;
      }
      if (takenBook.id === referenceBook.id) {
        message.error(
          `默认任务：还 ${entry.sourceSlot} 号槽的目标书「${entry.referenceBookName}」与该槽的书相同，无法作为定位目标`,
        );
        return;
      }
      returns.push({ sourceSlot: entry.sourceSlot, referenceBook });
    }
    const selectedRobotId = robotId ?? robots[0]?.id;
    if (!selectedRobotId) {
      message.error('默认任务：没有可用机器人');
      return;
    }
    if (robot && robot.status !== 'idle') {
      message.error(
        `机器人当前状态为「${ROBOT_STATUS_TEXT[robot.status]}」，需空闲才能开始压测`,
      );
      return;
    }
    if (robot && robot.localizationStatus !== 'localized') {
      message.error('机器人尚未完成定位，请先在「地图建模」页完成定位');
      return;
    }
    suppressMapResetRef.current = true;
    setMapId(map.id);
    setAnnotations(annotations);
    setTargetAnnotationId(target.id);
    // 默认任务的两取两还整体回填到表单（与一键运行的配置一致）。
    setTakeoutRows(
      takeouts.map((takeout) => ({
        bookId: takeout.book.id,
        destinationSlot: takeout.destinationSlot,
      })),
    );
    setReturnRows(
      returns.map((action) => ({
        sourceSlot: action.sourceSlot,
        referenceBookId: action.referenceBook.id,
      })),
    );
    setLiftSpeedLevel(DEFAULT_TASK.liftSpeedLevel);
    setMaxRounds(null);
    setStopOnFailure(true);
    void runLoop({
      robotId: selectedRobotId,
      mapId: map.id,
      targetAnnotationId: target.id,
      takeouts,
      returns,
      liftSpeedLevel: DEFAULT_TASK.liftSpeedLevel,
      maxRounds: null,
      stopOnFailure: true,
    });
  }

  function start() {
    const config = buildConfig();
    if (typeof config === 'string') {
      message.error(config);
      return;
    }
    void runLoop(config);
  }

  function stop() {
    stopRef.current = true;
    const current = currentTaskRef.current;
    if (current) {
      const request =
        current.phase === 'takeout'
          ? cancelCompleteTakeout(current.taskId)
          : current.phase === 'return'
            ? cancelCompleteReturn(current.taskId)
            : cancelAnnotationNavigation(current.robotId, current.taskId);
      request.catch(() => undefined);
    }
    message.info('已请求停止：当前动作取消后循环结束。');
  }

  const phaseMeta = PHASE_META[phase];
  const elapsedMs =
    startedAt === undefined ? 0 : (finishedAt ?? tick) - startedAt;
  const thermal = hardware?.arm.thermal;
  const temperatureEntries = Object.entries(thermal?.temperatures_c ?? {}).sort(
    (a, b) => a[0].localeCompare(b[0], undefined, { numeric: true }),
  );
  const pose = robot?.pose;
  const bookOptions = books.map((book) => ({
    value: book.id,
    label: `${book.name}（高度 ${book.heightMm} mm）`,
  }));
  const annotationOptions = annotations.map((item) => ({
    value: item.id,
    label: `${item.label}（${item.kind}）`,
  }));
  const slotOptions = BOOK_STAND_SLOTS.map((slot) => ({
    value: slot,
    label: `槽位 ${slot}`,
  }));

  return (
    <PageContainer
      title="压力测试"
      content="单循环执行「导航到目标点 → 按序取书入槽 → 按序中位还书（还到目标书左边）→ 收拢（失能机械臂+降升降台）→ 直退 1.5 米（不走导航）」，直退结束即完成一轮，下一轮直接导航到书架点。用于长时间运行稳定性验证。循环由本页面驱动：运行期间请保持页面打开；停止或关闭页面后当前动作取消、循环结束。"
    >
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        title="压测将真实驱动底盘导航、升降台与机械臂！"
        description="开始前请确认路线与机构周边清场、实体急停可触达、机器人已完成定位。「停止循环」会取消当前动作并在本轮边界结束；已完成的动作不会回退。"
      />
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={10}>
          <Card title="压测配置" loading={loadingOptions}>
            <Space
              orientation="vertical"
              size="middle"
              style={{ width: '100%' }}
            >
              <div>
                <Typography.Text type="secondary">机器人</Typography.Text>
                <Select
                  style={{ width: '100%' }}
                  placeholder="选择机器人"
                  value={robotId}
                  onChange={setRobotId}
                  options={robots.map((item) => ({
                    value: item.id,
                    label: `${item.name}（${item.id}）`,
                  }))}
                  disabled={running}
                />
                {robot ? (
                  <Space wrap style={{ marginTop: 4 }}>
                    <Tag color={robot.status === 'idle' ? 'green' : 'orange'}>
                      {ROBOT_STATUS_TEXT[robot.status]}
                    </Tag>
                    <Tag
                      color={
                        robot.localizationStatus === 'localized'
                          ? 'green'
                          : 'orange'
                      }
                    >
                      {LOCALIZATION_TEXT[robot.localizationStatus ?? 'unknown']}
                    </Tag>
                  </Space>
                ) : null}
              </div>
              <div>
                <Typography.Text type="secondary">任务模板</Typography.Text>
                <Select
                  style={{ width: '100%' }}
                  placeholder="选择已保存的模板（应用后覆盖下方配置）"
                  value={selectedTemplateId}
                  onChange={(value: string) => {
                    const template = templates.find(
                      (item) => item.id === value,
                    );
                    if (template) void applyTemplate(template);
                  }}
                  options={templates.map((item) => ({
                    value: item.id,
                    label: item.name,
                  }))}
                  showSearch={{ optionFilterProp: 'label' }}
                  disabled={running}
                  notFoundContent="暂无模板"
                />
                <Space style={{ marginTop: 8 }}>
                  <Button
                    icon={<SaveOutlined />}
                    onClick={openSaveModal}
                    disabled={running}
                  >
                    保存为模板
                  </Button>
                  <Popconfirm
                    title="删除任务模板？"
                    description={`将删除模板「${templates.find((item) => item.id === selectedTemplateId)?.name ?? ''}」，不影响正在运行的压测。`}
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => void removeTemplate()}
                    disabled={!selectedTemplateId || running}
                  >
                    <Button
                      danger
                      icon={<DeleteOutlined />}
                      disabled={!selectedTemplateId || running}
                    >
                      删除模板
                    </Button>
                  </Popconfirm>
                </Space>
              </div>
              <div>
                <Typography.Text type="secondary">地图</Typography.Text>
                <Select
                  style={{ width: '100%' }}
                  placeholder="选择地图"
                  value={mapId}
                  onChange={setMapId}
                  options={maps.map((item) => ({
                    value: item.id,
                    label: `${item.name}（${item.id}）`,
                  }))}
                  disabled={running}
                />
              </div>
              <div>
                <Typography.Text type="secondary">
                  目标点（书架点）
                </Typography.Text>
                <Select
                  style={{ width: '100%' }}
                  placeholder="选择目标点"
                  value={targetAnnotationId}
                  onChange={setTargetAnnotationId}
                  options={annotationOptions}
                  showSearch={{ optionFilterProp: 'label' }}
                  disabled={running}
                />
              </div>
              <div>
                <Typography.Text type="secondary">
                  取书动作（按顺序执行；升降杆高度按书籍登记值自动带出）
                </Typography.Text>
                <Space
                  orientation="vertical"
                  size="small"
                  style={{ width: '100%' }}
                >
                  {takeoutRows.map((row, index) => (
                    // 行数少且无重排，用序号做 key 即可。
                    // biome-ignore lint/suspicious/noArrayIndexKey: 编辑行按位置更新
                    <Row gutter={8} key={index} wrap={false} align="middle">
                      <Col flex="auto">
                        <Select
                          style={{ width: '100%' }}
                          placeholder={`取书 ${index + 1}：选择要取的书`}
                          value={row.bookId}
                          onChange={(value: string) =>
                            updateTakeoutRow(index, { bookId: value })
                          }
                          options={bookOptions}
                          showSearch={{ optionFilterProp: 'label' }}
                          disabled={running}
                        />
                      </Col>
                      <Col flex="96px">
                        <Select
                          style={{ width: '100%' }}
                          value={row.destinationSlot}
                          onChange={(value: number) =>
                            updateTakeoutRow(index, { destinationSlot: value })
                          }
                          options={slotOptions}
                          disabled={running}
                        />
                      </Col>
                      <Col>
                        <Button
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          aria-label={`删除取书动作 ${index + 1}`}
                          disabled={running || takeoutRows.length <= 1}
                          onClick={() => removeTakeoutRow(index)}
                        />
                      </Col>
                    </Row>
                  ))}
                  <Button
                    type="dashed"
                    block
                    icon={<PlusOutlined />}
                    onClick={addTakeoutRow}
                    disabled={
                      running || takeoutRows.length >= BOOK_STAND_SLOTS.length
                    }
                  >
                    添加取书
                  </Button>
                </Space>
              </div>
              <div>
                <Typography.Text type="secondary">
                  还书动作（中位还书：识别目标书后还到其左边，按顺序执行）
                </Typography.Text>
                <Space
                  orientation="vertical"
                  size="small"
                  style={{ width: '100%' }}
                >
                  {returnRows.map((row, index) => (
                    // biome-ignore lint/suspicious/noArrayIndexKey: 编辑行按位置更新
                    <Row gutter={8} key={index} wrap={false} align="middle">
                      <Col flex="96px">
                        <Select
                          style={{ width: '100%' }}
                          value={row.sourceSlot}
                          onChange={(value: number) =>
                            updateReturnRow(index, { sourceSlot: value })
                          }
                          options={slotOptions}
                          disabled={running}
                        />
                      </Col>
                      <Col flex="auto">
                        <Select
                          style={{ width: '100%' }}
                          placeholder={`还书 ${index + 1}：选择目标书（还到其左边）`}
                          value={row.referenceBookId}
                          onChange={(value: string) =>
                            updateReturnRow(index, { referenceBookId: value })
                          }
                          options={bookOptions}
                          showSearch={{ optionFilterProp: 'label' }}
                          disabled={running}
                        />
                      </Col>
                      <Col>
                        <Button
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          aria-label={`删除还书动作 ${index + 1}`}
                          disabled={running}
                          onClick={() => removeReturnRow(index)}
                        />
                      </Col>
                    </Row>
                  ))}
                  <Button
                    type="dashed"
                    block
                    icon={<PlusOutlined />}
                    onClick={addReturnRow}
                    disabled={
                      running || returnRows.length >= takeoutRows.length
                    }
                  >
                    添加还书
                  </Button>
                </Space>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  还书书格须是某个取书动作的目标槽位；目标书须留在架上，不能是该槽取入的书。
                </Typography.Text>
              </div>
              <Row gutter={12}>
                <Col span={12}>
                  <Typography.Text type="secondary">
                    升降速度档位
                  </Typography.Text>
                  <Select
                    style={{ width: '100%' }}
                    value={liftSpeedLevel}
                    onChange={setLiftSpeedLevel}
                    options={[
                      { value: 1, label: '1（慢速）' },
                      { value: 2, label: '2（中速）' },
                      { value: 3, label: '3（快速）' },
                    ]}
                    disabled={running}
                  />
                </Col>
                <Col span={12}>
                  <Typography.Text type="secondary">循环次数</Typography.Text>
                  <InputNumber
                    style={{ width: '100%' }}
                    min={1}
                    precision={0}
                    placeholder="留空：无限循环"
                    value={maxRounds}
                    onChange={(value) =>
                      setMaxRounds(typeof value === 'number' ? value : null)
                    }
                    disabled={running}
                  />
                </Col>
              </Row>
              <Space align="center">
                <Switch
                  checked={stopOnFailure}
                  onChange={setStopOnFailure}
                  disabled={running}
                />
                <Typography.Text>
                  失败即停（关闭后失败轮计入统计并继续）
                </Typography.Text>
              </Space>
              <Typography.Text type="secondary">
                取书成功后书放入对应槽位；还书时从指定槽位取回该书，中位还书会识别目标书并还到其左边。全部还书完成后收拢（失能机械臂、升降台降到停放高度），再直退
                1.5 米结束本轮；下一轮直接导航到书架点。
              </Typography.Text>
              <Space>
                <Popconfirm
                  title="一键运行默认任务？"
                  description={`按默认配置立即开跑：目标点 ${DEFAULT_TASK.targetLabel}，每轮${DEFAULT_TASK.takeouts.map((takeout) => `取「${takeout.bookName}」→${takeout.destinationSlot}号槽`).join('、')}、${DEFAULT_TASK.returns.map((item) => `还${item.sourceSlot}号槽（目标${item.referenceBookName}左边）`).join('、')}循环，收尾直退 1.5 米。确认现场已清场、急停可触达？`}
                  okText="运行"
                  cancelText="取消"
                  okButtonProps={{ danger: true }}
                  onConfirm={() => void applyDefaultTask()}
                  disabled={running || loadingOptions}
                >
                  <Button type="primary" disabled={running || loadingOptions}>
                    一键运行默认任务
                  </Button>
                </Popconfirm>
                <Popconfirm
                  title="确认开始压测？"
                  description="将立即开始真实导航与取还书循环，确认现场已清场、急停可触达？"
                  okText="开始"
                  cancelText="取消"
                  okButtonProps={{ danger: true }}
                  onConfirm={start}
                  disabled={running}
                >
                  <Button disabled={running}>开始压测</Button>
                </Popconfirm>
                <Button danger disabled={!running} onClick={stop}>
                  停止循环
                </Button>
              </Space>
            </Space>
          </Card>
        </Col>

        <Col xs={24} xl={14}>
          <Card
            title="运行状态"
            extra={
              running ? <Tag color="processing">运行中</Tag> : <Tag>未运行</Tag>
            }
          >
            <Space
              orientation="vertical"
              size="middle"
              style={{ width: '100%' }}
            >
              <Space wrap size="large">
                <Statistic
                  title="当前轮次"
                  value={round}
                  suffix={maxRounds ? `/ ${maxRounds}` : ''}
                />
                <Statistic
                  title="成功轮次"
                  value={successCount}
                  styles={{ content: { color: '#3f8600' } }}
                />
                <Statistic
                  title="失败轮次"
                  value={failCount}
                  styles={{
                    content: { color: failCount > 0 ? '#cf1322' : undefined },
                  }}
                />
                <Statistic title="已运行时长" value={fmtDuration(elapsedMs)} />
              </Space>
              <div>
                当前阶段：
                <Tag color={phaseMeta.color}>{phaseMeta.text}</Tag>
                {phase !== 'idle' && phaseDetail ? (
                  <Typography.Text type="secondary">
                    {phaseDetail}
                    {'　'}
                  </Typography.Text>
                ) : null}
                {currentTask?.workflowMessage ? (
                  <Typography.Text type="secondary">
                    {currentTask.workflowMessage}
                  </Typography.Text>
                ) : null}
              </div>
              {currentTask ? (
                <Progress
                  percent={currentTask.progress}
                  size="small"
                  status={
                    currentTask.status === 'failed' ? 'exception' : 'active'
                  }
                />
              ) : null}
              <Table<RoundLog>
                size="small"
                rowKey={(record) =>
                  `${record.round}-${record.result}-${record.at}`
                }
                pagination={false}
                dataSource={logs.slice(0, 20)}
                locale={{ emptyText: '尚未运行' }}
                columns={[
                  { title: '轮次', dataIndex: 'round', width: 64 },
                  {
                    title: '结果',
                    dataIndex: 'result',
                    width: 80,
                    render: (value: RoundLog['result']) =>
                      value === 'success' ? (
                        <Tag color="green">成功</Tag>
                      ) : value === 'failed' ? (
                        <Tag color="red">失败</Tag>
                      ) : (
                        <Tag>已停止</Tag>
                      ),
                  },
                  {
                    title: '用时',
                    dataIndex: 'durationS',
                    width: 90,
                    render: (value: number) => `${value.toFixed(0)} s`,
                  },
                  {
                    title: '信息',
                    dataIndex: 'message',
                    render: (value: string) => value || '—',
                  },
                  { title: '时间', dataIndex: 'at', width: 100 },
                ]}
              />
            </Space>
          </Card>

          <Card
            title="机器人位置与健康"
            style={{ marginTop: 16 }}
            extra={
              realtimeState.connected ? (
                <Tag color="green">实时通道已连接</Tag>
              ) : (
                <Tag color="orange">实时通道未连接</Tag>
              )
            }
          >
            <Space
              orientation="vertical"
              size="middle"
              style={{ width: '100%' }}
            >
              {pose ? (
                <Row gutter={16}>
                  <Col span={6}>
                    <Statistic title="X (m)" value={pose.x} precision={3} />
                  </Col>
                  <Col span={6}>
                    <Statistic title="Y (m)" value={pose.y} precision={3} />
                  </Col>
                  <Col span={6}>
                    <Statistic
                      title="朝向 (°)"
                      value={(pose.yaw * 180) / Math.PI}
                      precision={1}
                    />
                  </Col>
                </Row>
              ) : (
                <Typography.Text type="secondary">
                  暂无位姿数据（机器人离线或未上报）
                </Typography.Text>
              )}
              <Space wrap>
                <span>
                  定位：
                  {LOCALIZATION_TEXT[robot?.localizationStatus ?? 'unknown']}
                </span>
                {pose ? (
                  <Typography.Text type="secondary">
                    更新于 {new Date(pose.capturedAt).toLocaleTimeString()}
                  </Typography.Text>
                ) : null}
              </Space>
              <Space wrap>
                {HEALTH_ITEMS.map((item) => (
                  <Tag
                    key={item.key}
                    color={robot?.health?.[item.key] ? 'green' : 'red'}
                  >
                    {item.label}
                  </Tag>
                ))}
              </Space>
            </Space>
          </Card>

          <Card title="机械臂与硬件" style={{ marginTop: 16 }}>
            {hardware === null ? (
              <Alert
                type="error"
                showIcon
                title="BookArm 服务不可用"
                description="无法读取机械臂/升降台状态，请确认 lr-bookarm-takeout 服务已启动。"
              />
            ) : (
              <Space
                orientation="vertical"
                size="middle"
                style={{ width: '100%' }}
              >
                <Space wrap size="large">
                  <Statistic
                    title="升降台高度"
                    value={hardware.lift.height_mm ?? '—'}
                    precision={0}
                    suffix="mm"
                  />
                  <Statistic
                    title="推杆角度"
                    value={hardware.rod.angle_deg ?? '—'}
                    precision={1}
                    suffix="°"
                  />
                  <Statistic
                    title="CAN 帧率"
                    value={hardware.arm.health?.can_fps ?? 0}
                    precision={1}
                    suffix="fps"
                  />
                </Space>
                <Space wrap>
                  <span>
                    CAN：
                    {hardware.can.up ? (
                      <Tag color="green">在线</Tag>
                    ) : (
                      <Tag color="red">离线</Tag>
                    )}
                  </span>
                  <span>
                    机械臂：
                    {hardware.arm.health?.enabled ? (
                      <Tag color="green">已使能</Tag>
                    ) : (
                      <Tag>未使能</Tag>
                    )}
                  </span>
                  <span>
                    反馈：
                    {hardware.arm.health == null ? (
                      <Tag>未知</Tag>
                    ) : hardware.arm.health.healthy_feedback ? (
                      <Tag color="green">正常</Tag>
                    ) : (
                      <Tag color="red">异常</Tag>
                    )}
                  </span>
                  <span>
                    升降台：
                    {hardware.lift.connected ? (
                      <Tag color="green">已连接</Tag>
                    ) : (
                      <Tag color="orange">未连接</Tag>
                    )}
                  </span>
                  <span>
                    推杆：
                    {hardware.rod.connected ? (
                      <Tag color="green">已连接</Tag>
                    ) : (
                      <Tag color="red">未连接</Tag>
                    )}
                  </span>
                  <span>
                    相机：
                    {hardware.camera.present ? (
                      <Tag color="green">已连接</Tag>
                    ) : (
                      <Tag color="red">未连接</Tag>
                    )}
                  </span>
                  <span>
                    夹爪：
                    {hardware.arm.gripper_carry_armed ? (
                      <Tag color="blue">夹持中</Tag>
                    ) : (
                      <Tag>空</Tag>
                    )}
                  </span>
                </Space>
                {thermal?.lockout ? (
                  <Alert
                    type="error"
                    showIcon
                    title="温度保护已锁存，需处理后清除才能再次使能"
                  />
                ) : null}
                {hardware.arm.gripper_thermal_warning ? (
                  <Alert
                    type="warning"
                    showIcon
                    title={hardware.arm.gripper_thermal_warning}
                  />
                ) : null}
                <Table
                  size="small"
                  rowKey="name"
                  pagination={false}
                  dataSource={temperatureEntries.map(([name, temp]) => ({
                    name,
                    temp,
                  }))}
                  locale={{ emptyText: '暂无温度数据' }}
                  columns={[
                    { title: '电机', dataIndex: 'name', width: 90 },
                    {
                      title: `温度 (°C，上限 ${thermal?.limit_c ?? '—'})`,
                      dataIndex: 'temp',
                      render: (value: number) => (
                        <Typography.Text
                          type={
                            thermal && value >= thermal.limit_c
                              ? 'danger'
                              : undefined
                          }
                        >
                          {fmt(value, 0)}
                        </Typography.Text>
                      ),
                    },
                  ]}
                />
              </Space>
            )}
          </Card>
        </Col>
      </Row>
      <Modal
        title="保存为任务模板"
        open={saveModalOpen}
        onOk={() => void saveTemplate()}
        onCancel={() => setSaveModalOpen(false)}
        okText={
          templates.some((item) => item.name === saveTemplateName.trim())
            ? '覆盖保存'
            : '保存'
        }
        okButtonProps={{
          danger: templates.some(
            (item) => item.name === saveTemplateName.trim(),
          ),
          loading: templateSaving,
        }}
        cancelText="取消"
      >
        <Space orientation="vertical" size="small" style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            保存当前「地图 / 目标点 / 取还书动作 / 速度档位 /
            循环设置」，下次可一键回填。
          </Typography.Text>
          <Input
            placeholder="模板名称，如：书架点 4 两取两还"
            maxLength={120}
            value={saveTemplateName}
            onChange={(event) => setSaveTemplateName(event.target.value)}
          />
          {templates.some((item) => item.name === saveTemplateName.trim()) ? (
            <Alert
              type="warning"
              showIcon
              title={`已存在同名模板，保存将覆盖「${saveTemplateName.trim()}」`}
            />
          ) : null}
        </Space>
      </Modal>
    </PageContainer>
  );
}
