'use client';

import { useEffect, useRef, useState } from 'react';
import { sourceLabel } from './caption-feed';
import {
  Caption, CaptionLanguage, CaptureMode, Diarization, SpeakerSegment, useVoxInkRecorder,
} from './use-voxink-recorder';
import { useVocabulary } from './use-vocabulary';
import { VocabularyPanel } from './vocabulary-panel';

const bars = [22, 42, 30, 58, 36, 68, 44, 76, 52, 64, 34, 72, 46, 82, 40, 62, 30, 52, 24];
type Tab = 'record' | 'vocabulary';

/** Render an anonymous session-local speaker id as something readable.
 *
 * `MIC_SPEAKER_00` becomes `Speaker 1`, unless the user named it or the local
 * voice registry recognized it. The raw identifier is deliberately not shown:
 * it looks like a durable identity, and it is not one.
 */
function speakerName(speakerId: string, names: Record<string, string>): string {
  const named = names[speakerId];
  if (named) return named;
  const index = Number(speakerId.slice(speakerId.lastIndexOf('_') + 1));
  return Number.isFinite(index) ? `Speaker ${index + 1}` : speakerId;
}

/** The diarization segment for one caption, if the job produced one. */
function segmentFor(diarization: Diarization, caption: Caption): SpeakerSegment | null {
  if (diarization.state !== 'ready' || caption.source === 'system' || caption.kind === 'mark') return null;
  const middle = caption.startMs + (caption.endMs - caption.startMs) / 2;
  return diarization.segments.find((item) => item.startMs === caption.startMs)
    ?? diarization.segments.find((item) => item.startMs <= middle && middle < item.endMs)
    ?? null;
}

/** The label for one caption, or null to leave the source tag in place. */
function speakerFor(segment: SpeakerSegment | null, names: Record<string, string>): string | null {
  if (!segment) return null;
  // A segment with no speaker is a deliberate outcome, not a gap: reconciliation
  // saw a tie or too little speech and refused to guess. Saying so is better
  // than showing a blank the reader has to interpret.
  if (segment.speakerId) return speakerName(segment.speakerId, names);
  return segment.pieces.length > 1 ? 'Several' : 'Unclear';
}

/** A caption split where the speaker changed, one line per piece.
 *
 * Pieces are character ranges of the canonical block text, which is the
 * caption text trimmed; the ranges tile it, so nothing is dropped or invented.
 */
function CaptionText({ caption, segment, names }: { caption: Caption; segment: SpeakerSegment | null; names: Record<string, string> }) {
  const text = caption.text.trim();
  const pieces = segment?.pieces ?? [];
  if (pieces.length < 2 || pieces[pieces.length - 1].textEnd !== text.length) {
    return <p>{caption.text}</p>;
  }
  return (
    <>
      {pieces.map((piece) => (
        <p key={piece.textStart}>
          <span className="piece-speaker">
            {piece.speakerId ? speakerName(piece.speakerId, names) : 'Unclear'}
          </span>
          {text.slice(piece.textStart, piece.textEnd).trim()}
        </p>
      ))}
    </>
  );
}

function SpeakerChip({ diarization }: { diarization: Diarization }) {
  if (diarization.state === 'disabled') return null;
  if (diarization.state === 'waiting_for_stop') {
    return <span className="speaker-chip"><i />Speakers after stop</span>;
  }
  if (diarization.state === 'processing') {
    return <span className="speaker-chip busy"><i />Finding speakers…</span>;
  }
  if (diarization.state === 'ready') {
    const count = diarization.speakerCount;
    return (
      <span className="speaker-chip on">
        <i />{count} {count === 1 ? 'speaker' : 'speakers'}
        {diarization.labeledSegments < diarization.totalSegments
          && ` · ${diarization.labeledSegments}/${diarization.totalSegments} labelled`}
      </span>
    );
  }
  return (
    <span className="speaker-chip warn">
      <i />{diarization.state === 'stale' ? 'Speakers out of date' : 'Speakers unavailable'}
    </span>
  );
}

