import {
  ArrowDownOutlined,
  ArrowLeftOutlined,
  ArrowRightOutlined,
  ArrowUpOutlined,
  StopOutlined,
} from '@ant-design/icons';
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
  Spin,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  getRobotRealtimeState,
  getRobots,
  sendManualJog,
  subscribeRobotRealtime,
} from '@/services/libraryRobot';
import {
  applyChassisParams,
  type ChassisParam,
  type ChassisParamValues,
  dirtyChassisValues,
  getChassisParams,
  groupChassisParams,
  updateChassisParams,
} from '@/services/libraryRobot/chassisParams';
import type { RobotSummary } from '@/services/libraryRobot/types';
import { errorMessage } from '@/utils/errorText';
import { RobotStatusTag } from '../../shared';

function ParamEditor({
  param,
  value,
  onChange,
}: {
  param: ChassisParam;
  value: number | boolean | null;
  onChange: (value: number | boolean) => void;
}) {
  // 当前车端版本不支持的参数：禁用输入并展示原因。
  if (param.available === false) {
    return (
      <Tooltip title={param.unavailable_reason ?? '当前车端版本不支持'}>
        <div>
          <InputNumber
            style={{ width: '100%' }}
            disabled
            value={null}
            placeholder="--"
            suffix={param.unit || undefined}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {param.unavailable_reason ?? '当前车端版本不支持'}
          </Typography.Text>
        </div>
      </Tooltip>
    );
  }
  if (param.type === 'bool') {
    return (
      <Switch
        checked={value === true}
        checkedChildren="开"
        unCheckedChildren="关"
        onChange={(checked) => onChange(checked)}
      />
    );
  }
  return (
    <InputNumber
      style={{ width: '100%' }}
      min={param.min}
      max={param.max}
      step={param.step}
      value={typeof value === 'number' ? value : null}
      suffix={param.unit || undefined}
      onChange={(next) => {
        if (typeof next === 'number') onChange(next);
      }}
    />
  );
}

// ---- 手动控制（低速点动）：不依赖地图与定位，命令经中心 API 由本机 Gateway 限速校验后下发 ----

const JOG_DURATION_MS = 750;
const STOP_DURATION_MS = 300;
// 按住期间以 10Hz 续发：网关 20Hz 持续发布速度并以 durationMs 作为看门狗租约。
const MOTION_REFRESH_MS = 100;
/** 服务端安全上限（store.send_manual_jog）：|linear| ≤ 0.3 m/s、|angular| ≤ 0.8 rad/s。 */
const LINEAR_LIMIT = 0.3;
const ANGULAR_LIMIT = 0.8;

type DriveInputSource = 'none' | 'button' | 'keyboard';
type JogDirection = 'forward' | 'backward' | 'left' | 'right';

const DRIVE_SOURCE_TEXT: Record<DriveInputSource, string> = {
  none: '无',
  button: '按钮',
  keyboard: '键盘',
};

function isEditableTarget(target: EventTarget | null) {
  const element = target as HTMLElement | null;
  return Boolean(
    element?.isContentEditable ||
      ['INPUT', 'TEXTAREA', 'SELECT'].includes(element?.tagName ?? ''),
  );
}

