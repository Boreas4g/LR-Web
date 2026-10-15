import {
  AimOutlined,
  ArrowDownOutlined,
  ArrowLeftOutlined,
  ArrowRightOutlined,
  ArrowUpOutlined,
  CloudUploadOutlined,
  EnvironmentOutlined,
  FileImageOutlined,
  FileTextOutlined,
  PushpinOutlined,
  ReloadOutlined,
  RobotOutlined,
  SendOutlined,
  ThunderboltOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Empty,
  Input,
  InputNumber,
  List,
  Modal,
  message,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from 'antd';
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import {
  firstConnectedGamepad,
  gamepadVelocity,
  isZeroVelocity,
  type JogVelocity,
  type SentJog,
  shouldSendGamepadJog,
  ZERO_VELOCITY,
} from '@/pages/library/mapping/gamepad';
import {
  autoLocalize,
  cancelAnnotationNavigation,
  createMapAnnotation,
  deleteLibraryMap,
  deleteMapAnnotation,
  getMapAnnotations,
  getMapPreview,
  getMappingLocOverlay,
  getMapVersions,
  getRobotRealtimeState,
  getRobots,
  importMap,
  navigateRobotToAnnotation,
  pollTasks,
  previewCruiseSync,
  sendManualJog,
  setRobotInitialPose,
  subscribeRobotRealtime,
  switchRobotMap,
  syncCruiseAnnotations,
  updateMapAnnotation,
} from '@/services/libraryRobot';
import { IMU_DISCONNECTED_ALERT } from '@/services/libraryRobot/health';
import type {
  CruiseSyncPreview,
  DispatchTask,
  MapAnnotation,
  MapAnnotationKind,
  MappingLocOverlay,
  MapVersion,
  RobotSummary,
} from '@/services/libraryRobot/types';
import {
  ANNOTATION_KIND_LABELS,
  type AnnotationKindFilter,
  filterAnnotationsByKeyword,
  filterAnnotationsByKind,
  nextAnnotationLabel,
  yawArrowCssDegrees,
} from '@/utils/annotation';
import {
  type AnnotationNavigateTarget,
  annotationNavigateTarget,
} from './annotationNavigate';
import {
  buildAnnotationUpdate,
  IDENTITY_MAP_TRANSFORM,
  MAX_MAP_SCALE,
  type MapViewTransform,
  MIN_MAP_SCALE,
  mapCoordsFromPixel,
  mapLayerTransform,
  mapPixelFromClientPoint,
  mapPixelFromMapCoords,
  markerCenterTransform,
  normalizeYawDegrees,
  panMapBy,
  radiansToDegrees,
  zoomMapAt,
} from './mapView';
import YawDegreesField from './YawDegreesField';

type ImportFiles = {
  pgm?: File;
  yaml?: File;
};

type PendingPixel = { x: number; y: number };
type InitialPoseDraft = {
  robotId: string;
  x: number;
  y: number;
  yaw: number;
  sourceLabel: string;
};

const initialPoseAnnotationKinds: MapAnnotationKind[] = [
  'shelf',
  'cruise',
  'dock',
  'charging',
  'delivery',
];

const annotationMeta: Record<
  MapAnnotationKind,
  { color: string; icon: React.ReactNode; label: string }
> = {
  shelf: {
    color: '#1677ff',
    icon: <FileTextOutlined />,
    label: '书架',
  },
  charging: {
    color: '#722ed1',
    icon: <ThunderboltOutlined />,
    label: '充电区',
  },
  delivery: {
    color: '#fa8c16',
    icon: <EnvironmentOutlined />,
    label: '交付点',
  },
  dock: {
    color: '#13c2c2',
    icon: <AimOutlined />,
    label: '停靠位',
  },
  zone: {
    color: '#eb2f96',
    icon: <EnvironmentOutlined />,
    label: '区域',
  },
  cruise: {
    color: '#389e0d',
    icon: <AimOutlined />,
    label: '巡航点',
  },
};

export default function LibraryMapPage() {
  const [mapVersions, setMapVersions] = useState<MapVersion[]>([]);
  const [selectedId, setSelectedId] = useState<string>();
  const [previewUrl, setPreviewUrl] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importing, setImporting] = useState(false);
  const [mapName, setMapName] = useState('');
  const [mapImportId, setMapImportId] = useState('');
  const [files, setFiles] = useState<ImportFiles>({});
  const [annotations, setAnnotations] = useState<MapAnnotation[]>([]);
  const [annotationMode, setAnnotationMode] = useState(false);
  const [annotationOpen, setAnnotationOpen] = useState(false);
  const [annotationSaving, setAnnotationSaving] = useState(false);
  const [pendingPixel, setPendingPixel] = useState<PendingPixel>();
  const [annotationKind, setAnnotationKind] =
    useState<MapAnnotationKind>('shelf');
  const [annotationLabel, setAnnotationLabel] = useState('');
  const [annotationYaw, setAnnotationYaw] = useState(0);
  const [annotationSourceLabel, setAnnotationSourceLabel] = useState<string>();
  const [selectedAnnotation, setSelectedAnnotation] = useState<MapAnnotation>();
  const [annotationDeleting, setAnnotationDeleting] = useState(false);
  const [annotationRenaming, setAnnotationRenaming] = useState(false);
  const [annotationEditLabel, setAnnotationEditLabel] = useState('');
  const [coordinateEdit, setCoordinateEdit] = useState<MapAnnotation>();
  const [coordinateEditLabel, setCoordinateEditLabel] = useState('');
  const [coordinateEditX, setCoordinateEditX] = useState(0);
  const [coordinateEditY, setCoordinateEditY] = useState(0);
  const [coordinateEditYaw, setCoordinateEditYaw] = useState(0);
  const [coordinateEditSaving, setCoordinateEditSaving] = useState(false);
  const [syncingCruise, setSyncingCruise] = useState(false);
  const [cruisePreviewLoading, setCruisePreviewLoading] = useState(false);
  const [cruisePreview, setCruisePreview] = useState<CruiseSyncPreview>();
  const [robots, setRobots] = useState<RobotSummary[]>([]);
  const realtimeState = useSyncExternalStore(
    subscribeRobotRealtime,
    getRobotRealtimeState,
    getRobotRealtimeState,
  );
  const [localizationSetupOpen, setLocalizationSetupOpen] = useState(false);
  const [localizationRobotId, setLocalizationRobotId] = useState<string>();
  const [localizationAnnotationId, setLocalizationAnnotationId] =
    useState<string>();
  const [localizationModeRobotId, setLocalizationModeRobotId] =
    useState<string>();
  const [initialPoseDraft, setInitialPoseDraft] = useState<InitialPoseDraft>();
  const [localizationSaving, setLocalizationSaving] = useState(false);
  const [mapSwitchingId, setMapSwitchingId] = useState<string>();
  const [mapDeletingId, setMapDeletingId] = useState<string>();
  const [annotationNavigatingId, setAnnotationNavigatingId] =
    useState<string>();
  // 标注导航编排（导航→低速精调）的实时进度：父任务 + 各阶段指令任务。
  const [navFlowTask, setNavFlowTask] = useState<DispatchTask>();
  const [navFlowStages, setNavFlowStages] = useState<{
    navigation?: DispatchTask;
    fineTune?: DispatchTask;
  }>({});
  const [navFlowStopping, setNavFlowStopping] = useState(false);

  async function stopAnnotationNavigation() {
    if (!navFlowTask) return;
    setNavFlowStopping(true);
    try {
      setNavFlowTask(
        await cancelAnnotationNavigation(navFlowTask.robotId, navFlowTask.id),
      );
      message.info('已请求停止导航，车端正在停止当前阶段。');
    } catch (error) {
      message.error(error instanceof Error ? error.message : '停止导航失败。');
    } finally {
      setNavFlowStopping(false);
    }
  }
  const [autoLocalizingId, setAutoLocalizingId] = useState<string>();
  const autoLocalizeDeadlineRef = useRef(0);
  // 车端受理后 localizationStatus 会先离开 localized（manual_initializing /
  // global_localizing），看到该过渡后再出现的 localized 才是本次收敛结果。
  const autoLocalizeConvergingRef = useRef(false);
  const [initialPoseWaitingId, setInitialPoseWaitingId] = useState<string>();
  const initialPoseDeadlineRef = useRef(0);
  const initialPoseSentAtRef = useRef(0);
  const initialPoseConvergingRef = useRef(false);
  const [locOverlayEnabled, setLocOverlayEnabled] = useState(false);
  const [locOverlay, setLocOverlay] = useState<MappingLocOverlay>();
  const locOverlayWarnedAtRef = useRef(0);

  const robotCurrentMapId = useMemo(
    () => robots.find((item) => item.status !== 'offline' && item.mapId)?.mapId,
    [robots],
  );
  const selectedMap = useMemo(
    () =>
      mapVersions.find((item) => item.id === selectedId) ??
      mapVersions.find((item) => item.id === robotCurrentMapId) ??
      mapVersions[0],
    [mapVersions, selectedId, robotCurrentMapId],
  );
  const mapRobots = useMemo(
    () => robots.filter((item) => item.mapId === selectedMap?.id),
    [robots, selectedMap?.id],
  );
  const localizationRobot = useMemo(
    () => mapRobots.find((item) => item.id === localizationRobotId),
    [localizationRobotId, mapRobots],
  );
  const initialPoseAnnotations = useMemo(
    () =>
      annotations.filter((item) =>
        initialPoseAnnotationKinds.includes(item.kind),
      ),
    [annotations],
  );
  const mapSwitchRobot = useMemo(
    () => mapRobots[0] ?? robots.find((item) => item.status !== 'offline'),
    [mapRobots, robots],
  );
  // 建图期间导航栈停止，禁止切换地图（服务端同样 409 互斥）。
  const mapSwitchRobotMapping = mapSwitchRobot?.status === 'mapping';
  // 自动定位目标：当前选中地图上的第一台非离线机器人。
  const autoLocalizeTarget = useMemo<{
    robot?: RobotSummary;
    reason?: string;
  }>(() => {
    if (!selectedMap) return { reason: '请先选择地图。' };
    if (!mapRobots.length)
      return { reason: '当前地图没有机器人：请先将机器人「设为当前地图」。' };
    const robot = mapRobots.find((item) => item.status !== 'offline');
    if (!robot) return { reason: '当前地图的机器人已离线，不能自动定位。' };
    return { robot };
  }, [mapRobots, selectedMap]);
  // 标注导航目标：当前选中地图上空闲且已定位的非离线机器人。
  const navigateTarget = useMemo(
    () => annotationNavigateTarget(robots, selectedMap?.id),
    [robots, selectedMap?.id],
  );
  // 雷达点云叠图目标：当前选中地图上的第一台非离线机器人。
  const locOverlayRobot = useMemo(
    () => mapRobots.find((item) => item.status !== 'offline'),
    [mapRobots],
  );
  const locOverlayCapturedAt = useMemo(() => {
    if (!locOverlay) return undefined;
    const capturedAt = new Date(locOverlay.capturedAt * 1000);
    return Number.isNaN(capturedAt.getTime())
      ? undefined
      : capturedAt.toLocaleTimeString('zh-CN', { hour12: false });
  }, [locOverlay]);
  const poseAnnotationTarget = useMemo<{
    robot?: RobotSummary;
    reason?: string;
  }>(() => {
    if (!selectedMap) return { reason: '请先选择地图。' };
    const candidates = robots.filter((item) => item.mapId === selectedMap.id);
    if (!candidates.length) return { reason: '当前地图没有已接入的机器人。' };
    const localized = candidates.filter(
      (item) => item.localizationStatus === 'localized',
    );
    if (!localized.length)
      return { reason: '当前地图的机器人尚未完成实时定位。' };
    const now = Date.now();
    const robot = localized.find((item) => {
      if (!item.pose) return false;
      const capturedAt = new Date(item.pose.capturedAt).getTime();
      return !Number.isNaN(capturedAt) && now - capturedAt <= 10_000;
    });
    if (!robot) return { reason: '机器人位置已过期（超过 10 秒未上报）。' };
    return { robot };
  }, [robots, selectedMap]);

  async function refreshMaps(preferredId?: string) {
    setLoading(true);
    try {
      const versions = await getMapVersions();
      setMapVersions(versions);
      setSelectedId(preferredId ?? ((current) => current ?? versions[0]?.id));
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '读取地图版本失败。',
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refreshMaps();
  }, []);

  useEffect(() => {
    async function refreshRobots() {
      try {
        const robotData = await getRobots();
        setRobots(robotData);
      } catch (error) {
        message.error(
          error instanceof Error ? error.message : '读取机器人实时位置失败。',
        );
      }
    }
    void refreshRobots();
    // Keep an authoritative low-frequency fallback when a proxy or network drops WebSocket.
    const interval = window.setInterval(() => void refreshRobots(), 10_000);
    return () => {
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    if (realtimeState.robots.length) setRobots(realtimeState.robots);
  }, [realtimeState.robots]);

  // 自动定位收敛等待：复用上方机器人实时/轮询刷新，localized 即完成，
  // 超过 60 秒未收敛则提示仍未定位。
  useEffect(() => {
    if (!autoLocalizingId) return;
    const robot = robots.find((item) => item.id === autoLocalizingId);
    if (!robot) return;
    if (robot.localizationStatus !== 'localized') {
      autoLocalizeConvergingRef.current = true;
    } else if (autoLocalizeConvergingRef.current) {
      setAutoLocalizingId(undefined);
      const pose = robot.pose;
      message.success(
        pose
          ? `自动定位完成：(${pose.x.toFixed(3)}, ${pose.y.toFixed(3)}) m，朝向 ${radiansToDegrees(pose.yaw).toFixed(1)}°。`
          : '自动定位完成。',
      );
      return;
    }
    if (Date.now() >= autoLocalizeDeadlineRef.current) {
      setAutoLocalizingId(undefined);
      message.warning(
        '自动定位 60 秒内未收敛，机器人仍未定位，请改用「网页确认位置」。',
      );
    }
  }, [autoLocalizingId, robots]);

  // 手动确认位置收敛等待：与自动定位同一规则，先看到离开 localized 的
  // 过渡，再把随后的 localized 当作本次校验通过。发送时已是 localized 的
  // 机器人会被车端忽略该位姿，直接结束等待。
  useEffect(() => {
    if (!initialPoseWaitingId) return;
    const robot = robots.find((item) => item.id === initialPoseWaitingId);
    if (!robot) return;
    if (robot.localizationStatus !== 'localized') {
      initialPoseConvergingRef.current = true;
    } else if (initialPoseConvergingRef.current) {
      setInitialPoseWaitingId(undefined);
      const pose = robot.pose;
      message.success(
        pose
          ? `定位校验通过：(${pose.x.toFixed(3)}, ${pose.y.toFixed(3)}) m，机器人已实时定位。`
          : '定位校验通过，机器人已实时定位。',
      );
      return;
    } else {
      setInitialPoseWaitingId(undefined);
      return;
    }
    // 车端现在会用激光扫描校验所选点：匹配不上会立刻回到 manual_required。
    // 受理中的正确点通常 1-2 秒内转为 localized；给足传输余量后仍停在
    // manual_required 即视为被拒，马上告知操作员而不是干等 60 秒。
    if (
      robot.localizationStatus === 'manual_required' &&
      Date.now() - initialPoseSentAtRef.current > 5_000
    ) {
      setInitialPoseWaitingId(undefined);
      message.error(
        '定位校验未通过：所选位置与激光扫描不匹配，请对照机器人实际位置重新选择。',
      );
      return;
    }
    if (Date.now() >= initialPoseDeadlineRef.current) {
      setInitialPoseWaitingId(undefined);
      message.warning(
        '位置确认 60 秒内未通过 AMCL 校验，请重新在地图上选择，或改用「自动定位」。',
      );
    }
  }, [initialPoseWaitingId, robots]);

  useEffect(() => {
    if (!selectedMap) {
      setPreviewUrl(undefined);
      return;
    }
    let cancelled = false;
    let objectUrl: string | undefined;
    setPreviewLoading(true);
    setPreviewUrl(undefined);
    void getMapPreview(selectedMap.id)
      .then((url) => {
        objectUrl = url;
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        setPreviewUrl(url);
      })
      .catch((error) => {
        if (!cancelled)
          message.error(
            error instanceof Error ? error.message : '读取地图预览失败。',
          );
      })
      .finally(() => {
        if (!cancelled) setPreviewLoading(false);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [selectedMap?.id]);

  useEffect(() => {
    if (!selectedMap) {
      setAnnotations([]);
      return;
    }
    void getMapAnnotations(selectedMap.id)
      .then(setAnnotations)
      .catch((error) =>
        message.error(
          error instanceof Error ? error.message : '读取地图标注失败。',
        ),
      );
  }, [selectedMap?.id]);

  // 雷达点云叠图：开启后每 2 秒拉取一次车端定位点云；失败只节流提示，保留已有叠层。
  useEffect(() => {
    const robotId = locOverlayEnabled ? locOverlayRobot?.id : undefined;
    if (!robotId) {
      setLocOverlay(undefined);
      return;
    }
    const refreshLocOverlay = async () => {
      try {
        setLocOverlay(await getMappingLocOverlay(robotId));
      } catch (error) {
        const now = Date.now();
        if (now - locOverlayWarnedAtRef.current >= 5_000) {
          locOverlayWarnedAtRef.current = now;
          message.warning(
            error instanceof Error ? error.message : '读取雷达点云失败。',
          );
        }
      }
    };
    void refreshLocOverlay();
    const timer = window.setInterval(() => void refreshLocOverlay(), 2_000);
    return () => window.clearInterval(timer);
  }, [locOverlayEnabled, locOverlayRobot?.id]);

  async function submitImport() {
    if (!mapName.trim() || !files.yaml || !files.pgm) {
      message.warning('请填写地图名称，并分别选择 YAML 和 PGM 文件。');
      return;
    }
    setImporting(true);
    try {
      const imported = await importMap({
        name: mapName.trim(),
        id: mapImportId.trim() || undefined,
        yamlFile: files.yaml,
        pgmFile: files.pgm,
      });
      setImportOpen(false);
      setMapName('');
      setMapImportId('');
      setFiles({});
      await refreshMaps(imported.id);
      if (imported.vehicleRegistered) {
        message.success(`${imported.name} 已导入为草稿版本，已注册到车端。`);
      } else {
        message.warning(
          `${imported.name} 已导入为草稿版本，未注册到车端${
            imported.vehicleRegisterNote
              ? `：${imported.vehicleRegisterNote}`
              : '。'
          }`,
        );
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : '导入地图失败。');
    } finally {
      setImporting(false);
    }
  }

  function beginAnnotation(
    pixel: PendingPixel,
    options?: { yawDegrees?: number; sourceLabel?: string },
  ) {
    setPendingPixel(pixel);
    setAnnotationKind('shelf');
    setAnnotationLabel(nextAnnotationLabel('shelf', annotations));
    setAnnotationYaw(options?.yawDegrees ?? 0);
    setAnnotationSourceLabel(options?.sourceLabel);
    setAnnotationOpen(true);
  }

  // 「以车辆当前位置标注」唯一入口：页头按钮与标注卡片按钮共用同一严格校验
  // （localized 且位姿 10 秒内新鲜，见 poseAnnotationTarget）。
  function beginAnnotationFromRobot() {
    const robot = poseAnnotationTarget.robot;
    if (!selectedMap || !robot?.pose) return;
    const capturedAt = new Date(robot.pose.capturedAt);
    const capturedLabel = Number.isNaN(capturedAt.getTime())
      ? '未知时间'
      : capturedAt.toLocaleTimeString('zh-CN', { hour12: false });
    beginAnnotation(mapPixelFromMapCoords(selectedMap, robot.pose), {
      yawDegrees: Math.round(normalizeYawDegrees(robot.pose.yaw)),
      sourceLabel: `机器人当前位置：${robot.name}（${robot.id}），位姿采集于 ${capturedLabel}`,
    });
  }

  async function switchToMap(mapId: string) {
    if (!mapSwitchRobot) {
      message.warning('当前没有可用的机器人，无法切换地图。');
      return;
    }
    setMapSwitchingId(mapId);
    try {
      const result = await switchRobotMap(mapSwitchRobot.id, mapId);
      message.success(result.message || '地图已切换。');
    } catch (error) {
      message.error(error instanceof Error ? error.message : '切换地图失败。');
    } finally {
      setMapSwitchingId(undefined);
    }
  }

  async function removeMap(mapId: string) {
    setMapDeletingId(mapId);
    try {
      await deleteLibraryMap(mapId);
      message.success('地图已删除。');
      await refreshMaps();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '删除地图失败。');
    } finally {
      setMapDeletingId(undefined);
    }
  }

  async function navigateToAnnotation(annotation: MapAnnotation) {
    const robot = navigateTarget.robot;
    if (!selectedMap || !robot) return;
    setAnnotationNavigatingId(annotation.id);
    try {
      const task = await navigateRobotToAnnotation(robot.id, {
        mapId: selectedMap.id,
        annotationId: annotation.id,
      });
      setNavFlowTask(task);
      setNavFlowStages({});
      message.success(
        `开始导航至标注点「${annotation.label}」，到位后自动低速精调。`,
      );
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '导航命令下发失败。',
      );
    } finally {
      setAnnotationNavigatingId(undefined);
    }
  }

  // 标注导航编排的实时进度：父任务未结束时每 2 秒轮询一次任务列表，
  // 同步父任务（阶段文案/进度）与导航、精调两条阶段指令的状态。
  const navFlowActive =
    navFlowTask !== undefined &&
    !['completed', 'failed', 'cancelled'].includes(navFlowTask.status);
  useEffect(() => {
    if (!navFlowActive || !navFlowTask) return;
    const taskId = navFlowTask.id;
    let stopped = false;
    const poll = async () => {
      try {
        const tasks = await pollTasks();
        if (stopped) return;
        const parent = tasks.find((item) => item.id === taskId);
        if (!parent) return;
        setNavFlowTask(parent);
        const details = parent.workflowDetails;
        setNavFlowStages({
          navigation: tasks.find(
            (item) => item.id === details?.navigationTaskId,
          ),
          fineTune: tasks.find((item) => item.id === details?.fineTuneTaskId),
        });
      } catch {
        // 进度轮询尽力而为，下一次间隔自动重试。
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2_000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navFlowActive, navFlowTask?.id]);

  // ---- 车辆控制（按住就走，松开缓停；jog 直发无租约，失败由车端看门狗停车） ----

  const DRIVE_LINEAR_SPEED = 0.3;
  const DRIVE_ANGULAR_SPEED = 0.5;

  const driveRobot = mapRobots[0];
  // 机械臂作业/建图期间禁止遥控底盘（服务端对点动同样 409 互斥）。
  const driveRobotBusy =
    driveRobot?.status === 'busy' || driveRobot?.status === 'mapping';
  // 本次点动/手柄发令的目标机器人：按下一刻锁定，避免按住期间机器人列表
  // 刷新后把后续帧（含零速帧）发给另一台车。
  const driveRobotIdRef = useRef<string | undefined>(undefined);
  const driveDirectionRef = useRef<
    { linear: number; angular: number } | undefined
  >(undefined);
  const driveTimerRef = useRef<number | undefined>(undefined);
  const [driveActive, setDriveActive] = useState<string>();

  function sendDriveVelocity(
    direction: { linear: number; angular: number },
    durationMs = 500,
  ) {
    const robotId = driveRobotIdRef.current;
    if (!robotId) return;
    sendManualJog(robotId, {
      linear: direction.linear,
      angular: direction.angular,
      durationMs,
    }).catch(() => {
      // 发令失败不做恢复：车端 1 秒看门狗自动停车，按住期间下一拍继续尝试。
    });
  }

  function driveTick() {
    const direction = driveDirectionRef.current;
    if (!direction) return;
    sendDriveVelocity(direction);
  }

  function startDrive(key: string, linear: number, angular: number) {
    driveRobotIdRef.current = driveRobot?.id;
    driveDirectionRef.current = { linear, angular };
    setDriveActive(key);
    driveTick();
    if (driveTimerRef.current) window.clearInterval(driveTimerRef.current);
    driveTimerRef.current = window.setInterval(driveTick, 300);
  }

  function stopDrive() {
    driveDirectionRef.current = undefined;
    setDriveActive(undefined);
    if (driveTimerRef.current) window.clearInterval(driveTimerRef.current);
    driveTimerRef.current = undefined;
    // 立即补一帧零速，减速段由车端 velocity_smoother 平滑执行。
    sendDriveVelocity({ linear: 0, angular: 0 }, 300);
  }

  useEffect(
    () => () => {
      if (driveTimerRef.current) window.clearInterval(driveTimerRef.current);
      const robotId = driveRobotIdRef.current;
      if (robotId) {
        sendManualJog(robotId, {
          linear: 0,
          angular: 0,
          durationMs: 300,
        }).catch(() => undefined);
      }
    },
    [],
  );

  // ---- 手柄遥控（复用上方 jog 直发链路，与按钮点动互斥） ----

  const [gamepadId, setGamepadId] = useState<string>();
  const [gamepadEnabled, setGamepadEnabled] = useState(false);
  const driveRobotId = driveRobot?.id;

  useEffect(() => {
    const updateGamepad = () => setGamepadId(firstConnectedGamepad()?.id);
    window.addEventListener('gamepadconnected', updateGamepad);
    window.addEventListener('gamepaddisconnected', updateGamepad);
    // Chrome only exposes a gamepad after a button press while the page is
    // focused; polling also catches pads activated before this page loaded.
    const poll = window.setInterval(updateGamepad, 1000);
    updateGamepad();
    return () => {
      window.removeEventListener('gamepadconnected', updateGamepad);
      window.removeEventListener('gamepaddisconnected', updateGamepad);
      window.clearInterval(poll);
    };
  }, []);

  useEffect(() => {
    if (!driveRobotId || driveRobotBusy) setGamepadEnabled(false);
  }, [driveRobotId, driveRobotBusy]);

  useEffect(() => {
    if (!gamepadEnabled || !driveRobotId) return;
    driveRobotIdRef.current = driveRobotId;
    let animationFrame = 0;
    let lastSent: SentJog | undefined;

    const send = (velocity: JogVelocity) => {
      lastSent = { velocity, at: performance.now() };
      sendDriveVelocity(velocity);
    };
    // 绕过节流直接补零速：回中、断开、关闭开关、卸载都必须以零速收尾。
    const stop = () => {
      if (!lastSent || isZeroVelocity(lastSent.velocity)) return;
      sendDriveVelocity(ZERO_VELOCITY);
      lastSent = { velocity: ZERO_VELOCITY, at: performance.now() };
    };
    const poll = () => {
      const gamepad = firstConnectedGamepad();
      if (!gamepad) {
        stop();
      } else {
        const velocity = gamepadVelocity(
          gamepad,
          DRIVE_LINEAR_SPEED,
          DRIVE_ANGULAR_SPEED,
        );
        if (shouldSendGamepadJog(lastSent, velocity, performance.now()))
          send(velocity);
      }
      animationFrame = window.requestAnimationFrame(poll);
    };
    poll();
    return () => {
      window.cancelAnimationFrame(animationFrame);
      stop();
    };
  }, [gamepadEnabled, driveRobotId]);

  function openAnnotationDetails(annotation: MapAnnotation) {
    setSelectedAnnotation(annotation);
    setAnnotationEditLabel(annotation.label);
  }

  async function submitAnnotation() {
    if (!selectedMap || !pendingPixel || !annotationLabel.trim()) {
      message.warning('请填写标注名称。');
      return;
    }
    setAnnotationSaving(true);
    try {
      const created = await createMapAnnotation(selectedMap.id, {
        kind: annotationKind,
        label: annotationLabel.trim(),
        pixelX: pendingPixel.x,
        pixelY: pendingPixel.y,
        yaw: (annotationYaw * Math.PI) / 180,
      });
      setAnnotations((current) => [...current, created]);
      setAnnotationOpen(false);
      setPendingPixel(undefined);
      // 保持标注模式：保存后可立即点下一个位置连续打点。
      message.success(
        `${created.label} 已保存到地图，可继续点击地图标注下一点。`,
      );
      if (created.labelWarning) message.warning(created.labelWarning);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存标注失败。');
    } finally {
      setAnnotationSaving(false);
    }
  }

  async function removeAnnotation() {
    if (!selectedMap || !selectedAnnotation) return;
    setAnnotationDeleting(true);
    try {
      await deleteMapAnnotation(selectedMap.id, selectedAnnotation.id);
      setAnnotations((current) =>
        current.filter((item) => item.id !== selectedAnnotation.id),
      );
      setSelectedAnnotation(undefined);
      message.success(`${selectedAnnotation.label} 已删除。`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '删除标注失败。');
    } finally {
      setAnnotationDeleting(false);
    }
  }

  async function saveAnnotationName() {
    if (!selectedMap || !selectedAnnotation || !annotationEditLabel.trim()) {
      message.warning('请填写标注名称。');
      return;
    }
    setAnnotationRenaming(true);
    try {
      const updated = await updateMapAnnotation(
        selectedMap.id,
        selectedAnnotation.id,
        {
          label: annotationEditLabel.trim(),
        },
      );
      setAnnotations((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      setSelectedAnnotation(updated);
      setAnnotationEditLabel(updated.label);
      message.success('标注名称已保存。');
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '保存标注名称失败。',
      );
    } finally {
      setAnnotationRenaming(false);
    }
  }

  function beginCoordinateEdit(annotation: MapAnnotation) {
    setCoordinateEdit(annotation);
    setCoordinateEditLabel(annotation.label);
    setCoordinateEditX(Number(annotation.mapX.toFixed(2)));
    setCoordinateEditY(Number(annotation.mapY.toFixed(2)));
    setCoordinateEditYaw(
      Math.round(radiansToDegrees(annotation.yaw) * 10) / 10,
    );
  }

  async function saveCoordinateEdit() {
    if (!selectedMap || !coordinateEdit) return;
    if (!coordinateEditLabel.trim()) {
      message.warning('请填写标注名称。');
      return;
    }
    setCoordinateEditSaving(true);
    try {
      const updated = await updateMapAnnotation(
        selectedMap.id,
        coordinateEdit.id,
        buildAnnotationUpdate({
          label: coordinateEditLabel,
          x: coordinateEditX,
          y: coordinateEditY,
          yawDegrees: coordinateEditYaw,
        }),
      );
      setAnnotations((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      if (selectedAnnotation?.id === updated.id) {
        setSelectedAnnotation(updated);
        setAnnotationEditLabel(updated.label);
      }
      setCoordinateEdit(undefined);
      message.success(`${updated.label} 的坐标已保存。`);
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '保存标注坐标失败。',
      );
    } finally {
      setCoordinateEditSaving(false);
    }
  }

  async function openCruiseSyncPreview() {
    if (!selectedMap) return;
    setCruisePreviewLoading(true);
    try {
      setCruisePreview(await previewCruiseSync(selectedMap.id));
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '读取巡航点同步预览失败。',
      );
    } finally {
      setCruisePreviewLoading(false);
    }
  }

  async function executeCruiseSync() {
    if (!selectedMap) return;
    setSyncingCruise(true);
    try {
      const created = await syncCruiseAnnotations(selectedMap.id);
      setAnnotations(await getMapAnnotations(selectedMap.id));
      setCruisePreview(undefined);
      message.success(
        created.length
          ? `已导入 ${created.length} 个巡航点。`
          : '巡航点已是最新状态。',
      );
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '同步巡航点失败。',
      );
    } finally {
      setSyncingCruise(false);
    }
  }

  // 拖拽改位：marker 拖动松开后弹确认，确认才 PATCH 坐标（取消则位置回弹）。
  function confirmAnnotationMove(
    annotation: MapAnnotation,
    pixel: PendingPixel,
  ) {
    if (!selectedMap) return;
    const coords = mapCoordsFromPixel(selectedMap, pixel);
    Modal.confirm({
      title: '移动标注',
      content: `将「${annotation.label}」移动到 (${coords.x.toFixed(2)}, ${coords.y.toFixed(2)}) m？`,
      okText: '确认移动',
      cancelText: '取消',
      onOk: async () => {
        try {
          const updated = await updateMapAnnotation(
            selectedMap.id,
            annotation.id,
            { x: coords.x, y: coords.y },
          );
          setAnnotations((current) =>
            current.map((item) => (item.id === updated.id ? updated : item)),
          );
          setSelectedAnnotation((current) =>
            current?.id === updated.id ? updated : current,
          );
          message.success(`「${updated.label}」已移动到新位置。`);
        } catch (error) {
          message.error(
            error instanceof Error ? error.message : '移动标注失败。',
          );
          throw error;
        }
      },
    });
  }

  function beginWebLocalization() {
    if (!selectedMap || !localizationRobot) {
      message.warning('请选择当前地图中已接入的机器人。');
      return;
    }
    setAnnotationMode(false);
    const annotation = initialPoseAnnotations.find(
      (item) => item.id === localizationAnnotationId,
    );
    if (annotation) {
      setLocalizationSetupOpen(false);
      setInitialPoseDraft({
        robotId: localizationRobot.id,
        x: annotation.mapX,
        y: annotation.mapY,
        yaw: annotation.yaw,
        sourceLabel: `${annotationMeta[annotation.kind].label}：${annotation.label}`,
      });
      return;
    }
    setLocalizationSetupOpen(false);
    setLocalizationModeRobotId(localizationRobot.id);
    message.info('请在地图上按住机器人实际位置，并向车头方向拖动。');
  }

  function selectInitialPose(pixel: PendingPixel, yaw: number) {
    if (!selectedMap || !localizationModeRobotId) return;
    setLocalizationModeRobotId(undefined);
    setInitialPoseDraft({
      robotId: localizationModeRobotId,
      x: selectedMap.origin.x + pixel.x * selectedMap.resolution,
      y:
        selectedMap.origin.y +
        (selectedMap.height - 1 - pixel.y) * selectedMap.resolution,
      yaw,
      sourceLabel: '在地图上手动选择',
    });
  }

  async function submitInitialPose() {
    if (!selectedMap || !initialPoseDraft) return;
    setLocalizationSaving(true);
    try {
      await setRobotInitialPose(initialPoseDraft.robotId, {
        mapId: selectedMap.id,
        frameId: 'map',
        x: initialPoseDraft.x,
        y: initialPoseDraft.y,
        yaw: initialPoseDraft.yaw,
      });
      setInitialPoseDraft(undefined);
      initialPoseDeadlineRef.current = Date.now() + 60_000;
      initialPoseSentAtRef.current = Date.now();
      initialPoseConvergingRef.current = false;
      setInitialPoseWaitingId(initialPoseDraft.robotId);
      message.success(
        '已发送位置确认，机器人正在校验定位；该操作不会移动机器人。',
      );
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '发送位置确认失败。',
      );
    } finally {
      setLocalizationSaving(false);
    }
  }

  async function beginAutoLocalize() {
    const robot = autoLocalizeTarget.robot;
    if (!robot) return;
    autoLocalizeDeadlineRef.current = Date.now() + 60_000;
    autoLocalizeConvergingRef.current =
      robot.localizationStatus !== 'localized';
    setAutoLocalizingId(robot.id);
    try {
      await autoLocalize(robot.id);
      message.info(
        `已下发自动定位指令，${robot.name} 正在全图匹配位置，不会移动。`,
      );
    } catch (error) {
      setAutoLocalizingId(undefined);
      message.error(
        error instanceof Error ? error.message : '自动定位指令下发失败。',
      );
    }
  }

  return (
    <PageContainer
      title="地图建模"
      extra={[
        <Button
          key="refresh"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={() => void refreshMaps()}
        >
          刷新
        </Button>,
        <Button
          key="import"
          type="primary"
          icon={<CloudUploadOutlined />}
          onClick={() => setImportOpen(true)}
        >
          导入地图
        </Button>,
        <Button
          key="annotate"
          disabled={!selectedMap}
          icon={<PushpinOutlined />}
          type={annotationMode ? 'primary' : 'default'}
          onClick={() => setAnnotationMode((current) => !current)}
        >
          {annotationMode ? '标注中：点击地图' : '创建标注'}
        </Button>,
        annotationMode ? (
          <Tooltip
            key="annotate-robot-pose"
            title={
              poseAnnotationTarget.reason ??
              '以机器人当前位置创建标注，可在弹出窗口中调整。'
            }
          >
            <Button
              disabled={Boolean(poseAnnotationTarget.reason)}
              icon={<EnvironmentOutlined />}
              onClick={beginAnnotationFromRobot}
            >
              以车辆当前位置标注
            </Button>
          </Tooltip>
        ) : null,
        <Button
          key="sync-cruise"
          disabled={!selectedMap}
          icon={<AimOutlined />}
          loading={cruisePreviewLoading}
          onClick={() => void openCruiseSyncPreview()}
        >
          同步巡航点
        </Button>,
        <Tooltip
          key="localize"
          title={
            selectedMap && mapRobots.length === 0
              ? '当前地图没有机器人：请先在上方地图版本列表将机器人「设为当前地图」'
              : undefined
          }
        >
          <span>
            <Button
              disabled={!selectedMap || mapRobots.length === 0}
              icon={<RobotOutlined />}
              onClick={() => {
                setLocalizationRobotId(mapRobots[0]?.id);
                setLocalizationAnnotationId(undefined);
                setLocalizationSetupOpen(true);
              }}
            >
              网页确认位置
            </Button>
          </span>
        </Tooltip>,
        <Tooltip
          key="auto-localize"
          title={
            autoLocalizeTarget.reason ??
            (autoLocalizeTarget.robot
              ? `${autoLocalizeTarget.robot.name}（${autoLocalizeTarget.robot.id}）将执行全图激光匹配自动定位`
              : undefined)
          }
        >
          <span>
            <Popconfirm
              cancelText="取消"
              description="机器人将执行一次全图自动定位（不会移动），继续？"
              disabled={!autoLocalizeTarget.robot || Boolean(autoLocalizingId)}
              okText="继续"
              title="自动定位"
              onConfirm={() => void beginAutoLocalize()}
            >
              <Button
                disabled={
                  !autoLocalizeTarget.robot || Boolean(autoLocalizingId)
                }
                icon={<AimOutlined />}
                loading={Boolean(autoLocalizingId)}
              >
                {autoLocalizingId ? '定位中' : '自动定位'}
              </Button>
            </Popconfirm>
          </span>
        </Tooltip>,
      ]}
    >
      {localizationModeRobotId ? (
        <Alert
          showIcon
          type="warning"
          message="正在网页确认位置"
          description={`请在地图中按住 ${robots.find((item) => item.id === localizationModeRobotId)?.name ?? localizationModeRobotId} 的实际位置，并向机器人车头方向拖动；松开后确认坐标。此操作不会驱动车辆。`}
          action={
            <Button
              size="small"
              onClick={() => setLocalizationModeRobotId(undefined)}
            >
              取消
            </Button>
          }
          style={{ marginBottom: 16 }}
        />
      ) : null}
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={{ flex: '0 0 300px' }}>
          <Card
            title="车辆控制"
            size="small"
            extra={
              driveActive ? <Tag color="processing">手动控制中</Tag> : undefined
            }
          >
            {!driveRobot ? (
              <Typography.Text type="secondary">
                当前地图上没有机器人，切换机器人到此地图后可遥控。
              </Typography.Text>
            ) : (
              <>
                <Typography.Paragraph
                  type="secondary"
                  style={{ marginBottom: 12 }}
                >
                  按住按钮持续运动，松开自动缓停（低速 0.3
                  m/s，由车端限速与平滑器保护）。按钮与手柄可同时使用。
                </Typography.Paragraph>
                {driveRobotBusy ? (
                  <Alert
                    showIcon
                    type="info"
                    title={
                      driveRobot?.status === 'mapping'
                        ? '机器人正在建图，导航栈已停止，待结束建图后再遥控底盘。'
                        : '机械臂作业中，待完成后再遥控底盘。'
                    }
                    style={{ marginBottom: 12 }}
                  />
                ) : null}
                <div style={{ textAlign: 'center', userSelect: 'none' }}>
                  <div>
                    <Button
                      disabled={driveRobotBusy}
                      icon={<ArrowUpOutlined />}
                      type={driveActive === 'forward' ? 'primary' : 'default'}
                      onPointerDown={(event) => {
                        event.currentTarget.setPointerCapture(event.pointerId);
                        void startDrive('forward', DRIVE_LINEAR_SPEED, 0);
                      }}
                      onPointerUp={stopDrive}
                      onPointerCancel={stopDrive}
                      onPointerLeave={stopDrive}
                    />
                  </div>
                  <Space size={48} style={{ margin: '8px 0' }}>
                    <Button
                      disabled={driveRobotBusy}
                      icon={<ArrowLeftOutlined />}
                      type={driveActive === 'left' ? 'primary' : 'default'}
                      onPointerDown={(event) => {
                        event.currentTarget.setPointerCapture(event.pointerId);
                        void startDrive('left', 0, DRIVE_ANGULAR_SPEED);
                      }}
                      onPointerUp={stopDrive}
                      onPointerCancel={stopDrive}
                      onPointerLeave={stopDrive}
                    />
                    <Button danger onClick={stopDrive}>
                      停止
                    </Button>
                    <Button
                      disabled={driveRobotBusy}
                      icon={<ArrowRightOutlined />}
                      type={driveActive === 'right' ? 'primary' : 'default'}
                      onPointerDown={(event) => {
                        event.currentTarget.setPointerCapture(event.pointerId);
                        void startDrive('right', 0, -DRIVE_ANGULAR_SPEED);
                      }}
                      onPointerUp={stopDrive}
                      onPointerCancel={stopDrive}
                      onPointerLeave={stopDrive}
                    />
                  </Space>
                  <div>
                    <Button
                      disabled={driveRobotBusy}
                      icon={<ArrowDownOutlined />}
                      type={driveActive === 'backward' ? 'primary' : 'default'}
                      onPointerDown={(event) => {
                        event.currentTarget.setPointerCapture(event.pointerId);
                        void startDrive('backward', -DRIVE_LINEAR_SPEED, 0);
                      }}
                      onPointerUp={stopDrive}
                      onPointerCancel={stopDrive}
                      onPointerLeave={stopDrive}
                    />
                  </div>
                </div>
                <div
                  style={{
                    borderTop: '1px solid #f0f0f0',
                    marginTop: 12,
                    paddingTop: 12,
                    textAlign: 'center',
                  }}
                >
                  <Space size={8} wrap>
                    <Switch
                      checked={gamepadEnabled}
                      disabled={!gamepadId || driveRobotBusy}
                      onChange={setGamepadEnabled}
                    />
                    <span>手柄遥控</span>
                    {gamepadId ? (
                      <Tag color="success">已连接：{gamepadId}</Tag>
                    ) : (
                      <Tag>未检测到手柄</Tag>
                    )}
                  </Space>
                  {gamepadEnabled ? (
                    <Typography.Paragraph
                      type="secondary"
                      style={{ marginBottom: 0, marginTop: 8 }}
                    >
                      左摇杆前后控制线速度、左右控制角速度，回中自动停车；开启时按钮点动已禁用。
                    </Typography.Paragraph>
                  ) : null}
                </div>
              </>
            )}
          </Card>
          <Card
            title={`地图版本（${mapVersions.length}）`}
            size="small"
            style={{ marginTop: 16 }}
          >
            <List<MapVersion>
              dataSource={mapVersions}
              loading={loading}
              locale={{
                emptyText: '导入第一张地图后将在这里显示版本记录。',
              }}
              renderItem={(item) => {
                const mapInUse = robots.some(
                  (robot) => robot.mapId === item.id,
                );
                return (
                  <List.Item
                    style={{ cursor: 'pointer', paddingInline: 4 }}
                    onClick={() => setSelectedId(item.id)}
                  >
                    <List.Item.Meta
                      avatar={
                        <FileImageOutlined
                          style={{
                            color:
                              item.id === selectedMap?.id
                                ? '#1677ff'
                                : undefined,
                          }}
                        />
                      }
                      title={
                        <Typography.Text strong={item.id === selectedMap?.id}>
                          {item.name}
                        </Typography.Text>
                      }
                      description={`${item.version} · ${item.width} × ${item.height} px`}
                    />
                    <Tag
                      color={
                        item.status === 'published' ? 'success' : 'default'
                      }
                    >
                      {item.status === 'published' ? '发布' : '草稿'}
                    </Tag>
                    {mapInUse ? (
                      <Tag color="success">当前地图</Tag>
                    ) : (
                      <Tooltip
                        title={
                          mapSwitchRobotMapping
                            ? '机器人正在建图，结束并恢复导航后才能切换地图。'
                            : mapSwitchRobot
                              ? `立即切换 ${mapSwitchRobot.name}（${mapSwitchRobot.id}）到此地图`
                              : '当前没有可用的机器人。'
                        }
                      >
                        <Popconfirm
                          cancelText="取消"
                          description="将取消全部在途任务并需重新定位。"
                          disabled={!mapSwitchRobot || mapSwitchRobotMapping}
                          okText="确认切换"
                          title="设为当前地图？"
                          onConfirm={() => void switchToMap(item.id)}
                        >
                          <Button
                            disabled={!mapSwitchRobot || mapSwitchRobotMapping}
                            loading={mapSwitchingId === item.id}
                            size="small"
                            type="link"
                            onClick={(event) => {
                              event.stopPropagation();
                            }}
                          >
                            设为当前地图
                          </Button>
                        </Popconfirm>
                      </Tooltip>
                    )}
                    <Tooltip
                      title={
                        mapInUse
                          ? '该地图正在使用：机器人当前活动地图不能删除。'
                          : undefined
                      }
                    >
                      <Popconfirm
                        cancelText="取消"
                        description={`删除后不可恢复：地图「${item.name}」及其全部标注将被永久删除。`}
                        disabled={mapInUse}
                        okButtonProps={{ danger: true }}
                        okText="确认删除"
                        title="删除地图"
                        onConfirm={() => void removeMap(item.id)}
                      >
                        <Button
                          danger
                          disabled={mapInUse}
                          loading={mapDeletingId === item.id}
                          size="small"
                          type="link"
                          onClick={(event) => event.stopPropagation()}
                        >
                          删除
                        </Button>
                      </Popconfirm>
                    </Tooltip>
                  </List.Item>
                );
              }}
            />
          </Card>
          <Card
            title={`实时机器人（${mapRobots.length}）`}
            extra={
              <Tag color={realtimeState.connected ? 'success' : 'default'}>
                {realtimeState.connected ? '实时已连接' : '轮询兜底'}
              </Tag>
            }
            size="small"
            style={{ marginTop: 16 }}
          >
            <RobotPositionList robots={mapRobots} />
          </Card>
          <Card
            title={`全部标注（${annotations.length}）`}
            size="small"
            style={{ marginTop: 16 }}
            extra={
              <Tooltip
                title={
                  poseAnnotationTarget.reason ??
                  '以机器人当前位置创建标注，可在弹出窗口中调整。'
                }
              >
                <span>
                  <Button
                    disabled={Boolean(poseAnnotationTarget.reason)}
                    icon={<RobotOutlined />}
                    size="small"
                    onClick={beginAnnotationFromRobot}
                  >
                    以车辆当前位置标注
                  </Button>
                </span>
              </Tooltip>
            }
          >
            <AnnotationList
              annotations={annotations}
              navigateTarget={navigateTarget}
              navigatingId={annotationNavigatingId}
              selectedAnnotationId={selectedAnnotation?.id}
              onEdit={beginCoordinateEdit}
              onNavigate={(annotation) => void navigateToAnnotation(annotation)}
              onSelect={openAnnotationDetails}
            />
          </Card>
        </Col>
        <Col xs={24} xl={{ flex: '1 1 0' }} style={{ minWidth: 0 }}>
          <Collapse
            defaultActiveKey={['properties']}
            items={[
              {
                key: 'properties',
                label: '地图属性',
                children: selectedMap ? (
                  <MapProperties
                    map={selectedMap}
                    annotationCount={annotations.length}
                  />
                ) : (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="尚未导入地图"
                  />
                ),
              },
            ]}
          />
          {navFlowTask ? (
            <NavFlowProgress
              onClose={() => setNavFlowTask(undefined)}
              onStop={
                ['queued', 'running', 'paused'].includes(navFlowTask.status)
                  ? () => void stopAnnotationNavigation()
                  : undefined
              }
              stages={navFlowStages}
              stopping={navFlowStopping}
              task={navFlowTask}
            />
          ) : null}
          <Card
            style={{ marginTop: 16 }}
            title={
              selectedMap
                ? `${selectedMap.name} · ${selectedMap.version}`
                : '地图预览'
            }
            extra={
              selectedMap ? (
                <Space size={8}>
                  <Tooltip
                    title={
                      locOverlayRobot
                        ? '叠加显示车端实时定位的激光点云（每 2 秒刷新）'
                        : '当前地图没有在线机器人，无法读取雷达点云。'
                    }
                  >
                    <span>
                      <Switch
                        checked={locOverlayEnabled}
                        disabled={!locOverlayRobot}
                        onChange={setLocOverlayEnabled}
                      />
                    </span>
                  </Tooltip>
                  <span>雷达点云</span>
                  {locOverlayEnabled && locOverlayCapturedAt ? (
                    <Typography.Text type="secondary">
                      采集于 {locOverlayCapturedAt}
                    </Typography.Text>
                  ) : null}
                  <Tag
                    color={
                      selectedMap.status === 'published'
                        ? 'success'
                        : 'processing'
                    }
                  >
                    {selectedMap.status === 'published' ? '已发布' : '草稿'}
                  </Tag>
                </Space>
              ) : undefined
            }
          >
            {selectedMap && mapRobots.length === 0 && robotCurrentMapId ? (
              <Alert
                showIcon
                type="warning"
                message="机器人当前不在这张地图上"
                description={`${robots
                  .filter((robot) => robot.mapId)
                  .map((robot) => `${robot.name} 当前在「${robot.mapVersion}」`)
                  .join(
                    '、',
                  )}。要在此地图查看机器人实时位置，请先将机器人切换到此地图（换图后需重新定位）。`}
                action={
                  <Space size={8} wrap>
                    {mapVersions.some(
                      (item) => item.id === robotCurrentMapId,
                    ) ? (
                      <Button
                        size="small"
                        onClick={() => setSelectedId(robotCurrentMapId)}
                      >
                        查看机器人当前地图
                      </Button>
                    ) : null}
                    <Popconfirm
                      cancelText="取消"
                      description="将取消全部在途任务并需重新定位。"
                      disabled={!mapSwitchRobot || mapSwitchRobotMapping}
                      okText="确认切换"
                      title="切换到此地图？"
                      onConfirm={() => void switchToMap(selectedMap.id)}
                    >
                      <Button
                        size="small"
                        type="primary"
                        disabled={!mapSwitchRobot || mapSwitchRobotMapping}
                        loading={mapSwitchingId === selectedMap.id}
                      >
                        切换到此地图
                      </Button>
                    </Popconfirm>
                  </Space>
                }
                style={{ marginBottom: 16 }}
              />
            ) : null}
            <MapPreview
              map={selectedMap}
              previewUrl={previewUrl}
              annotations={annotations}
              robots={robots}
              annotationMode={annotationMode}
              localizationMode={Boolean(localizationModeRobotId)}
              loading={previewLoading || loading}
              locOverlayPoints={
                locOverlayEnabled ? locOverlay?.points : undefined
              }
              onAnnotationClick={openAnnotationDetails}
              onAnnotationMove={confirmAnnotationMove}
              onMapClick={beginAnnotation}
              onInitialPosePick={selectInitialPose}
            />
          </Card>
        </Col>
      </Row>
      <Modal
        destroyOnClose
        open={importOpen}
        title="导入 ROS 地图"
        okText="开始导入"
        cancelText="取消"
        confirmLoading={importing}
        onCancel={() => setImportOpen(false)}
        onOk={() => void submitImport()}
      >
        <Typography.Paragraph type="secondary">
          将 YAML 与 PGM 成对上传。YAML 中的{' '}
          <Typography.Text code>image</Typography.Text> 必须引用当前上传的 PGM
          文件名。
        </Typography.Paragraph>
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Input
            value={mapName}
            maxLength={120}
            placeholder="地图名称，例如：一层 302 实验室"
            onChange={(event) => setMapName(event.target.value)}
          />
          <Input
            value={mapImportId}
            maxLength={90}
            placeholder="地图 ID（可选，MAP- 开头，留空自动生成）"
            onChange={(event) => setMapImportId(event.target.value)}
          />
          <MapFilePicker
            accept=".yaml,.yml"
            file={files.yaml}
            icon={<FileTextOutlined />}
            label="1. YAML 位置与分辨率文件"
            onChange={(file) =>
              setFiles((current) => ({ ...current, yaml: file }))
            }
          />
          <MapFilePicker
            accept=".pgm"
            file={files.pgm}
            icon={<FileImageOutlined />}
            label="2. PGM 占据栅格图"
            onChange={(file) =>
              setFiles((current) => ({ ...current, pgm: file }))
            }
          />
        </Space>
      </Modal>
      <Modal
        destroyOnClose
        open={annotationOpen}
        title="创建地图标注"
        okText="保存标注"
        cancelText="取消"
        confirmLoading={annotationSaving}
        onCancel={() => setAnnotationOpen(false)}
        onOk={() => void submitAnnotation()}
      >
        {annotationSourceLabel ? (
          <Alert
            showIcon
            type="info"
            message={`来源：${annotationSourceLabel}`}
            style={{ marginBottom: 16 }}
          />
        ) : null}
        <Typography.Paragraph type="secondary">
          {pendingPixel && selectedMap
            ? `已选择图像像素 (${pendingPixel.x}, ${pendingPixel.y})。保存后会自动换算为 ${selectedMap.resolution.toFixed(3)} m 分辨率下的 ROS map 坐标。`
            : '请先在地图上选择位置。'}
        </Typography.Paragraph>
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <div>
            <Typography.Paragraph strong style={{ marginBottom: 8 }}>
              标注类型
            </Typography.Paragraph>
            <Select<MapAnnotationKind>
              style={{ width: '100%' }}
              value={annotationKind}
              options={Object.entries(annotationMeta)
                .filter(([value]) => value !== 'cruise')
                .map(([value, meta]) => ({
                  label: meta.label,
                  value,
                }))}
              onChange={(value) => {
                setAnnotationKind(value);
                setAnnotationLabel(nextAnnotationLabel(value, annotations));
              }}
            />
          </div>
          <div>
            <Typography.Paragraph strong style={{ marginBottom: 8 }}>
              名称
            </Typography.Paragraph>
            <Input
              value={annotationLabel}
              maxLength={120}
              placeholder="标注名称，例如：A-03 书架"
              onChange={(event) => setAnnotationLabel(event.target.value)}
            />
          </div>
          <Row gutter={16}>
            <Col span={12}>
              <Typography.Paragraph strong style={{ marginBottom: 8 }}>
                X（米）
              </Typography.Paragraph>
              <Typography.Text code>
                {pendingPixel && selectedMap
                  ? (
                      selectedMap.origin.x +
                      pendingPixel.x * selectedMap.resolution
                    ).toFixed(2)
                  : '-'}
              </Typography.Text>
            </Col>
            <Col span={12}>
              <Typography.Paragraph strong style={{ marginBottom: 8 }}>
                Y（米）
              </Typography.Paragraph>
              <Typography.Text code>
                {pendingPixel && selectedMap
                  ? (
                      selectedMap.origin.y +
                      (selectedMap.height - 1 - pendingPixel.y) *
                        selectedMap.resolution
                    ).toFixed(2)
                  : '-'}
              </Typography.Text>
            </Col>
          </Row>
          <div>
            <Typography.Paragraph strong style={{ marginBottom: 8 }}>
              朝向
            </Typography.Paragraph>
            <YawDegreesField
              value={annotationYaw}
              onChange={setAnnotationYaw}
            />
          </div>
        </Space>
      </Modal>
      <Modal
        open={localizationSetupOpen}
        title="网页确认机器人位置"
        okText={localizationAnnotationId ? '使用所选点' : '开始在地图上选择'}
        cancelText="取消"
        onCancel={() => setLocalizationSetupOpen(false)}
        onOk={beginWebLocalization}
      >
        <Alert
          showIcon
          type="warning"
          message="仅在能看到机器人实际位置时使用"
          description="此操作只为 AMCL 提供初始位置，不会让机器人移动。请确认当前选择的地图与机器人加载的地图相同。"
          style={{ marginBottom: 16 }}
        />
        <Select
          style={{ width: '100%' }}
          value={localizationRobotId}
          placeholder="选择机器人"
          options={mapRobots.map((robot) => ({
            value: robot.id,
            label: `${robot.name}（${robot.id}）`,
          }))}
          onChange={setLocalizationRobotId}
        />
        <Typography.Paragraph strong style={{ marginTop: 16, marginBottom: 8 }}>
          初始位置
        </Typography.Paragraph>
        <Select
          style={{ width: '100%' }}
          value={localizationAnnotationId ?? 'manual'}
          options={[
            { value: 'manual', label: '手动在地图上选择位置和朝向' },
            ...initialPoseAnnotations.map((annotation) => ({
              value: annotation.id,
              label: `${annotationMeta[annotation.kind].label} · ${annotation.label}（${annotation.mapX.toFixed(2)}, ${annotation.mapY.toFixed(2)}）`,
            })),
          ]}
          onChange={(value) =>
            setLocalizationAnnotationId(value === 'manual' ? undefined : value)
          }
        />
        <Typography.Paragraph
          type="secondary"
          style={{ marginTop: 16, marginBottom: 0 }}
        >
          {localizationAnnotationId
            ? '将使用该标注保存的坐标和朝向作为 AMCL 初始位置；请确认机器人实际停在该点附近。'
            : '下一步请在地图上从机器人的实际位置向车头方向拖动，系统会换算为 ROS 坐标和朝向。'}
        </Typography.Paragraph>
      </Modal>
      <Modal
        open={Boolean(initialPoseDraft)}
        title="确认机器人初始位置"
        okText="确认并发送"
        cancelText="重新选择"
        confirmLoading={localizationSaving}
        onCancel={() => setInitialPoseDraft(undefined)}
        onOk={() => void submitInitialPose()}
      >
        {initialPoseDraft ? (
          <>
            <Alert
              showIcon
              type="info"
              message="将发送给机器人进行定位校验"
              description="位置确认不会驱动车辆。机器人会用激光扫描校验该位置与地图是否匹配；匹配不上会被拒绝并提示重新选择，校验通过后状态才会切换为“实时定位”并允许导航。"
              style={{ marginBottom: 16 }}
            />
            {previewUrl && selectedMap ? (
              <InitialPosePreview
                map={selectedMap}
                previewUrl={previewUrl}
                x={initialPoseDraft.x}
                y={initialPoseDraft.y}
                yaw={initialPoseDraft.yaw}
              />
            ) : null}
            <Descriptions column={1} size="small">
              <Descriptions.Item label="机器人">
                {robots.find((item) => item.id === initialPoseDraft.robotId)
                  ?.name ?? initialPoseDraft.robotId}
              </Descriptions.Item>
              <Descriptions.Item label="位置来源">
                {initialPoseDraft.sourceLabel}
              </Descriptions.Item>
              <Descriptions.Item label="ROS 坐标">
                ({initialPoseDraft.x.toFixed(3)},{' '}
                {initialPoseDraft.y.toFixed(3)}) m
              </Descriptions.Item>
              <Descriptions.Item label="车头朝向">
                {((initialPoseDraft.yaw * 180) / Math.PI).toFixed(0)}°
              </Descriptions.Item>
            </Descriptions>
          </>
        ) : null}
      </Modal>
      <Modal
        open={Boolean(selectedAnnotation)}
        title={
          selectedAnnotation
            ? `${annotationMeta[selectedAnnotation.kind].label}详情`
            : '标注详情'
        }
        onCancel={() => setSelectedAnnotation(undefined)}
        footer={
          selectedAnnotation
            ? [
                <Button
                  key="close"
                  onClick={() => setSelectedAnnotation(undefined)}
                >
                  关闭
                </Button>,
                <Popconfirm
                  key="delete"
                  cancelText="取消"
                  description="删除后不可恢复；历史任务记录不受影响，仅进行中的任务会阻止删除。"
                  okButtonProps={{ danger: true }}
                  okText="确认删除"
                  title="删除标注"
                  onConfirm={() => void removeAnnotation()}
                >
                  <Button danger loading={annotationDeleting}>
                    删除
                  </Button>
                </Popconfirm>,
                <Button
                  disabled={!annotationEditLabel.trim()}
                  key="save"
                  loading={annotationRenaming}
                  type="primary"
                  onClick={() => void saveAnnotationName()}
                >
                  保存名称
                </Button>,
              ]
            : null
        }
      >
        {selectedAnnotation ? (
          <Descriptions column={1} size="small">
            <Descriptions.Item label="名称">
              <Input
                maxLength={120}
                value={annotationEditLabel}
                onChange={(event) => setAnnotationEditLabel(event.target.value)}
              />
            </Descriptions.Item>
            <Descriptions.Item label="图像像素">
              ({selectedAnnotation.pixelX}, {selectedAnnotation.pixelY})
            </Descriptions.Item>
            <Descriptions.Item label="ROS 坐标">
              ({selectedAnnotation.mapX.toFixed(3)},{' '}
              {selectedAnnotation.mapY.toFixed(3)}) m
            </Descriptions.Item>
            <Descriptions.Item label="朝向">
              {((selectedAnnotation.yaw * 180) / Math.PI).toFixed(0)}°
            </Descriptions.Item>
            <Descriptions.Item label="创建人">
              {selectedAnnotation.createdBy}
            </Descriptions.Item>
          </Descriptions>
        ) : null}
      </Modal>
      <Modal
        destroyOnClose
        open={Boolean(coordinateEdit)}
        title="编辑标注"
        okText="保存"
        cancelText="取消"
        confirmLoading={coordinateEditSaving}
        onCancel={() => setCoordinateEdit(undefined)}
        onOk={() => void saveCoordinateEdit()}
      >
        {coordinateEdit && selectedMap ? (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <div>
              <Typography.Paragraph strong style={{ marginBottom: 8 }}>
                标注名称
              </Typography.Paragraph>
              <Input
                maxLength={120}
                value={coordinateEditLabel}
                onChange={(event) => setCoordinateEditLabel(event.target.value)}
              />
            </div>
            <div>
              <Typography.Paragraph strong style={{ marginBottom: 8 }}>
                X 坐标（{selectedMap.origin.x.toFixed(2)} ~{' '}
                {(
                  selectedMap.origin.x +
                  (selectedMap.width - 1) * selectedMap.resolution
                ).toFixed(2)}{' '}
                m）
              </Typography.Paragraph>
              <InputNumber
                addonAfter="m"
                max={
                  selectedMap.origin.x +
                  (selectedMap.width - 1) * selectedMap.resolution
                }
                min={selectedMap.origin.x}
                step={0.01}
                style={{ width: '100%' }}
                value={coordinateEditX}
                onChange={(value) =>
                  setCoordinateEditX(
                    typeof value === 'number' ? value : selectedMap.origin.x,
                  )
                }
              />
            </div>
            <div>
              <Typography.Paragraph strong style={{ marginBottom: 8 }}>
                Y 坐标（{selectedMap.origin.y.toFixed(2)} ~{' '}
                {(
                  selectedMap.origin.y +
                  (selectedMap.height - 1) * selectedMap.resolution
                ).toFixed(2)}{' '}
                m）
              </Typography.Paragraph>
              <InputNumber
                addonAfter="m"
                max={
                  selectedMap.origin.y +
                  (selectedMap.height - 1) * selectedMap.resolution
                }
                min={selectedMap.origin.y}
                step={0.01}
                style={{ width: '100%' }}
                value={coordinateEditY}
                onChange={(value) =>
                  setCoordinateEditY(
                    typeof value === 'number' ? value : selectedMap.origin.y,
                  )
                }
              />
            </div>
            <div>
              <Typography.Paragraph strong style={{ marginBottom: 8 }}>
                朝向
              </Typography.Paragraph>
              <YawDegreesField
                value={coordinateEditYaw}
                onChange={setCoordinateEditYaw}
              />
            </div>
            <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
              保存后系统会将米制坐标按地图分辨率换算回图像像素，并同步更新地图上的标注位置；也可以直接在地图上拖动标注点改位。
            </Typography.Paragraph>
          </Space>
        ) : null}
      </Modal>
      <Modal
        destroyOnClose
        open={Boolean(cruisePreview)}
        title="同步巡航点预览"
        okText="确认同步"
        cancelText="取消"
        confirmLoading={syncingCruise}
        onCancel={() => setCruisePreview(undefined)}
        onOk={() => void executeCruiseSync()}
      >
        {cruisePreview ? (
          <>
            <Typography.Paragraph type="secondary">
              巡航点来源文件：
              <Typography.Text code>{cruisePreview.file}</Typography.Text>
              。确认后将按下列差异把「新增」的点预标到当前地图；「跳过」的点不会改动。
            </Typography.Paragraph>
            <List
              dataSource={cruisePreview.items}
              locale={{
                emptyText: '配置文件中没有可同步的巡航点（或文件未部署）。',
              }}
              rowKey="name"
              size="small"
              style={{ maxHeight: 320, overflowY: 'auto' }}
              renderItem={(item) => (
                <List.Item
                  actions={[
                    item.action === 'create' ? (
                      <Tag key="action" color="success">
                        新增
                      </Tag>
                    ) : (
                      <Tag key="action">跳过</Tag>
                    ),
                  ]}
                >
                  <List.Item.Meta
                    title={item.name}
                    description={`(${item.x.toFixed(3)}, ${item.y.toFixed(3)}) m，朝向 ${radiansToDegrees(item.yaw).toFixed(0)}°${
                      item.reason ? ` · ${item.reason}` : ''
                    }`}
                  />
                </List.Item>
              )}
            />
          </>
        ) : null}
      </Modal>
    </PageContainer>
  );
}

function AnnotationList({
  annotations,
  navigateTarget,
  navigatingId,
  selectedAnnotationId,
  onEdit,
  onNavigate,
  onSelect,
}: {
  annotations: MapAnnotation[];
  navigateTarget: AnnotationNavigateTarget;
  navigatingId?: string;
  selectedAnnotationId?: string;
  onEdit: (annotation: MapAnnotation) => void;
  onNavigate: (annotation: MapAnnotation) => void;
  onSelect: (annotation: MapAnnotation) => void;
}) {
  const [keyword, setKeyword] = useState('');
  const [kindFilter, setKindFilter] = useState<AnnotationKindFilter>('all');
  const visible = useMemo(
    () =>
      filterAnnotationsByKeyword(
        filterAnnotationsByKind(annotations, kindFilter),
        keyword,
      ),
    [annotations, kindFilter, keyword],
  );
  return (
    <>
      <Space.Compact style={{ width: '100%', marginBottom: 8 }}>
        <Input
          allowClear
          placeholder="搜索标注名称"
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
        />
        <Select<AnnotationKindFilter>
          style={{ width: 104 }}
          value={kindFilter}
          options={[
            { value: 'all', label: '全部类型' },
            ...Object.entries(ANNOTATION_KIND_LABELS).map(([value, label]) => ({
              value: value as MapAnnotationKind,
              label,
            })),
          ]}
          onChange={setKindFilter}
        />
      </Space.Compact>
      <List<MapAnnotation>
        dataSource={visible}
        locale={{
          emptyText:
            annotations.length === 0
              ? '当前地图还没有标注。'
              : '没有匹配搜索条件的标注。',
        }}
        rowKey="id"
        size="small"
        style={{ maxHeight: 260, overflowY: 'auto' }}
        renderItem={(annotation) => {
          const meta = annotationMeta[annotation.kind];
          const selected = annotation.id === selectedAnnotationId;
          return (
            <List.Item
              style={{
                background: selected ? '#e6f4ff' : undefined,
                borderRadius: 6,
                cursor: 'pointer',
                paddingInline: 8,
              }}
              onClick={() => onSelect(annotation)}
            >
              <List.Item.Meta
                avatar={
                  <span
                    style={{
                      alignItems: 'center',
                      background: meta.color,
                      borderRadius: '50%',
                      color: '#fff',
                      display: 'inline-flex',
                      height: 26,
                      justifyContent: 'center',
                      width: 26,
                    }}
                  >
                    {meta.icon}
                  </span>
                }
                title={
                  <Space size={6}>
                    <Typography.Text
                      ellipsis={{ tooltip: annotation.label }}
                      strong={selected}
                    >
                      {annotation.label}
                    </Typography.Text>
                    <Tag color={meta.color}>{meta.label}</Tag>
                  </Space>
                }
                description={`(${annotation.mapX.toFixed(2)}, ${annotation.mapY.toFixed(2)}) m · ${radiansToDegrees(annotation.yaw).toFixed(1)}°`}
              />
              <Tooltip
                title={
                  navigateTarget.reason ??
                  `导航到「${annotation.label}」（${navigateTarget.robot?.name ?? ''}）`
                }
              >
                <span>
                  <Button
                    disabled={!navigateTarget.robot}
                    icon={<SendOutlined />}
                    loading={navigatingId === annotation.id}
                    size="small"
                    type="link"
                    onClick={(event) => {
                      // The stopPropagation keeps the row's onSelect quiet, but
                      // it also swallows the synthetic click a Popconfirm
                      // trigger relies on — drive the confirm explicitly.
                      event.stopPropagation();
                      if (!navigateTarget.robot) return;
                      Modal.confirm({
                        title: '导航到此点',
                        content: `机器人将导航到「${annotation.label}」，确认？`,
                        okText: '确认',
                        cancelText: '取消',
                        onOk: () => onNavigate(annotation),
                      });
                    }}
                  >
                    导航
                  </Button>
                </span>
              </Tooltip>
              <Button
                size="small"
                type="link"
                onClick={(event) => {
                  event.stopPropagation();
                  onEdit(annotation);
                }}
              >
                编辑
              </Button>
            </List.Item>
          );
        }}
      />
    </>
  );
}

function RobotPositionList({ robots }: { robots: RobotSummary[] }) {
  const now = Date.now();
  return (
    <List<RobotSummary>
      dataSource={robots}
      locale={{ emptyText: '当前地图暂无已接入的机器人。' }}
      rowKey="id"
      size="small"
      style={{ maxHeight: 260, overflowY: 'auto' }}
      renderItem={(robot) => {
        const pose = robot.pose;
        const localization =
          localizationMeta[robot.localizationStatus ?? 'unknown'];
        const capturedAt = pose ? new Date(pose.capturedAt) : undefined;
        const invalidTime = !capturedAt || Number.isNaN(capturedAt.getTime());
        const expired = invalidTime || now - capturedAt.getTime() > 10_000;
        const yawDegrees = pose ? (pose.yaw * 180) / Math.PI : undefined;
        const displayStatus =
          pose && expired
            ? { color: 'default', label: '位置过期' }
            : localization;
        const timeLabel = invalidTime
          ? '未获得定位'
          : capturedAt.toLocaleTimeString('zh-CN', { hour12: false });
        return (
          <List.Item style={{ paddingInline: 8 }}>
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                gap: 8,
                width: '100%',
              }}
            >
              {robot.health.imu === false ? (
                <Alert showIcon type="error" title={IMU_DISCONNECTED_ALERT} />
              ) : null}
              <List.Item.Meta
                avatar={
                  <span
                    style={{
                      alignItems: 'center',
                      background:
                        localization.color === 'success' && !expired
                          ? '#f5222d'
                          : '#8c8c8c',
                      borderRadius: '50%',
                      color: '#fff',
                      display: 'inline-flex',
                      height: 28,
                      justifyContent: 'center',
                      width: 28,
                    }}
                  >
                    <RobotOutlined />
                  </span>
                }
                title={
                  <Space size={6} wrap>
                    <Typography.Text strong>{robot.name}</Typography.Text>
                    <Typography.Text type="secondary">
                      {robot.id}
                    </Typography.Text>
                    <Tag color={displayStatus.color}>{displayStatus.label}</Tag>
                  </Space>
                }
                description={
                  pose ? (
                    <Space direction="vertical" size={0}>
                      <Typography.Text type="secondary">
                        坐标：({pose.x.toFixed(3)}, {pose.y.toFixed(3)}) m
                      </Typography.Text>
                      <Typography.Text type="secondary">
                        朝向：{yawDegrees?.toFixed(1)}° · 采集：{timeLabel}
                      </Typography.Text>
                    </Space>
                  ) : (
                    <Typography.Text type="secondary">
                      {localization.description}
                    </Typography.Text>
                  )
                }
              />
            </div>
          </List.Item>
        );
      }}
    />
  );
}

const localizationMeta: Record<
  NonNullable<RobotSummary['localizationStatus']>,
  { color: string; label: string; description: string }
> = {
  starting: {
    color: 'default',
    label: '定位启动中',
    description: '正在等待 AMCL 定位服务。',
  },
  restoring_saved_pose: {
    color: 'processing',
    label: '恢复上次位置',
    description: '正在用上次稳定位置恢复定位。',
  },
  global_localizing: {
    color: 'processing',
    label: '自动搜索位置',
    description: '正在用激光与地图自动匹配位置。',
  },
  manual_initializing: {
    color: 'processing',
    label: '确认位置中',
    description: '正在验证人工确认的位置。',
  },
  localized: {
    color: 'success',
    label: '实时定位',
    description: '已完成可靠定位。',
  },
  manual_required: {
    color: 'warning',
    label: '需要确认位置',
    description: '自动定位未收敛，请在本页使用“网页确认位置”核对实际位置。',
  },
  unknown: {
    color: 'default',
    label: '定位状态未知',
    description: '等待机器人上报定位状态。',
  },
};

const NAV_FLOW_STATUS: Record<
  DispatchTask['status'],
  { label: string; color: string }
> = {
  queued: { label: '排队中', color: 'default' },
  running: { label: '执行中', color: 'processing' },
  paused: { label: '已暂停', color: 'warning' },
  failed: { label: '失败', color: 'error' },
  completed: { label: '已完成', color: 'success' },
  cancelled: { label: '已取消', color: 'default' },
  recovery_required: { label: '待人工确认', color: 'volcano' },
};

function NavFlowStageTag({
  label,
  stage,
}: {
  label: string;
  stage?: DispatchTask;
}) {
  if (!stage) return <Tag>{label}：待下发</Tag>;
  const status = NAV_FLOW_STATUS[stage.status];
  return (
    <Tag color={status.color}>
      {label} {stage.id.replace('TSK-', '')}：{status.label}
    </Tag>
  );
}

function NavFlowProgress({
  onClose,
  onStop,
  stages,
  stopping,
  task,
}: {
  onClose: () => void;
  onStop?: () => void;
  stages: { navigation?: DispatchTask; fineTune?: DispatchTask };
  stopping?: boolean;
  task: DispatchTask;
}) {
  const status = NAV_FLOW_STATUS[task.status];
  return (
    <Card
      extra={
        <Space size={4}>
          {onStop ? (
            <Button danger loading={stopping} size="small" onClick={onStop}>
              停止导航
            </Button>
          ) : null}
          <Button size="small" type="text" onClick={onClose}>
            关闭
          </Button>
        </Space>
      }
      size="small"
      style={{ marginBottom: 16 }}
      title="标注导航进度"
    >
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        <Space size={8} wrap>
          <Tag color={status.color}>{status.label}</Tag>
          <Typography.Text strong>{task.target}</Typography.Text>
          <Typography.Text type="secondary">
            {task.workflowMessage ?? '等待状态更新'}
          </Typography.Text>
        </Space>
        <Progress percent={task.progress} size="small" />
        {task.status === 'failed' && task.failureReason ? (
          <Typography.Text type="danger">{task.failureReason}</Typography.Text>
        ) : null}
        {stages.navigation?.status === 'failed' &&
        stages.navigation.failureReason ? (
          <Typography.Text type="danger">
            导航阶段：{stages.navigation.failureReason}
          </Typography.Text>
        ) : null}
        {stages.fineTune?.status === 'failed' &&
        stages.fineTune.failureReason ? (
          <Typography.Text type="danger">
            精调阶段：{stages.fineTune.failureReason}
          </Typography.Text>
        ) : null}
        <Space size={8} wrap>
          <Typography.Text type="secondary">执行指令：</Typography.Text>
          <NavFlowStageTag label="导航" stage={stages.navigation} />
          <NavFlowStageTag label="低速精调" stage={stages.fineTune} />
        </Space>
      </Space>
    </Card>
  );
}

function MapPreview({
  map,
  previewUrl,
  annotations,
  robots,
  annotationMode,
  localizationMode,
  loading,
  locOverlayPoints,
  onAnnotationClick,
  onAnnotationMove,
  onMapClick,
  onInitialPosePick,
}: {
  map?: MapVersion;
  previewUrl?: string;
  annotations: MapAnnotation[];
  robots: RobotSummary[];
  annotationMode: boolean;
  localizationMode: boolean;
  loading: boolean;
  locOverlayPoints?: [number, number][];
  onAnnotationClick: (annotation: MapAnnotation) => void;
  onAnnotationMove: (annotation: MapAnnotation, pixel: PendingPixel) => void;
  onMapClick: (pixel: PendingPixel) => void;
  onInitialPosePick: (pixel: PendingPixel, yaw: number) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const layerRef = useRef<HTMLDivElement>(null);
  const [transform, setTransform] = useState<MapViewTransform>(
    IDENTITY_MAP_TRANSFORM,
  );
  const [panning, setPanning] = useState(false);
  // Live press→drag feedback while picking an initial pose in localization
  // mode; without it the heading gesture was invisible until release.
  const [posePickDrag, setPosePickDrag] = useState<{
    start: PendingPixel;
    current: PendingPixel;
  }>();
  const panRef = useRef<
    | {
        pointerId: number;
        lastX: number;
        lastY: number;
        moved: boolean;
      }
    | undefined
  >(undefined);

  const mapId = map?.id;
  useEffect(() => {
    setTransform(IDENTITY_MAP_TRANSFORM);
  }, [mapId]);

  // The overlay layers (point cloud canvas, annotation/robot markers, drag
  // SVG, click coordinates) all span the full layer box, so the box must
  // share the map's aspect exactly. A pure-CSS aspect-ratio box breaks when
  // maxWidth clamps wide maps (the img then letterboxes while overlays still
  // fill the taller box — the "point cloud vertically stretched" mismatch).
  // Measure the container and size the layer in pixels instead.
  const [layerSize, setLayerSize] = useState<
    { width: number; height: number } | undefined
  >(undefined);
  const mapPixelWidth = map?.width;
  const mapPixelHeight = map?.height;
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !mapPixelWidth || !mapPixelHeight) {
      setLayerSize(undefined);
      return;
    }
    const compute = () => {
      const rect = container.getBoundingClientRect();
      // The container pads the layer by 16 px on every side.
      const availableW = Math.max(rect.width - 32, 50);
      const availableH = Math.max(rect.height - 32, 50);
      const scale = Math.min(
        availableW / mapPixelWidth,
        availableH / mapPixelHeight,
      );
      setLayerSize({
        width: Math.max(1, Math.round(mapPixelWidth * scale)),
        height: Math.max(1, Math.round(mapPixelHeight * scale)),
      });
    };
    compute();
    const observer = new ResizeObserver(compute);
    observer.observe(container);
    return () => observer.disconnect();
  }, [mapPixelWidth, mapPixelHeight]);

  // Ctrl + wheel zooms around the cursor. A native listener is required
  // because React wheel handlers are passive and cannot preventDefault().
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      const layer = layerRef.current;
      if (!layer) return;
      const rect = layer.getBoundingClientRect();
      const factor = Math.exp(-event.deltaY * 0.002);
      setTransform((current) =>
        zoomMapAt(current, current.scale * factor, {
          x: event.clientX - (rect.left - current.x),
          y: event.clientY - (rect.top - current.y),
        }),
      );
    };
    container.addEventListener('wheel', onWheel, { passive: false });
    return () => container.removeEventListener('wheel', onWheel);
  }, []);

  function zoomFromToolbar(factor: number) {
    const container = containerRef.current;
    const layer = layerRef.current;
    if (!container || !layer) return;
    const containerRect = container.getBoundingClientRect();
    const layerRect = layer.getBoundingClientRect();
    setTransform((current) =>
      zoomMapAt(current, current.scale * factor, {
        x:
          containerRect.left +
          containerRect.width / 2 -
          (layerRect.left - current.x),
        y:
          containerRect.top +
          containerRect.height / 2 -
          (layerRect.top - current.y),
      }),
    );
  }

  function handlePanPointerDown(event: React.PointerEvent<HTMLDivElement>) {
    if (annotationMode || localizationMode) return;
    if (transform.scale <= MIN_MAP_SCALE || event.button !== 0) return;
    panRef.current = {
      pointerId: event.pointerId,
      lastX: event.clientX,
      lastY: event.clientY,
      moved: false,
    };
  }

  function handlePanPointerMove(event: React.PointerEvent<HTMLDivElement>) {
    const pan = panRef.current;
    if (!pan || pan.pointerId !== event.pointerId) return;
    if (!pan.moved) {
      if (Math.hypot(event.clientX - pan.lastX, event.clientY - pan.lastY) < 4)
        return;
      pan.moved = true;
      setPanning(true);
      // Capture only once the drag threshold passed so plain clicks on
      // annotation markers keep working while zoomed in.
      event.currentTarget.setPointerCapture(event.pointerId);
    }
    setTransform((current) =>
      panMapBy(current, {
        x: event.clientX - pan.lastX,
        y: event.clientY - pan.lastY,
      }),
    );
    pan.lastX = event.clientX;
    pan.lastY = event.clientY;
  }

  function handlePanPointerUp(event: React.PointerEvent<HTMLDivElement>) {
    const pan = panRef.current;
    if (!pan || pan.pointerId !== event.pointerId) return;
    panRef.current = undefined;
    if (pan.moved) setPanning(false);
  }

  if (!map && !loading) {
    return (
      <Empty
        description="还没有真实地图，点击右上角“导入地图”开始。"
        style={{ height: 500, paddingTop: 180 }}
      />
    );
  }
  return (
    <Spin spinning={loading} tip="正在生成地图预览">
      <div
        ref={containerRef}
        style={{
          alignItems: 'center',
          background: '#e8edf3',
          border: '1px solid #d9e0e8',
          borderRadius: 8,
          display: 'flex',
          // Keep the map, metadata, and version list visible in a single desktop viewport.
          height: 'clamp(400px, calc(100vh - 360px), 520px)',
          justifyContent: 'center',
          overflow: 'hidden',
          padding: 16,
          position: 'relative',
        }}
        onPointerDown={handlePanPointerDown}
        onPointerMove={handlePanPointerMove}
        onPointerUp={handlePanPointerUp}
        onPointerCancel={handlePanPointerUp}
      >
        {previewUrl && map ? (
          <div
            ref={layerRef}
            style={{
              alignItems: 'center',
              cursor:
                annotationMode || localizationMode
                  ? 'crosshair'
                  : transform.scale > MIN_MAP_SCALE
                    ? panning
                      ? 'grabbing'
                      : 'grab'
                    : 'default',
              aspectRatio: `${map.width} / ${map.height}`,
              display: 'block',
              height: layerSize ? layerSize.height : '100%',
              lineHeight: 0,
              margin: 'auto',
              maxWidth: '100%',
              position: 'relative',
              transform: mapLayerTransform(transform),
              transformOrigin: '0 0',
              width: layerSize ? layerSize.width : 'auto',
            }}
          >
            <img
              alt={`${map.name} 地图预览`}
              src={previewUrl}
              // Browsers natively drag <img> elements as a scaled-down ghost
              // and swallow pointer events meanwhile — that ghost was the
              // "canvas shrinking and following the cursor" seen during the
              // initial-pose drag. touch-action keeps touch gestures on the
              // pointer-event path as well.
              draggable={false}
              style={{
                display: 'block',
                height: '100%',
                imageRendering: 'pixelated',
                objectFit: 'contain',
                touchAction: 'none',
                userSelect: 'none',
                width: '100%',
              }}
              onPointerUp={(event) => {
                if ((!annotationMode && !localizationMode) || !map) return;
                const bounds = event.currentTarget.getBoundingClientRect();
                if (localizationMode) {
                  const drag = posePickDrag;
                  setPosePickDrag(undefined);
                  const downX = Number(
                    event.currentTarget.dataset.pointerDownX,
                  );
                  const downY = Number(
                    event.currentTarget.dataset.pointerDownY,
                  );
                  const deltaX = event.clientX - downX;
                  const deltaY = event.clientY - downY;
                  if (
                    !drag ||
                    !Number.isFinite(downX) ||
                    !Number.isFinite(downY) ||
                    Math.hypot(deltaX, deltaY) < 8
                  ) {
                    message.warning(
                      '请从机器人实际位置向车头方向拖动，以确认朝向。',
                    );
                    return;
                  }
                  // Browser Y grows downward; ROS map Y grows upward.
                  onInitialPosePick(drag.start, Math.atan2(-deltaY, deltaX));
                  return;
                }
                onMapClick(
                  mapPixelFromClientPoint(
                    { x: event.clientX, y: event.clientY },
                    bounds,
                    map,
                  ),
                );
              }}
              onPointerDown={(event) => {
                event.currentTarget.dataset.pointerDownX = String(
                  event.clientX,
                );
                event.currentTarget.dataset.pointerDownY = String(
                  event.clientY,
                );
                if (localizationMode && map) {
                  const bounds = event.currentTarget.getBoundingClientRect();
                  const pixel = mapPixelFromClientPoint(
                    { x: event.clientX, y: event.clientY },
                    bounds,
                    map,
                  );
                  setPosePickDrag({ start: pixel, current: pixel });
                }
              }}
              onPointerMove={(event) => {
                if (!localizationMode || !map) return;
                const bounds = event.currentTarget.getBoundingClientRect();
                const pixel = mapPixelFromClientPoint(
                  { x: event.clientX, y: event.clientY },
                  bounds,
                  map,
                );
                setPosePickDrag((current) =>
                  current ? { ...current, current: pixel } : current,
                );
              }}
              onPointerLeave={() => setPosePickDrag(undefined)}
            />
            <div
              style={{
                backgroundImage:
                  'linear-gradient(to right, #1677ff22 1px, transparent 1px), linear-gradient(to bottom, #1677ff22 1px, transparent 1px)',
                backgroundSize: '10% 10%',
                inset: 0,
                pointerEvents: 'none',
                position: 'absolute',
              }}
            />
            {locOverlayPoints && map ? (
              <LocOverlayCanvas
                map={map}
                points={locOverlayPoints}
                scale={transform.scale}
              />
            ) : null}
            {posePickDrag && map ? (
              <svg
                viewBox={`0 0 ${map.width} ${map.height}`}
                preserveAspectRatio="none"
                style={{
                  height: '100%',
                  inset: 0,
                  pointerEvents: 'none',
                  position: 'absolute',
                  width: '100%',
                  zIndex: 5,
                }}
              >
                <title>初始位置朝向拖拽指示</title>
                {(() => {
                  const x1 = posePickDrag.start.x + 0.5;
                  const y1 = posePickDrag.start.y + 0.5;
                  const x2 = posePickDrag.current.x + 0.5;
                  const y2 = posePickDrag.current.y + 0.5;
                  const angle = Math.atan2(y2 - y1, x2 - x1);
                  const head = 6;
                  const leftX = x2 - head * Math.cos(angle - 0.45);
                  const leftY = y2 - head * Math.sin(angle - 0.45);
                  const rightX = x2 - head * Math.cos(angle + 0.45);
                  const rightY = y2 - head * Math.sin(angle + 0.45);
                  return (
                    <>
                      <line
                        x1={x1}
                        y1={y1}
                        x2={x2}
                        y2={y2}
                        stroke="#f5222d"
                        strokeWidth={1.5}
                      />
                      <polygon
                        points={`${x2},${y2} ${leftX},${leftY} ${rightX},${rightY}`}
                        fill="#f5222d"
                      />
                      <circle
                        cx={x1}
                        cy={y1}
                        r={2.5}
                        fill="#fff"
                        stroke="#f5222d"
                        strokeWidth={1.5}
                      />
                    </>
                  );
                })()}
              </svg>
            ) : null}
            {annotations.map((annotation) => (
              <MapAnnotationMarker
                key={annotation.id}
                annotation={annotation}
                map={map}
                scale={transform.scale}
                onClick={onAnnotationClick}
                onMove={onAnnotationMove}
              />
            ))}
            {robots
              .filter((robot) => robot.mapId === map.id && robot.pose)
              .map((robot) => (
                <RobotPositionMarker
                  key={robot.id}
                  map={map}
                  robot={robot}
                  scale={transform.scale}
                />
              ))}
          </div>
        ) : null}
        {previewUrl && map ? (
          <div style={{ position: 'absolute', right: 12, top: 12, zIndex: 6 }}>
            <Space size={4}>
              <Button
                aria-label="缩小"
                disabled={transform.scale <= MIN_MAP_SCALE}
                icon={<ZoomOutOutlined />}
                size="small"
                onClick={() => zoomFromToolbar(1 / 1.5)}
              />
              <Tooltip title="重置缩放（Ctrl + 滚轮缩放，缩放后拖拽平移）">
                <Button
                  size="small"
                  style={{ minWidth: 52 }}
                  onClick={() => setTransform(IDENTITY_MAP_TRANSFORM)}
                >
                  {Math.round(transform.scale * 100)}%
                </Button>
              </Tooltip>
              <Button
                aria-label="放大"
                disabled={transform.scale >= MAX_MAP_SCALE}
                icon={<ZoomInOutlined />}
                size="small"
                onClick={() => zoomFromToolbar(1.5)}
              />
            </Space>
          </div>
        ) : null}
      </div>
      {map ? <MapScale map={map} /> : null}
    </Spin>
  );
}

