import { describe, expect, it } from 'vitest';

import {
  buildAnnotationUpdate,
  clampMapScale,
  degreesToRadians,
  IDENTITY_MAP_TRANSFORM,
  inverseMapScale,
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

describe('clampMapScale', () => {
  it('keeps the scale inside [MIN_MAP_SCALE, MAX_MAP_SCALE]', () => {
    expect(clampMapScale(2)).toBe(2);
    expect(clampMapScale(0.5)).toBe(MIN_MAP_SCALE);
    expect(clampMapScale(100)).toBe(MAX_MAP_SCALE);
  });

  it('falls back to MIN_MAP_SCALE for non-finite values', () => {
    expect(clampMapScale(Number.NaN)).toBe(MIN_MAP_SCALE);
    expect(clampMapScale(Number.POSITIVE_INFINITY)).toBe(MIN_MAP_SCALE);
  });
});

describe('zoomMapAt', () => {
  it('keeps the zoom center stationary on screen', () => {
    const current = { scale: 2, x: -40, y: 24 };
    const center = { x: 120, y: 80 };
    const next = zoomMapAt(current, 4, center);
    // The layer-local point under the cursor before zooming...
    const layerPointX = (center.x - current.x) / current.scale;
    const layerPointY = (center.y - current.y) / current.scale;
    // ...must render at the same viewport point after zooming.
    expect(next.x + next.scale * layerPointX).toBeCloseTo(center.x, 10);
    expect(next.y + next.scale * layerPointY).toBeCloseTo(center.y, 10);
  });

  it('clamps the target scale', () => {
    const next = zoomMapAt(IDENTITY_MAP_TRANSFORM, 0.1, { x: 0, y: 0 });
    expect(next.scale).toBe(MIN_MAP_SCALE);
  });

  it('snaps back to the identity transform at the minimum scale', () => {
    const next = zoomMapAt({ scale: 1.2, x: -30, y: 15 }, 1, { x: 10, y: 10 });
    expect(next).toEqual(IDENTITY_MAP_TRANSFORM);
  });
});

describe('panMapBy', () => {
  it('offsets the translation without touching the scale', () => {
    const next = panMapBy({ scale: 3, x: 10, y: -5 }, { x: 7, y: -2 });
    expect(next).toEqual({ scale: 3, x: 17, y: -7 });
  });
});

describe('inverseMapScale', () => {
  it('returns the reciprocal of the zoom factor', () => {
    expect(inverseMapScale(1)).toBe(1);
    expect(inverseMapScale(2)).toBe(0.5);
    expect(inverseMapScale(4)).toBe(0.25);
  });

  it('clamps non-finite or out-of-range input before inverting', () => {
    expect(inverseMapScale(Number.NaN)).toBe(1 / MIN_MAP_SCALE);
    expect(inverseMapScale(100)).toBe(1 / MAX_MAP_SCALE);
  });
});

describe('markerCenterTransform', () => {
  it('keeps the marker centered and counter-scaled', () => {
    expect(markerCenterTransform(1)).toBe('translate(-50%, -50%) scale(1)');
    expect(markerCenterTransform(2)).toBe('translate(-50%, -50%) scale(0.5)');
    expect(markerCenterTransform(8)).toBe('translate(-50%, -50%) scale(0.125)');
  });
});

describe('mapPixelFromClientPoint', () => {
  const map = { width: 100, height: 50 };

  it('maps the top-left and bottom-right corners', () => {
    const bounds = { left: 10, top: 20, width: 400, height: 200 };
    expect(mapPixelFromClientPoint({ x: 10, y: 20 }, bounds, map)).toEqual({
      x: 0,
      y: 0,
    });
    expect(
      mapPixelFromClientPoint({ x: 409.9, y: 219.9 }, bounds, map),
    ).toEqual({ x: 99, y: 49 });
  });

  it('converts correctly when the map is zoomed and panned', () => {
    // Same map rendered at 2x scale, shifted by translation.
    const bounds = { left: -50, top: 60, width: 800, height: 400 };
    // Layer-local center (400px, 200px into the rendered image).
    expect(mapPixelFromClientPoint({ x: 350, y: 260 }, bounds, map)).toEqual({
      x: 50,
      y: 25,
    });
  });

  it('clamps points outside the image to the nearest edge pixel', () => {
    const bounds = { left: 0, top: 0, width: 400, height: 200 };
    expect(mapPixelFromClientPoint({ x: -5, y: 500 }, bounds, map)).toEqual({
      x: 0,
      y: 49,
    });
    expect(mapPixelFromClientPoint({ x: 999, y: -1 }, bounds, map)).toEqual({
      x: 99,
      y: 0,
    });
  });
});

describe('angle conversion', () => {
  it('converts between radians and degrees both ways', () => {
    expect(radiansToDegrees(Math.PI)).toBeCloseTo(180, 10);
    expect(degreesToRadians(-90)).toBeCloseTo(-Math.PI / 2, 10);
    expect(degreesToRadians(radiansToDegrees(1.23))).toBeCloseTo(1.23, 10);
  });
});

describe('normalizeYawDegrees', () => {
  it('keeps in-range angles untouched', () => {
    expect(normalizeYawDegrees(0)).toBe(0);
    expect(normalizeYawDegrees(Math.PI / 2)).toBeCloseTo(90, 10);
    expect(normalizeYawDegrees(-Math.PI / 2)).toBeCloseTo(-90, 10);
  });

  it('wraps angles beyond ±180°', () => {
    expect(normalizeYawDegrees(Math.PI)).toBeCloseTo(-180, 10);
    expect(normalizeYawDegrees(2 * Math.PI)).toBeCloseTo(0, 10);
    expect(normalizeYawDegrees(-3 * Math.PI)).toBeCloseTo(-180, 10);
  });
});

describe('mapCoordsFromPixel', () => {
  const map = {
    width: 100,
    height: 50,
    resolution: 0.05,
    origin: { x: -1, y: -2, yaw: 0 },
  };

  it('matches the backend pixel-to-meter conversion', () => {
    expect(mapCoordsFromPixel(map, { x: 10, y: 5 })).toEqual({
      x: -1 + 10 * 0.05,
      y: -2 + (50 - 1 - 5) * 0.05,
    });
  });

  it('round-trips with mapPixelFromMapCoords for in-range pixels', () => {
    const pixel = { x: 42, y: 7 };
    expect(mapPixelFromMapCoords(map, mapCoordsFromPixel(map, pixel))).toEqual(
      pixel,
    );
  });
});

describe('mapPixelFromMapCoords', () => {
  const map = {
    width: 100,
    height: 50,
    resolution: 0.05,
    origin: { x: -1, y: -2, yaw: 0 },
  };

  it('is the inverse of the backend pixel-to-meter conversion', () => {
    // Pixel (10, 5) → meters: x = -1 + 10*0.05, y = -2 + (50-1-5)*0.05.
    const coords = { x: -1 + 10 * 0.05, y: -2 + (50 - 1 - 5) * 0.05 };
    expect(mapPixelFromMapCoords(map, coords)).toEqual({ x: 10, y: 5 });
  });

  it('maps the map origin to the bottom-left pixel', () => {
    expect(mapPixelFromMapCoords(map, { x: -1, y: -2 })).toEqual({
      x: 0,
      y: 49,
    });
  });

  it('clamps out-of-map coordinates to the nearest edge pixel', () => {
    expect(mapPixelFromMapCoords(map, { x: 100, y: -2 })).toEqual({
      x: 99,
      y: 49,
    });
    expect(mapPixelFromMapCoords(map, { x: -5, y: 100 })).toEqual({
      x: 0,
      y: 0,
    });
  });
});

describe('buildAnnotationUpdate', () => {
  it('trims the label and converts the yaw from degrees to radians', () => {
    expect(
      buildAnnotationUpdate({
        label: '  A-01 书架  ',
        x: 1.234,
        y: -0.5,
        yawDegrees: 90,
      }),
    ).toEqual({
      label: 'A-01 书架',
      x: 1.234,
      y: -0.5,
      yaw: Math.PI / 2,
    });
  });
});

/**
 * 2D affine matrix `[a, b, c, d, e, f]` mapping a point `(x, y)` to
 * `(a·x + c·y + e, b·x + d·y + f)`.
 */
type Affine = [number, number, number, number, number, number];

const IDENTITY_AFFINE: Affine = [1, 0, 0, 1, 0, 0];

/** Compose `outer ∘ inner`: `inner` is applied to local points first. */
function multiplyAffine(outer: Affine, inner: Affine): Affine {
  const [a1, b1, c1, d1, e1, f1] = outer;
  const [a2, b2, c2, d2, e2, f2] = inner;
  return [
    a1 * a2 + c1 * b2,
    b1 * a2 + d1 * b2,
    a1 * c2 + c1 * d2,
    b1 * c2 + d1 * d2,
    a1 * e2 + c1 * f2 + e1,
    b1 * e2 + d1 * f2 + f1,
  ];
}

function applyAffine(matrix: Affine, point: { x: number; y: number }) {
  return {
    x: matrix[0] * point.x + matrix[2] * point.y + matrix[4],
    y: matrix[1] * point.x + matrix[3] * point.y + matrix[5],
  };
}

/**
 * Minimal ordered interpreter for the exact transform lists this module emits
 * (`translate(x, y)`, `scale(k)`, `rotate(θdeg)`; translate accepts px or %,
 * percentages resolved against the element's own size). CSS applies the list
 * left-associatively around the element's transform-origin.
 */
function cssTransformMatrix(
  transform: string,
  origin: { x: number; y: number },
  element: { width: number; height: number },
): Affine {
  let list = IDENTITY_AFFINE;
  for (const match of transform.matchAll(
    /(translate|scale|rotate)\(([^)]*)\)/g,
  )) {
    const args = match[2].split(',').map((value) => value.trim());
    let next: Affine;
    if (match[1] === 'translate') {
      const [tx, ty] = args.map((value, index) => {
        if (!value.endsWith('%')) return Number.parseFloat(value);
        const basis = index === 0 ? element.width : element.height;
        return (Number.parseFloat(value) / 100) * basis;
      });
      next = [1, 0, 0, 1, tx, ty];
    } else if (match[1] === 'scale') {
      const factor = Number.parseFloat(args[0]);
      next = [factor, 0, 0, factor, 0, 0];
    } else {
      const radians = (Number.parseFloat(args[0]) * Math.PI) / 180;
      const cos = Math.cos(radians);
      const sin = Math.sin(radians);
      next = [cos, sin, -sin, cos, 0, 0];
    }
    list = multiplyAffine(list, next);
  }
  return multiplyAffine(
    [1, 0, 0, 1, origin.x, origin.y],
    multiplyAffine(list, [1, 0, 0, 1, -origin.x, -origin.y]),
  );
}

