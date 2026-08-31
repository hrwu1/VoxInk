'use client';

import { useEffect, useRef, useState } from 'react';
import { CaptureMode, useVoxInkRecorder } from './use-voxink-recorder';
import { useVocabulary } from './use-vocabulary';
import { VocabularyPanel } from './vocabulary-panel';

const bars = [22, 42, 30, 58, 36, 68, 44, 76, 52, 64, 34, 72, 46, 82, 40, 62, 30, 52, 24];
type Tab = 'record' | 'vocabulary';

export default function Home() {
  const recorder = useVoxInkRecorder();
  const vocabulary = useVocabulary(recorder.authorizedFetch, recorder.isLive);
  const [tab, setTab] = useState<Tab>('record');
  const { state, elapsed, captions, message, isLive } = recorder;
  const choosingApplications = recorder.mode === 'applications' || recorder.mode === 'microphone_applications';
  const selectionLocked = recorder.modelSwitching || !['ready', 'failed'].includes(state);
  const selectedModel = recorder.models.find((model) => model.id === recorder.activeModel);
  const captionListRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Space belongs to whatever the vocabulary tab is being typed into.
    if (tab !== 'record') return;
    function handleShortcut(event: KeyboardEvent) {
      if (event.code !== 'Space' || ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes((event.target as HTMLElement).tagName)) return;
      event.preventDefault();
      if (state === 'ready' || state === 'failed') void recorder.start();
      else if (state === 'recording') recorder.pause();
      else if (state === 'paused') recorder.resume();
    }
    window.addEventListener('keydown', handleShortcut);
    return () => window.removeEventListener('keydown', handleShortcut);
  }, [recorder, state, tab]);

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

        <div className="tab-strip" role="tablist" aria-label="Console sections">
          <button
            type="button"
            role="tab"
            id="tab-record"
            aria-selected={tab === 'record'}
            aria-controls="panel-record"
            className={tab === 'record' ? 'tab active' : 'tab'}
            onClick={() => setTab('record')}
          >
            Record
          </button>
          <button
            type="button"
            role="tab"
            id="tab-vocabulary"
            aria-selected={tab === 'vocabulary'}
            aria-controls="panel-vocabulary"
            className={tab === 'vocabulary' ? 'tab active' : 'tab'}
            onClick={() => setTab('vocabulary')}
          >
            Vocabulary
            {vocabulary.summary.terms.length > 0 && <em>{vocabulary.summary.terms.length}</em>}
            {vocabulary.dirty && <i className="tab-dot" aria-label="unsaved changes" />}
          </button>
        </div>

        {tab === 'vocabulary' ? (
          <div role="tabpanel" id="panel-vocabulary" aria-labelledby="tab-vocabulary">
            <VocabularyPanel vocabulary={vocabulary} isLive={isLive} />
          </div>
        ) : (
        <div className="content-grid" role="tabpanel" id="panel-record" aria-labelledby="tab-record">
          <section className="recorder-card" aria-label="Recorder controls">
            <div className="recorder-topline">
              <label className="source-control">
                <span className="source-icon">◉</span>
                <span><small>Capture mode</small><strong>{modeLabel(recorder.mode)}</strong></span>
                <select
                  aria-label="Capture mode"
                  value={recorder.mode}
                  disabled={selectionLocked}
                  onChange={(event) => recorder.setMode(event.target.value as CaptureMode)}
                >
                  <option value="microphone">Microphone</option>
                  <option value="system" disabled={!recorder.nativeAvailable}>All system audio</option>
                  <option value="applications" disabled={!recorder.nativeAvailable}>Selected applications</option>
                  <option value="microphone_system" disabled={!recorder.nativeAvailable}>Microphone + system</option>
                  <option value="microphone_applications" disabled={!recorder.nativeAvailable}>Microphone + applications</option>
                </select>
              </label>
              <label className="model-control" title={selectedModel?.description}>
                <small>Model profile</small>
                <strong>{selectedModel ? `${selectedModel.displayName} · ${selectedModel.profile}` : 'Qwen3 · Local'}</strong>
                <select
                  aria-label="Transcription model"
                  value={recorder.activeModel}
                  disabled={selectionLocked || !isLive || recorder.models.length < 2}
                  onChange={(event) => void recorder.selectModel(event.target.value)}
                >
                  {recorder.models.length ? recorder.models.map((model) => (
                    <option value={model.id} key={model.id}>{model.displayName} — {model.profile}</option>
                  )) : <option value={recorder.activeModel}>Qwen3 — Local</option>}
                </select>
              </label>
            </div>

            <label className="audio-retention-control">
              <span>
                <strong>Save audio</strong>
                <small>Keep separate local WAV tracks beside the transcript</small>
              </span>
              <input
                type="checkbox"
                checked={recorder.retainAudio}
                disabled={selectionLocked}
                onChange={(event) => recorder.setRetainAudio(event.target.checked)}
              />
            </label>

            {choosingApplications && (
              <div className="application-picker">
                <div className="application-picker-heading">
                  <span><small>Running applications</small><strong>Select one or more</strong></span>
                  <div><em>{recorder.selectedApplications.length} selected</em><button type="button" disabled={selectionLocked} onClick={() => void recorder.refreshApplications()}>Refresh</button></div>
                </div>
                <div className="application-options">
                  {recorder.applications.length ? recorder.applications.map((application) => (
                    <label key={application.bundleId}>
                      <input
                        type="checkbox"
                        checked={recorder.selectedApplications.includes(application.bundleId)}
                        disabled={selectionLocked}
                        onChange={() => recorder.toggleApplication(application.bundleId)}
                      />
                      <span><strong>{application.name}</strong><small>{application.bundleId}</small></span>
                    </label>
                  )) : <p>Open the application you want to capture, then press Refresh.</p>}
                </div>
              </div>
            )}

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
              <button className={`record-control ${state}`} disabled={recorder.modelSwitching || state === 'starting' || state === 'stopping'} onClick={() => {
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
                  <div><span className="speaker-label">{caption.source === 'system' ? 'SYSTEM / APP' : 'MIC'}</span><p>{caption.text}</p></div>
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
              <span>{message}</span>
            </footer>
          </aside>
        </div>
        )}

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

function modeLabel(mode: CaptureMode) {
  return {
    microphone: 'Microphone',
    system: 'All system audio',
    applications: 'Selected applications',
    microphone_system: 'Microphone + system',
    microphone_applications: 'Microphone + applications',
  }[mode];
}
