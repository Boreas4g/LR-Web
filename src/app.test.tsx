import { beforeEach, describe, expect, it, vi } from 'vitest';

const mockHistory = {
  location: { pathname: '/overview/dashboard', search: '', hash: '' },
  replace: vi.fn(),
};

vi.mock('@umijs/max', () => ({
  history: mockHistory,
  Link: ({ children }: { children: unknown }) => children,
  useModel: () => ({ initialState: undefined, setInitialState: vi.fn() }),
}));

vi.mock('./services/libraryRobot', () => ({
  getRobotRealtimeState: () => ({ connected: false }),
  startRobotRealtime: vi.fn(),
  stopRobotRealtime: vi.fn(),
  subscribeRobotRealtime: () => () => undefined,
}));

vi.mock('../config/defaultSettings', () => ({
  default: { navTheme: 'light' },
}));

describe('app getInitialState', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockHistory.location = {
      pathname: '/overview/dashboard',
      search: '',
      hash: '',
    };
  });

  it('always provides the built-in operator user without a login request', async () => {
    const { getInitialState } = await import('./app');

    const state = await getInitialState();

    expect(state.currentUser).toEqual({
      username: 'operator',
      name: '操作员',
      role: '系统管理员',
    });
    expect(state.settings).toEqual({ navTheme: 'light' });
    expect(state.settingDrawerOpen).toBe(false);
    expect(await state.fetchUserInfo()).toEqual(state.currentUser);
    expect(mockHistory.replace).not.toHaveBeenCalled();
  });
});
