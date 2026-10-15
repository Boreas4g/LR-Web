import { RocketOutlined } from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  InputNumber,
  Popconfirm,
  Progress,
  Radio,
  Select,
  Space,
  Tag,
  Typography,
} from 'antd';
import { useEffect, useState } from 'react';

import {
  cancelMultiRound,
  createMultiRound,
  getMapAnnotations,
  getMapVersions,
  getRobots,
  listBooks,
  pollTasks,
} from '@/services/libraryRobot';
import type {
  Book,
  DispatchTask,
  MapAnnotation,
  MapVersion,
  MultiRoundBookEntry,
  RobotSummary,
} from '@/services/libraryRobot/types';
import { errorMessage } from '@/utils/errorText';
import AnnotationTargetSelect from '../components/AnnotationTargetSelect';
import MapPosePreview from '../components/MapPosePreview';
import { mapBoundsText, targetWithinMap } from '../components/mapBounds';
import TaskProgressCard from '../components/TaskProgressCard';
import VisionImage from '../components/VisionImage';
import {
  MAX_MULTI_ROUND_TAKES,
  validateMultiRoundSelection,
} from './multiRound';

const TERMINAL_STATES = [
  'completed',
  'failed',
  'cancelled',
  'recovery_required',
];

function entryTagColor(status?: string) {
  if (status === 'completed') return 'success';
  if (status === 'running') return 'processing';
  return 'default';
}

function entryStatusText(status?: string) {
  if (status === 'completed') return '完成';
  if (status === 'running') return '执行中';
  return '等待';
}

/**
 * Multi-round take/return: optionally navigate once to the target (default),
 * then take N books into stand slots N..1 (inside-out; three books -> 3/2/1)
 * and return the book in slot 1/2/3 to the 1st/2nd/3rd return target,
 * without moving the chassis again.
 */
