import { describe, expect, it } from 'vitest';

import {
  centeredAspectRect,
  clampCropRect,
  FALLBACK_SPINE_ASPECT,
  medianSpineAspect,
  rotatedBounds,
} from './cropUtils';

describe('medianSpineAspect', () => {
  it('奇数个样本取中位数', () => {
    expect(
      medianSpineAspect([
        { width: 10, height: 100 },
        { width: 8, height: 100 },
        { width: 9, height: 100 },
      ]),
    ).toBeCloseTo(0.09);
  });

  it('偶数个样本取中间两项均值', () => {
    expect(
      medianSpineAspect([
        { width: 8, height: 100 },
        { width: 12, height: 100 },
      ]),
    ).toBeCloseTo(0.1);
  });

  it('忽略非有限或 ≤0 的尺寸', () => {
    expect(
      medianSpineAspect([
        { width: 0, height: 100 },
        { width: 10, height: 0 },
        { width: -4, height: -50 },
        { width: Number.NaN, height: 100 },
        { width: 10, height: Number.POSITIVE_INFINITY },
        { width: 8, height: 100 },
      ]),
    ).toBeCloseTo(0.08);
  });

  it('无有效样本时返回 undefined', () => {
    expect(medianSpineAspect([])).toBeUndefined();
    expect(medianSpineAspect([{ width: 0, height: 0 }])).toBeUndefined();
  });
});

describe('centeredAspectRect', () => {
  it('宽图 + 窄高比：高度顶满，水平居中', () => {
    expect(centeredAspectRect(1000, 500, FALLBACK_SPINE_ASPECT)).toEqual({
      x: 480,
      y: 0,
      width: 40,
      height: 500,
    });
  });

  it('高图 + 相对更宽的高比：宽度顶满，垂直居中', () => {
    expect(centeredAspectRect(100, 2000, 0.08)).toEqual({
      x: 0,
      y: 375,
      width: 100,
      height: 1250,
    });
  });

  it('高比 > 1：宽图高度顶满，高图宽度顶满', () => {
    expect(centeredAspectRect(1000, 300, 2)).toEqual({
      x: 200,
      y: 0,
      width: 600,
      height: 300,
    });
    expect(centeredAspectRect(800, 500, 2)).toEqual({
      x: 0,
      y: 50,
      width: 800,
      height: 400,
    });
  });

  it('高比与图像一致时返回整图', () => {
    expect(centeredAspectRect(400, 200, 2)).toEqual({
      x: 0,
      y: 0,
      width: 400,
      height: 200,
    });
  });
});

describe('clampCropRect', () => {
  it('越界的裁剪框被拉回图像内', () => {
    expect(
      clampCropRect({ x: -10, y: 450, width: 100, height: 100 }, 500, 500),
    ).toEqual({ x: 0, y: 400, width: 100, height: 100 });
    expect(
      clampCropRect({ x: 480, y: -5, width: 100, height: 100 }, 500, 500),
    ).toEqual({ x: 400, y: 0, width: 100, height: 100 });
  });

  it('小于最小尺寸的裁剪框被放大到 min', () => {
    expect(
      clampCropRect({ x: 10, y: 20, width: 4, height: 2 }, 500, 500),
    ).toEqual({ x: 10, y: 20, width: 16, height: 16 });
    expect(
      clampCropRect({ x: 10, y: 20, width: 4, height: 2 }, 500, 500, 32),
    ).toEqual({ x: 10, y: 20, width: 32, height: 32 });
  });

  it('裁剪框大于图像时收缩到图像尺寸', () => {
    expect(
      clampCropRect({ x: 5, y: 5, width: 600, height: 700 }, 500, 500),
    ).toEqual({ x: 0, y: 0, width: 500, height: 500 });
  });

  it('图像小于 min 时收缩到图像尺寸', () => {
    expect(clampCropRect({ x: 0, y: 0, width: 50, height: 50 }, 8, 12)).toEqual(
      { x: 0, y: 0, width: 8, height: 12 },
    );
  });
});

describe('rotatedBounds', () => {
  it('0° 保持原尺寸', () => {
    expect(rotatedBounds(100, 200, 0)).toEqual({ width: 100, height: 200 });
  });

  it('90° 宽高互换（浮点尾差不放大）', () => {
    expect(rotatedBounds(100, 200, 90)).toEqual({ width: 200, height: 100 });
    expect(rotatedBounds(100, 200, -90)).toEqual({ width: 200, height: 100 });
  });

  it('45° 正方形的外接矩形仍为方形且放大到 √2 倍', () => {
    const { width, height } = rotatedBounds(100, 100, 45);
    expect(width).toBe(height);
    expect(width).toBe(Math.ceil(100 * Math.SQRT2));
  });

  it('正负角度结果一致', () => {
    expect(rotatedBounds(120, 300, -17)).toEqual(rotatedBounds(120, 300, 17));
  });
});
