import { describe, expect, it } from 'vitest';

import {
  MAX_MULTI_ROUND_TAKES,
  validateMultiRoundSelection,
} from './multiRound';

describe('validateMultiRoundSelection', () => {
  it('rejects an empty take list', () => {
    expect(validateMultiRoundSelection([], [])).toContain('至少选择一本取书');
  });

  it('rejects more than the slot count allows', () => {
    const takes = Array.from(
      { length: MAX_MULTI_ROUND_TAKES + 1 },
      (_, index) => `BOOK-${index}`,
    );
    expect(validateMultiRoundSelection(takes, [])).toContain('最多');
  });

  it('rejects more returns than takes', () => {
    expect(
      validateMultiRoundSelection(['BOOK-1'], ['BOOK-1', 'BOOK-2']),
    ).toContain('还书数量');
  });

  it('accepts takes up to the slot count with up to the same number of returns', () => {
    expect(validateMultiRoundSelection(['BOOK-1'], [])).toBeUndefined();
    expect(
      validateMultiRoundSelection(['BOOK-1', 'BOOK-2'], ['BOOK-2']),
    ).toBeUndefined();
    const maxTakes = Array.from(
      { length: MAX_MULTI_ROUND_TAKES },
      (_, index) => `BOOK-${index}`,
    );
    expect(validateMultiRoundSelection(maxTakes, maxTakes)).toBeUndefined();
  });
});
