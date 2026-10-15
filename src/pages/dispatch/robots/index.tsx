import {
  ArrowDownOutlined,
  ArrowLeftOutlined,
  ArrowRightOutlined,
  ArrowUpOutlined,
  ControlOutlined,
  PoweroffOutlined,
  RobotOutlined,
  StopOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import { useModel } from '@umijs/max';
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Row,
  Space,
  Tag,
} from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';

import {
  getMapPreview,
  getMapVersions,
  getRobotRealtimeState,
  getRobots,
  resetSoftwareEmergencyStop,
  sendManualJog,
  submitRobotCommand,
  subscribeRobotRealtime,
} from '@/services/libraryRobot';
import {
  IMU_DISCONNECTED_ALERT,
  imuHealthStatus,
} from '@/services/libraryRobot/health';
import type { MapVersion, RobotSummary } from '@/services/libraryRobot/types';
import { errorMessage } from '@/utils/errorText';
import { RobotStatusTag } from '../../shared';

const JOG_DURATION_MS = 750;
const STOP_DURATION_MS = 100;
// 续发间隔 250 ms、租约 750 ms：即便个别帧经 MQTT 迟到，车端租约仍有
// 约 500 ms 余量，不会在按住期间掉速。
const MOTION_REFRESH_MS = 250;
const MANUAL_LINEAR_SPEED = 0.1;
const MANUAL_ANGULAR_SPEED = 0.2;
const GAMEPAD_DEADZONE = 0.18;

type JogDirection = 'forward' | 'backward' | 'left' | 'right';
type InputSource = 'none' | 'button' | 'keyboard' | 'gamepad';
type Motion = { source: InputSource; linear: number; angular: number };

const jogValues: Record<JogDirection, { linear: number; angular: number }> = {
  forward: { linear: MANUAL_LINEAR_SPEED, angular: 0 },
  backward: { linear: -MANUAL_LINEAR_SPEED, angular: 0 },
  left: { linear: 0, angular: MANUAL_ANGULAR_SPEED },
  right: { linear: 0, angular: -MANUAL_ANGULAR_SPEED },
};

const inputSourceLabel: Record<InputSource, string> = {
  none: '等待输入',
  button: '页面按钮',
  keyboard: '键盘',
  gamepad: '手柄',
};

function isEditableTarget(target: EventTarget | null) {
  const element = target as HTMLElement | null;
  return Boolean(
    element?.isContentEditable ||
      ['INPUT', 'TEXTAREA', 'SELECT'].includes(element?.tagName ?? ''),
  );
}

function applyDeadzone(value: number) {
  if (Math.abs(value) < GAMEPAD_DEADZONE) return 0;
  return (
    (Math.sign(value) * (Math.abs(value) - GAMEPAD_DEADZONE)) /
    (1 - GAMEPAD_DEADZONE)
  );
}

const scanModeLabel: Record<
  NonNullable<RobotSummary['localizationScanMode']>,
  string
> = {
  dual: '双雷达',
  single_laser_1: '前雷达降级',
  single_laser_2: '后雷达降级',
  unavailable: '无可用雷达',
  unknown: '雷达状态未知',
};

