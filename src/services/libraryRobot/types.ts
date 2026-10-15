export type RobotStatus =
  | 'idle'
  | 'navigating'
  | 'charging'
  | 'manual'
  | 'fault'
  | 'offline'
  | 'busy'
  | 'mapping';

export interface RobotSummary {
  id: string;
  name: string;
  status: RobotStatus;
  location: string;
  currentTask: string;
  mapVersion: string;
  mapId?: string;
  pose?: {
    frameId: 'map';
    x: number;
    y: number;
    yaw: number;
    capturedAt: string;
  };
  localizationStatus?:
    | 'starting'
    | 'restoring_saved_pose'
    | 'global_localizing'
    | 'manual_initializing'
    | 'localized'
    | 'manual_required'
    | 'unknown';
  localizationScanMode?:
    | 'dual'
    | 'single_laser_1'
    | 'single_laser_2'
    | 'unavailable'
    | 'unknown';
  lastSeenAt?: string;
  health: {
    navigation: boolean;
    chassis: boolean;
    arm: boolean;
    rod: boolean;
    imu: boolean;
    lift: boolean;
    camera: boolean;
    liftAbsolutePositioningReady?: boolean;
  };
  armDiagnostics?: {
    connected: boolean;
    feedbackHealthy: boolean;
    canFps: number;
    feedbackReason: string;
  };
  updatedAt: string;
}

export interface ControlStationHealth {
  status: 'ok' | 'degraded';
  service: string;
  environment: string;
  dependencies: {
    postgres: 'connected' | 'memory';
    redis: 'connected' | 'memory';
    mqtt: 'connected' | 'disconnected';
  };
  station: {
    product: string;
    id: string;
    name: string;
  };
}

/** 还书模式：mid 中位还书（默认）；end 末尾还书（识别目标书后插到其旁）。 */
export type ReturnMode = 'mid' | 'end';

export interface DispatchTask {
  id: string;
  type: 'borrow' | 'return' | 'inventory' | 'navigate' | 'cruise' | 'charge' | 'arm_test' | 'takeout' | 'multi_round' | 'fine_tune';
  robotId: string;
  target: string;
  status: 'queued' | 'running' | 'paused' | 'failed' | 'completed' | 'cancelled' | 'recovery_required';
  progress: number;
  updatedAt: string;
  mapId?: string;
  annotationId?: string;
  shelfId?: string;
  routeAnnotationIds?: string[];
  loopCount?: number;
  bookId?: string;
  bookTitle?: string;
  workflowMode?: 'takeout' | 'putback' | 'putback2' | 'tail_putback';
  deliveryAnnotationId?: string;
  deliveryLabel?: string;
  workflowId?: string;
  workflowStep?: number;
  workflowStepCount?: number;
  workflowStepName?: string;
  workflowMessage?: string;
  workflowDetails?: {
    preflightRequired?: boolean;
    preflightComplete?: boolean;
    preflightState?: 'required' | 'moving' | 'complete' | 'failed';
    preflightName?: string;
    templateId?: string;
    destinationSlot?: number;
    sourceSlot?: number;
    liftHeightMm?: number;
    bookarmTaskId?: string;
    navigationTaskId?: string;
    fineTuneTaskId?: string;
    annotationNavigation?: boolean;
    /** 还书任务实际使用的还书模式（缺省为中位还书）。 */
    returnMode?: ReturnMode;
    target?: { x: number; y: number; yaw: number };
    takeBooks?: MultiRoundBookEntry[];
    returnBooks?: MultiRoundBookEntry[];
    /** D435 spine-recognition snapshot(s) captured during the flow; urls are
     * versioned so a later recognition overwrite busts the cached image. */
    visionImages?: { spine?: string; gap?: string };
    captureResult?: {
      capture_id?: string;
      mode?: string;
      width?: number;
      height?: number;
      point_count?: number;
      depth_max_m?: number;
    };
    recognitionResult?: {
      capture_id?: string;
      confidence?: number;
      good_count?: number;
      inlier_count?: number;
      backend?: string;
      pixel_uv?: number[];
      camera_point_m?: number[];
      robot_point_raw_m?: number[];
      target_point_m?: number[];
    };
  };
  requiresConfirmation?: boolean;
  failureReason?: string;
}

/** A managed book: name, spine-library template (its spine image), and the
 * shelf height used as the lift target when taking or returning it. */
