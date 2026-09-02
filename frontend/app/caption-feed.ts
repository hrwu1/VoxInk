/**
 * The live caption feed as a pure reducer over helper status events.
 *
 * The helper's feed carries three kinds of entry: transcript events (partial
 * or final, keyed by segment id), translations that arrive for a segment
 * after its final, and markers the user dropped. Keeping the merge logic out
 * of the React hook means it can be tested without a browser.
 */

export type CaptionKind = 'transcript' | 'mark';

export type Caption = {
  id: string;
  kind: CaptionKind;
  source: string;
  text: string;
  startMs: number;
  endMs: number;
  isFinal: boolean;
  /** Application name for a per-application system stream. */
  channelName?: string;
  /** On-device translation into the other language, once it arrives. */
  translation?: string;
  translationLanguage?: string;
};

export type FeedEvent = {
  type?: string;
  segment_id: string;
  source?: string | null;
  text: string;
  start_ms: number;
  end_ms: number;
  is_final?: boolean;
  channel?: string | null;
  channel_name?: string | null;
  language?: string | null;
  translation?: string | null;
  translation_language?: string | null;
};

export const MAX_VISIBLE_CAPTIONS = 250;

function upsert(captions: Caption[], caption: Caption): Caption[] {
  const existing = captions.findIndex((entry) => entry.id === caption.id);
  if (existing < 0) {
    return [...captions, caption].sort((a, b) => a.startMs - b.startMs).slice(-MAX_VISIBLE_CAPTIONS);
  }
  const previous = captions[existing];
  // A final that replaces a partial keeps a translation that beat it here.
  const next = [...captions];
  next[existing] = { ...caption, translation: caption.translation ?? previous.translation, translationLanguage: caption.translationLanguage ?? previous.translationLanguage };
  return next;
}

/** Fold one helper feed entry into the caption list. */
export function applyFeedEvent(captions: Caption[], event: FeedEvent): Caption[] {
  const type = event.type ?? 'transcript';
  if (type === 'translation') {
    return captions.map((caption) => caption.id === event.segment_id
      ? { ...caption, translation: event.text, translationLanguage: event.language ?? undefined }
      : caption);
  }
  if (type === 'mark') {
    return upsert(captions, {
      id: event.segment_id, kind: 'mark', source: 'mark', text: event.text,
      startMs: event.start_ms, endMs: event.end_ms, isFinal: true,
    });
  }
  return upsert(captions, {
    id: event.segment_id,
    kind: 'transcript',
    source: event.source ?? 'microphone',
    text: event.text,
    startMs: event.start_ms,
    endMs: event.end_ms,
    isFinal: event.is_final ?? true,
    channelName: event.channel_name ?? event.channel ?? undefined,
    translation: event.translation ?? undefined,
    translationLanguage: event.translation_language ?? undefined,
  });
}

export function applyFeedEvents(captions: Caption[], events: FeedEvent[]): Caption[] {
  return events.reduce(applyFeedEvent, captions);
}

/** The label shown before a caption: who or what it came from. */
export function sourceLabel(caption: Caption): string {
  if (caption.kind === 'mark') return 'MARK';
  if (caption.source === 'system') return caption.channelName ? caption.channelName.toUpperCase() : 'SYSTEM / APP';
  return 'MIC';
}