function LocOverlayCanvas({
  map,
  points,
  scale,
}: {
  map: MapVersion;
  points: [number, number][];
  scale: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  // 点云已由车端投影到 map 坐标（米）；换算图像像素的方式与 RobotPositionMarker
  // 相同，越界点直接丢弃（画布之外本就不会显示）。
  useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext('2d');
    if (!canvas || !context) return;
    context.clearRect(0, 0, map.width, map.height);
    // 画布随地图图层一起缩放：按当前显示宽度反推半径，让红点在屏幕上始终保持约 2px。
    const displayedWidth = canvas.getBoundingClientRect().width;
    const radius = displayedWidth > 0 ? (2 * map.width) / displayedWidth : 2;
    context.fillStyle = '#f5222d';
    for (const [x, y] of points) {
      const pixelX = (x - map.origin.x) / map.resolution;
      const pixelY = map.height - 1 - (y - map.origin.y) / map.resolution;
      if (
        pixelX < 0 ||
        pixelX >= map.width ||
        pixelY < 0 ||
        pixelY >= map.height
      )
        continue;
      context.beginPath();
      context.arc(pixelX + 0.5, pixelY + 0.5, radius, 0, Math.PI * 2);
      context.fill();
    }
  }, [map, points, scale]);
  return (
    <canvas
      ref={canvasRef}
      width={map.width}
      height={map.height}
      style={{
        height: '100%',
        inset: 0,
        pointerEvents: 'none',
        position: 'absolute',
        width: '100%',
        zIndex: 1,
      }}
    />
  );
}

