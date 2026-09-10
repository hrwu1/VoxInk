import assert from 'node:assert/strict';
import test from 'node:test';

import { applyFeedEvents, liveSpeakerLabel, sourceLabel } from '../app/caption-feed.ts';

const partial = { type: 'transcript', segment_id: 's1', source: 'microphone', text: 'hello th', start_ms: 0, end_ms: 800, is_final: false };
const final = { ...partial, text: 'Hello there.', end_ms: 1200, is_final: true };

test('a final replaces the partial shown under the same segment id', () => {
  const captions = applyFeedEvents([], [partial, final]);
  assert.equal(captions.length, 1);
  assert.equal(captions[0].text, 'Hello there.');
  assert.equal(captions[0].isFinal, true);
});

test('a translation attaches to its segment and survives a later final', () => {
  const translation = { type: 'translation', segment_id: 's1', text: '你好。', start_ms: 0, end_ms: 1200, language: 'zh' };
  const captions = applyFeedEvents([], [partial, translation, final]);
  assert.equal(captions[0].translation, '你好。');
  assert.equal(captions[0].translationLanguage, 'zh');
});

test('marks are their own kind and sort by time', () => {
  const mark = { type: 'mark', segment_id: 'mark-1', text: 'decision', start_ms: 500, end_ms: 500 };
  const captions = applyFeedEvents([], [final, mark]);
  assert.deepEqual(captions.map((c) => c.kind), ['transcript', 'mark']);
  assert.equal(sourceLabel(captions[1]), 'MARK');
});

test('a per-application system stream is labeled by its application', () => {
  const zoom = { type: 'transcript', segment_id: 's2', source: 'system', text: 'From the call.', start_ms: 0, end_ms: 900, channel: 'us.zoom.xos', channel_name: 'Zoom' };
  const [caption] = applyFeedEvents([], [zoom]);
  assert.equal(sourceLabel(caption), 'ZOOM');
  assert.equal(sourceLabel({ ...caption, channelName: undefined }), 'SYSTEM / APP');
});

test('enrolled speaker names survive the feed and later translation', () => {
  const captions = applyFeedEvents([], [
    { ...final, speaker: 'Wei' },
    { type: 'translation', segment_id: 's1', text: '你好。', start_ms: 0, end_ms: 1200, language: 'zh' },
  ]);
  assert.equal(captions[0].speaker, 'Wei');
  assert.equal(liveSpeakerLabel(captions[0]), 'Wei');
  assert.equal(liveSpeakerLabel({ ...captions[0], kind: 'mark' }), null);
});

test('live speaker identities stay provisional after caption text settles', () => {
  const [caption] = applyFeedEvents([], [
    { ...partial, speaker: 'Speaker 1', speakers: ['microphone:speaker0'], speaker_provisional: true, speaker_overlap: false },
    final,
  ]);
  assert.equal(caption.isFinal, true);
  assert.deepEqual(caption.speakers, ['microphone:speaker0']);
  assert.equal(liveSpeakerLabel(caption), 'Speaker 1 · provisional');
});

test('overlap keeps all labels and the entire interjection in one caption', () => {
  const [caption] = applyFeedEvents([], [{
    ...final, text: '嗯，yes.', start_ms: 1000, end_ms: 1080,
    speaker: 'Speaker 1 + Speaker 2', speakers: ['microphone:speaker0', 'microphone:speaker1'],
    speaker_provisional: true, speaker_overlap: true,
  }]);
  assert.equal(caption.text, '嗯，yes.');
  assert.equal(caption.endMs - caption.startMs, 80);
  assert.equal(liveSpeakerLabel(caption), 'Speaker 1 + Speaker 2 · overlap · provisional');
});

test('explicit unassigned metadata clears a previous speaker guess', () => {
  const [caption] = applyFeedEvents([], [
    { ...partial, speaker: 'Speaker 1', speakers: ['microphone:speaker0'], speaker_provisional: true, speaker_overlap: true },
    { ...final, speaker: null, speakers: [], speaker_provisional: true, speaker_overlap: false },
  ]);
  assert.equal(caption.speaker, null);
  assert.deepEqual(caption.speakers, []);
  assert.equal(liveSpeakerLabel(caption), 'Unassigned · provisional');
});

test('a system stream without speaker metadata keeps its source label', () => {
  const [caption] = applyFeedEvents([], [{ ...final, source: 'system', channel_name: 'Zoom' }]);
  assert.equal(liveSpeakerLabel(caption), null);
  assert.equal(sourceLabel(caption), 'ZOOM');
});
