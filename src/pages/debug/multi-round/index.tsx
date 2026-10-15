import {
  CaretRightOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  RocketOutlined,
  StepForwardOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Popconfirm,
  Row,
  Select,
  Space,
  Tag,
  Typography,
} from 'antd';
import { useEffect, useRef, useState } from 'react';

import { listBooks } from '@/services/libraryRobot';
import {
  getTuningSession,
  prepareTuningSession,
  TuningApiError,
  type TuningKind,
  tuningArmDebugPose,
  tuningArmEnable,
  tuningStepRun,
} from '@/services/libraryRobot/tuning';
import type { Book } from '@/services/libraryRobot/types';
import { errorMessage } from '@/utils/errorText';

/** Multi-round tuning: drive the shared take / mid-return tuning sessions
 * once per book, the only difference between books being the stand slot. */

type EntryStatus =
  | 'pending'
  | 'preparing'
  | 'ready'
  | 'running'
  | 'done'
  | 'failed'
  | 'skipped';

type QueueEntry = {
  key: string;
  kind: TuningKind;
  slot: number;
  book: Book;
  status: EntryStatus;
  steps: string[];
  /** Next step index to run; equals steps.length when the book is done. */
  stepIndex: number;
  confidence?: number;
  error?: string;
};

const ENTRY_STATUS_LABEL: Record<
  EntryStatus,
  { color: string; label: string }
> = {
  pending: { color: 'default', label: '待执行' },
  preparing: { color: 'processing', label: '识别中' },
  ready: { color: 'warning', label: '已识别' },
  running: { color: 'processing', label: '执行中' },
  done: { color: 'success', label: '已完成' },
  failed: { color: 'error', label: '失败' },
  skipped: { color: 'default', label: '已跳过' },
};

const KIND_LABEL: Record<TuningKind, string> = {
  take: '取书',
  return: '还书',
  midreturn: '还书',
};

function entryTitle(entry: QueueEntry) {
  return `${KIND_LABEL[entry.kind]} · 槽位 ${entry.slot} ·《${entry.book.name}》`;
}

