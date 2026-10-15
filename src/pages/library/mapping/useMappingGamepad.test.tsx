import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { sendMappingJog } from '@/services/libraryRobot';
import { useMappingGamepad } from './useMappingGamepad';

vi.mock('@/services/libraryRobot', () => ({
  sendMappingJog: vi.fn(() => Promise.resolve({ ok: true })),
}));

const sendMappingJogMock = vi.mocked(sendMappingJog);

function makeGamepad(axes: number[], markPressed = false): Gamepad {
  return {
    id: 'Test Pad (STANDARD GAMEPAD)',
    connected: true,
    axes,
    buttons: [
      {
        pressed: markPressed,
        touched: markPressed,
        value: markPressed ? 1 : 0,
      },
    ],
  } as unknown as Gamepad;
}

describe('useMappingGamepad', () => {
  let now: number;
  let gamepads: (Gamepad | null)[];
  let frameQueue: FrameRequestCallback[];
  let hadGetGamepads: boolean;

  function runFrame() {
    const callback = frameQueue.shift();
    if (callback) callback(now);
  }

  beforeEach(() => {
    now = 0;
    gamepads = [makeGamepad([0, 0])];
    frameQueue = [];
    sendMappingJogMock.mockClear();

    hadGetGamepads = 'getGamepads' in navigator;
    Object.defineProperty(navigator, 'getGamepads', {
      configurable: true,
      value: () => gamepads,
    });
    Object.defineProperty(window, 'requestAnimationFrame', {
      configurable: true,
      value: (callback: FrameRequestCallback) => {
        frameQueue.push(callback);
        return frameQueue.length;
      },
    });
    Object.defineProperty(window, 'cancelAnimationFrame', {
      configurable: true,
      value: () => undefined,
    });
    vi.spyOn(performance, 'now').mockImplementation(() => now);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    if (!hadGetGamepads) {
      delete (navigator as { getGamepads?: unknown }).getGamepads;
    }
  });

  function renderActive(active = true, onMark?: () => void) {
    return renderHook(
      ({ active: enabled }) =>
        useMappingGamepad({ robotId: 'R1', active: enabled, onMark }),
      { initialProps: { active } },
    );
  }

  it('streams throttled analog jog commands and a final zero on centering', () => {
    gamepads = [makeGamepad([0, -1])];
    renderActive();

    // First poll sends the deflection immediately.
    expect(sendMappingJogMock).toHaveBeenCalledTimes(1);
    expect(sendMappingJogMock).toHaveBeenLastCalledWith('R1', {
      linear: 0.3,
      angular: 0,
    });

    // Inside the throttle window nothing is sent.
    now = 50;
    act(runFrame);
    expect(sendMappingJogMock).toHaveBeenCalledTimes(1);

    // After 100 ms the held deflection is resent as a watchdog keep-alive.
    now = 100;
    act(runFrame);
    expect(sendMappingJogMock).toHaveBeenCalledTimes(2);

    // Centering the stick produces exactly one final zero command.
    gamepads = [makeGamepad([0, 0])];
    now = 150;
    act(runFrame);
    expect(sendMappingJogMock).toHaveBeenCalledTimes(2);
    now = 200;
    act(runFrame);
    expect(sendMappingJogMock).toHaveBeenCalledTimes(3);
    expect(sendMappingJogMock).toHaveBeenLastCalledWith('R1', {
      linear: 0,
      angular: 0,
    });
    now = 300;
    act(runFrame);
    expect(sendMappingJogMock).toHaveBeenCalledTimes(3);
  });

  it('reports the current output velocity', () => {
    gamepads = [makeGamepad([0, -0.55])];
    const { result } = renderActive();
    expect(result.current.output.linear).toBeCloseTo(0.15, 10);
    expect(result.current.output.angular).toBe(0);
  });

  it('sends a final zero when the switch is turned off', () => {
    gamepads = [makeGamepad([0, -1])];
    const { rerender } = renderActive();
    expect(sendMappingJogMock).toHaveBeenCalledTimes(1);

    rerender({ active: false });
    expect(sendMappingJogMock).toHaveBeenCalledTimes(2);
    expect(sendMappingJogMock).toHaveBeenLastCalledWith('R1', {
      linear: 0,
      angular: 0,
    });
  });

  it('sends a final zero on unmount', () => {
    gamepads = [makeGamepad([0, -1])];
    const { unmount } = renderActive();
    unmount();
    expect(sendMappingJogMock).toHaveBeenLastCalledWith('R1', {
      linear: 0,
      angular: 0,
    });
  });

  it('sends a final zero when the gamepad disconnects', () => {
    gamepads = [makeGamepad([0, -1])];
    renderActive();
    expect(sendMappingJogMock).toHaveBeenCalledTimes(1);

    gamepads = [null];
    now = 16;
    act(runFrame);
    expect(sendMappingJogMock).toHaveBeenCalledTimes(2);
    expect(sendMappingJogMock).toHaveBeenLastCalledWith('R1', {
      linear: 0,
      angular: 0,
    });
  });

  it('sends nothing while inactive', () => {
    gamepads = [makeGamepad([0, -1])];
    renderActive(false);
    act(runFrame);
    expect(sendMappingJogMock).not.toHaveBeenCalled();
  });

  it('fires onMark once per A-button rising edge', () => {
    const onMark = vi.fn();
    gamepads = [makeGamepad([0, 0], true)];
    renderActive(true, onMark);
    expect(onMark).toHaveBeenCalledTimes(1);

    // Holding the button does not retrigger.
    now = 16;
    act(runFrame);
    now = 32;
    act(runFrame);
    expect(onMark).toHaveBeenCalledTimes(1);

    // Releasing and pressing again fires a second rising edge.
    gamepads = [makeGamepad([0, 0], false)];
    now = 48;
    act(runFrame);
    gamepads = [makeGamepad([0, 0], true)];
    now = 64;
    act(runFrame);
    expect(onMark).toHaveBeenCalledTimes(2);
  });
});
