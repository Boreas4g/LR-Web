export const GAMEPAD_LINEAR_SPEED = 0.3;
export const GAMEPAD_ANGULAR_SPEED = 0.8;
export const GAMEPAD_DEADZONE = 0.1;
export const GAMEPAD_JOG_INTERVAL_MS = 100;

export type JogVelocity = { linear: number; angular: number };

export const ZERO_VELOCITY: JogVelocity = { linear: 0, angular: 0 };

/** Zero out stick noise inside the deadzone, then rescale to keep full range. */
export function applyDeadzone(value: number, deadzone = GAMEPAD_DEADZONE) {
  if (Math.abs(value) < deadzone) return 0;
  return (Math.sign(value) * (Math.abs(value) - deadzone)) / (1 - deadzone);
}

/**
 * Standard mapping: left stick forward (axes[1] < 0) drives forward,
 * left stick left (axes[0] < 0) turns left (positive angular velocity).
 * Speeds default to the mapping-page limits; manual-control pages pass
 * their own fixed limits instead. Adding 0 normalizes -0 so centered
 * sticks compare equal to ZERO_VELOCITY.
 */
export function gamepadVelocity(
  gamepad: Gamepad,
  linearSpeed = GAMEPAD_LINEAR_SPEED,
  angularSpeed = GAMEPAD_ANGULAR_SPEED,
): JogVelocity {
  return {
    linear: -applyDeadzone(gamepad.axes[1] ?? 0) * linearSpeed + 0,
    angular: -applyDeadzone(gamepad.axes[0] ?? 0) * angularSpeed + 0,
  };
}

export function isZeroVelocity(velocity: JogVelocity) {
  return velocity.linear === 0 && velocity.angular === 0;
}

export function firstConnectedGamepad() {
  if (
    typeof navigator === 'undefined' ||
    typeof navigator.getGamepads !== 'function'
  )
    return undefined;
  return Array.from(navigator.getGamepads()).find((item) => item?.connected);
}

export type SentJog = { velocity: JogVelocity; at: number };

/**
 * Throttle the analog jog stream to one command per GAMEPAD_JOG_INTERVAL_MS.
 * Zero velocity is only sent to stop a previously commanded motion; a
 * centered stick that never moved sends nothing.
 */
export function shouldSendGamepadJog(
  lastSent: SentJog | undefined,
  next: JogVelocity,
  now: number,
) {
  if (!lastSent) return !isZeroVelocity(next);
  if (now - lastSent.at < GAMEPAD_JOG_INTERVAL_MS) return false;
  return !isZeroVelocity(next) || !isZeroVelocity(lastSent.velocity);
}
