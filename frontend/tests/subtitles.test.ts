import assert from 'node:assert/strict';
import test from 'node:test';
import type { SubtitleEvent } from '../src/api.ts';
import { upsertSubtitle } from '../src/subtitles.ts';

const source = (id: string): SubtitleEvent => ({
  seq: 1, utterance_id: id, revision: 0, zh: '你好。', en: '',
  ts: '12:00:00', translation_status: 'pending',
});

test('translation updates its source row without changing caption order', () => {
  const events: SubtitleEvent[] = [];
  upsertSubtitle(events, source('first'));
  upsertSubtitle(events, source('second'));
  upsertSubtitle(events, {...source('first'), revision: 1, en: 'Hello.', translation_status: 'completed'});
  assert.equal(events.length, 2);
  assert.equal(events[0].en, 'Hello.');
  assert.equal(events[1].utterance_id, 'second');
});

test('duplicate and stale revisions cannot replace a translation', () => {
  const translated: SubtitleEvent = {...source('first'), revision: 1, en: 'Hello.', translation_status: 'completed'};
  const events = [translated];
  upsertSubtitle(events, source('first'));
  upsertSubtitle(events, {...translated, en: 'Duplicate'});
  assert.deepEqual(events, [translated]);
});

test('speech errors update the same row and retain its translation', () => {
  const events: SubtitleEvent[] = [source('first')];
  upsertSubtitle(events, {...source('first'), revision: 2, en: 'Hello.',
    translation_status: 'completed', speech_error: 'Speech timeout'});
  assert.equal(events.length, 1);
  assert.equal(events[0].en, 'Hello.');
  assert.equal(events[0].speech_error, 'Speech timeout');
});

test('legacy events remain valid and caption history stays bounded', () => {
  const events: SubtitleEvent[] = [];
  for (let i = 0; i < 51; i++) {
    const caption = source(String(i));
    delete caption.revision;
    delete caption.translation_status;
    upsertSubtitle(events, caption);
  }
  upsertSubtitle(events, {...events[0]});
  assert.equal(events.length, 50);
  assert.equal(events[0].utterance_id, '1');
});
