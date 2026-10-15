import { describe, expect, it } from 'vitest';

import type { RobotSummary } from '@/services/libraryRobot/types';
import { annotationNavigateTarget } from './annotationNavigate';

function makeRobot(update: Partial<RobotSummary> = {}): RobotSummary {
  return {
    id: 'LR-01',
    name: '灵足一号',
    status: 'idle',
    location: '',
    currentTask: '',
    mapVersion: 'v1',
    mapId: 'MAP-A',
    localizationStatus: 'localized',
    health: {
      navigation: true,
      chassis: true,
      arm: true,
      rod: true,
      imu: true,
      lift: true,
      camera: true,
    },
    updatedAt: '刚刚',
    ...update,
  };
}

describe('annotationNavigateTarget', () => {
  it('requires a selected map', () => {
    const result = annotationNavigateTarget([makeRobot()], undefined);
    expect(result.robot).toBeUndefined();
    expect(result.reason).toBe('请先选择地图。');
  });

  it('requires a robot loaded on the selected map', () => {
    const result = annotationNavigateTarget(
      [makeRobot({ mapId: 'MAP-B' })],
      'MAP-A',
    );
    expect(result.robot).toBeUndefined();
    expect(result.reason).toContain('当前地图没有机器人');
  });

  it('rejects offline-only robots on the map', () => {
    const result = annotationNavigateTarget(
      [makeRobot({ status: 'offline' })],
      'MAP-A',
    );
    expect(result.robot).toBeUndefined();
    expect(result.reason).toBe('当前地图的机器人已离线，不能导航。');
  });

  it('rejects robots that are not localized yet', () => {
    const result = annotationNavigateTarget(
      [makeRobot({ localizationStatus: 'global_localizing' })],
      'MAP-A',
    );
    expect(result.robot).toBeUndefined();
    expect(result.reason).toBe('机器人尚未完成实时定位，不能导航。');
  });

  it('rejects localized robots that are busy', () => {
    const result = annotationNavigateTarget(
      [makeRobot({ status: 'navigating' })],
      'MAP-A',
    );
    expect(result.robot).toBeUndefined();
    expect(result.reason).toBe('机器人正在执行任务，待空闲后才能导航。');
  });

  it('reports arm-busy robots with the dedicated reason', () => {
    const result = annotationNavigateTarget(
      [makeRobot({ status: 'busy' })],
      'MAP-A',
    );
    expect(result.robot).toBeUndefined();
    expect(result.reason).toBe('机械臂作业中，待完成后才能导航。');
  });

  it('rejects robots that are mapping', () => {
    const result = annotationNavigateTarget(
      [makeRobot({ status: 'mapping' })],
      'MAP-A',
    );
    expect(result.robot).toBeUndefined();
    expect(result.reason).toBe('机器人正在建图，结束并恢复导航后才能导航。');
  });

  it('returns the idle localized robot on the map', () => {
    const robot = makeRobot({ id: 'LR-02' });
    const result = annotationNavigateTarget(
      [makeRobot({ status: 'offline' }), robot],
      'MAP-A',
    );
    expect(result.reason).toBeUndefined();
    expect(result.robot?.id).toBe('LR-02');
  });
});
