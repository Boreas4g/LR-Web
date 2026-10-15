import { App, Button, InputNumber, Modal, Slider, Space, Typography } from 'antd';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '@/utils/errorText';
import {
  type CropRect,
  centeredAspectRect,
  clampCropRect,
  cropImageToFile,
  drawRotatedImage,
} from './cropUtils';

const MIN_CROP_PX = 16;
/** 手柄命中区（CSS 像素边长），换算成图像像素后做命中测试。 */
const HANDLE_HIT_CSS_PX = 16;
/** 手柄可视尺寸（CSS 像素）。 */
const HANDLE_CSS_PX = 12;

type HandleId = 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w';

// 角手柄排在前面：命中区重叠时角优先于边。
const HANDLES: HandleId[] = ['nw', 'ne', 'se', 'sw', 'n', 'e', 's', 'w'];

const HANDLE_CURSORS: Record<HandleId, string> = {
  nw: 'nwse-resize',
  se: 'nwse-resize',
  ne: 'nesw-resize',
  sw: 'nesw-resize',
  n: 'ns-resize',
  s: 'ns-resize',
  e: 'ew-resize',
  w: 'ew-resize',
};

function handlePoint(rect: CropRect, handle: HandleId): [number, number] {
  const { x, y, width, height } = rect;
  const x2 = x + width;
  const y2 = y + height;
  const cx = x + width / 2;
  const cy = y + height / 2;
  switch (handle) {
    case 'nw':
      return [x, y];
    case 'n':
      return [cx, y];
    case 'ne':
      return [x2, y];
    case 'e':
      return [x2, cy];
    case 'se':
      return [x2, y2];
    case 's':
      return [cx, y2];
    case 'sw':
      return [x, y2];
    case 'w':
      return [x, cy];
  }
}

type DragState = {
  mode: 'move' | HandleId;
  startClientX: number;
  startClientY: number;
  origin: CropRect;
};

type SpineCropModalProps = {
  open: boolean;
  file: File | undefined;
  /** 初始裁剪框的参考宽高比（w/h），来自已有书脊图样本中位数。 */
  referenceAspect: number;
  onConfirm: (file: File) => void;
  onUseOriginal: () => void;
  onCancel: () => void;
};

