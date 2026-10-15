import { describe, expect, it } from 'vitest';

import {
  applyDeadzone,
  firstConnectedGamepad,
  GAMEPAD_ANGULAR_SPEED,
  GAMEPAD_DEADZONE,
  GAMEPAD_JOG_INTERVAL_MS,
  GAMEPAD_LINEAR_SPEED,
  gamepadVelocity,
  isZeroVelocity,
  shouldSendGamepadJog,
} from './gamepad';

function makeGamepad(axes: number[]): Gamepad {
  return { id: 'Test Pad', connected: true, axes } as unknown as Gamepad;
}

describe('applyDeadzone', () => {
  it('returns zero inside the deadzone', () => {
    expect(applyDeadzone(0)).toBe(0);
    expect(applyDeadzone(GAMEPAD_DEADZONE - 0.001)).toBe(0);
    expect(applyDeadzone(-GAMEPAD_DEADZONE + 0.001)).toBe(0);
  });

  it('rescales values outside the deadzone to keep full range', () => {
    expect(applyDeadzone(1)).toBe(1);
    expect(applyDeadzone(-1)).toBe(-1);
    expect(applyDeadzone(GAMEPAD_DEADZONE)).toBeCloseTo(0, 10);
    expect(applyDeadzone(0.55)).toBeCloseTo(0.5, 10);
    expect(applyDeadzone(-0.55)).toBeCloseTo(-0.5, 10);
  });
});

describe('gamepadVelocity', () => {
  it('maps stick forward to positive linear velocity', () => {
    const velocity = gamepadVelocity(makeGamepad([0, -1]));
    expect(velocity.linear).toBeCloseTo(GAMEPAD_LINEAR_SPEED, 10);
    expect(velocity.angular).toBe(0);
  });

  it('maps stick left to positive angular velocity', () => {
    const velocity = gamepadVelocity(makeGamepad([-1, 0]));
    expect(velocity.linear).toBe(0);
    expect(velocity.angular).toBeCloseTo(GAMEPAD_ANGULAR_SPEED, 10);
  });

  it('scales linearly with stick travel', () => {
    const velocity = gamepadVelocity(makeGamepad([0, -0.55]));
    expect(velocity.linear).toBeCloseTo(GAMEPAD_LINEAR_SPEED * 0.5, 10);
  });

  it('outputs zero inside the deadzone', () => {
    expect(isZeroVelocity(gamepadVelocity(makeGamepad([0.05, -0.05])))).toBe(
      true,
    );
  });

  it('scales to explicit speed limits when provided', () => {
    const forward = gamepadVelocity(makeGamepad([0, -1]), 0.15, 0.5);
    expect(forward.linear).toBeCloseTo(0.15, 10);
    expect(forward.angular).toBe(0);
    const left = gamepadVelocity(makeGamepad([-1, 0]), 0.15, 0.5);
    expect(left.linear).toBe(0);
    expect(left.angular).toBeCloseTo(0.5, 10);
    const half = gamepadVelocity(makeGamepad([0, -0.55]), 0.15, 0.5);
    expect(half.linear).toBeCloseTo(0.075, 10);
  });
});

describe('shouldSendGamepadJog', () => {
  const forward = { linear: 0.3, angular: 0 };
  const zero = { linear: 0, angular: 0 };

  it('sends the first non-zero command immediately', () => {
    expect(shouldSendGamepadJog(undefined, forward, 0)).toBe(true);
  });

  it('sends nothing while the stick stays centered', () => {
    expect(shouldSendGamepadJog(undefined, zero, 0)).toBe(false);
    expect(shouldSendGamepadJog({ velocity: zero, at: 0 }, zero, 10_000)).toBe(
      false,
    );
  });

  it('throttles the analog stream to one command per interval', () => {
    const lastSent = { velocity: forward, at: 0 };
    expect(shouldSendGamepadJog(lastSent, forward, 50)).toBe(false);
    expect(
      shouldSendGamepadJog(lastSent, forward, GAMEPAD_JOG_INTERVAL_MS),
    ).toBe(true);
  });

  it('keeps resending a held deflection to beat the watchdog', () => {
    const lastSent = { velocity: forward, at: 1_000 };
    expect(
      shouldSendGamepadJog(lastSent, forward, 1_000 + GAMEPAD_JOG_INTERVAL_MS),
    ).toBe(true);
  });

  it('sends a final zero after the stick centers', () => {
    const lastSent = { velocity: forward, at: 0 };
    expect(shouldSendGamepadJog(lastSent, zero, GAMEPAD_JOG_INTERVAL_MS)).toBe(
      true,
    );
  });
});

describe('firstConnectedGamepad', () => {
  it('returns undefined when the Gamepad API is unavailable', () => {
    expect(firstConnectedGamepad()).toBeUndefined();
  });
});
