import { describe, expect, it, vi } from 'vitest';

vi.mock('@umijs/max', () => ({
  request: vi.fn(),
}));

import {
  mapPoseToPixel,
  pixelToPercent,
  yawToScreenDegrees,
} from './MapPosePreview';

describe('mapPoseToPixel', () => {
  const map = {
    origin: { x: -10, y: -10, yaw: 0 },
    resolution: 0.05,
    height: 367,
  };

  it('places the map origin at the bottom-left pixel', () => {
    const pixel = mapPoseToPixel({ x: -10, y: -10 }, map);
    expect(pixel.x).toBe(0);
    expect(pixel.y).toBe(366);
  });

  it('flips the Y axis (ROS map Y grows upward, pixels downward)', () => {
    const pixel = mapPoseToPixel({ x: 0, y: 0 }, map);
    expect(pixel.x).toBe(200);
    expect(pixel.y).toBe(166);
  });
});

describe('pixelToPercent', () => {
  const map = { width: 272, height: 367 };

  it('converts pixels to percentages of the map box', () => {
    expect(pixelToPercent({ x: 136, y: 0 }, map)).toEqual({ left: 50, top: 0 });
    expect(pixelToPercent({ x: 272, y: 367 }, map)).toEqual({
      left: 100,
      top: 100,
    });
  });

  it('keeps fractional positions proportional', () => {
    const position = pixelToPercent({ x: 68, y: 91.75 }, map);
    expect(position.left).toBeCloseTo(25, 10);
    expect(position.top).toBeCloseTo(25, 10);
  });
});

describe('yawToScreenDegrees', () => {
  it('keeps yaw 0 pointing right', () => {
    expect(yawToScreenDegrees(0)).toBe(0);
  });

  it('rotates map +90° (up) to -90° on screen', () => {
    expect(yawToScreenDegrees(Math.PI / 2)).toBe(-90);
  });

  it('rotates map -90° (down) to +90° on screen', () => {
    expect(yawToScreenDegrees(-Math.PI / 2)).toBe(90);
  });

  it('keeps ±180° pointing left', () => {
    expect(yawToScreenDegrees(Math.PI)).toBe(-180);
    expect(yawToScreenDegrees(-Math.PI)).toBe(180);
  });
});
