import { describe, expect, it } from 'vitest';

import { errorMessage } from './errorText';

describe('errorText', () => {
  it('translates camera frame timeouts with a recovery hint', () => {
    const text = errorMessage(
      new Error("Frame didn't arrive within 5000"),
      '拍照失败。',
    );
    expect(text).toContain('相机取帧失败');
    expect(text).toContain('自动重试');
    expect(text).toContain("Frame didn't arrive within 5000");
  });

  it('translates inverse kinematics failures', () => {
    const text = errorMessage(
      new Error('MovePByJ inverse kinematics failed: did not converge'),
      '执行失败。',
    );
    expect(text).toContain('机械臂无法到达');
  });

  it('translates serial EIO with the reconnect hint', () => {
    const text = errorMessage(
      new Error('write failed: [Errno 5] Input/output error'),
      '推杆移动失败。',
    );
    expect(text).toContain('自动重连');
  });

  it('translates bare 503s', () => {
    expect(errorMessage(new Error('请求失败（503）'), '操作失败。')).toContain(
      '本机服务暂时不可用',
    );
  });

  it('passes Chinese backend detail through unchanged', () => {
    expect(errorMessage(new Error('请先准备（识别目标）'), '执行失败。')).toBe(
      '请先准备（识别目标）',
    );
  });

  it('keeps unmatched raw detail after the fallback context', () => {
    const text = errorMessage(
      new Error('some exotic driver state'),
      '执行失败。',
    );
    expect(text).toContain('执行失败。');
    expect(text).toContain('some exotic driver state');
  });

  it('returns the fallback for non-error input', () => {
    expect(errorMessage(undefined, '执行失败。')).toBe('执行失败。');
  });

  it('does not mistranslate on-robot operation timeouts as connection failures', () => {
    // 车端作业超时（recovery_required 的 failureReason）应原文展示，
    // 而不是误译为「无法连接服务器」。
    const raw = 'timed out（需要人工接管恢复（机械臂可能夹着书停在半路））';
    expect(errorMessage(new Error(raw), '本轮执行失败。')).toBe(raw);
    const text = errorMessage(new Error('timed out'), '执行失败。');
    expect(text).not.toContain('无法连接服务器');
    expect(text).toContain('timed out');
  });
});
