import assert from 'node:assert/strict';
import test from 'node:test';
import type { SubtitleEvent } from '../src/api.ts';
import {
  captionSourceNote, captionSpeechStatus, captionText, captionTiming, latestTranslation, originalCaptionText,
  refinedCandidate, upsertSubtitle,
} from '../src/subtitles.ts';

const source = (id: string): SubtitleEvent => ({
  seq: 1, utterance_id: id, revision: 0, zh: '你好。', en: '',
  ts: '12:00:00', translation_status: 'pending',
});

test('a rejected candidate remains available after a newer partial caption', () => {
  const candidate: SubtitleEvent = {...source('session:1'), en: 'This is 跑分.',
    translation_status: 'failed', translation_error: 'Untranslated Chinese'};
  const events = [candidate, {...source('session:2'), translation_status: 'recognizing' as const}];
  assert.equal(latestTranslation(events), candidate);
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

test('partial, final and translation use one row with increasing revisions', () => {
  const events: SubtitleEvent[] = [];
  upsertSubtitle(events, {...source('session:1'), final: false, translation_status: 'recognizing'});
  upsertSubtitle(events, {...source('session:1'), revision: 1, final: true});
  upsertSubtitle(events, {...source('session:1'), revision: 2, final: true, en: 'Hello.', translation_status: 'completed'});
  assert.equal(events.length, 1);
  assert.equal(events[0].final, true);
  assert.equal(events[0].en, 'Hello.');
});

test('late translation cannot restore an old row after history eviction', () => {
  const events: SubtitleEvent[] = [];
  for (let seq = 1; seq <= 51; seq++) upsertSubtitle(events, {...source(`session:${seq}`), seq});
  upsertSubtitle(events, {...source('session:1'), seq: 1, revision: 2, en: 'Late'});
  assert.equal(events.length, 50);
  assert.equal(events[0].seq, 2);
  assert.equal(events[49].seq, 51);
});

test('a newer partial keeps the latest completed translation available separately', () => {
  const events: SubtitleEvent[] = [];
  upsertSubtitle(events, {...source('session:1'), seq: 1});
  upsertSubtitle(events, {...source('session:2'), seq: 2, final: false, translation_status: 'recognizing'});
  upsertSubtitle(events, {...source('session:1'), seq: 1, revision: 1, en: 'Hello.', translation_status: 'completed'});
  assert.equal(events[events.length-1].utterance_id, 'session:2');
  assert.equal(latestTranslation(events)?.utterance_id, 'session:1');
  assert.equal(latestTranslation(events)?.en, 'Hello.');
});

test('legacy events without a source selection show the recognized text', () => {
  const legacy: SubtitleEvent = {...source('legacy'), en: 'Hello.', translate_ms: 900,
    translation_status: 'completed', refinement_status: 'completed', refined_zh: '你好呀。'};
  assert.equal(captionText(legacy), '你好。');
  assert.equal(originalCaptionText(legacy), null);
  assert.equal(refinedCandidate(legacy), '你好呀。');
  assert.equal(captionText(null), '');
  assert.equal(captionSourceNote(legacy), '');
});

test('an adopted refinement supplies the caption source text', () => {
  const adopted: SubtitleEvent = {...source('adopted'), en: 'Hello.', translate_ms: 700,
    translation_status: 'completed', translation_source_text: '你好呀。', refined_zh: '你好呀。',
    translation_source_reason: 'refined',
    translation_source_audit: {selected: 'refined', reason: 'refined', issues: []}};
  assert.equal(captionText(adopted), '你好呀。');
  assert.equal(originalCaptionText(adopted), '你好。');
  assert.equal(refinedCandidate(adopted), null);
  assert.match(captionSourceNote(adopted), /复核已采用/);
});

test('a rejected refinement stays a candidate with its reason', () => {
  const rejected: SubtitleEvent = {...source('rejected'), en: 'Hello.', translate_ms: 640,
    translation_status: 'completed', translation_source_text: '你好。', refined_zh: '你好 3 次。',
    translation_source_reason: 'quantity_conflict',
    translation_source_audit: {selected: 'online', reason: 'quantity_conflict',
      issues: ['refinement_quantity_disagreement'], changed_terms: ['跑分']}};
  assert.equal(captionText(rejected), '你好。');
  assert.equal(originalCaptionText(rejected), null);
  assert.equal(refinedCandidate(rejected), '你好 3 次。');
  assert.match(captionSourceNote(rejected), /复核未采用（quantity_conflict）/);
  assert.match(captionSourceNote(rejected), /refinement_quantity_disagreement/);
  assert.match(captionSourceNote(rejected), /跑分/);
});

test('a late refinement does not replace the translation source', () => {
  const events: SubtitleEvent[] = [];
  upsertSubtitle(events, {...source('session:1'), en: 'Hello.', translate_ms: 640,
    translation_status: 'completed', translation_source_text: '你好。',
    translation_source_audit: {selected: 'online', reason: 'refinement_timeout', issues: []}});
  upsertSubtitle(events, {...events[0], revision: 1, refinement_status: 'completed',
    refined_zh: '你好呀。'});
  assert.equal(events.length, 1);
  assert.equal(captionText(events[0]), '你好。');
  assert.equal(refinedCandidate(events[0]), '你好呀。');
});

test('speech admission status remains independent of completed translation', () => {
  const caption: SubtitleEvent = {...source('speech'), en: 'Hello.', translation_status: 'completed',
    speech_submission_status: 'retrying', speech_submission_attempts: 2, speech_error: 'Timeout'};
  assert.equal(captionSpeechStatus(caption), '播报投递重试中，已尝试 2 次');
  assert.equal(caption.en, 'Hello.');
  assert.equal(caption.translation_status, 'completed');
  assert.equal(captionSpeechStatus({...caption, speech_submission_status: 'submitted'}), '语音服务已受理');
  assert.equal(captionSpeechStatus({...caption, speech_submission_status: 'failed'}), '播报投递失败');
  assert.equal(captionSpeechStatus({...caption, speech_submission_status: 'skipped'}), '播报请求已跳过');
  assert.equal(captionSpeechStatus({...caption, speech_submission_status: 'queued'}), '播报请求已排队');
  assert.equal(captionSpeechStatus({...caption, speech_submission_status: 'sending'}), '正在投递播报请求');
  assert.equal(captionSpeechStatus(source('legacy')), '');
  assert.equal(captionSpeechStatus(null), '');
});

test('legacy events fall back to the translation request timing', () => {
  assert.deepEqual(captionTiming({...source('legacy'), translate_ms: 900}),
    {label: '翻译 900 ms', title: '翻译请求：900 ms'});
  assert.equal(captionTiming(null), null);
  assert.equal(captionTiming(source('untimed')), null);
});

test('timing prefers the ASR-to-translation total and lists its segments', () => {
  const timed: SubtitleEvent = {...source('timed'), translate_ms: 640,
    translation_after_asr_ms: 1900, translation_queue_wait_ms: 120,
    translation_refinement_wait_ms: 1100, translation_attempts: 2};
  const timing = captionTiming(timed)!;
  assert.equal(timing.label, 'ASR 后 1900 ms');
  assert.match(timing.title, /排队：120 ms/);
  assert.match(timing.title, /复核等待：1100 ms/);
  assert.match(timing.title, /翻译请求：640 ms/);
  assert.match(timing.title, /ASR 后总计：1900 ms/);
  assert.match(timing.title, /翻译尝试：2 次/);
});

test('a disk-restored caption shows an unknown queue wait as unknown', () => {
  const restored: SubtitleEvent = {...source('restored'), translate_ms: 500,
    translation_queue_wait_ms: null, translation_after_asr_ms: null, translation_attempts: 1};
  const timing = captionTiming(restored)!;
  assert.equal(timing.label, '翻译 500 ms');
  assert.match(timing.title, /排队等待：未知（磁盘恢复）/);
  assert.doesNotMatch(timing.title, /排队：0 ms/);
  assert.doesNotMatch(timing.title, /翻译尝试/);
  assert.doesNotMatch(timing.title, /ASR 后总计/);
});