export default function MultiRoundDebugPage() {
  const { message } = App.useApp();
  const [books, setBooks] = useState<Book[]>([]);
  const [takeBookIds, setTakeBookIds] = useState<string[]>([]);
  const [returnBookIds, setReturnBookIds] = useState<string[]>([]);
  const [queue, setQueue] = useState<QueueEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [armAction, setArmAction] = useState(false);
  const stopRef = useRef(false);
  /** Mirror of queue for async loops; render-time state would go stale. */
  const queueRef = useRef<QueueEntry[]>([]);
  queueRef.current = queue;

  useEffect(() => {
    (async () => {
      try {
        setBooks(await listBooks());
      } catch (error) {
        message.error(errorMessage(error, '无法读取书籍管理。'));
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function bookById(id: string) {
    return books.find((book) => book.id === id);
  }

  function buildQueue(takeIds: string[], returnIds: string[]): QueueEntry[] {
    // Same slot order as the production multi-round flow: N takes occupy
    // slots N..1 inside-out, return j picks slot j back up outside-in.
    const takes = takeIds
      .map((id, index) => ({
        id,
        slot: takeIds.length - index,
        kind: 'take' as const,
      }))
      .filter((item) => bookById(item.id));
    const returns = returnIds
      .slice(0, takeIds.length)
      .map((id, index) => ({ id, slot: index + 1, kind: 'midreturn' as const }))
      .filter((item) => bookById(item.id));
    return [...takes, ...returns].map((item) => ({
      key: `${item.kind}-${item.slot}-${item.id}`,
      kind: item.kind,
      slot: item.slot,
      book: bookById(item.id) as Book,
      status: 'pending',
      steps: [],
      stepIndex: 0,
    }));
  }

  function updateTakeIds(values: string[]) {
    setTakeBookIds(values);
    const nextReturns = returnBookIds.slice(0, values.length);
    setReturnBookIds(nextReturns);
    setQueue(buildQueue(values, nextReturns));
  }

  function updateReturnIds(values: string[]) {
    setReturnBookIds(values);
    setQueue(buildQueue(takeBookIds, values));
  }

  function patchEntry(key: string, patch: Partial<QueueEntry>) {
    setQueue((current) =>
      current.map((entry) =>
        entry.key === key ? { ...entry, ...patch } : entry,
      ),
    );
  }

  function replaceEntry(next: QueueEntry) {
    setQueue((current) =>
      current.map((entry) => (entry.key === next.key ? next : entry)),
    );
  }

  function reportError(error: unknown, fallback: string) {
    if (error instanceof TuningApiError && error.status === 409) {
      message.warning('服务正忙：请等待当前动作结束后再试。');
    } else {
      message.error(errorMessage(error, fallback));
    }
  }

  async function armActionRun(action: 'enable' | 'debug-pose') {
    setArmAction(true);
    try {
      if (action === 'enable') {
        await tuningArmEnable('take');
        message.success('机械臂已使能。');
      } else {
        await tuningArmDebugPose('take');
        message.success('机械臂已到调试位。');
      }
    } catch (error) {
      reportError(error, '机械臂操作失败。');
    } finally {
      setArmAction(false);
    }
  }

  /** Prepare one book: vision-locate its spine and load the step list. */
  async function prepareEntry(entry: QueueEntry): Promise<QueueEntry | null> {
    patchEntry(entry.key, { status: 'preparing', error: undefined });
    try {
      const result = await prepareTuningSession(entry.kind, {
        slot: entry.slot,
        template_id: entry.book.templateId,
      });
      const session = await getTuningSession(entry.kind);
      const next: QueueEntry = {
        ...entry,
        status: 'ready',
        steps: session.steps,
        stepIndex: 0,
        confidence: result.confidence,
        error: undefined,
      };
      replaceEntry(next);
      message.success(
        `${entryTitle(entry)}：识别成功（置信度 ${result.confidence.toFixed(3)}）。`,
      );
      return next;
    } catch (error) {
      patchEntry(entry.key, {
        status: 'failed',
        error: errorMessage(error, '识别失败'),
      });
      reportError(error, `${entryTitle(entry)}：目标识别失败。`);
      return null;
    }
  }

  /** Run one step of one book; returns the updated entry or null on failure. */
  async function runEntryStep(entry: QueueEntry): Promise<QueueEntry | null> {
    if (entry.status !== 'ready' && entry.status !== 'running') return null;
    patchEntry(entry.key, { status: 'running' });
    try {
      const result = await tuningStepRun(entry.kind, entry.stepIndex);
      if (!result.ok) {
        patchEntry(entry.key, {
          status: 'failed',
          error: `步骤「${result.step_name}」失败：${result.detail ?? ''}`,
        });
        message.error(
          `${entryTitle(entry)}：步骤「${result.step_name}」执行失败。`,
        );
        return null;
      }
      const nextIndex = entry.stepIndex + 1;
      const done = nextIndex >= entry.steps.length;
      const next: QueueEntry = {
        ...entry,
        status: done ? 'done' : 'ready',
        stepIndex: nextIndex,
      };
      replaceEntry(next);
      if (done) message.success(`${entryTitle(entry)}：全部步骤完成。`);
      return next;
    } catch (error) {
      patchEntry(entry.key, {
        status: 'failed',
        error: errorMessage(error, '步骤执行失败'),
      });
      reportError(error, `${entryTitle(entry)}：步骤执行失败。`);
      return null;
    }
  }

  async function withBusy(action: () => Promise<void>) {
    setBusy(true);
    stopRef.current = false;
    try {
      await action();
    } finally {
      setBusy(false);
    }
  }

  /** Prepare + run all remaining steps of one book (no busy wrapper). */
  async function runWholeEntryInner(key: string) {
    let entry = queueRef.current.find((item) => item.key === key);
    if (!entry || entry.status === 'done' || entry.status === 'skipped') {
      return;
    }
    if (entry.status === 'pending') {
      const prepared = await prepareEntry(entry);
      if (!prepared) return;
      entry = prepared;
    }
    while (!stopRef.current && entry.status !== 'done') {
      const next = await runEntryStep(entry);
      if (!next) return;
      entry = next;
    }
  }

  /** Prepare + run all remaining steps of one book. */
  function runWholeEntry(key: string) {
    return withBusy(() => runWholeEntryInner(key));
  }

  /** Run every queued book in order; stop at the first failure. */
  function runWholeQueue() {
    return withBusy(async () => {
      for (const queued of queueRef.current) {
        if (stopRef.current) return;
        const latest = queueRef.current.find((item) => item.key === queued.key);
        if (
          !latest ||
          latest.status === 'done' ||
          latest.status === 'skipped'
        ) {
          continue;
        }
        await runWholeEntryInner(latest.key);
        const after = queueRef.current.find((item) => item.key === queued.key);
        if (after?.status === 'failed') {
          message.error('多轮调试在失败处停止；已完成的动作不会回退。');
          return;
        }
      }
      if (!stopRef.current) message.success('多轮取还调试全部完成。');
    });
  }

  const queueEmpty = queue.length === 0;

  return (
    <PageContainer
      title="多轮取还调试"
      content="按书名选择取/还书籍后逐本调试：每本书复用快速取书 / 快速还书的调参会话，仅槽位不同（取书从内向外占槽位 N..1，还书从槽位 1..M 由外向内取回）。运动参数与书立槽位标定在「流程调参」页编辑，热生效。"
    >
      <Alert
        showIcon
        type="warning"
        title="将真实驱动升降台、机械臂和推书杆！"
        description="执行前请确认机构周边清场、实体急停可触达。步骤执行中无法中断，「停止排队」只在步骤之间生效；失败即停，已完成的动作不会回退。"
        style={{ marginBottom: 16 }}
      />
      <Card size="small" title="书籍与槽位" style={{ marginBottom: 16 }}>
        <Space wrap size={[16, 12]}>
          <span>
            <Typography.Text type="secondary">
              取书（顺序 = 槽位 N..1，由内向外）
            </Typography.Text>
            <br />
            <Select
              mode="multiple"
              style={{ minWidth: 320 }}
              placeholder="选择 1-3 本取书"
              value={takeBookIds}
              onChange={updateTakeIds}
              maxCount={3}
              options={books.map((book) => ({
                value: book.id,
                label: `${book.name}（高度 ${book.heightMm} mm）`,
              }))}
              showSearch
              optionFilterProp="label"
              disabled={busy}
            />
          </span>
          <span>
            <Typography.Text type="secondary">
              还书（依次从槽位 1、2、3 取回）
            </Typography.Text>
            <br />
            <Select
              mode="multiple"
              style={{ minWidth: 320 }}
              placeholder="选择 0-3 本还书"
              value={returnBookIds}
              onChange={updateReturnIds}
              maxCount={Math.max(takeBookIds.length, 1)}
              options={books.map((book) => ({
                value: book.id,
                label: `${book.name}（高度 ${book.heightMm} mm）`,
              }))}
              showSearch
              optionFilterProp="label"
              disabled={busy || takeBookIds.length === 0}
            />
          </span>
          <Space wrap>
            <Button
              icon={<PoweroffOutlined />}
              loading={armAction}
              onClick={() => void armActionRun('enable')}
              disabled={busy}
            >
              使能机械臂
            </Button>
            <Button
              loading={armAction}
              onClick={() => void armActionRun('debug-pose')}
              disabled={busy}
            >
              机械臂回调试位
            </Button>
          </Space>
          <Space wrap>
            <Popconfirm
              title="开始多轮全流程调试？"
              description="将按顺序逐本识别并执行全部取/还步骤。请确认运动范围内无人、实体急停可触达。"
              okText="确认安全并开始"
              cancelText="再检查一下"
              okButtonProps={{ danger: true }}
              onConfirm={() => void runWholeQueue()}
            >
              <Button
                type="primary"
                icon={<RocketOutlined />}
                loading={busy}
                disabled={queueEmpty}
              >
                开始全流程
              </Button>
            </Popconfirm>
            <Button
              danger
              disabled={!busy}
              onClick={() => {
                stopRef.current = true;
                message.warning('已请求停止：当前步骤完成后排队终止。');
              }}
            >
              停止排队
            </Button>
            <Button
              icon={<ReloadOutlined />}
              disabled={busy || queueEmpty}
              onClick={() => setQueue(buildQueue(takeBookIds, returnBookIds))}
            >
              重置进度
            </Button>
          </Space>
        </Space>
      </Card>

      {queueEmpty ? (
        <Card size="small">
          <Typography.Text type="secondary">
            请先在上方选择取书/还书书籍，生成调试队列。运动参数（速度、位姿偏移、夹爪）与书立槽位标定在「流程调参」页编辑，两个页面共用同一份车上配置。
          </Typography.Text>
        </Card>
      ) : (
        <Row gutter={[16, 16]}>
          {queue.map((entry) => {
            const status = ENTRY_STATUS_LABEL[entry.status];
            return (
              <Col key={entry.key} xs={24} xl={12}>
                <Card
                  size="small"
                  title={
                    <Space size={8} wrap>
                      <span>{entryTitle(entry)}</span>
                      <Tag color={status.color}>{status.label}</Tag>
                      {entry.confidence !== undefined ? (
                        <Typography.Text
                          type="secondary"
                          style={{ fontSize: 12 }}
                        >
                          置信度 {entry.confidence.toFixed(3)}
                        </Typography.Text>
                      ) : null}
                    </Space>
                  }
                  extra={
                    <Space size={4} wrap>
                      <Button
                        size="small"
                        disabled={busy || entry.status !== 'pending'}
                        onClick={() =>
                          void withBusy(async () => {
                            await prepareEntry(entry);
                          })
                        }
                      >
                        识别目标
                      </Button>
                      <Popconfirm
                        title={`执行《${entry.book.name}》剩余全部步骤？`}
                        description="将真实驱动机构完成该书的取/还动作。"
                        okText="确认执行"
                        cancelText="取消"
                        okButtonProps={{ danger: true }}
                        onConfirm={() => void runWholeEntry(entry.key)}
                      >
                        <Button
                          size="small"
                          type="primary"
                          icon={<CaretRightOutlined />}
                          disabled={
                            busy ||
                            entry.status === 'done' ||
                            entry.status === 'skipped'
                          }
                        >
                          执行全部
                        </Button>
                      </Popconfirm>
                      <Button
                        size="small"
                        icon={<StepForwardOutlined />}
                        disabled={
                          busy ||
                          (entry.status !== 'ready' &&
                            entry.status !== 'running')
                        }
                        onClick={() =>
                          void withBusy(async () => {
                            await runEntryStep(entry);
                          })
                        }
                      >
                        单步执行
                      </Button>
                      <Button
                        size="small"
                        disabled={
                          busy ||
                          entry.status === 'done' ||
                          entry.status === 'skipped'
                        }
                        onClick={() =>
                          patchEntry(entry.key, { status: 'skipped' })
                        }
                      >
                        跳过
                      </Button>
                    </Space>
                  }
                >
                  {entry.error ? (
                    <Alert
                      showIcon
                      type="error"
                      title={entry.error}
                      style={{ marginBottom: 8 }}
                    />
                  ) : null}
                  {entry.steps.length ? (
                    <Space size={4} wrap>
                      {entry.steps.map((stepName, index) => (
                        <Tag
                          key={`${entry.key}-step-${stepName}`}
                          color={
                            index < entry.stepIndex
                              ? 'success'
                              : index === entry.stepIndex &&
                                  entry.status !== 'done'
                                ? 'processing'
                                : 'default'
                          }
                        >
                          {index + 1}. {stepName}
                        </Tag>
                      ))}
                    </Space>
                  ) : (
                    <Typography.Text type="secondary">
                      尚未识别目标；识别后显示该书的步骤列表。
                    </Typography.Text>
                  )}
                </Card>
              </Col>
            );
          })}
        </Row>
      )}
    </PageContainer>
  );
}
