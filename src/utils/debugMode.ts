/**
 * 调试模式开关：进入需密码，退出后调试中心菜单与页面全部隐藏。
 *
 * 说明：这是纯前端显示开关（密码明文打包在前端 bundle 中），用于避免
 * 现场人员误入调试页，不构成安全边界；真正的代码保护在服务端编译层。
 */
import { useEffect, useState } from 'react';

const DEBUG_MODE_KEY = 'lr-debug-mode';
const DEBUG_MODE_EVENT = 'lr-debug-mode-changed';
const DEBUG_PASSWORD = 'LR123456';

export function isDebugMode(): boolean {
  if (typeof window === 'undefined') return false;
  return window.localStorage.getItem(DEBUG_MODE_KEY) === '1';
}

export function verifyDebugPassword(input: string): boolean {
  return input === DEBUG_PASSWORD;
}

export function setDebugMode(on: boolean): void {
  if (on) {
    window.localStorage.setItem(DEBUG_MODE_KEY, '1');
  } else {
    window.localStorage.removeItem(DEBUG_MODE_KEY);
  }
  window.dispatchEvent(new Event(DEBUG_MODE_EVENT));
}

/** 调试中心占用的 URL 前缀/页面（与 config/routes.ts 的 debug 组一致） */
export function isDebugPath(pathname: string): boolean {
  return (
    pathname.startsWith('/debug/') ||
    pathname === '/circulation/hardware-debug' ||
    pathname === '/circulation/spine-studio' ||
    pathname.endsWith('-tuning')
  );
}

/** 组件内订阅调试模式状态（切换后自动重渲染） */
export function useDebugMode(): boolean {
  const [on, setOn] = useState(isDebugMode);
  useEffect(() => {
    const handler = () => setOn(isDebugMode());
    window.addEventListener(DEBUG_MODE_EVENT, handler);
    window.addEventListener('storage', handler);
    return () => {
      window.removeEventListener(DEBUG_MODE_EVENT, handler);
      window.removeEventListener('storage', handler);
    };
  }, []);
  return on;
}
