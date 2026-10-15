import {
  ArrowDownOutlined,
  ArrowLeftOutlined,
  ArrowRightOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  PlayCircleOutlined,
  PushpinOutlined,
  ReloadOutlined,
  SaveOutlined,
  StopOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import { history } from '@umijs/max';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Input,
  List,
  Modal,
  message,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from 'antd';
import { useEffect, useRef, useState } from 'react';

import {
  clearMappingMarks,
  createMappingMark,
  deleteMappingMark,
  getMappingMarks,
  getMappingSnapshotUrl,
  getMappingStatus,
  getRobots,
  saveMapping,
  sendMappingJog,
  startMapping,
  stopMapping,
} from '@/services/libraryRobot';
import type {
  MappingMark,
  MappingStatus,
  RobotSummary,
  SaveMappingResult,
} from '@/services/libraryRobot/types';
import {
  firstConnectedGamepad,
  GAMEPAD_ANGULAR_SPEED,
  GAMEPAD_LINEAR_SPEED,
} from './gamepad';
import { useMappingGamepad } from './useMappingGamepad';

const LINEAR_SPEED = 0.2;
const ANGULAR_SPEED = 0.5;

const modeMeta: Record<
  MappingStatus['mode'],
  { color: string; label: string }
> = {
  navigation: { color: 'success', label: '导航中' },
  mapping: { color: 'processing', label: '建图中' },
  transition: { color: 'warning', label: '切换中' },
};

export default function LibraryMappingPage() {
  const [robots, setRobots] = useState<RobotSummary[]>([]);
  const [robotId, setRobotId] = useState<string>();
  const [mappingStatus, setMappingStatus] = useState<MappingStatus>();
  const [statusError, setStatusError] = useState<string>();
  const [actionLoading, setActionLoading] = useState<'start' | 'stop'>();
  const [snapshotUrl, setSnapshotUrl] = useState<string>();
  const [snapshotError, setSnapshotError] = useState<string>();
  const snapshotRef = useRef<string | undefined>(undefined);
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveName, setSaveName] = useState('');
  const [saving, setSaving] = useState(false);
  const [savedMap, setSavedMap] = useState<SaveMappingResult>();
  const [gamepadId, setGamepadId] = useState<string>();
  const [gamepadEnabled, setGamepadEnabled] = useState(false);
  const [marks, setMarks] = useState<MappingMark[]>([]);
  const [clearingMarks, setClearingMarks] = useState(false);
  const [marking, setMarking] = useState(false);
  const [deletingMarkIndex, setDeletingMarkIndex] = useState<number>();
  const marksRef = useRef<MappingMark[]>([]);
  const markingRef = useRef(false);

  const mappingActive = mappingStatus?.mode === 'mapping';
  // transition（双栈皆停、导航恢复中）也允许点击"结束并恢复导航"作为死锁出口。
  const stopAllowed =
    mappingStatus?.mode === 'mapping' || mappingStatus?.mode === 'transition';

  function replaceSnapshot(url?: string) {
    if (snapshotRef.current) URL.revokeObjectURL(snapshotRef.current);
    snapshotRef.current = url;
    setSnapshotUrl(url);
  }

  async function loadRobots() {
    try {
      const data = await getRobots();
      setRobots(data);
      setRobotId((current) => current ?? data[0]?.id);
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '读取机器人列表失败。',
      );
    }
  }

  useEffect(() => {
    void loadRobots();
  }, []);

  async function refreshStatus(targetRobotId?: string) {
    const id = targetRobotId ?? robotId;
    if (!id) return;
    try {
      const status = await getMappingStatus(id);
      setMappingStatus(status);
      setStatusError(undefined);
    } catch (error) {
      setStatusError(
        error instanceof Error ? error.message : '读取建图状态失败。',
      );
    }
  }

  useEffect(() => {
    if (!robotId) return;
    setMappingStatus(undefined);
    setStatusError(undefined);
    void refreshStatus(robotId);
    const timer = window.setInterval(() => void refreshStatus(robotId), 3_000);
    return () => window.clearInterval(timer);
  }, [robotId]);

  useEffect(() => {
    if (!robotId || !mappingActive) {
      replaceSnapshot(undefined);
      setSnapshotError(undefined);
      return;
    }
    let cancelled = false;
    async function refreshSnapshot(id: string) {
      try {
        const url = await getMappingSnapshotUrl(id);
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        replaceSnapshot(url);
        setSnapshotError(undefined);
      } catch (error) {
        if (cancelled) return;
        setSnapshotError(
          error instanceof Error ? error.message : '无法读取建图快照。',
        );
      }
    }
    void refreshSnapshot(robotId);
    const timer = window.setInterval(
      () => void refreshSnapshot(robotId),
      3_000,
    );
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [robotId, mappingActive]);

  useEffect(
    () => () => {
      if (snapshotRef.current) URL.revokeObjectURL(snapshotRef.current);
    },
    [],
  );

  function sendJog(
    linear: number,
    angular: number,
    options?: { quiet?: boolean },
  ) {
    if (!robotId) return;
    void sendMappingJog(robotId, { linear, angular }).catch((error) => {
      if (options?.quiet) return;
      message.error(
        error instanceof Error ? error.message : '发送点动速度失败。',
      );
    });
  }

  function releaseJog() {
    sendJog(0, 0, { quiet: true });
  }

  useEffect(() => {
    const updateGamepad = () => setGamepadId(firstConnectedGamepad()?.id);
    window.addEventListener('gamepadconnected', updateGamepad);
    window.addEventListener('gamepaddisconnected', updateGamepad);
    // Chrome 要求页面聚焦状态下按一次手柄按键才会暴露设备；
    // 轮询同时覆盖“页面加载前已激活”的情况。
    const poll = window.setInterval(updateGamepad, 1000);
    updateGamepad();
    return () => {
      window.removeEventListener('gamepadconnected', updateGamepad);
      window.removeEventListener('gamepaddisconnected', updateGamepad);
      window.clearInterval(poll);
    };
  }, []);

  useEffect(() => {
    if (!mappingActive) setGamepadEnabled(false);
  }, [mappingActive]);

  const { output: gamepadOutput } = useMappingGamepad({
    robotId,
    active: mappingActive && gamepadEnabled,
    onMark: () => void submitMark(),
  });

  async function refreshMarks(targetRobotId?: string) {
    const id = targetRobotId ?? robotId;
    if (!id) return;
    try {
      const data = await getMappingMarks(id);
      marksRef.current = data.marks;
      setMarks(data.marks);
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '读取书架点标注失败。',
      );
    }
  }

  useEffect(() => {
    marksRef.current = [];
    setMarks([]);
    if (!robotId || !mappingActive) return;
    void refreshMarks(robotId);
  }, [robotId, mappingActive]);

  async function submitMark() {
    if (!robotId || markingRef.current) return;
    markingRef.current = true;
    setMarking(true);
    const label = `书架点 ${marksRef.current.length + 1}`;
    try {
      await createMappingMark(robotId, label);
      message.success(`已标记“${label}”。`);
      await refreshMarks();
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '标记书架点失败。',
      );
    } finally {
      markingRef.current = false;
      setMarking(false);
    }
  }

  async function performDeleteMark(index: number) {
    if (!robotId) return;
    setDeletingMarkIndex(index);
    try {
      await deleteMappingMark(robotId, index);
      message.success('已删除该书架点标注。');
      await refreshMarks();
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '删除书架点标注失败。',
      );
    } finally {
      setDeletingMarkIndex(undefined);
    }
  }

  async function performClearMarks() {
    if (!robotId) return;
    setClearingMarks(true);
    try {
      await clearMappingMarks(robotId);
      marksRef.current = [];
      setMarks([]);
      message.success('已清空书架点标注。');
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '清空书架点标注失败。',
      );
    } finally {
      setClearingMarks(false);
    }
  }

  async function performStart() {
    if (!robotId) return;
    setActionLoading('start');
    try {
      await startMapping(robotId);
      message.success('已进入建图模式，导航栈已停止。');
    } catch (error) {
      message.error(error instanceof Error ? error.message : '开始建图失败。');
    } finally {
      setActionLoading(undefined);
      await refreshStatus();
    }
  }

  async function performStop() {
    if (!robotId) return;
    setActionLoading('stop');
    try {
      await stopMapping(robotId);
      message.success('已结束建图，导航栈正在恢复。');
    } catch (error) {
      message.error(error instanceof Error ? error.message : '结束建图失败。');
    } finally {
      setActionLoading(undefined);
      await refreshStatus();
    }
  }

  async function submitSave() {
    if (!robotId) return;
    setSaving(true);
    try {
      const saved = await saveMapping(robotId, saveName.trim() || undefined);
      setSaveOpen(false);
      setSaveName('');
      setSavedMap(saved);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存地图失败。');
    } finally {
      setSaving(false);
    }
  }

  const snapshotEmptyText =
    snapshotError && !snapshotError.endsWith('（404）')
      ? snapshotError
      : '等待 SLAM 首帧…';

  return (
    <PageContainer
      title="手动建图"
      content="在机器人本机启动 SLAM 手动建图：按住遥控按钮完成区域扫描，实时预览建图结果，保存后自动导入地图库。"
      extra={[
        <Select
          key="robot"
          style={{ minWidth: 220 }}
          value={robotId}
          placeholder="选择机器人"
          options={robots.map((robot) => ({
            value: robot.id,
            label: `${robot.name}（${robot.id}）`,
          }))}
          onChange={setRobotId}
        />,
        <Button
          key="refresh"
          icon={<ReloadOutlined />}
          onClick={() => void loadRobots()}
        >
          刷新
        </Button>,
      ]}
    >
      <Alert
        showIcon
        type="warning"
        message="建图与导航互斥"
        description="建图期间导航栈停止：取还书、导航任务、巡航与遥控均不可用。完成扫描后请先保存地图并入库，再结束建图恢复导航。"
        style={{ marginBottom: 16 }}
      />
      <Card
        title="建图状态"
        style={{ marginBottom: 16 }}
        extra={
          <Space wrap>
            <Popconfirm
              cancelText="取消"
              okText="确认开始"
              title="开始手动建图？"
              description="将停止导航栈，取还书、导航和遥控任务在此期间不可用。"
              onConfirm={() => void performStart()}
            >
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                loading={actionLoading === 'start'}
                disabled={mappingStatus?.mode !== 'navigation'}
              >
                开始建图
              </Button>
            </Popconfirm>
            <Popconfirm
              cancelText="取消"
              okText="确认结束"
              title="结束建图并恢复导航？"
              description="未保存的建图结果会丢失，请确认已经保存地图。"
              onConfirm={() => void performStop()}
            >
              <Button
                danger
                icon={<StopOutlined />}
                loading={actionLoading === 'stop'}
                disabled={!stopAllowed}
              >
                结束并恢复导航
              </Button>
            </Popconfirm>
            <Button
              icon={<SaveOutlined />}
              disabled={!mappingActive}
              onClick={() => setSaveOpen(true)}
            >
              保存地图并入库
            </Button>
          </Space>
        }
      >
        {mappingStatus ? (
          <Space wrap size={16}>
            <span>
              当前模式：
              <Tag color={modeMeta[mappingStatus.mode].color}>
                {modeMeta[mappingStatus.mode].label}
              </Tag>
            </span>
            <span>
              SLAM：
              <Tag color={mappingStatus.slamRunning ? 'success' : 'default'}>
                {mappingStatus.slamRunning ? '运行中' : '未运行'}
              </Tag>
            </span>
            {mappingStatus.message ? (
              <Typography.Text type="secondary">
                {mappingStatus.message}
              </Typography.Text>
            ) : null}
          </Space>
        ) : statusError ? null : (
          <Spin size="small" />
        )}
        {statusError ? (
          <Alert
            showIcon
            type="error"
            message={statusError}
            style={{ marginTop: 12 }}
          />
        ) : null}
        {mappingStatus?.slamRestarted ? (
          <Alert
            showIcon
            type="warning"
            message="车端 SLAM 已重启"
            description="此前未保存的建图内容可能已被重置，请核对建图预览与书架点标注后再继续。"
            style={{ marginTop: 12 }}
          />
        ) : null}
      </Card>
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={14}>
          <Card
            title="建图预览"
            extra={
              mappingActive ? (
                <Tag color="processing">每 3 秒刷新</Tag>
              ) : undefined
            }
          >
            {mappingActive ? (
              snapshotUrl ? (
                <img
                  src={snapshotUrl}
                  alt="SLAM 建图预览"
                  style={{ display: 'block', width: '100%' }}
                />
              ) : (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={snapshotEmptyText}
                />
              )
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="开始建图后显示实时 SLAM 预览。"
              />
            )}
          </Card>
        </Col>
        <Col xs={24} xl={10}>
          <Card
            title="建图遥控"
            extra={
              mappingActive ? (
                <Tag color="processing">按住按钮点动</Tag>
              ) : (
                <Tag>仅建图模式可用</Tag>
              )
            }
          >
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(3, 1fr)',
                gap: 8,
                maxWidth: 320,
              }}
            >
              <span />
              <Button
                icon={<ArrowUpOutlined />}
                disabled={!mappingActive}
                onPointerDown={(event) => {
                  event.preventDefault();
                  event.currentTarget.setPointerCapture(event.pointerId);
                  sendJog(LINEAR_SPEED, 0);
                }}
                onPointerUp={releaseJog}
                onPointerLeave={releaseJog}
                onPointerCancel={releaseJog}
              >
                前进
              </Button>
              <span />
              <Button
                icon={<ArrowLeftOutlined />}
                disabled={!mappingActive}
                onPointerDown={(event) => {
                  event.preventDefault();
                  event.currentTarget.setPointerCapture(event.pointerId);
                  sendJog(0, ANGULAR_SPEED);
                }}
                onPointerUp={releaseJog}
                onPointerLeave={releaseJog}
                onPointerCancel={releaseJog}
              >
                左转
              </Button>
              <span />
              <Button
                icon={<ArrowRightOutlined />}
                disabled={!mappingActive}
                onPointerDown={(event) => {
                  event.preventDefault();
                  event.currentTarget.setPointerCapture(event.pointerId);
                  sendJog(0, -ANGULAR_SPEED);
                }}
                onPointerUp={releaseJog}
                onPointerLeave={releaseJog}
                onPointerCancel={releaseJog}
              >
                右转
              </Button>
              <span />
              <Button
                icon={<ArrowDownOutlined />}
                disabled={!mappingActive}
                onPointerDown={(event) => {
                  event.preventDefault();
                  event.currentTarget.setPointerCapture(event.pointerId);
                  sendJog(-LINEAR_SPEED, 0);
                }}
                onPointerUp={releaseJog}
                onPointerLeave={releaseJog}
                onPointerCancel={releaseJog}
              >
                后退
              </Button>
              <span />
            </div>
            <Button
              block
              danger
              icon={<StopOutlined />}
              disabled={!mappingActive}
              style={{ marginTop: 12, maxWidth: 320 }}
              onClick={() => sendJog(0, 0)}
            >
              停止
            </Button>
            <Typography.Paragraph
              type="secondary"
              style={{ marginTop: 12, marginBottom: 0 }}
            >
              按住按钮即发送固定档速度（线速度 {LINEAR_SPEED} m/s，角速度{' '}
              {ANGULAR_SPEED} rad/s），松开自动停车；车端 0.6
              秒看门狗超时也会自动停车。
            </Typography.Paragraph>
            <Space vertical size={8} style={{ marginTop: 16 }}>
              <Space wrap>
                <Switch
                  checked={gamepadEnabled}
                  disabled={!mappingActive || !gamepadId}
                  onChange={setGamepadEnabled}
                />
                <span>手柄遥控</span>
                {gamepadId ? (
                  <Tag color="success">已连接：{gamepadId}</Tag>
                ) : (
                  <Tag>
                    未检测到手柄（请按下手柄任意按键激活；需用 localhost
                    打开页面）
                  </Tag>
                )}
              </Space>
              {gamepadEnabled ? (
                <Typography.Text type="secondary">
                  当前输出：线速度 {gamepadOutput.linear.toFixed(2)} m/s，角速度{' '}
                  {gamepadOutput.angular.toFixed(2)} rad/s
                </Typography.Text>
              ) : null}
              <Typography.Text type="secondary">
                左摇杆模拟量调速（满行程线速度 ±{GAMEPAD_LINEAR_SPEED}{' '}
                m/s，角速度 ±{GAMEPAD_ANGULAR_SPEED}{' '}
                rad/s），摇杆回中自动停车；按 A 键把当前位置标记为书架点。
              </Typography.Text>
            </Space>
          </Card>
          <Card
            title="书架点标注"
            style={{ marginTop: 16 }}
            extra={
              <Space size={8} wrap>
                <Button
                  size="small"
                  type="primary"
                  icon={<PushpinOutlined />}
                  loading={marking}
                  disabled={!mappingActive}
                  onClick={() => void submitMark()}
                >
                  在当前位置标记
                </Button>
                <Popconfirm
                  cancelText="取消"
                  okText="确认清空"
                  title="清空全部书架点标注？"
                  description="已标记的书架点会全部删除，保存地图前请确认。"
                  onConfirm={() => void performClearMarks()}
                >
                  <Button
                    danger
                    size="small"
                    icon={<DeleteOutlined />}
                    loading={clearingMarks}
                    disabled={!mappingActive || marks.length === 0}
                  >
                    清空标注
                  </Button>
                </Popconfirm>
              </Space>
            }
          >
            {marks.length > 0 ? (
              <List
                size="small"
                dataSource={marks}
                rowKey={(mark) => `${mark.label}@${mark.at}`}
                renderItem={(mark, index) => (
                  <List.Item
                    actions={[
                      <Button
                        key="delete"
                        danger
                        size="small"
                        type="text"
                        icon={<DeleteOutlined />}
                        loading={deletingMarkIndex === index}
                        disabled={!mappingActive}
                        onClick={() => void performDeleteMark(index)}
                      />,
                    ]}
                  >
                    <Space>
                      <span>{mark.label}</span>
                      <Typography.Text type="secondary">
                        （{mark.x.toFixed(2)}，{mark.y.toFixed(2)}）
                      </Typography.Text>
                    </Space>
                  </List.Item>
                )}
              />
            ) : (
              <Typography.Text type="secondary">
                暂无标注。建图模式下点「在当前位置标记」或按手柄 A
                键，把当前位置记录为书架点。
              </Typography.Text>
            )}
          </Card>
        </Col>
      </Row>
      <Modal
        destroyOnClose
        open={saveOpen}
        title="保存地图并入库"
        okText="保存并入库"
        cancelText="取消"
        confirmLoading={saving}
        onCancel={() => setSaveOpen(false)}
        onOk={() => void submitSave()}
      >
        <Typography.Paragraph type="secondary">
          将当前 SLAM 结果保存为 ROS 地图并自动导入地图库；留空名称时按“手动建图
          &lt;地图ID&gt;”命名。
        </Typography.Paragraph>
        <Input
          value={saveName}
          maxLength={120}
          placeholder="地图名称，例如：一层 302 实验室"
          onChange={(event) => setSaveName(event.target.value)}
        />
      </Modal>
      <Modal
        open={Boolean(savedMap)}
        title="地图已入库"
        onCancel={() => setSavedMap(undefined)}
        footer={[
          <Button key="close" onClick={() => setSavedMap(undefined)}>
            继续建图
          </Button>,
          <Button
            key="goto"
            type="primary"
            onClick={() => history.push('/library/map')}
          >
            前往地图建模页查看
          </Button>,
        ]}
      >
        {savedMap ? (
          <>
            {savedMap.alreadySaved ? (
              <Alert
                showIcon
                type="info"
                message="本次建图已保存过，地图 ID 相同"
                style={{ marginBottom: 16 }}
              />
            ) : null}
            <Descriptions column={1} size="small">
              <Descriptions.Item label="地图名称">
                {savedMap.name}
              </Descriptions.Item>
              <Descriptions.Item label="地图 ID">
                {savedMap.id}
              </Descriptions.Item>
              <Descriptions.Item label="版本">
                {savedMap.version}
              </Descriptions.Item>
              <Descriptions.Item label="尺寸">
                {savedMap.width} × {savedMap.height} px
              </Descriptions.Item>
              <Descriptions.Item label="分辨率">
                {savedMap.resolution} m/px
              </Descriptions.Item>
              <Descriptions.Item label="书架点">
                导入书架点 {savedMap.importedMarks} 条（跳过{' '}
                {savedMap.skippedMarks} 条）
              </Descriptions.Item>
            </Descriptions>
            <Alert
              showIcon
              type="info"
              message="地图以草稿状态入库，可在地图建模页查看、标注并设为当前地图。"
              style={{ marginTop: 16 }}
            />
          </>
        ) : null}
      </Modal>
    </PageContainer>
  );
}
