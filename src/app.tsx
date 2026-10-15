import {
  BellOutlined,
  CloudServerOutlined,
  MoonOutlined,
  SunOutlined,
  UserOutlined,
} from '@ant-design/icons';
import type { Settings as LayoutSettings } from '@ant-design/pro-components';
import type { RunTimeLayoutConfig } from '@umijs/max';
import { history, Link, useModel } from '@umijs/max';
import {
  App,
  Avatar,
  Badge,
  Button,
  ConfigProvider,
  Dropdown,
  Tag,
  Tooltip,
  Typography,
  theme,
} from 'antd';
import React, { useEffect } from 'react';

import defaultSettings from '../config/defaultSettings';
import { getCurrentUser, type LocalUser } from './services/auth';
import { isDebugMode, isDebugPath } from './utils/debugMode';
import {
  getAlerts,
  getRobotRealtimeState,
  startRobotRealtime,
  stopRobotRealtime,
  subscribeRobotRealtime,
} from './services/libraryRobot';
import type { AlertRecord } from './services/libraryRobot/types';

type ThemeMode = 'light' | 'dark';

export type InitialStateType = {
  settings: Partial<LayoutSettings>;
  currentUser?: LocalUser;
  themeMode: ThemeMode;
  fetchUserInfo: () => Promise<LocalUser | undefined>;
  settingDrawerOpen: boolean;
};

const THEME_STORAGE_KEY = 'lr-web-theme';

function getThemeMode(): ThemeMode {
  if (typeof window === 'undefined') return 'light';
  return window.localStorage.getItem(THEME_STORAGE_KEY) === 'dark'
    ? 'dark'
    : 'light';
}

function getLayoutSettings(themeMode: ThemeMode): Partial<LayoutSettings> {
  return {
    ...(defaultSettings as Partial<LayoutSettings>),
    navTheme: themeMode === 'dark' ? 'realDark' : 'light',
  };
}

export async function getInitialState(): Promise<InitialStateType> {
  const themeMode = getThemeMode();
  return {
    currentUser: getCurrentUser(),
    settings: getLayoutSettings(themeMode),
    themeMode,
    fetchUserInfo: async () => getCurrentUser(),
    settingDrawerOpen: false,
  };
}

function AppTheme({ children }: { children: React.ReactNode }) {
  const { initialState } = useModel('@@initialState');
  const themeMode = initialState?.themeMode ?? 'light';

  useEffect(() => {
    document.documentElement.dataset.theme = themeMode;
  }, [themeMode]);

  useEffect(() => {
    if (initialState?.currentUser) startRobotRealtime();
    else stopRobotRealtime();
  }, [initialState?.currentUser?.username]);

  return (
    <ConfigProvider
      theme={{
        algorithm:
          themeMode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          colorPrimary: '#1677ff',
          fontFamily: 'AlibabaSans, sans-serif',
        },
      }}
    >
      <App>{children}</App>
    </ConfigProvider>
  );
}

function ThemeToggle() {
  const { initialState, setInitialState } = useModel('@@initialState');
  const themeMode = initialState?.themeMode ?? 'light';
  const isDark = themeMode === 'dark';

  function toggleTheme() {
    const nextThemeMode: ThemeMode = isDark ? 'light' : 'dark';
    window.localStorage.setItem(THEME_STORAGE_KEY, nextThemeMode);
    setInitialState((state) => ({
      ...(state ?? {}),
      settings: getLayoutSettings(nextThemeMode),
      themeMode: nextThemeMode,
      fetchUserInfo: state?.fetchUserInfo ?? (async () => getCurrentUser()),
      settingDrawerOpen: state?.settingDrawerOpen ?? false,
    }));
  }

  return (
    <Tooltip title={isDark ? '切换为浅色模式' : '切换为深色模式'}>
      <Button
        type="text"
        aria-label={isDark ? '切换为浅色模式' : '切换为深色模式'}
        icon={isDark ? <SunOutlined /> : <MoonOutlined />}
        onClick={toggleTheme}
      />
    </Tooltip>
  );
}

