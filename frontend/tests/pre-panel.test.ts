import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { parse, compileScript } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import ts from 'typescript';

const status = (capture = 'a', playback = 'p', devices?: any) => ({ available: true, current: { capture, playback },
  devices: devices ?? { capture: ['a', 'b', 'c'].map(id => ({ id, label: id })),
    playback: ['p', 'q'].map(id => ({ id, label: id })) } });
async function panel(t: any, devices?: any) {
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
  const props = reactive<any>({ status: status('a', 'p', devices), busy: false, error: '' });
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

const usbDevices = {
  capture: [{ id: 'usb-in', label: 'USB 协议 / 管线麦克风', group: 'usb' },
    { id: 'a', label: 'a', group: 'local' }],
  playback: [{ id: 'p', label: 'p', group: 'local' }],
};

test('usb devices are grouped apart from local cards', async t => {
  const { bindings } = await panel(t, usbDevices);
  const groups = bindings.captureGroups;
  assert.deepEqual(groups.map((group: any) => group.label), ['USB 协议', '本机声卡']);
  assert.deepEqual(groups[0].devices.map((device: any) => device.id), ['usb-in']);
});

test('devices without a group fall back to the local card group', async t => {
  const { bindings } = await panel(t);
  assert.deepEqual(bindings.captureGroups.map((group: any) => group.label), ['本机声卡']);
});

test('bridge note reports counters while the bridge runs', async t => {
  const { bindings } = await panel(t, { ...usbDevices,
    bridge: { active: true, received: 12, dropped: 1 } });
  bindings.capture = 'usb-in'; await nextTick();
  assert.match(bindings.bridgeNote, /收 12 帧/);
  assert.match(bindings.bridgeNote, /丢 1/);
});

test('bridge note warns with the reported reason when the bridge is down', async t => {
  const { bindings } = await panel(t, { ...usbDevices,
    bridge: { active: false, reason: 'USB 链路未就绪' } });
  bindings.capture = 'usb-in'; await nextTick();
  assert.match(bindings.bridgeNote, /USB 桥未运行/);
  assert.match(bindings.bridgeNote, /USB 链路未就绪/);
});

test('bridge note stays hidden for local devices', async t => {
  const { bindings } = await panel(t, { ...usbDevices,
    bridge: { active: false, reason: '桥状态超过刷新窗口' } });
  assert.equal(bindings.bridgeNote, '');
});
