import type { RobotSummary } from '@/services/libraryRobot/types';

export type AnnotationNavigateTarget = {
  robot?: RobotSummary;
  reason?: string;
};

/**
 * Pick the robot allowed to run annotation navigation on the selected map,
 * or the reason the action must stay disabled. Navigation requires an
 * online robot loaded on this map, fully localized and currently idle.
 */
export function annotationNavigateTarget(
  robots: RobotSummary[],
  mapId?: string,
): AnnotationNavigateTarget {
  if (!mapId) return { reason: '请先选择地图。' };
  const mapRobots = robots.filter((item) => item.mapId === mapId);
  if (!mapRobots.length)
    return { reason: '当前地图没有机器人：请先将机器人「设为当前地图」。' };
  const online = mapRobots.filter((item) => item.status !== 'offline');
  if (!online.length) return { reason: '当前地图的机器人已离线，不能导航。' };
  const localized = online.filter(
    (item) => item.localizationStatus === 'localized',
  );
  if (!localized.length)
    return { reason: '机器人尚未完成实时定位，不能导航。' };
  if (localized.some((item) => item.status === 'mapping'))
    return { reason: '机器人正在建图，结束并恢复导航后才能导航。' };
  if (localized.some((item) => item.status === 'busy'))
    return { reason: '机械臂作业中，待完成后才能导航。' };
  const idle = localized.find((item) => item.status === 'idle');
  if (!idle) return { reason: '机器人正在执行任务，待空闲后才能导航。' };
  return { robot: idle };
}
