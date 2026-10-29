import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, createSSRApp, watch } from 'vue';
import ts from 'typescript';
import { renderToString } from '@vue/server-renderer';

async function component() {
  const filename = new URL('../src/SpeechPanel.vue', import.meta.url);
  const { descriptor } = parse(await readFile(filename, 'utf8'), { filename: filename.pathname });
  const script = compileScript(descriptor, { id: 'speech-draft-test' });
  const javascript = ts.transpileModule(script.content, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
  } }).outputText.replace(/from ["']vue["']/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`)
    .replace(/from ["']\.\/(api|poll|stack|speech)["']/g,
      (_, name) => `from ${JSON.stringify(new URL(`../src/${name}.ts`, import.meta.url).href)}`);
  const result = (await import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)).default;
  const template = compileTemplate({ source: descriptor.template!.content, filename: filename.pathname,
    id: 'speech-draft-test', compilerOptions: { bindingMetadata: script.bindings } });
  const templateJavascript = ts.transpileModule(template.code, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
  } }).outputText.replace(/from ["']vue["']/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`);
  result.templateRender = (await import(`data:text/javascript;base64,${Buffer.from(templateJavascript).toString('base64')}`)).render;
  result.render = () => null;
  return result;
}
function mount(component: any, props: any) {
  const renderer = createRenderer({
    insert() {}, remove() {}, createElement() { return {}; }, createText() { return {}; },
    createComment() { return {}; }, setText() {}, setElementText() {}, parentNode() { return null; },
    nextSibling() { return null; }, patchProp() {},
  });
  const app = renderer.createApp(component, props);
  const instance = app.mount({});
  const bindings = instance.$.setupState;
  return { app, bindings, html: () => renderToString(createSSRApp({
    render: () => component.templateRender({}, [], props, bindings),
  })) };
}
const ready = () => ({ control_generation: 0, engine: 'pocket', runtime_ready: true, runtime_state: 'ready',
  voices: [{ id: 'first', name: 'First' }, { id: 'second', name: 'Second' }], jobs: [], languages: ['en', 'zh'],
  session: { enabled: false, voice_id: 'first', language: 'en', playback: false }, playback_device: 'hardware' });
const tick = () => new Promise(resolve => setImmediate(resolve));

test('real speech form preserves drafts during polling and applies output only on explicit action', async t => {
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { value: { setInterval }, configurable: true });
  t.after(() => { if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow); else delete (globalThis as any).window; });
  let status = ready();
  const posts: any[] = [];
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body));
      posts.push(body);
      status = { ...status, session: body };
    }
    return new Response(JSON.stringify(status));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings } = mount(await component(), { stackStatus: { managed: true, available: true,
    phase: 'waiting_for_voice', requirements: { voice_required: true, language: 'en', playback: true } } });
  t.after(() => app.unmount());
  await tick();
  bindings.selectedVoice = 'second';
  bindings.language = 'zh';
  bindings.playback = false;
  bindings.text = 'Keep this edited preview';
  bindings.name = 'Keep this voice name';
  status = { ...status, session: { enabled: true, voice_id: 'first', language: 'en', playback: true } };
  await bindings.refresh(true);
  assert.equal(bindings.selectedVoice, 'second');
  assert.equal(bindings.language, 'zh');
  assert.equal(bindings.playback, false);
  assert.equal(bindings.text, 'Keep this edited preview');
  bindings.matchOutput();
  assert.equal(bindings.language, 'en');
  assert.equal(bindings.playback, true);
  assert.equal(bindings.selectedVoice, 'second');
  assert.equal(bindings.name, 'Keep this voice name');
  assert.equal(posts.length, 0);
  await bindings.session(true);
  assert.deepEqual(posts, [{ enabled: true, voice_id: 'second', language: 'en', playback: true, expected_control_generation: 0 }]);
});

test('unmount aborts a pending speech status request', async t => {
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { value: { setInterval }, configurable: true });
  t.after(() => { if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow); else delete (globalThis as any).window; });
  const previousFetch = globalThis.fetch;
  let signal!: AbortSignal;
  globalThis.fetch = async (_, init) => new Promise((_, reject) => {
    signal = init!.signal!;
    signal.addEventListener('abort', () => reject(signal.reason), { once: true });
  });
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app } = mount(await component(), {});
  assert.equal(signal.aborted, false);
  app.unmount();
  await tick();
  assert.equal(signal.aborted, true);
});


