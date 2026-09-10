'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { applyFeedEvents, type Caption, type FeedEvent } from './caption-feed';
import { elapsedSeconds, freezeElapsed } from './elapsed-timer';

export type { Caption } from './caption-feed';
export type RecorderState = 'ready' | 'starting' | 'recording' | 'paused' | 'stopping' | 'failed';
export type CaptureMode = 'microphone' | 'system' | 'applications' | 'microphone_system' | 'microphone_applications';
export type CaptionLanguage = 'auto' | 'en' | 'zh';
export type CaptureApplication = { bundleId: string; name: string };
export type InputDevice = { uid: string; name: string; isDefault: boolean };
export type LastSession = { sessionId: string; directory: string; parts: string[] };
export type LocalModel = {
  id: string;
  displayName: string;
  profile: string;
  description: string;
  sizeBytes: number;
  active: boolean;
};

type LaunchConfig = { origin: string; token: string; retainAudio: boolean };
type NativeStatus = {
  state: 'ready' | 'recording' | 'paused' | 'stopping' | 'failed';
  session_id?: string | null;
  events: FeedEvent[];
  next_cursor: number;
  exit_code: number | null;
  error: string | null;
  /** The capture helper's latest status line, `code: message`, while it runs. */
  notice?: string | null;
  retention?: { retaining: boolean; retained_bytes: number; stopped_reason: string | null };
  last_session?: { session_id: string; directory: string; transcript_parts: string[] } | null;
  translation?: { requested: boolean; available: boolean; error: string | null };
  live_diarization?: LiveDiarization;
};

export type LiveDiarization = {
  available: boolean;
  requested: boolean;
  state: 'disabled' | 'starting' | 'ready' | 'running' | 'failed' | 'stopped';
  error: string | null;
};

export type DiarizationState =
  | 'disabled' | 'waiting_for_stop' | 'processing' | 'ready' | 'failed' | 'stale';
export type SpeakerPiece = {
  textStart: number; textEnd: number; startMs: number; endMs: number; speakerId: string | null;
};
export type SpeakerSegment = {
  startMs: number; endMs: number; speakerId: string | null; overlapDetected: boolean;
  /** Same-speaker character ranges of the caption, when it was split. */
  pieces: SpeakerPiece[];
};
export type Diarization = {
  state: DiarizationState;
  speakerCount: number;
  labeledSegments: number;
  totalSegments: number;
  segments: SpeakerSegment[];
  /** Names the user gave, or the registry recognized, per anonymous id. */
  names: Record<string, string>;
};

type DiarizationStatus = {
  state: DiarizationState;
  speaker_count: number;
  labeled_segments: number;
  total_segments: number;
  speakers?: Record<string, { name?: string | null; registry_id?: string | null }>;
  segments?: Array<{
    start_ms: number; end_ms: number; speaker_id: string | null; overlap_detected: boolean;
    pieces?: Array<{
      text_start: number; text_end: number; start_ms: number; end_ms: number; speaker_id: string | null;
    }>;
  }>;
};

const IDLE_DIARIZATION: Diarization = {
  state: 'disabled', speakerCount: 0, labeledSegments: 0, totalSegments: 0, segments: [], names: {},
};

// Diarization runs for minutes, not milliseconds, so it is polled far more
// slowly than the caption feed. Nothing waits on it.
const DIARIZATION_POLL_MS = 3000;

/** Map a capture mode to the source set the control plane understands. */
function captureSources(mode: CaptureMode): string[] {
  if (mode === 'microphone') return ['microphone'];
  if (mode.startsWith('microphone_')) return ['microphone', 'system'];
  return ['system'];
}

function parseDiarization(status: DiarizationStatus): Diarization {
  const names: Record<string, string> = {};
  for (const [speakerId, info] of Object.entries(status.speakers ?? {})) {
    if (info?.name) names[speakerId] = info.name;
  }
  return {
    state: status.state,
    speakerCount: status.speaker_count ?? 0,
    labeledSegments: status.labeled_segments ?? 0,
    totalSegments: status.total_segments ?? 0,
    names,
    segments: (status.segments ?? []).map((item) => ({
      startMs: item.start_ms, endMs: item.end_ms,
      speakerId: item.speaker_id, overlapDetected: item.overlap_detected,
      pieces: (item.pieces ?? []).map((piece) => ({
        textStart: piece.text_start, textEnd: piece.text_end,
        startMs: piece.start_ms, endMs: piece.end_ms, speakerId: piece.speaker_id,
      })),
    })),
  };
}

