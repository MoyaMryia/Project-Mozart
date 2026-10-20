import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { parse, compileScript } from '@vue/compiler-sfc';
import { renderToString } from '@vue/server-renderer';
import { createSSRApp } from 'vue';
import ts from 'typescript';
import { voiceWaitingText, outputMatches } from '../src/stack.ts';
import { speechRuntimeLabel } from '../src/speech.ts';
import type { StackStatus } from '../src/api.ts';

async function panel() {
  const filename = new URL('../src/StackPanel.vue', import.meta.url);
  const source = await readFile(filename, 'utf8');
  const { descriptor } = parse(source, { filename: filename.pathname });
  const script = compileScript(descriptor, { id: 'stack-status-test', inlineTemplate: true });
  const javascript = ts.transpileModule(script.content, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
  } }).outputText.replace(/from ["']vue["']/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`)
    .replace(/from ["']\.\/stack["']/g, `from ${JSON.stringify(new URL('../src/stack.ts', import.meta.url).href)}`);
  return (await import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)).default;
}
const waiting: StackStatus = { managed: true, available: true, state: 'starting', phase: 'waiting_for_voice',
  updated_at: 1792468800, requirements: { language: 'en', playback: true, voice_required: true },
  readiness: { translation: true, speech_runtime: true, voice_session: false }, degraded: {} };

test('the actual status panel explains voice selection and required playback', async () => {
  const component = await panel();
  const html = await renderToString(createSSRApp(component, { status: waiting, error: '' }));
  assert.match(html, /等待选择音色/);
  assert.match(html, /请选择参考音色/);
  assert.match(html, /设备播放/);
  assert.match(html, /完成后开始采集/);
  assert.match(html, /记录更新时间/);
  assert.doesNotMatch(html, /开始全部处理|全局启动/);
});

test('WAV requirements keep physical playback disabled instead of relaxing the gate', () => {
  const requirements = { language: 'en', playback: false, voice_required: true };
  assert.match(voiceWaitingText(requirements), /WAV 下载/);
  assert.equal(outputMatches(requirements, 'en', false), true);
  assert.equal(outputMatches(requirements, 'en', true), false);
  assert.equal(outputMatches(requirements, 'zh', false), false);
});

test('unavailable records and real errors never render an old running phase', async () => {
  const component = await panel();
  const html = await renderToString(createSSRApp(component, { status: { ...waiting, state: 'running', phase: 'capturing' },
    error: 'Supervisor status does not match the active instance' }));
  assert.match(html, /现场状态不可用/);
  assert.doesNotMatch(html, /正在采集/);
  const failed = await renderToString(createSSRApp(component, { status: { ...waiting, state: 'failed', phase: 'failed',
    error: { message: 'Output device missing' }, degraded: { speech: 'Warmup failed' } }, error: '' }));
  assert.match(failed, /Output device missing/);
  assert.match(failed, /Warmup failed/);
});

test('legacy or historical runtime information cannot label speech ready', () => {
  assert.equal(speechRuntimeLabel({}), '语音就绪状态未知');
  assert.equal(speechRuntimeLabel({ runtime_ready: false, runtime_state: 'stopped', runtime_error: 'Speech stopped' }), '语音已停止');
  assert.equal(speechRuntimeLabel({ runtime_ready: false, runtime_state: 'stopped', runtime_error: 'Active speech task cancelled' }), '语音已停止');
  assert.equal(speechRuntimeLabel({ runtime_ready: false, runtime_state: 'unprepared' }), '语音待准备');
  assert.equal(speechRuntimeLabel({ runtime_ready: false, runtime_state: 'queued' }), '语音准备已排队');
  assert.equal(speechRuntimeLabel({ runtime_ready: false, runtime_state: 'closed', runtime_error: 'Service closed' }), '语音已关闭');
  assert.equal(speechRuntimeLabel({ runtime_ready: false, runtime_state: 'preparing' }), '语音预热中');
  assert.equal(speechRuntimeLabel({ runtime_ready: false, runtime_error: 'Worker exited' }), '语音准备失败');
  assert.equal(speechRuntimeLabel({ runtime_ready: true, runtime_state: 'ready' }), '语音已就绪');
});
