import { freezeElapsed } from './elapsed-timer.ts';

export type RecorderState = 'ready' | 'starting' | 'recording' | 'paused' | 'stopping' | 'failed';
export type CaptureCommand = 'start' | 'pause' | 'resume' | 'stop';
export type CaptureStatus = {
  state: RecorderState;
  session_id?: string | null;
  next_cursor: number;
  /** Accumulated recording time, excluding pauses, sampled by the helper. */
  elapsed_ms?: number;
};

export type CaptureState = {
  state: RecorderState;
  sessionId: string | null;
  cursor: number;
  completedMs: number;
  runningSinceMs: number | null;
  generation: number;
  pending: CaptureCommand | null;
};

export const INITIAL_CAPTURE: CaptureState = {
  state: 'ready', sessionId: null, cursor: 0, completedMs: 0,
  runningSinceMs: null, generation: 0, pending: null,
};

export type CaptureAction =
  | { type: 'command'; command: CaptureCommand; nowMs: number }
  | { type: 'status'; status: CaptureStatus; generation: number; source: 'poll' | 'command'; nowMs: number }
  | { type: 'failure'; generation: number; nowMs: number };

/** One state transition policy for browser commands and external service changes. */
export function reduceCapture(current: CaptureState, action: CaptureAction): CaptureState {
  if (action.type === 'command') {
    const starting = action.command === 'start';
    const state = { start: 'starting', pause: 'paused', resume: 'recording', stop: 'stopping' } as const;
    return {
      ...current,
      state: state[action.command],
      sessionId: starting ? null : current.sessionId,
      cursor: starting ? 0 : current.cursor,
      completedMs: starting ? 0 : freezeElapsed(current.completedMs, current.runningSinceMs, action.nowMs),
      runningSinceMs: action.command === 'resume' ? action.nowMs : null,
      generation: current.generation + 1,
      pending: action.command,
    };
  }
  if (action.generation !== current.generation) return current;
  if (action.type === 'failure') {
    return {
      ...current, state: 'failed', pending: null,
      completedMs: freezeElapsed(current.completedMs, current.runningSinceMs, action.nowMs),
      runningSinceMs: null,
    };
  }
  // A poll may have sampled pre-command state. Keep controls locked until the
  // current command completes; older start/stop replies never revive a session.
  if (action.source === 'poll' && current.pending) return current;
  const sessionId = action.status.session_id ?? null;
  const replaced = sessionId !== current.sessionId;
  const elapsed = action.status.elapsed_ms;
  const completedMs = typeof elapsed === 'number' && Number.isFinite(elapsed) && elapsed >= 0
    ? elapsed
    : replaced ? 0 : freezeElapsed(current.completedMs, current.runningSinceMs, action.nowMs);
  return {
    state: action.status.state,
    sessionId,
    cursor: action.status.next_cursor,
    completedMs,
    runningSinceMs: action.status.state === 'recording' ? action.nowMs : null,
    // Session replacement also invalidates asynchronous reads for the old one.
    generation: current.generation + (replaced ? 1 : 0),
    pending: null,
  };
}
