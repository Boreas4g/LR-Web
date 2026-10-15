import { RocketOutlined } from '@ant-design/icons';
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
  Typography,
} from 'antd';
import { useEffect, useState } from 'react';

import {
  cancelCompleteTakeout,
  createCompleteTakeout,
  DEFAULT_BOOK_HEIGHT_MM,
  getMapAnnotations,
  getMapVersions,
  getRobots,
  listBooks,
  pollTasks,
} from '@/services/libraryRobot';
import { listLibrary } from '@/services/libraryRobot/studio';
import type {
  Book,
  DispatchTask,
  MapAnnotation,
  MapVersion,
  RobotSummary,
} from '@/services/libraryRobot/types';
import { errorMessage } from '@/utils/errorText';
import AnnotationTargetSelect from '../components/AnnotationTargetSelect';
import MapPosePreview from '../components/MapPosePreview';
import { mapBoundsText, targetWithinMap } from '../components/mapBounds';
import VisionImage from '../components/VisionImage';

const TERMINAL_STATES = [
  'completed',
  'failed',
  'cancelled',
  'recovery_required',
];

/** Calibrated onboard book-stand slots (see the bookarm slot calibration). */
const BOOK_STAND_SLOTS = [1, 2, 3, 4, 5];

/** Navigate-to-shelf then takeout orchestration, driven by the control station. */
export default function CompleteTakeoutPanel({
  onTaskCreated,
}: {
  /** Called after a new orchestrated takeout task is successfully created. */
  onTaskCreated?: (task: DispatchTask) => void;
}) {
  const { message } = App.useApp();
  const [books, setBooks] = useState<Book[]>([]);
  const [annotations, setAnnotations] = useState<MapAnnotation[]>([]);
  const [maps, setMaps] = useState<MapVersion[]>([]);
  const [robot, setRobot] = useState<RobotSummary>();
  const [mapId, setMapId] = useState<string>();
  const [robotId, setRobotId] = useState<string>('LR-01');
  const [bookId, setBookId] = useState<string>();
  const [annotationId, setAnnotationId] = useState<string>();
  const [manualMode, setManualMode] = useState(false);
  const [manualTarget, setManualTarget] = useState({ x: 0, y: 0, yaw: 0 });
  const [liftHeightMm, setLiftHeightMm] = useState(DEFAULT_BOOK_HEIGHT_MM);
  const [destinationSlot, setDestinationSlot] = useState<number>();
  const [sendNavigation, setSendNavigation] = useState(true);
  const [activeTaskId, setActiveTaskId] = useState<string>();
  const [activeTask, setActiveTask] = useState<DispatchTask>();
  const [submitting, setSubmitting] = useState(false);
  const [loadingOptions, setLoadingOptions] = useState(true);
  const [libraryIds, setLibraryIds] = useState<Set<string>>();

  useEffect(() => {
    (async () => {
      try {
        const [bookList, robots, maps, library] = await Promise.all([
          listBooks(),
          getRobots(),
          getMapVersions(),
          // 书脊库 id 清单用于「模板缺失」拦截；服务不可达时不阻断。
          listLibrary().catch(() => null),
        ]);
        setBooks(bookList);
        setMaps(maps);
        if (library) {
          setLibraryIds(new Set(library.items.map((item) => item.id)));
        }
        const robot = robots[0];
        if (robot) {
          setRobotId(robot.id);
          setRobot(robot);
        }
        const chosenMap =
          maps.find((item) => item.id === robot?.mapId) ?? maps[0];
        if (chosenMap) {
          setMapId(chosenMap.id);
          setAnnotations(await getMapAnnotations(chosenMap.id));
        }
      } catch (error) {
        message.error(errorMessage(error, '无法加载完整取书选项。'));
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
  const selectedBook = books.find((item) => item.id === bookId);
  // 模板缺失：书脊库中已没有该书的模板，取书识别必然失败，直接拦截。
  const templateMissing = Boolean(
    selectedBook && libraryIds && !libraryIds.has(selectedBook.templateId),
  );
  const taskActive =
    activeTask !== undefined && !TERMINAL_STATES.includes(activeTask.status);
  const robotBusy = robot?.status === 'busy' || robot?.status === 'mapping';
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
    !templateMissing &&
    selectedBook !== undefined &&
    (!sendNavigation || target !== undefined) &&
    destinationSlot !== undefined &&
    liftHeightMm > 0;

  async function start() {
    if (
      !selectedBook ||
      destinationSlot === undefined ||
      (sendNavigation && !target)
    )
      return;
    if (templateMissing) {
      message.error(
        `《${selectedBook.name}》的书脊模板在机器人书脊库中已缺失，请先到「书籍管理」补录书脊图。`,
      );
      return;
    }
    setSubmitting(true);
    try {
      // 提交前重新拉取机器人/地图/标注，挂载时的快照可能已经过期
      // （机器人切换地图、标注被增删都会让旧目标失效）。
      const [freshRobots, freshMaps] = await Promise.all([
        getRobots(),
        getMapVersions(),
      ]);
      const freshRobot =
        freshRobots.find((item) => item.id === robotId) ?? freshRobots[0];
      const freshMap =
        freshMaps.find((item) => item.id === freshRobot?.mapId) ??
        freshMaps.find((item) => item.id === mapId) ??
        freshMaps[0];
      let effectiveTarget = target;
      const effectiveAnnotationId =
        sendNavigation && !manualMode ? annotationId : undefined;
      if (sendNavigation && freshMap) {
        const freshAnnotations = await getMapAnnotations(freshMap.id);
        setMaps(freshMaps);
        setAnnotations(freshAnnotations);
        setMapId(freshMap.id);
        if (freshRobot) setRobot(freshRobot);
        if (manualMode) {
          if (!targetWithinMap(freshMap, manualTarget.x, manualTarget.y)) {
            message.error(
              `手填坐标超出地图「${freshMap.name}」范围（${mapBoundsText(freshMap)}），请修正后再提交。`,
            );
            return;
          }
        } else if (annotationId) {
          const freshAnnotation = freshAnnotations.find(
            (item) => item.id === annotationId,
          );
          if (!freshAnnotation) {
            message.error('所选标注点已不存在，请重新选择导航目标。');
            return;
          }
          effectiveTarget = {
            x: freshAnnotation.mapX,
            y: freshAnnotation.mapY,
            yaw: freshAnnotation.yaw,
          };
        }
      }
      const task = await createCompleteTakeout({
        robotId,
        templateId: selectedBook.templateId,
        target: sendNavigation ? effectiveTarget : undefined,
        liftHeightMm,
        destinationSlot,
        liftSpeedLevel: 2,
        mapId: freshMap?.id ?? mapId,
        annotationId: effectiveAnnotationId,
        sendNavigation,
      });
      setActiveTaskId(task.id);
      setActiveTask(task);
      message.success(
        sendNavigation
          ? '完整取书任务已开始：先导航，到位后自动取书。'
          : '完整取书任务已开始：跳过导航，原地直接取书。',
      );
      onTaskCreated?.(task);
    } catch (error) {
      message.error(errorMessage(error, '完整取书任务创建失败。'));
    } finally {
      setSubmitting(false);
    }
  }

  async function cancelActive() {
    if (!activeTaskId) return;
    try {
      await cancelCompleteTakeout(activeTaskId);
      message.warning('已请求取消完整取书任务。');
    } catch (error) {
      message.error(errorMessage(error, '取消失败。'));
    }
  }

  return (
    <Card
      size="small"
      title="完整取书（导航一次或原地开始）"
      style={{ marginBottom: 16 }}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Checkbox
          checked={sendNavigation}
          onChange={(event) => setSendNavigation(event.target.checked)}
          disabled={taskActive}
        >
          发送导航（勾选后先导航到目标点并低速精调；不勾选则底盘不动，原地直接开始升降取书）
        </Checkbox>
        <Space wrap size={[12, 8]}>
          <Select
            style={{ minWidth: 260 }}
            placeholder="选择书籍（书籍管理）"
            value={bookId}
            onChange={(value) => {
              setBookId(value);
              const book = books.find((item) => item.id === value);
              if (book) {
                setLiftHeightMm(book.heightMm);
                // 选书后自动带出该书登记的取书点与常用槽位（仍可手改）。
                if (book.annotationId) setAnnotationId(book.annotationId);
                if (book.preferredSlot) setDestinationSlot(book.preferredSlot);
              }
            }}
            loading={loadingOptions}
            options={books.map((book) => ({
              value: book.id,
              label: `${book.name}（高度 ${book.heightMm} mm）`,
            }))}
            showSearch
            optionFilterProp="label"
            disabled={taskActive}
          />
          {sendNavigation ? (
            <>
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
                      step={axis === 'yaw' ? 0.1 : 0.1}
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
            </>
          ) : null}
          <InputNumber
            addonBefore="取书高度（升降台 mm）"
            size="small"
            min={10}
            max={1700}
            precision={0}
            value={liftHeightMm}
            onChange={(value) =>
              setLiftHeightMm(Number(value ?? DEFAULT_BOOK_HEIGHT_MM))
            }
            style={{ width: 240 }}
            disabled={taskActive}
          />
          <Select
            style={{ minWidth: 200 }}
            placeholder="放入书立槽位"
            value={destinationSlot}
            onChange={(value) => setDestinationSlot(value)}
            options={BOOK_STAND_SLOTS.map((slotNo) => ({
              value: slotNo,
              label: `槽位 ${slotNo}`,
            }))}
            disabled={taskActive}
          />
        </Space>
        <MapPosePreview
          mapId={mapId}
          maps={maps}
          annotations={annotations}
          selectedAnnotationId={
            sendNavigation && !manualMode ? annotationId : undefined
          }
          robot={robot}
        />
        {templateMissing && selectedBook ? (
          <Alert
            showIcon
            type="error"
            title={`《${selectedBook.name}》的书脊模板在机器人书脊库中已缺失（模板缺失）。请先到「书籍管理」补录书脊图，再创建取书任务。`}
          />
        ) : null}
        {manualOutOfBounds ? (
          <Alert
            showIcon
            type="warning"
            title={`手填坐标超出地图「${selectedMap?.name ?? mapId}」范围（${selectedMap ? mapBoundsText(selectedMap) : ''}），请修正后再提交。`}
          />
        ) : null}
        {robotBusy ? (
          <Alert
            showIcon
            type="info"
            title={
              robot?.status === 'mapping'
                ? '机器人正在建图，结束并恢复导航后才能开始新的取书任务。'
                : '机械臂作业中，完成后才能开始新的取书任务。'
            }
          />
        ) : null}
        <Space wrap>
          <Popconfirm
            title="开始完整取书？"
            description={
              sendNavigation
                ? '机器人将先导航到目标点，到位后自动执行取书并放入所选书立槽位。请确认路径与运动范围内无人、实体急停可触达。'
                : '机器人不导航、底盘保持不动：原地直接升降取书并放入所选书立槽位。请确认机械臂运动范围内无人、实体急停可触达。'
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
              开始完整取书
            </Button>
          </Popconfirm>
          {taskActive ? (
            <Popconfirm
              title="取消完整取书任务？"
              description="导航中将取消导航；取书中将请求机械臂停止。取消后机械臂可能夹着书停在半路，需要人工现场确认。"
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
              <Progress
                percent={activeTask.progress}
                size="small"
                status={
                  activeTask.status === 'failed' ? 'exception' : undefined
                }
              />
            }
          />
        ) : null}
        {activeTask?.workflowDetails?.visionImages?.spine ? (
          <VisionImage
            url={activeTask.workflowDetails.visionImages.spine}
            label="书脊识别（D435）"
          />
        ) : null}
      </Space>
    </Card>
  );
}