export interface Book {
  id: string;
  name: string;
  templateId: string;
  heightMm: number;
  /** 取书点（地图标注 id）：取书/还书面板选书后自动带出的导航目标。 */
  annotationId?: string | null;
  /** 常用书立槽位（可空）：选书后自动带出，用户可改。 */
  preferredSlot?: number | null;
  createdAt: string;
  updatedAt: string;
}

export interface BookUpsert {
  name: string;
  templateId: string;
  heightMm: number;
  annotationId?: string | null;
  preferredSlot?: number | null;
}

/** 批量导入单行失败明细：row 为 CSV 文件行号（1 起，含表头）。 */
export interface BookImportFailure {
  row: number;
  reason: string;
}

export interface BookImportResult {
  total: number;
  succeeded: number;
  failed: BookImportFailure[];
}

/** Per-book snapshot in a multi-round task's workflowDetails. */
export interface MultiRoundBookEntry {
  bookId: string;
  name: string;
  templateId: string;
  heightMm: number;
  slot: number;
  status?: 'pending' | 'running' | 'completed';
  bookarmTaskId?: string;
  /** Vision snapshots captured right after this book's sub-task ended. */
  visionImages?: { spine?: string; gap?: string };
}

export interface RobotCommand {
  commandId: string;
  robotId: string;
  command: string;
  expiresAt: string;
  accepted: boolean;
}

export interface AlertRecord {
  id: string;
  severity: 'critical' | 'warning' | 'info';
  source: string;
  title: string;
  time: string;
  acknowledged: boolean;
}

export interface MapVersion {
  id: string;
  name: string;
  version: string;
  yamlFile: string;
  pgmFile: string;
  width: number;
  height: number;
  resolution: number;
  origin: { x: number; y: number; yaw: number };
  mode: 'trinary' | 'scale' | 'raw';
  negate: boolean;
  occupiedThreshold: number;
  freeThreshold: number;
  status: 'draft' | 'published';
  createdAt: string;
  createdBy: string;
}

export type MapAnnotationKind = 'zone' | 'shelf' | 'charging' | 'delivery' | 'dock' | 'cruise';

export interface MapAnnotation {
  id: string;
  mapId: string;
  kind: MapAnnotationKind;
  label: string;
  pixelX: number;
  pixelY: number;
  mapX: number;
  mapY: number;
  yaw: number;
  createdAt: string;
  createdBy: string;
}

/** 创建标注响应：重名警告仅本次返回，不持久化。 */
export interface MapAnnotationResult extends MapAnnotation {
  labelWarning?: string | null;
}

export interface CruiseSyncPreviewItem {
  name: string;
  x: number;
  y: number;
  yaw: number;
  pixelX?: number | null;
  pixelY?: number | null;
  action: 'create' | 'skip';
  reason?: string | null;
}

export interface CruiseSyncPreview {
  file: string;
  items: CruiseSyncPreviewItem[];
}

export type MappingMode = 'navigation' | 'mapping' | 'transition';

export interface MappingStatus {
  mode: MappingMode;
  slamRunning: boolean;
  message: string;
  /** 车端 SLAM 重启后由建图代理上报：未保存的建图会话已被重置。 */
  slamRestarted?: boolean;
}

export interface MappingActionResult {
  ok: boolean;
  mode: string;
}

export interface MappingPose {
  ok: boolean;
  x: number;
  y: number;
  yaw: number;
}

export interface MappingMark {
  x: number;
  y: number;
  yaw: number;
  label: string;
  at: string;
}

export interface MappingMarksResult {
  marks: MappingMark[];
}

export interface MappingMarkResult {
  mark: MappingMark;
  count: number;
}

export interface MappingMarksClearResult {
  ok: boolean;
  cleared: number;
}

/** 保存建图入库结果：地图记录 + 书架点导入计数；alreadySaved 表示本次建图
 * 会话已在车端保存过，返回的是首存地图。 */
export interface SaveMappingResult extends MapVersion {
  importedMarks: number;
  skippedMarks: number;
  alreadySaved?: boolean;
}

/** 导入地图结果：vehicleRegistered 为 false 时 vehicleRegisterNote 给出原因。 */
export interface ImportMapResult extends MapVersion {
  vehicleRegistered?: boolean;
  vehicleRegisterNote?: string;
}

/** Localization lidar overlay projected into the map frame by the on-robot agent. */
export interface MappingLocOverlay {
  ok: boolean;
  capturedAt: number;
  frameId: string;
  pose: { x: number; y: number; yaw: number };
  points: [number, number][];
}
