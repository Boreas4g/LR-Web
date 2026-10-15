import { describe, expect, it } from 'vitest';

import type { MapAnnotation } from '@/services/libraryRobot/types';

import {
  ANNOTATION_KIND_LABELS,
  annotationKindLabel,
  DEFAULT_ANNOTATION_KIND_FILTER,
  filterAnnotationsByKeyword,
  filterAnnotationsByKind,
  nearestYawDirection8,
  nextAnnotationLabel,
  normalizeDegrees180,
  yawArrowCssDegrees,
} from './annotation';

function annotationOf(
  kind: MapAnnotation['kind'],
  label: string,
): MapAnnotation {
  return {
    id: `ANN-${label}`,
    mapId: 'MAP-TEST',
    kind,
    label,
    pixelX: 0,
    pixelY: 0,
    mapX: 0,
    mapY: 0,
    yaw: 0,
    createdAt: '2026-01-01T00:00:00Z',
    createdBy: 'tester',
  };
}

describe('annotationKindLabel', () => {
  it('maps every kind to its Chinese label', () => {
    expect(ANNOTATION_KIND_LABELS).toEqual({
      shelf: '书架',
      cruise: '巡航',
      dock: '停靠',
      charging: '充电',
      delivery: '交付',
      zone: '区域',
    });
    expect(annotationKindLabel('shelf')).toBe('书架');
    expect(annotationKindLabel('zone')).toBe('区域');
  });
});

describe('filterAnnotationsByKind', () => {
  const annotations = [
    annotationOf('shelf', 'A-01'),
    annotationOf('cruise', 'P-01'),
    annotationOf('shelf', 'A-02'),
  ];

  it('returns every annotation for the all filter', () => {
    expect(filterAnnotationsByKind(annotations, 'all')).toHaveLength(3);
  });

  it('keeps only the requested kind', () => {
    expect(
      filterAnnotationsByKind(annotations, 'shelf').map((item) => item.label),
    ).toEqual(['A-01', 'A-02']);
    expect(
      filterAnnotationsByKind(annotations, 'cruise').map((item) => item.label),
    ).toEqual(['P-01']);
  });

  it('defaults to shelf for target selectors', () => {
    expect(DEFAULT_ANNOTATION_KIND_FILTER).toBe('shelf');
    expect(
      filterAnnotationsByKind(annotations, DEFAULT_ANNOTATION_KIND_FILTER),
    ).toHaveLength(2);
  });
});

describe('filterAnnotationsByKeyword', () => {
  const annotations = [
    annotationOf('shelf', 'A-01 书架'),
    annotationOf('dock', '东门停靠位'),
  ];

  it('matches case-insensitively and trims the query', () => {
    expect(
      filterAnnotationsByKeyword(annotations, ' a-01 ').map((i) => i.label),
    ).toEqual(['A-01 书架']);
  });

  it('returns everything for an empty query', () => {
    expect(filterAnnotationsByKeyword(annotations, '  ')).toHaveLength(2);
  });

  it('matches Chinese substrings', () => {
    expect(
      filterAnnotationsByKeyword(annotations, '停靠').map((i) => i.label),
    ).toEqual(['东门停靠位']);
  });
});

describe('nextAnnotationLabel', () => {
  it('starts at 1 when nothing exists', () => {
    expect(nextAnnotationLabel('shelf', [])).toBe('书架 1');
  });

  it('increments past taken labels', () => {
    const existing = [{ label: '书架 1' }, { label: '书架 2' }];
    expect(nextAnnotationLabel('shelf', existing)).toBe('书架 3');
  });

  it('fills gaps left by deletions', () => {
    const existing = [{ label: '书架 1' }, { label: '书架 3' }];
    expect(nextAnnotationLabel('shelf', existing)).toBe('书架 2');
  });

  it('scopes the sequence per kind name', () => {
    const existing = [{ label: '书架 1' }];
    expect(nextAnnotationLabel('dock', existing)).toBe('停靠 1');
  });
});

describe('normalizeDegrees180', () => {
  it('wraps out-of-range values into [-180, 180)', () => {
    expect(normalizeDegrees180(0)).toBe(0);
    expect(normalizeDegrees180(190)).toBe(-170);
    expect(normalizeDegrees180(-190)).toBe(170);
    expect(normalizeDegrees180(180)).toBe(-180);
    expect(normalizeDegrees180(540)).toBe(-180);
  });

  it('falls back to 0 for non-finite input', () => {
    expect(normalizeDegrees180(Number.NaN)).toBe(0);
    expect(normalizeDegrees180(Number.POSITIVE_INFINITY)).toBe(0);
  });
});

describe('nearestYawDirection8', () => {
  it('snaps to the closest of the 8 compass directions', () => {
    expect(nearestYawDirection8(0)).toBe(0); // 右
    expect(nearestYawDirection8(44)).toBe(1); // 右上
    expect(nearestYawDirection8(90)).toBe(2); // 上
    expect(nearestYawDirection8(-91)).toBe(6); // 下（-90°）
  });

  it('wraps around ±180 consistently (左)', () => {
    expect(nearestYawDirection8(179)).toBe(4);
    expect(nearestYawDirection8(-179)).toBe(4);
  });
});

describe('yawArrowCssDegrees', () => {
  // CSS 旋转角允许等效值（270deg ≡ -90deg），按模 360 比较。
  const equivalent = (actual: number, expected: number) =>
    expect((((actual - expected) % 360) + 360) % 360).toBe(0);

  it('points an up-glyph right at 0°, up at 90°, left at 180°', () => {
    equivalent(yawArrowCssDegrees(0), 90);
    equivalent(yawArrowCssDegrees(90), 0);
    equivalent(yawArrowCssDegrees(180), -90);
    equivalent(yawArrowCssDegrees(-90), 180);
  });
});
