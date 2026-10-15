/** 裁剪框（图像像素坐标）。 */
export type CropRect = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/** 无参考样本时的书脊宽高比（库中书脊条 w/h 中位数约 0.08）。 */
export const FALLBACK_SPINE_ASPECT = 0.08;

/** 已有书脊图宽高比的中位数：忽略非有限或 ≤0 的尺寸；无有效样本时返回 undefined。 */
export function medianSpineAspect(
  dims: Array<{ width: number; height: number }>,
): number | undefined {
  const aspects = dims
    .filter(
      ({ width, height }) =>
        Number.isFinite(width) &&
        Number.isFinite(height) &&
        width > 0 &&
        height > 0,
    )
    .map(({ width, height }) => width / height)
    .sort((a, b) => a - b);
  if (!aspects.length) return undefined;
  const middle = Math.floor(aspects.length / 2);
  return aspects.length % 2 === 1
    ? aspects[middle]
    : (aspects[middle - 1] + aspects[middle]) / 2;
}

/** 以图像中心为基准、给定宽高比（w/h）的最大内接矩形（图像像素）。 */
export function centeredAspectRect(
  imgW: number,
  imgH: number,
  aspect: number,
): CropRect {
  const safeAspect =
    Number.isFinite(aspect) && aspect > 0 ? aspect : imgW / imgH;
  let width: number;
  let height: number;
  if (imgW / imgH >= safeAspect) {
    height = imgH;
    width = Math.min(imgW, Math.round(imgH * safeAspect));
  } else {
    width = imgW;
    height = Math.min(imgH, Math.round(imgW / safeAspect));
  }
  return {
    x: Math.round((imgW - width) / 2),
    y: Math.round((imgH - height) / 2),
    width,
    height,
  };
}

/** 把裁剪框收进图像范围，并保证最小宽/高（图像小于 min 时收缩到图像尺寸）。 */
export function clampCropRect(
  rect: CropRect,
  imgW: number,
  imgH: number,
  min = 16,
): CropRect {
  const width = Math.max(1, Math.min(imgW, Math.max(min, rect.width)));
  const height = Math.max(1, Math.min(imgH, Math.max(min, rect.height)));
  const x = Math.min(Math.max(0, rect.x), imgW - width);
  const y = Math.min(Math.max(0, rect.y), imgH - height);
  return { x, y, width, height };
}

/** 旋转 angleDeg 后图像外接矩形尺寸（图像像素，向上取整）。 */
export function rotatedBounds(
  imgW: number,
  imgH: number,
  angleDeg: number,
): { width: number; height: number } {
  const rad = (Math.abs(angleDeg) * Math.PI) / 180;
  const cos = Math.abs(Math.cos(rad));
  const sin = Math.abs(Math.sin(rad));
  // cos(90°) 等本应为 0 的场景存在浮点尾差，先吸附邻近整数再向上取整。
  const snapCeil = (value: number) => {
    const nearest = Math.round(value);
    return Math.abs(value - nearest) < 1e-6 ? nearest : Math.ceil(value);
  };
  return {
    width: Math.max(1, snapCeil(imgW * cos + imgH * sin)),
    height: Math.max(1, snapCeil(imgW * sin + imgH * cos)),
  };
}

/**
 * 把源图按角度旋转绘制到目标画布（中心旋转，角度 0 原样绘制）。
 * 非 0 时先填白底：JPEG 导出会把透明三角区变黑，白底对书脊识别更友好。
 * 返回画布在旋转后像素坐标系中的尺寸。
 */
export function drawRotatedImage(
  source: HTMLImageElement,
  canvas: HTMLCanvasElement,
  angleDeg: number,
): { width: number; height: number } {
  const imgW = source.naturalWidth;
  const imgH = source.naturalHeight;
  const size =
    angleDeg === 0 ? { width: imgW, height: imgH } : rotatedBounds(imgW, imgH, angleDeg);
  canvas.width = size.width;
  canvas.height = size.height;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('当前浏览器不支持画布导出。');
  if (angleDeg !== 0) {
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, size.width, size.height);
    ctx.save();
    ctx.translate(size.width / 2, size.height / 2);
    ctx.rotate((angleDeg * Math.PI) / 180);
    ctx.drawImage(source, -imgW / 2, -imgH / 2);
    ctx.restore();
  } else {
    ctx.drawImage(source, 0, 0);
  }
  return size;
}

/** 按裁剪框从源图导出 File：canvas 以原始分辨率绘制，沿用原文件类型（jpeg 质量 0.92）。 */
export async function cropImageToFile(
  source: HTMLImageElement | HTMLCanvasElement,
  rect: CropRect,
  file: File,
): Promise<File> {
  const width = Math.max(1, Math.round(rect.width));
  const height = Math.max(1, Math.round(rect.height));
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('当前浏览器不支持画布导出。');
  ctx.drawImage(
    source,
    Math.round(rect.x),
    Math.round(rect.y),
    width,
    height,
    0,
    0,
    width,
    height,
  );
  const type = file.type || 'image/png';
  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, type, type === 'image/jpeg' ? 0.92 : undefined),
  );
  if (!blob) throw new Error('裁剪后的图片导出失败。');
  return new File([blob], file.name, { type: blob.type || type });
}
