import {
  CameraOutlined,
  CarOutlined,
  EditOutlined,
  ExperimentOutlined,
  PlayCircleOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  ScanOutlined,
  SearchOutlined,
  ZoomInOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Tooltip,
  Typography,
} from 'antd';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  createBook,
  DEFAULT_BOOK_HEIGHT_MM,
  listBooks,
} from '@/services/libraryRobot';
import {
  type ArmThermalStatus,
  captureImageUrl,
  getVehicleAlignStatus,
  type LibraryItem,
  visionDebugAlignRun,
  visionDebugSpineCheck,
  type VehicleAlignStatus,
  type VisionAlignRunResult,
  type VisionSpineCheckResult,
  clearArmThermalLockout,
  createCapture,
  getArmThermal,
  getLiftHeight,
  getVisionDebugGap,
  libraryImageUrl,
  manualSegmentCapture,
  moveLift,
  resetArmToDebugPose,
  runVisionDebug,
  type SegmentResult,
  StudioApiError,
  type StudioCapture,
  type StudioSpine,
  listLibrary,
  segmentCapture,
  type VisionDebugGapMeta,
  visionDebugImageUrl,
} from '@/services/libraryRobot/studio';
import type { Book } from '@/services/libraryRobot/types';
import { errorMessage } from '@/utils/errorText';

function formatTimestamp(timestamp: string | undefined) {
  if (!timestamp) return '-';
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime())
    ? timestamp
    : date.toLocaleString('zh-CN', { hour12: false });
}

function revokeUrls(urls: Record<string, string>) {
  for (const url of Object.values(urls)) URL.revokeObjectURL(url);
}

/** Draw the capture with numbered detection outlines for visual review. */
function AnnotatedCapture({
  imageUrl,
  spines,
}: {
  imageUrl: string;
  spines: StudioSpine[];
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;
    const img = new Image();
    img.onload = () => {
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      ctx.drawImage(img, 0, 0);
      const lineWidth = Math.max(2, img.naturalWidth / 400);
      const fontSize = Math.max(16, img.naturalWidth / 36);
      spines.forEach((spine, order) => {
        const color = `hsl(${(order * 47) % 360}, 90%, 42%)`;
        ctx.strokeStyle = color;
        ctx.lineWidth = lineWidth;
        let labelX = spine.bbox_xyxy[0];
        let labelY = spine.bbox_xyxy[1];
        if (spine.polygon_xy.length >= 3) {
          ctx.beginPath();
          ctx.moveTo(spine.polygon_xy[0][0], spine.polygon_xy[0][1]);
          for (const [x, y] of spine.polygon_xy.slice(1)) ctx.lineTo(x, y);
          ctx.closePath();
          ctx.stroke();
          labelX = spine.polygon_xy[0][0];
          labelY = spine.polygon_xy[0][1];
        } else {
          const [x1, y1, x2, y2] = spine.bbox_xyxy;
          ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
        }
        const label = String(spine.index + 1);
        ctx.font = `bold ${fontSize}px sans-serif`;
        const textWidth = ctx.measureText(label).width;
        const boxY = Math.max(0, labelY - fontSize * 1.25 - lineWidth);
        ctx.fillStyle = color;
        ctx.fillRect(labelX, boxY, textWidth + fontSize * 0.6, fontSize * 1.25);
        ctx.fillStyle = '#ffffff';
        ctx.fillText(label, labelX + fontSize * 0.3, boxY + fontSize);
      });
    };
    img.src = imageUrl;
  }, [imageUrl, spines]);

  return (
    <canvas
      ref={canvasRef}
      style={{ maxWidth: '100%', borderRadius: 8, marginBottom: 16 }}
    />
  );
}

type MoveFormValues = {
  targetHeightMm: number;
  speedLevel: number;
};

const MAX_MANUAL_POINTS = 40;

/** Interactive capture image used for hand-drawn spine polygons. */
function ManualCanvas({
  capture,
  imageUrl,
  points,
  onAddPoint,
}: {
  capture: StudioCapture;
  imageUrl: string;
  points: [number, number][];
  onAddPoint: (point: [number, number]) => void;
}) {
  const { message } = App.useApp();
  const [pointer, setPointer] = useState<[number, number]>();

  function imagePoint(
    event: ReactPointerEvent<HTMLDivElement>,
  ): [number, number] {
    const bounds = event.currentTarget.getBoundingClientRect();
    return [
      Math.max(
        0,
        Math.min(
          capture.width - 1,
          Math.round(
            ((event.clientX - bounds.left) / bounds.width) * capture.width,
          ),
        ),
      ),
      Math.max(
        0,
        Math.min(
          capture.height - 1,
          Math.round(
            ((event.clientY - bounds.top) / bounds.height) * capture.height,
          ),
        ),
      ),
    ];
  }

  function handlePointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    const point = imagePoint(event);
    if (points.length >= MAX_MANUAL_POINTS) {
      message.warning(
        `一个多边形最多 ${MAX_MANUAL_POINTS} 个顶点，请先保存或清除当前形状。`,
      );
      return;
    }
    const minDistance = Math.max(capture.width, capture.height) * 0.004;
    if (
      points.some(
        ([x, y]) => Math.hypot(x - point[0], y - point[1]) < minDistance,
      )
    ) {
      message.warning('顶点过于接近已有顶点，请点击下一个轮廓位置。');
      return;
    }
    onAddPoint(point);
    setPointer(point);
  }

  return (
    <div
      role="presentation"
      onPointerDown={handlePointerDown}
      onPointerMove={(event) => {
        if (points.length) setPointer(imagePoint(event));
      }}
      onPointerLeave={() => setPointer(undefined)}
      onPointerCancel={() => setPointer(undefined)}
      style={{
        display: 'inline-block',
        position: 'relative',
        cursor: 'crosshair',
        maxWidth: '100%',
      }}
    >
      <img
        src={imageUrl}
        alt={`第 ${capture.frame_number} 帧拍摄图像`}
        draggable={false}
        style={{ maxWidth: '100%', borderRadius: 8, display: 'block' }}
      />
      <svg
        viewBox={`0 0 ${capture.width} ${capture.height}`}
        preserveAspectRatio="none"
        aria-hidden="true"
        style={{
          position: 'absolute',
          inset: 0,
          width: '100%',
          height: '100%',
          pointerEvents: 'none',
        }}
      >
        {points.length >= 3 ? (
          <polygon
            points={points.map(([x, y]) => `${x},${y}`).join(' ')}
            fill="rgba(22, 119, 255, 0.2)"
            stroke="#1677ff"
            strokeWidth={2}
            vectorEffect="non-scaling-stroke"
          />
        ) : null}
        {points.length === 2 ? (
          <polyline
            points={points.map(([x, y]) => `${x},${y}`).join(' ')}
            fill="none"
            stroke="#1677ff"
            strokeWidth={2}
            vectorEffect="non-scaling-stroke"
          />
        ) : null}
        {pointer && points.length ? (
          <polyline
            points={[...points, pointer].map(([x, y]) => `${x},${y}`).join(' ')}
            fill="none"
            stroke="#faad14"
            strokeWidth={1.5}
            strokeDasharray="6 4"
            vectorEffect="non-scaling-stroke"
          />
        ) : null}
        {points.map(([x, y], index) => (
          <circle
            key={`${x}-${y}`}
            cx={x}
            cy={y}
            r={Math.max(capture.width, capture.height) / 160}
            fill={index === 0 ? '#faad14' : '#1677ff'}
          />
        ))}
      </svg>
    </div>
  );
}

