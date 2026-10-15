import { cancelCompleteTakeout } from '@/services/libraryRobot';
import type { DispatchTask } from '@/services/libraryRobot/types';
import TaskProgressCard, {
  latestTaskOfType,
  TERMINAL_STATES,
  taskStatusMeta,
} from '../components/TaskProgressCard';

/** Status color/label for the takeout progress card (queued 蓝 / running 金 / completed 绿 / failed 红 / cancelled 灰). */
export const takeoutStatusMeta = taskStatusMeta;

/** The most recently updated takeout task, or undefined when there is none. */
export function latestTakeoutTask(
  tasks: DispatchTask[],
): DispatchTask | undefined {
  return latestTaskOfType(tasks, 'takeout');
}

/** Queued/running/paused tasks can still be cancelled. */
export function isTakeoutTaskActive(status: DispatchTask['status']) {
  return !TERMINAL_STATES.includes(status);
}

/** Latest takeout dispatch task with progress, polled from the control station. */
export default function TakeoutProgressCard({
  refreshKey,
}: {
  /** Change this value to trigger an immediate refresh outside the poll cycle. */
  refreshKey?: number;
}) {
  return (
    <TaskProgressCard
      taskType="takeout"
      title="当前取书任务"
      cancelTask={cancelCompleteTakeout}
      refreshKey={refreshKey}
    />
  );
}
