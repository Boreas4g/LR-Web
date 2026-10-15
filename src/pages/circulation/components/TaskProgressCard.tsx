import { ReloadOutlined } from '@ant-design/icons';
import { Link } from '@umijs/max';
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Empty,
  Popconfirm,
  Progress,
  Space,
  Tag,
  Typography,
} from 'antd';
import type { ReactNode } from 'react';
import { useCallback, useEffect, useState } from 'react';

import { dismissBookarmRecovery, pollTasks } from '@/services/libraryRobot';
import type { DispatchTask } from '@/services/libraryRobot/types';
import { errorMessage } from '@/utils/errorText';

/** Tasks in these states no longer transition on their own. */
export const TERMINAL_STATES: DispatchTask['status'][] = [
  'completed',
  'failed',
  'cancelled',
  'recovery_required',
];

/** Status color/label for the orchestration progress cards
 * (queued 蓝 / running 金 / completed 绿 / failed 红 / cancelled 灰 / recovery 橙红). */
export const taskStatusMeta: Record<
  DispatchTask['status'],
  { color: string; label: string }
> = {
  queued: { color: 'blue', label: '排队中' },
  running: { color: 'gold', label: '执行中' },
  paused: { color: 'orange', label: '已暂停' },
  completed: { color: 'green', label: '已完成' },
  failed: { color: 'red', label: '失败' },
  cancelled: { color: 'default', label: '已取消' },
  recovery_required: { color: 'volcano', label: '待人工确认' },
};

/** Parse updatedAt into epoch millis; legacy display strings (e.g.「刚刚」) parse to 0. */
export function taskUpdatedMillis(updatedAt: string): number {
  const parsed = Date.parse(updatedAt);
  return Number.isNaN(parsed) ? 0 : parsed;
}

/** The most recently updated task of one orchestration type. The control
 * station lists newest first, so on equal timestamps the earlier entry wins. */
export function latestTaskOfType(
  tasks: DispatchTask[],
  type: DispatchTask['type'],
): DispatchTask | undefined {
  let latest: DispatchTask | undefined;
  for (const task of tasks) {
    if (task.type !== type) continue;
    if (
      !latest ||
      taskUpdatedMillis(task.updatedAt) > taskUpdatedMillis(latest.updatedAt)
    ) {
      latest = task;
    }
  }
  return latest;
}

