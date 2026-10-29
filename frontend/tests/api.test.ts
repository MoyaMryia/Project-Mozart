import assert from 'node:assert/strict';
import test from 'node:test';
import { api, ApiRequestError } from '../src/api.ts';

function mockFetch(t: any, fn: typeof fetch) {
  const previous = globalThis.fetch;
  globalThis.fetch = fn;
  t.after(() => { globalThis.fetch = previous; });
}

test('HTTP rejection preserves the server reason in a request error', async t => {
  mockFetch(t, async () => new Response(JSON.stringify({ error: 'Permission denied' }), { status: 403 }));
  await assert.rejects(api('/api/test'), { name: 'ApiRequestError', message: 'Permission denied' });
});

test('invalid JSON is a response error with its parsing cause', async t => {
  mockFetch(t, async () => new Response('<html>Unavailable</html>', { status: 502 }));
  await assert.rejects(api('/api/test'), error => {
    assert.ok(error instanceof ApiRequestError);
    assert.ok(error.cause instanceof SyntaxError);
    assert.match(error.message, /HTTP 502/);
    return true;
  });
});

test('transport TypeErrors are request failures', async t => {
  const cause = new TypeError('Failed to fetch');
  mockFetch(t, async () => { throw cause; });
  await assert.rejects(api('/api/test'), error => {
    assert.ok(error instanceof ApiRequestError);
    assert.equal(error.cause, cause);
    return true;
  });
});

test('programming errors propagate unchanged', async t => {
  const cause = new ReferenceError('Missing request implementation');
  mockFetch(t, async () => { throw cause; });
  await assert.rejects(api('/api/test'), error => error === cause);
});

test('explicit cancellation preserves the abort reason', async t => {
  const controller = new AbortController();
  const reason = new Error('Cancelled by the user');
  controller.abort(reason);
  mockFetch(t, async (_, init) => { init!.signal!.throwIfAborted(); return new Response('{}'); });
  await assert.rejects(api('/api/test', { signal: controller.signal }), error => error === reason);
});

test('malformed HTTP error payloads still reject as HTTP failures', async t => {
  let body: unknown;
  mockFetch(t, async () => new Response(JSON.stringify(body), { status: 500 }));
  for (body of [null, { error: 42 }]) {
    await assert.rejects(api('/api/test'), { name: 'ApiRequestError', message: 'Backend returned HTTP 500' });
  }
});
