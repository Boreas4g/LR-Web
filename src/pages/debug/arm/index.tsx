import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  InputNumber,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';

import {
  type ArmJointsView,
  calibrateArmZero,
  cartesianJog,
  clearArmFaults,
  disableArm,
  enableArm,
  getArmJoints,
  getHardwareStatus,
  type HardwareStatus,
  moveArmJoints,
  moveArmToZero,
  resetArmToDebugPose,
} from '@/services/libraryRobot/studio';

const JOINT_NAMES = [
  '关节 1',
  '关节 2',
  '关节 3',
  '关节 4',
  '关节 5',
  '关节 6',
];

function fmt(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

export default function ArmDebugPage() {
  const { message } = App.useApp();
  const [joints, setJoints] = useState<ArmJointsView | null>(null);
  const [hardware, setHardware] = useState<HardwareStatus | null>(null);
  const [targets, setTargets] = useState<(number | null)[]>(
    Array(6).fill(null),
  );
  const [durationS, setDurationS] = useState<number>(3.0);
  const [jogDelta, setJogDelta] = useState<number>(10);
  const [zeroMotor, setZeroMotor] = useState<number>(0);
  const [busy, setBusy] = useState<string | null>(null);
  const busyRef = useRef(false);

  const refreshJoints = useCallback(async () => {
    try {
      setJoints(await getArmJoints());
    } catch {
      // 服务未启动时由硬件状态卡片呈现原因，不重复报错
    }
  }, []);

  const refreshHardware = useCallback(async () => {
    try {
      setHardware(await getHardwareStatus());
    } catch {
      setHardware(null);
    }
  }, []);

  useEffect(() => {
    void refreshJoints();
    void refreshHardware();
    const jointsTimer = setInterval(() => void refreshJoints(), 2000);
    const hardwareTimer = setInterval(() => void refreshHardware(), 3000);
    return () => {
      clearInterval(jointsTimer);
      clearInterval(hardwareTimer);
    };
  }, [refreshJoints, refreshHardware]);

  const run = useCallback(
    async (action: string, fn: () => Promise<unknown>, okText: string) => {
      if (busyRef.current) return;
      busyRef.current = true;
      setBusy(action);
      try {
        await fn();
        message.success(okText);
        await Promise.all([refreshJoints(), refreshHardware()]);
      } catch (error) {
        message.error(error instanceof Error ? error.message : `${action}失败`);
      } finally {
        busyRef.current = false;
        setBusy(null);
      }
    },
    [message, refreshJoints, refreshHardware],
  );

  const fillTargetsFromCurrent = () => {
    if (joints)
      setTargets(joints.joints_deg.map((value) => Number(value.toFixed(1))));
  };

  const sendTargets = () => {
    if (targets.some((value) => typeof value !== 'number')) {
      message.warning('请填齐 6 个关节目标角度（可先用"填入当前角度"）');
      return;
    }
    return run(
      '关节运动',
      () => moveArmJoints(targets as number[], durationS),
      '关节目标已执行',
    );
  };

  const enabled = joints?.enabled === true;
  const armHealth = hardware?.arm.health;
  const thermal = hardware?.arm.thermal;
  const lockout = (thermal as { lockout?: unknown } | undefined)?.lockout;
  const temperatures =
    (thermal as { temperatures_c?: Record<string, number> } | undefined)
      ?.temperatures_c ?? {};

  return (
    <PageContainer title="机械臂调试">
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        message="运动前请确认机械臂运动范围内无人、实体急停可用；所有动作都会立即驱动真实机械臂。"
      />
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={10}>
          <Card
            title="机械臂状态"
            extra={
              enabled ? <Tag color="green">已使能</Tag> : <Tag>未使能</Tag>
            }
          >
            <Space direction="vertical" style={{ width: '100%' }} size="middle">
              <Row gutter={16}>
                <Col span={12}>
                  <Statistic
                    title="CAN 帧率"
                    value={armHealth?.can_fps ?? 0}
                    precision={1}
                    suffix="fps"
                  />
                </Col>
                <Col span={12}>
                  <Statistic
                    title="反馈状态"
                    value={
                      armHealth == null
                        ? '未知'
                        : armHealth.healthy_feedback
                          ? '正常'
                          : '异常'
                    }
                    valueStyle={{
                      color: armHealth?.healthy_feedback
                        ? '#3f8600'
                        : '#cf1322',
                    }}
                  />
                </Col>
              </Row>
              {armHealth?.reason ? (
                <Typography.Text type="danger">
                  {armHealth.reason}
                </Typography.Text>
              ) : null}
              {lockout ? (
                <Alert
                  type="error"
                  showIcon
                  message="温度保护已锁存，需处理后清除才能再次使能"
                />
              ) : null}
              <Table
                size="small"
                rowKey="name"
                pagination={false}
                dataSource={JOINT_NAMES.map((name, index) => ({
                  name,
                  deg: joints?.joints_deg?.[index],
                  temp:
                    temperatures[`j${index + 1}`] ??
                    temperatures[String(index + 1)],
                  fault:
                    armHealth?.joint_feedback?.[String(index + 1)]?.fault_code,
                }))}
                columns={[
                  { title: '关节', dataIndex: 'name' },
                  {
                    title: '当前角度 (°)',
                    dataIndex: 'deg',
                    render: (value: number) => fmt(value),
                  },
                  {
                    title: '温度 (°C)',
                    dataIndex: 'temp',
                    render: (value: number) => fmt(value, 0),
                  },
                  {
                    title: '故障码',
                    dataIndex: 'fault',
                    render: (value: number | null | undefined) =>
                      value === null || value === undefined ? (
                        '—'
                      ) : value === 0 ? (
                        '0'
                      ) : (
                        <Typography.Text type="danger">{value}</Typography.Text>
                      ),
                  },
                ]}
              />
              <Typography.Text type="secondary">
                TCP：
                {joints?.tcp
                  ? `x ${fmt((joints.tcp as { x_mm?: number }).x_mm, 0)} mm, y ${fmt(
                      (joints.tcp as { y_mm?: number }).y_mm,
                      0,
                    )} mm, z ${fmt((joints.tcp as { z_mm?: number }).z_mm, 0)} mm`
                  : '不可用'}
              </Typography.Text>
            </Space>
          </Card>

          <Card title="硬件总览" style={{ marginTop: 16 }}>
            <Space wrap size="middle">
              <span>
                CAN：
                {hardware?.can.up ? (
                  <Tag color="green">在线</Tag>
                ) : (
                  <Tag color="red">离线</Tag>
                )}
              </span>
              <span>
                杆电机：
                {hardware?.rod.connected ? (
                  <Tag color="green">已连接</Tag>
                ) : (
                  <Tag color="red">未连接</Tag>
                )}
              </span>
              <span>
                升降台：
                {hardware?.lift.connected ? (
                  <Tag color="green">已连接</Tag>
                ) : (
                  <Tag color="orange">未连接</Tag>
                )}
              </span>
              <span>
                相机：
                {hardware?.camera.present ? (
                  <Tag color="green">已连接</Tag>
                ) : (
                  <Tag color="red">未连接</Tag>
                )}
              </span>
            </Space>
            {hardware?.lift.error ? (
              <Typography.Paragraph
                type="secondary"
                style={{ marginTop: 8, marginBottom: 0 }}
              >
                {hardware.lift.error}
              </Typography.Paragraph>
            ) : null}
          </Card>

          <Card
            title={
              <span>
                零位校准（写入编码器）
                <Tag color="red" style={{ marginLeft: 8 }}>
                  装机专用
                </Tag>
              </span>
            }
            style={{ marginTop: 16 }}
          >
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 12 }}
              message="该操作把机械臂当前姿态永久写为编码器零点，软件无法撤销。仅在新机械臂装机时使用：先安全失能，手动把各关节摆到机械零位刻线，再执行。"
            />
            <Space wrap align="center">
              <Select
                style={{ width: 150 }}
                value={zeroMotor}
                onChange={setZeroMotor}
                options={[
                  { value: 0, label: '全部六关节' },
                  ...JOINT_NAMES.map((name, index) => ({
                    value: index + 1,
                    label: name,
                  })),
                ]}
              />
              <Popconfirm
                title="确认写入零位？"
                description={`将把「${
                  zeroMotor === 0 ? '全部六关节' : JOINT_NAMES[zeroMotor - 1]
                }」的当前位置写为编码器零点。确认机械臂已失能且已摆到机械零位？`}
                okText="确认写入"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                onConfirm={() =>
                  run(
                    '零位校准',
                    () => calibrateArmZero(zeroMotor),
                    '零位已写入编码器',
                  )
                }
              >
                <Button danger loading={busy === '零位校准'} disabled={enabled}>
                  写入零位
                </Button>
              </Popconfirm>
              {enabled ? (
                <Typography.Text type="danger">
                  机械臂仍在使能状态，请先安全失能
                </Typography.Text>
              ) : null}
            </Space>
          </Card>
        </Col>

        <Col xs={24} xl={14}>
          <Card title="使能控制">
            <Space wrap>
              <Button
                type="primary"
                loading={busy === '使能'}
                onClick={() => run('使能', enableArm, '机械臂已使能')}
              >
                使能机械臂
              </Button>
              <Popconfirm
                title="安全失能"
                description="机械臂将先回到折收位再断使能，确认执行？"
                onConfirm={() =>
                  run('失能', () => disableArm(), '机械臂已安全失能')
                }
              >
                <Button danger loading={busy === '失能'}>
                  安全失能
                </Button>
              </Popconfirm>
              <Popconfirm
                title="回到调试位"
                description="机械臂将运动到取书调试位 [0,10,-65,50,0,0]°，确认执行？"
                onConfirm={() =>
                  run('调试位', () => resetArmToDebugPose(), '已到达调试位')
                }
              >
                <Button loading={busy === '调试位'}>回到调试位</Button>
              </Popconfirm>
              <Popconfirm
                title="故障恢复"
                description="将向全部 7 个电机发送故障码清除帧（电机会先失能）并重新使能，用于夹取过流等锁存故障的恢复，确认执行？"
                onConfirm={() =>
                  run(
                    '故障恢复',
                    async () => {
                      await clearArmFaults();
                      return enableArm();
                    },
                    '故障码已清除并重新使能',
                  )
                }
              >
                <Button loading={busy === '故障恢复'}>故障恢复</Button>
              </Popconfirm>
              <Popconfirm
                title="回零位（零位校准）"
                description="机械臂六个关节将运动到 0° 零位，确认执行？"
                onConfirm={() =>
                  run('回零位', () => moveArmToZero(), '已到达零位')
                }
              >
                <Button loading={busy === '回零位'}>回零位</Button>
              </Popconfirm>
            </Space>
          </Card>

          <Card title="关节目标（度）" style={{ marginTop: 16 }}>
            <Row gutter={[8, 8]}>
              {JOINT_NAMES.map((name, index) => (
                <Col span={8} key={name}>
                  <InputNumber
                    style={{ width: '100%' }}
                    addonBefore={name.replace('关节', 'J')}
                    min={-360}
                    max={360}
                    step={1}
                    value={targets[index]}
                    placeholder="目标 °"
                    onChange={(value) =>
                      setTargets((prev) => {
                        const next = [...prev];
                        next[index] = typeof value === 'number' ? value : null;
                        return next;
                      })
                    }
                  />
                </Col>
              ))}
            </Row>
            <Space style={{ marginTop: 12 }} wrap>
              <span>
                时长：
                <InputNumber
                  min={0.5}
                  max={15}
                  step={0.5}
                  value={durationS}
                  onChange={(value) =>
                    typeof value === 'number' && setDurationS(value)
                  }
                  style={{ width: 90 }}
                />{' '}
                s
              </span>
              <Button onClick={fillTargetsFromCurrent}>填入当前角度</Button>
              <Popconfirm
                title="发送关节目标"
                description="机械臂将立即按上表角度运动，确认执行？"
                onConfirm={sendTargets}
              >
                <Button type="primary" loading={busy === '关节运动'}>
                  发送关节目标
                </Button>
              </Popconfirm>
            </Space>
          </Card>

          <Card title="TCP 点动（相对当前位置）" style={{ marginTop: 16 }}>
            <Space wrap align="center">
              <span>
                步长：
                <InputNumber
                  min={1}
                  max={100}
                  value={jogDelta}
                  onChange={(value) =>
                    typeof value === 'number' && setJogDelta(value)
                  }
                  style={{ width: 80 }}
                />
              </span>
              {(['x', 'y', 'z'] as const).map((axis) => (
                <Space key={axis}>
                  <Button
                    disabled={busy !== null}
                    onClick={() =>
                      run(
                        'TCP 点动',
                        () => cartesianJog({ axis, delta: jogDelta }),
                        `${axis.toUpperCase()} +${jogDelta}`,
                      )
                    }
                  >
                    {axis.toUpperCase()}+
                  </Button>
                  <Button
                    disabled={busy !== null}
                    onClick={() =>
                      run(
                        'TCP 点动',
                        () => cartesianJog({ axis, delta: -jogDelta }),
                        `${axis.toUpperCase()} -${jogDelta}`,
                      )
                    }
                  >
                    {axis.toUpperCase()}-
                  </Button>
                </Space>
              ))}
              <Typography.Text type="secondary">
                平动单位 mm（服务端限幅 ±100 mm/步）
              </Typography.Text>
            </Space>
          </Card>
        </Col>
      </Row>
    </PageContainer>
  );
}