function ManualPositionMap({
  map,
  previewUrl,
  robot,
}: {
  map?: MapVersion;
  previewUrl?: string;
  robot: RobotSummary;
}) {
  const pose = robot.pose;
  const poseUsable = Boolean(pose && robot.localizationStatus === 'localized');
  const capturedAt = pose ? new Date(pose.capturedAt) : undefined;
  const capturedLabel =
    capturedAt && !Number.isNaN(capturedAt.getTime())
      ? capturedAt.toLocaleTimeString('zh-CN', { hour12: false })
      : '暂无可信位置';
  const pixelX =
    pose && map ? (pose.x - map.origin.x) / map.resolution : undefined;
  const pixelY =
    pose && map
      ? map.height - 1 - (pose.y - map.origin.y) / map.resolution
      : undefined;
  const poseIsOnMap = Boolean(
    pixelX !== undefined &&
      pixelY !== undefined &&
      pixelX >= 0 &&
      pixelX < (map?.width ?? 0) &&
      pixelY >= 0 &&
      pixelY < (map?.height ?? 0),
  );
  const yawDegrees = pose ? (pose.yaw * 180) / Math.PI : 0;

  return (
    <Card
      title="实时位置"
      extra={
        <Tag color={poseUsable ? 'success' : 'default'}>
          {poseUsable ? '实时定位' : '位置不可用或未定位'}
        </Tag>
      }
      size="small"
    >
      <Descriptions
        column={{ xs: 1, sm: 2 }}
        size="small"
        style={{ marginBottom: 12 }}
      >
        <Descriptions.Item label="定位状态">
          {robot.localizationStatus ?? 'unknown'}
        </Descriptions.Item>
        <Descriptions.Item label="定位雷达">
          {scanModeLabel[robot.localizationScanMode ?? 'unknown']}
        </Descriptions.Item>
        <Descriptions.Item label="坐标" span={2}>
          {pose
            ? `(${pose.x.toFixed(3)}, ${pose.y.toFixed(3)}) m，${yawDegrees.toFixed(0)}°`
            : '暂无可信坐标'}
        </Descriptions.Item>
        <Descriptions.Item label="采集时间" span={2}>
          {capturedLabel}
        </Descriptions.Item>
      </Descriptions>
      {map && previewUrl ? (
        <div
          style={{
            background: '#f5f5f5',
            lineHeight: 0,
            margin: 'auto',
            maxWidth: 640,
            position: 'relative',
          }}
        >
          <img
            alt={`${map.name} 地图`}
            src={previewUrl}
            style={{ display: 'block', height: 'auto', width: '100%' }}
          />
          {poseIsOnMap && pixelX !== undefined && pixelY !== undefined ? (
            <div
              title={`${robot.name}：(${pose?.x.toFixed(3)}, ${pose?.y.toFixed(3)}) m`}
              style={{
                alignItems: 'center',
                color: '#fff',
                display: 'flex',
                flexDirection: 'column',
                left: `${((pixelX + 0.5) / map.width) * 100}%`,
                pointerEvents: 'none',
                position: 'absolute',
                top: `${((pixelY + 0.5) / map.height) * 100}%`,
                transform: 'translate(-50%, -50%)',
              }}
            >
              <span
                style={{
                  alignItems: 'center',
                  background: poseUsable ? '#f5222d' : '#8c8c8c',
                  border: '2px solid #fff',
                  borderRadius: '50%',
                  boxShadow: '0 1px 6px #0008',
                  display: 'inline-flex',
                  height: 34,
                  justifyContent: 'center',
                  transform: `rotate(${yawDegrees}deg)`,
                  width: 34,
                }}
              >
                <RobotOutlined
                  style={{ transform: `rotate(${-yawDegrees}deg)` }}
                />
              </span>
            </div>
          ) : null}
        </div>
      ) : (
        <Alert
          showIcon
          type="info"
          message="正在加载机器人当前地图；地图不可用时仍会显示可信坐标与采集时间。"
        />
      )}
    </Card>
  );
}

