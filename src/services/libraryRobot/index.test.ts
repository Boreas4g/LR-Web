import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  acknowledgeAlert,
  getAlerts,
  getTasks,
  pollTasks,
} from '@/services/libraryRobot';

function mockFetchJson(payload: unknown, status = 200) {
  return vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
    headers: new Headers(),
  })) as unknown as typeof fetch;
}

describe('libraryRobot task polling API', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', mockFetchJson([]));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('getTasks 不带参数时请求全量列表（保持兼容）', async () => {
    await getTasks();
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/dispatch/tasks',
      expect.anything(),
    );
  });

  it('getTasks 追加 status 与 limit 查询参数', async () => {
    await getTasks({ status: ['running', 'queued'], limit: 20 });
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(url).toBe(
      '/api/v1/dispatch/tasks?status=running&status=queued&limit=20',
    );
  });

  it('pollTasks 只拉活动状态加最近 20 条', async () => {
    await pollTasks();
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(url).toBe(
      '/api/v1/dispatch/tasks?status=queued&status=running&status=paused&limit=20',
    );
  });
});

describe('libraryRobot alerts API', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      mockFetchJson({
        id: 'ALT-1',
        severity: 'warning',
        source: 'LR-01',
        title: '测试告警',
        time: '10:42:17',
        acknowledged: true,
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('getAlerts 请求真实告警接口', async () => {
    await getAlerts();
    expect(fetch).toHaveBeenCalledWith('/api/v1/alerts', expect.anything());
  });

  it('acknowledgeAlert 发送 POST 到 ack 端点并转义 ID', async () => {
    const result = await acknowledgeAlert('ALT/1');
    const [url, options] = (fetch as ReturnType<typeof vi.fn>).mock
      .calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/alerts/ALT%2F1/ack');
    expect(options.method).toBe('POST');
    expect(result.acknowledged).toBe(true);
  });
});