/** Rename a speaker for this session, optionally remembering the voice. */
function SpeakerNameDialog({ speakerId, current, onClose, onSave }: {
  speakerId: string; current: string; onClose: () => void;
  onSave: (name: string, remember: boolean) => void;
}) {
  const [name, setName] = useState(current);
  const [remember, setRemember] = useState(false);
  return (
    <div className="dialog-backdrop" role="presentation" onClick={onClose}>
      <form
        className="dialog"
        role="dialog"
        aria-label="Name this speaker"
        onClick={(event) => event.stopPropagation()}
        onSubmit={(event) => { event.preventDefault(); if (name.trim()) onSave(name.trim(), remember); }}
      >
        <p className="eyebrow">Name this speaker</p>
        <input autoFocus aria-label="Speaker name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Wei" />
        <label className="dialog-check">
          <input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} />
          <span><strong>Remember this voice</strong><small>Stored only on this Mac, so later sessions can label {speakerId.startsWith('MIC') ? 'them' : 'this voice'} automatically</small></span>
        </label>
        <div className="dialog-actions">
          <button type="button" className="secondary-control" onClick={onClose}>Cancel</button>
          <button type="submit" className="record-control ready" disabled={!name.trim()}>Save</button>
        </div>
      </form>
    </div>
  );
}