function mockWindow(t: any) {
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { value: { setInterval }, configurable: true });
  t.after(() => { if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow); else delete (globalThis as any).window; });
}

test('unprepared speech prepares once and polling unlocks the actual preview control', async t => {
  mockWindow(t);
  let status = { ...ready(), runtime_ready: false, runtime_state: 'unprepared', runtime_error: null as string | null };
  const paths: string[] = [];
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    if (init?.method === 'POST') {
      paths.push(String(path));
      assert.equal(path, '/api/speech/prepare');
      assert.deepEqual(JSON.parse(String(init.body)), { expected_control_generation: 0 });
      status = { ...status, runtime_state: 'queued' };
    }
    return new Response(JSON.stringify(status));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings, html } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  assert.match(await html(), /准备／恢复语音/);
  assert.match(await html(), /disabled[^>]*>生成试听/);
  await bindings.prepare();
  assert.equal(bindings.preparationPending, true);
  assert.match(await html(), /disabled[^>]*>语音准备已排队/);
  status = { ...status, runtime_state: 'preparing' };
  await bindings.refresh(true);
  assert.match(await html(), /disabled[^>]*>语音预热中/);
  status = { ...status, runtime_ready: true, runtime_state: 'ready', runtime_error: null };
  await bindings.refresh(true);
  const output = await html();
  assert.match(output, /语音已就绪/);
  assert.doesNotMatch(output, /disabled[^>]*>生成试听/);
  assert.doesNotMatch(output, /准备／恢复语音/);
  assert.deepEqual(paths, ['/api/speech/prepare']);
});

test('normal stop retains its reason and prepare restores preview without changing the draft', async t => {
  mockWindow(t);
  let status = { ...ready(), runtime_error: null as string | null };
  const posts: { path: string; body: unknown }[] = [];
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    if (init?.method === 'POST') {
      posts.push({ path: String(path), body: JSON.parse(String(init.body)) });
      if (path === '/api/speech/stop') status = { ...status, control_generation: status.control_generation+1, runtime_ready: false, runtime_state: 'stopped', runtime_error: 'Speech stopped' };
      else if (path === '/api/speech/prepare') status = { ...status, runtime_state: 'preparing', runtime_error: null };
      else assert.equal(path, '/api/speech/jobs');
    }
    return new Response(JSON.stringify(status));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings, html } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  bindings.selectedVoice = 'second';
  bindings.text = 'Keep this preview after stopping';
  await bindings.stop();
  const stopped = await html();
  assert.match(stopped, /语音已停止/);
  assert.match(stopped, /Speech stopped/);
  assert.doesNotMatch(stopped, /语音准备失败/);
  assert.match(stopped, /准备／恢复语音/);
  await bindings.prepare();
  status = { ...status, runtime_ready: true, runtime_state: 'ready' };
  await bindings.refresh(true);
  assert.doesNotMatch(await html(), /disabled[^>]*>生成试听/);
  await bindings.synthesize();
  assert.deepEqual(posts, [
    { path: '/api/speech/stop', body: {} },
    { path: '/api/speech/prepare', body: { expected_control_generation: 1 } },
    { path: '/api/speech/jobs', body: { text: 'Keep this preview after stopping', language: 'en', voice_id: 'second', playback: false, expected_control_generation: 1 } },
  ]);
});

test('closed services and failed worker threads require restart without a prepare button', async t => {
  mockWindow(t);
  let status: any = { ...ready(), runtime_ready: false, runtime_state: 'closed', runtime_error: 'Service closed' };
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify(status));
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings, html } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  assert.match(await html(), /语音服务已关闭，请重启语音服务/);
  assert.doesNotMatch(await html(), /准备／恢复语音/);
  status = { ...status, runtime_state: 'failed', worker_error: { type: 'RuntimeError', message: 'Worker failed', at: 1 } };
  await bindings.refresh(true);
  assert.match(await html(), /语音工作线程已停止，请重启语音服务/);
  assert.doesNotMatch(await html(), /准备／恢复语音/);
});


