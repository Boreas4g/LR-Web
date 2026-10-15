export interface HealthStatusLabel {
  color: 'success' | 'error';
  text: '正常' | '异常';
}

/**
 * An IMU dropout silently degrades the odometry yaw integration, so the
 * health mapping always resolves to a visible red/green state with no
 * "unknown" middle ground.
 */
export function imuHealthStatus(imu: boolean): HealthStatusLabel {
  return imu
    ? { color: 'success', text: '正常' }
    : { color: 'error', text: '异常' };
}

export const IMU_DISCONNECTED_ALERT =
  'IMU 断联：里程计与定位不可用，请检查 IMU 连接';