function InitialPosePreview({
  map,
  previewUrl,
  x,
  y,
  yaw,
}: {
  map: MapVersion;
  previewUrl: string;
  x: number;
  y: number;
  yaw: number;
}) {
  const pixelX = (x - map.origin.x) / map.resolution;
  const pixelY = map.height - 1 - (y - map.origin.y) / map.resolution;
  const yawDegrees = (yaw * 180) / Math.PI;
  return (
    <div
      style={{
        border: '1px solid #d9e0e8',
        borderRadius: 8,
        lineHeight: 0,
        marginBottom: 16,
        overflow: 'hidden',
        position: 'relative',
      }}
    >
      <img
        alt="初始位置预览"
        src={previewUrl}
        style={{
          display: 'block',
          imageRendering: 'pixelated',
          width: '100%',
        }}
      />
      <div
        style={{
          left: `${((pixelX + 0.5) / map.width) * 100}%`,
          position: 'absolute',
          top: `${((pixelY + 0.5) / map.height) * 100}%`,
          transform: 'translate(-50%, -50%)',
        }}
      >
        <span
          style={{
            borderBottom: '20px solid #f5222d',
            borderLeft: '9px solid transparent',
            borderRight: '9px solid transparent',
            display: 'inline-block',
            filter: 'drop-shadow(0 1px 3px #0009)',
            height: 0,
            transform: `rotate(${90 - yawDegrees}deg)`,
            width: 0,
          }}
        />
      </div>
    </div>
  );
}