export default function SpineStudioPage() {
  const { message, modal } = App.useApp();
  const [moveForm] = Form.useForm<MoveFormValues>();
  const [liftHeight, setLiftHeight] = useState<number>();
  const [liftError, setLiftError] = useState<string>();
  const [moving, setMoving] = useState(false);
  const [capture, setCapture] = useState<StudioCapture>();
  const [captureUrl, setCaptureUrl] = useState<string>();
  const [capturing, setCapturing] = useState(false);
  const [segmenting, setSegmenting] = useState(false);
  const [segmentResult, setSegmentResult] = useState<SegmentResult>();
  const [spineUrls, setSpineUrls] = useState<Record<string, string>>({});
  const [books, setBooks] = useState<Book[]>([]);
  const [resettingArm, setResettingArm] = useState(false);
  const [thermal, setThermal] = useState<ArmThermalStatus>();
  const [thermalLoading, setThermalLoading] = useState(false);
  const [thermalError, setThermalError] = useState<string>();
  const [clearingLockout, setClearingLockout] = useState(false);
  const [gapMeta, setGapMeta] = useState<VisionDebugGapMeta>();
  const [gapRawUrl, setGapRawUrl] = useState<string>();
  const [gapOverlayUrl, setGapOverlayUrl] = useState<string>();
  const [gapBookId, setGapBookId] = useState<string>();
  const [gapRunning, setGapRunning] = useState(false);
  const [gapLoading, setGapLoading] = useState(false);
  const [gapError, setGapError] = useState<string>();
  // 功能调试（书脊斜度判定 / 车辆居中对齐）
  const [fvLibraryId, setFvLibraryId] = useState<string>();
  const [fvLibrary, setFvLibrary] = useState<LibraryItem[]>([]);
  const [fvChecking, setFvChecking] = useState(false);
  const [fvCheck, setFvCheck] = useState<VisionSpineCheckResult>();
  const [fvCheckError, setFvCheckError] = useState<string>();
  const [fvAligning, setFvAligning] = useState(false);
  const [fvAlign, setFvAlign] = useState<VisionAlignRunResult>();
  const [fvVehicle, setFvVehicle] = useState<VehicleAlignStatus>();
  const [fvVehicleError, setFvVehicleError] = useState<string>();
  const [manualMode, setManualMode] = useState(false);
  const [manualPoints, setManualPoints] = useState<[number, number][]>([]);
  const [manualName, setManualName] = useState('');
  const [manualSaving, setManualSaving] = useState(false);
  const [zoomOpen, setZoomOpen] = useState(false);
  // YOLO 分割结果「命名为书籍」弹窗。
  const [namingSpine, setNamingSpine] = useState<StudioSpine>();
  const [namingName, setNamingName] = useState('');
  const [namingHeightMm, setNamingHeightMm] = useState(DEFAULT_BOOK_HEIGHT_MM);
  const [namingSaving, setNamingSaving] = useState(false);
  // 手动分割图已入库但建书失败：保留 library_id 供本页直接重试命名。
  const [retryImport, setRetryImport] = useState<{
    libraryId: string;
    name: string;
  }>();
  const [retryName, setRetryName] = useState('');
  const [retryHeightMm, setRetryHeightMm] = useState(DEFAULT_BOOK_HEIGHT_MM);
  const [retrySaving, setRetrySaving] = useState(false);

  // 建书时以当前实测升降高度预填（取书高度语义）；读不到时用默认值。
  function defaultBookHeightMm() {
    return liftHeight !== undefined
      ? Math.min(1700, Math.max(10, Math.round(liftHeight)))
      : DEFAULT_BOOK_HEIGHT_MM;
  }

  useEffect(() => {
    let cancelled = false;
    async function pollHeight() {
      try {
        const { height_mm: heightMm } = await getLiftHeight();
        if (!cancelled) {
          setLiftHeight(heightMm);
          setLiftError(undefined);
        }
      } catch (error) {
        if (!cancelled)
          setLiftError(errorMessage(error, '无法读取升降台高度。'));
      }
    }
    void pollHeight();
    const timer = window.setInterval(() => void pollHeight(), 2_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    void refreshBooks();
    void refreshGapDebug();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 功能调试：加载书脊库模板与车端脉冲服务状态
  useEffect(() => {
    let cancelled = false;
    async function loadLibrary() {
      try {
        const data = await listLibrary();
        if (!cancelled) setFvLibrary(data.items);
      } catch {
        if (!cancelled) setFvLibrary([]);
      }
    }
    void loadLibrary();
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshFvVehicle = useCallback(async () => {
    try {
      setFvVehicle(await getVehicleAlignStatus());
      setFvVehicleError(undefined);
    } catch (error) {
      setFvVehicle(undefined);
      setFvVehicleError(errorMessage(error, '车辆对齐服务离线'));
    }
  }, []);

  useEffect(() => {
    void refreshFvVehicle();
    const timer = window.setInterval(() => void refreshFvVehicle(), 5_000);
    return () => window.clearInterval(timer);
  }, [refreshFvVehicle]);

  async function runFvCheck() {
    if (!fvLibraryId) {
      message.warning('请先选择书脊库模板');
      return;
    }
    setFvChecking(true);
    setFvCheckError(undefined);
    try {
      setFvCheck(await visionDebugSpineCheck(fvLibraryId));
    } catch (error) {
      setFvCheck(undefined);
      setFvCheckError(errorMessage(error, '拍摄检测失败'));
    } finally {
      setFvChecking(false);
    }
  }

  async function runFvAlign() {
    if (!fvLibraryId) {
      message.warning('请先选择书脊库模板');
      return;
    }
    setFvAligning(true);
    setFvCheckError(undefined);
    try {
      const result = await visionDebugAlignRun(fvLibraryId);
      setFvAlign(result);
      message.success('对齐流程执行完毕');
    } catch (error) {
      setFvAlign(undefined);
      setFvCheckError(errorMessage(error, '对齐执行失败'));
    } finally {
      setFvAligning(false);
      void refreshFvVehicle();
    }
  }

  async function refreshBooks() {
    try {
      setBooks(await listBooks());
    } catch (error) {
      message.error(errorMessage(error, '无法读取书籍管理。'));
    }
  }

  function confirmMove(values: MoveFormValues) {
    const currentMm = liftHeight;
    const deltaMm =
      currentMm === undefined ? undefined : values.targetHeightMm - currentMm;
    const nearlyNoop = deltaMm !== undefined && Math.abs(deltaMm) < 5;
    modal.confirm({
      title: `确认移动升降台到 ${values.targetHeightMm} mm？`,
      content: (
        <div>
          {deltaMm !== undefined && currentMm !== undefined && (
            <p>
              当前高度 {currentMm.toFixed(1)} mm，预计移动{' '}
              {deltaMm >= 0 ? '上升' : '下降'} {Math.abs(deltaMm).toFixed(1)}{' '}
              mm。
            </p>
          )}
          {nearlyNoop && (
            <p style={{ color: '#faad14' }}>
              目标高度与当前高度几乎一致，升降台可能不会产生肉眼可见的移动。
            </p>
          )}
          <p>
            升降台将执行真实机械运动，到位前接口会一直阻塞。请确认运动范围内无人、升降台周边无干涉且实体急停可触达。
          </p>
        </div>
      ),
      okText: '确认安全并移动',
      cancelText: '再检查一下',
      okButtonProps: { danger: true },
      onOk: () => moveLiftNow(values),
    });
  }

  async function moveLiftNow(values: MoveFormValues) {
    setMoving(true);
    try {
      const result = await moveLift(values.targetHeightMm, values.speedLevel);
      setLiftHeight(result.measured_height_mm);
      message.success(
        `升降台已到位：实测高度 ${result.measured_height_mm} mm。`,
      );
    } catch (error) {
      if (error instanceof StudioApiError && error.status === 409) {
        message.warning('取还书工作流执行中，暂不能移动升降台。');
      } else {
        message.error(errorMessage(error, '升降台移动失败。'));
      }
    } finally {
      setMoving(false);
    }
  }

  async function takeCapture() {
    setCapturing(true);
    try {
      const created = await createCapture();
      const url = await captureImageUrl(created.id);
      setCapture(created);
      setCaptureUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return url;
      });
      setSegmentResult(undefined);
      setManualMode(false);
      setManualPoints([]);
      setSpineUrls((previous) => {
        revokeUrls(previous);
        return {};
      });
      message.success(`已拍摄第 ${created.frame_number} 帧。`);
    } catch (error) {
      if (error instanceof StudioApiError && error.status === 409) {
        message.warning('取还书工作流执行中，暂不能拍摄。');
      } else {
        message.error(errorMessage(error, '拍摄失败。'));
      }
    } finally {
      setCapturing(false);
    }
  }

  async function runSegment() {
    if (!capture) return;
    setSegmenting(true);
    try {
      const result = await segmentCapture(capture.id);
      setSegmentResult(result);
      const entries = await Promise.all(
        result.spines.map(
          async (spine) =>
            [
              spine.library_id,
              await libraryImageUrl(spine.library_id),
            ] as const,
        ),
      );
      const urls = Object.fromEntries(entries);
      setSpineUrls((previous) => {
        revokeUrls(previous);
        return urls;
      });
      if (result.spine_count === 0) {
        message.warning(
          '未识别到书脊：请调整视角、光照或拉近拍摄距离后重新拍摄。',
        );
      } else {
        message.success(`识别到 ${result.spine_count} 个书脊，已保存分割图。`);
      }
      if (result.selfCheck && !result.selfCheck.ok) {
        message.warning(
          `特征不足，建议重拍：${result.selfCheck.detail ?? '分割图可能无法被稳定识别。'}`,
        );
      }
    } catch (error) {
      if (error instanceof StudioApiError && error.status === 503) {
        message.error(`书脊分割暂不可用：${error.message}`);
      } else {
        message.error(errorMessage(error, '书脊分割失败。'));
      }
    } finally {
      setSegmenting(false);
    }
  }

  function toggleManualMode() {
    setManualMode((current) => !current);
    setManualPoints([]);
  }

  function addManualPoint(point: [number, number]) {
    setManualPoints((current) => [...current, point]);
  }

  async function saveManualSegment() {
    if (!capture) return;
    if (manualPoints.length < 3) {
      message.warning('请至少勾勒 3 个顶点。');
      return;
    }
    const xs = manualPoints.map(([x]) => x);
    const ys = manualPoints.map(([, y]) => y);
    const boundsWidth = Math.max(...xs) - Math.min(...xs);
    const boundsHeight = Math.max(...ys) - Math.min(...ys);
    const twiceArea = Math.abs(
      manualPoints.reduce((area, [x, y], index) => {
        const [nextX, nextY] = manualPoints[(index + 1) % manualPoints.length];
        return area + x * nextY - nextX * y;
      }, 0),
    );
    if (
      boundsWidth < capture.width * 0.01 ||
      boundsHeight < capture.height * 0.01 ||
      twiceArea < 0.00002 * capture.width * capture.height
    ) {
      message.warning('多边形书脊范围过小，请重新勾勒。');
      return;
    }
    const name = manualName.trim();
    if (!name) {
      message.warning('请输入书名。');
      return;
    }
    setManualSaving(true);
    try {
      const spine = await manualSegmentCapture(capture.id, {
        name,
        polygon: manualPoints,
      });
      if (spine.selfCheck && !spine.selfCheck.ok) {
        message.warning(
          `特征不足，建议重拍：${spine.selfCheck.detail ?? '该书脊图可能无法被稳定识别。'}`,
        );
      }
      const heightMm = defaultBookHeightMm();
      try {
        await createBook({
          name,
          templateId: spine.library_id,
          heightMm,
        });
        message.success(
          `《${name}》已分割并导入书籍管理（取书高度 ${heightMm} mm）。`,
        );
        setManualPoints([]);
        setManualName('');
        await refreshBooks();
      } catch (bookError) {
        // 书脊图已入库：保留分割结果，弹窗直接重试命名建书，不再要求去书籍管理页登记。
        setRetryImport({ libraryId: spine.library_id, name });
        setRetryName(name);
        setRetryHeightMm(heightMm);
        message.warning(
          `书脊图已分割保存，但导入书籍管理失败：${errorMessage(bookError, '可在弹窗中修正后重试。')}`,
        );
      }
    } catch (error) {
      message.error(errorMessage(error, '保存手动分割书脊失败。'));
    } finally {
      setManualSaving(false);
    }
  }

  async function retryCreateBook() {
    if (!retryImport) return;
    const name = retryName.trim();
    if (!name) {
      message.warning('请输入书名。');
      return;
    }
    setRetrySaving(true);
    try {
      await createBook({
        name,
        templateId: retryImport.libraryId,
        heightMm: retryHeightMm,
      });
      message.success(`《${name}》已导入书籍管理。`);
      setRetryImport(undefined);
      setManualPoints([]);
      setManualName('');
      await refreshBooks();
    } catch (error) {
      message.error(
        errorMessage(error, '导入书籍管理仍失败，请检查书名与高度后重试。'),
      );
    } finally {
      setRetrySaving(false);
    }
  }

  function openNameBook(spine: StudioSpine) {
    setNamingSpine(spine);
    setNamingName('');
    setNamingHeightMm(defaultBookHeightMm());
  }

  async function nameBookAsNew() {
    if (!namingSpine) return;
    const name = namingName.trim();
    if (!name) {
      message.warning('请输入书名。');
      return;
    }
    setNamingSaving(true);
    try {
      if (books.some((book) => book.name === name)) {
        message.warning(`已存在同名书籍《${name}》，请注意区分。`);
      }
      await createBook({
        name,
        templateId: namingSpine.library_id,
        heightMm: namingHeightMm,
      });
      message.success(`《${name}》已导入书籍管理。`);
      setNamingSpine(undefined);
      await refreshBooks();
    } catch (error) {
      message.error(errorMessage(error, '导入书籍管理失败。'));
    } finally {
      setNamingSaving(false);
    }
  }

  function renderManualToolbar(showZoomButton: boolean) {
    return (
      <Space wrap style={{ marginBottom: 12 }}>
        <Typography.Text type="secondary">
          已选 {manualPoints.length} 点
        </Typography.Text>
        <Button
          size="small"
          disabled={!manualPoints.length}
          onClick={() => setManualPoints((current) => current.slice(0, -1))}
        >
          撤销一点
        </Button>
        <Button
          size="small"
          disabled={!manualPoints.length}
          onClick={() => setManualPoints([])}
        >
          清除
        </Button>
        <Input
          size="small"
          placeholder="书名"
          value={manualName}
          maxLength={60}
          style={{ width: 200 }}
          onChange={(event) => setManualName(event.target.value)}
        />
        <Button
          size="small"
          type="primary"
          loading={manualSaving}
          disabled={manualPoints.length < 3 || !manualName.trim()}
          onClick={() => void saveManualSegment()}
        >
          保存并导入书籍管理
        </Button>
        {showZoomButton ? (
          <Button
            size="small"
            icon={<ZoomInOutlined />}
            onClick={() => setZoomOpen(true)}
          >
            放大
          </Button>
        ) : null}
      </Space>
    );
  }

  async function resetArm() {
    setResettingArm(true);
    try {
      const result = await resetArmToDebugPose();
      message.success(
        `机械臂已复位到调试位（rad）：${result.joints_rad
          .map((value) => value.toFixed(3))
          .join(', ')}。`,
      );
    } catch (error) {
      if (error instanceof StudioApiError && error.status === 409) {
        message.warning('取还书服务正忙：任务执行中不能复位机械臂。');
      } else {
        message.error(errorMessage(error, '复位机械臂失败。'));
      }
    } finally {
      setResettingArm(false);
    }
  }

  async function refreshThermal() {
    setThermalLoading(true);
    try {
      setThermal(await getArmThermal());
      setThermalError(undefined);
    } catch (error) {
      setThermalError(errorMessage(error, '无法读取关节温度。'));
    } finally {
      setThermalLoading(false);
    }
  }

  async function clearLockout() {
    setClearingLockout(true);
    try {
      setThermal(await clearArmThermalLockout());
      message.success('温度锁定已解除。');
    } catch (error) {
      message.error(errorMessage(error, '解除锁定失败。'));
    } finally {
      setClearingLockout(false);
    }
  }

  async function loadGapImages() {
    const [rawUrl, overlayUrl] = await Promise.all([
      visionDebugImageUrl('gap_raw'),
      visionDebugImageUrl('gap_overlay'),
    ]);
    setGapRawUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return rawUrl;
    });
    setGapOverlayUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return overlayUrl;
    });
  }

  async function refreshGapDebug() {
    setGapLoading(true);
    try {
      setGapMeta(await getVisionDebugGap());
      await loadGapImages();
      setGapError(undefined);
    } catch (error) {
      setGapError(errorMessage(error, '暂无识别调试记录。'));
    } finally {
      setGapLoading(false);
    }
  }

  async function runGapDebug() {
    setGapRunning(true);
    try {
      const gapBook = books.find((book) => book.id === gapBookId);
      const meta = await runVisionDebug(
        gapBook ? { templateId: gapBook.templateId } : {},
      );
      setGapMeta(meta);
      await loadGapImages();
      setGapError(undefined);
      if (meta.gap_found === false) {
        message.warning(
          `未识别到书缝：${meta.reason ?? '请调整视角后重试'}（书脊匹配结果已显示）。`,
        );
      } else {
        message.success(
          `识别完成：选中${meta.selected_side === 'left' ? '左' : '右'}侧书缝（宽 ${Number(meta.gap_width_px ?? 0).toFixed(0)} px，Y1 = ${(Number(meta.y1_m ?? 0) * 100).toFixed(1)} cm）。`,
        );
      }
    } catch (error) {
      if (error instanceof StudioApiError && error.status === 409) {
        message.warning('取还书工作流执行中，暂不能拍摄识别。');
      } else {
        message.error(errorMessage(error, '拍摄识别失败。'));
      }
    } finally {
      setGapRunning(false);
    }
  }

  return (
    <PageContainer
      title="书脊分割工作台"
      content="控制升降台到目标层高并拍摄书架照片；YOLO 候选可用于快速取书 / 末尾还书，手动勾勒分割并填写书名后直接导入书籍管理。"
    >
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={9}>
          <Card title="升降台控制">
            {liftError ? (
              <Alert
                showIcon
                type="error"
                title="无法读取升降台高度"
                description={liftError}
                style={{ marginBottom: 16 }}
              />
            ) : null}
            <Descriptions column={1} size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label="当前高度">
                {liftHeight !== undefined ? `${liftHeight} mm` : '读取中…'}
              </Descriptions.Item>
            </Descriptions>
            <Form
              form={moveForm}
              layout="vertical"
              initialValues={{ targetHeightMm: 800, speedLevel: 1 }}
              onFinish={confirmMove}
            >
              <Form.Item
                label="目标高度"
                name="targetHeightMm"
                rules={[{ required: true, message: '请输入目标高度' }]}
              >
                <InputNumber
                  min={10}
                  max={1700}
                  precision={0}
                  addonAfter="mm"
                  style={{ width: '100%' }}
                />
              </Form.Item>
              <Form.Item
                label="升降速度"
                name="speedLevel"
                rules={[{ required: true, message: '请选择升降速度' }]}
              >
                <Select
                  options={[
                    { value: 1, label: '1 · 慢速（默认）' },
                    { value: 2, label: '2 · 中速' },
                    { value: 3, label: '3 · 快速' },
                  ]}
                />
              </Form.Item>
              <Button block type="primary" htmlType="submit" loading={moving}>
                移动升降台
              </Button>
            </Form>
            <Popconfirm
              title="复位机械臂到调试位？"
              description="机械臂将使能（如需要）并移动到调试位 [0, 10, -65, 50, 0, 0]°。请确认运动范围内无人、周边无干涉且实体急停可触达。"
              okText="确认复位"
              cancelText="再检查一下"
              okButtonProps={{ danger: true, loading: resettingArm }}
              onConfirm={resetArm}
            >
              <Button block loading={resettingArm} style={{ marginTop: 8 }}>
                复位机械臂到调试位
              </Button>
            </Popconfirm>
          </Card>
        </Col>

        <Col xs={24} xl={15}>
          <Card
            title="拍摄与分割"
            extra={
              <Space wrap>
                <Button
                  icon={<CameraOutlined />}
                  onClick={takeCapture}
                  loading={capturing}
                >
                  开始拍摄
                </Button>
                <Button
                  type="primary"
                  icon={<ScanOutlined />}
                  onClick={runSegment}
                  loading={segmenting}
                  disabled={!capture}
                >
                  开始分割
                </Button>
                <Button
                  icon={<EditOutlined />}
                  onClick={toggleManualMode}
                  disabled={!capture}
                >
                  {manualMode ? '退出手动分割' : '手动分割'}
                </Button>
              </Space>
            }
          >
            {segmenting ? (
              <Alert
                showIcon
                type="info"
                title="正在分割书脊"
                description="YOLO 分割耗时较长，请耐心等待，期间请勿离开本页。"
                style={{ marginBottom: 16 }}
              />
            ) : null}
            {capture && captureUrl ? (
              manualMode ? (
                <>
                  <Alert
                    showIcon
                    type="info"
                    title="手动分割模式"
                    description="在图像上沿书脊轮廓逐点点击（至少 3 个点），输入书名后保存：分割图与书名会直接导入书籍管理；保存后可继续勾勒下一本。"
                    style={{ marginBottom: 16 }}
                  />
                  {renderManualToolbar(true)}
                  <div style={{ marginBottom: 16 }}>
                    <ManualCanvas
                      capture={capture}
                      imageUrl={captureUrl}
                      points={manualPoints}
                      onAddPoint={addManualPoint}
                    />
                  </div>
                </>
              ) : (
                <>
                  {segmentResult ? (
                    <Row gutter={[16, 16]}>
                      <Col xs={24} lg={12}>
                        <Typography.Text type="secondary">
                          原始图像
                        </Typography.Text>
                        <div>
                          <img
                            src={captureUrl}
                            alt={`第 ${capture.frame_number} 帧拍摄图像`}
                            style={{
                              maxWidth: '100%',
                              borderRadius: 8,
                              marginBottom: 16,
                            }}
                          />
                        </div>
                      </Col>
                      <Col xs={24} lg={12}>
                        <Typography.Text type="secondary">
                          识别标注（{segmentResult.spine_count}{' '}
                          个书脊，序号与下方书脊图一一对应）
                        </Typography.Text>
                        <AnnotatedCapture
                          imageUrl={captureUrl}
                          spines={segmentResult.spines}
                        />
                      </Col>
                    </Row>
                  ) : (
                    <img
                      src={captureUrl}
                      alt={`第 ${capture.frame_number} 帧拍摄图像`}
                      style={{
                        maxWidth: '100%',
                        borderRadius: 8,
                        marginBottom: 16,
                      }}
                    />
                  )}
                  <Descriptions
                    column={{ xs: 1, md: 3 }}
                    size="small"
                    style={{ marginBottom: 16 }}
                  >
                    <Descriptions.Item label="帧号">
                      {capture.frame_number}
                    </Descriptions.Item>
                    <Descriptions.Item label="图像尺寸">
                      {capture.width} × {capture.height}
                    </Descriptions.Item>
                    <Descriptions.Item label="拍摄时间">
                      {formatTimestamp(capture.created_at)}
                    </Descriptions.Item>
                  </Descriptions>
                </>
              )
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="尚未拍摄，请先开始拍摄"
              />
            )}
            {segmentResult ? (
              <>
                {segmentResult.spine_count === 0 ? (
                  <Alert
                    showIcon
                    type="warning"
                    title="未识别到书脊"
                    description="请调整视角、光照或拉近拍摄距离后重新拍摄；也可切换手动分割勾勒书脊。"
                    style={{ marginBottom: 16 }}
                  />
                ) : (
                  <Alert
                    showIcon
                    type="success"
                    title={`识别到 ${segmentResult.spine_count} 个书脊`}
                    style={{ marginBottom: 16 }}
                  />
                )}
                {segmentResult.selfCheck && !segmentResult.selfCheck.ok ? (
                  <Alert
                    showIcon
                    type="warning"
                    title="特征不足，建议重拍"
                    description={
                      segmentResult.selfCheck.detail ??
                      '分割出的书脊图特征点过少，识别成功率低，建议重新拍摄。'
                    }
                    style={{ marginBottom: 16 }}
                  />
                ) : null}
                <Row gutter={[8, 8]}>
                  {segmentResult.spines.map((spine) => (
                    <Col key={spine.library_id} xs={8} sm={6} md={4}>
                      <div style={{ textAlign: 'center' }}>
                        {spineUrls[spine.library_id] ? (
                          <img
                            src={spineUrls[spine.library_id]}
                            alt={`书脊 ${spine.index + 1}`}
                            style={{
                              maxWidth: '100%',
                              maxHeight: 160,
                              borderRadius: 8,
                            }}
                          />
                        ) : null}
                        <div>
                          <Typography.Text type="secondary">
                            书脊 {spine.index + 1} · 置信度{' '}
                            {spine.confidence.toFixed(3)}
                          </Typography.Text>
                        </div>
                        <Button
                          size="small"
                          type="link"
                          onClick={() => openNameBook(spine)}
                        >
                          命名为书籍
                        </Button>
                      </div>
                    </Col>
                  ))}
                </Row>
              </>
            ) : null}
          </Card>
        </Col>
      </Row>

      <Card
        title="识别调试（书脊匹配 + 深度书缝）"
        style={{ marginTop: 16 }}
        extra={
          <Space wrap>
            <Select
              style={{ minWidth: 220 }}
              placeholder="参照书籍（默认最新分割图）"
              value={gapBookId}
              onChange={setGapBookId}
              allowClear
              showSearch
              optionFilterProp="label"
              options={books.map((book) => ({
                value: book.id,
                label: `${book.name}（高度 ${book.heightMm} mm）`,
              }))}
              disabled={gapRunning}
            />
            <Button
              icon={<ReloadOutlined />}
              onClick={() => void refreshGapDebug()}
              loading={gapLoading}
            >
              读取最新结果
            </Button>
            <Button
              type="primary"
              icon={<SearchOutlined />}
              onClick={() => void runGapDebug()}
              loading={gapRunning}
            >
              拍摄并识别
            </Button>
          </Space>
        }
      >
        {gapError ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={gapError} />
        ) : null}
        {gapMeta ? (
          <>
            {gapMeta.gap_found === false ? (
              <Alert
                showIcon
                type="info"
                title="本次未识别到书缝"
                description={`${gapMeta.reason ?? '请调整视角后重试'}；已显示识别到的书脊匹配结果。`}
                style={{ marginBottom: 16 }}
              />
            ) : null}
            <Row gutter={[16, 16]}>
              {gapRawUrl ? (
                <Col xs={24} lg={12}>
                  <Typography.Text type="secondary">原始图像</Typography.Text>
                  <div>
                    <img
                      src={gapRawUrl}
                      alt="深度书缝识别原始图像"
                      style={{
                        maxWidth: '100%',
                        borderRadius: 8,
                        marginBottom: 16,
                      }}
                    />
                  </div>
                </Col>
              ) : null}
              {gapOverlayUrl ? (
                <Col xs={24} lg={12}>
                  <Typography.Text type="secondary">
                    识别叠加（书脊多边形 + 扫描行 + 左右缝框 + 选中十字）
                  </Typography.Text>
                  <div>
                    <img
                      src={gapOverlayUrl}
                      alt="深度书缝识别叠加图像"
                      style={{
                        maxWidth: '100%',
                        borderRadius: 8,
                        marginBottom: 16,
                      }}
                    />
                  </div>
                </Col>
              ) : null}
            </Row>
            <Descriptions column={{ xs: 1, md: 3 }} size="small">
              {gapMeta.gap_found !== false ? (
                <>
                  <Descriptions.Item label="选中侧">
                    {gapMeta.selected_side === 'left' ? '左缝' : '右缝'}
                  </Descriptions.Item>
                  <Descriptions.Item label="左缝宽度">
                    {gapMeta.left_gap_width_px === null ||
                    gapMeta.left_gap_width_px === undefined
                      ? '无'
                      : `${gapMeta.left_gap_width_px.toFixed(0)} px`}
                  </Descriptions.Item>
                  <Descriptions.Item label="右缝宽度">
                    {gapMeta.right_gap_width_px === null ||
                    gapMeta.right_gap_width_px === undefined
                      ? '无'
                      : `${gapMeta.right_gap_width_px.toFixed(0)} px`}
                  </Descriptions.Item>
                  <Descriptions.Item label="中线 Y1">
                    {gapMeta.y1_m === undefined
                      ? '-'
                      : `${(gapMeta.y1_m * 100).toFixed(1)} cm`}
                  </Descriptions.Item>
                  <Descriptions.Item label="缝隙中点像素">
                    {gapMeta.gap_center_uv
                      ? `(${gapMeta.gap_center_uv[0]}, ${gapMeta.gap_center_uv[1]})`
                      : '-'}
                  </Descriptions.Item>
                </>
              ) : null}
              <Descriptions.Item label="书脊匹配置信度">
                {gapMeta.spine_confidence === undefined
                  ? '-'
                  : gapMeta.spine_confidence.toFixed(3)}
              </Descriptions.Item>
              <Descriptions.Item label="参考/缝隙深度">
                {gapMeta.reference_depth_m === undefined
                  ? '-'
                  : `${(gapMeta.reference_depth_m * 1000).toFixed(0)} mm`}
                {' / '}
                {gapMeta.gap_depth_m === undefined
                  ? '-'
                  : `${(gapMeta.gap_depth_m * 1000).toFixed(0)} mm`}
              </Descriptions.Item>
              <Descriptions.Item label="扫描参数">
                高度比 {gapMeta.scan_height_ratio ?? '-'} · 带高{' '}
                {gapMeta.scan_band_px ?? '-'} px · 阈值{' '}
                {gapMeta.depth_gap_mm ?? '-'} mm
              </Descriptions.Item>
              <Descriptions.Item label="识别时间">
                {formatTimestamp(
                  typeof gapMeta.timestamp === 'string'
                    ? gapMeta.timestamp
                    : undefined,
                )}
              </Descriptions.Item>
            </Descriptions>
          </>
        ) : null}
      </Card>

      <Card
        title="功能调试（书脊斜度判定 + 车辆居中对齐）"
        style={{ marginTop: 16 }}
        extra={
          <Space wrap>
            <Typography.Text type={fvVehicle ? 'success' : 'danger'}>
              <CarOutlined /> 车辆脉冲服务：
              {fvVehicle
                ? fvVehicle.moving
                  ? `移动中（${fvVehicle.out_linear.toFixed(3)} m/s）`
                  : `在线（${fvVehicle.pulse_speed} m/s · 行程 ${fvVehicle.travel_m_est.toFixed(3)} m${
                      fvVehicle.travel_locked ? ' · 行程闸冷却中' : ''
                    }）`
                : fvVehicleError ?? '离线'}
            </Typography.Text>
            <Select
              style={{ minWidth: 220 }}
              placeholder="选择书脊库模板"
              value={fvLibraryId}
              onChange={setFvLibraryId}
              showSearch
              optionFilterProp="label"
              options={fvLibrary.map((item) => ({
                value: item.id,
                label: item.name || item.id,
              }))}
              disabled={fvChecking || fvAligning}
            />
            <Button
              icon={<ExperimentOutlined />}
              onClick={() => void runFvCheck()}
              loading={fvChecking}
            >
              拍照检测（不动车）
            </Button>
            <Popconfirm
              title="执行一次居中对齐实测？"
              description="车辆将前后缓速脉冲移动直至目标书进入画面正中区域，请现场确认车辆周边安全、升降杆周边无干涉。"
              okText="现场确认并执行"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => void runFvAlign()}
            >
              <Button
                type="primary"
                danger
                icon={<PlayCircleOutlined />}
                loading={fvAligning}
              >
                执行居中对齐（动车）
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        {fvCheckError ? (
          <Alert
            showIcon
            type="error"
            title={fvCheckError}
            style={{ marginBottom: 16 }}
          />
        ) : null}
        {fvCheck && fvCheck.matched === false ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={fvCheck.detail ?? '未识别到书脊'}
          />
        ) : null}
        {fvCheck && fvCheck.matched ? (
          <>
            <Descriptions column={{ xs: 1, md: 3 }} size="small">
              <Descriptions.Item label="书脊倾斜角">
                {fvCheck.tilt_deg === null || fvCheck.tilt_deg === undefined
                  ? '无（兜底识别无倾斜信息）'
                  : `${fvCheck.tilt_deg}°`}
              </Descriptions.Item>
              <Descriptions.Item label="倾斜阈值">
                {fvCheck.tilt_max_deg ?? '-'}°
              </Descriptions.Item>
              <Descriptions.Item label="倾斜判定">
                {fvCheck.tilt_ok ? (
                  <Typography.Text type="success">可执行</Typography.Text>
                ) : (
                  <Typography.Text type="danger">
                    不可执行（超过阈值，任务将报错中止）
                  </Typography.Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="书脊中心位置">
                {fvCheck.center_x_ratio === null ||
                fvCheck.center_x_ratio === undefined
                  ? '-'
                  : `${(fvCheck.center_x_ratio * 100).toFixed(1)}%`}
              </Descriptions.Item>
              <Descriptions.Item label="正中区域">
                {fvCheck.center_band
                  ? `${(fvCheck.center_band[0] * 100).toFixed(0)}% ~ ${(
                      fvCheck.center_band[1] * 100
                    ).toFixed(0)}%`
                  : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="居中判定">
                {fvCheck.in_band === null || fvCheck.in_band === undefined ? (
                  '-'
                ) : fvCheck.in_band ? (
                  <Typography.Text type="success">在正中区域</Typography.Text>
                ) : (
                  <Typography.Text type="warning">
                    偏离正中（作业时将触发车辆缓移对齐）
                  </Typography.Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="匹配置信度">
                {fvCheck.confidence === undefined || fvCheck.confidence === null
                  ? '-'
                  : fvCheck.confidence.toFixed(3)}
              </Descriptions.Item>
              <Descriptions.Item label="capture_id">
                {fvCheck.capture_id ?? '-'}
              </Descriptions.Item>
            </Descriptions>
            {fvCheck.center_x_ratio !== null &&
            fvCheck.center_x_ratio !== undefined &&
            fvCheck.center_band ? (
              <div style={{ marginTop: 12 }}>
                <Typography.Text type="secondary">
                  画面水平位置（竖线为书脊中心，绿色为正中区域）
                </Typography.Text>
                <div
                  style={{
                    position: 'relative',
                    height: 18,
                    borderRadius: 4,
                    background: 'rgba(128,128,128,0.18)',
                    overflow: 'hidden',
                    marginTop: 4,
                  }}
                >
                  <div
                    style={{
                      position: 'absolute',
                      left: `${fvCheck.center_band[0] * 100}%`,
                      width: `${(fvCheck.center_band[1] - fvCheck.center_band[0]) * 100}%`,
                      top: 0,
                      bottom: 0,
                      background: 'rgba(82,196,26,0.35)',
                    }}
                  />
                  <div
                    style={{
                      position: 'absolute',
                      left: `${fvCheck.center_x_ratio * 100}%`,
                      width: 3,
                      top: 0,
                      bottom: 0,
                      marginLeft: -1,
                      background: fvCheck.in_band ? '#52c41a' : '#faad14',
                    }}
                  />
                </div>
              </div>
            ) : null}
          </>
        ) : null}
        {fvAlign ? (
          <>
            <Typography.Title level={5} style={{ marginTop: 16 }}>
              居中对齐实测结果
            </Typography.Title>
            {fvAlign.matched === false ? (
              <Alert
                showIcon
                type="info"
                title={fvAlign.detail ?? '未识别到书脊'}
              />
            ) : (
              <Descriptions column={{ xs: 1, md: 3 }} size="small">
                <Descriptions.Item label="倾斜角">
                  {fvAlign.tilt_deg === null || fvAlign.tilt_deg === undefined
                    ? '无'
                    : `${fvAlign.tilt_deg}°（阈值 ${fvAlign.tilt_max_deg ?? '-'}°）`}
                </Descriptions.Item>
                <Descriptions.Item label="对齐结果">
                  {fvAlign.aligned ? (
                    <Typography.Text type="success">
                      已对齐（移动 {fvAlign.align_moves ?? 0} 次）
                    </Typography.Text>
                  ) : fvAlign.align_skipped ? (
                    `跳过（${fvAlign.align_skipped}）`
                  ) : (
                    '本就在正中区域'
                  )}
                </Descriptions.Item>
                <Descriptions.Item label="最终中心位置">
                  {fvAlign.final_center_x_ratio === null ||
                  fvAlign.final_center_x_ratio === undefined
                    ? '-'
                    : `${(fvAlign.final_center_x_ratio * 100).toFixed(1)}%`}
                </Descriptions.Item>
                <Descriptions.Item label="累计行程（估算）">
                  {fvAlign.align_travel_m === undefined
                    ? '-'
                    : `${(fvAlign.align_travel_m * 1000).toFixed(1)} mm`}
                </Descriptions.Item>
                <Descriptions.Item label="脉冲记录">
                  {!fvAlign.align_pulses || fvAlign.align_pulses.length === 0
                    ? '无'
                    : fvAlign.align_pulses
                        .map(
                          (pulse) =>
                            `${pulse.direction === 'forward' ? '前进' : '后退'} ${
                              pulse.duration_ms
                            }ms`,
                        )
                        .join('；')}
                </Descriptions.Item>
              </Descriptions>
            )}
          </>
        ) : null}
        {!fvCheck && !fvAlign && !fvCheckError ? (
          <Typography.Text type="secondary">
            选择书脊库模板后：「拍照检测」只拍照计算倾斜角与居中位置（不动车）；
            「执行居中对齐」会按当前调参页阈值完整走一遍判定与缓移对齐（车辆会动）。
          </Typography.Text>
        ) : null}
      </Card>

      <Card
        title="关节温度"
        style={{ marginTop: 16 }}
        extra={
          <Button
            icon={<ReloadOutlined />}
            onClick={() => void refreshThermal()}
            loading={thermalLoading}
          >
            刷新
          </Button>
        }
      >
        {thermal?.lockout ? (
          <Alert
            showIcon
            type="error"
            title={`温度保护已锁定：${thermal.lockout.safe_posed ? '已安全归位后失能' : '归位失败直接失能'}`}
            description={thermal.lockout.message}
            style={{ marginBottom: 16 }}
            action={
              <Popconfirm
                title="确认温度已恢复正常？"
                okText="解除锁定"
                cancelText="再等等"
                onConfirm={() => void clearLockout()}
              >
                <Button danger size="small" loading={clearingLockout}>
                  解除锁定
                </Button>
              </Popconfirm>
            }
          />
        ) : null}
        {thermalError ? (
          <Alert showIcon type="error" message={thermalError} />
        ) : null}
        {thermal && Object.keys(thermal.temperatures_c).length ? (
          <Row gutter={[12, 12]}>
            {Array.from({ length: 7 }, (_, index) => {
              const joint = index + 1;
              const value = thermal.temperatures_c[String(joint)];
              const danger = value !== undefined && value >= thermal.limit_c;
              const warn =
                value !== undefined && value >= thermal.limit_c - 15 && !danger;
              return (
                <Col key={joint} xs={12} sm={8} md={6} xl={3}>
                  <Card size="small" styles={{ body: { textAlign: 'center' } }}>
                    <Typography.Text type="secondary">
                      轴 {joint}
                    </Typography.Text>
                    <div>
                      <Typography.Text
                        strong
                        style={{
                          fontSize: 20,
                          color: danger
                            ? '#cf1322'
                            : warn
                              ? '#d46b08'
                              : '#3f8600',
                        }}
                      >
                        {value === undefined ? '--' : `${value.toFixed(0)}°C`}
                      </Typography.Text>
                    </div>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      上限 {thermal.limit_c.toFixed(0)}°C
                    </Typography.Text>
                  </Card>
                </Col>
              );
            })}
          </Row>
        ) : (
          !thermalError && (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="点击刷新读取各轴温度（后台每秒也在持续记录 CSV 日志）"
            />
          )
        )}
      </Card>
      <Modal
        open={zoomOpen && manualMode && Boolean(capture && captureUrl)}
        onCancel={() => setZoomOpen(false)}
        footer={null}
        width="92vw"
        title="手动分割 · 放大视图"
        styles={{
          body: { maxHeight: '80vh', overflow: 'auto', textAlign: 'center' },
        }}
      >
        {capture && captureUrl ? (
          <>
            {renderManualToolbar(false)}
            <ManualCanvas
              capture={capture}
              imageUrl={captureUrl}
              points={manualPoints}
              onAddPoint={addManualPoint}
            />
          </>
        ) : null}
      </Modal>
      <Modal
        destroyOnHidden
        open={namingSpine !== undefined}
        title="命名为书籍"
        okText="导入书籍管理"
        cancelText="取消"
        confirmLoading={namingSaving}
        onCancel={() => setNamingSpine(undefined)}
        onOk={() => void nameBookAsNew()}
      >
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            将书脊 {namingSpine !== undefined ? namingSpine.index + 1 : ''}{' '}
            的分割图登记为一本新书（书脊图已自动关联）。
          </Typography.Text>
          <div>
            <Typography.Text>书名</Typography.Text>
            <Input
              value={namingName}
              maxLength={80}
              placeholder="例如：深入理解计算机系统"
              onChange={(event) => setNamingName(event.target.value)}
            />
          </div>
          <div>
            <Typography.Text>
              取书高度（升降台 mm）{' '}
              <Tooltip title="该书所在层的升降台目标高度，非书本物理高度；已按当前实测升降高度预填。">
                <QuestionCircleOutlined />
              </Tooltip>
            </Typography.Text>
            <InputNumber
              style={{ width: '100%' }}
              min={10}
              max={1700}
              precision={0}
              value={namingHeightMm}
              onChange={(value) =>
                setNamingHeightMm(Number(value ?? DEFAULT_BOOK_HEIGHT_MM))
              }
            />
          </div>
        </Space>
      </Modal>
      <Modal
        destroyOnHidden
        open={retryImport !== undefined}
        title="重试导入书籍管理"
        okText="重试导入"
        cancelText="取消"
        confirmLoading={retrySaving}
        onCancel={() => setRetryImport(undefined)}
        onOk={() => void retryCreateBook()}
      >
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            书脊图已分割保存（无需重新勾勒），修正书名或高度后直接重试。
          </Typography.Text>
          <div>
            <Typography.Text>书名</Typography.Text>
            <Input
              value={retryName}
              maxLength={80}
              onChange={(event) => setRetryName(event.target.value)}
            />
          </div>
          <div>
            <Typography.Text>取书高度（升降台 mm）</Typography.Text>
            <InputNumber
              style={{ width: '100%' }}
              min={10}
              max={1700}
              precision={0}
              value={retryHeightMm}
              onChange={(value) =>
                setRetryHeightMm(Number(value ?? DEFAULT_BOOK_HEIGHT_MM))
              }
            />
          </div>
        </Space>
      </Modal>
    </PageContainer>
  );
}
