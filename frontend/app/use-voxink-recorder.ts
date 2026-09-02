'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { elapsedSeconds, freezeElapsed } from './elapsed-timer';

export type RecorderState = 'ready' | 'starting' | 'recording' | 'paused' | 'stopping' | 'failed';
export type CaptureMode = 'microphone' | 'system' | 'applications' | 'microphone_system' | 'microphone_applications';
export type Caption = { id: string; source: string; text: string; startMs: number; endMs: number; isFinal: boolean };
export type CaptureApplication = { bundleId: string; name: string };
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
  events: Array<{ segment_id: string; source: string; text: string; start_ms: number; end_ms: number; is_final: boolean }>;
  next_cursor: number;
  exit_code: number | null;
  error: string | null;
  retention?: { retaining: boolean; retained_bytes: number; stopped_reason: string | null };
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
};

type DiarizationStatus = {
  state: DiarizationState;
  speaker_count: number;
  labeled_segments: number;
  total_segments: number;
  segments?: Array<{
    start_ms: number; end_ms: number; speaker_id: string | null; overlap_detected: boolean;
    pieces?: Array<{
      text_start: number; text_end: number; start_ms: number; end_ms: number; speaker_id: string | null;
    }>;
  }>;
};

const IDLE_DIARIZATION: Diarization = {
  state: 'disabled', speakerCount: 0, labeledSegments: 0, totalSegments: 0, segments: [],
};

const MAX_VISIBLE_CAPTIONS = 250;
// Diarization runs for minutes, not milliseconds, so it is polled far more
// slowly than the caption feed. Nothing waits on it.
const DIARIZATION_POLL_MS = 3000;

/** Map a capture mode to the source set the control plane understands. */
function captureSources(mode: CaptureMode): string[] {
  if (mode === 'microphone') return ['microphone'];
  if (mode.startsWith('microphone_')) return ['microphone', 'system'];
  return ['system'];
}

