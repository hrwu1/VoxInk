import assert from 'node:assert/strict';
import test from 'node:test';

import { elapsedSeconds, freezeElapsed } from '../app/elapsed-timer.ts';

test('derives elapsed time from the clock even when callbacks are throttled', () => {
  assert.equal(elapsedSeconds(0, 5_000, 3_605_000), 3_600);
});

test('freezes paused time and continues from the accumulated duration', () => {
  const pausedAtMs = freezeElapsed(0, 2_000, 62_000);
  assert.equal(elapsedSeconds(pausedAtMs, null, 3_600_000), 60);
  assert.equal(elapsedSeconds(pausedAtMs, 3_600_000, 3_630_000), 90);
});

