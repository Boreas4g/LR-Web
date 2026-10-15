import { describe, expect, it } from 'vitest';

import { validateStressTestActions } from './stressTestTemplates';

describe('validateStressTestActions', () => {
  it('接受两取两还的标准压测序列', () => {
    expect(
      validateStressTestActions(
        [{ destinationSlot: 2 }, { destinationSlot: 1 }],
        [{ sourceSlot: 1 }, { sourceSlot: 2 }],
      ),
    ).toBeNull();
  });

  it('要求至少一步取书动作', () => {
    expect(validateStressTestActions([], [])).toBe('至少需要一步取书动作');
  });

  it('拒绝重复的取书目标槽位', () => {
    expect(
      validateStressTestActions([{ destinationSlot: 1 }, { destinationSlot: 1 }], []),
    ).toBe('取书目标槽位不能重复');
  });

  it('拒绝没有对应取书动作的还书槽位', () => {
    expect(validateStressTestActions([{ destinationSlot: 1 }], [{ sourceSlot: 2 }])).toBe(
      '还书槽位 2 没有对应的取书动作',
    );
  });

  it('拒绝重复的还书槽位（同一槽位的书只能还一次）', () => {
    expect(
      validateStressTestActions(
        [{ destinationSlot: 1 }],
        [{ sourceSlot: 1 }, { sourceSlot: 1 }],
      ),
    ).toBe('还书槽位不能重复');
  });

  it('允许只取不还', () => {
    expect(validateStressTestActions([{ destinationSlot: 3 }], [])).toBeNull();
  });
});
