import assert from 'node:assert/strict';
import test from 'node:test';
import { subscribeSubtitles } from '../src/subtitle-stream.ts';
import type { SubtitleEvent } from '../src/api.ts';

class Source {
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  close() { this.closed = true; }
}

function fixture() {
  const sources: Source[] = [];
  const captions: SubtitleEvent[] = [];
  const states: string[] = [];
  const stop = subscribeSubtitles(caption => captions.push(caption), state => states.push(state), () => {
    const source = new Source();
    sources.push(source);
    return source as unknown as EventSource;
  });
  return { sources, captions, states, stop };
}

test('a closed HTTP stream reconnects and accepts replayed captions', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const { sources, captions, states, stop } = fixture();
  t.after(stop);
  sources[0].onopen!();
  sources[0].onerror!();
  assert.equal(sources[0].closed, true);
  assert.equal(sources[0].onerror, null);
  assert.equal(states.at(-1), 'offline');
  t.mock.timers.tick(999);
  assert.equal(sources.length, 1);
  t.mock.timers.tick(1);
  assert.equal(sources.length, 2);
  sources[1].onopen!();
  sources[1].onmessage!({ data: '{"seq":1,"zh":"你好","en":"Hello"}' });
  assert.equal(states.at(-1), 'online');
  assert.equal(captions[0].zh, '你好');
});

test('repeated service failures keep only one retry and stop cancels it', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const { sources, stop } = fixture();
  t.after(stop);
  for (let i = 0; i < 3; i++) {
    sources[i].onerror!();
    t.mock.timers.tick(1000);
    assert.equal(sources.length, i + 2);
  }
  sources[3].onerror!();
  stop();
  t.mock.timers.tick(10000);
  assert.equal(sources.length, 4);
  assert.ok(sources.every(source => source.closed));
});

test('stop closes an active stream and malformed JSON does not reach the consumer', () => {
  const { sources, captions, stop } = fixture();
  sources[0].onmessage!({ data: 'not JSON' });
  assert.equal(captions.length, 0);
  stop();
  assert.equal(sources[0].closed, true);
  assert.equal(sources[0].onmessage, null);
});

test('consumer errors are not discarded as malformed packets', () => {
  const source = new Source();
  const stop = subscribeSubtitles(() => { throw new Error('consumer bug'); }, () => {},
    () => source as unknown as EventSource);
  try {
    assert.throws(() => source.onmessage!({ data: '{}' }), /consumer bug/);
  } finally {
    stop();
  }
});
