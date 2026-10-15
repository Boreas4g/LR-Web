import { describe, expect, it } from 'vitest';

import { mapBounds, mapBoundsText, targetWithinMap } from './mapBounds';

const MAP = {
  origin: { x: -10, y: -5, yaw: 0 },
  resolution: 0.05,
  width: 400,
  height: 200,
};

describe('mapBounds', () => {
  it('computes the map-frame coverage from origin/resolution/size', () => {
    expect(mapBounds(MAP)).toEqual({
      minX: -10,
      maxX: 10,
      minY: -5,
      maxY: 5,
    });
  });
});

describe('targetWithinMap', () => {
  it('accepts targets inside the map', () => {
    expect(targetWithinMap(MAP, -10, -5)).toBe(true);
    expect(targetWithinMap(MAP, 0, 0)).toBe(true);
    expect(targetWithinMap(MAP, 10, 5)).toBe(true);
  });

  it('rejects targets outside the map', () => {
    expect(targetWithinMap(MAP, 10.01, 0)).toBe(false);
    expect(targetWithinMap(MAP, 0, -5.01)).toBe(false);
    expect(targetWithinMap(MAP, -10.5, 6)).toBe(false);
  });

  it('passes through when map metadata is missing', () => {
    expect(targetWithinMap(undefined, 999, 999)).toBe(true);
  });
});

describe('mapBoundsText', () => {
  it('renders the range hint', () => {
    expect(mapBoundsText(MAP)).toBe('X ∈ [-10.00, 10.00]，Y ∈ [-5.00, 5.00]');
  });
});
