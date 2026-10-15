import { describe, expect, it } from 'vitest';

import {
  DEFAULT_SPEED_LEVEL,
  normalizeSpeedLevel,
  SPEED_LEVEL_OPTIONS,
} from './tuning';

describe('SPEED_LEVEL_OPTIONS', () => {
  it('offers slow/standard/fast in display order', () => {
    expect(SPEED_LEVEL_OPTIONS.map((option) => option.value)).toEqual([
      'slow',
      'standard',
      'fast',
    ]);
  });
});

describe('normalizeSpeedLevel', () => {
  it('reads the level stored in a profile', () => {
    expect(normalizeSpeedLevel({ speed_level: 'slow' })).toBe('slow');
    expect(normalizeSpeedLevel({ speed_level: 'fast' })).toBe('fast');
    expect(normalizeSpeedLevel({ speed_level: 'standard' })).toBe('standard');
  });

  it('falls back to standard for missing or unexpected values', () => {
    expect(DEFAULT_SPEED_LEVEL).toBe('standard');
    expect(normalizeSpeedLevel({})).toBe('standard');
    expect(normalizeSpeedLevel({ speed_level: 'turbo' })).toBe('standard');
    expect(normalizeSpeedLevel({ speed_level: 3 })).toBe('standard');
  });
});