/** 本地书脊图的浏览器内裁剪：自由比例拖动裁剪框，坐标全部换算为图像像素。 */
export default function SpineCropModal({
  open,
  file,
  referenceAspect,
  onConfirm,
  onUseOriginal,
  onCancel,
}: SpineCropModalProps) {
  const { message } = App.useApp();
  const containerRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const [imageUrl, setImageUrl] = useState<string>();
  const [srcReady, setSrcReady] = useState(false);
  const [angle, setAngle] = useState(0);
  const [imgSize, setImgSize] = useState<{ width: number; height: number }>();
  const [rect, setRect] = useState<CropRect>();
  const [cursor, setCursor] = useState('default');
  const [cropping, setCropping] = useState(false);

  useEffect(() => {
    setImgSize(undefined);
    setRect(undefined);
    setSrcReady(false);
    setAngle(0);
    if (!file) {
      setImageUrl(undefined);
      return;
    }
    const url = URL.createObjectURL(file);
    setImageUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  function handleImageLoad() {
    const img = imgRef.current;
    if (!img) return;
    const { naturalWidth: width, naturalHeight: height } = img;
    if (width < MIN_CROP_PX || height < MIN_CROP_PX) {
      message.warning(
        `图片仅 ${width} × ${height} px，小于最小裁剪尺寸，已使用原图。`,
      );
      onUseOriginal();
      return;
    }
    setSrcReady(true);
  }

  // 源图就绪或角度变化时，把源图旋转绘制到可见画布；裁剪框在旋转后
  // 像素坐标系中工作，尺寸变化时收进新边界（保留用户框选成果）。
  useEffect(() => {
    const img = imgRef.current;
    const canvas = canvasRef.current;
    if (!srcReady || !img || !canvas) return;
    try {
      const size = drawRotatedImage(img, canvas, angle);
      setImgSize(size);
      setRect((prev) =>
        prev
          ? clampCropRect(prev, size.width, size.height, MIN_CROP_PX)
          : centeredAspectRect(size.width, size.height, referenceAspect),
      );
    } catch (error) {
      message.error(errorMessage(error, '旋转图片失败。'));
    }
  }, [srcReady, angle, referenceAspect, message]);

  /** 指针位置 → 图像像素坐标 + 当前显示比例（图像像素 / CSS 像素）。 */
  function toImagePoint(
    event: ReactPointerEvent<HTMLDivElement>,
  ): { x: number; y: number; scale: number } | undefined {
    const bounds = containerRef.current?.getBoundingClientRect();
    if (!bounds || !imgSize || bounds.width === 0) return undefined;
    const scale = imgSize.width / bounds.width;
    return {
      x: (event.clientX - bounds.left) * scale,
      y: (event.clientY - bounds.top) * scale,
      scale,
    };
  }

  function hitHandle(
    point: { x: number; y: number },
    target: CropRect,
    scale: number,
  ): HandleId | undefined {
    const half = (HANDLE_HIT_CSS_PX / 2) * scale;
    for (const handle of HANDLES) {
      const [hx, hy] = handlePoint(target, handle);
      if (Math.abs(point.x - hx) <= half && Math.abs(point.y - hy) <= half) {
        return handle;
      }
    }
    return undefined;
  }

  function insideRect(point: { x: number; y: number }, target: CropRect) {
    return (
      point.x >= target.x &&
      point.x <= target.x + target.width &&
      point.y >= target.y &&
      point.y <= target.y + target.height
    );
  }

  function handlePointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || !rect || !imgSize) return;
    const point = toImagePoint(event);
    if (!point) return;
    const handle = hitHandle(point, rect, point.scale);
    if (!handle && !insideRect(point, rect)) return;
    dragRef.current = {
      mode: handle ?? 'move',
      startClientX: event.clientX,
      startClientY: event.clientY,
      origin: rect,
    };
    setCursor(handle ? HANDLE_CURSORS[handle] : 'move');
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  function handlePointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const point = toImagePoint(event);
    if (!point || !imgSize) return;
    const drag = dragRef.current;
    if (!drag) {
      if (!rect) return;
      const handle = hitHandle(point, rect, point.scale);
      setCursor(
        handle
          ? HANDLE_CURSORS[handle]
          : insideRect(point, rect)
            ? 'move'
            : 'default',
      );
      return;
    }
    const mode = drag.mode;
    const dx = (event.clientX - drag.startClientX) * point.scale;
    const dy = (event.clientY - drag.startClientY) * point.scale;
    const origin = drag.origin;
    const { width: imgW, height: imgH } = imgSize;
    if (mode === 'move') {
      setRect(
        clampCropRect(
          { ...origin, x: origin.x + dx, y: origin.y + dy },
          imgW,
          imgH,
          MIN_CROP_PX,
        ),
      );
      return;
    }
    // 缩放手柄：对侧边/对角锚定，只移动当前手柄拖住的边。
    let x1 = origin.x;
    let y1 = origin.y;
    let x2 = origin.x + origin.width;
    let y2 = origin.y + origin.height;
    if (mode.includes('w')) {
      x1 = Math.min(Math.max(origin.x + dx, 0), x2 - MIN_CROP_PX);
    }
    if (mode.includes('e')) {
      x2 = Math.max(
        Math.min(origin.x + origin.width + dx, imgW),
        x1 + MIN_CROP_PX,
      );
    }
    if (mode.includes('n')) {
      y1 = Math.min(Math.max(origin.y + dy, 0), y2 - MIN_CROP_PX);
    }
    if (mode.includes('s')) {
      y2 = Math.max(
        Math.min(origin.y + origin.height + dy, imgH),
        y1 + MIN_CROP_PX,
      );
    }
    setRect(
      clampCropRect(
        { x: x1, y: y1, width: x2 - x1, height: y2 - y1 },
        imgW,
        imgH,
        MIN_CROP_PX,
      ),
    );
  }

  function endDrag(event: ReactPointerEvent<HTMLDivElement>) {
    dragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  function resetRect() {
    if (imgSize) {
      setRect(
        centeredAspectRect(imgSize.width, imgSize.height, referenceAspect),
      );
    }
  }

  async function confirmCrop() {
    const canvas = canvasRef.current;
    if (!canvas || !rect || !file || !imgSize) return;
    setCropping(true);
    try {
      onConfirm(await cropImageToFile(canvas, rect, file));
    } catch (error) {
      message.error(errorMessage(error, '裁剪图片失败。'));
    } finally {
      setCropping(false);
    }
  }

  return (
    <Modal
      destroyOnHidden
      open={open}
      title="裁剪书脊图"
      width={640}
      onCancel={onCancel}
      footer={
        <Space>
          <Button onClick={onUseOriginal}>使用原图</Button>
          <Button onClick={resetRect} disabled={!imgSize}>
            重置参考比例
          </Button>
          <Button
            type="primary"
            loading={cropping}
            disabled={!rect}
            onClick={() => void confirmCrop()}
          >
            确认裁剪
          </Button>
        </Space>
      }
    >
      <Space orientation="vertical" size={8} style={{ width: '100%' }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          先用滑杆旋转调正书脊（1° 步进），再拖动裁剪框移动位置、拖动边角手柄调整大小（自由比例）。
        </Typography.Text>
        <Space align="center" style={{ width: '100%' }}>
          <Typography.Text style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
            旋转
          </Typography.Text>
          <Slider
            aria-label="旋转角度"
            style={{ flex: 1, minWidth: 120 }}
            min={-45}
            max={45}
            step={1}
            value={angle}
            tooltip={{ formatter: (value) => `${value}°` }}
            onChange={(value) => setAngle(value)}
            disabled={!imgSize}
          />
          <InputNumber
            aria-label="旋转角度数值"
            min={-45}
            max={45}
            step={1}
            value={angle}
            onChange={(value) =>
              setAngle(typeof value === 'number' ? value : 0)
            }
            disabled={!imgSize}
            style={{ width: 76 }}
          />
          <Button
            size="small"
            onClick={() => setAngle(0)}
            disabled={!imgSize || angle === 0}
          >
            角度复位
          </Button>
        </Space>
        <div style={{ textAlign: 'center' }}>
          {imageUrl ? (
            <div
              ref={containerRef}
              role="presentation"
              onPointerDown={handlePointerDown}
              onPointerMove={handlePointerMove}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
              onPointerLeave={() => {
                if (!dragRef.current) setCursor('default');
              }}
              style={{
                display: 'inline-block',
                position: 'relative',
                maxWidth: '100%',
                cursor,
                touchAction: 'none',
              }}
            >
              <img
                key={imageUrl}
                ref={imgRef}
                src={imageUrl}
                alt=""
                aria-hidden="true"
                draggable={false}
                onLoad={handleImageLoad}
                onError={() => {
                  message.error('图片加载失败，无法裁剪。');
                  onCancel();
                }}
                style={{ display: 'none' }}
              />
              <canvas
                ref={canvasRef}
                aria-label="待裁剪书脊图"
                style={{
                  maxWidth: '100%',
                  maxHeight: 520,
                  display: 'block',
                  userSelect: 'none',
                }}
              />
              {imgSize && rect ? (
                <>
                  <svg
                    viewBox={`0 0 ${imgSize.width} ${imgSize.height}`}
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
                    <path
                      d={`M0 0H${imgSize.width}V${imgSize.height}H0Z M${rect.x} ${rect.y}H${rect.x + rect.width}V${rect.y + rect.height}H${rect.x}Z`}
                      fill="rgba(0, 0, 0, 0.45)"
                      fillRule="evenodd"
                    />
                    <rect
                      x={rect.x}
                      y={rect.y}
                      width={rect.width}
                      height={rect.height}
                      fill="none"
                      stroke="#1677ff"
                      strokeWidth={2}
                      vectorEffect="non-scaling-stroke"
                    />
                  </svg>
                  {HANDLES.map((handle) => {
                    const [hx, hy] = handlePoint(rect, handle);
                    return (
                      <div
                        key={handle}
                        aria-hidden="true"
                        style={{
                          position: 'absolute',
                          left: `${(hx / imgSize.width) * 100}%`,
                          top: `${(hy / imgSize.height) * 100}%`,
                          width: HANDLE_CSS_PX,
                          height: HANDLE_CSS_PX,
                          transform: 'translate(-50%, -50%)',
                          background: '#fff',
                          border: '1.5px solid #1677ff',
                          borderRadius: 2,
                          pointerEvents: 'none',
                        }}
                      />
                    );
                  })}
                </>
              ) : null}
            </div>
          ) : null}
        </div>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {rect
            ? `裁剪区域：${Math.round(rect.width)} × ${Math.round(rect.height)} px`
            : '图片加载中…'}
        </Typography.Text>
      </Space>
    </Modal>
  );
}