test('a recoverable preparation failure keeps its cause and permits an explicit retry', async t => {
  mockWindow(t);
  let status = { ...ready(), runtime_ready: false, runtime_state: 'failed', runtime_error: 'Model preparation failed' as string | null };
  const posts: string[] = [];
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    if (init?.method === 'POST') {
      posts.push(String(path));
      status = { ...status, runtime_state: 'queued', runtime_error: null };
    }
    return new Response(JSON.stringify(status));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings, html } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  assert.match(await html(), /语音准备失败/);
  assert.match(await html(), /Model preparation failed/);
  assert.match(await html(), /准备／恢复语音/);
  await bindings.prepare();
  assert.match(await html(), /语音准备已排队/);
  assert.doesNotMatch(await html(), /Model preparation failed/);
  status = { ...status, runtime_ready: true, runtime_state: 'ready' };
  await bindings.refresh(true);
  assert.match(await html(), /语音已就绪/);
  assert.deepEqual(posts, ['/api/speech/prepare']);
});

test('primary controls and preview precede two flat management and history disclosures', async t => {
  mockWindow(t);
  const previousFetch = globalThis.fetch;
  const status = { ...ready(), jobs: Array.from({ length: 9 }, (_, i) => ({
    id: `job-${i}`, text: `Task ${i}`, status: 'completed', result_url: `/audio/${i}`,
  })) };
  globalThis.fetch = async () => new Response(JSON.stringify(status));
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, html } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  const output = await html();
  const disclosures = [...output.matchAll(/<details\b[^>]*>/g)];
  assert.equal(disclosures.length, 2);
  assert.match(output, /<details[^>]*aria-label="音色管理"/);
  assert.match(output, /<details[^>]*aria-label="播报任务"/);
  assert.doesNotMatch(output, /<details[^>]*aria-label="音色试听"/);
  for (const disclosure of disclosures) assert.doesNotMatch(disclosure[0], /\bopen\b/);
  const controls = output.slice(0, output.indexOf('<details'));
  assert.match(controls, /启用翻译播报/);
  assert.match(controls, /停止播报并清空队列/);
  assert.match(controls, /speech-preview-text/);
  assert.match(controls, /生成试听/);
  assert.doesNotMatch(controls, /Task [0-9]/);
  assert.equal((output.match(/<audio\b/g) ?? []).length, 6);
});

test('disabling speech remains separate from applying changed voice settings', async t => {
  mockWindow(t);
  let status = { ...ready(), session: { ...ready().session, enabled: true } };
  const posts: unknown[] = [];
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (_, init) => {
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body));
      posts.push(body);
      status = { ...status, session: { ...status.session, ...body } };
    }
    return new Response(JSON.stringify(status));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings, html } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  bindings.selectedVoice = 'second';
  bindings.language = 'zh';
  assert.match(await html(), /禁用翻译播报/);
  assert.match(await html(), /应用音色与输出/);
  await bindings.session(false);
  assert.deepEqual(posts, [{ enabled: false, expected_control_generation: 0 }]);
  assert.equal(status.session.voice_id, 'first');
});

test('stop remains available and aborts an outstanding preview request', async t => {
  mockWindow(t);
  let previewSignal!: AbortSignal;
  const posts: string[] = [];
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    if (init?.method === 'POST') posts.push(String(path));
    if (path === '/api/speech/jobs') return new Promise((_, reject) => {
      previewSignal = init!.signal!;
      previewSignal.addEventListener('abort', () => reject(previewSignal.reason), { once: true });
    });
    return new Response(JSON.stringify(path === '/api/speech/stop' ? { stop_errors: 0 } : ready()));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings, html } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  const preview = bindings.synthesize();
  assert.equal(bindings.busy, true);
  const stopButton = (await html()).match(/<button\b[^>]*>停止播报并清空队列<\/button>/);
  assert.ok(stopButton);
  assert.doesNotMatch(stopButton[0], /\bdisabled\b/);
  await bindings.stop();
  await preview;
  assert.equal(previewSignal.aborted, true);
  assert.equal(bindings.busy, false);
  assert.equal(bindings.error, '');
  assert.deepEqual(posts, ['/api/speech/jobs', '/api/speech/stop']);
});

