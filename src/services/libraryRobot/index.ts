import type {
  AlertRecord,
  Book,
  BookImportResult,
  BookUpsert,
  ControlStationHealth,
  CruiseSyncPreview,
  DispatchTask,
  ImportMapResult,
  MapAnnotation,
  MapAnnotationKind,
  MapAnnotationResult,
  MapVersion,
  MappingActionResult,
  MappingLocOverlay,
  MappingMarkResult,
  MappingMarksClearResult,
  MappingMarksResult,
  MappingPose,
  MappingStatus,
  ReturnMode,
  RobotCommand,
  RobotSummary,
  SaveMappingResult,
} from './types';

export async function apiRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set('Content-Type', 'application/json');

  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail =
      body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string'
        ? body.detail
        : `请求失败（${response.status}）`;
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  // 202 可能不带响应体（如 manual-control/jog），空 body 按 undefined 处理。
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export async function getControlStationHealth(): Promise<ControlStationHealth> {
  const response = await fetch('/health', { cache: 'no-store' });
  if (!response.ok) throw new Error(`无法读取控制站状态（${response.status}）`);
  return (await response.json()) as ControlStationHealth;
}

export async function getRobots(): Promise<RobotSummary[]> {
  return apiRequest('/api/v1/robots');
}

type RobotRealtimeMessage =
  | { type: 'robots.snapshot'; robots: RobotSummary[] }
  | { type: 'robot.updated'; robot: RobotSummary };

export type RobotRealtimeState = {
  connected: boolean;
  robots: RobotSummary[];
};

let realtimeState: RobotRealtimeState = { connected: false, robots: [] };
let realtimeSocket: WebSocket | undefined;
let realtimeReconnectTimer: number | undefined;
let realtimeStarted = false;
const realtimeListeners = new Set<() => void>();

function emitRealtimeState() {
  for (const listener of realtimeListeners) listener();
}

function updateRealtimeState(update: Partial<RobotRealtimeState>) {
  realtimeState = { ...realtimeState, ...update };
  emitRealtimeState();
}

