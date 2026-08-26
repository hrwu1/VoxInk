'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

export type RecorderState = 'ready' | 'starting' | 'recording' | 'paused' | 'stopping' | 'failed';

export type Caption = {
  id: string;
  source: string;
  text: string;
  startMs: number;
  endMs: number;
  isFinal: boolean;
};

type LaunchConfig = { endpoint: string; token: string };

const TARGET_SAMPLE_RATE = 16_000;
const FRAME_SAMPLES = 320;
const MAX_BUFFERED_BYTES = 2 * 1024 * 1024;

export function useVoxInkRecorder() {
  const [state, setState] = useState<RecorderState>('ready');
  const [seconds, setSeconds] = useState(0);
  const [captions, setCaptions] = useState<Caption[]>([]);
  const [droppedFrames, setDroppedFrames] = useState(0);
  const [launch, setLaunch] = useState<LaunchConfig | null>(null);
  const [message, setMessage] = useState('Open through voxink-dev --web to connect');
  const socketRef = useRef<WebSocket | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const contextRef = useRef<AudioContext | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const acceptingAudio = useRef(false);
  const sentSamples = useRef(0);
  const sequence = useRef(0);
  const pendingSamples = useRef<number[]>([]);

  useEffect(() => {
    const values = new URLSearchParams(window.location.hash.slice(1));
    const bootstrap = values.get('bootstrap');
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
    if (!bootstrap) return;
    void fetch('/v1/ui/bootstrap', {
      method: 'POST',
      headers: { 'X-VoxInk-Bootstrap': bootstrap },
      cache: 'no-store',
    }).then(async (response) => {
      if (!response.ok) throw new Error('The UI launch link expired. Run voxink-dev --web again.');
      const payload = await response.json();
      const endpoint = window.location.origin.replace(/^http/, 'ws');
      setLaunch({ endpoint, token: payload.token });
      setMessage('Authenticated local helper ready');
    }).catch((error) => {
      setState('failed');
      setMessage(error instanceof Error ? error.message : 'Cannot authenticate the local UI');
    });
  }, []);

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

  const cleanupAudio = useCallback(async () => {
    acceptingAudio.current = false;
    processorRef.current?.disconnect();
    sourceRef.current?.disconnect();
    streamRef.current?.getTracks().forEach((track) => track.stop());
    if (contextRef.current && contextRef.current.state !== 'closed') {
      await contextRef.current.close();
    }
    processorRef.current = null;
    sourceRef.current = null;
    streamRef.current = null;
    contextRef.current = null;
    pendingSamples.current = [];
  }, []);

  const fail = useCallback(async (error: unknown) => {
    await cleanupAudio();
    socketRef.current?.close();
    socketRef.current = null;
    setState('failed');
    setMessage(error instanceof Error ? error.message : 'The local session stopped unexpectedly');
  }, [cleanupAudio]);

  const handleMessage = useCallback(async (event: MessageEvent<string>) => {
    const payload = JSON.parse(event.data);
    if (payload.type === 'transcript.final' || payload.type === 'transcript.partial') {
      const item = payload.event;
      const caption: Caption = {
        id: item.segment_id,
        source: item.source,
        text: item.text,
        startMs: item.start_ms,
        endMs: item.end_ms,
        isFinal: payload.type === 'transcript.final',
      };
      setCaptions((current) => {
        const existing = current.findIndex((entry) => entry.id === caption.id);
        if (existing < 0) return [...current, caption].sort((a, b) => a.startMs - b.startMs);
        const next = [...current];
        next[existing] = caption;
        return next;
      });
    } else if (payload.type === 'stream.lag') {
      setMessage(`Inference is ${payload.real_time_factor}× real time`);
    } else if (payload.type === 'stream.stopped') {
      await cleanupAudio();
      socketRef.current?.close();
      socketRef.current = null;
      setState('ready');
      setMessage('Transcript finalized and saved locally');
    } else if (payload.type === 'error') {
      await fail(new Error(payload.message || 'The helper reported an error'));
    }
  }, [cleanupAudio, fail]);

  const sendFrame = useCallback((samples: Int16Array) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN || !acceptingAudio.current) return;
    if (socket.bufferedAmount > MAX_BUFFERED_BYTES) {
      setDroppedFrames((value) => value + 1);
      return;
    }
    const startMs = sentSamples.current * 1000 / TARGET_SAMPLE_RATE;
    socket.send(JSON.stringify({
      type: 'audio.frame',
      sequence: sequence.current++,
      stream_id: 'microphone-1',
      start_ms: startMs,
      duration_ms: 20,
      captured_at_ns: Math.round(performance.now() * 1_000_000),
      byte_length: samples.byteLength,
    }));
    socket.send(samples.buffer);
    sentSamples.current += samples.length;
  }, []);

  const captureAudio = useCallback((input: Float32Array, inputRate: number) => {
    if (!acceptingAudio.current) return;
    const ratio = inputRate / TARGET_SAMPLE_RATE;
    const outputLength = Math.floor(input.length / ratio);
    for (let index = 0; index < outputLength; index += 1) {
      const start = Math.floor(index * ratio);
      const end = Math.max(start + 1, Math.floor((index + 1) * ratio));
      let sum = 0;
      for (let sourceIndex = start; sourceIndex < end && sourceIndex < input.length; sourceIndex += 1) {
        sum += input[sourceIndex];
      }
      const value = Math.max(-1, Math.min(1, sum / (end - start)));
      pendingSamples.current.push(value < 0 ? value * 32768 : value * 32767);
    }
    while (pendingSamples.current.length >= FRAME_SAMPLES) {
      sendFrame(Int16Array.from(pendingSamples.current.splice(0, FRAME_SAMPLES)));
    }
  }, [sendFrame]);

  const start = useCallback(async () => {
    if (!launch) {
      setMessage('Preview mode — launch with ./scripts/voxink-dev --web for live audio');
      setSeconds(0);
      setCaptions([]);
      setState('recording');
      return;
    }
    setState('starting');
    setMessage('Requesting microphone access…');
    setSeconds(0);
    setCaptions([]);
    setDroppedFrames(0);
    sentSamples.current = 0;
    sequence.current = 0;
    try {
      const sessionId = new Date().toISOString().replaceAll(':', '-').replaceAll('.', '-');
      const socketUrl = `${launch.endpoint.replace(/\/$/, '')}/v1/sessions/${sessionId}/stream`;
      const socket = new WebSocket(socketUrl, [`voxink.auth.${launch.token}`]);
      socketRef.current = socket;
      await new Promise<void>((resolve, reject) => {
        socket.addEventListener('open', () => resolve(), { once: true });
        socket.addEventListener('error', () => reject(new Error('Cannot reach the local VoxInk helper')), { once: true });
      });
      socket.addEventListener('message', handleMessage);
      socket.addEventListener('close', () => {
        if (acceptingAudio.current) void fail(new Error('The local helper connection closed'));
      });
      socket.send(JSON.stringify({
        type: 'stream.start', schema_version: 1, session_id: sessionId,
        session_sources: ['microphone'], stream_id: 'microphone-1', source: 'microphone',
        audio: { encoding: 'pcm_s16le', sample_rate_hz: TARGET_SAMPLE_RATE, channels: 1, frame_duration_ms: 20 },
        client: { name: 'voxink-web', version: '0.1.0' }, stream_mode: 'frames',
      }));
      const media = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
      const context = new AudioContext();
      const source = context.createMediaStreamSource(media);
      const processor = context.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (audioEvent) => captureAudio(audioEvent.inputBuffer.getChannelData(0), context.sampleRate);
      source.connect(processor);
      processor.connect(context.destination);
      streamRef.current = media;
      contextRef.current = context;
      sourceRef.current = source;
      processorRef.current = processor;
      acceptingAudio.current = true;
      setState('recording');
      setMessage('Listening locally');
    } catch (error) {
      await fail(error);
    }
  }, [captureAudio, fail, handleMessage, launch]);

  const pause = useCallback(() => {
    acceptingAudio.current = false;
    socketRef.current?.send(JSON.stringify({ type: 'stream.pause', stream_id: 'microphone-1', at_ms: sentSamples.current * 1000 / TARGET_SAMPLE_RATE }));
    setState('paused');
    setMessage('Session paused');
  }, []);

  const resume = useCallback(() => {
    socketRef.current?.send(JSON.stringify({ type: 'stream.resume', stream_id: 'microphone-1', at_ms: sentSamples.current * 1000 / TARGET_SAMPLE_RATE }));
    acceptingAudio.current = true;
    setState('recording');
    setMessage('Listening locally');
  }, []);

  const stop = useCallback(async () => {
    if (!launch) {
      setState('ready');
      setMessage('Preview ready');
      return;
    }
    acceptingAudio.current = false;
    setState('stopping');
    setMessage('Finalizing the last utterance…');
    socketRef.current?.send(JSON.stringify({ type: 'stream.stop', stream_id: 'microphone-1', at_ms: sentSamples.current * 1000 / TARGET_SAMPLE_RATE }));
  }, [launch]);

  useEffect(() => () => {
    void cleanupAudio();
    socketRef.current?.close();
  }, [cleanupAudio]);

  return { state, elapsed, captions, droppedFrames, message, isLive: Boolean(launch), start, pause, resume, stop };
}