test('a late cancelled file read cannot release a newer action or upload a voice', async t => {
  mockWindow(t);
  const previousReader = Object.getOwnPropertyDescriptor(globalThis, 'FileReader');
  let finishRead!: () => void;
  class DelayedReader {
    result = 'data:audio/wav;base64,AAAA';
    onload!: () => void;
    readAsDataURL() { finishRead = () => this.onload(); }
  }
  Object.defineProperty(globalThis, 'FileReader', { value: DelayedReader, configurable: true });
  t.after(() => { if (previousReader) Object.defineProperty(globalThis, 'FileReader', previousReader); else delete (globalThis as any).FileReader; });
  let finishPrepare!: () => void;
  const posts: string[] = [];
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    init?.signal?.throwIfAborted();
    if (init?.method === 'POST') posts.push(String(path));
    if (path === '/api/speech/prepare') return new Promise<Response>(resolve => {
      finishPrepare = () => resolve(new Response('{}'));
    });
    return new Response(JSON.stringify(path === '/api/speech/stop' ? { stop_errors: 0 } : ready()));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  bindings.reference = { name: 'voice.wav', size: 4 };
  const upload = bindings.upload();
  await bindings.stop();
  const preparing = bindings.prepare();
  finishRead();
  await upload;
  assert.equal(bindings.busy, true);
  assert.deepEqual(posts, ['/api/speech/stop', '/api/speech/prepare']);
  finishPrepare();
  await preparing;
  assert.equal(bindings.busy, false);
});

test('partial stop failure remains visible above collapsed settings', async t => {
  mockWindow(t);
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path) => new Response(JSON.stringify(
    path === '/api/speech/stop' ? { stopped: true, stop_errors: 1 } : ready()));
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings, html } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  await bindings.stop();
  const output = await html();
  assert.match(output.slice(0, output.indexOf('<details')), /停止操作发生错误/);
});

test('stop request failures keep their reason and permit a retry', async t => {
  mockWindow(t);
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path) => {
    if (path === '/api/speech/stop') throw new TypeError('Failed to fetch');
    return new Response(JSON.stringify(ready()));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  await bindings.stop();
  assert.equal(bindings.error, 'Failed to fetch');
  assert.equal(bindings.stopping, false);
});

test('unexpected stop errors propagate instead of becoming request warnings', async t => {
  mockWindow(t);
  const cause = new ReferenceError('Missing stop implementation');
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path) => {
    if (path === '/api/speech/stop') throw cause;
    return new Response(JSON.stringify(ready()));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  await assert.rejects(bindings.stop(), error => error === cause);
  assert.equal(bindings.stopping, false);
  assert.equal(bindings.error, '');
});

test('internal action errors propagate instead of becoming request warnings', async t => {
  mockWindow(t);
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify(ready()));
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  for (const cause of [new TypeError('Invalid internal state'), new DOMException('Unexpected DOM state', 'InvalidStateError')]) {
    await assert.rejects(bindings.action(async () => { throw cause; }), error => error === cause);
  }
  assert.equal(bindings.busy, false);
  assert.equal(bindings.error, '');
});

test('a reference file read failure remains queryable without uploading', async t => {
  mockWindow(t);
  const previousReader = Object.getOwnPropertyDescriptor(globalThis, 'FileReader');
  class FailedReader {
    error = new DOMException('Reference file unavailable', 'NotReadableError');
    onerror!: () => void;
    readAsDataURL() { this.onerror(); }
  }
  Object.defineProperty(globalThis, 'FileReader', { value: FailedReader, configurable: true });
  t.after(() => { if (previousReader) Object.defineProperty(globalThis, 'FileReader', previousReader); else delete (globalThis as any).FileReader; });
  const previousFetch = globalThis.fetch;
  const posts: string[] = [];
  globalThis.fetch = async (path, init) => {
    if (init?.method === 'POST') posts.push(String(path));
    return new Response(JSON.stringify(ready()));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  bindings.reference = { name: 'voice.wav', size: 4 };
  await bindings.upload();
  assert.equal(bindings.error, 'Reference file unavailable');
  assert.equal(bindings.busy, false);
  assert.deepEqual(posts, []);
});


test('completed preview stays beside its button instead of inside history', async t => {
  mockWindow(t);
  const job = { id: 'preview', status: 'completed', text: 'Preview.', result_url: '/preview.wav' };
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => new Response(JSON.stringify(
    init?.method === 'POST' ? job : { ...ready(), jobs: [job] }));
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings, html } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  await bindings.synthesize();
  const output = await html();
  const visible = output.slice(0, output.indexOf('<details'));
  assert.match(visible, /aria-label="试听结果"/);
  assert.match(visible, /<audio[^>]*src="\/preview.wav"/);
  assert.match(visible, /已完成/);
  assert.equal(bindings.recentJobs.length, 0);
});

