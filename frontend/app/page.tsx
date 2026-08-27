'use client';

import { useEffect, useRef } from 'react';
import { useVoxInkRecorder } from './use-voxink-recorder';

const bars = [22, 42, 30, 58, 36, 68, 44, 76, 52, 64, 34, 72, 46, 82, 40, 62, 30, 52, 24];

export default function Home() {
  const recorder = useVoxInkRecorder();
  const { state, elapsed, captions, droppedFrames, message, isLive } = recorder;
  const captionListRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleShortcut(event: KeyboardEvent) {
      if (event.code !== 'Space' || ['INPUT', 'SELECT', 'BUTTON'].includes((event.target as HTMLElement).tagName)) return;
      event.preventDefault();
      if (state === 'ready' || state === 'failed') void recorder.start();
      else if (state === 'recording') recorder.pause();
      else if (state === 'paused') recorder.resume();
    }
    window.addEventListener('keydown', handleShortcut);
    return () => window.removeEventListener('keydown', handleShortcut);
  }, [recorder, state]);

  useEffect(() => {
    const list = captionListRef.current;
    if (list) list.scrollTo({ top: list.scrollHeight, behavior: 'smooth' });
  }, [captions]);

  return (
    <main className="app-shell">
      <nav className="topbar" aria-label="VoxInk navigation">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
          <span>VoxInk</span>
        </div>
        <div className="privacy-pill"><span /> {isLive ? 'Local helper' : 'UI preview'}</div>
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
            {state === 'ready' ? (isLive ? 'Ready to record' : 'Preview ready') : state === 'paused' ? 'Session paused' : state === 'failed' ? 'Needs attention' : state === 'stopping' ? 'Finalizing' : state === 'starting' ? 'Starting' : 'Listening now'}
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
              <button className="secondary-control" onClick={() => void recorder.stop()} disabled={state === 'ready' || state === 'failed'}>Stop</button>
              <button className={`record-control ${state}`} disabled={state === 'starting' || state === 'stopping'} onClick={() => {
                if (state === 'ready' || state === 'failed') void recorder.start();
                else if (state === 'recording') recorder.pause();
                else if (state === 'paused') recorder.resume();
              }}>
                <span aria-hidden="true">{state === 'recording' ? 'Ⅱ' : '●'}</span>
                {state === 'ready' || state === 'failed' ? 'Start recording' : state === 'paused' ? 'Resume' : state === 'starting' ? 'Connecting…' : state === 'stopping' ? 'Finalizing…' : 'Pause'}
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
            <div
              ref={captionListRef}
              className="caption-list"
              aria-label="Scrollable live transcript"
              aria-live="polite"
              tabIndex={0}
            >
              {captions.length ? captions.map((caption) => (
                <article className="caption-line" key={caption.id}>
                  <time>{formatTime(caption.startMs)}</time>
                  <div><span className="speaker-label">{caption.source === 'system' ? 'SYSTEM' : 'MIC'}</span><p>{caption.text}</p></div>
                </article>
              )) : (
                <div className="empty-captions">
                  <span className="listening-orbit"><i /></span>
                  <strong>{state === 'recording' ? 'Listening…' : state === 'paused' ? 'Paused' : state === 'failed' ? 'Connection failed' : 'Your captions will appear here'}</strong>
                  <p>{message}. Finalized words are saved locally as Markdown.</p>
                </div>
              )}
            </div>
            <footer className="caption-footer">
              <span><i /> {droppedFrames} dropped {droppedFrames === 1 ? 'frame' : 'frames'}</span>
              <span>{message}</span>
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

function formatTime(milliseconds: number) {
  const totalSeconds = Math.floor(milliseconds / 1000);
  return `${Math.floor(totalSeconds / 60).toString().padStart(2, '0')}:${(totalSeconds % 60).toString().padStart(2, '0')}`;
}
