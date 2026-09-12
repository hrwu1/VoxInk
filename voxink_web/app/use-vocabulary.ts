'use client';

import { useCallback, useEffect, useState } from 'react';
import type { AuthorizedFetch } from './local-service';

export type VocabularyCorrection = { misheard: string; correct: string };
export type VocabularySummary = {
  path: string;
  terms: string[];
  corrections: VocabularyCorrection[];
  biasedTerms: number;
};


const EMPTY_SUMMARY: VocabularySummary = { path: '', terms: [], corrections: [], biasedTerms: 0 };

const PLACEHOLDER = `# Names and terms VoxInk should recognize.
VoxInk

# When biasing is not enough, correct what it actually hears.
vox ink => VoxInk
`;

/** Edit the vocabulary file the local helper reads before every transcription. */
export function useVocabulary(authorizedFetch: AuthorizedFetch, isLive: boolean) {
  const [draft, setDraft] = useState('');
  const [saved, setSaved] = useState('');
  const [summary, setSummary] = useState<VocabularySummary>(EMPTY_SUMMARY);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [failed, setFailed] = useState(false);

  const absorb = useCallback((payload: unknown, { keepDraft = false } = {}) => {
    const view = payload as {
      path?: string;
      content?: string;
      terms?: string[];
      corrections?: VocabularyCorrection[];
      biased_terms?: number;
    };
    const content = view.content ?? '';
    setSaved(content);
    if (!keepDraft) setDraft(content);
    setSummary({
      path: view.path ?? '',
      terms: view.terms ?? [],
      corrections: view.corrections ?? [],
      biasedTerms: view.biased_terms ?? 0,
    });
  }, []);

  // Written as a promise chain rather than an async function so the mount
  // effect below never reaches a setState synchronously.
  const reload = useCallback(() => {
    if (!isLive) return Promise.resolve();
    return authorizedFetch('/v1/vocabulary')
      .then((response) => response.json())
      .then((payload) => {
        absorb(payload);
        setLoaded(true);
        setFailed(false);
        setMessage('');
      })
      .catch((error: unknown) => {
        setFailed(true);
        setMessage(error instanceof Error ? error.message : 'Cannot read the vocabulary file');
      });
  }, [absorb, authorizedFetch, isLive]);

  useEffect(() => { void reload(); }, [reload]);

  const save = useCallback(async () => {
    if (!isLive || saving) return;
    setSaving(true);
    try {
      const response = await authorizedFetch('/v1/vocabulary', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: draft }),
      });
      // The saved text is echoed back, so keep the draft the user is looking at
      // rather than replacing the cursor position they were typing at.
      absorb(await response.json(), { keepDraft: true });
      setFailed(false);
      setMessage('Saved — the next phrase uses it');
    } catch (error) {
      setFailed(true);
      setMessage(error instanceof Error ? error.message : 'Cannot save the vocabulary file');
    } finally {
      setSaving(false);
    }
  }, [absorb, authorizedFetch, draft, isLive, saving]);

  const revert = useCallback(() => {
    setDraft(saved);
    setFailed(false);
    setMessage('');
  }, [saved]);

  const edit = useCallback((value: string) => {
    setDraft(value);
    setMessage('');
  }, []);

  return {
    draft,
    edit,
    save,
    revert,
    reload,
    saving,
    loaded,
    message,
    failed,
    summary,
    placeholder: PLACEHOLDER,
    dirty: draft !== saved,
  };
}