function robotRealtimeUrl() {
  const apiPath = '/api/v1/realtime/robots';
  // Utoopack wraps configured define values in an additional string layer.
  const configuredOrigin = process.env.LR_REALTIME_API_ORIGIN?.replace(/^"(.*)"$/, '$1');
  if (configuredOrigin) {
    const url = new URL(configuredOrigin);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    url.pathname = apiPath;
    url.search = '';
    return url.toString();
  }

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}${apiPath}`;
}

function connectRobotRealtime() {
  if (!realtimeStarted || typeof window === 'undefined') return;
  realtimeSocket = new WebSocket(robotRealtimeUrl());
  realtimeSocket.onopen = () => updateRealtimeState({ connected: true });
  realtimeSocket.onmessage = (event) => {
    try {
      const message = JSON.parse(event.data) as RobotRealtimeMessage;
      if (message.type === 'robots.snapshot') {
        updateRealtimeState({ robots: message.robots });
        return;
      }
      if (message.type === 'robot.updated') {
        const index = realtimeState.robots.findIndex((item) => item.id === message.robot.id);
        const robots = [...realtimeState.robots];
        if (index === -1) robots.push(message.robot);
        else robots[index] = message.robot;
        updateRealtimeState({ robots });
      }
    } catch {
      // Ignore malformed frames; the page keeps an HTTP fallback refresh.
    }
  };
  realtimeSocket.onerror = () => realtimeSocket?.close();
  realtimeSocket.onclose = () => {
    updateRealtimeState({ connected: false });
    if (realtimeStarted) realtimeReconnectTimer = window.setTimeout(connectRobotRealtime, 2_000);
  };
}

export function startRobotRealtime() {
  if (realtimeStarted || typeof window === 'undefined') return;
  realtimeStarted = true;
  connectRobotRealtime();
}

export function stopRobotRealtime() {
  realtimeStarted = false;
  if (realtimeReconnectTimer) window.clearTimeout(realtimeReconnectTimer);
  realtimeReconnectTimer = undefined;
  realtimeSocket?.close();
  realtimeSocket = undefined;
  updateRealtimeState({ connected: false, robots: [] });
}

export function getRobotRealtimeState() {
  return realtimeState;
}

export function subscribeRobotRealtime(listener: () => void) {
  realtimeListeners.add(listener);
  return () => {
    realtimeListeners.delete(listener);
  };
}

/** Active (non-terminal) task states; terminal states are completed/failed/cancelled/recovery_required. */
export const ACTIVE_TASK_STATUSES: DispatchTask['status'][] = [
  'queued',
  'running',
  'paused',
];

/** How many of the newest tasks polling clients keep in addition to active ones. */
export const TASK_POLL_RECENT_LIMIT = 20;

export async function getTasks(options?: {
  status?: DispatchTask['status'][];
  limit?: number;
}): Promise<DispatchTask[]> {
  const params = new URLSearchParams();
  for (const status of options?.status ?? []) params.append('status', status);
  if (options?.limit !== undefined) params.set('limit', String(options.limit));
  const query = params.toString();
  return apiRequest(`/api/v1/dispatch/tasks${query ? `?${query}` : ''}`);
}

/** 轮询任务进度的标准拉取：全部活动任务 + 最近 20 条（终态任务刚结束时仍可见）。 */
export async function pollTasks(): Promise<DispatchTask[]> {
  return getTasks({ status: ACTIVE_TASK_STATUSES, limit: TASK_POLL_RECENT_LIMIT });
}

/** Navigate to a shelf target (unless sendNavigation is false), then run
 * the on-robot takeout workflow. */
export async function createCompleteTakeout(values: {
  robotId: string;
  templateId: string;
  target?: { x: number; y: number; yaw: number };
  liftHeightMm: number;
  destinationSlot: number;
  liftSpeedLevel?: number;
  bookId?: string;
  mapId?: string;
  annotationId?: string;
  sendNavigation?: boolean;
}): Promise<DispatchTask> {
  return apiRequest('/api/v1/circulation/complete-takeout', {
    method: 'POST',
    body: JSON.stringify(values),
  });
}

export async function cancelCompleteTakeout(taskId: string): Promise<DispatchTask> {
  return apiRequest(`/api/v1/circulation/complete-takeout/${taskId}/cancel`, {
    method: 'POST',
  });
}

/** Navigate to a shelf target (unless sendNavigation is false), then run
 * the on-robot return workflow (mid-return by default; end-of-row return
 * when returnMode is 'end'). */
export async function createCompleteReturn(values: {
  robotId: string;
  templateId: string;
  target?: { x: number; y: number; yaw: number };
  liftHeightMm: number;
  sourceSlot: number;
  liftSpeedLevel?: number;
  bookId?: string;
  mapId?: string;
  annotationId?: string;
  sendNavigation?: boolean;
  /** 还书模式：mid 中位还书（默认）；end 末尾还书。end 模式下后端拒绝
   * injectedTargetGraspPose（422）。 */
  returnMode?: ReturnMode;
  /** Stress-test shortcut: reuse a previously recognized grasp pose (6 values:
   * x/y/z metres + rx/ry/rz radians) as the insert anchor (mid-return step 24 on).
   * Only valid for mid-return. */
  injectedTargetGraspPose?: number[];
}): Promise<DispatchTask> {
  return apiRequest('/api/v1/circulation/complete-return', {
    method: 'POST',
    body: JSON.stringify(values),
  });
}

export async function cancelCompleteReturn(taskId: string): Promise<DispatchTask> {
  return apiRequest(`/api/v1/circulation/complete-return/${taskId}/cancel`, {
    method: 'POST',
  });
}

export async function listBooks(): Promise<Book[]> {
  return apiRequest('/api/v1/books');
}

/** Default shelf (lift) height assigned when books are registered from the spine studio. */
export const DEFAULT_BOOK_HEIGHT_MM = 550;

export async function createBook(values: BookUpsert): Promise<Book> {
  return apiRequest('/api/v1/books', {
    method: 'POST',
    body: JSON.stringify(values),
  });
}

export async function updateBook(bookId: string, values: BookUpsert): Promise<Book> {
  return apiRequest(`/api/v1/books/${bookId}`, {
    method: 'PUT',
    body: JSON.stringify(values),
  });
}

export async function deleteBook(bookId: string): Promise<void> {
  return apiRequest(`/api/v1/books/${bookId}`, { method: 'DELETE' });
}

/** Batch-register books from a CSV (书名,书脊图文件名,高度mm,点位标注名,槽位)
 * plus a zip of the referenced spine images (Base64 JSON, like map import). */
export async function importBooks(
  csvFile: File,
  imagesZip: File,
): Promise<BookImportResult> {
  const [csvContent, imagesContent] = await Promise.all([
    fileToBase64(csvFile),
    fileToBase64(imagesZip),
  ]);
  return apiRequest('/api/v1/library/book-imports', {
    method: 'POST',
    body: JSON.stringify({
      csvName: csvFile.name,
      csvContent,
      imagesName: imagesZip.name,
      imagesContent,
    }),
  });
}

/** Download the library backup (books + maps + annotations + spine listing). */
export async function exportLibraryBackup(): Promise<void> {
  const response = await fetch('/api/v1/library/export', { cache: 'no-store' });
  if (!response.ok) throw new Error(`无法导出备份（${response.status}）`);
  const disposition = response.headers.get('content-disposition') ?? '';
  const match = /filename="?([^";]+)"?/.exec(disposition);
  const filename = match?.[1] ?? 'lr-library-backup.json';
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

/** Navigate once (unless sendNavigation is false), then take N books into
 * stand slots N..1 (inside-out) and return slots 1..M (outside-in). */
export async function createMultiRound(values: {
  robotId: string;
  target?: { x: number; y: number; yaw: number };
  takeBookIds: string[];
  returnBookIds?: string[];
  liftSpeedLevel?: number;
  mapId?: string;
  annotationId?: string;
  sendNavigation?: boolean;
}): Promise<DispatchTask> {
  return apiRequest('/api/v1/circulation/multi-round', {
    method: 'POST',
    body: JSON.stringify(values),
  });
}

export async function cancelMultiRound(taskId: string): Promise<DispatchTask> {
  return apiRequest(`/api/v1/circulation/multi-round/${taskId}/cancel`, {
    method: 'POST',
  });
}

/** Clear a bookarm RECOVERY_REQUIRED task after the operator confirms the arm
 * has been handled manually (the arm may be holding a book mid-way). */
export async function dismissBookarmRecovery(
  bookarmTaskId: string,
): Promise<unknown> {
  return apiRequest(`/api/v1/bookarm/tasks/${bookarmTaskId}/dismiss-recovery`, {
    method: 'POST',
  });
}

export async function moveLift(robotId: string, distanceCm: number): Promise<RobotCommand> {
  return apiRequest(`/api/v1/robots/${robotId}/lift/move`, {
    method: 'POST',
    body: JSON.stringify({ distanceCm, acknowledgeSafety: true }),
  });
}

export async function getAlerts(): Promise<AlertRecord[]> {
  return apiRequest('/api/v1/alerts');
}

/** Mark one alert acknowledged; the overview's pending count drops accordingly. */
export async function acknowledgeAlert(alertId: string): Promise<AlertRecord> {
  return apiRequest(`/api/v1/alerts/${encodeURIComponent(alertId)}/ack`, {
    method: 'POST',
  });
}

export async function getMapVersions(): Promise<MapVersion[]> {
  return apiRequest('/api/v1/library/maps');
}

export async function importMap(values: {
  name: string;
  /** 可选显式地图 ID（MAP- 开头），留空由服务端自动生成。 */
  id?: string;
  yamlFile: File;
  pgmFile: File;
}): Promise<ImportMapResult> {
  const [yamlContent, pgmContent] = await Promise.all([fileToBase64(values.yamlFile), fileToBase64(values.pgmFile)]);
  return apiRequest('/api/v1/library/maps/import', {
    method: 'POST',
    body: JSON.stringify({
      name: values.name,
      ...(values.id ? { id: values.id } : {}),
      yamlName: values.yamlFile.name,
      yamlContent,
      pgmName: values.pgmFile.name,
      pgmContent,
    }),
  });
}

export function mapPreviewUrl(mapId: string) {
  return `/api/v1/library/maps/${mapId}/preview`;
}

export async function deleteLibraryMap(mapId: string): Promise<void> {
  await apiRequest<void>(`/api/v1/library/maps/${mapId}`, { method: 'DELETE' });
}

export async function getMapPreview(mapId: string): Promise<string> {
  const response = await fetch(mapPreviewUrl(mapId));
  if (!response.ok) throw new Error(`无法读取地图预览（${response.status}）`);
  return URL.createObjectURL(await response.blob());
}

export async function getMapAnnotations(mapId: string): Promise<MapAnnotation[]> {
  return apiRequest(`/api/v1/library/maps/${mapId}/annotations`);
}

export async function createMapAnnotation(
  mapId: string,
  values: { kind: MapAnnotationKind; label: string; pixelX: number; pixelY: number; yaw: number },
): Promise<MapAnnotationResult> {
  return apiRequest(`/api/v1/library/maps/${mapId}/annotations`, {
    method: 'POST',
    body: JSON.stringify(values),
  });
}

export async function deleteMapAnnotation(mapId: string, annotationId: string): Promise<void> {
  await apiRequest<void>(`/api/v1/library/maps/${mapId}/annotations/${annotationId}`, { method: 'DELETE' });
}

export async function updateMapAnnotation(
  mapId: string,
  annotationId: string,
  values: { label?: string; x?: number; y?: number; yaw?: number },
): Promise<MapAnnotation> {
  return apiRequest(`/api/v1/library/maps/${mapId}/annotations/${annotationId}`, {
    method: 'PATCH',
    body: JSON.stringify(values),
  });
}

export async function previewCruiseSync(mapId: string): Promise<CruiseSyncPreview> {
  return apiRequest(`/api/v1/library/maps/${mapId}/annotations/sync-cruise/preview`);
}

export async function syncCruiseAnnotations(mapId: string): Promise<MapAnnotation[]> {
  return apiRequest(`/api/v1/library/maps/${mapId}/annotations/sync-cruise`, { method: 'POST' });
}

export async function setRobotInitialPose(
  robotId: string,
  values: { mapId: string; frameId: 'map'; x: number; y: number; yaw: number },
) {
  return apiRequest(`/api/v1/robots/${robotId}/localization/initial-pose`, {
    method: 'POST',
    body: JSON.stringify(values),
  });
}

/** Ask the robot to run a whole-map laser match and localize itself (no motion). */
export async function autoLocalize(robotId: string): Promise<RobotCommand> {
  return apiRequest(`/api/v1/robots/${robotId}/localization/auto-localize`, {
    method: 'POST',
  });
}

export type MapSwitchResult = {
  ok: boolean;
  mapId: string;
  commandId: string;
  message: string;
};

/** Direct map switch: one click waits for the vehicle's switched/failed result. */
export async function switchRobotMap(robotId: string, mapId: string): Promise<MapSwitchResult> {
  return apiRequest(`/api/v1/robots/${robotId}/map/switch`, {
    method: 'POST',
    body: JSON.stringify({ mapId }),
  });
}

/** Navigate to an annotation with the low-speed fine-tune stage; returns the
 * parent task whose workflowMessage/progress and stage rows give realtime
 * feedback. Pass `skipFineTune: true` when approximate arrival is enough. */
export async function navigateRobotToAnnotation(
  robotId: string,
  values: { mapId: string; annotationId: string; skipFineTune?: boolean },
): Promise<DispatchTask> {
  return apiRequest(`/api/v1/robots/${robotId}/annotations/navigate`, {
    method: 'POST',
    body: JSON.stringify(values),
  });
}

/** Cancel a running annotation navigation flow; the vehicle stops the current stage. */
export async function cancelAnnotationNavigation(
  robotId: string,
  taskId: string,
): Promise<DispatchTask> {
  return apiRequest(
    `/api/v1/robots/${robotId}/annotations/navigate/${taskId}/cancel`,
    { method: 'POST' },
  );
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== 'string') {
        reject(new Error('读取文件失败'));
        return;
      }
      resolve(result.split(',', 2)[1] ?? '');
    };
    reader.onerror = () => reject(new Error(`无法读取文件：${file.name}`));
    reader.readAsDataURL(file);
  });
}

export async function createRobot(values: { id: string; name: string; gatewayHost: string }): Promise<RobotSummary> {
  return apiRequest('/api/v1/robots', { method: 'POST', body: JSON.stringify(values) });
}

export async function deleteRobot(robotId: string): Promise<void> {
  await apiRequest<void>(`/api/v1/robots/${robotId}`, { method: 'DELETE' });
}

/** Ack of a submitted robot command; `detail` carries the operator-facing
 * outcome note (e.g. the E-stop arm-interlock result) when the API sets one. */
export interface RobotCommandAck {
  commandId: string;
  robotId?: string;
  command?: string;
  accepted?: boolean;
  payload?: Record<string, unknown>;
  detail?: string;
}

export async function submitRobotCommand(
  robotId: string,
  command: 'navigate' | 'charge' | 'pause' | 'stop' | 'emergency_stop',
  payload: Record<string, unknown> = {},
): Promise<RobotCommandAck> {
  return apiRequest(`/api/v1/robots/${robotId}/commands`, {
    method: 'POST',
    body: JSON.stringify({ command, payload }),
  });
}

export async function resetSoftwareEmergencyStop(robotId: string): Promise<RobotSummary> {
  return apiRequest(`/api/v1/robots/${robotId}/emergency-stop/reset`, {
    method: 'POST',
    body: JSON.stringify({ acknowledgeSafety: true }),
  });
}

/** Fire-and-forget velocity jog; the vehicle watchdog stops it within 1s. */
export async function sendManualJog(
  robotId: string,
  body: { linear: number; angular: number; durationMs: number },
): Promise<void> {
  await apiRequest<void>(`/api/v1/robots/${robotId}/manual-control/jog`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function getMappingStatus(robotId: string): Promise<MappingStatus> {
  return apiRequest(`/api/v1/robots/${robotId}/mapping/status`);
}

export async function startMapping(robotId: string): Promise<MappingActionResult> {
  return apiRequest(`/api/v1/robots/${robotId}/mapping/start`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function stopMapping(robotId: string): Promise<MappingActionResult> {
  return apiRequest(`/api/v1/robots/${robotId}/mapping/stop`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function sendMappingJog(
  robotId: string,
  values: { linear: number; angular: number },
): Promise<{ ok: boolean }> {
  return apiRequest(`/api/v1/robots/${robotId}/mapping/jog`, {
    method: 'POST',
    body: JSON.stringify(values),
  });
}

export async function getMappingPose(robotId: string): Promise<MappingPose> {
  return apiRequest(`/api/v1/robots/${robotId}/mapping/pose`);
}

export async function getMappingLocOverlay(robotId: string): Promise<MappingLocOverlay> {
  return apiRequest(`/api/v1/robots/${robotId}/mapping/loc-overlay`);
}

export async function getMappingMarks(robotId: string): Promise<MappingMarksResult> {
  return apiRequest(`/api/v1/robots/${robotId}/mapping/marks`);
}

export async function createMappingMark(robotId: string, label?: string): Promise<MappingMarkResult> {
  return apiRequest(`/api/v1/robots/${robotId}/mapping/mark`, {
    method: 'POST',
    body: JSON.stringify(label ? { label } : {}),
  });
}

export async function clearMappingMarks(robotId: string): Promise<MappingMarksClearResult> {
  return apiRequest(`/api/v1/robots/${robotId}/mapping/marks/clear`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

/** 单删一条书架点标注（按列表序号，越界由车端返回 404）。 */
export async function deleteMappingMark(robotId: string, index: number): Promise<unknown> {
  return apiRequest(`/api/v1/robots/${robotId}/mapping/marks/${index}`, {
    method: 'DELETE',
  });
}

export async function saveMapping(robotId: string, name?: string): Promise<SaveMappingResult> {
  return apiRequest(`/api/v1/robots/${robotId}/mapping/save`, {
    method: 'POST',
    body: JSON.stringify(name ? { name } : {}),
  });
}

export function mappingSnapshotUrl(robotId: string) {
  return `/api/v1/robots/${robotId}/mapping/snapshot.png`;
}

async function getProtectedImage(url: string, label: string): Promise<string> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${label}（${response.status}）`);
  return URL.createObjectURL(await response.blob());
}

