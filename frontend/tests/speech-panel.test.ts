import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, createSSRApp } from 'vue';
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
const ready = () => ({ engine: 'pocket', runtime_ready: true, runtime_state: 'ready',
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
  assert.deepEqual(posts, [{ enabled: true, voice_id: 'second', language: 'en', playback: true }]);
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
      assert.deepEqual(JSON.parse(String(init.body)), {});
      status = { ...status, runtime_state: 'queued' };
    }
    return new Response(JSON.stringify(status));
  };
  t.after(() => { globalThis.fetch = previousFetch; });
  const { app, bindings, html } = mount(await component(), {});
  t.after(() => app.unmount());
  await tick();
  assert.match(await html(), /准备／恢复语音/);
  assert.match(await html(), /disabled[^>]*>Generate preview/);
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
  assert.doesNotMatch(output, /disabled[^>]*>Generate preview/);
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
      if (path === '/api/speech/stop') status = { ...status, runtime_ready: false, runtime_state: 'stopped', runtime_error: 'Speech stopped' };
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
  assert.doesNotMatch(await html(), /disabled[^>]*>Generate preview/);
  await bindings.synthesize();
  assert.deepEqual(posts, [
    { path: '/api/speech/stop', body: {} },
    { path: '/api/speech/prepare', body: {} },
    { path: '/api/speech/jobs', body: { text: 'Keep this preview after stopping', language: 'en', voice_id: 'second', playback: false } },
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