test('preview outside the recent job window is queried by its own ID', async t => {
  mockWindow(t);
  const calls: string[] = [];
  const job = { id: 'preview', status: 'completed', result_url: '/preview.wav' };
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    calls.push(String(path));
    return new Response(JSON.stringify(init?.method === 'POST' ? { ...job, status: 'queued' }
      : path === '/api/speech/jobs/preview' ? job : ready()));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  await bindings.synthesize();
  assert.ok(calls.includes('/api/speech/jobs/preview'));
  assert.equal(bindings.previewJob.status, 'completed');
  assert.equal(bindings.state.runtime_ready, true);
});

test('missing preview does not hide the available speech service', async t => {
  mockWindow(t);
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    if (path === '/api/speech/jobs/preview') return new Response(JSON.stringify({ error: 'Preview no longer available' }), { status: 404 });
    return new Response(JSON.stringify(init?.method === 'POST' ? { id: 'preview', status: 'queued' } : ready()));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings, html } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  await bindings.synthesize();
  assert.equal(bindings.state.runtime_ready, true);
  assert.equal(bindings.unavailable, '');
  assert.equal(bindings.previewError, 'Preview no longer available');
  assert.match(await html(), /role="alert"[^>]*>Preview no longer available/);
});

test('a previous preview response cannot replace the newly submitted preview', async t => {
  mockWindow(t);
  const oldJob = { id: 'old', status: 'completed', result_url: '/old.wav' };
  const newJob = { id: 'new', status: 'completed', result_url: '/new.wav' };
  let holdOld = false, submitted = 0;
  let finishOld!: (response: Response) => void;
  let finishNew!: (response: Response) => void;
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    if (init?.method === 'POST') {
      if (++submitted === 1) return new Response(JSON.stringify(oldJob));
      return new Promise<Response>(resolve => { finishNew = resolve; });
    }
    if (path === '/api/speech/jobs/old') {
      if (holdOld) return new Promise<Response>(resolve => { finishOld = resolve; });
      return new Response(JSON.stringify(oldJob));
    }
    return new Response(JSON.stringify(path === '/api/speech/jobs/new' ? newJob : ready()));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  await bindings.synthesize();
  holdOld = true;
  const stalePoll = bindings.refresh(true);
  await tick();
  const wrongOwners: string[] = [];
  const stopWatching = watch(() => bindings.previewJob?.id, id => {
    if (bindings.previewId === 'new' && id === 'old') wrongOwners.push(id);
  }, { flush: 'sync' });
  t.after(stopWatching);
  const current = bindings.synthesize();
  await tick();
  finishOld(new Response(JSON.stringify(oldJob)));
  finishNew(new Response(JSON.stringify(newJob)));
  await Promise.all([stalePoll, current]);
  assert.deepEqual(wrongOwners, []);
  assert.equal(bindings.previewJob.id, 'new');
});

test('cancelled preview keeps its old generation while recovery uses the new generation', async t => {
  mockWindow(t);
  let status = ready();
  let finishAdmission!: () => void;
  let admitted = 0;
  const posts: { path: string; body: any }[] = [];
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body));
      posts.push({ path: String(path), body });
      if (path === '/api/speech/jobs') return new Promise<Response>(resolve => {
        finishAdmission = () => {
          if (body.expected_control_generation === status.control_generation) admitted++;
          resolve(new Response(JSON.stringify({ error: 'Speech was stopped' }), { status: 409 }));
        };
      });
      if (path === '/api/speech/stop') status = { ...status, control_generation: status.control_generation+1,
        runtime_ready: false, runtime_state: 'stopped' };
    }
    return new Response(JSON.stringify(status));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  const preview = bindings.synthesize();
  await bindings.stop();
  finishAdmission(); await preview;
  assert.equal(admitted, 0);
  assert.equal(posts[0].body.expected_control_generation, 0);
  assert.equal(bindings.previewId, '');
  assert.equal(bindings.state.control_generation, 1);
  await bindings.prepare();
  assert.equal(posts.at(-1)!.body.expected_control_generation, 1);
});

test('legacy status without a generation preserves the existing request contract', async t => {
  mockWindow(t);
  const status: any = ready(); delete status.control_generation;
  const previousFetch = globalThis.fetch;
  let body: unknown;
  globalThis.fetch = async (_, init) => {
    if (init?.method === 'POST') body = JSON.parse(String(init.body));
    return new Response(JSON.stringify(status));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  await bindings.session(false);
  assert.deepEqual(body, { enabled: false });
});

test('voice management opens when the voice list is empty', async t => {
  mockWindow(t);
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ ...ready(), voices: [] }));
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, html } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  assert.match(await html(), /<details[^>]*aria-label="音色管理"[^>]*\bopen\b/);
});
