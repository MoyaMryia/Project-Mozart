import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { parse, compileScript } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import ts from 'typescript';

const status = (capture = 'a', playback = 'p') => ({ available: true, current: { capture, playback },
  devices: { capture: ['a', 'b', 'c'].map(id => ({ id, label: id })),
    playback: ['p', 'q'].map(id => ({ id, label: id })) } });
async function panel(t: any) {
  const filename = new URL('../src/PrePanel.vue', import.meta.url);
  const { descriptor } = parse(await readFile(filename, 'utf8'));
  const script = compileScript(descriptor, { id: 'pre-draft-test' });
  const javascript = ts.transpileModule(script.content, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
  } }).outputText.replace(/from ["']vue["']/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`);
  const component = (await import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)).default;
  component.render = () => null;
  const renderer = createRenderer({ insert() {}, remove() {}, createElement() { return {}; },
    createText() { return {}; }, createComment() { return {}; }, setText() {}, setElementText() {},
    parentNode() { return null; }, nextSibling() { return null; }, patchProp() {} });
  const props = reactive<any>({ status: status(), busy: false, error: '' });
  const applied: unknown[] = [];
  const app = renderer.createApp({ render: () => h(component, { ...props, onApply: (body: unknown) => applied.push(body) }) });
  app.mount({});
  t.after(() => app.unmount());
  return { props, applied, bindings: (app as any)._instance.subTree.component.setupState };
}

test('device polling preserves unapplied capture and output drafts', async t => {
  const { props, bindings, applied } = await panel(t);
  bindings.capture = 'b'; bindings.playback = 'q';
  props.status = status(); await nextTick();
  assert.equal(bindings.capture, 'b'); assert.equal(bindings.playback, 'q');
  bindings.apply();
  assert.deepEqual(applied, [{ capture: 'b', playback: 'q' }]);
});

test('untouched device fields follow changes made by the backend', async t => {
  const { props, bindings } = await panel(t);
  props.status = status('c', 'q'); await nextTick();
  assert.equal(bindings.capture, 'c'); assert.equal(bindings.playback, 'q');
});

test('device draft survives a status outage and reconnect', async t => {
  const { props, bindings } = await panel(t);
  bindings.capture = 'b';
  props.status = null; await nextTick();
  props.status = status('c', 'q'); await nextTick();
  assert.equal(bindings.capture, 'b'); assert.equal(bindings.playback, 'q');
});

test('an applied draft becomes the baseline for subsequent device updates', async t => {
  const { props, bindings } = await panel(t);
  bindings.capture = 'b';
  props.status = status('b'); await nextTick();
  props.status = status('c'); await nextTick();
  assert.equal(bindings.capture, 'c');
});