/** Latest orchestration task of one type with progress, polled from the control station. */
export default function TaskProgressCard({
  taskType,
  title,
  cancelTask,
  refreshKey,
  renderExtra,
}: {
  taskType: DispatchTask['type'];
  title: string;
  cancelTask: (taskId: string) => Promise<unknown>;
  /** Change this value to trigger an immediate refresh outside the poll cycle. */
  refreshKey?: number;
  /** Optional per-type extra rendering below the progress bar (e.g. multi-round entries). */
  renderExtra?: (task: DispatchTask) => ReactNode;
}) {
  const { message } = App.useApp();
  const [task, setTask] = useState<DispatchTask>();
  const [cancelling, setCancelling] = useState(false);
  const [dismissing, setDismissing] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setTask(latestTaskOfType(await pollTasks(), taskType));
    } catch {
      // 轮询失败保持旧状态。
    }
  }, [taskType]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3_000);
    return () => window.clearInterval(timer);
  }, [refresh, refreshKey]);

  async function cancel() {
    if (!task) return;
    setCancelling(true);
    try {
      await cancelTask(task.id);
      message.warning('已请求取消任务。');
      await refresh();
    } catch (error) {
      message.error(errorMessage(error, '取消任务失败。'));
    } finally {
      setCancelling(false);
    }
  }

  async function dismissRecovery(bookarmTaskId: string) {
    setDismissing(true);
    try {
      await dismissBookarmRecovery(bookarmTaskId);
      message.success('已确认人工处理，机械臂中断任务已清除。');
      await refresh();
    } catch (error) {
      message.error(errorMessage(error, '确认人工处理失败。'));
    } finally {
      setDismissing(false);
    }
  }

  const details = task?.workflowDetails;
  const statusMeta = task ? taskStatusMeta[task.status] : undefined;
  const bookarmTaskId = details?.bookarmTaskId;

  return (
    <Card
      size="small"
      title={title}
      extra={
        <Button
          type="text"
          size="small"
          icon={<ReloadOutlined />}
          onClick={() => void refresh()}
        />
      }
    >
      {task && statusMeta ? (
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <Space size={8} wrap>
            <Typography.Text copyable strong>
              {task.id}
            </Typography.Text>
            <Tag color={statusMeta.color}>{statusMeta.label}</Tag>
          </Space>
          <Progress
            percent={task.progress}
            size="small"
            status={task.status === 'failed' ? 'exception' : undefined}
          />
          {task.workflowMessage ? (
            <Typography.Text type="secondary">
              {task.workflowMessage}
            </Typography.Text>
          ) : null}
          {task.status === 'failed' && task.failureReason ? (
            <Alert
              showIcon
              type="error"
              title="失败原因"
              description={task.failureReason}
            />
          ) : null}
          {task.status === 'recovery_required' ? (
            <Alert
              showIcon
              type="warning"
              title="机械臂中断待确认（可能夹着书）"
              description={
                <Space orientation="vertical" size={8}>
                  {task.failureReason ? (
                    <Typography.Text type="secondary">
                      {task.failureReason}
                    </Typography.Text>
                  ) : null}
                  <Typography.Text type="secondary">
                    请先到现场确认机械臂状态、取下夹持的书，再点击确认。
                  </Typography.Text>
                  <Space wrap size={8}>
                    {bookarmTaskId ? (
                      <Popconfirm
                        title="确认已人工处理？"
                        description="请确认机械臂夹持的书已被取下、机械臂运动范围内无人。"
                        okText="确认已人工处理"
                        cancelText="再想想"
                        okButtonProps={{ danger: true, loading: dismissing }}
                        onConfirm={() => void dismissRecovery(bookarmTaskId)}
                      >
                        <Button
                          size="small"
                          type="primary"
                          loading={dismissing}
                        >
                          确认已人工处理
                        </Button>
                      </Popconfirm>
                    ) : null}
                    <Link to="/debug/arm">前往机械臂调试页</Link>
                  </Space>
                </Space>
              }
            />
          ) : null}
          {details ? (
            <Descriptions column={{ xs: 1, md: 2 }} size="small">
              {details.navigationTaskId ? (
                <Descriptions.Item label="导航任务 ID">
                  {details.navigationTaskId}
                </Descriptions.Item>
              ) : null}
              {details.fineTuneTaskId ? (
                <Descriptions.Item label="精调任务 ID">
                  {details.fineTuneTaskId}
                </Descriptions.Item>
              ) : null}
              {details.bookarmTaskId ? (
                <Descriptions.Item label="机械臂任务 ID">
                  {details.bookarmTaskId}
                </Descriptions.Item>
              ) : null}
              {details.destinationSlot !== undefined ? (
                <Descriptions.Item label="书立槽位">
                  {details.destinationSlot}
                </Descriptions.Item>
              ) : null}
              {details.sourceSlot !== undefined ? (
                <Descriptions.Item label="来源槽位">
                  {details.sourceSlot}
                </Descriptions.Item>
              ) : null}
              {details.liftHeightMm !== undefined ? (
                <Descriptions.Item label="层高">
                  {details.liftHeightMm} mm
                </Descriptions.Item>
              ) : null}
            </Descriptions>
          ) : null}
          {renderExtra?.(task)}
          {!TERMINAL_STATES.includes(task.status) ? (
            <Popconfirm
              title="取消任务？"
              description="导航中将取消导航；机械臂作业中将请求机械臂停止。取消后机械臂可能夹着书停在半路，需要人工现场确认。紧急情况请直接使用实体急停。"
              okText="确认取消"
              cancelText="再想想"
              okButtonProps={{ danger: true, loading: cancelling }}
              onConfirm={() => void cancel()}
            >
              <Button danger loading={cancelling}>
                取消任务
              </Button>
            </Popconfirm>
          ) : null}
        </Space>
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无任务" />
      )}
    </Card>
  );
}
