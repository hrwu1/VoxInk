'use client';

import { useEffect, useMemo, useState } from 'react';

type SessionState = 'ready' | 'recording' | 'paused';

const bars = [22, 42, 30, 58, 36, 68, 44, 76, 52, 64, 34, 72, 46, 82, 40, 62, 30, 52, 24];

const demoCaptions = [
  { time: '00:03', text: 'VoxInk is listening locally.' },
  { time: '00:08', text: 'Your words appear here as they are finalized.' },
];

export default function Home() {
  const [state, setState] = useState<SessionState>('ready');
  const [seconds, setSeconds] = useState(0);
  const [captions, setCaptions] = useState(demoCaptions);

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

  function toggleRecording() {
    if (state === 'ready') {
      setSeconds(0);
      setCaptions([]);
      setState('recording');
      return;
    }
    setState(state === 'recording' ? 'paused' : 'recording');
  }

  function stop() {
    setState('ready');
  }

  return (
    <main className="app-shell">
      <nav className="topbar" aria-label="VoxInk navigation">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
          <span>VoxInk</span>
        </div>
        <div className="privacy-pill"><span /> Local only</div>
        <button className="icon-button" aria-label="Open settings">⌘</button>
      </nav>

      <section className="workspace">
        <header className="workspace-heading">
          <div>
            <p className="eyebrow">Developer edition</p>
            <h1>Turn speech into something<br />you can keep.</h1>
          </div>
          <div className={`connection-state ${state}`}>
            <span />
            {state === 'ready' ? 'Ready to record' : state === 'paused' ? 'Session paused' : 'Listening now'}
          </div>
        </header>

        <div className="content-grid">
          <section className="recorder-card" aria-label="Recorder controls">
            <div className="recorder-topline">
              <label className="source-control">
                <span className="source-icon">◉</span>
                <span><small>Input source</small><strong>MacBook Microphone</strong></span>
                <select aria-label="Input source" defaultValue="microphone">
                  <option value="microphone">MacBook Microphone</option>
                </select>
              </label>
              <div className="model-label"><small>Model</small><strong>Qwen3 · Local</strong></div>
            </div>

            <div className={`waveform ${state}`} aria-label={`${state} waveform`}>
              {bars.map((height, index) => (
                <span key={index} style={{ '--bar-height': `${height}%`, '--delay': `${index * -65}ms` } as React.CSSProperties} />
              ))}
            </div>

            <div className="timer-row">
              <span className="timer">{elapsed}</span>
              <span className="session-label">{state === 'ready' ? 'New session' : 'Untitled session'}</span>
            </div>

            <div className="control-row">
              <button className="secondary-control" onClick={stop} disabled={state === 'ready'}>Stop</button>
              <button className={`record-control ${state}`} onClick={toggleRecording}>
                <span aria-hidden="true">{state === 'recording' ? 'Ⅱ' : '●'}</span>
                {state === 'ready' ? 'Start recording' : state === 'paused' ? 'Resume' : 'Pause'}
              </button>
              <button className="secondary-control" disabled>Mark</button>
            </div>
            <p className="shortcut-hint">Press <kbd>Space</kbd> to start or pause</p>
          </section>

          <aside className="session-card">
            <div className="panel-heading">
              <div><p className="eyebrow">Live transcript</p><h2>Captions</h2></div>
              <span className="language-chip">Auto · EN / 中文</span>
            </div>
            <div className="caption-list" aria-live="polite">
              {captions.length ? captions.map((caption) => (
                <article className="caption-line" key={`${caption.time}-${caption.text}`}>
                  <time>{caption.time}</time>
                  <div><span className="speaker-label">MIC</span><p>{caption.text}</p></div>
                </article>
              )) : (
                <div className="empty-captions">
                  <span className="listening-orbit"><i /></span>
                  <strong>{state === 'recording' ? 'Listening…' : state === 'paused' ? 'Paused' : 'Your captions will appear here'}</strong>
                  <p>Finalized words are saved locally as Markdown.</p>
                </div>
              )}
            </div>
            <footer className="caption-footer">
              <span><i /> 0 dropped frames</span>
              <button>Open transcript folder ↗</button>
            </footer>
          </aside>
        </div>

        <footer className="recent-strip">
          <div><p className="eyebrow">Last session</p><strong>Nothing recorded yet</strong></div>
          <div className="local-note"><span>⌁</span><p><strong>Private by design</strong><br />Audio never leaves this Mac.</p></div>
        </footer>
      </section>
    </main>
  );
}
