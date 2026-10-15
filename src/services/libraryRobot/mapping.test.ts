import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@umijs/max', () => ({
  request: vi.fn(),
}));

import {
  clearMappingMarks,
  createMappingMark,
  deleteMappingMark,
  getMappingMarks,
  getMappingPose,
} from './index';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('libraryRobot mapping API', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads the current mapping pose', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ ok: true, x: 1, y: 2, yaw: 0.5 }),
    );
    const pose = await getMappingPose('R1');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/robots/R1/mapping/pose',
      expect.objectContaining({}),
    );
    expect(pose).toEqual({ ok: true, x: 1, y: 2, yaw: 0.5 });
  });

  it('lists mapping marks', async () => {
    const marks = [{ x: 1.5, y: -0.25, yaw: 0, label: '书架点 1', at: '2026-01-01T00:00:00Z' }];
    fetchMock.mockResolvedValue(jsonResponse({ marks }));
    const result = await getMappingMarks('R1');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/robots/R1/mapping/marks',
      expect.objectContaining({}),
    );
    expect(result.marks).toEqual(marks);
  });

  it('creates a mapping mark with the given label', async () => {
    const mark = { x: 1, y: 2, yaw: 0, label: '书架点 2', at: '2026-01-01T00:00:00Z' };
    fetchMock.mockResolvedValue(jsonResponse({ mark, count: 2 }));
    const result = await createMappingMark('R1', '书架点 2');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/robots/R1/mapping/mark',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ label: '书架点 2' }),
      }),
    );
    expect(result).toEqual({ mark, count: 2 });
  });

  it('creates a mapping mark without a label', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ mark: {}, count: 1 }));
    await createMappingMark('R1');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/robots/R1/mapping/mark',
      expect.objectContaining({ method: 'POST', body: '{}' }),
    );
  });

  it('clears all mapping marks', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true, cleared: 3 }));
    const result = await clearMappingMarks('R1');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/robots/R1/mapping/marks/clear',
      expect.objectContaining({ method: 'POST', body: '{}' }),
    );
    expect(result).toEqual({ ok: true, cleared: 3 });
  });

  it('deletes one mapping mark by index', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true, remaining: 2 }));
    await deleteMappingMark('R1', 1);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/robots/R1/mapping/marks/1',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });

  it('surfaces the agent 404 when the mark index is out of range', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: '标注序号越界' }, 404));
    await expect(deleteMappingMark('R1', 9)).rejects.toThrow('标注序号越界');
  });

  it('surfaces the backend detail message on failure', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: '机器人不在建图模式' }, 409),
    );
    await expect(createMappingMark('R1', '书架点 1')).rejects.toThrow(
      '机器人不在建图模式',
    );
  });
});