function RobotPositionMarker({
  map,
  robot,
  scale,
}: {
  map: MapVersion;
  robot: RobotSummary;
  scale: number;
}) {
  const pose = robot.pose;
  if (!pose) return null;
  const pixelX = (pose.x - map.origin.x) / map.resolution;
  const pixelY = map.height - 1 - (pose.y - map.origin.y) / map.resolution;
  if (pixelX < 0 || pixelX >= map.width || pixelY < 0 || pixelY >= map.height)
    return null;
  const yawDegrees = (pose.yaw * 180) / Math.PI;
  const capturedAt = new Date(pose.capturedAt);
  const timeLabel = Number.isNaN(capturedAt.getTime())
    ? '未知时间'
    : capturedAt.toLocaleTimeString('zh-CN', { hour12: false });
  const localization = localizationMeta[robot.localizationStatus ?? 'unknown'];
  return (
    <div
      title={`${robot.name} (${robot.id})\n${localization.label}：(${pose.x.toFixed(3)}, ${pose.y.toFixed(3)}) m\n朝向：${yawDegrees.toFixed(0)}°\n采集时间：${timeLabel}`}
      style={{
        alignItems: 'center',
        color: '#fff',
        display: 'flex',
        flexDirection: 'column',
        left: `${((pixelX + 0.5) / map.width) * 100}%`,
        pointerEvents: 'none',
        position: 'absolute',
        top: `${((pixelY + 0.5) / map.height) * 100}%`,
        transform: markerCenterTransform(scale),
        zIndex: 4,
      }}
    >
      <span
        style={{
          alignItems: 'center',
          background:
            robot.localizationStatus !== 'localized' ? '#8c8c8c' : '#f5222d',
          border: '2px solid #fff',
          borderRadius: '50%',
          boxShadow: '0 1px 6px #0008',
          display: 'inline-flex',
          height: 34,
          justifyContent: 'center',
          position: 'relative',
          width: 34,
        }}
      >
        <RobotOutlined />
        <span
          role="img"
          aria-label={`机器人朝向 ${yawDegrees.toFixed(0)} 度`}
          style={{
            inset: -10,
            position: 'absolute',
            transform: `rotate(${yawArrowCssDegrees(yawDegrees)}deg)`,
          }}
        >
          <span
            style={{
              borderBottom: '10px solid #f5222d',
              borderLeft: '6px solid transparent',
              borderRight: '6px solid transparent',
              left: '50%',
              position: 'absolute',
              top: 0,
              transform: 'translateX(-50%)',
            }}
          />
        </span>
      </span>
      <span
        style={{
          background:
            robot.localizationStatus !== 'localized'
              ? '#8c8c8ce6'
              : '#f5222de6',
          borderRadius: 3,
          fontSize: 11,
          lineHeight: 1.4,
          marginTop: 3,
          maxWidth: 112,
          overflow: 'hidden',
          padding: '1px 5px',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {robot.name}（{localization.label}）
      </span>
    </div>
  );
}

function MapScale({ map }: { map: MapVersion }) {
  const gridSize = (map.width * map.resolution) / 10;
  return (
    <div
      style={{
        alignItems: 'center',
        background: 'linear-gradient(90deg, #f8fbff 0%, #ffffff 100%)',
        border: '1px solid #d9e7f7',
        borderRadius: 8,
        display: 'flex',
        gap: 16,
        justifyContent: 'space-between',
        marginTop: 12,
        padding: '9px 14px',
      }}
    >
      <Space size={10}>
        <div style={{ display: 'flex', height: 18, width: 150 }}>
          {['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j'].map(
            (key, index) => (
              <span
                key={key}
                style={{
                  background: index % 2 === 0 ? '#1677ff' : '#d6e4ff',
                  borderBottom: '2px solid #1677ff',
                  borderTop: '2px solid #1677ff',
                  height: 12,
                  width: 15,
                }}
              />
            ),
          )}
        </div>
        <Typography.Text strong>比例尺</Typography.Text>
      </Space>
      <Space split={<span style={{ color: '#d9d9d9' }}>|</span>} wrap>
        <Typography.Text type="secondary">
          10 格 = {(gridSize * 10).toFixed(2)} m
        </Typography.Text>
        <Typography.Text type="secondary">
          每格约 {gridSize.toFixed(2)} m
        </Typography.Text>
        <Typography.Text type="secondary">
          分辨率 {map.resolution.toFixed(3)} m / px
        </Typography.Text>
      </Space>
      <Space size={8}>
        <span
          title="地图朝向：上方为 90 度，右侧为 0 度"
          style={{
            alignItems: 'center',
            background: '#e6f4ff',
            border: '1px solid #91caff',
            borderRadius: '50%',
            color: '#1677ff',
            display: 'inline-flex',
            fontSize: 16,
            height: 28,
            justifyContent: 'center',
            width: 28,
          }}
        >
          ↑
        </span>
        <Typography.Text type="secondary">
          朝向：右 0° · 上 90° · 左 ±180° · 下 -90°
        </Typography.Text>
      </Space>
    </div>
  );
}

function MapAnnotationMarker({
  annotation,
  map,
  scale,
  onClick,
  onMove,
}: {
  annotation: MapAnnotation;
  map: MapVersion;
  scale: number;
  onClick: (annotation: MapAnnotation) => void;
  onMove: (annotation: MapAnnotation, pixel: PendingPixel) => void;
}) {
  const meta = annotationMeta[annotation.kind];
  // 拖拽改位：拖动中的临时像素位置（屏幕坐标增量按当前缩放换算回地图像素），
  // 松开后交给父层确认，确认前 marker 回到原位。
  const [dragPixel, setDragPixel] = useState<PendingPixel>();
  const dragRef = useRef<
    | { pointerId: number; startX: number; startY: number; moved: boolean }
    | undefined
  >(undefined);
  const suppressClickRef = useRef(false);
  const pixelX = dragPixel?.x ?? annotation.pixelX;
  const pixelY = dragPixel?.y ?? annotation.pixelY;
  const yawDegrees = radiansToDegrees(annotation.yaw);
  const arrowRotation = yawArrowCssDegrees(yawDegrees);

  function handlePointerDown(event: React.PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    // 阻止冒泡：避免触发地图平移/标注点选，把这次按压专属于拖动 marker。
    event.stopPropagation();
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function handlePointerMove(event: React.PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const deltaX = event.clientX - drag.startX;
    const deltaY = event.clientY - drag.startY;
    if (!drag.moved && Math.hypot(deltaX, deltaY) < 4) return;
    drag.moved = true;
    const layer = event.currentTarget.parentElement;
    if (!layer) return;
    // 图层 rect 已包含缩放，直接按显示比例把屏幕位移换算成地图像素。
    const rect = layer.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    setDragPixel({
      x: Math.min(
        map.width - 1,
        Math.max(
          0,
          Math.round(annotation.pixelX + deltaX / (rect.width / map.width)),
        ),
      ),
      y: Math.min(
        map.height - 1,
        Math.max(
          0,
          Math.round(annotation.pixelY + deltaY / (rect.height / map.height)),
        ),
      ),
    });
  }

  function handlePointerUp(event: React.PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    dragRef.current = undefined;
    if (!drag.moved) return;
    // 拖动后的合成 click 不应打开详情；在 click 处理器里自清。
    suppressClickRef.current = true;
    const target = dragPixel;
    setDragPixel(undefined);
    if (
      target &&
      (target.x !== annotation.pixelX || target.y !== annotation.pixelY)
    ) {
      onMove(annotation, target);
    }
  }

  return (
    <div
      title={`${meta.label}：${annotation.label}\nROS 坐标：(${annotation.mapX.toFixed(3)}, ${annotation.mapY.toFixed(3)})，朝向 ${yawDegrees.toFixed(0)}°\n拖动可改位（松开后确认）`}
      style={{
        alignItems: 'center',
        color: '#fff',
        cursor: dragPixel ? 'grabbing' : 'grab',
        display: 'flex',
        flexDirection: 'column',
        left: `${((pixelX + 0.5) / map.width) * 100}%`,
        pointerEvents: 'auto',
        position: 'absolute',
        top: `${((pixelY + 0.5) / map.height) * 100}%`,
        touchAction: 'none',
        transform: markerCenterTransform(scale),
        userSelect: 'none',
        zIndex: dragPixel ? 3 : 2,
      }}
      onClick={(event) => {
        event.stopPropagation();
        if (suppressClickRef.current) {
          suppressClickRef.current = false;
          return;
        }
        onClick(annotation);
      }}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
    >
      <span
        style={{
          display: 'inline-flex',
          position: 'relative',
          transform: `rotate(${arrowRotation}deg)`,
        }}
      >
        <span
          style={{
            alignItems: 'center',
            background: meta.color,
            border: '2px solid #fff',
            borderRadius: '50%',
            boxShadow: '0 1px 4px #0006',
            display: 'inline-flex',
            height: 30,
            justifyContent: 'center',
            transform: `rotate(${-arrowRotation}deg)`,
            width: 30,
          }}
        >
          {meta.icon}
        </span>
        <span
          style={{
            borderBottom: `9px solid ${meta.color}`,
            borderLeft: '5px solid transparent',
            borderRight: '5px solid transparent',
            filter: 'drop-shadow(0 1px 2px #0007)',
            height: 0,
            left: '50%',
            position: 'absolute',
            top: -8,
            transform: 'translateX(-50%)',
            width: 0,
          }}
        />
      </span>
      <span
        style={{
          background: '#18202ccc',
          borderRadius: 3,
          fontSize: 11,
          lineHeight: 1.4,
          marginTop: 3,
          maxWidth: 100,
          overflow: 'hidden',
          padding: '1px 5px',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {annotation.label}
      </span>
    </div>
  );
}

function MapProperties({
  map,
  annotationCount,
}: {
  map: MapVersion;
  annotationCount: number;
}) {
  return (
    <Descriptions column={1} size="small">
      <Descriptions.Item label="地图 ID">
        <Typography.Text
          code
          copyable={{ text: map.id, tooltips: ['点击复制', '已复制'] }}
        >
          {map.id}
        </Typography.Text>
      </Descriptions.Item>
      <Descriptions.Item label="原始文件">
        {map.yamlFile} / {map.pgmFile}
      </Descriptions.Item>
      <Descriptions.Item label="图像尺寸">
        {map.width} × {map.height} px
      </Descriptions.Item>
      <Descriptions.Item label="实际范围">
        {(map.width * map.resolution).toFixed(2)} ×{' '}
        {(map.height * map.resolution).toFixed(2)} m
      </Descriptions.Item>
      <Descriptions.Item label="分辨率">
        {map.resolution.toFixed(3)} m / px
      </Descriptions.Item>
      <Descriptions.Item label="地图原点">
        [{map.origin.x.toFixed(3)}, {map.origin.y.toFixed(3)},{' '}
        {map.origin.yaw.toFixed(3)}]
      </Descriptions.Item>
      <Descriptions.Item label="导入人">{map.createdBy}</Descriptions.Item>
      <Descriptions.Item label="地图标注">
        {annotationCount} 个
      </Descriptions.Item>
    </Descriptions>
  );
}

function MapFilePicker({
  accept,
  file,
  icon,
  label,
  onChange,
}: {
  accept: string;
  file?: File;
  icon: React.ReactNode;
  label: string;
  onChange: (file: File | undefined) => void;
}) {
  return (
    <Upload
      accept={accept}
      beforeUpload={() => false}
      maxCount={1}
      fileList={
        file ? [{ uid: file.name, name: file.name, status: 'done' }] : []
      }
      onChange={({ fileList }) => onChange(fileList[0]?.originFileObj)}
    >
      <Button block icon={icon}>
        {file ? `${label}：${file.name}` : label}
      </Button>
    </Upload>
  );
}