export function useVoxInkRecorder() {
  const [state, setState] = useState<RecorderState>('ready');
  const [seconds, setSeconds] = useState(0);
  const [captions, setCaptions] = useState<Caption[]>([]);
  const [launch, setLaunch] = useState<LaunchConfig | null>(null);
  const [retainAudio, setRetainAudioState] = useState(true);
  const [identifySpeakers, setIdentifySpeakersState] = useState(false);
  const [liveSpeakers, setLiveSpeakersState] = useState(false);
  const [liveDiarization, setLiveDiarization] = useState<LiveDiarization>({
    available: false, requested: false, state: 'disabled', error: null,
  });
  const [diarization, setDiarization] = useState<Diarization>(IDLE_DIARIZATION);
  const [diarizationAvailable, setDiarizationAvailable] = useState(false);
  const diarizedSessionId = useRef<string | null>(null);
  const [message, setMessage] = useState('Open through voxink-dev to connect');
  const [mode, setModeState] = useState<CaptureMode>('microphone');
  const [nativeAvailable, setNativeAvailable] = useState(false);
  // The helper's backend may be microphone-only (the portable Python one).
  const [systemAvailable, setSystemAvailable] = useState(false);
  const [partialCaptions, setPartialCaptions] = useState(false);
  const [translationAvailable, setTranslationAvailable] = useState(false);
  const [translate, setTranslateState] = useState(false);
  const [language, setLanguageState] = useState<CaptionLanguage>('auto');
  const [devices, setDevices] = useState<InputDevice[]>([]);
  const [selectedDevice, setSelectedDeviceState] = useState<string>('');
  const [applications, setApplications] = useState<CaptureApplication[]>([]);
  const [selectedApplications, setSelectedApplications] = useState<string[]>([]);
  const [models, setModels] = useState<LocalModel[]>([]);
  const [activeModel, setActiveModel] = useState('Qwen3-ASR-1.7B');
  const [modelSwitching, setModelSwitching] = useState(false);
  const [lastSession, setLastSession] = useState<LastSession | null>(null);
  const [active, setActive] = useState(false);
  // Network replies from an earlier session/start must never reopen capture
  // after Stop, restore old captions, or unlock a newer loading session.
  const sessionGeneration = useRef(0);
  const startPending = useRef(false);
  const stopPending = useRef(false);
  const nativeCursor = useRef(0);
  const retentionNotice = useRef<string | null>(null);
  const translationNotice = useRef<string | null>(null);
  const helperNotice = useRef<string | null>(null);
  const completedElapsedMs = useRef(0);
  const runningSinceMs = useRef<number | null>(null);

  const refreshElapsed = useCallback(() => {
    setSeconds(elapsedSeconds(completedElapsedMs.current, runningSinceMs.current, performance.now()));
  }, []);

  const resetElapsed = useCallback(() => {
    completedElapsedMs.current = 0;
    runningSinceMs.current = null;
    setSeconds(0);
  }, []);

  const beginElapsed = useCallback(() => {
    if (runningSinceMs.current === null) runningSinceMs.current = performance.now();
    refreshElapsed();
  }, [refreshElapsed]);

  const pauseElapsed = useCallback(() => {
    const nowMs = performance.now();
    completedElapsedMs.current = freezeElapsed(
      completedElapsedMs.current, runningSinceMs.current, nowMs,
    );
    runningSinceMs.current = null;
    setSeconds(elapsedSeconds(completedElapsedMs.current, null, nowMs));
  }, []);

  const authorizedFetch = useCallback(async (path: string, init: RequestInit = {}) => {
    if (!launch) throw new Error('The local helper is not connected');
    const response = await fetch(path, {
      ...init,
      cache: 'no-store',
      headers: { ...init.headers, Authorization: `Bearer ${launch.token}` },
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({})) as { detail?: string };
      throw new Error(payload.detail || `Local helper request failed (${response.status})`);
    }
    return response;
  }, [launch]);

  useEffect(() => {
    const values = new URLSearchParams(window.location.hash.slice(1));
    const bootstrap = values.get('bootstrap');
    const retainAudio = values.get('retain_audio') === '1';
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
    if (!bootstrap) return;
    void fetch('/v1/ui/bootstrap', {
      method: 'POST', headers: { 'X-VoxInk-Bootstrap': bootstrap }, cache: 'no-store',
    }).then(async (response) => {
      if (!response.ok) throw new Error('The UI launch link expired. Run voxink-dev again.');
      const payload = (await response.json()) as { token?: unknown };
      if (typeof payload.token !== 'string' || !payload.token) throw new Error('The local helper returned no launch credential.');
      setLaunch({ origin: window.location.origin, token: payload.token, retainAudio });
      setRetainAudioState(retainAudio);
      setMessage(retainAudio ? 'Local helper ready — saving audio' : 'Authenticated local helper ready');
    }).catch((error) => {
      setState('failed');
      setMessage(error instanceof Error ? error.message : 'Cannot authenticate the local UI');
    });
  }, []);

  const refreshApplications = useCallback(async () => {
    if (!launch) return;
    try {
      const response = await authorizedFetch('/v1/ui/capture/applications');
      const catalog = await response.json() as { applications?: Array<{ bundle_id: string; name: string }> };
      setApplications((catalog.applications || []).map((item) => ({ bundleId: item.bundle_id, name: item.name })));
    } catch {
      setApplications([]);
    }
  }, [authorizedFetch, launch]);

  const refreshDevices = useCallback(async () => {
    if (!launch) return;
    try {
      const response = await authorizedFetch('/v1/devices');
      const catalog = await response.json() as { devices?: Array<{ uid?: string | null; name: string; default?: boolean }> };
      setDevices((catalog.devices || [])
        .filter((item): item is { uid: string; name: string; default?: boolean } => typeof item.uid === 'string' && item.uid.length > 0)
        .map((item) => ({ uid: item.uid, name: item.name, isDefault: Boolean(item.default) })));
    } catch {
      setDevices([]);
    }
  }, [authorizedFetch, launch]);

  useEffect(() => {
    if (!launch) return;
    void authorizedFetch('/v1/ui/capture/capabilities')
      .then((response) => response.json())
      .then((payload) => {
        const capabilities = payload as {
          native_capture_available?: boolean; modes?: string[]; partial_captions?: boolean;
          translation_available?: boolean; live_diarization?: LiveDiarization;
        };
        setNativeAvailable(Boolean(capabilities.native_capture_available));
        setSystemAvailable(Array.isArray(capabilities.modes) && capabilities.modes.includes('system'));
        setPartialCaptions(Boolean(capabilities.partial_captions));
        setTranslationAvailable(Boolean(capabilities.translation_available));
        if (capabilities.live_diarization) setLiveDiarization(capabilities.live_diarization);
        if (capabilities.native_capture_available) void refreshDevices();
      })
      .catch(() => {
        // A stale token or a restarted helper would otherwise leave every native
        // mode disabled with nothing said about why.
        setNativeAvailable(false);
        setSystemAvailable(false);
        setMessage('Cannot read local capture capabilities; native modes are unavailable');
      });
  }, [authorizedFetch, launch, refreshDevices]);

  useEffect(() => {
    if (!launch) return;
    void authorizedFetch('/v1/models')
      .then((response) => response.json())
      .then((payload) => {
        const catalog = payload as {
          active_model?: string;
          models?: Array<{
            id: string;
            display_name: string;
            profile: string;
            description: string;
            size_bytes: number;
            active: boolean;
          }>;
        };
        setActiveModel(catalog.active_model || 'Qwen3-ASR-1.7B');
        setModels((catalog.models || []).map((item) => ({
          id: item.id,
          displayName: item.display_name,
          profile: item.profile,
          description: item.description,
          sizeBytes: item.size_bytes,
          active: item.active,
        })));
      })
      .catch((error) => {
        setMessage(error instanceof Error ? error.message : 'Cannot read installed local models');
      });
  }, [authorizedFetch, launch]);

  useEffect(() => {
    if (state !== 'recording') return;
    const timer = window.setInterval(refreshElapsed, 1000);
    document.addEventListener('visibilitychange', refreshElapsed);
    refreshElapsed();
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', refreshElapsed);
    };
  }, [refreshElapsed, state]);

  const elapsed = useMemo(() => {
    const minutes = Math.floor(seconds / 60).toString().padStart(2, '0');
    const remainder = (seconds % 60).toString().padStart(2, '0');
    return `${minutes}:${remainder}`;
  }, [seconds]);

  const fail = useCallback((error: unknown) => {
    pauseElapsed();
    setActive(false); setState('failed');
    setMessage(error instanceof Error ? error.message : 'The local session stopped unexpectedly');
  }, [pauseElapsed]);

  const applyNativeStatus = useCallback((status: NativeStatus) => {
    if (status.events.length) setCaptions((current) => applyFeedEvents(current, status.events));
    if (status.live_diarization) setLiveDiarization(status.live_diarization);
    nativeCursor.current = status.next_cursor;
    if (status.last_session) {
      setLastSession({
        sessionId: status.last_session.session_id,
        directory: status.last_session.directory,
        parts: status.last_session.transcript_parts,
      });
    }
    // Retention stopping does not stop the session: the transcript is still
    // being written, so this is a notice rather than a failure. Reported once,
    // because the status poll keeps returning the same reason afterwards.
    const stopped = status.retention?.stopped_reason ?? null;
    if (stopped && stopped !== retentionNotice.current) {
      retentionNotice.current = stopped;
      setMessage(
        stopped === 'low_disk'
          ? 'Low disk space: audio is no longer being saved. The transcript is still recording.'
          : 'Audio retention limit reached. The transcript is still recording.',
      );
    }
    const translationError = status.translation?.error ?? null;
    if (translationError && translationError !== translationNotice.current) {
      translationNotice.current = translationError;
      setMessage(`Translation is failing: ${translationError}. Captions continue.`);
    }
    // A reconnect or an abandoned stream is the helper's to report; the poll
    // repeats the same line until the next one, so each is shown once.
    const notice = status.notice ?? null;
    if (notice && notice !== helperNotice.current) {
      helperNotice.current = notice;
      setMessage(notice.slice(notice.indexOf(': ') + 2) || notice);
    }
    // Keep controls locked until the Stop request has completed its cleanup.
    // A poll can observe the helper exit before that request returns.
    if (stopPending.current) return;
    if (status.state === 'failed') {
      pauseElapsed();
      setActive(false); setState('failed');
      setMessage(status.error || `Capture stopped unexpectedly${status.exit_code === null ? '' : ` (exit ${status.exit_code})`}`);
    } else if (status.state === 'ready') {
      pauseElapsed();
      setActive(false); setState('ready'); setMessage('Transcript finalized and saved locally');
    }
  }, [pauseElapsed]);

  useEffect(() => {
    if (!active || !launch) return;
    const generation = sessionGeneration.current;
    let cancelled = false;
    let inFlight = false;
    const poll = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const response = await authorizedFetch(`/v1/ui/capture/status?after=${nativeCursor.current}`);
        const status = await response.json() as NativeStatus;
        if (!cancelled && generation === sessionGeneration.current) applyNativeStatus(status);
      } catch (error) {
        if (!cancelled && generation === sessionGeneration.current) {
          setMessage(error instanceof Error ? error.message : 'Cannot read native capture status');
        }
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(() => void poll(), 500);
    void poll();
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [active, applyNativeStatus, authorizedFetch, launch]);

  // Ask once whether diarization can run here. The weights are a separate
  // licence-gated download, so "supported" and "installed" are different
  // questions and the control should not offer what cannot happen.
  useEffect(() => {
    if (!launch) return;
    let cancelled = false;
    void (async () => {
      try {
        const response = await authorizedFetch('/v1/diarization');
        const capability = await response.json() as { available?: boolean };
        if (!cancelled) setDiarizationAvailable(Boolean(capability.available));
      } catch {
        if (!cancelled) setDiarizationAvailable(false);
      }
    })();
    return () => { cancelled = true; };
  }, [authorizedFetch, launch]);

  const refreshDiarization = useCallback(async () => {
    const sessionId = diarizedSessionId.current;
    const generation = sessionGeneration.current;
    if (!launch || !sessionId) return;
    const response = await authorizedFetch(`/v1/sessions/${sessionId}/diarization`);
    const status = await response.json() as DiarizationStatus;
    if (generation === sessionGeneration.current && sessionId === diarizedSessionId.current) {
      setDiarization(parseDiarization(status));
    }
  }, [authorizedFetch, launch]);

  // Poll only after Stop, and only until the job settles. Nothing here blocks
  // the transcript, which is already complete and on screen.
  useEffect(() => {
    const sessionId = diarizedSessionId.current;
    if (!launch || !sessionId) return;
    if (state !== 'ready' && state !== 'failed') return;
    if (diarization.state === 'ready' || diarization.state === 'failed') return;
    let cancelled = false;
    const poll = async () => {
      try {
        if (!cancelled) await refreshDiarization();
      } catch {
        // A failed poll is not a failed job; the next tick tries again.
      }
    };
    const timer = window.setInterval(() => void poll(), DIARIZATION_POLL_MS);
    void poll();
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [diarization.state, launch, refreshDiarization, state]);

  const start = useCallback(async () => {
    if (modelSwitching) { setMessage('Wait for the local model to finish changing'); return; }
    if (startPending.current || stopPending.current) return;
    const generation = ++sessionGeneration.current;
    if (!launch) { setMessage('Preview mode — launch with ./scripts/voxink-dev for live audio'); resetElapsed(); beginElapsed(); setCaptions([]); setState('recording'); return; }
    startPending.current = true;
    setActive(false);
    setState('starting'); resetElapsed(); setCaptions([]); nativeCursor.current = 0;
    diarizedSessionId.current = null;
    setDiarization(IDLE_DIARIZATION);
    translationNotice.current = null;
    // Speakers are only findable in audio that was kept, so the two travel
    // together. The control is disabled without retention, and this makes the
    // request agree with what the UI already shows.
    const wantsSpeakers = identifySpeakers && retainAudio && diarizationAvailable;
    const wantsLiveSpeakers = liveSpeakers && mode.startsWith('microphone') && liveDiarization.available;
    setLiveDiarization((current) => ({
      ...current, requested: wantsLiveSpeakers,
      state: wantsLiveSpeakers ? 'starting' : 'disabled', error: null,
    }));
    try {
      if ((mode === 'applications' || mode === 'microphone_applications') && selectedApplications.length === 0) throw new Error('Choose at least one running application first');
      setMessage('Starting macOS audio capture…');
      const response = await authorizedFetch('/v1/ui/capture/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sources: captureSources(mode),
          applications: mode.endsWith('applications') ? selectedApplications : [],
          retain_audio: retainAudio,
          diarize: wantsSpeakers,
          live_diarization: wantsLiveSpeakers,
          microphone_device: selectedDevice || null,
          language: language === 'auto' ? null : language,
          translate: translate && translationAvailable,
        }),
      });
      const status = await response.json() as NativeStatus;
      if (generation !== sessionGeneration.current) return;
      if (status.events.length) setCaptions((current) => applyFeedEvents(current, status.events));
      if (status.live_diarization) setLiveDiarization(status.live_diarization);
      nativeCursor.current = status.next_cursor;
      diarizedSessionId.current = wantsSpeakers ? status.session_id ?? null : null;
      setDiarization(wantsSpeakers
        ? { ...IDLE_DIARIZATION, state: 'waiting_for_stop' }
        : IDLE_DIARIZATION);
      beginElapsed();
      setActive(true); setState('recording');
      setMessage(partialCaptions ? 'Listening locally — words appear as you speak' : 'Listening locally with macOS capture');
    } catch (error) {
      if (generation !== sessionGeneration.current) return;
      if (wantsLiveSpeakers) setLiveDiarization((current) => ({
        ...current, state: 'failed', error: error instanceof Error ? error.message : 'Cannot start live speakers',
      }));
      fail(error);
    } finally {
      if (generation === sessionGeneration.current) startPending.current = false;
    }
  }, [authorizedFetch, beginElapsed, diarizationAvailable, fail, identifySpeakers, language, launch, liveDiarization.available, liveSpeakers, mode, modelSwitching, partialCaptions, resetElapsed, retainAudio, selectedApplications, selectedDevice, translate, translationAvailable]);

  const control = useCallback(async (
    action: 'pause' | 'resume' | 'stop', next: RecorderState, note: string,
  ) => {
    if (stopPending.current) return;
    const cancellingStart = action === 'stop' && startPending.current;
    if (cancellingStart) {
      ++sessionGeneration.current;
      startPending.current = false;
    }
    const generation = sessionGeneration.current;
    if (action !== 'resume') pauseElapsed();
    else beginElapsed();
    if (!launch) { setState(action === 'stop' ? 'ready' : next); setMessage('Preview ready'); return; }
    if (action === 'stop') stopPending.current = true;
    setState(action === 'stop' ? 'stopping' : next);
    setMessage(cancellingStart ? 'Cancelling startup…' : action === 'stop' ? 'Finalizing the last utterance…' : note);
    try {
      const response = await authorizedFetch(`/v1/ui/capture/${action}`, { method: 'POST' });
      const status = await response.json() as NativeStatus;
      if (generation !== sessionGeneration.current) return;
      if (action === 'stop') stopPending.current = false;
      applyNativeStatus(cancellingStart ? { ...status, events: [], next_cursor: 0 } : status);
      if (cancellingStart && status.state === 'ready') setMessage('Startup cancelled — ready to record');
      if (action !== 'stop') setMessage(note);
    } catch (error) {
      if (generation === sessionGeneration.current) {
        if (action === 'stop') stopPending.current = false;
        fail(error);
      }
    }
  }, [applyNativeStatus, authorizedFetch, beginElapsed, fail, launch, pauseElapsed]);

  const pause = useCallback(() => control('pause', 'paused', 'Session paused'), [control]);
  const resume = useCallback(
    () => control('resume', 'recording', 'Listening locally with macOS capture'), [control],
  );
  const stop = useCallback(() => control('stop', 'stopping', ''), [control]);

  const mark = useCallback(async (label = '') => {
    if (state !== 'recording' && state !== 'paused') return;
    if (!launch) {
      setCaptions((current) => applyFeedEvents(current, [{
        type: 'mark', segment_id: `mark-${Date.now()}`, text: label, start_ms: seconds * 1000, end_ms: seconds * 1000,
      }]));
      return;
    }
    try {
      const response = await authorizedFetch('/v1/ui/capture/mark', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label }),
      });
      const marked = await response.json() as { at_ms: number };
      const total = Math.floor(marked.at_ms / 1000);
      setMessage(`Marked at ${Math.floor(total / 60).toString().padStart(2, '0')}:${(total % 60).toString().padStart(2, '0')}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Cannot mark the transcript');
    }
  }, [authorizedFetch, launch, seconds, state]);

  const revealFolder = useCallback(async () => {
    if (!launch) { setMessage('Preview mode — nothing has been saved'); return; }
    try {
      await authorizedFetch('/v1/ui/actions/reveal', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(lastSession ? { session_id: lastSession.sessionId } : {}),
      });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Cannot open the transcript folder');
    }
  }, [authorizedFetch, lastSession, launch]);

  const nameSpeaker = useCallback(async (speakerId: string, name: string, remember: boolean) => {
    const sessionId = diarizedSessionId.current;
    if (!launch || !sessionId) return;
    try {
      await authorizedFetch(`/v1/sessions/${sessionId}/speakers/${encodeURIComponent(speakerId)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, remember }),
      });
      await refreshDiarization();
      setMessage(remember ? `${name} will be recognized in later sessions` : `Named ${name} for this session`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Cannot name the speaker');
    }
  }, [authorizedFetch, launch, refreshDiarization]);

  const setMode = useCallback((value: CaptureMode) => {
    if (state !== 'ready' && state !== 'failed') return;
    setModeState(value);
    if (!value.startsWith('microphone')) setLiveSpeakersState(false);
    // Listing applications starts the capture helper, which calls
    // SCShareableContent and prompts for Screen & System Audio Recording.
    // Asking on load would demand that permission from someone recording only
    // their microphone, and an unanswered prompt blocks the helper for the full
    // request timeout, so only ask once a mode that needs the list is chosen.
    if (value === 'applications' || value === 'microphone_applications') void refreshApplications();
  }, [state, refreshApplications]);
  const toggleApplication = useCallback((bundleId: string) => {
    setSelectedApplications((current) => current.includes(bundleId) ? current.filter((item) => item !== bundleId) : [...current, bundleId]);
  }, []);
  const setRetainAudio = useCallback((enabled: boolean) => {
    if (state !== 'ready' && state !== 'failed') return;
    setRetainAudioState(enabled);
    // Speaker identification reads the retained microphone track, so turning
    // audio off turns it off too rather than leaving a control switched on
    // that silently cannot run.
    if (!enabled) setIdentifySpeakersState(false);
    setMessage(enabled ? 'Audio will be saved locally with the transcript' : 'Audio saving is off for the next session');
  }, [state]);

  const setIdentifySpeakers = useCallback((enabled: boolean) => {
    if (state !== 'ready' && state !== 'failed') return;
    if (enabled && !retainAudio) return;
    setIdentifySpeakersState(enabled);
    setMessage(enabled
      ? 'Speakers will be identified after you stop recording'
      : 'Speaker identification is off for the next session');
  }, [retainAudio, state]);

  const setLiveSpeakers = useCallback((enabled: boolean) => {
    if (state !== 'ready' && state !== 'failed') return;
    if (enabled && (!launch || !nativeAvailable || !liveDiarization.available || !mode.startsWith('microphone'))) return;
    setLiveSpeakersState(enabled);
    setMessage(enabled
      ? 'Microphone captions will show provisional speaker labels during recording'
      : 'Live speaker labels are off for the next session');
  }, [launch, liveDiarization.available, mode, nativeAvailable, state]);

  const setTranslate = useCallback((enabled: boolean) => {
    if (state !== 'ready' && state !== 'failed') return;
    if (enabled && !translationAvailable) return;
    setTranslateState(enabled);
    setMessage(enabled ? 'Captions will be translated on this Mac' : 'Translation is off for the next session');
  }, [state, translationAvailable]);

  const setLanguage = useCallback((value: CaptionLanguage) => {
    if (state !== 'ready' && state !== 'failed') return;
    setLanguageState(value);
  }, [state]);

  const setSelectedDevice = useCallback((uid: string) => {
    if (state !== 'ready' && state !== 'failed') return;
    setSelectedDeviceState(uid);
  }, [state]);

  const selectModel = useCallback(async (modelId: string) => {
    if (!launch || modelId === activeModel || modelSwitching) return;
    if (state !== 'ready' && state !== 'failed') return;
    setModelSwitching(true);
    setMessage('Verifying and changing the local model…');
    try {
      const response = await authorizedFetch('/v1/models/active', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: modelId }),
      });
      const selected = await response.json() as { id: string };
      setActiveModel(selected.id);
      setModels((current) => current.map((item) => ({ ...item, active: item.id === selected.id })));
      setState('ready');
      setMessage('Local model selected — ready to record');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Cannot change the local model');
    } finally {
      setModelSwitching(false);
    }
  }, [activeModel, authorizedFetch, launch, modelSwitching, state]);

  return {
    state, elapsed, captions, message, isLive: Boolean(launch), mode, setMode,
    retainAudio, setRetainAudio,
    identifySpeakers, setIdentifySpeakers, diarizationAvailable, diarization, nameSpeaker,
    liveSpeakers, setLiveSpeakers, liveDiarization,
    nativeAvailable, systemAvailable, applications, selectedApplications, toggleApplication, refreshApplications,
    devices, selectedDevice, setSelectedDevice, refreshDevices,
    language, setLanguage, translate, setTranslate, translationAvailable, partialCaptions,
    models, activeModel, selectModel, modelSwitching,
    lastSession, revealFolder, mark,
    start, pause, resume, stop,
    // Other panels reach the helper through the same one-time launch credential.
    authorizedFetch,
  };
}
