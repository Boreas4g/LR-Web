import { describe, expect, it, vi } from 'vitest';

vi.mock('@umijs/max', () => ({
  request: vi.fn(),
}));

import type { DispatchTask } from '@/services/libraryRobot/types';
import {
  isTakeoutTaskActive,
  latestTakeoutTask,
  takeoutStatusMeta,
} from './TakeoutProgressCard';

function makeTask(partial: Partial<DispatchTask>): DispatchTask {
  return {
    id: 'TSK-1',
    type: 'takeout',
    robotId: 'LR-01',
    target: '',
    status: 'queued',
    progress: 0,
    updatedAt: '2026-08-20T10:00:00Z',
    ...partial,
  };
}

describe('latestTakeoutTask', () => {
  it('returns undefined when there is no takeout task', () => {
    expect(latestTakeoutTask([])).toBeUndefined();
    expect(
      latestTakeoutTask([
        makeTask({ type: 'return' }),
        makeTask({ type: 'cruise' }),
      ]),
    ).toBeUndefined();
  });

  it('returns the only takeout task', () => {
    const task = makeTask({ id: 'TSK-9' });
    expect(latestTakeoutTask([makeTask({ type: 'inventory' }), task])).toBe(
      task,
    );
  });

  it('picks the takeout task with the newest updatedAt', () => {
    const older = makeTask({ id: 'TSK-1', updatedAt: '2026-08-20T09:00:00Z' });
    const newer = makeTask({ id: 'TSK-2', updatedAt: '2026-08-20T11:00:00Z' });
    expect(latestTakeoutTask([older, newer])).toBe(newer);
    expect(latestTakeoutTask([newer, older])).toBe(newer);
  });

  it('picks the truly newest task from a newest-first ISO list', () => {
    // The control station inserts new tasks at the head with ISO updatedAt.
    const newest = makeTask({
      id: 'TSK-3',
      updatedAt: '2026-09-29T08:00:01.500000+00:00',
    });
    const middle = makeTask({
      id: 'TSK-2',
      updatedAt: '2026-09-29T08:00:01.400000+00:00',
    });
    const oldest = makeTask({
      id: 'TSK-1',
      updatedAt: '2026-09-29T07:59:59.900000+00:00',
    });
    expect(latestTakeoutTask([newest, middle, oldest])).toBe(newest);
  });

  it('prefers the earlier list entry when timestamps tie', () => {
    const first = makeTask({ id: 'TSK-1', updatedAt: '2026-08-20T10:00:00Z' });
    const second = makeTask({ id: 'TSK-2', updatedAt: '2026-08-20T10:00:00Z' });
    expect(latestTakeoutTask([first, second])).toBe(first);
  });

  it('treats legacy display strings as older than ISO timestamps', () => {
    const legacy = makeTask({ id: 'TSK-1', updatedAt: '刚刚' });
    const iso = makeTask({ id: 'TSK-2', updatedAt: '2026-08-20T10:00:00Z' });
    expect(latestTakeoutTask([legacy, iso])).toBe(iso);
  });

  it('ignores newer tasks of other types', () => {
    const takeout = makeTask({
      id: 'TSK-1',
      updatedAt: '2026-08-20T09:00:00Z',
    });
    const other = makeTask({
      id: 'TSK-2',
      type: 'return',
      updatedAt: '2026-08-20T12:00:00Z',
    });
    expect(latestTakeoutTask([takeout, other])).toBe(takeout);
  });
});

describe('takeoutStatusMeta', () => {
  it('maps every dispatch status to the required colors', () => {
    expect(takeoutStatusMeta.queued.color).toBe('blue');
    expect(takeoutStatusMeta.running.color).toBe('gold');
    expect(takeoutStatusMeta.completed.color).toBe('green');
    expect(takeoutStatusMeta.failed.color).toBe('red');
    expect(takeoutStatusMeta.cancelled.color).toBe('default');
  });

  it('provides a Chinese label for every status', () => {
    for (const meta of Object.values(takeoutStatusMeta)) {
      expect(meta.label).toBeTruthy();
    }
  });
});

describe('isTakeoutTaskActive', () => {
  it('treats queued/running/paused as active', () => {
    expect(isTakeoutTaskActive('queued')).toBe(true);
    expect(isTakeoutTaskActive('running')).toBe(true);
    expect(isTakeoutTaskActive('paused')).toBe(true);
  });

  it('treats terminal states as inactive', () => {
    expect(isTakeoutTaskActive('completed')).toBe(false);
    expect(isTakeoutTaskActive('failed')).toBe(false);
    expect(isTakeoutTaskActive('cancelled')).toBe(false);
  });
});
