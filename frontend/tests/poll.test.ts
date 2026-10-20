import assert from 'node:assert/strict';
import test from 'node:test';
import { createPoll } from '../src/poll.ts';

test('overlapping state reads share one request', async () => {
  let calls = 0;
  let resolve!: (value: number) => void;
  const accepted: number[] = [];
  const poll = createPoll(() => { calls++; return new Promise<number>(done => { resolve = done; }); },
    value => accepted.push(value), error => { throw error; });
  const first = poll.run();
  const second = poll.run();
  assert.equal(calls, 1);
  resolve(2);
  await Promise.all([first, second]);
  assert.deepEqual(accepted, [2]);
  poll.stop();
});

test('an action refresh discards an older response and unmount cancels the next request', async () => {
  const requests: { signal: AbortSignal; resolve: (value: string) => void }[] = [];
  const accepted: string[] = [];
  const poll = createPoll(signal => new Promise<string>(resolve => requests.push({ signal, resolve })),
    value => accepted.push(value), error => { throw error; });
  const old = poll.run();
  const fresh = poll.run(true);
  assert.equal(requests[0].signal.aborted, true);
  requests[1].resolve('new session');
  await fresh;
  requests[0].resolve('old session');
  await old;
  assert.deepEqual(accepted, ['new session']);
  const last = poll.run();
  poll.stop();
  assert.equal(requests[2].signal.aborted, true);
  requests[2].resolve('unmounted');
  await last;
  await poll.run();
  assert.equal(requests.length, 3);
  assert.deepEqual(accepted, ['new session']);
});

test('a stuck network read reports its deadline and allows the next poll', async () => {
  const failures: unknown[] = [];
  let calls = 0;
  const poll = createPoll(signal => { calls++; return new Promise<number>((_, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true });
  }); }, () => assert.fail('A timed out response cannot be accepted'), error => failures.push(error), 5);
  await poll.run();
  assert.equal((failures[0] as Error).name, 'TimeoutError');
  await poll.run();
  assert.equal(calls, 2);
  poll.stop();
});


test('an internal state update error remains a rejected operation', async () => {
  let networkFailure = false;
  const poll = createPoll(async () => 1, () => { throw new Error('state update bug'); },
    () => { networkFailure = true; });
  await assert.rejects(poll.run(), /state update bug/);
  assert.equal(networkFailure, false);
  poll.stop();
});
