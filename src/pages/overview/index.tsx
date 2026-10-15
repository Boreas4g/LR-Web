import {
  AlertOutlined,
  BranchesOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  EnvironmentOutlined,
  LockOutlined,
  LogoutOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  RobotOutlined,
  RocketOutlined,
  SettingOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  App,
  Badge,
  Button,
  Col,
  Descriptions,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { history, useModel } from '@umijs/max';
import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  acknowledgeAlert,
  type BookarmStartPolicy,
  getAlerts,
  getBookarmStartPolicy,
  getRobotRealtimeState,
  getRobots,
  getTasks,
  restartAllServices,
  setBookarmStartPolicy,
  type StartAllResult,
  startAllServices,
  subscribeRobotRealtime,
} from '@/services/libraryRobot';
import {
  getHardwareStatus,
  type HardwareStatus,
} from '@/services/libraryRobot/studio';
import type {
  AlertRecord,
  DispatchTask,
  RobotSummary,
} from '@/services/libraryRobot/types';
import { errorMessage } from '@/utils/errorText';
import {
  isDebugPath,
  setDebugMode,
  useDebugMode,
  verifyDebugPassword,
} from '@/utils/debugMode';
import { AlertIcon, RobotStatusTag } from '../shared';
import { useStyles } from './style';

const taskStatus: Record<
  DispatchTask['status'],
  { label: string; color: string }
> = {
  queued: { label: '排队中', color: 'default' },
  running: { label: '执行中', color: 'processing' },
  paused: { label: '已暂停', color: 'warning' },
  failed: { label: '失败', color: 'error' },
  completed: { label: '已完成', color: 'success' },
  cancelled: { label: '已取消', color: 'default' },
  recovery_required: { label: '待人工确认', color: 'volcano' },
};

const taskType: Record<DispatchTask['type'], string> = {
  borrow: '取书任务',
  return: '还书任务',
  inventory: '盘点任务',
  navigate: '导航任务',
  cruise: '巡航任务',
  charge: '回充任务',
  arm_test: '机械臂测试',
  takeout: '完整取书',
  multi_round: '多轮取还书',
  fine_tune: '低速精调',
};