function ManualDrivePanel() {
  const { message } = App.useApp();
  const [robots, setRobots] = useState<RobotSummary[]>([]);
  const [driveRobotId, setDriveRobotId] = useState<string>();
  const [driveEnabled, setDriveEnabled] = useState(false);
  const [linearSpeed, setLinearSpeed] = useState(0.1);
  const [angularSpeed, setAngularSpeed] = useState(0.2);
  const [inputSource, setInputSource] = useState<DriveInputSource>('none');

  const driveRobotIdRef = useRef<string | undefined>(undefined);
  const linearSpeedRef = useRef(linearSpeed);
  const angularSpeedRef = useRef(angularSpeed);
  const heldKeysRef = useRef(new Set<string>());
  const buttonDirectionRef = useRef<JogDirection | undefined>(undefined);
  const motionRef = useRef<{
    source: DriveInputSource;
    linear: number;
    angular: number;
  }>({ source: 'none', linear: 0, angular: 0 });
  const lastJogSentAtRef = useRef(0);
  const lastJogErrorAtRef = useRef(0);

  const activeRobot = robots.find((robot) => robot.id === driveRobotId);

  // 机器人列表：首屏拉取 + 实时通道更新。
  useEffect(() => {
    void getRobots()
      .then((list) => {
        setRobots(list);
        if (!driveRobotIdRef.current && list[0]) {
          driveRobotIdRef.current = list[0].id;
          setDriveRobotId(list[0].id);
        }
      })
      .catch((error) =>
        message.error(errorMessage(error, '读取机器人状态失败。')),
      );
    const unsubscribe = subscribeRobotRealtime(() => {
      const realtimeRobots = getRobotRealtimeState().robots;
      if (realtimeRobots.length) setRobots(realtimeRobots);
    });
    return () => unsubscribe();
  }, [message]);

  const setMotion = useCallback(
    (motion: { source: DriveInputSource; linear: number; angular: number }) => {
      const previous = motionRef.current;
      motionRef.current = motion;
      if (previous.source !== motion.source) setInputSource(motion.source);
    },
    [],
  );

  const sendCurrentMotion = useCallback(
    (force = false) => {
      const robotId = driveRobotIdRef.current;
      const motion = motionRef.current;
      if (!robotId) return;
      const timestamp = Date.now();
      if (
        !force &&
        timestamp - lastJogSentAtRef.current < MOTION_REFRESH_MS - 25
      )
        return;
      lastJogSentAtRef.current = timestamp;
      void sendManualJog(robotId, {
        linear: motion.linear,
        angular: motion.angular,
        durationMs:
          motion.source === 'none' ? STOP_DURATION_MS : JOG_DURATION_MS,
      }).catch((error) => {
        if (timestamp - lastJogErrorAtRef.current >= 5_000) {
          lastJogErrorAtRef.current = timestamp;
          message.error(
            errorMessage(
              error,
              '手动控制命令未被中心 API 接受，Gateway 将在超时后停车。',
            ),
          );
        }
      });
    },
    [message],
  );

  const jogValues = useCallback((direction: JogDirection) => {
    switch (direction) {
      case 'forward':
        return { linear: linearSpeedRef.current, angular: 0 };
      case 'backward':
        return { linear: -linearSpeedRef.current, angular: 0 };
      case 'left':
        return { linear: 0, angular: angularSpeedRef.current };
      case 'right':
        return { linear: 0, angular: -angularSpeedRef.current };
    }
  }, []);

  const recomputeMotion = useCallback(() => {
    const heldKeys = heldKeysRef.current;
    const forward = heldKeys.has('w') || heldKeys.has('arrowup');
    const backward = heldKeys.has('s') || heldKeys.has('arrowdown');
    const left = heldKeys.has('a') || heldKeys.has('arrowleft');
    const right = heldKeys.has('d') || heldKeys.has('arrowright');
    if (forward || backward || left || right) {
      setMotion({
        source: 'keyboard',
        linear:
          (forward ? linearSpeedRef.current : 0) +
          (backward ? -linearSpeedRef.current : 0),
        angular:
          (left ? angularSpeedRef.current : 0) +
          (right ? -angularSpeedRef.current : 0),
      });
      return;
    }
    const buttonDirection = buttonDirectionRef.current;
    if (buttonDirection) {
      setMotion({ source: 'button', ...jogValues(buttonDirection) });
      return;
    }
    setMotion({ source: 'none', linear: 0, angular: 0 });
  }, [jogValues, setMotion]);

  const stopMotion = useCallback(() => {
    heldKeysRef.current.clear();
    buttonDirectionRef.current = undefined;
    setMotion({ source: 'none', linear: 0, angular: 0 });
    sendCurrentMotion(true);
  }, [sendCurrentMotion, setMotion]);

  // 按住期间按 100ms 周期续发，车端 1 秒看门狗兜底停车。
  useEffect(() => {
    if (!driveEnabled || !driveRobotId) return;
    const timer = window.setInterval(() => {
      if (motionRef.current.source !== 'none') sendCurrentMotion();
    }, MOTION_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [driveEnabled, driveRobotId, sendCurrentMotion]);

  // 键盘 W/A/S/D 与方向键点动，空格立即停车。
  useEffect(() => {
    if (!driveEnabled || !driveRobotId) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (isEditableTarget(event.target)) return;
      const key = event.key.toLowerCase();
      if (key === ' ') {
        event.preventDefault();
        stopMotion();
        return;
      }
      if (
        ![
          'w',
          'a',
          's',
          'd',
          'arrowup',
          'arrowdown',
          'arrowleft',
          'arrowright',
        ].includes(key)
      )
        return;
      event.preventDefault();
      if (heldKeysRef.current.has(key)) return;
      heldKeysRef.current.add(key);
      recomputeMotion();
      sendCurrentMotion(true);
    };
    const onKeyUp = (event: KeyboardEvent) => {
      const key = event.key.toLowerCase();
      if (!heldKeysRef.current.delete(key)) return;
      event.preventDefault();
      recomputeMotion();
      sendCurrentMotion(true);
    };
    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
    };
  }, [
    driveEnabled,
    driveRobotId,
    recomputeMotion,
    sendCurrentMotion,
    stopMotion,
  ]);

  // 失焦、隐藏、离开页面或卸载时只补零速帧；车端 1 秒看门狗兜底停车。
  useEffect(() => {
    if (!driveEnabled) return;
    const leaveControlPage = () => stopMotion();
    const onVisibilityChange = () => {
      if (document.hidden) leaveControlPage();
    };
    window.addEventListener('blur', leaveControlPage);
    window.addEventListener('pagehide', leaveControlPage);
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      window.removeEventListener('blur', leaveControlPage);
      window.removeEventListener('pagehide', leaveControlPage);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      leaveControlPage();
    };
  }, [driveEnabled, stopMotion]);

  function startButtonMotion(direction: JogDirection) {
    buttonDirectionRef.current = direction;
    recomputeMotion();
    sendCurrentMotion(true);
  }

  function stopButtonMotion() {
    if (!buttonDirectionRef.current) return;
    buttonDirectionRef.current = undefined;
    recomputeMotion();
    sendCurrentMotion(true);
  }

  function disableDrive() {
    stopMotion();
    setDriveEnabled(false);
  }

  const holdButtonProps = (direction: JogDirection) => ({
    // 触屏按住必须禁用浏览器手势接管，否则系统抢手势触发 pointercancel，
    // 速度帧后紧跟零速帧，表现为"一顿一顿"。
    style: { touchAction: 'none' as const, userSelect: 'none' as const },
    onContextMenu: (event: React.MouseEvent) => event.preventDefault(),
    onPointerDown: (event: React.PointerEvent<HTMLElement>) => {
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      startButtonMotion(direction);
    },
    onPointerUp: stopButtonMotion,
    onPointerLeave: stopButtonMotion,
    onPointerCancel: stopButtonMotion,
  });

  return (
    <Card
      title="手动控制（低速遥控）"
      style={{ marginBottom: 16 }}
      extra={driveEnabled ? <Tag color="warning">手动控制中</Tag> : null}
    >
      <Alert
        showIcon
        type="warning"
        title="按住持续移动、松开即停；无需地图与定位"
        description={`按住期间车端以 20Hz 持续输出速度（带加速度斜坡防打滑），命令经中心 API 由本机 Gateway 限速校验（≤${LINEAR_LIMIT} m/s、≤${ANGULAR_LIMIT} rad/s）后下发；松开、页面失焦或命令超时都会停车。危险情况请优先使用硬件急停。`}
        style={{ marginBottom: 12 }}
      />
      <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
        <Space wrap size="middle" align="center">
          <span>
            <Typography.Text type="secondary">机器人：</Typography.Text>
            <Select
              style={{ minWidth: 200 }}
              placeholder="选择机器人"
              value={driveRobotId}
              onChange={(value: string) => {
                stopMotion();
                driveRobotIdRef.current = value;
                setDriveRobotId(value);
              }}
              options={robots.map((robot) => ({
                value: robot.id,
                label: `${robot.name}（${robot.id}）`,
              }))}
              disabled={driveEnabled}
            />
          </span>
          {activeRobot ? <RobotStatusTag status={activeRobot.status} /> : null}
          {activeRobot && !activeRobot.health.chassis ? (
            <Tag color="red">底盘健康检查未通过</Tag>
          ) : null}
          <span>
            <Typography.Text type="secondary">线速度：</Typography.Text>
            <InputNumber
              style={{ width: 100 }}
              min={0.02}
              max={LINEAR_LIMIT}
              step={0.02}
              value={linearSpeed}
              onChange={(value) => {
                if (typeof value === 'number') {
                  linearSpeedRef.current = value;
                  setLinearSpeed(value);
                }
              }}
              disabled={driveEnabled}
            />
            <Typography.Text type="secondary"> m/s</Typography.Text>
          </span>
          <span>
            <Typography.Text type="secondary">角速度：</Typography.Text>
            <InputNumber
              style={{ width: 100 }}
              min={0.05}
              max={ANGULAR_LIMIT}
              step={0.05}
              value={angularSpeed}
              onChange={(value) => {
                if (typeof value === 'number') {
                  angularSpeedRef.current = value;
                  setAngularSpeed(value);
                }
              }}
              disabled={driveEnabled}
            />
            <Typography.Text type="secondary"> rad/s</Typography.Text>
          </span>
          {driveEnabled ? (
            <Typography.Text type="secondary">
              输入来源：{DRIVE_SOURCE_TEXT[inputSource]}
            </Typography.Text>
          ) : null}
        </Space>
        {driveEnabled ? (
          <Space wrap size="middle">
            <Button
              size="large"
              icon={<ArrowUpOutlined />}
              {...holdButtonProps('forward')}
            >
              按住前进
            </Button>
            <Button
              size="large"
              icon={<ArrowDownOutlined />}
              {...holdButtonProps('backward')}
            >
              按住后退
            </Button>
            <Button
              size="large"
              icon={<ArrowLeftOutlined />}
              {...holdButtonProps('left')}
            >
              按住左转
            </Button>
            <Button
              size="large"
              icon={<ArrowRightOutlined />}
              {...holdButtonProps('right')}
            >
              按住右转
            </Button>
            <Button size="large" icon={<StopOutlined />} onClick={stopMotion}>
              立即停止
            </Button>
            <Button danger onClick={disableDrive}>
              结束控制
            </Button>
            <Typography.Text type="secondary">
              键盘：W/↑ 前进、S/↓ 后退、A/← 左转、D/→ 右转、空格立即停止
            </Typography.Text>
          </Space>
        ) : (
          <Popconfirm
            title="启用低速手动控制？"
            description="确认车辆周边已清场、实体急停可触达？启用后可按住按钮或用键盘 W/A/S/D 持续遥控底盘。"
            okText="确认启用"
            cancelText="取消"
            onConfirm={() => setDriveEnabled(true)}
          >
            <Button type="primary" disabled={!driveRobotId}>
              启用手动控制
            </Button>
          </Popconfirm>
        )}
      </Space>
    </Card>
  );
}

