import assert from 'node:assert/strict';
import test from 'node:test';

import { INITIAL_CAPTURE, reduceCapture, type CaptureState, type CaptureStatus } from '../app/capture-state.ts';
import { elapsedSeconds } from '../app/elapsed-timer.ts';

function status(current: CaptureState, changes: Partial<CaptureStatus>, nowMs = 1000): CaptureState {
  return reduceCapture(current, {
    type: 'status', source: 'poll', generation: current.generation, nowMs,
    status: { state: 'recording', session_id: 'external', next_cursor: 3, ...changes },
  });
}

test('an idle browser adopts an externally started session and its elapsed time', () => {
  const current = status(INITIAL_CAPTURE, { elapsed_ms: 65000 });
  assert.equal(current.state, 'recording');
  assert.equal(current.sessionId, 'external');
  assert.equal(current.cursor, 3);
  assert.equal(elapsedSeconds(current.completedMs, current.runningSinceMs, 2500), 66);
});

test('external pause, resume, stopping, and stop reconcile controls and freeze elapsed time', () => {
  let current = status(INITIAL_CAPTURE, { elapsed_ms: 12000 });
  current = status(current, { state: 'paused', elapsed_ms: 13500 }, 2500);
  assert.equal(current.state, 'paused');
  assert.equal(elapsedSeconds(current.completedMs, current.runningSinceMs, 9500), 13);
  current = status(current, { state: 'recording', elapsed_ms: 13500 }, 10000);
  assert.equal(elapsedSeconds(current.completedMs, current.runningSinceMs, 12500), 16);
  current = status(current, { state: 'stopping', elapsed_ms: 16000 }, 12500);
  assert.equal(current.runningSinceMs, null);
  current = status(current, { state: 'ready', elapsed_ms: 16000 }, 15000);
  assert.equal(current.state, 'ready');
  assert.equal(elapsedSeconds(current.completedMs, current.runningSinceMs, 20000), 16);
});

test('session replacement resets cursors and elapsed time even when the new cursor is larger', () => {
  let current = status(INITIAL_CAPTURE, { next_cursor: 20, elapsed_ms: 120000 });
  const generation = current.generation;
  current = status(current, { session_id: 'replacement', next_cursor: 40, elapsed_ms: 3000 });
  assert.equal(current.sessionId, 'replacement');
  assert.equal(current.cursor, 40);
  assert.equal(current.completedMs, 3000);
  assert.equal(current.generation, generation + 1);
});

test('older helpers without elapsed time use the local monotonic clock and pause correctly', () => {
  let current = status(INITIAL_CAPTURE, {});
  current = status(current, { state: 'paused' }, 3600);
  assert.equal(current.completedMs, 2600);
  current = status(current, { state: 'recording' }, 10000);
  current = status(current, { state: 'ready' }, 12000);
  assert.equal(current.completedMs, 4600);
  current = status(current, { session_id: 'replacement' }, 13000);
  assert.equal(current.completedMs, 0);
});

test('a stop during startup rejects late start responses and does not unlock on an early poll', () => {
  const starting = reduceCapture(INITIAL_CAPTURE, { type: 'command', command: 'start', nowMs: 0 });
  const stopping = reduceCapture(starting, { type: 'command', command: 'stop', nowMs: 100 });
  const lateStart = reduceCapture(stopping, {
    type: 'status', source: 'command', generation: starting.generation, nowMs: 200,
    status: { state: 'recording', session_id: 'cancelled', next_cursor: 4 },
  });
  assert.equal(lateStart, stopping);
  assert.equal(status(stopping, { state: 'ready' }), stopping);
  const stopped = reduceCapture(stopping, {
    type: 'status', source: 'command', generation: stopping.generation, nowMs: 300,
    status: { state: 'ready', session_id: null, next_cursor: 0 },
  });
  assert.equal(stopped.state, 'ready');
  assert.equal(stopped.pending, null);
});

test('pause and resume reject older polls and adopt the command response state', () => {
  const recording = status(INITIAL_CAPTURE, { elapsed_ms: 1000 });
  const paused = reduceCapture(recording, { type: 'command', command: 'pause', nowMs: 2000 });
  const stale = reduceCapture(paused, {
    type: 'status', source: 'poll', generation: recording.generation, nowMs: 2100,
    status: { state: 'recording', session_id: 'external', next_cursor: 4 },
  });
  assert.equal(stale, paused);
  const resumedElsewhere = reduceCapture(paused, {
    type: 'status', source: 'command', generation: paused.generation, nowMs: 2200,
    status: { state: 'recording', session_id: 'external', next_cursor: 4, elapsed_ms: 2000 },
  });
  assert.equal(resumedElsewhere.state, 'recording');
  assert.equal(resumedElsewhere.pending, null);
});

test('service failure freezes timing and stale failures cannot replace a new session', () => {
  const recording = status(INITIAL_CAPTURE, { elapsed_ms: 1000 });
  const failed = reduceCapture(recording, { type: 'failure', generation: recording.generation, nowMs: 2500 });
  assert.equal(failed.state, 'failed');
  assert.equal(failed.completedMs, 2500);
  const replacement = status(failed, { session_id: 'new' });
  assert.equal(reduceCapture(replacement, { type: 'failure', generation: failed.generation, nowMs: 3000 }), replacement);
});