export default function MultiRoundPage() {
  const { message } = App.useApp();
  const [books, setBooks] = useState<Book[]>([]);
  const [annotations, setAnnotations] = useState<MapAnnotation[]>([]);
  const [maps, setMaps] = useState<MapVersion[]>([]);
  const [robot, setRobot] = useState<RobotSummary>();
  const [mapId, setMapId] = useState<string>();
  const [robotId, setRobotId] = useState<string>('LR-01');
  const [takeBookIds, setTakeBookIds] = useState<string[]>([]);
  const [returnBookIds, setReturnBookIds] = useState<string[]>([]);
  const [annotationId, setAnnotationId] = useState<string>();
  const [manualMode, setManualMode] = useState(false);
  const [manualTarget, setManualTarget] = useState({ x: 0, y: 0, yaw: 0 });
  const [sendNavigation, setSendNavigation] = useState(true);
  const [activeTaskId, setActiveTaskId] = useState<string>();
  const [activeTask, setActiveTask] = useState<DispatchTask>();
  const [submitting, setSubmitting] = useState(false);
  const [loadingOptions, setLoadingOptions] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const [bookList, robots, mapList] = await Promise.all([
          listBooks(),
          getRobots(),
          getMapVersions(),
        ]);
        setBooks(bookList);
        setMaps(mapList);
        const first = robots[0];
        if (first) {
          setRobotId(first.id);
          setRobot(first);
        }
        const chosenMap =
          mapList.find((item) => item.id === first?.mapId) ?? mapList[0];
        if (chosenMap) {
          setMapId(chosenMap.id);
          setAnnotations(await getMapAnnotations(chosenMap.id));
        }
      } catch (error) {
        message.error(errorMessage(error, '无法加载多轮取还书选项。'));
      } finally {
        setLoadingOptions(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Refresh the robot pose for the map preview; failures keep the old pose.
  useEffect(() => {
    let cancelled = false;
    async function refreshRobot() {
      try {
        const robots = await getRobots();
        if (!cancelled)
          setRobot(robots.find((item) => item.id === robotId) ?? robots[0]);
      } catch {
        // 轮询失败保持旧状态。
      }
    }
    void refreshRobot();
    const timer = window.setInterval(() => void refreshRobot(), 3_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [robotId]);

  // Poll the orchestrated task until it reaches a terminal state.
  useEffect(() => {
    if (!activeTaskId) return;
    let stopped = false;
    let timer = 0;
    const tick = async () => {
      try {
        const tasks = await pollTasks();
        const task = tasks.find((item) => item.id === activeTaskId);
        if (task) {
          setActiveTask(task);
          if (TERMINAL_STATES.includes(task.status)) {
            stopped = true;
          }
        }
      } catch {
        // 轮询失败保持旧状态。
      }
      if (!stopped) timer = window.setTimeout(() => void tick(), 2000);
    };
    void tick();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [activeTaskId]);

  const selectedAnnotation = annotations.find(
    (item) => item.id === annotationId,
  );
  const target = manualMode
    ? manualTarget
    : selectedAnnotation
      ? {
          x: selectedAnnotation.mapX,
          y: selectedAnnotation.mapY,
          yaw: selectedAnnotation.yaw,
        }
      : undefined;
  const taskActive =
    activeTask !== undefined && !TERMINAL_STATES.includes(activeTask.status);
  const robotBusy = robot?.status === 'busy' || robot?.status === 'mapping';
  const bookOptions = books.map((book) => ({
    value: book.id,
    label: `${book.name}（高度 ${book.heightMm} mm）`,
  }));
  const selectionError = validateMultiRoundSelection(
    takeBookIds,
    returnBookIds,
  );
  const selectedMap = maps.find((item) => item.id === mapId);
  const manualOutOfBounds =
    sendNavigation &&
    manualMode &&
    !targetWithinMap(selectedMap, manualTarget.x, manualTarget.y);
  const canSubmit =
    !taskActive &&
    !robotBusy &&
    !submitting &&
    !manualOutOfBounds &&
    (!sendNavigation || target !== undefined) &&
    selectionError === undefined;

  function onTakeChange(values: string[]) {
    setTakeBookIds(values);
    // Returns consume slots 1..M in order, so they can never outnumber takes.
    setReturnBookIds((previous) => previous.slice(0, values.length));
    // 选中带取书点的书时自动带出导航目标（多书时以最后选中的为准，仍可手改）。
    const added = [...values].reverse().find((id) => {
      const book = books.find((item) => item.id === id);
      return book?.annotationId;
    });
    if (added) {
      setAnnotationId(
        books.find((item) => item.id === added)?.annotationId ?? undefined,
      );
    }
  }

  function onReturnChange(values: string[]) {
    setReturnBookIds(values);
    if (annotationId) return;
    const added = [...values].reverse().find((id) => {
      const book = books.find((item) => item.id === id);
      return book?.annotationId;
    });
    if (added) {
      setAnnotationId(
        books.find((item) => item.id === added)?.annotationId ?? undefined,
      );
    }
  }

  async function start() {
    if (sendNavigation && !target) return;
    setSubmitting(true);
    try {
      const task = await createMultiRound({
        robotId,
        target: sendNavigation ? target : undefined,
        takeBookIds,
        returnBookIds,
        liftSpeedLevel: 2,
        mapId,
        annotationId: sendNavigation && !manualMode ? annotationId : undefined,
        sendNavigation,
      });
      setActiveTaskId(task.id);
      setActiveTask(task);
      message.success(
        sendNavigation
          ? '多轮取还书任务已开始：先导航，到位后依次取书、还书。'
          : '多轮取还书任务已开始：跳过导航，原地依次取书、还书。',
      );
    } catch (error) {
      message.error(errorMessage(error, '多轮取还书任务创建失败。'));
    } finally {
      setSubmitting(false);
    }
  }

  async function cancelActive() {
    if (!activeTaskId) return;
    try {
      await cancelMultiRound(activeTaskId);
      message.warning('已请求取消多轮取还书任务。');
    } catch (error) {
      message.error(errorMessage(error, '取消失败。'));
    }
  }

  function renderEntries(title: string, entries?: MultiRoundBookEntry[]) {
    if (!entries || entries.length === 0) return null;
    const withImages = entries.filter((entry) => entry.visionImages);
    return (
      <>
        <Space wrap size={4}>
          <Typography.Text type="secondary">{title}</Typography.Text>
          {entries.map((entry) => (
            <Tag key={entry.slot} color={entryTagColor(entry.status)}>
              槽位 {entry.slot}《{entry.name}》{entryStatusText(entry.status)}
            </Tag>
          ))}
        </Space>
        {withImages.length > 0 ? (
          <Space wrap size={16} style={{ marginTop: 4 }}>
            {withImages.map((entry) => (
              <span key={entry.slot}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  槽位 {entry.slot}《{entry.name}》
                </Typography.Text>
                <br />
                <Space size={8}>
                  {entry.visionImages?.spine ? (
                    <VisionImage
                      url={entry.visionImages.spine}
                      label="书脊识别"
                    />
                  ) : null}
                  {entry.visionImages?.gap ? (
                    <VisionImage url={entry.visionImages.gap} label="书缝" />
                  ) : null}
                </Space>
              </span>
            ))}
          </Space>
        ) : null}
      </>
    );
  }

  return (
    <PageContainer
      title="多轮取还书"
      content="选择最多五本取书与若干还书目标书；默认先导航一次到目标点（可取消勾选改为原地直接开始）。取书从内向外放：N 本依次放入 N..1 号槽位（5 本即 5、4、3、2、1 号）；还书从外向内取：第 1 个还书目标用 1 号槽位的书、第 2 个用 2 号、…、第 5 个用 5 号；全程底盘不再移动。"
    >
      <Card
        size="small"
        title="多轮取还书（导航一次或原地开始，连续取还）"
        style={{ marginBottom: 16 }}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Checkbox
            checked={sendNavigation}
            onChange={(event) => setSendNavigation(event.target.checked)}
            disabled={taskActive}
          >
            发送导航（勾选后先导航到目标点并低速精调；不勾选则底盘不动，原地直接开始升降取还书）
          </Checkbox>
          {sendNavigation ? (
            <Space wrap size={[12, 8]}>
              <Radio.Group
                size="small"
                value={manualMode ? 'manual' : 'annotation'}
                onChange={(event) =>
                  setManualMode(event.target.value === 'manual')
                }
                options={[
                  { value: 'annotation', label: '地图标注' },
                  { value: 'manual', label: '手填坐标' },
                ]}
                optionType="button"
                disabled={taskActive}
              />
              {!manualMode ? (
                <AnnotationTargetSelect
                  annotations={annotations}
                  value={annotationId}
                  onChange={setAnnotationId}
                  loading={loadingOptions}
                  disabled={taskActive}
                />
              ) : (
                <Space wrap size={4}>
                  {(['x', 'y', 'yaw'] as const).map((axis) => (
                    <InputNumber
                      key={axis}
                      addonBefore={axis.toUpperCase()}
                      size="small"
                      step={0.1}
                      precision={2}
                      value={manualTarget[axis]}
                      onChange={(value) =>
                        setManualTarget((previous) => ({
                          ...previous,
                          [axis]: Number(value ?? 0),
                        }))
                      }
                      style={{ width: 130 }}
                      disabled={taskActive}
                    />
                  ))}
                </Space>
              )}
            </Space>
          ) : null}
          <Space wrap size={[12, 8]}>
            <Select
              mode="multiple"
              style={{ minWidth: 320 }}
              placeholder={`取书（最多 ${MAX_MULTI_ROUND_TAKES} 本；从内向外放入槽位：5 本时依次为 5、4、3、2、1 号）`}
              value={takeBookIds}
              onChange={onTakeChange}
              loading={loadingOptions}
              options={bookOptions}
              maxCount={MAX_MULTI_ROUND_TAKES}
              showSearch
              optionFilterProp="label"
              disabled={taskActive}
            />
            <Select
              mode="multiple"
              style={{ minWidth: 320 }}
              placeholder="还书目标书（可选；第 1 个目标用 1 号槽位的书、第 2 个用 2 号、…、第 5 个用 5 号，还到目标书左边）"
              value={returnBookIds}
              onChange={onReturnChange}
              options={bookOptions}
              maxCount={Math.max(takeBookIds.length, 1)}
              showSearch
              optionFilterProp="label"
              disabled={taskActive || takeBookIds.length === 0}
            />
          </Space>
          {books.length === 0 && !loadingOptions ? (
            <Alert
              type="warning"
              showIcon
              title="还没有登记书籍，请先到「书籍管理」登记书名、书脊图与书高度。"
            />
          ) : null}
          {selectionError && takeBookIds.length > 0 ? (
            <Alert type="warning" showIcon title={selectionError} />
          ) : null}
          {manualOutOfBounds ? (
            <Alert
              type="warning"
              showIcon
              title={`手填坐标超出地图「${selectedMap?.name ?? mapId}」范围（${selectedMap ? mapBoundsText(selectedMap) : ''}），请修正后再提交。`}
            />
          ) : null}
          <MapPosePreview
            mapId={mapId}
            maps={maps}
            annotations={annotations}
            selectedAnnotationId={
              sendNavigation && !manualMode ? annotationId : undefined
            }
            robot={robot}
          />
          {robotBusy ? (
            <Alert
              showIcon
              type="info"
              title={
                robot?.status === 'mapping'
                  ? '机器人正在建图，结束并恢复导航后才能开始新的多轮取还书任务。'
                  : '机械臂作业中，完成后才能开始新的多轮取还书任务。'
              }
            />
          ) : null}
          <Space wrap>
            <Popconfirm
              title="开始多轮取还书？"
              description={
                sendNavigation
                  ? '机器人将先导航到目标点并低速精调，到位后底盘不再移动：依次升降取书放入对应槽位，再依次从槽位取书还回书架。请确认路径与运动范围内无人、实体急停可触达。'
                  : '机器人不导航、底盘保持不动：原地依次升降取书放入对应槽位，再依次从槽位取书还回书架。请确认机械臂运动范围内无人、实体急停可触达。'
              }
              okText="确认安全并开始"
              cancelText="再检查一下"
              okButtonProps={{ danger: true }}
              onConfirm={() => void start()}
            >
              <Button
                type="primary"
                icon={<RocketOutlined />}
                loading={submitting}
                disabled={!canSubmit}
              >
                开始多轮取还书
              </Button>
            </Popconfirm>
            {taskActive ? (
              <Popconfirm
                title="取消多轮取还书任务？"
                description="导航中将取消导航；取还书中将请求机械臂停止，已完成的取还不会回退。取消后机械臂可能夹着书停在半路，需要人工现场确认。"
                okText="确认取消"
                cancelText="再想想"
                onConfirm={() => void cancelActive()}
              >
                <Button danger>取消任务</Button>
              </Popconfirm>
            ) : null}
          </Space>
          {activeTask ? (
            <Alert
              type={
                activeTask.status === 'completed'
                  ? 'success'
                  : activeTask.status === 'failed'
                    ? 'error'
                    : activeTask.status === 'cancelled' ||
                        activeTask.status === 'recovery_required'
                      ? 'warning'
                      : 'info'
              }
              title={
                <Space size={8} wrap>
                  <span>
                    任务 {activeTask.id}：
                    {activeTask.workflowMessage ?? activeTask.status}
                  </span>
                  {activeTask.failureReason ? (
                    <Typography.Text type="danger" style={{ fontSize: 12 }}>
                      {activeTask.failureReason}
                    </Typography.Text>
                  ) : null}
                </Space>
              }
              description={
                <Space direction="vertical" size={8} style={{ width: '100%' }}>
                  <Progress
                    percent={activeTask.progress}
                    size="small"
                    status={
                      activeTask.status === 'failed' ? 'exception' : undefined
                    }
                  />
                  {renderEntries('取书', activeTask.workflowDetails?.takeBooks)}
                  {renderEntries(
                    '还书',
                    activeTask.workflowDetails?.returnBooks,
                  )}
                </Space>
              }
            />
          ) : null}
        </Space>
      </Card>
      {/* 轮询最新多轮任务：页面刷新后也能恢复显示仍在运行的任务。 */}
      <TaskProgressCard
        taskType="multi_round"
        title="当前多轮取还书任务"
        cancelTask={cancelMultiRound}
        renderExtra={(task) => (
          <>
            {renderEntries('取书', task.workflowDetails?.takeBooks)}
            {renderEntries('还书', task.workflowDetails?.returnBooks)}
          </>
        )}
      />
    </PageContainer>
  );
}
