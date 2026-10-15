import { useEffect, useRef, useState } from 'react';

import { sendMappingJog } from '@/services/libraryRobot';
import {
  firstConnectedGamepad,
  gamepadVelocity,
  isZeroVelocity,
  type JogVelocity,
  type SentJog,
  shouldSendGamepadJog,
  ZERO_VELOCITY,
} from './gamepad';

export type MappingGamepadOptions = {
  robotId?: string;
  /** Polling runs only while active (mapping mode on and the switch enabled). */
  active: boolean;
  /** Fired once on every rising edge of the A button (standard buttons[0]). */
  onMark?: () => void;
};

/**
 * Poll the first connected gamepad with requestAnimationFrame and stream the
 * left stick as analog mapping/jog commands (throttled to 10 Hz). A final
 * zero command is guaranteed when the stick centers, the gamepad
 * disconnects, or the hook tears down (switch off, mapping ends, unmount).
 */
export function useMappingGamepad({
  robotId,
  active,
  onMark,
}: MappingGamepadOptions) {
  const [output, setOutput] = useState<JogVelocity>(ZERO_VELOCITY);
  const onMarkRef = useRef(onMark);
  onMarkRef.current = onMark;

  useEffect(() => {
    if (!robotId || !active || !firstConnectedGamepadSupported()) return;

    let animationFrame = 0;
    let lastSent: SentJog | undefined;
    let markPressed = false;

    const send = (velocity: JogVelocity) => {
      lastSent = { velocity, at: performance.now() };
      // Quiet on purpose: the on-vehicle watchdog stops the robot anyway.
      void sendMappingJog(robotId, velocity).catch(() => undefined);
    };

    const stop = () => {
      if (lastSent && !isZeroVelocity(lastSent.velocity)) send(ZERO_VELOCITY);
    };

    const poll = () => {
      const gamepad = firstConnectedGamepad();
      if (!gamepad) {
        markPressed = false;
        setOutput((previous) =>
          isZeroVelocity(previous) ? previous : ZERO_VELOCITY,
        );
        stop();
      } else {
        const velocity = gamepadVelocity(gamepad);
        setOutput((previous) =>
          previous.linear === velocity.linear &&
          previous.angular === velocity.angular
            ? previous
            : velocity,
        );
        if (shouldSendGamepadJog(lastSent, velocity, performance.now()))
          send(velocity);
        const pressed = Boolean(gamepad.buttons[0]?.pressed);
        if (pressed && !markPressed) onMarkRef.current?.();
        markPressed = pressed;
      }
      animationFrame = window.requestAnimationFrame(poll);
    };

    poll();
    return () => {
      window.cancelAnimationFrame(animationFrame);
      stop();
    };
  }, [robotId, active]);

  return { output };
}

function firstConnectedGamepadSupported() {
  return (
    typeof navigator !== 'undefined' &&
    typeof navigator.getGamepads === 'function'
  );
}