function ControlStationStatus() {
  const { initialState } = useModel('@@initialState');
  const [connected, setConnected] = React.useState(
    () => getRobotRealtimeState().connected,
  );
  const [health, setHealth] = React.useState<
    'checking' | 'ok' | 'degraded' | 'unavailable'
  >('checking');

  useEffect(
    () =>
      subscribeRobotRealtime(() =>
        setConnected(getRobotRealtimeState().connected),
      ),
    [],
  );

  useEffect(() => {
    if (!initialState?.currentUser) return undefined;
    let disposed = false;
    async function refreshHealth() {
      try {
        const response = await fetch('/health', { cache: 'no-store' });
        const payload = (await response.json()) as { status?: string };
        if (
          !response.ok ||
          !['ok', 'degraded'].includes(payload.status ?? '')
        ) {
          throw new Error('Unexpected health response');
        }
        if (!disposed) setHealth(payload.status as 'ok' | 'degraded');
      } catch {
        if (!disposed) setHealth('unavailable');
      }
    }
    void refreshHealth();
    const timer = window.setInterval(refreshHealth, 15_000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [initialState?.currentUser?.username]);

  if (!initialState?.currentUser) return null;
  const status = !connected
    ? {
        color: 'warning',
        label: '正在重连控制站',
        help: '暂未收到实时状态；控制命令不会直连机器人',
      }
    : health === 'degraded'
      ? {
          color: 'warning',
          label: '控制站需检查',
          help: '控制站可访问，但机器人消息链路未就绪；请联系维护人员。',
        }
      : health === 'unavailable'
        ? {
            color: 'error',
            label: '控制站健康检查失败',
            help: '无法读取控制站健康状态；请联系维护人员检查服务和网络。',
          }
        : health === 'checking'
          ? {
              color: 'processing',
              label: '正在检查控制站',
              help: '正在确认控制站服务和机器人消息链路。',
            }
          : {
              color: 'success',
              label: '控制站已连接',
              help: '控制站服务和机器人消息链路均正常。',
            };
  return (
    <Tooltip title={status.help}>
      <Tag color={status.color} icon={<CloudServerOutlined />}>
        {status.label}
      </Tag>
    </Tooltip>
  );
}

const ALERT_POLL_INTERVAL_MS = 30_000;

function NotificationBell() {
  const { initialState } = useModel('@@initialState');
  const [alerts, setAlerts] = React.useState<AlertRecord[]>([]);

  useEffect(() => {
    if (!initialState?.currentUser) return undefined;
    let disposed = false;
    async function refresh() {
      try {
        const data = await getAlerts();
        if (!disposed) setAlerts(data);
      } catch {
        // 拉取失败静默处理：保持现有状态，不在顶栏报错。
      }
    }
    void refresh();
    const timer = window.setInterval(refresh, ALERT_POLL_INTERVAL_MS);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [initialState?.currentUser?.username]);

  if (!initialState?.currentUser) return null;
  const unacknowledged = alerts.filter((alert) => !alert.acknowledged);
  // 无未确认告警时不显示 Badge（count=0 即隐藏）。
  const items =
    unacknowledged.length === 0
      ? [{ key: 'empty', label: '暂无未确认告警', disabled: true }]
      : unacknowledged.slice(0, 8).map((alert) => ({
          key: alert.id,
          label: `${alert.title}（${alert.time}）`,
        }));
  return (
    <Dropdown key="notifications" menu={{ items }}>
      <Badge count={unacknowledged.length} size="small">
        <Button type="text" icon={<BellOutlined />} aria-label="告警通知" />
      </Badge>
    </Dropdown>
  );
}

export const layout: RunTimeLayoutConfig = ({ initialState }) => ({
  title: '智巡-图书馆机器人',
  logo: <CloudServerOutlined style={{ color: '#69b1ff', fontSize: 22 }} />,
  menuItemRender: (item, dom) => {
    // 调试中心整组菜单（含子项）仅在调试模式开启时显示
    if (
      !isDebugMode() &&
      (item.key === 'debug' || (item.path && isDebugPath(item.path)))
    ) {
      return null;
    }
    return item.path ? <Link to={item.path}>{dom}</Link> : dom;
  },
  actionsRender: () => [
    <ThemeToggle key="theme" />,
    <ControlStationStatus key="control-station" />,
    <NotificationBell key="notifications" />,
  ],
  avatarProps: {
    render: () => (
      <Dropdown
        menu={{
          items: [
            {
              key: 'role',
              label: initialState?.currentUser?.role ?? '操作员',
              disabled: true,
            },
          ],
        }}
      >
        <Button type="text" className="user-menu-button">
          <Avatar size={28} icon={<UserOutlined />} />
          <span className="user-menu-details">
            <Typography.Text className="user-menu-name">
              {initialState?.currentUser?.name}
            </Typography.Text>
            <Typography.Text type="secondary" className="user-menu-role">
              {initialState?.currentUser?.role}
            </Typography.Text>
          </span>
        </Button>
      </Dropdown>
    ),
  },
  footerRender: false,
  childrenRender: (children) => <AppTheme>{children}</AppTheme>,
  ...initialState?.settings,
});

// 路由守卫：调试模式未开启时，调试中心相关地址一律退回运行总览
export function onRouteChange({
  location,
}: {
  location: { pathname: string };
}) {
  if (!isDebugMode() && isDebugPath(location.pathname)) {
    history.replace('/overview/dashboard');
  }
}
