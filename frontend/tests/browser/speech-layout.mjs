import assert from 'node:assert/strict';
import http from 'node:http';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { once } from 'node:events';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
const out = path.resolve(process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), 'mozart-speech-layout-')));
fs.mkdirSync(out, { recursive: true });
const root = path.resolve(fileURLToPath(new URL('../../dist/', import.meta.url)));
const binary = process.env.MOZART_TEST_CHROMIUM;
if (!binary) throw new Error('请用 MOZART_TEST_CHROMIUM 指定 Chromium 可执行文件。');
fs.accessSync(binary, fs.constants.X_OK);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const posts = [], held = [], exceptions = [];
let speechOnly = false, holdPreview = false, runtimeReady = true, modelsFail = false;
let mockPreview = null, mode = 'idle', fileAvailable = true, controlGeneration = 0, stopFailure = '';
const session = { enabled: true, voice_id: 'first', language: 'en', playback: true };
const caption = { utterance_id: 'mock:1', seq: 1, zh: '这是本地模拟的长句字幕，用于检查长句能否完整显示以及正文是否容易阅读。'.repeat(6), en: 'This is a local simulated translation for checking subtitle readability.', translation_status: 'completed', speech_submission_status: 'submitted', speech: 'mock-job', ts: '12:00:00' };
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const route = url.pathname;
  let data = '', body;
  for await (const part of req) data += part;
  if (data) body = JSON.parse(data);
  if (req.method === 'POST') posts.push({ route, body });
  if (route === '/api/speech/stop' && stopFailure === 'http') {
    res.writeHead(503, { 'Content-Type': 'application/json' }); return res.end(JSON.stringify({ error: 'Mock speech stop unavailable' }));
  }
  if (route === '/api/speech/jobs' && req.method === 'POST' && holdPreview) { held.push(res); return; }
  if (route === '/api/speech/jobs' && req.method === 'POST') mockPreview = { id: 'mock-preview', text: body.text, status: 'completed', result_url: '/mock-preview.wav', duration_seconds: 2 };
  if (route === '/api/speech/session' && body) Object.assign(session, body);
  if (route === '/api/speech/stop') { session.enabled = false; runtimeReady = false; controlGeneration++; }
  if (route === '/api/mode/switch' && body) mode = body.mode;
  if (route === '/api/subtitles') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
    res.write(`data: ${JSON.stringify(caption)}\n\n`);
    const timer = setInterval(() => res.write(': keepalive\n\n'), 2000);
    res.on('close', () => clearInterval(timer));
    return;
  }
  const routes = {
    '/api/status': { mode, pending_target_mode: null, queue: [], active_model_id: 'model', worker_running: false, last_error: '', capabilities: { rt_rvc: !speechOnly, file_rvc: !speechOnly && fileAvailable }, file_queue_paused: false, model: { has_index: 'false' } },
    '/api/stack/status': { managed: true, available: true, phase: 'running', state: 'running', readiness: { translation: true, speech_runtime: true, voice_session: true }, requirements: { voice_required: true, language: 'en', playback: true } },
    '/api/preprocessor': { available: true, current: { capture: 'capture-a', playback: 'output-a' }, devices: { capture: [{ id: 'capture-a', label: '麦克风 A' }, { id: 'capture-b', label: '麦克风 B' }], playback: [{ id: 'output-a', label: '扬声器 A' }] }, state: { state: 'running', frames: 1200 } },
    '/api/models': { models: [{ id: 'model', exists: true, current: true }] },
    '/api/parameters': { f0_method: 'rmvpe', pitch_shift: 0, index_rate: .75, filter_radius: 3, rms_mix_rate: .25, protect: .33 },
    '/api/presets': { presets: [] }, '/api/logs': { entries: [] }, '/api/glossary': { terms: { '原神': 'Genshin Impact' } },
    '/api/speech/status': { control_generation: controlGeneration, engine: 'zipvoice', runtime_ready: runtimeReady, runtime_state: runtimeReady ? 'ready' : 'stopped', voices: [{ id: 'first', name: '音色 A', status: 'ready' }, { id: 'second', name: '音色 B', status: 'ready' }], jobs: [...(mockPreview ? [mockPreview] : []), ...Array.from({ length: 6 }, (_, i) => ({ id: 'job-'+i, text: 'A completed translated speech task.', status: 'completed', result_url: '/mock.wav', duration_seconds: 2, synthesis_seconds: .5 }))], languages: ['en', 'zh'], queue_limit: 8, delivery_policy: 'coverage', pending_jobs: 0, playback_device: 'mock-device', session },
    '/api/speech/session': {}, '/api/speech/jobs': mockPreview, '/api/speech/jobs/mock-preview': mockPreview, '/api/speech/stop': { stopped: true, stop_errors: stopFailure === 'partial' ? 1 : 0 }
  };
  if (route.startsWith('/api/')) {
    const fail = route === '/api/monitor' || (modelsFail && route === '/api/models');
    res.writeHead(fail ? 503 : 200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify(fail ? { error: 'Mock model list unavailable' } : routes[route] ?? {}));
  }
  const file = path.join(root, route === '/' ? 'index.html' : route);
  if (!file.startsWith(root + '/') || !fs.existsSync(file)) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' });
  res.end(fs.readFileSync(file));
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'mozart-layout-chromium-'));
const chrome = spawn(binary, ['--headless', '--no-sandbox', '--disable-gpu', '--disable-background-networking', '--disable-component-update', '--disable-sync', '--no-first-run', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { stdio: ['ignore', 'ignore', fs.openSync(path.join(out, 'chrome.log'), 'w')] });
let socket;
try {
  const portFile = path.join(profile, 'DevToolsActivePort');
  for (let i = 0; !fs.existsSync(portFile) && i < 100; i++) await sleep(100);
  const port = fs.readFileSync(portFile, 'utf8').split('\n')[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(p => p.type === 'page');
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise(r => socket.addEventListener('open', r, { once: true }));
  let id = 0; const pending = new Map();
  socket.addEventListener('message', ({ data }) => {
    const m = JSON.parse(data);
    if (m.id) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result); }
    if (m.method === 'Runtime.exceptionThrown') exceptions.push(m.params.exceptionDetails.text);
  });
  const call = (method, params = {}) => new Promise((resolve, reject) => { pending.set(++id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params })); });
  const evaluate = async expression => { const r = await call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails)); return r.result.value; };
  const buttons = `Array.from(document.querySelectorAll('#reference-speech button'))`;
  const snapshot = () => evaluate(`({ subtitle: (() => { const e=document.querySelector('#subtitle-panel'); const p=e.querySelector('.whitespace-pre-wrap'); return {box:e.getBoundingClientRect().toJSON(),font:getComputedStyle(p).fontSize,clipped:p.scrollWidth>p.clientWidth}; })(), log: (()=>{const e=document.querySelector('#log-panel');return {box:e.getBoundingClientRect().toJSON(),hiddenContentHeight:e.clientHeight-e.querySelector('summary').offsetHeight};})(), horizontalOverflow:document.documentElement.scrollWidth>innerWidth })`);
  const capture = async name => { const r = await call('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(path.join(out, name+'.png'), Buffer.from(r.data, 'base64')); };
  await call('Runtime.enable');
  await call('Network.enable');
  await call('Network.setBlockedURLs', { urls: ['https://*'] });
  await call('Emulation.setDeviceMetricsOverride', { width: 1366, height: 768, deviceScaleFactor: 1, mobile: false });
  await call('Page.navigate', { url: base }); await sleep(1200);
  const visibleControls = () => evaluate(`Array.from(document.querySelectorAll('#reference-speech [aria-label="播报控制"] button')).map(e=>{const r=e.getBoundingClientRect();return {text:e.textContent.trim(),visible:r.top>=0 && r.bottom<=innerHeight && document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.closest('button')===e}})`);
  const evidence = { base, browser: (await call('Browser.getVersion')).product, configuration: 'Local mock API, Chromium, no model or audio services', desktop: await snapshot(), desktopControls: await visibleControls() };
  await capture('desktop-after');
  evidence.closedDisclosures = await evaluate(`Array.from(document.querySelectorAll('#reference-speech details,#log-panel')).map(e=>({label:e.querySelector('summary').textContent.trim(),open:e.open}))`);
  await evaluate(`(()=>{const e=document.querySelector('#pre-capture');e.value='capture-b';e.dispatchEvent(new Event('change',{bubbles:true}));})()`); await sleep(80);
  evidence.deviceDraftBeforePoll = await evaluate(`document.querySelector('#pre-capture').value`);
  await sleep(5100);
  evidence.deviceDraftAfterPoll = await evaluate(`document.querySelector('#pre-capture').value`);
  evidence.initialSessionAction = await evaluate(`${buttons}.find(b=>b.textContent.includes('禁用翻译播报'))?.textContent.trim()`);
  await evaluate(`(()=>{const s=document.querySelector('#reference-speech select'); const all=Array.from(document.querySelectorAll('#reference-speech select')); const voice=all.find(e=>Array.from(e.options).some(o=>o.value==='second'));voice.value='second';voice.dispatchEvent(new Event('change',{bubbles:true}));})()`); await sleep(80);
  evidence.changedSessionAction = await evaluate(`${buttons}.find(b=>b.textContent.includes('应用音色与输出'))?.textContent.trim()`);
  evidence.disableStillAvailableAfterDraftChange = await evaluate(`${buttons}.some(b=>b.textContent.trim()==='禁用翻译播报')`);
  await evaluate(`${buttons}.find(b=>b.textContent.includes('生成试听')).click()`); await sleep(150);
  evidence.inlinePreview = await evaluate(`(()=>{const e=document.querySelector('[aria-label="试听结果"] audio');const r=e.getBoundingClientRect();return {outsideDetails:!e.closest('details'),visible:r.top>=0&&r.bottom<=innerHeight&&document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===e}})()`);
  evidence.flatLayout = await evaluate(`({nestedDisclosures:document.querySelectorAll('details details').length,modeToggles:document.querySelectorAll('[id^="toggle-"]').length,referenceLinks:document.querySelectorAll('a[href="#reference-speech"]').length,speechHeadings:document.querySelectorAll('#reference-speech h2').length,logSubheadings:document.querySelectorAll('#log-panel h2').length})`);
  await capture('inline-preview');
  const switchesBeforeSelection = posts.filter(post=>post.route==='/api/mode/switch').length;
  await evaluate(`document.querySelector('button[data-mode="file_rvc"]').click()`); await sleep(50);
  evidence.modeSelectionDoesNotStart = posts.filter(post=>post.route==='/api/mode/switch').length === switchesBeforeSelection;
  await evaluate(`document.querySelector('[aria-label="启动 RVC 处理"]').click()`); await sleep(100);
  evidence.startUsesSelectedMode = posts.some(post=>post.route==='/api/mode/switch'&&post.body.mode==='file_rvc');
  await evaluate(`document.querySelector('button[data-mode="rt_rvc"]').click()`); await sleep(50);
  holdPreview = true;
  await evaluate(`${buttons}.find(b=>b.textContent.includes('生成试听')).click()`); await sleep(150);
  evidence.stopDisabledDuringPreview = await evaluate(`${buttons}.find(b=>b.textContent.includes('停止播报并清空队列')).disabled`);
  await capture('preview-busy-stop-disabled');
  await evaluate(`${buttons}.find(b=>b.textContent.includes('停止播报并清空队列')).click()`); await sleep(150);
  evidence.previewRequestAbortedByStop = held.length === 1 && held[0].destroyed;
  evidence.stopRequestSent = posts.some(post=>post.route==='/api/speech/stop');
  speechOnly = true; holdPreview = false; runtimeReady = true; session.enabled = true; mode = 'idle';
  await call('Page.reload'); await sleep(1000);
  await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  evidence.mobile = await snapshot(); evidence.mobileControls = await visibleControls(); await capture('mobile-after');
  evidence.speechOnlySidebarCount = await evaluate(`document.querySelectorAll('.left-sidebar').length`);
  evidence.speechOnlyFileUploaderHidden = await evaluate(`document.querySelector('input[accept="audio/*"]').getBoundingClientRect().height===0`);
  evidence.glossaryCount = await evaluate(`document.querySelectorAll('[aria-label="术语表"]').length`);
  evidence.mobileConnectionVisible = await evaluate(`(()=>{const e=document.querySelector('[data-status="api"]');const r=e.getBoundingClientRect();return r.x>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;})()`);
  await evaluate(`(()=>{const d=document.querySelector('details[aria-label="播报任务"]');d.open=true;d.querySelectorAll('audio')[5].scrollIntoView({block:'end'});})()`);
  await sleep(250);
  evidence.controlsAfterHistoryScroll = await visibleControls();

  await capture('history-controls');
  speechOnly = false; fileAvailable = false;
  await call('Page.reload'); await sleep(1000);
  evidence.unsupportedModeDisabled = await evaluate(`(()=>{const e=document.querySelector('button[data-mode="file_rvc"]');e.click();return e.disabled&&e.getAttribute('aria-pressed')==='false';})()`);
  stopFailure = 'http';
  await evaluate(`document.querySelector('[aria-label="停止 RVC 和语音播报"]').click()`); await sleep(150);
  evidence.globalStopHttpFailureVisible = await evaluate(`document.body.innerText.includes('播报停止请求失败：Mock speech stop unavailable')`);
  stopFailure = 'partial';
  await evaluate(`document.querySelector('[aria-label="停止 RVC 和语音播报"]').click()`); await sleep(150);
  evidence.globalStopPartialFailureVisible = await evaluate(`document.body.innerText.includes('播报停止操作部分失败')`);
  stopFailure = ''; speechOnly = true; modelsFail = true;
  await call('Page.reload'); await sleep(1000);
  evidence.globalErrorVisible = await evaluate(`Array.from(document.querySelectorAll('[role="alert"]')).some(e=>e.textContent.includes('Mock model list unavailable')&&e.getBoundingClientRect().height>0)`);
  evidence.exceptions = exceptions; evidence.posts = posts;
  assert.deepEqual(evidence.desktopControls, [
    {text:'禁用翻译播报',visible:true}, {text:'停止播报并清空队列',visible:true},
  ]);
  assert.deepEqual(evidence.mobileControls, evidence.desktopControls);
  assert.deepEqual(evidence.controlsAfterHistoryScroll, evidence.desktopControls);
  assert.equal(evidence.closedDisclosures.length, 3);
  assert.deepEqual(evidence.flatLayout, {nestedDisclosures:0,modeToggles:0,referenceLinks:0,speechHeadings:1,logSubheadings:0});
  assert.deepEqual(evidence.inlinePreview, {outsideDetails:true,visible:true});
  assert.equal(evidence.speechOnlySidebarCount, 0);
  assert.equal(evidence.speechOnlyFileUploaderHidden, true);
  assert.equal(evidence.glossaryCount, 1);
  assert.equal(evidence.globalErrorVisible, true);
  assert.equal(evidence.globalStopHttpFailureVisible, true);
  assert.equal(evidence.globalStopPartialFailureVisible, true);
  assert.equal(evidence.deviceDraftBeforePoll, 'capture-b');
  assert.equal(evidence.deviceDraftAfterPoll, 'capture-b');
  assert.equal(evidence.mobileConnectionVisible, true);
  assert.equal(evidence.unsupportedModeDisabled, true);
  assert.equal(evidence.modeSelectionDoesNotStart, true);
  assert.equal(evidence.startUsesSelectedMode, true);
  assert.ok(evidence.closedDisclosures.every(item=>!item.open));
  assert.equal(evidence.desktop.log.hiddenContentHeight, 0);
  assert.equal(evidence.mobile.log.hiddenContentHeight, 0);
  assert.equal(evidence.desktop.subtitle.clipped, false);
  assert.equal(evidence.mobile.subtitle.clipped, false);
  assert.equal(evidence.desktop.horizontalOverflow, false);
  assert.equal(evidence.mobile.horizontalOverflow, false);
  assert.equal(evidence.disableStillAvailableAfterDraftChange, true);
  assert.equal(evidence.stopDisabledDuringPreview, false);
  assert.equal(evidence.previewRequestAbortedByStop, true);
  assert.equal(evidence.stopRequestSent, true);
  assert.deepEqual(exceptions, []);
  fs.writeFileSync(path.join(out, 'evidence.json'), JSON.stringify(evidence, null, 2));
  console.log(JSON.stringify(evidence, null, 2));
} finally {
  socket?.close();
  if (chrome.exitCode === null && chrome.signalCode === null) {
    const exited = once(chrome, 'exit'); chrome.kill('SIGTERM'); await exited;
  }
  for (const res of held) res.destroy();
  server.closeAllConnections(); await new Promise(r => server.close(r));
  fs.rmSync(profile, { recursive: true, force: true });
}
