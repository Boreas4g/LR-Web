import { describe, expect, it } from 'vitest';

import { IMU_DISCONNECTED_ALERT, imuHealthStatus } from './health';

describe('imuHealthStatus', () => {
  it('maps a healthy IMU to a green 正常 label', () => {
    expect(imuHealthStatus(true)).toEqual({ color: 'success', text: '正常' });
  });

  it('maps a disconnected IMU to a red 异常 label', () => {
    expect(imuHealthStatus(false)).toEqual({ color: 'error', text: '异常' });
  });
});

describe('IMU_DISCONNECTED_ALERT', () => {
  it('keeps the exact operator-facing warning text', () => {
    expect(IMU_DISCONNECTED_ALERT).toBe(
      'IMU 断联：里程计与定位不可用，请检查 IMU 连接',
    );
  });
});
