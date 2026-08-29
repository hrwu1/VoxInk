'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

export type RecorderState = 'ready' | 'starting' | 'recording' | 'paused' | 'stopping' | 'failed';
export type CaptureMode = 'microphone' | 'system' | 'applications' | 'microphone_system' | 'microphone_applications';
export type Caption = { id: string; source: string; text: string; startMs: number; endMs: number; isFinal: boolean };
export type CaptureApplication = { bundleId: string; name: string };

type LaunchConfig = { origin: string; token: string; retainAudio: boolean };
type NativeStatus = {
  state: 'ready' | 'recording' | 'paused' | 'stopping' | 'failed';
  events: Array<{ segment_id: string; source: string; text: string; start_ms: number; end_ms: number; is_final: boolean }>;
  next_cursor: number;
  exit_code: number | null;
  error: string | null;
};

const TARGET_SAMPLE_RATE = 16_000;
const FRAME_SAMPLES = 320;
const MAX_BUFFERED_BYTES = 2 * 1024 * 1024;
const MAX_VISIBLE_CAPTIONS = 250;

export function useVoxInkRecorder() {
  const [state, setState] = useState<RecorderState>('ready');
  const [seconds, setSeconds] = useState(0);
  const [captions, setCaptions] = useState<Caption[]>([]);
  const [droppedFrames, setDroppedFrames] = useState(0);
  const [launch, setLaunch] = useState<LaunchConfig | null>(null);
  const [message, setMessage] = useState('Open through voxink-dev to connect');
  const [mode, setModeState] = useState<CaptureMode>('microphone');
  const [nativeAvailable, setNativeAvailable] = useState(false);
  const [applications, setApplications] = useState<CaptureApplication[]>([]);
  const [selectedApplications, setSelectedApplications] = useState<string[]>([]);
  const [captureKind, setCaptureKind] = useState<'browser' | 'native' | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const contextRef = useRef<AudioContext | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const acceptingAudio = useRef(false);
  const sentSamples = useRef(0);
  const sequence = useRef(0);
  const nativeCursor = useRef(0);
  const pendingSamples = useRef<number[]>([]);

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
    if (state !== 'recording') return;
    const timer = window.setInterval(() => setSeconds((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [state]);

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

  const cleanupAudio = useCallback(async () => {
    acceptingAudio.current = false;
    processorRef.current?.disconnect();
    sourceRef.current?.disconnect();
    streamRef.current?.getTracks().forEach((track) => track.stop());
    if (contextRef.current && contextRef.current.state !== 'closed') await contextRef.current.close();
    processorRef.current = null; sourceRef.current = null; streamRef.current = null; contextRef.current = null;
    pendingSamples.current = [];
  }, []);

  const fail = useCallback(async (error: unknown) => {
    await cleanupAudio();
    socketRef.current?.close(); socketRef.current = null;
    setCaptureKind(null); setState('failed');
    setMessage(error instanceof Error ? error.message : 'The local session stopped unexpectedly');
  }, [cleanupAudio]);

  const handleMessage = useCallback(async (event: MessageEvent<string>) => {
    const payload = JSON.parse(event.data);
    if (payload.type === 'transcript.final' || payload.type === 'transcript.partial') {
      const item = payload.event;
      addCaption({ id: item.segment_id, source: item.source, text: item.text, startMs: item.start_ms, endMs: item.end_ms, isFinal: payload.type === 'transcript.final' });
    } else if (payload.type === 'stream.lag') {
      setMessage(`Inference is ${payload.real_time_factor}× real time`);
    } else if (payload.type === 'stream.stopped') {
      await cleanupAudio(); socketRef.current?.close(); socketRef.current = null;
      setCaptureKind(null); setState('ready'); setMessage('Transcript finalized and saved locally');
    } else if (payload.type === 'error') {
      await fail(new Error(payload.message || 'The helper reported an error'));
    }
  }, [addCaption, cleanupAudio, fail]);

  const applyNativeStatus = useCallback((status: NativeStatus) => {
    status.events.forEach((item) => addCaption({
      id: item.segment_id, source: item.source, text: item.text,
      startMs: item.start_ms, endMs: item.end_ms, isFinal: item.is_final,
    }));
    nativeCursor.current = status.next_cursor;
    if (status.state === 'failed') {
      setCaptureKind(null); setState('failed');
      setMessage(status.error || `Native capture stopped unexpectedly${status.exit_code === null ? '' : ` (exit ${status.exit_code})`}`);
    } else if (status.state === 'ready') {
      setCaptureKind(null); setState('ready'); setMessage('Transcript finalized and saved locally');
    }
  }, [addCaption]);

  useEffect(() => {
    if (captureKind !== 'native' || !launch) return;
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
  }, [applyNativeStatus, authorizedFetch, captureKind, launch]);

  const sendFrame = useCallback((samples: Int16Array) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN || !acceptingAudio.current) return;
    const startMs = sentSamples.current * 1000 / TARGET_SAMPLE_RATE;
    sentSamples.current += samples.length;
    if (socket.bufferedAmount > MAX_BUFFERED_BYTES) { setDroppedFrames((value) => value + 1); return; }
    socket.send(JSON.stringify({
      type: 'audio.frame', sequence: sequence.current++, stream_id: 'microphone-1', start_ms: startMs,
      duration_ms: 20, captured_at_ns: Math.round(performance.now() * 1_000_000), byte_length: samples.byteLength,
    }));
    socket.send(samples.buffer);
  }, []);

  const captureAudio = useCallback((input: Float32Array, inputRate: number) => {
    if (!acceptingAudio.current) return;
    const ratio = inputRate / TARGET_SAMPLE_RATE;
    const outputLength = Math.floor(input.length / ratio);
    for (let index = 0; index < outputLength; index += 1) {
      const start = Math.floor(index * ratio);
      const end = Math.max(start + 1, Math.floor((index + 1) * ratio));
      let sum = 0;
      for (let sourceIndex = start; sourceIndex < end && sourceIndex < input.length; sourceIndex += 1) sum += input[sourceIndex];
      const value = Math.max(-1, Math.min(1, sum / (end - start)));
      pendingSamples.current.push(value < 0 ? value * 32768 : value * 32767);
    }
    while (pendingSamples.current.length >= FRAME_SAMPLES) sendFrame(Int16Array.from(pendingSamples.current.splice(0, FRAME_SAMPLES)));
  }, [sendFrame]);

  const start = useCallback(async () => {
    if (!launch) { setMessage('Preview mode — launch with ./scripts/voxink-dev for live audio'); setSeconds(0); setCaptions([]); setState('recording'); return; }
    setState('starting'); setSeconds(0); setCaptions([]); setDroppedFrames(0);
    sentSamples.current = 0; sequence.current = 0; nativeCursor.current = 0;
    try {
      if (mode !== 'microphone') {
        if ((mode === 'applications' || mode === 'microphone_applications') && selectedApplications.length === 0) throw new Error('Choose at least one running application first');
        setMessage('Starting macOS audio capture…');
        const response = await authorizedFetch('/v1/ui/capture/start', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sources: mode.startsWith('microphone_') ? ['microphone', 'system'] : ['system'],
            applications: mode.endsWith('applications') ? selectedApplications : [],
            retain_audio: launch.retainAudio,
          }),
        });
        const status = await response.json() as NativeStatus;
        nativeCursor.current = status.next_cursor;
        setCaptureKind('native'); setState('recording'); setMessage('Listening locally with macOS capture');
        return;
      }
      setMessage('Requesting microphone access…');
      const sessionId = new Date().toISOString().replaceAll(':', '-').replaceAll('.', '-');
      const socket = new WebSocket(`${launch.origin.replace(/^http/, 'ws')}/v1/sessions/${sessionId}/stream`, [`voxink.auth.${launch.token}`]);
      socketRef.current = socket;
      await new Promise<void>((resolve, reject) => {
        socket.addEventListener('open', () => resolve(), { once: true });
        socket.addEventListener('error', () => reject(new Error('Cannot reach the local VoxInk helper')), { once: true });
      });
      socket.addEventListener('message', handleMessage);
      socket.addEventListener('close', () => { if (acceptingAudio.current) void fail(new Error('The local helper connection closed')); });
      socket.send(JSON.stringify({
        type: 'stream.start', schema_version: 1, session_id: sessionId, session_sources: ['microphone'], stream_id: 'microphone-1', source: 'microphone',
        audio: { encoding: 'pcm_s16le', sample_rate_hz: TARGET_SAMPLE_RATE, channels: 1, frame_duration_ms: 20 },
        client: { name: 'voxink-web', version: '0.2.0' }, stream_mode: 'frames',
        retention: { audio: launch.retainAudio, separate: true, combined: false },
      }));
      const media = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
      const context = new AudioContext();
      const source = context.createMediaStreamSource(media);
      const processor = context.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (audioEvent) => captureAudio(audioEvent.inputBuffer.getChannelData(0), context.sampleRate);
      source.connect(processor); processor.connect(context.destination);
      streamRef.current = media; contextRef.current = context; sourceRef.current = source; processorRef.current = processor;
      acceptingAudio.current = true; setCaptureKind('browser'); setState('recording'); setMessage('Listening locally');
    } catch (error) { await fail(error); }
  }, [authorizedFetch, captureAudio, fail, handleMessage, launch, mode, selectedApplications]);

  const pause = useCallback(async () => {
    if (captureKind === 'native') {
      try { await authorizedFetch('/v1/ui/capture/pause', { method: 'POST' }); setState('paused'); setMessage('Session paused'); } catch (error) { await fail(error); }
      return;
    }
    acceptingAudio.current = false;
    socketRef.current?.send(JSON.stringify({ type: 'stream.pause', stream_id: 'microphone-1', at_ms: sentSamples.current * 1000 / TARGET_SAMPLE_RATE }));
    setState('paused'); setMessage('Session paused');
  }, [authorizedFetch, captureKind, fail]);

  const resume = useCallback(async () => {
    if (captureKind === 'native') {
      try { await authorizedFetch('/v1/ui/capture/resume', { method: 'POST' }); setState('recording'); setMessage('Listening locally with macOS capture'); } catch (error) { await fail(error); }
      return;
    }
    socketRef.current?.send(JSON.stringify({ type: 'stream.resume', stream_id: 'microphone-1', at_ms: sentSamples.current * 1000 / TARGET_SAMPLE_RATE }));
    acceptingAudio.current = true; setState('recording'); setMessage('Listening locally');
  }, [authorizedFetch, captureKind, fail]);

  const stop = useCallback(async () => {
    if (!launch) { setState('ready'); setMessage('Preview ready'); return; }
    setState('stopping'); setMessage('Finalizing the last utterance…');
    if (captureKind === 'native') {
      try { const response = await authorizedFetch('/v1/ui/capture/stop', { method: 'POST' }); applyNativeStatus(await response.json() as NativeStatus); } catch (error) { await fail(error); }
      return;
    }
    acceptingAudio.current = false;
    socketRef.current?.send(JSON.stringify({ type: 'stream.stop', stream_id: 'microphone-1', at_ms: sentSamples.current * 1000 / TARGET_SAMPLE_RATE }));
  }, [applyNativeStatus, authorizedFetch, captureKind, fail, launch]);

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

  useEffect(() => () => { void cleanupAudio(); socketRef.current?.close(); }, [cleanupAudio]);

  return {
    state, elapsed, captions, droppedFrames, message, isLive: Boolean(launch), mode, setMode,
    nativeAvailable, applications, selectedApplications, toggleApplication, refreshApplications,
    start, pause, resume, stop,
  };
}
