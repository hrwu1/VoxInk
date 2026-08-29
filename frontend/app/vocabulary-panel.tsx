'use client';

import { useVocabulary } from './use-vocabulary';

type VocabularyPanelProps = {
  vocabulary: ReturnType<typeof useVocabulary>;
  isLive: boolean;
};

export function VocabularyPanel({ vocabulary, isLive }: VocabularyPanelProps) {
  const { summary, dirty, saving, failed, message } = vocabulary;
  const unbiased = summary.terms.length - summary.biasedTerms;

  return (
    <div className="content-grid">
      <section className="vocabulary-card" aria-label="Vocabulary editor">
        <div className="vocabulary-topline">
          <span>
            <small>Vocabulary file</small>
            <strong title={summary.path}>{fileLabel(summary.path)}</strong>
          </span>
          <span className={`vocabulary-status ${failed ? 'failed' : dirty ? 'dirty' : 'clean'}`}>
            {failed ? 'Not saved' : dirty ? 'Unsaved changes' : saving ? 'Saving…' : 'Saved'}
          </span>
        </div>

        <textarea
          className="vocabulary-editor"
          aria-label="Vocabulary file contents"
          spellCheck={false}
          value={vocabulary.draft}
          placeholder={vocabulary.placeholder}
          disabled={!isLive}
          onChange={(event) => vocabulary.edit(event.target.value)}
        />

        <p className="vocabulary-message" role="status">
          {isLive
            ? message || 'One term per line. Use “misheard => Correct” to fix a word VoxInk keeps getting wrong.'
            : 'Preview mode — launch with ./scripts/voxink-dev to edit your vocabulary.'}
        </p>

        <div className="vocabulary-actions">
          <button
            type="button"
            className="secondary-control"
            disabled={!dirty || saving}
            onClick={() => vocabulary.revert()}
          >
            Revert
          </button>
          <button
            type="button"
            className="record-control"
            disabled={!isLive || !dirty || saving}
            onClick={() => void vocabulary.save()}
          >
            {saving ? 'Saving…' : 'Save vocabulary'}
          </button>
        </div>
        <p className="shortcut-hint">Edits apply to the next spoken phrase, even mid-recording.</p>
      </section>

      <aside className="session-card">
        <div className="panel-heading">
          <div><p className="eyebrow">Parsed by the helper</p><h2>In effect</h2></div>
          <span className="language-chip">{summary.terms.length} {summary.terms.length === 1 ? 'term' : 'terms'}</span>
        </div>

        <div className="vocabulary-summary" aria-live="polite">
          {summary.terms.length ? (
            <section>
              <p className="eyebrow">Recognized terms</p>
              <div className="term-chips">
                {summary.terms.map((term, index) => (
                  <span key={term} className={index < summary.biasedTerms ? 'term-chip' : 'term-chip inactive'}>
                    {term}
                  </span>
                ))}
              </div>
              {unbiased > 0 && (
                <p className="vocabulary-warning">
                  The last {unbiased} {unbiased === 1 ? 'term is' : 'terms are'} past the size the helper
                  sends with each phrase, so {unbiased === 1 ? 'it is' : 'they are'} not recognized. Shorten the list.
                </p>
              )}
            </section>
          ) : (
            <div className="empty-captions">
              <span className="listening-orbit"><i /></span>
              <strong>No terms yet</strong>
              <p>Add the names and jargon VoxInk keeps mishearing, one per line.</p>
            </div>
          )}

          {summary.corrections.length > 0 && (
            <section>
              <p className="eyebrow">Correction rules</p>
              <ul className="correction-rules">
                {summary.corrections.map((rule) => (
                  <li key={`${rule.misheard}=>${rule.correct}`}>
                    <span>{rule.misheard}</span>
                    <i aria-hidden="true">→</i>
                    <strong>{rule.correct}</strong>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>

        <footer className="caption-footer">
          <span><i /> Read before every phrase</span>
          <button type="button" onClick={() => void vocabulary.reload()} disabled={!isLive}>
            Reload from disk
          </button>
        </footer>
      </aside>
    </div>
  );
}

function fileLabel(path: string) {
  if (!path) return 'vocabulary.txt';
  const home = path.match(/^\/Users\/[^/]+\/(.*)$/);
  return home ? `~/${home[1]}` : path;
}
