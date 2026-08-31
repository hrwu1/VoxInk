/** Return whole elapsed seconds from a completed total plus an optional live run. */
export function elapsedSeconds(
  completedMs: number,
  runningSinceMs: number | null,
  nowMs: number,
): number {
  const runningMs = runningSinceMs === null ? 0 : Math.max(0, nowMs - runningSinceMs);
  return Math.floor((completedMs + runningMs) / 1000);
}

/** Freeze a live run into the completed elapsed total. */
export function freezeElapsed(
  completedMs: number,
  runningSinceMs: number | null,
  nowMs: number,
): number {
  return runningSinceMs === null
    ? completedMs
    : completedMs + Math.max(0, nowMs - runningSinceMs);
}