describe('constant-size marker composition', () => {
  // A point overlay anchored at (anchor.x, anchor.y) in layer coordinates,
  // counter-scaled by markerCenterTransform inside the zoomed map layer.
  const element = { width: 34, height: 50 };
  const origin = { x: element.width / 2, y: element.height / 2 };
  const anchor = { x: 123, y: 456 };
  const ZOOMS = [MIN_MAP_SCALE, 2, 4.7, MAX_MAP_SCALE];

  function screenMatrix(
    transform: MapViewTransform,
    elementCss: string,
  ): Affine {
    const layer = cssTransformMatrix(
      mapLayerTransform(transform),
      { x: 0, y: 0 },
      element,
    );
    const marker = cssTransformMatrix(elementCss, origin, element);
    return multiplyAffine(
      layer,
      multiplyAffine([1, 0, 0, 1, anchor.x, anchor.y], marker),
    );
  }

  it('keeps the marker center on the transformed anchor at any zoom', () => {
    for (const scale of ZOOMS) {
      const transform = { scale, x: -30, y: 55 };
      const center = applyAffine(
        screenMatrix(transform, markerCenterTransform(scale)),
        origin,
      );
      const layer = cssTransformMatrix(
        mapLayerTransform(transform),
        { x: 0, y: 0 },
        element,
      );
      const expected = applyAffine(layer, anchor);
      expect(center.x).toBeCloseTo(expected.x, 10);
      expect(center.y).toBeCloseTo(expected.y, 10);
    }
  });

  it('renders the marker at its 1x pixel size at any zoom', () => {
    for (const scale of ZOOMS) {
      const matrix = screenMatrix(
        transform(scale),
        markerCenterTransform(scale),
      );
      const left = applyAffine(matrix, { x: 0, y: origin.y });
      const right = applyAffine(matrix, { x: element.width, y: origin.y });
      expect(Math.hypot(right.x - left.x, right.y - left.y)).toBeCloseTo(
        element.width,
        10,
      );
    }
  });

  it('keeps a rotated heading arrow at constant length and angle', () => {
    const yawDegrees = 45;
    for (const scale of ZOOMS) {
      // 与 DOM 嵌套一致的顺序：外层先反向缩放，内层再做 yaw 旋转。
      const matrix = screenMatrix(
        transform(scale),
        `${markerCenterTransform(scale)} rotate(${yawDegrees}deg)`,
      );
      const tail = applyAffine(matrix, origin);
      const tip = applyAffine(matrix, { x: origin.x + 10, y: origin.y });
      expect(Math.hypot(tip.x - tail.x, tip.y - tail.y)).toBeCloseTo(10, 10);
      expect(
        (Math.atan2(tip.y - tail.y, tip.x - tail.x) * 180) / Math.PI,
      ).toBeCloseTo(yawDegrees, 10);
    }
  });

  function transform(scale: number): MapViewTransform {
    return { scale, x: -30, y: 55 };
  }
});