export default function OverviewPage() {
  const { message } = App.useApp();
  const { styles } = useStyles();
  const [robots, setRobots] = useState<RobotSummary[]>([]);
  const [tasks, setTasks] = useState<DispatchTask[]>([]);
  const [alerts, setAlerts] = useState<AlertRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string>();
  const [realtimeConnected, setRealtimeConnected] = useState(
    () => getRobotRealtimeState().connected,
  );
  const [lastUpdatedAt, setLastUpdatedAt] = useState<Date>();
  const [startAllLoading, setStartAllLoading] = useState(false);
  const [restartAllLoading, setRestartAllLoading] = useState(false);
  const [actionResult, setActionResult] = useState<{
    kind: 'start' | 'restart';
    result: StartAllResult;
  }>();
  const [hwStatus, setHwStatus] = useState<HardwareStatus>();
  const [hwLoading, setHwLoading] = useState(false);
  const [hwError, setHwError] = useState<string>();
  const [ackingAlertId, setAckingAlertId] = useState<string>();
  const [armPolicy, setArmPolicy] = useState<BookarmStartPolicy>();
  const [armPolicyLoading, setArmPolicyLoading] = useState(false);
  const { setInitialState } = useModel('@@initialState');
  const debugMode = useDebugMode();
  const [debugModalOpen, setDebugModalOpen] = useState(false);
  const [debugPassword, setDebugPassword] = useState('');

  // 切换调试模式后强制 layout 重渲染，使调试中心菜单即时显隐
  const bumpLayout = useCallback(() => {
    setInitialState((state) => (state ? { ...state } : state));
  }, [setInitialState]);

  const handleEnterDebug = useCallback(() => {
    if (!verifyDebugPassword(debugPassword.trim())) {
      message.error('调试密码错误');
      return;
    }
    setDebugMode(true);
    setDebugModalOpen(false);
    setDebugPassword('');
    message.success('已进入调试模式，调试中心已显示');
    bumpLayout();
  }, [debugPassword, message, bumpLayout]);

  const handleExitDebug = useCallback(() => {
    setDebugMode(false);
    message.success('已退出调试模式，调试中心已隐藏');
    bumpLayout();
    if (isDebugPath(window.location.pathname)) {
      history.push('/overview/dashboard');
    }
  }, [message, bumpLayout]);

  const refreshArmPolicy = useCallback(async () => {
    try {
      setArmPolicy(await getBookarmStartPolicy());
    } catch {
      setArmPolicy(undefined);
    }
  }, []);

  useEffect(() => {
    void refreshArmPolicy();
  }, [refreshArmPolicy]);

  async function toggleArmPolicy(allow: boolean) {
    setArmPolicyLoading(true);
    try {
      const updated = await setBookarmStartPolicy(allow);
      setArmPolicy(updated);
      if (updated.allow) {
        message.success('已确认：之后的一键启动将配置 can0 并启动机械臂服务。');
      } else {
        message.info('已取消确认：一键启动将跳过机械臂硬件步骤。');
      }
    } catch (error) {
      message.error(errorMessage(error, '更新机械臂硬件启动确认失败。'));
    } finally {
      setArmPolicyLoading(false);
    }
  }

  async function ackAlert(alert: AlertRecord) {
    setAckingAlertId(alert.id);
    try {
      const updated = await acknowledgeAlert(alert.id);
      setAlerts((previous) =>
        previous.map((item) => (item.id === updated.id ? updated : item)),
      );
      message.success(`告警「${alert.title}」已确认。`);
    } catch (error) {
      message.error(errorMessage(error, '确认告警失败。'));
    } finally {
      setAckingAlertId(undefined);
    }
  }

  const refreshHardware = useCallback(async () => {
    setHwLoading(true);
    try {
      setHwStatus(await getHardwareStatus());
      setHwError(undefined);
    } catch (error) {
      setHwError(errorMessage(error, '无法读取硬件真实状态。'));
    } finally {
      setHwLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshHardware();
    const timer = window.setInterval(() => void refreshHardware(), 15_000);
    return () => window.clearInterval(timer);
  }, [refreshHardware]);

  async function runStartAll() {
    setStartAllLoading(true);
    try {
      const result = await startAllServices();
      setActionResult({ kind: 'start', result });
      void refreshArmPolicy();
      if (result.ok) message.success('一键启动完成，全部依赖项就绪。');
      else message.warning('一键启动完成，但有组件失败，请查看明细。');
      const hardwareSkipped = result.steps.some(
        (step) =>
          (step.name === 'can0' || step.name === 'bookarm-service') &&
          step.status === 'SKIPPED',
      );
      if (hardwareSkipped) {
        message.info(
          '机械臂硬件步骤被跳过：现场检查后点击「机械臂硬件启动确认」放行，再重新一键启动。',
        );
      }
    } catch (error) {
      message.error(errorMessage(error, '一键启动失败。'));
    } finally {
      setStartAllLoading(false);
    }
  }

  async function runRestartAll() {
    setRestartAllLoading(true);
    try {
      const result = await restartAllServices();
      setActionResult({ kind: 'restart', result });
      if (result.ok) {
        message.success(
          '后端模块已重启；平台后端即将重启，页面将在约 25 秒后自动刷新。',
        );
        window.setTimeout(() => window.location.reload(), 25_000);
      } else {
        message.warning('部分后端模块重启失败，请查看明细。');
      }
    } catch (error) {
      message.error(errorMessage(error, '重启全部后端模块失败。'));
    } finally {
      setRestartAllLoading(false);
    }
  }

  const refresh = useCallback(
    async (initial = false) => {
      if (initial) setLoading(true);
      else setRefreshing(true);
      try {
        const [robotData, taskData, alertData] = await Promise.all([
          getRobots(),
          getTasks(),
          getAlerts(),
        ]);
        setRobots(robotData);
        setTasks(taskData);
        setAlerts(alertData);
        setLoadError(undefined);
        setLastUpdatedAt(new Date());
      } catch (error) {
        const detail = errorMessage(error, '无法读取运行总览数据。');
        setLoadError(detail);
        if (!initial) message.error(detail);
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [message],
  );

  useEffect(() => {
    void refresh(true);
    const timer = window.setInterval(() => void refresh(false), 15_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(
    () =>
      subscribeRobotRealtime(() => {
        const state = getRobotRealtimeState();
        setRealtimeConnected(state.connected);
        if (state.robots.length > 0) {
          setRobots(state.robots);
          setLastUpdatedAt(new Date());
        }
      }),
    [],
  );

  const online = robots.filter((robot) => robot.status !== 'offline').length;
  const runningTasks = tasks.filter((task) => task.status === 'running').length;
  const waitingAlerts = alerts.filter(
    (alert) => alert.severity !== 'info' && !alert.acknowledged,
  ).length;
  const activeTasks = useMemo(
    () =>
      tasks
        .filter((task) => ['queued', 'running', 'paused'].includes(task.status))
        .slice(0, 6),
    [tasks],
  );
  const visibleAlerts = useMemo(() => {
    const severityRank: Record<AlertRecord['severity'], number> = {
      critical: 0,
      warning: 1,
      info: 2,
    };
    return [...alerts]
      .sort(
        (left, right) =>
          Number(left.acknowledged) - Number(right.acknowledged) ||
          severityRank[left.severity] - severityRank[right.severity],
      )
      .slice(0, 6);
  }, [alerts]);

  return (
    <PageContainer
      title="运行总览"
      extra={[
        <Tooltip key="refresh" title="刷新运行数据">
          <Button
            aria-label="刷新运行数据"
            icon={<ReloadOutlined />}
            loading={refreshing}
            onClick={() => void refresh(false)}
          />
        </Tooltip>,
        <Button
          key="start-all"
          icon={<RocketOutlined />}
          loading={startAllLoading}
          onClick={() => void runStartAll()}
        >
          一键启动
        </Button>,
        armPolicy?.allow ? (
          <Popconfirm
            key="arm-policy"
            title="取消机械臂硬件启动确认？"
            description="取消后，一键启动将跳过 can0 配置与机械臂服务启动。"
            okText="取消确认"
            cancelText="返回"
            onConfirm={() => void toggleArmPolicy(false)}
          >
            <Button
              icon={<CheckCircleOutlined />}
              loading={armPolicyLoading}
            >
              机械臂硬件：已确认
            </Button>
          </Popconfirm>
        ) : (
          <Popconfirm
            key="arm-policy"
            title="确认允许一键启动拉起机械臂硬件？"
            description="仅在已现场确认急停有效、供电正常、关节 3/5 无异常后开启。开启后每次一键启动都会配置 can0 并启动机械臂服务。"
            okText="现场确认并开启"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => void toggleArmPolicy(true)}
          >
            <Button
              danger
              icon={<WarningOutlined />}
              loading={armPolicyLoading}
            >
              机械臂硬件启动确认
            </Button>
          </Popconfirm>
        ),
        <Popconfirm
          key="restart-all"
          title="重启全部后端功能模块？"
          description="将重启机械臂服务、导航栈、建图代理与平台后端，期间页面会短暂失联。请确认机器人处于安全状态。"
          okText="确认重启"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          onConfirm={() => void runRestartAll()}
        >
          <Button
            danger
            icon={<PoweroffOutlined />}
            loading={restartAllLoading}
          >
            重启全部
          </Button>
        </Popconfirm>,
        debugMode ? (
          <Button
            key="debug-exit"
            danger
            icon={<LogoutOutlined />}
            onClick={handleExitDebug}
          >
            退出调试模式
          </Button>
        ) : (
          <Button
            key="debug-enter"
            icon={<LockOutlined />}
            onClick={() => setDebugModalOpen(true)}
          >
            进入调试模式
          </Button>
        ),
        <Button
          key="robots"
          type="primary"
          icon={<SettingOutlined />}
          href="/overview/robots"
        >
          设备管理
        </Button>,
      ]}
    >
      {loadError ? (
        <Alert
          showIcon
          closable
          className={styles.errorAlert}
          type="error"
          message="运行数据加载失败"
          description={loadError}
          onClose={() => setLoadError(undefined)}
        />
      ) : null}

      <section className={styles.summaryBand} aria-label="运行摘要">
        <div className={styles.summaryItem}>
          <span className={`${styles.summaryIcon} ${styles.blueIcon}`}>
            <RobotOutlined />
          </span>
          <div className={styles.summaryContent}>
            <Typography.Text type="secondary">在线机器人</Typography.Text>
            <div className={styles.summaryValue}>
              {online}
              <small>/ {robots.length}</small>
            </div>
            <span className={styles.summaryHint}>
              {robots.length - online} 台离线
            </span>
          </div>
        </div>
        <div className={styles.summaryItem}>
          <span className={`${styles.summaryIcon} ${styles.greenIcon}`}>
            <BranchesOutlined />
          </span>
          <div className={styles.summaryContent}>
            <Typography.Text type="secondary">执行中任务</Typography.Text>
            <div className={styles.summaryValue}>{runningTasks}</div>
            <span className={styles.summaryHint}>
              {activeTasks.length} 项活动任务
            </span>
          </div>
        </div>
        <div className={styles.summaryItem}>
          <span className={`${styles.summaryIcon} ${styles.cyanIcon}`}>
            <CheckCircleOutlined />
          </span>
          <div className={styles.summaryContent}>
            <Typography.Text type="secondary">机械臂反馈</Typography.Text>
            <div className={styles.summaryValue}>
              {hwStatus
                ? hwStatus.arm.health?.healthy_feedback
                  ? '正常'
                  : '异常'
                : '--'}
            </div>
            <span className={styles.summaryHint}>bookarm 直连检查</span>
          </div>
        </div>
        <div className={styles.summaryItem}>
          <span className={`${styles.summaryIcon} ${styles.redIcon}`}>
            <WarningOutlined />
          </span>
          <div className={styles.summaryContent}>
            <Typography.Text type="secondary">待处理异常</Typography.Text>
            <div className={styles.summaryValue}>{waitingAlerts}</div>
            <span className={styles.summaryHint}>
              {alerts.length} 条告警记录
            </span>
          </div>
        </div>
      </section>

      <section className={styles.fleetSection}>
        <div className={styles.sectionHeader}>
          <div>
            <Typography.Title level={4}>机器人与设备</Typography.Title>
            <Typography.Text type="secondary">
              {lastUpdatedAt
                ? `最近更新 ${lastUpdatedAt.toLocaleTimeString('zh-CN', { hour12: false })}`
                : '等待状态数据'}
            </Typography.Text>
          </div>
          <Badge
            status={realtimeConnected ? 'success' : 'warning'}
            text={realtimeConnected ? '实时链路在线' : '实时链路重连中'}
          />
        </div>

        <div className={styles.robotGrid}>
          {robots.map((robot) => (
            <article className={styles.robotCard} key={robot.id}>
              <Row gutter={[16, 16]}>
                <Col xs={24} xl={7}>
                  <header className={styles.robotHeader}>
                    <div className={styles.robotIdentity}>
                      <span className={styles.robotAvatar}>
                        <RobotOutlined />
                      </span>
                      <div className={styles.primaryText}>
                        <Typography.Text strong>{robot.name}</Typography.Text>
                        <Typography.Text type="secondary">
                          {robot.id} · {robot.mapVersion}
                        </Typography.Text>
                      </div>
                    </div>
                    <RobotStatusTag status={robot.status} />
                  </header>

                  <div className={styles.robotContext}>
                    <div className={styles.contextItem}>
                      <EnvironmentOutlined />
                      <div className={styles.primaryText}>
                        <Typography.Text type="secondary">
                          当前位置
                        </Typography.Text>
                        <Typography.Text ellipsis={{ tooltip: robot.location }}>
                          {robot.location}
                        </Typography.Text>
                      </div>
                    </div>
                    <div className={styles.contextItem}>
                      <BranchesOutlined />
                      <div className={styles.primaryText}>
                        <Typography.Text type="secondary">
                          当前任务
                        </Typography.Text>
                        <Typography.Text
                          ellipsis={{ tooltip: robot.currentTask }}
                        >
                          {robot.currentTask || '无活动任务'}
                        </Typography.Text>
                      </div>
                    </div>
                  </div>
                </Col>

                <Col xs={24} xl={17}>
                  <div
                    style={{
                      alignItems: 'center',
                      display: 'flex',
                      justifyContent: 'space-between',
                    }}
                  >
                    <Typography.Text strong>
                      硬件真实状态（bookarm 直连检查）
                    </Typography.Text>
                    <Button
                      icon={<ReloadOutlined />}
                      loading={hwLoading}
                      onClick={() => void refreshHardware()}
                      size="small"
                      type="text"
                    />
                  </div>
                  {hwError ? (
                    <Alert
                      showIcon
                      message={hwError}
                      style={{ marginTop: 8 }}
                      type="error"
                    />
                  ) : null}
                  {hwStatus ? (
                    <>
                      <Space size={[8, 8]} style={{ marginTop: 8 }} wrap>
                        <Tag color={hwStatus.can.up ? 'success' : 'error'}>
                          CAN {hwStatus.can.up ? '在线' : '离线'}
                        </Tag>
                        <Tag
                          color={
                            hwStatus.arm.health?.healthy_feedback
                              ? 'success'
                              : 'error'
                          }
                        >
                          机械臂反馈
                          {hwStatus.arm.health?.healthy_feedback
                            ? '正常'
                            : '异常'}
                        </Tag>
                        <Tag
                          color={hwStatus.camera.present ? 'success' : 'error'}
                        >
                          D435i {hwStatus.camera.present ? '在线' : '离线'}
                        </Tag>
                        {Object.entries(hwStatus.devices).map(
                          ([name, exists]) => (
                            <Tag
                              color={exists ? 'success' : 'default'}
                              key={name}
                            >
                              /dev/{name}
                            </Tag>
                          ),
                        )}
                      </Space>
                      <Descriptions
                        column={{ xs: 1, sm: 2 }}
                        size="small"
                        style={{ marginTop: 8 }}
                      >
                        <Descriptions.Item label="升降台">
                          {hwStatus.lift.connected
                            ? `${hwStatus.lift.height_mm?.toFixed(1)} mm`
                            : (hwStatus.lift.error ?? '未连接')}
                        </Descriptions.Item>
                        <Descriptions.Item label="推杆">
                          {hwStatus.rod.connected
                            ? `${hwStatus.rod.angle_deg?.toFixed(1)}°`
                            : (hwStatus.rod.error ?? '未连接')}
                        </Descriptions.Item>
                      </Descriptions>
                      <Row gutter={[6, 6]} style={{ marginTop: 4 }}>
                        {Array.from({ length: 7 }, (_, index) => {
                          const joint = index + 1;
                          const poseRad =
                            hwStatus.arm.pose?.values_rad?.[index];
                          const temp =
                            hwStatus.arm.thermal?.temperatures_c?.[
                              String(joint)
                            ];
                          const fault =
                            hwStatus.arm.health?.joint_feedback?.[String(joint)]
                              ?.fault_code;
                          return (
                            <Col flex="1 0 13%" key={joint}>
                              <div
                                style={{
                                  background:
                                    fault && fault !== 0
                                      ? '#fff1f0'
                                      : '#fafafa',
                                  border: '1px solid #f0f0f0',
                                  borderRadius: 6,
                                  padding: '4px 6px',
                                  textAlign: 'center',
                                }}
                              >
                                <div>
                                  <Typography.Text strong>
                                    轴{joint}
                                  </Typography.Text>
                                </div>
                                <div>
                                  <Typography.Text
                                    style={{ fontSize: 13 }}
                                    type={fault ? 'danger' : undefined}
                                  >
                                    {poseRad === undefined
                                      ? '--'
                                      : `${((poseRad * 180) / Math.PI).toFixed(1)}°`}
                                    {' / '}
                                    {temp === undefined
                                      ? '--'
                                      : `${temp.toFixed(0)}°C`}
                                  </Typography.Text>
                                </div>
                                {fault ? (
                                  <Typography.Text
                                    style={{ fontSize: 11 }}
                                    type="danger"
                                  >
                                    故障码 {fault}
                                  </Typography.Text>
                                ) : null}
                              </div>
                            </Col>
                          );
                        })}
                      </Row>
                    </>
                  ) : !hwError ? (
                    <Typography.Text type="secondary">
                      正在读取硬件状态…
                    </Typography.Text>
                  ) : null}
                </Col>
              </Row>
            </article>
          ))}
          {!loading && robots.length === 0 ? (
            <div className={styles.emptyState}>
              <Empty description="暂无机器人状态" />
            </div>
          ) : null}
          {loading && robots.length === 0 ? (
            <div className={styles.loadingState}>正在加载设备状态</div>
          ) : null}
        </div>
      </section>

      <div className={styles.operationsGrid}>
        <section className={styles.panel}>
          <div className={styles.panelHeader}>
            <div>
              <Typography.Title level={5}>活动任务</Typography.Title>
              <Typography.Text type="secondary">
                按调度队列实时排序
              </Typography.Text>
            </div>
          </div>
          <div className={styles.taskList}>
            {activeTasks.map((task) => (
              <div className={styles.taskRow} key={task.id}>
                <span className={styles.taskIcon}>
                  <BranchesOutlined />
                </span>
                <div className={styles.taskMain}>
                  <div className={styles.taskTitle}>
                    <Typography.Text strong>
                      {task.bookTitle || taskType[task.type]}
                    </Typography.Text>
                    <Tag color={taskStatus[task.status].color}>
                      {taskStatus[task.status].label}
                    </Tag>
                  </div>
                  <div className={styles.taskMeta}>
                    <span>{task.id}</span>
                    <span>{task.robotId}</span>
                    <span>{task.target}</span>
                  </div>
                  <Progress
                    percent={task.progress}
                    size="small"
                    showInfo={false}
                  />
                </div>
                <strong className={styles.progressValue}>
                  {task.progress}%
                </strong>
              </div>
            ))}
            {!loading && activeTasks.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="当前没有活动任务"
              />
            ) : null}
          </div>
        </section>

        <section className={styles.panel}>
          <div className={styles.panelHeader}>
            <div>
              <Typography.Title level={5}>告警队列</Typography.Title>
              <Typography.Text type="secondary">未处理告警优先</Typography.Text>
            </div>
          </div>
          <div className={styles.alertList}>
            {visibleAlerts.map((alert) => (
              <div className={styles.alertRow} key={alert.id}>
                <span className={styles.alertIcon}>
                  <AlertIcon severity={alert.severity} />
                </span>
                <div className={styles.alertMain}>
                  <div className={styles.alertTitle}>
                    <Typography.Text strong>{alert.title}</Typography.Text>
                    {alert.acknowledged ? (
                      <Tag>已确认</Tag>
                    ) : (
                      <Button
                        size="small"
                        type="link"
                        loading={ackingAlertId === alert.id}
                        onClick={() => void ackAlert(alert)}
                      >
                        确认
                      </Button>
                    )}
                  </div>
                  <Space
                    size={6}
                    split={<span className={styles.divider}>·</span>}
                  >
                    <span>
                      <AlertOutlined /> {alert.source}
                    </span>
                    <span>
                      <ClockCircleOutlined /> {alert.time}
                    </span>
                  </Space>
                </div>
              </div>
            ))}
            {!loading && visibleAlerts.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="当前没有告警"
              />
            ) : null}
          </div>
        </section>
      </div>
      <Modal
        open={actionResult !== undefined}
        title={
          actionResult?.kind === 'start'
            ? actionResult.result.ok
              ? '一键启动完成'
              : '一键启动（有组件失败）'
            : actionResult?.result.ok
              ? '后端模块重启完成'
              : '后端模块重启（有组件失败）'
        }
        footer={null}
        onCancel={() => setActionResult(undefined)}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={8}>
          {actionResult?.result.steps.map((step) => (
            <Space key={step.name} size={8}>
              <Tag
                color={
                  step.status === 'OK'
                    ? 'success'
                    : step.status === 'STARTED' || step.status === 'RESTARTED'
                      ? 'processing'
                      : step.status === 'FAILED'
                        ? 'error'
                        : 'default'
                }
              >
                {step.status}
              </Tag>
              <Typography.Text strong>{step.name}</Typography.Text>
              <Typography.Text type="secondary">{step.message}</Typography.Text>
            </Space>
          ))}
          {actionResult && !actionResult.result.ok ? (
            <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
              {actionResult.kind === 'start'
                ? '有组件未就绪，可按提示日志排查后重新点击「一键启动」（幂等，可重复执行）。'
                : '有模块重启失败，可按提示日志排查后重新点击「重启全部」。'}
            </Typography.Paragraph>
          ) : null}
          {actionResult?.kind === 'restart' && actionResult.result.ok ? (
            <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
              平台后端正在重启，接口短暂失联属预期，页面将自动刷新。
            </Typography.Paragraph>
          ) : null}
        </Space>
      </Modal>
      <Modal
        title="进入调试模式"
        open={debugModalOpen}
        okText="确认进入"
        cancelText="取消"
        onOk={handleEnterDebug}
        onCancel={() => {
          setDebugModalOpen(false);
          setDebugPassword('');
        }}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary">
          调试中心面向维护人员，输入调试密码后显示全部调试功能。
        </Typography.Paragraph>
        <Input.Password
          autoFocus
          placeholder="请输入调试密码"
          value={debugPassword}
          onChange={(e) => setDebugPassword(e.target.value)}
          onPressEnter={handleEnterDebug}
        />
      </Modal>
    </PageContainer>
  );
}