export function useVoxInkRecorder() {
  const [state, setState] = useState<RecorderState>('ready');
  const [seconds, setSeconds] = useState(0);
  const [captions, setCaptions] = useState<Caption[]>([]);
  const [launch, setLaunch] = useState<LaunchConfig | null>(null);
  const [retainAudio, setRetainAudioState] = useState(true);
  const [identifySpeakers, setIdentifySpeakersState] = useState(false);
  const [diarization, setDiarization] = useState<Diarization>(IDLE_DIARIZATION);
  const [diarizationAvailable, setDiarizationAvailable] = useState(false);
  const diarizedSessionId = useRef<string | null>(null);
  const [message, setMessage] = useState('Open through voxink-dev to connect');
  const [mode, setModeState] = useState<CaptureMode>('microphone');
  const [nativeAvailable, setNativeAvailable] = useState(false);
  const [applications, setApplications] = useState<CaptureApplication[]>([]);
  const [selectedApplications, setSelectedApplications] = useState<string[]>([]);
  const [models, setModels] = useState<LocalModel[]>([]);
  const [activeModel, setActiveModel] = useState('Qwen3-ASR-1.7B');
  const [modelSwitching, setModelSwitching] = useState(false);
  const [active, setActive] = useState(false);
  const nativeCursor = useRef(0);
  const retentionNotice = useRef<string | null>(null);
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

  useEffect(() => {
    if (!launch) return;
    void authorizedFetch('/v1/ui/capture/capabilities')
      .then((response) => response.json())
      .then((payload) => {
        const capabilities = payload as { native_capture_available?: boolean };
        setNativeAvailable(Boolean(capabilities.native_capture_available));
      })
      .catch(() => {
        // A stale token or a restarted helper would otherwise leave every native
        // mode disabled with nothing said about why.
        setNativeAvailable(false);
        setMessage('Cannot read local capture capabilities; native modes are unavailable');
      });
  }, [authorizedFetch, launch]);

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

  const addCaption = useCallback((caption: Caption) => {
    setCaptions((current) => {
      const existing = current.findIndex((entry) => entry.id === caption.id);
      if (existing < 0) return [...current, caption].sort((a, b) => a.startMs - b.startMs).slice(-MAX_VISIBLE_CAPTIONS);
      const next = [...current];
      next[existing] = caption;
      return next;
    });
  }, []);

  const fail = useCallback((error: unknown) => {
    pauseElapsed();
    setActive(false); setState('failed');
    setMessage(error instanceof Error ? error.message : 'The local session stopped unexpectedly');
  }, [pauseElapsed]);

  const applyNativeStatus = useCallback((status: NativeStatus) => {
    status.events.forEach((item) => addCaption({
      id: item.segment_id, source: item.source, text: item.text,
      startMs: item.start_ms, endMs: item.end_ms, isFinal: item.is_final,
    }));
    nativeCursor.current = status.next_cursor;
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
    if (status.state === 'failed') {
      pauseElapsed();
      setActive(false); setState('failed');
      setMessage(status.error || `Capture stopped unexpectedly${status.exit_code === null ? '' : ` (exit ${status.exit_code})`}`);
    } else if (status.state === 'ready') {
      pauseElapsed();
      setActive(false); setState('ready'); setMessage('Transcript finalized and saved locally');
    }
  }, [addCaption, pauseElapsed]);

  useEffect(() => {
    if (!active || !launch) return;
    const poll = async () => {
      try {
        const response = await authorizedFetch(`/v1/ui/capture/status?after=${nativeCursor.current}`);
        applyNativeStatus(await response.json() as NativeStatus);
      } catch (error) {
        setMessage(error instanceof Error ? error.message : 'Cannot read native capture status');
      }
    };
    const timer = window.setInterval(() => void poll(), 500);
    void poll();
    return () => window.clearInterval(timer);
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
        const response = await authorizedFetch(`/v1/sessions/${sessionId}/diarization`);
        const status = await response.json() as DiarizationStatus;
        if (cancelled) return;
        setDiarization({
          state: status.state,
          speakerCount: status.speaker_count ?? 0,
          labeledSegments: status.labeled_segments ?? 0,
          totalSegments: status.total_segments ?? 0,
          segments: (status.segments ?? []).map((item) => ({
            startMs: item.start_ms, endMs: item.end_ms,
            speakerId: item.speaker_id, overlapDetected: item.overlap_detected,
            pieces: (item.pieces ?? []).map((piece) => ({
              textStart: piece.text_start, textEnd: piece.text_end,
              startMs: piece.start_ms, endMs: piece.end_ms, speakerId: piece.speaker_id,
            })),
          })),
        });
      } catch {
        // A failed poll is not a failed job; the next tick tries again.
      }
    };
    const timer = window.setInterval(() => void poll(), DIARIZATION_POLL_MS);
    void poll();
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [authorizedFetch, diarization.state, launch, state]);

  const start = useCallback(async () => {
    if (modelSwitching) { setMessage('Wait for the local model to finish changing'); return; }
    if (!launch) { setMessage('Preview mode — launch with ./scripts/voxink-dev for live audio'); resetElapsed(); beginElapsed(); setCaptions([]); setState('recording'); return; }
    setState('starting'); resetElapsed(); setCaptions([]); nativeCursor.current = 0;
    // Speakers are only findable in audio that was kept, so the two travel
    // together. The control is disabled without retention, and this makes the
    // request agree with what the UI already shows.
    const wantsSpeakers = identifySpeakers && retainAudio && diarizationAvailable;
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
        }),
      });
      const status = await response.json() as NativeStatus;
      nativeCursor.current = status.next_cursor;
      diarizedSessionId.current = wantsSpeakers ? status.session_id ?? null : null;
      setDiarization(wantsSpeakers
        ? { ...IDLE_DIARIZATION, state: 'waiting_for_stop' }
        : IDLE_DIARIZATION);
      beginElapsed();
      setActive(true); setState('recording'); setMessage('Listening locally with macOS capture');
    } catch (error) { fail(error); }
  }, [authorizedFetch, beginElapsed, diarizationAvailable, fail, identifySpeakers, launch, mode, modelSwitching, resetElapsed, retainAudio, selectedApplications]);

  const control = useCallback(async (
    action: 'pause' | 'resume' | 'stop', next: RecorderState, note: string,
  ) => {
    if (action !== 'resume') pauseElapsed();
    else beginElapsed();
    if (!launch) { setState(action === 'stop' ? 'ready' : next); setMessage('Preview ready'); return; }
    setState(action === 'stop' ? 'stopping' : next);
    setMessage(action === 'stop' ? 'Finalizing the last utterance…' : note);
    try {
      const response = await authorizedFetch(`/v1/ui/capture/${action}`, { method: 'POST' });
      applyNativeStatus(await response.json() as NativeStatus);
      if (action !== 'stop') setMessage(note);
    } catch (error) { fail(error); }
  }, [applyNativeStatus, authorizedFetch, beginElapsed, fail, launch, pauseElapsed]);

  const pause = useCallback(() => control('pause', 'paused', 'Session paused'), [control]);
  const resume = useCallback(
    () => control('resume', 'recording', 'Listening locally with macOS capture'), [control],
  );
  const stop = useCallback(() => control('stop', 'stopping', ''), [control]);

  const setMode = useCallback((value: CaptureMode) => {
    if (state !== 'ready' && state !== 'failed') return;
    setModeState(value);
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
    identifySpeakers, setIdentifySpeakers, diarizationAvailable, diarization,
    nativeAvailable, applications, selectedApplications, toggleApplication, refreshApplications,
    models, activeModel, selectModel, modelSwitching,
    start, pause, resume, stop,
    // Other panels reach the helper through the same one-time launch credential.
    authorizedFetch,
  };
}