export default function RobotControlPage() {
  const { initialState } = useModel('@@initialState');
  const { message, modal } = App.useApp();
  const [robots, setRobots] = useState<RobotSummary[]>([]);
  const [driveRobotId, setDriveRobotId] = useState<string>();
  const driveRobotIdRef = useRef<string | undefined>(undefined);
  const [loadingRobotId, setLoadingRobotId] = useState<string>();
  const [inputSource, setInputSource] = useState<InputSource>('none');
  const [gamepadEnabled, setGamepadEnabled] = useState(false);
  const [gamepadName, setGamepadName] = useState<string>();
  const [activeMap, setActiveMap] = useState<MapVersion>();
  const [activeMapPreview, setActiveMapPreview] = useState<string>();
  const heldKeysRef = useRef(new Set<string>());
  const buttonDirectionRef = useRef<JogDirection | undefined>(undefined);
  const gamepadMotionRef = useRef({ linear: 0, angular: 0 });
  const motionRef = useRef<Motion>({ source: 'none', linear: 0, angular: 0 });
  const lastJogSentAtRef = useRef(0);
  const lastJogErrorAtRef = useRef(0);
  const canMaintain = Boolean(initialState?.currentUser);
  const canResetSoftwareEmergencyStop = Boolean(initialState?.currentUser);
  const activeRobot = robots.find((robot) => robot.id === driveRobotId);
  const manualReady = Boolean(activeRobot);
  const driveBusy =
    activeRobot?.status === 'busy' || activeRobot?.status === 'mapping';

  const selectDriveRobot = useCallback((robotId?: string) => {
    driveRobotIdRef.current = robotId;
    setDriveRobotId(robotId);
  }, []);

  const setMotion = useCallback((motion: Motion) => {
    const previous = motionRef.current;
    motionRef.current = motion;
    if (previous.source !== motion.source) setInputSource(motion.source);
  }, []);

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
          (forward ? MANUAL_LINEAR_SPEED : 0) +
          (backward ? -MANUAL_LINEAR_SPEED : 0),
        angular:
          (left ? MANUAL_ANGULAR_SPEED : 0) +
          (right ? -MANUAL_ANGULAR_SPEED : 0),
      });
      return;
    }
    const buttonDirection = buttonDirectionRef.current;
    if (buttonDirection) {
      setMotion({ source: 'button', ...jogValues[buttonDirection] });
      return;
    }
    const gamepadMotion = gamepadMotionRef.current;
    if (gamepadMotion.linear || gamepadMotion.angular) {
      setMotion({ source: 'gamepad', ...gamepadMotion });
      return;
    }
    setMotion({ source: 'none', linear: 0, angular: 0 });
  }, [setMotion]);

  const stopMotion = useCallback(() => {
    heldKeysRef.current.clear();
    buttonDirectionRef.current = undefined;
    gamepadMotionRef.current = { linear: 0, angular: 0 };
    setMotion({ source: 'none', linear: 0, angular: 0 });
    sendCurrentMotion(true);
  }, [sendCurrentMotion, setMotion]);

  useEffect(() => {
    void getRobots()
      .then(setRobots)
      .catch((error) =>
        message.error(errorMessage(error, '读取机器人状态失败。')),
      );
    const unsubscribe = subscribeRobotRealtime(() => {
      const realtimeRobots = getRobotRealtimeState().robots;
      if (realtimeRobots.length) setRobots(realtimeRobots);
    });
    return () => {
      unsubscribe();
    };
  }, [message]);

  useEffect(() => {
    if (!manualReady || !activeRobot?.mapId) {
      setActiveMap(undefined);
      setActiveMapPreview(undefined);
      return;
    }
    let cancelled = false;
    let objectUrl: string | undefined;
    void getMapVersions()
      .then(async (maps) => {
        const map = maps.find((item) => item.id === activeRobot.mapId);
        if (!map) throw new Error('未找到机器人当前地图');
        const previewUrl = await getMapPreview(map.id);
        objectUrl = previewUrl;
        if (cancelled) return;
        setActiveMap(map);
        setActiveMapPreview(previewUrl);
      })
      .catch((error) => {
        if (!cancelled)
          message.error(errorMessage(error, '读取手动控制地图失败。'));
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [activeRobot?.mapId, manualReady, message]);

  useEffect(() => {
    if (!driveRobotId) {
      heldKeysRef.current.clear();
      buttonDirectionRef.current = undefined;
      gamepadMotionRef.current = { linear: 0, angular: 0 };
      setMotion({ source: 'none', linear: 0, angular: 0 });
    }
  }, [driveRobotId, setMotion]);

  useEffect(() => {
    if (!driveRobotId) return;
    const timer = window.setInterval(() => {
      if (motionRef.current.source !== 'none') sendCurrentMotion();
    }, MOTION_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [driveRobotId, sendCurrentMotion]);

  useEffect(() => {
    if (!driveRobotId) return;
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
  }, [driveRobotId, recomputeMotion, sendCurrentMotion, stopMotion]);

  useEffect(() => {
    if (!driveRobotId || !gamepadEnabled || !navigator.getGamepads) {
      gamepadMotionRef.current = { linear: 0, angular: 0 };
      recomputeMotion();
      return;
    }
    let animationFrame = 0;
    const pollGamepad = () => {
      const gamepad = Array.from(navigator.getGamepads()).find(
        (item): item is Gamepad => Boolean(item),
      );
      if (!gamepad) {
        gamepadMotionRef.current = { linear: 0, angular: 0 };
        setGamepadName(undefined);
      } else {
        setGamepadName(gamepad.id);
        gamepadMotionRef.current = {
          linear: -applyDeadzone(gamepad.axes[1] ?? 0) * MANUAL_LINEAR_SPEED,
          angular: -applyDeadzone(gamepad.axes[0] ?? 0) * MANUAL_ANGULAR_SPEED,
        };
      }
      recomputeMotion();
      animationFrame = window.requestAnimationFrame(pollGamepad);
    };
    pollGamepad();
    return () => {
      window.cancelAnimationFrame(animationFrame);
      gamepadMotionRef.current = { linear: 0, angular: 0 };
      recomputeMotion();
    };
  }, [driveRobotId, gamepadEnabled, recomputeMotion]);

  const endDriveControl = useCallback(() => {
    stopMotion();
    selectDriveRobot(undefined);
    setGamepadEnabled(false);
  }, [selectDriveRobot, stopMotion]);

  // 失焦、隐藏、离开页面或卸载时只补零速帧；车端 1 秒看门狗兜底停车。
  useEffect(() => {
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
  }, [stopMotion]);

  function beginDriveControl(robot: RobotSummary) {
    selectDriveRobot(robot.id);
    setGamepadEnabled(false);
  }

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

  async function emergencyStop(robot: RobotSummary) {
    if (driveRobotIdRef.current === robot.id) stopMotion();
    setLoadingRobotId(robot.id);
    try {
      const ack = await submitRobotCommand(robot.id, 'emergency_stop');
      if (driveRobotIdRef.current === robot.id) selectDriveRobot(undefined);
      setGamepadEnabled(false);
      const safetyNote = '请立即确认现场并优先使用硬件急停。';
      if (ack.detail?.includes('机械臂停止失败')) {
        message.error(`${robot.name}：${ack.detail}；${safetyNote}`);
      } else if (ack.detail?.includes('机械臂已停止')) {
        message.success(`${robot.name}：${ack.detail}；${safetyNote}`);
      } else {
        message.warning(
          ack.detail
            ? `${robot.name}：${ack.detail}；${safetyNote}`
            : `${robot.name} 已请求紧急停止；${safetyNote}`,
        );
      }
    } catch (error) {
      message.error(
        errorMessage(error, '紧急停止命令未发送。请立即使用硬件急停。'),
      );
    } finally {
      setLoadingRobotId(undefined);
    }
  }

  function requestSoftwareEmergencyStopReset(robot: RobotSummary) {
    modal.confirm({
      title: `解除 ${robot.name} 的软件急停锁定`,
      content:
        '请仅在确认机器人已静止、硬件急停已释放、通道安全且现场人员同意后继续。该操作不会解除硬件急停、不会向底盘发送速度，也不能修复其他硬件故障。',
      okText: '确认已完成现场检查',
      cancelText: '取消',
      okButtonProps: { danger: true },
      maskClosable: false,
      onOk: async () => {
        setLoadingRobotId(robot.id);
        try {
          const updatedRobot = await resetSoftwareEmergencyStop(robot.id);
          setRobots((current) =>
            current.map((item) => (item.id === robot.id ? updatedRobot : item)),
          );
          message.success(
            `${robot.name} 的软件急停锁定已解除，可重新开始手动控制。`,
          );
        } catch (error) {
          message.error(errorMessage(error, '无法解除软件紧急停止锁定。'));
        } finally {
          setLoadingRobotId(undefined);
        }
      },
    });
  }

  const softwareEmergencyStopRobots = robots.filter(
    (robot) =>
      robot.status === 'fault' &&
      robot.currentTask === '已请求紧急停止，等待 Gateway 确认',
  );

  return (
    <PageContainer
      title="机器人控制"
      content="网页仅提交低速控制意图。机器人本机 Edge Gateway 会校验限速与命令时长，并将速度送入 Nav2 平滑和碰撞监控链路后才可能驱动底盘。"
    >
      <Alert
        showIcon
        type="warning"
        title="安全手动控制"
        description="仅维护人员可使用；最高 0.1 m/s、0.2 rad/s。页面按钮和键盘为按住移动、松开即停；键盘优先于手柄。网页失焦、隐藏或命令超时都将发送零速度，但危险情况必须优先按机器人硬件急停。"
        style={{ marginBottom: 16 }}
      />
      {!canMaintain ? (
        <Alert
          showIcon
          type="error"
          title="当前账号没有维护权限"
          description="只有“系统管理员”或“维护人员”可使用手动控制。"
          style={{ marginBottom: 16 }}
        />
      ) : null}
      {softwareEmergencyStopRobots.map((robot) => (
        <Alert
          key={robot.id}
          showIcon
          type="error"
          title={`${robot.name} 处于软件紧急停止锁定`}
          description="确认机器人已静止、硬件急停已释放且现场安全后，才能解除平台锁定。该操作不会驱动底盘，也不会解除硬件急停。"
          action={
            <Button
              danger
              disabled={!canResetSoftwareEmergencyStop}
              loading={loadingRobotId === robot.id}
              onClick={() => requestSoftwareEmergencyStopReset(robot)}
            >
              解除软件急停锁定
            </Button>
          }
          style={{ marginBottom: 16 }}
        />
      ))}
      {manualReady && activeRobot ? (
        <Card
          title={`正在控制：${activeRobot.name}`}
          extra={
            driveBusy ? (
              <Tag color="geekblue">
                {activeRobot.status === 'mapping' ? '建图中' : '机械臂作业中'}
              </Tag>
            ) : (
              <Tag color="warning">手动控制中</Tag>
            )
          }
          style={{ marginBottom: 16 }}
        >
          <Descriptions
            column={{ xs: 1, sm: 2 }}
            size="small"
            style={{ marginBottom: 16 }}
          >
            <Descriptions.Item label="输入来源">
              {inputSourceLabel[inputSource]}
            </Descriptions.Item>
            <Descriptions.Item label="限速">
              {MANUAL_LINEAR_SPEED} m/s，{MANUAL_ANGULAR_SPEED} rad/s
            </Descriptions.Item>
            <Descriptions.Item label="键盘" span={2}>
              W / 上：前进；S / 下：后退；A / 左：左转；D /
              右：右转；空格：立即停止。
            </Descriptions.Item>
            <Descriptions.Item label="手柄" span={2}>
              左摇杆：前后和转向；死区 18%。
              {gamepadName ? ` 已连接：${gamepadName}` : ' 未检测到手柄。'}
            </Descriptions.Item>
          </Descriptions>
          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            <Col xs={24} xl={14}>
              <ManualPositionMap
                map={activeMap}
                previewUrl={activeMapPreview}
                robot={activeRobot}
              />
            </Col>
            <Col xs={24} xl={10}>
              <Alert
                showIcon
                type={
                  activeRobot.localizationStatus === 'localized'
                    ? 'success'
                    : 'warning'
                }
                message={
                  activeRobot.localizationStatus === 'localized'
                    ? '位置可用于实时监看'
                    : '定位尚未稳定'
                }
                description={
                  activeRobot.localizationStatus === 'localized'
                    ? '地图中的机器人图标按 Gateway 上报的 AMCL 位姿刷新。'
                    : '先到地图建模页确认机器人实际位置，待 AMCL 收敛后才能显示可信实时位置。'
                }
              />
            </Col>
          </Row>
          <Space wrap>
            <Button
              disabled={driveBusy}
              icon={<ArrowUpOutlined />}
              onPointerDown={(event) => {
                event.preventDefault();
                event.currentTarget.setPointerCapture(event.pointerId);
                startButtonMotion('forward');
              }}
              onPointerUp={stopButtonMotion}
              onPointerLeave={stopButtonMotion}
              onPointerCancel={stopButtonMotion}
            >
              按住前进
            </Button>
            <Button
              disabled={driveBusy}
              icon={<ArrowDownOutlined />}
              onPointerDown={(event) => {
                event.preventDefault();
                event.currentTarget.setPointerCapture(event.pointerId);
                startButtonMotion('backward');
              }}
              onPointerUp={stopButtonMotion}
              onPointerLeave={stopButtonMotion}
              onPointerCancel={stopButtonMotion}
            >
              按住后退
            </Button>
            <Button
              disabled={driveBusy}
              icon={<ArrowLeftOutlined />}
              onPointerDown={(event) => {
                event.preventDefault();
                event.currentTarget.setPointerCapture(event.pointerId);
                startButtonMotion('left');
              }}
              onPointerUp={stopButtonMotion}
              onPointerLeave={stopButtonMotion}
              onPointerCancel={stopButtonMotion}
            >
              按住左转
            </Button>
            <Button
              disabled={driveBusy}
              icon={<ArrowRightOutlined />}
              onPointerDown={(event) => {
                event.preventDefault();
                event.currentTarget.setPointerCapture(event.pointerId);
                startButtonMotion('right');
              }}
              onPointerUp={stopButtonMotion}
              onPointerLeave={stopButtonMotion}
              onPointerCancel={stopButtonMotion}
            >
              按住右转
            </Button>
            <Button icon={<StopOutlined />} onClick={stopMotion}>
              立即停止
            </Button>
            <Button
              disabled={driveBusy}
              onClick={() => setGamepadEnabled((enabled) => !enabled)}
            >
              {gamepadEnabled ? '停用手柄' : '启用手柄'}
            </Button>
            <Button danger onClick={endDriveControl}>
              结束控制
            </Button>
          </Space>
        </Card>
      ) : null}
      <Row gutter={[16, 16]}>
        {robots.map((robot) => {
          const controllingThisRobot = driveRobotId === robot.id && manualReady;
          const isSoftwareEmergencyStop =
            robot.status === 'fault' &&
            robot.currentTask === '已请求紧急停止，等待 Gateway 确认';
          const canDrive =
            canMaintain &&
            !driveRobotId &&
            ['idle', 'navigating'].includes(robot.status) &&
            robot.health.chassis;
          const controlAvailability = !canMaintain
            ? '当前账号没有维护权限'
            : driveRobotId
              ? '已有机器人在手动控制'
              : !robot.health.chassis
                ? '底盘健康检查未通过'
                : isSoftwareEmergencyStop
                  ? '软件紧急停止锁定；现场检查后先解除'
                  : robot.status === 'busy'
                    ? '机械臂作业中，待完成后再控制'
                    : robot.status === 'mapping'
                      ? '机器人正在建图，结束并恢复导航后再控制'
                      : robot.status === 'charging'
                        ? '机器人正在充电'
                        : robot.status === 'offline'
                          ? '机器人离线'
                          : robot.status === 'fault'
                            ? '机器人存在硬件或安全故障'
                            : '可手动控制';
          return (
            <Col key={robot.id} xs={24} xl={12}>
              <Card
                title={
                  <Space>
                    {robot.name} <RobotStatusTag status={robot.status} />
                  </Space>
                }
                size="small"
                extra={
                  controllingThisRobot ? (
                    <Tag color="warning">手动控制中</Tag>
                  ) : null
                }
              >
                {robot.health.imu === false ? (
                  <Alert
                    showIcon
                    type="error"
                    title={IMU_DISCONNECTED_ALERT}
                    style={{ marginBottom: 12 }}
                  />
                ) : null}
                <Descriptions column={2} size="small">
                  <Descriptions.Item label="机器人 ID">
                    {robot.id}
                  </Descriptions.Item>
                  <Descriptions.Item label="地图">
                    {robot.mapVersion}
                  </Descriptions.Item>
                  <Descriptions.Item label="当前位置" span={2}>
                    {robot.location}
                  </Descriptions.Item>
                  <Descriptions.Item label="Gateway" span={2}>
                    {robot.lastSeenAt ? '遥测已接入' : '未接入'}
                  </Descriptions.Item>
                  <Descriptions.Item label="底盘健康">
                    {robot.health.chassis ? '通过' : '未通过'}
                  </Descriptions.Item>
                  <Descriptions.Item label="IMU">
                    <Tag color={imuHealthStatus(robot.health.imu).color}>
                      {imuHealthStatus(robot.health.imu).text}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="控制状态">
                    {controllingThisRobot ? '手动控制中' : '未控制'}
                  </Descriptions.Item>
                  <Descriptions.Item label="控制条件" span={2}>
                    {controlAvailability}
                  </Descriptions.Item>
                </Descriptions>
                <Space wrap style={{ marginTop: 16 }}>
                  {controllingThisRobot ? (
                    <Button
                      icon={<ControlOutlined />}
                      onClick={() =>
                        window.scrollTo({ top: 0, behavior: 'smooth' })
                      }
                    >
                      查看控制台
                    </Button>
                  ) : (
                    <Button
                      disabled={!canDrive}
                      icon={<ControlOutlined />}
                      onClick={() => beginDriveControl(robot)}
                    >
                      {driveRobotId ? '已有机器人在手动控制' : '手动控制'}
                    </Button>
                  )}
                  {isSoftwareEmergencyStop ? (
                    <Button
                      danger
                      disabled={!canResetSoftwareEmergencyStop}
                      loading={loadingRobotId === robot.id}
                      onClick={() => requestSoftwareEmergencyStopReset(robot)}
                    >
                      解除软件急停锁定
                    </Button>
                  ) : null}
                  <Button
                    danger
                    loading={loadingRobotId === robot.id}
                    icon={<PoweroffOutlined />}
                    onClick={() => void emergencyStop(robot)}
                  >
                    请求紧急停止
                  </Button>
                </Space>
              </Card>
            </Col>
          );
        })}
      </Row>
    </PageContainer>
  );
}