export async function getMappingSnapshotUrl(robotId: string): Promise<string> {
  return getProtectedImage(mappingSnapshotUrl(robotId), '无法读取建图快照');
}

/** Fetch one multi-round vision snapshot (auth-protected) as an object URL. */
export async function getVisionImageUrl(url: string): Promise<string> {
  return getProtectedImage(url, '无法读取识别图');
}

export type StartAllStep = { name: string; status: string; message: string };

export type StartAllResult = {
  ok: boolean;
  steps: StartAllStep[];
  output: string;
};

/** Run the curated one-click startup for every platform dependency. */
export async function startAllServices(): Promise<StartAllResult> {
  return apiRequest('/api/v1/system/start-all', {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

/** Restart every backend module via the curated fixed host script. */
export async function restartAllServices(): Promise<StartAllResult> {
  return apiRequest('/api/v1/system/restart-all', {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

/** Whether one-click startup may bring up arm CAN/service right now. */
export type BookarmStartPolicy = {
  allow: boolean;
};

/** Read the operator-confirmed arm hardware start policy. */
export async function getBookarmStartPolicy(): Promise<BookarmStartPolicy> {
  return apiRequest('/api/v1/system/bookarm-start-policy');
}

/** Confirm or revoke arm hardware start-up after on-site inspection. */
export async function setBookarmStartPolicy(
  allow: boolean,
): Promise<BookarmStartPolicy> {
  return apiRequest('/api/v1/system/bookarm-start-policy', {
    method: 'PUT',
    body: JSON.stringify({ allow }),
  });
}
