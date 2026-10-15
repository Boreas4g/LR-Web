import {
  ApiOutlined,
  BugOutlined,
  ClearOutlined,
  MinusOutlined,
  PlusOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  RocketOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  InputNumber,
  Popconfirm,
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd';
import { useCallback, useEffect, useState } from 'react';

import {
  type CameraTransformView,
  cartesianJog,
  clearArmThermalLockout,
  disableArm,
  enableArm,
  getCameraTransform,
  getHardwareStatus,
  type HardwareStatus,
  type JogAxis,
  moveLift,
  moveRod,
  resetArmToDebugPose,
  resetCameraTransform,
  StudioApiError,
  saveCameraTransform,
  type TcpPoseView,
} from '@/services/libraryRobot/studio';
import { errorMessage } from '@/utils/errorText';

const RAD_TO_DEG = 180 / Math.PI;
const JOINT_NAMES = ['J1', 'J2', 'J3', 'J4', 'J5', 'J6', '夹爪'];

function apiErrorStatus(error: unknown) {
  if (error instanceof StudioApiError) return error.status;
  if (error && typeof error === 'object' && 'status' in error) {
    const status = (error as { status: unknown }).status;
    if (typeof status === 'number') return status;
  }
  return undefined;
}

function okTag(
  value: boolean | undefined | null,
  okText = '正常',
  badText = '异常',
) {
  if (value === undefined || value === null) return <Tag>未知</Tag>;
  return value ? (
    <Tag color="success">{okText}</Tag>
  ) : (
    <Tag color="error">{badText}</Tag>
  );
}

function numberText(value: number | null | undefined, digits = 1, suffix = '') {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return `${value.toFixed(digits)}${suffix}`;
}

const JOG_AXES: { key: JogAxis; label: string }[] = [
  { key: 'x', label: 'X' },
  { key: 'y', label: 'Y' },
  { key: 'z', label: 'Z' },
  { key: 'rx', label: 'Rx' },
  { key: 'ry', label: 'Ry' },
  { key: 'rz', label: 'Rz' },
];

const POSE_FIELDS: {
  key: keyof TcpPoseView;
  label: string;
  addon: string;
  step: number;
  precision: number;
}[] = [
  { key: 'x_mm', label: 'X', addon: 'mm', step: 1, precision: 1 },
  { key: 'y_mm', label: 'Y', addon: 'mm', step: 1, precision: 1 },
  { key: 'z_mm', label: 'Z', addon: 'mm', step: 1, precision: 1 },
  { key: 'rx_deg', label: 'Rx', addon: '°', step: 0.1, precision: 2 },
  { key: 'ry_deg', label: 'Ry', addon: '°', step: 0.1, precision: 2 },
  { key: 'rz_deg', label: 'Rz', addon: '°', step: 0.1, precision: 2 },
];

function pose6Of(view: CameraTransformView): TcpPoseView {
  const { x_mm, y_mm, z_mm, rx_deg, ry_deg, rz_deg } = view;
  return { x_mm, y_mm, z_mm, rx_deg, ry_deg, rz_deg };
}

export default function HardwareDebugPage() {
  const { message, modal } = App.useApp();
  const [status, setStatus] = useState<HardwareStatus>();
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string>();
  const [action, setAction] = useState<string>();
  const [lastUpdated, setLastUpdated] = useState<Date>();

  const [liftTarget, setLiftTarget] = useState(800);
  const [liftSpeed, setLiftSpeed] = useState(2);
  const [rodTarget, setRodTarget] = useState(0);
  const [rodSpeed, setRodSpeed] = useState(1000);
  const [rodAcceleration, setRodAcceleration] = useState(50);
  const [rodTolerance, setRodTolerance] = useState(1.5);
  const [rodTimeout, setRodTimeout] = useState(12);
  const [armDuration, setArmDuration] = useState(3);

  const [posStep, setPosStep] = useState(10);
  const [rotStep, setRotStep] = useState(5);
  const [transform, setTransform] = useState<CameraTransformView>();
  const [transformDraft, setTransformDraft] = useState<TcpPoseView>();
  const [transformError, setTransformError] = useState<string>();
  const [transformSaving, setTransformSaving] = useState(false);
  const [transformResetting, setTransformResetting] = useState(false);

  const refresh = useCallback(async (silent = false) => {
    if (!silent) setRefreshing(true);
    try {
      const next = await getHardwareStatus();
      setStatus(next);
      setError(undefined);
      setLastUpdated(new Date());
    } catch (err) {
      setError(errorMessage(err, '无法读取硬件状态。'));
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refresh(true);
    const timer = window.setInterval(() => void refresh(true), 5_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function runAction(
    key: string,
    actionFn: () => Promise<void>,
    fallback: string,
  ) {
    setAction(key);
    try {
      await actionFn();
      await refresh(true);
    } catch (err) {
      if (apiErrorStatus(err) === 409) {
        message.warning('服务正忙：请等待当前动作结束后再试。');
      } else {
        message.error(errorMessage(err, fallback));
      }
    } finally {
      setAction(undefined);
    }
  }

  const refreshTransform = useCallback(async () => {
    try {
      const view = await getCameraTransform();
      setTransform(view);
      setTransformDraft(pose6Of(view));
      setTransformError(undefined);
    } catch (err) {
      setTransformError(errorMessage(err, '无法读取 D435 外参。'));
    }
  }, []);

  useEffect(() => {
    void refreshTransform();
  }, [refreshTransform]);

  async function jog(axis: JogAxis, sign: 1 | -1) {
    const key = `jog-${axis}${sign > 0 ? '+' : '-'}`;
    const isPosition = axis === 'x' || axis === 'y' || axis === 'z';
    setAction(key);
    try {
      const result = await cartesianJog({
        axis,
        delta: (isPosition ? posStep : rotStep) * sign,
      });
      message.success(
        `点动完成（${axis} ${result.delta_applied.toFixed(1)}${isPosition ? ' mm' : '°'}）。`,
      );
      await refresh(true);
    } catch (err) {
      // 409 可能是未使能或服务正忙，服务端 detail 更有信息量。
      if (apiErrorStatus(err) === 409) {
        message.warning(errorMessage(err, '操作冲突。'));
      } else {
        message.error(errorMessage(err, '点动失败。'));
      }
    } finally {
      setAction(undefined);
    }
  }

  async function applyTransform() {
    if (!transformDraft) return;
    setTransformSaving(true);
    try {
      const view = await saveCameraTransform(transformDraft);
      setTransform(view);
      setTransformDraft(pose6Of(view));
      message.success('外参已热生效并写入 camera_to_base.json。');
    } catch (err) {
      message.error(errorMessage(err, '外参校验失败，未写入。'));
    } finally {
      setTransformSaving(false);
    }
  }

  async function restoreDefaultTransform() {
    setTransformResetting(true);
    try {
      const view = await resetCameraTransform();
      setTransform(view);
      setTransformDraft(pose6Of(view));
      message.success('已删除用户覆盖文件，恢复示例外参。');
    } catch (err) {
      message.error(errorMessage(err, '恢复默认失败。'));
    } finally {
      setTransformResetting(false);
    }
  }

  function confirmLiftMove() {
    modal.confirm({
      title: `确认移动升降台到 ${liftTarget} mm？`,
      content:
        '升降台将执行真实机械运动，到位前接口会一直阻塞。请确认运动范围内无人、升降台周边无干涉且实体急停可触达。',
      okText: '确认安全并移动',
      cancelText: '再检查一下',
      okButtonProps: { danger: true },
      onOk: () =>
        runAction(
          'lift',
          async () => {
            const result = await moveLift(liftTarget, liftSpeed);
            setStatus((previous) =>
              previous
                ? {
                    ...previous,
                    lift: {
                      ...previous.lift,
                      connected: true,
                      height_mm: result.measured_height_mm,
                      error: null,
                    },
                  }
                : previous,
            );
            message.success(
              `升降台已到位：实测 ${result.measured_height_mm.toFixed(1)} mm。`,
            );
          },
          '升降台移动失败。',
        ),
    });
  }

  function confirmRodMove(targetDeg: number, label = '移动') {
    modal.confirm({
      title: `确认推杆${label}到 ${targetDeg}°？`,
      content:
        '推杆将执行真实机械运动，请确认推杆运动范围内无人、无书本或周边无干涉，实体急停可触达。',
      okText: '确认安全并执行',
      cancelText: '再检查一下',
      okButtonProps: { danger: true },
      onOk: () =>
        runAction(
          'rod',
          async () => {
            const result = await moveRod(targetDeg, {
              speed: rodSpeed,
              acceleration: rodAcceleration,
              toleranceDeg: rodTolerance,
              timeoutS: rodTimeout,
            });
            setStatus((previous) =>
              previous
                ? {
                    ...previous,
                    rod: {
                      ...previous.rod,
                      connected: true,
                      angle_deg: result.measured_angle_deg,
                      error: null,
                    },
                  }
                : previous,
            );
            message.success(
              `推杆已到位：实测 ${result.measured_angle_deg.toFixed(1)}°。`,
            );
          },
          '推杆移动失败。',
        ),
    });
  }

  const arm = status?.arm;
  const health = arm?.health;
  const thermal = arm?.thermal;
  const tcp = arm?.pose?.tcp;
  const lift = status?.lift;
  const rod = status?.rod;
  const devices = status?.devices ?? {};
  const transformDirty =
    transform !== undefined &&
    transformDraft !== undefined &&
    JSON.stringify(pose6Of(transform)) !== JSON.stringify(transformDraft);

  return (
    <PageContainer
      title="硬件调试"
      content="集中检查 CAN、机械臂、升降台、推杆与 D435 的连接和实时状态，并可执行安全调试动作。"
      extra={[
        <Button
          key="refresh"
          icon={<ReloadOutlined />}
          loading={refreshing}
          onClick={() => void refresh(false)}
        >
          刷新状态
        </Button>,
      ]}
    >
      {error && !status ? (
        <Alert
          showIcon
          type="error"
          message="无法读取硬件状态"
          description={`${error} 请确认 bookarm 服务已启动且 /bookarm-api 代理可用。`}
          style={{ marginBottom: 16 }}
        />
      ) : null}

      <Card
        size="small"
        title="连接总览"
        style={{ marginBottom: 16 }}
        extra={
          lastUpdated ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {lastUpdated.toLocaleTimeString('zh-CN', { hour12: false })}
            </Typography.Text>
          ) : null
        }
      >
        {!status ? (
          <Space>
            <Spin />
            <Typography.Text type="secondary">
              正在读取硬件状态…
            </Typography.Text>
          </Space>
        ) : (
          <Space wrap size={[8, 8]}>
            <Tag
              icon={<ApiOutlined />}
              color={status.can.exists && status.can.up ? 'success' : 'error'}
            >
              CAN {status.can.up ? '在线' : '离线'}
            </Tag>
            <Tag
              icon={<BugOutlined />}
              color={
                arm?.wired
                  ? health?.healthy_feedback
                    ? 'success'
                    : health
                      ? 'warning'
                      : 'default'
                  : 'default'
              }
            >
              机械臂{' '}
              {arm?.wired
                ? health?.healthy_feedback
                  ? '反馈正常'
                  : health
                    ? '反馈异常'
                    : '状态未知'
                : '未接入'}
            </Tag>
            <Tag color={lift?.connected ? 'success' : 'error'}>
              升降台 {lift?.connected ? '在线' : '离线'}
            </Tag>
            <Tag
              color={
                rod?.connected
                  ? 'success'
                  : rod?.connected === undefined
                    ? 'default'
                    : 'error'
              }
            >
              推杆{' '}
              {rod?.connected
                ? '在线'
                : rod?.connected === undefined
                  ? '未知'
                  : '离线'}
            </Tag>
            <Tag
              color={
                status.camera.present
                  ? 'success'
                  : status.camera.present === null
                    ? 'default'
                    : 'error'
              }
            >
              D435{' '}
              {status.camera.present
                ? '在线'
                : status.camera.present === null
                  ? '未知'
                  : '离线'}
            </Tag>
            {Object.entries(devices).map(([name, exists]) => (
              <Tag key={name} color={exists ? 'success' : 'default'}>
                /dev/{name} {exists ? '存在' : '缺失'}
              </Tag>
            ))}
          </Space>
        )}
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} xl={15}>
          <Card
            size="small"
            title="机械臂"
            extra={
              <Space wrap>
                <Popconfirm
                  title="运动到调试位？"
                  description="机械臂将使能（如需要）并运动到调试位。请确认运动范围内无人、周边无干涉且实体急停可触达。"
                  okText="确认运动"
                  cancelText="再检查一下"
                  okButtonProps={{ danger: true }}
                  onConfirm={() =>
                    void runAction(
                      'arm-debug',
                      async () => {
                        await resetArmToDebugPose(armDuration);
                        message.success('机械臂已运动到调试位。');
                      },
                      '运动到调试位失败。',
                    )
                  }
                >
                  <Button
                    icon={<RocketOutlined />}
                    loading={action === 'arm-debug'}
                    disabled={action !== undefined && action !== 'arm-debug'}
                  >
                    调试位
                  </Button>
                </Popconfirm>
                <Button
                  icon={<ThunderboltOutlined />}
                  loading={action === 'arm-enable'}
                  disabled={action !== undefined && action !== 'arm-enable'}
                  onClick={() =>
                    void runAction(
                      'arm-enable',
                      async () => {
                        await enableArm();
                        message.success('机械臂已使能。');
                      },
                      '机械臂使能失败。',
                    )
                  }
                >
                  使能
                </Button>
                <Popconfirm
                  title="失能机械臂？"
                  description="失能前请确认机械臂已停靠在安全位姿：失能后关节失去保持力矩，臂体可能在重力作用下缓慢下垂。请确认实体急停可触达。"
                  okText="确认失能"
                  cancelText="再检查一下"
                  okButtonProps={{ danger: true }}
                  onConfirm={() =>
                    void runAction(
                      'arm-disable',
                      async () => {
                        await disableArm(armDuration);
                        message.success('机械臂已失能。');
                      },
                      '机械臂失能失败。',
                    )
                  }
                >
                  <Button
                    danger
                    icon={<PoweroffOutlined />}
                    loading={action === 'arm-disable'}
                    disabled={action !== undefined && action !== 'arm-disable'}
                  >
                    失能
                  </Button>
                </Popconfirm>
                {thermal?.lockout ? (
                  <Popconfirm
                    title="清除温度保护锁定？"
                    description="仅在确认机械臂已冷却且现场安全后操作。"
                    okText="确认清除"
                    cancelText="取消"
                    onConfirm={() =>
                      void runAction(
                        'thermal-clear',
                        async () => {
                          await clearArmThermalLockout();
                          message.success('温度保护锁定已清除。');
                        },
                        '清除温度锁定失败。',
                      )
                    }
                  >
                    <Button
                      icon={<ClearOutlined />}
                      loading={action === 'thermal-clear'}
                      disabled={
                        action !== undefined && action !== 'thermal-clear'
                      }
                    >
                      清除温度锁定
                    </Button>
                  </Popconfirm>
                ) : null}
              </Space>
            }
          >
            {arm?.health_error ? (
              <Alert
                showIcon
                type="warning"
                message="机械臂健康检查异常"
                description={arm.health_error}
                style={{ marginBottom: 12 }}
              />
            ) : null}
            {arm?.pose_error ? (
              <Alert
                showIcon
                type="warning"
                message="机械臂位姿读取异常"
                description={arm.pose_error}
                style={{ marginBottom: 12 }}
              />
            ) : null}
            <Descriptions
              column={{ xs: 1, sm: 2, md: 3 }}
              size="small"
              style={{ marginBottom: 12 }}
            >
              <Descriptions.Item label="接入">
                {arm?.wired ? (
                  <Tag color="success">已接入</Tag>
                ) : (
                  <Tag>未接入</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="使能状态">
                {health ? okTag(health.enabled, '已使能', '未使能') : '--'}
              </Descriptions.Item>
              <Descriptions.Item label="反馈健康">
                {health ? okTag(health.healthy_feedback, '健康', '异常') : '--'}
              </Descriptions.Item>
              <Descriptions.Item label="CAN 帧率">
                {health ? `${health.can_fps.toFixed(1)} fps` : '--'}
              </Descriptions.Item>
              <Descriptions.Item label="控制环">
                {health
                  ? okTag(health.control_loop_running, '运行中', '未运行')
                  : '--'}
              </Descriptions.Item>
              <Descriptions.Item label="温度保护">
                {thermal ? (
                  thermal.lockout ? (
                    <Space size={4} wrap>
                      <Tag color="error">
                        {thermal.lockout.safe_posed
                          ? '已安全归位后失能'
                          : '归位失败直接失能'}
                      </Tag>
                      <Typography.Text
                        type="secondary"
                        style={{ fontSize: 12 }}
                      >
                        {thermal.lockout.message}
                      </Typography.Text>
                    </Space>
                  ) : (
                    <Tag color="success">未锁定</Tag>
                  )
                ) : (
                  <Tag>未知</Tag>
                )}
              </Descriptions.Item>
            </Descriptions>
            {health?.reason ? (
              <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
                反馈说明：{health.reason}
              </Typography.Paragraph>
            ) : null}

            <Typography.Text
              strong
              style={{ display: 'block', marginBottom: 8 }}
            >
              各轴位置 / 温度 / 反馈
            </Typography.Text>
            <Row gutter={[8, 8]}>
              {Array.from({ length: 7 }, (_, index) => {
                const joint = index + 1;
                const poseRad = arm?.pose?.values_rad?.[index];
                const temp = thermal?.temperatures_c?.[String(joint)];
                const feedback = health?.joint_feedback?.[String(joint)];
                const danger =
                  temp !== undefined &&
                  thermal != null &&
                  temp >= thermal.limit_c;
                const warn =
                  temp !== undefined &&
                  thermal != null &&
                  temp >= thermal.limit_c - 15 &&
                  !danger;
                return (
                  <Col xs={12} sm={8} md={6} xl={6} key={joint}>
                    <div
                      style={{
                        background: danger
                          ? '#fff1f0'
                          : warn
                            ? '#fffbe6'
                            : '#fafafa',
                        border: '1px solid #f0f0f0',
                        borderRadius: 6,
                        padding: '6px 8px',
                        minHeight: 86,
                      }}
                    >
                      <Space size={4} style={{ marginBottom: 4 }}>
                        <Typography.Text strong>
                          {JOINT_NAMES[index]}
                        </Typography.Text>
                        <Typography.Text type="secondary">
                          轴{joint}
                        </Typography.Text>
                      </Space>
                      <div>
                        <Typography.Text style={{ fontSize: 13 }}>
                          位姿{' '}
                          {numberText(
                            poseRad === undefined
                              ? undefined
                              : poseRad * RAD_TO_DEG,
                            1,
                            '°',
                          )}
                        </Typography.Text>
                      </div>
                      <div>
                        <Typography.Text
                          style={{ fontSize: 13 }}
                          type={danger ? 'danger' : undefined}
                        >
                          温度 {numberText(temp, 0, '°C')}
                        </Typography.Text>
                      </div>
                      <div style={{ fontSize: 12, color: '#999' }}>
                        反馈{' '}
                        {feedback ? (
                          <>
                            age {numberText(feedback.age_s, 2, 's')}
                            {feedback.fault_code
                              ? ` · 故障${feedback.fault_code}`
                              : ''}
                          </>
                        ) : (
                          '--'
                        )}
                      </div>
                    </div>
                  </Col>
                );
              })}
            </Row>
            <Space wrap style={{ marginTop: 12 }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                动作时长（调试位 / 失能）
              </Typography.Text>
              <InputNumber
                addonAfter="s"
                min={0.5}
                max={15}
                step={0.5}
                precision={1}
                value={armDuration}
                onChange={(value) => setArmDuration(Number(value ?? 3))}
                style={{ width: 100 }}
              />
            </Space>
          </Card>
        </Col>

        <Col xs={24} xl={9}>
          <Space direction="vertical" style={{ width: '100%' }} size={16}>
            <Card
              size="small"
              title="升降台"
              extra={
                <Button
                  type="text"
                  icon={<ReloadOutlined />}
                  onClick={() => void refresh(true)}
                />
              }
            >
              <Descriptions column={1} size="small">
                <Descriptions.Item label="连接">
                  {okTag(lift?.connected, '在线', '离线')}
                </Descriptions.Item>
                <Descriptions.Item label="当前位置">
                  <Typography.Text strong>
                    {numberText(lift?.height_mm, 1, ' mm')}
                  </Typography.Text>
                </Descriptions.Item>
                {lift?.error ? (
                  <Descriptions.Item label="错误">
                    {lift.error}
                  </Descriptions.Item>
                ) : null}
              </Descriptions>
              <Space wrap style={{ marginTop: 8 }}>
                <InputNumber
                  addonAfter="mm"
                  min={10}
                  max={1700}
                  precision={0}
                  value={liftTarget}
                  onChange={(value) => setLiftTarget(Number(value ?? 800))}
                  style={{ width: 150 }}
                />
                <Select
                  value={liftSpeed}
                  onChange={(value) => setLiftSpeed(value)}
                  options={[
                    { value: 1, label: '1 慢速' },
                    { value: 2, label: '2 中速' },
                    { value: 3, label: '3 快速' },
                  ]}
                  style={{ width: 100 }}
                />
                <Button
                  type="primary"
                  loading={action === 'lift'}
                  disabled={action !== undefined && action !== 'lift'}
                  onClick={confirmLiftMove}
                >
                  移动
                </Button>
              </Space>
            </Card>

            <Card
              size="small"
              title="推杆"
              extra={
                <Button
                  type="text"
                  icon={<ReloadOutlined />}
                  onClick={() => void refresh(true)}
                />
              }
            >
              <Descriptions column={1} size="small">
                <Descriptions.Item label="连接">
                  {okTag(rod?.connected, '在线', '离线')}
                </Descriptions.Item>
                <Descriptions.Item label="当前位置">
                  <Typography.Text strong>
                    {numberText(rod?.angle_deg, 1, '°')}
                  </Typography.Text>
                </Descriptions.Item>
                {rod?.error ? (
                  <Descriptions.Item label="错误">
                    {rod.error}
                  </Descriptions.Item>
                ) : null}
              </Descriptions>
              <Space wrap style={{ marginTop: 8 }}>
                <InputNumber
                  addonAfter="°"
                  min={-100}
                  max={150}
                  precision={1}
                  value={rodTarget}
                  onChange={(value) => setRodTarget(Number(value ?? 0))}
                  style={{ width: 140 }}
                />
                <Button
                  type="primary"
                  loading={action === 'rod'}
                  disabled={action !== undefined && action !== 'rod'}
                  onClick={() => confirmRodMove(rodTarget, '移动')}
                >
                  移动
                </Button>
                <Button
                  disabled={action !== undefined}
                  onClick={() => {
                    setRodTarget(0);
                    confirmRodMove(0, '归零');
                  }}
                >
                  归零
                </Button>
              </Space>
              <div style={{ marginTop: 12 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  高级参数（速度 / 加速度 / 容差 / 超时）
                </Typography.Text>
                <Space wrap style={{ marginTop: 4 }}>
                  <InputNumber
                    addonBefore="速度"
                    min={1}
                    max={10000}
                    precision={0}
                    value={rodSpeed}
                    onChange={(value) => setRodSpeed(Number(value ?? 1000))}
                    style={{ width: 130 }}
                  />
                  <InputNumber
                    addonBefore="加速度"
                    min={1}
                    max={1000}
                    precision={0}
                    value={rodAcceleration}
                    onChange={(value) =>
                      setRodAcceleration(Number(value ?? 50))
                    }
                    style={{ width: 130 }}
                  />
                  <InputNumber
                    addonBefore="容差"
                    addonAfter="°"
                    min={0.1}
                    max={10}
                    precision={1}
                    value={rodTolerance}
                    onChange={(value) => setRodTolerance(Number(value ?? 1.5))}
                    style={{ width: 130 }}
                  />
                  <InputNumber
                    addonBefore="超时"
                    addonAfter="s"
                    min={1}
                    max={60}
                    precision={0}
                    value={rodTimeout}
                    onChange={(value) => setRodTimeout(Number(value ?? 12))}
                    style={{ width: 130 }}
                  />
                </Space>
              </div>
            </Card>

            <Card
              size="small"
              title="TCP 笛卡尔点动"
              extra={
                <Button
                  type="text"
                  icon={<ReloadOutlined />}
                  onClick={() => void refresh(true)}
                />
              }
            >
              {health?.enabled !== true ? (
                <Alert
                  showIcon
                  type="warning"
                  title="机械臂未使能：请先在上方「机械臂」卡片使能，再进行点动。"
                  style={{ marginBottom: 8 }}
                />
              ) : null}
              <Space wrap align="center" style={{ marginBottom: 8 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  当前 TCP：
                </Typography.Text>
                <Typography.Text strong>
                  {tcp
                    ? `X ${tcp.x_mm.toFixed(1)} · Y ${tcp.y_mm.toFixed(1)} · Z ${tcp.z_mm.toFixed(1)} mm ｜ Rx ${tcp.rx_deg.toFixed(1)}° · Ry ${tcp.ry_deg.toFixed(1)}° · Rz ${tcp.rz_deg.toFixed(1)}°`
                    : '--'}
                </Typography.Text>
              </Space>
              <Space wrap size={[8, 8]} style={{ marginBottom: 8 }}>
                <Radio.Group
                  size="small"
                  optionType="button"
                  value={posStep}
                  onChange={(event) => setPosStep(Number(event.target.value))}
                  options={[1, 5, 10, 50].map((value) => ({
                    value,
                    label: `${value} mm`,
                  }))}
                />
                <Radio.Group
                  size="small"
                  optionType="button"
                  value={rotStep}
                  onChange={(event) => setRotStep(Number(event.target.value))}
                  options={[1, 5, 15].map((value) => ({
                    value,
                    label: `${value}°`,
                  }))}
                />
              </Space>
              <Space wrap size={[12, 8]}>
                {JOG_AXES.map(({ key, label }) => (
                  <Space key={key} size={2}>
                    <Typography.Text
                      style={{ display: 'inline-block', width: 22 }}
                    >
                      {label}
                    </Typography.Text>
                    <Button
                      size="small"
                      icon={<MinusOutlined />}
                      loading={action === `jog-${key}-`}
                      disabled={
                        health?.enabled !== true ||
                        (action !== undefined && action !== `jog-${key}-`)
                      }
                      onClick={() => void jog(key, -1)}
                    />
                    <Button
                      size="small"
                      icon={<PlusOutlined />}
                      loading={action === `jog-${key}+`}
                      disabled={
                        health?.enabled !== true ||
                        (action !== undefined && action !== `jog-${key}+`)
                      }
                      onClick={() => void jog(key, 1)}
                    />
                  </Space>
                ))}
              </Space>
              <div style={{ marginTop: 8 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  直线点动（MoveL）：单步钳制 100 mm /
                  30°，目标超出工作区（水平半径 ≤0.6 m、z -0.1~0.8 m）或 IK
                  失败时机械臂不动。
                </Typography.Text>
              </div>
            </Card>

            <Card
              size="small"
              title="D435 外参标定"
              extra={
                <Button
                  type="text"
                  icon={<ReloadOutlined />}
                  onClick={() => void refreshTransform()}
                />
              }
            >
              {transformError && !transform ? (
                <Alert
                  showIcon
                  type="error"
                  title={transformError}
                  style={{ marginBottom: 8 }}
                />
              ) : null}
              {!transform ? (
                transformError ? null : (
                  <Space>
                    <Spin />
                    <Typography.Text type="secondary">
                      正在读取外参…
                    </Typography.Text>
                  </Space>
                )
              ) : (
                <>
                  <Space wrap size={[8, 8]} style={{ marginBottom: 8 }}>
                    <Tag color={transform.override ? 'warning' : 'default'}>
                      {transform.override ? '用户覆盖' : '示例默认'}
                    </Tag>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      来源：{transform.source}
                    </Typography.Text>
                  </Space>
                  <Space wrap size={[8, 8]} style={{ marginBottom: 8 }}>
                    {POSE_FIELDS.map(
                      ({ key, label, addon, step, precision }) => (
                        <InputNumber
                          key={key}
                          addonBefore={label}
                          addonAfter={addon}
                          step={step}
                          precision={precision}
                          size="small"
                          value={transformDraft?.[key]}
                          onChange={(value) =>
                            setTransformDraft((previous) =>
                              previous
                                ? { ...previous, [key]: Number(value ?? 0) }
                                : previous,
                            )
                          }
                          style={{ width: 165 }}
                        />
                      ),
                    )}
                  </Space>
                  <Space wrap>
                    <Popconfirm
                      title="应用并保存外参？"
                      description="将热替换当前识别用的外参并写入 camera_to_base.json（用户覆盖文件，示例文件不受影响）。"
                      okText="应用并保存"
                      cancelText="再检查一下"
                      onConfirm={() => void applyTransform()}
                    >
                      <Button
                        type="primary"
                        size="small"
                        loading={transformSaving}
                        disabled={!transformDirty || transformResetting}
                      >
                        应用并保存
                      </Button>
                    </Popconfirm>
                    <Popconfirm
                      title="恢复默认外参？"
                      description="将删除 camera_to_base.json 用户覆盖文件并回退到 camera_to_base.example.json。"
                      okText="恢复默认"
                      cancelText="再检查一下"
                      onConfirm={() => void restoreDefaultTransform()}
                    >
                      <Button
                        size="small"
                        loading={transformResetting}
                        disabled={!transform.override || transformSaving}
                      >
                        恢复默认
                      </Button>
                    </Popconfirm>
                  </Space>
                  <Typography.Paragraph
                    type="secondary"
                    style={{ fontSize: 12, marginBottom: 0, marginTop: 8 }}
                  >
                    精调流程：①
                    在书架上贴一个明显标记点（或直接用某书脊角点）；② 用上方 TCP
                    点动把夹爪尖端移到该标记点，记录当前 TCP（基座坐标真值）；③
                    在视觉调试拍一张照，用识别框读出该点的相机坐标，经外参转换得到基座坐标测量值；④
                    对比两者，微调外参 6DOF 使测量值逼近真值（x/y/z
                    直接补偿差值，姿态误差表现为点云整体偏转时调 rx/ry/rz）；⑤
                    换 2-3 个不同位置重复验证，残差 &lt;5mm 即可。
                  </Typography.Paragraph>
                </>
              )}
            </Card>

            <Card size="small" title="调试提示">
              <Typography.Paragraph
                type="secondary"
                style={{ fontSize: 12, marginBottom: 0 }}
              >
                本页所有动作都通过 bookarm
                服务串行执行。若提示“服务正忙”，请等待正在执行的取还书/调参/其他硬件动作结束后再试。机械臂温度保护阈值默认
                90°C，超过会先归位再失能锁定。
              </Typography.Paragraph>
            </Card>
          </Space>
        </Col>
      </Row>
    </PageContainer>
  );
}