export default function Home() {
  const recorder = useVoxInkRecorder();
  const vocabulary = useVocabulary(recorder.authorizedFetch, recorder.isLive);
  const [tab, setTab] = useState<Tab>('record');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [naming, setNaming] = useState<{ speakerId: string; current: string } | null>(null);
  const { state, elapsed, captions, message, isLive } = recorder;
  const choosingApplications = recorder.mode === 'applications' || recorder.mode === 'microphone_applications';
  const selectionLocked = recorder.modelSwitching || !['ready', 'failed'].includes(state);
  const selectedModel = recorder.models.find((model) => model.id === recorder.activeModel);
  const usesMicrophone = recorder.mode.startsWith('microphone');
  const captionListRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Space belongs to whatever the vocabulary tab is being typed into.
    if (tab !== 'record' || naming) return;
    function handleShortcut(event: KeyboardEvent) {
      const target = event.target as HTMLElement;
      if (['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(target.tagName)) return;
      if (event.code === 'Space') {
        event.preventDefault();
        if (state === 'ready' || state === 'failed') void recorder.start();
        else if (state === 'recording') recorder.pause();
        else if (state === 'paused') recorder.resume();
      } else if (event.key === 'm' || event.key === 'M') {
        if (state === 'recording' || state === 'paused') { event.preventDefault(); void recorder.mark(); }
      }
    }
    window.addEventListener('keydown', handleShortcut);
    return () => window.removeEventListener('keydown', handleShortcut);
  }, [naming, recorder, state, tab]);

  useEffect(() => {
    const list = captionListRef.current;
    if (list) list.scrollTo({ top: list.scrollHeight, behavior: 'smooth' });
  }, [captions]);

  const languageLabel = { auto: 'Auto · EN / 中文', en: 'English', zh: '中文' }[recorder.language];

  return (
    <main className="app-shell">
      <nav className="topbar" aria-label="VoxInk navigation">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
          <span>VoxInk</span>
        </div>
        <div className="privacy-pill"><span /> {isLive ? 'Local helper' : 'UI preview'}</div>
        <button
          type="button"
          className={settingsOpen ? 'icon-button active' : 'icon-button'}
          aria-label="Open settings"
          aria-expanded={settingsOpen}
          onClick={() => setSettingsOpen((open) => !open)}
        >
          ⌘
        </button>
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

        {settingsOpen && (
          <section className="settings-panel" aria-label="Session settings">
            <div className="settings-grid">
              <label>
                <small>Microphone</small>
                <select
                  aria-label="Microphone device"
                  value={recorder.selectedDevice}
                  disabled={selectionLocked || !usesMicrophone}
                  onChange={(event) => recorder.setSelectedDevice(event.target.value)}
                >
                  <option value="">System default</option>
                  {recorder.devices.map((device) => (
                    <option key={device.uid} value={device.uid}>{device.name}{device.isDefault ? ' (default)' : ''}</option>
                  ))}
                </select>
                <button type="button" className="link-button" disabled={selectionLocked} onClick={() => void recorder.refreshDevices()}>Refresh devices</button>
              </label>
              <label>
                <small>Language</small>
                <select
                  aria-label="Caption language"
                  value={recorder.language}
                  disabled={selectionLocked}
                  onChange={(event) => recorder.setLanguage(event.target.value as CaptionLanguage)}
                >
                  <option value="auto">Detect automatically</option>
                  <option value="en">English</option>
                  <option value="zh">中文 (Chinese)</option>
                </select>
                <em>Fixing the language helps when one speaker mixes both.</em>
              </label>
              <label className="settings-toggle">
                <span>
                  <strong>Translate captions</strong>
                  <small>
                    {recorder.translationAvailable
                      ? 'Each finished sentence gets an on-device translation into the other language'
                      : 'Install models/Qwen3-0.6B to translate on this Mac'}
                  </small>
                </span>
                <input
                  type="checkbox"
                  checked={recorder.translate}
                  disabled={selectionLocked || !recorder.translationAvailable}
                  onChange={(event) => recorder.setTranslate(event.target.checked)}
                />
              </label>
              <div className="settings-note">
                <strong>Live words</strong>
                <small>
                  {recorder.partialCaptions
                    ? 'Captions appear while you speak and settle when you pause.'
                    : 'Partial captions are off for this helper; captions appear after each pause.'}
                </small>
              </div>
            </div>
          </section>
        )}

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

            {recorder.diarizationAvailable && (
              <label className="audio-retention-control speaker-control">
                <span>
                  <strong>Identify speakers</strong>
                  <small>
                    {recorder.retainAudio
                      ? 'Label who spoke, after recording stops'
                      : 'Needs saved audio — turn on Save audio first'}
                  </small>
                </span>
                <input
                  type="checkbox"
                  checked={recorder.identifySpeakers}
                  disabled={selectionLocked || !recorder.retainAudio}
                  onChange={(event) => recorder.setIdentifySpeakers(event.target.checked)}
                />
              </label>
            )}

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
                <p className="picker-note">Each application becomes its own labelled channel in the transcript.</p>
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
              <button
                className="secondary-control"
                disabled={state !== 'recording' && state !== 'paused'}
                title="Drop a timestamped marker into the transcript (M)"
                onClick={() => void recorder.mark()}
              >
                Mark
              </button>
            </div>
            <p className="shortcut-hint">Press <kbd>Space</kbd> to start or pause, <kbd>M</kbd> to mark</p>
          </section>

          <aside className="session-card">
            <div className="panel-heading">
              <div><p className="eyebrow">Live transcript</p><h2>Captions</h2></div>
              <div className="panel-chips">
                <SpeakerChip diarization={recorder.diarization} />
                {recorder.translate && recorder.translationAvailable && <span className="language-chip">Translating</span>}
                <span className="language-chip">{languageLabel}</span>
              </div>
            </div>
            <div
              ref={captionListRef}
              className="caption-list"
              aria-label="Scrollable live transcript"
              aria-live="polite"
              tabIndex={0}
            >
              {captions.length ? captions.map((caption) => {
                if (caption.kind === 'mark') {
                  return (
                    <article className="caption-line mark" key={caption.id}>
                      <time>{formatTime(caption.startMs)}</time>
                      <div>
                        <span className="speaker-label">◆ MARK</span>
                        <p>{caption.text || 'Marked'}</p>
                      </div>
                    </article>
                  );
                }
                const segment = segmentFor(recorder.diarization, caption);
                const speaker = speakerFor(segment, recorder.diarization.names);
                return (
                  <article className={caption.isFinal ? 'caption-line' : 'caption-line partial'} key={caption.id}>
                    <time>{formatTime(caption.startMs)}</time>
                    <div>
                      {speaker && segment?.speakerId ? (
                        <button
                          type="button"
                          className="speaker-label speaker-button"
                          title="Name this speaker"
                          onClick={() => setNaming({ speakerId: segment.speakerId as string, current: recorder.diarization.names[segment.speakerId as string] ?? '' })}
                        >
                          {speaker}
                        </button>
                      ) : (
                        <span className="speaker-label">{speaker ?? sourceLabel(caption)}</span>
                      )}
                      <CaptionText caption={caption} segment={segment} names={recorder.diarization.names} />
                      {caption.translation && (
                        <p className="translation" lang={caption.translationLanguage}>{caption.translation}</p>
                      )}
                    </div>
                  </article>
                );
              }) : (
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
          <div>
            <p className="eyebrow">Last session</p>
            <strong>{recorder.lastSession ? recorder.lastSession.sessionId : 'Nothing recorded yet'}</strong>
            {recorder.lastSession && (
              <button type="button" className="link-button" onClick={() => void recorder.revealFolder()}>Open folder</button>
            )}
          </div>
          <div className="local-note"><span>⌁</span><p><strong>Private by design</strong><br />Audio never leaves this Mac.</p></div>
        </footer>
      </section>

      {naming && (
        <SpeakerNameDialog
          speakerId={naming.speakerId}
          current={naming.current}
          onClose={() => setNaming(null)}
          onSave={(name, remember) => { setNaming(null); void recorder.nameSpeaker(naming.speakerId, name, remember); }}
        />
      )}
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