export default function ChassisDebugPage() {
  const { message } = App.useApp();
  const [params, setParams] = useState<ChassisParam[] | null>(null);
  const [draft, setDraft] = useState<ChassisParamValues>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [applying, setApplying] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await getChassisParams();
      setParams(response.params);
      setDraft({});
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '加载底盘参数失败',
      );
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirtyValues = useMemo(
    () => dirtyChassisValues(params ?? [], draft),
    [params, draft],
  );
  const dirtyCount = Object.keys(dirtyValues).length;

  // Warn before leaving the page with unsaved edits.
  useEffect(() => {
    if (dirtyCount === 0) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirtyCount]);

  const groups = useMemo(() => groupChassisParams(params ?? []), [params]);

  const currentValue = (param: ChassisParam) => draft[param.key] ?? param.value;

  const handleChange = (key: string, value: number | boolean) => {
    setDraft((previous) => ({ ...previous, [key]: value }));
  };

  const save = async (): Promise<boolean> => {
    if (dirtyCount === 0) return true;
    setSaving(true);
    try {
      const response = await updateChassisParams(dirtyValues);
      setParams(response.params);
      setDraft({});
      return true;
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '保存底盘参数失败',
      );
      return false;
    } finally {
      setSaving(false);
    }
  };

  const handleSave = async () => {
    if (await save()) {
      message.success('已保存，重启导航运行时后生效');
    }
  };

  const handleSaveAndApply = async () => {
    if (!(await save())) return;
    setApplying(true);
    try {
      await applyChassisParams();
      message.success('导航运行时已重启，参数已生效');
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '重启导航运行时失败',
      );
    } finally {
      setApplying(false);
    }
  };

  return (
    <PageContainer
      title="底盘调试"
      content="读取并修改 nav2、底盘轮系与雷达 TF 调试参数；保存写入配置文件，重启导航运行时后生效。顶部提供低速手动遥控（按住持续移动），无需地图与定位，用于方向校验与底盘自检。"
      extra={
        <Space>
          <Button
            onClick={() => void load()}
            disabled={loading || saving || applying}
          >
            重新加载
          </Button>
          <Button
            type={dirtyCount > 0 ? 'primary' : 'default'}
            loading={saving}
            disabled={dirtyCount === 0 || applying}
            onClick={() => void handleSave()}
          >
            保存修改{dirtyCount > 0 ? `（${dirtyCount}）` : ''}
          </Button>
          <Popconfirm
            title="将重启导航栈（约30秒），期间机器人不可导航，确认？"
            okText="确认重启"
            cancelText="取消"
            onConfirm={() => void handleSaveAndApply()}
          >
            <Button type="primary" danger loading={applying} disabled={loading}>
              保存并重启生效
            </Button>
          </Popconfirm>
        </Space>
      }
    >
      <ManualDrivePanel />
      {loading ? (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin size="large" />
        </div>
      ) : params === null ? (
        <Alert
          type="error"
          showIcon
          title="加载底盘参数失败"
          description="请检查控制站服务与配置文件后点击“重新加载”。"
        />
      ) : (
        <Space orientation="vertical" size={16} style={{ width: '100%' }}>
          {dirtyCount > 0 && (
            <Alert
              type="warning"
              showIcon
              title={`有 ${dirtyCount} 项未保存的修改`}
            />
          )}
          {groups.map((group) => (
            <Card key={group.group} title={group.group} size="small">
              <Row gutter={[24, 16]}>
                {group.params.map((param) => {
                  const dirty =
                    draft[param.key] !== undefined &&
                    draft[param.key] !== param.value;
                  return (
                    <Col xs={24} md={12} xl={8} key={param.key}>
                      <div style={{ marginBottom: 4 }}>
                        <Typography.Text strong={dirty}>
                          {param.label}
                        </Typography.Text>
                        <Typography.Text
                          type="secondary"
                          style={{ marginLeft: 8, fontSize: 12 }}
                        >
                          {param.key}
                        </Typography.Text>
                        {dirty && (
                          <Typography.Text
                            type="warning"
                            style={{ marginLeft: 8 }}
                          >
                            未保存
                          </Typography.Text>
                        )}
                      </div>
                      <ParamEditor
                        param={param}
                        value={currentValue(param)}
                        onChange={(value) => handleChange(param.key, value)}
                      />
                    </Col>
                  );
                })}
              </Row>
            </Card>
          ))}
        </Space>
      )}
    </PageContainer>
  );
}
