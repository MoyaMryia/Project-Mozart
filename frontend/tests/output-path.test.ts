import assert from 'node:assert/strict';
import test from 'node:test';
import { OutputPathError, selectOutput, type OutputActions, type SpeechSnapshot } from '../src/output-path.ts';

const PLAYBACK = 'plughw:CARD=Device,DEV=0';

const recorder = () => {
  const calls: string[] = [];
  const actions: OutputActions = {
    setPlayback: async (playback) => { calls.push(`playback:${playback}`); },
    setSpeechPlayback: async (device) => { calls.push(`speech:device:${device}`); },
    setRouting: async (patch) => { calls.push(`routing:${JSON.stringify(patch)}`); },
    stopSpeech: async () => { calls.push('speech:stop'); },
    prepareSpeech: async () => { calls.push('speech:prepare'); },
    enableSpeech: async (voiceId) => { calls.push(`speech:enable:${voiceId}`); },
  };
  return { calls, actions };
};

const speech = (patch: Partial<SpeechSnapshot> = {}): SpeechSnapshot => ({
  sessionEnabled: false, runtimeReady: true, voiceId: 'voice-1', ...patch,
});

test('voice frees the device before the preprocessor restarts on it', async () => {
  const { calls, actions } = recorder();
  await selectOutput('voice', speech({ sessionEnabled: true }), actions, PLAYBACK);
  assert.deepEqual(calls, [
    'speech:stop',
    `playback:${PLAYBACK}`,
    'routing:{"bypass":false,"mic_muted":false}',
  ]);
});

test('voice without an active speech session only restarts the preprocessor', async () => {
  const { calls, actions } = recorder();
  await selectOutput('voice', null, actions, PLAYBACK);
  assert.deepEqual(calls, [`playback:${PLAYBACK}`, 'routing:{"bypass":false,"mic_muted":false}']);
});

test('direct bypasses inference after taking the device', async () => {
  const { calls, actions } = recorder();
  await selectOutput('direct', speech(), actions, PLAYBACK);
  assert.deepEqual(calls, [`playback:${PLAYBACK}`, 'routing:{"bypass":true,"mic_muted":false}']);
});

test('speech hands the device to the speech service before enabling it', async () => {
  const { calls, actions } = recorder();
  await selectOutput('speech', speech(), actions, PLAYBACK);
  assert.deepEqual(calls, ['playback:null', `speech:device:${PLAYBACK}`, 'speech:enable:voice-1']);
});

test('speech prepares a stopped runtime before enabling the session', async () => {
  const { calls, actions } = recorder();
  await selectOutput('speech', speech({ runtimeReady: false }), actions, PLAYBACK);
  assert.deepEqual(calls, ['playback:null', `speech:device:${PLAYBACK}`, 'speech:prepare',
    'speech:enable:voice-1']);
});

test('speech redirects the speech service to the usb bridge pair', async () => {
  const { calls, actions } = recorder();
  const bridge = 'plughw:CARD=Loopback,DEV=0,SUBDEV=1';
  await selectOutput('speech', speech(), actions, bridge);
  assert.deepEqual(calls, ['playback:null', `speech:device:${bridge}`, 'speech:enable:voice-1']);
});

test('direct also stops the speaking session before taking the device', async () => {
  const { calls, actions } = recorder();
  await selectOutput('direct', speech({ sessionEnabled: true }), actions, PLAYBACK);
  assert.deepEqual(calls, [
    'speech:stop',
    `playback:${PLAYBACK}`,
    'routing:{"bypass":true,"mic_muted":false}',
  ]);
});

test('speech is rejected without a running speech service', async () => {
  const { calls, actions } = recorder();
  await assert.rejects(selectOutput('speech', null, actions, PLAYBACK), OutputPathError);
  assert.deepEqual(calls, []);
});

test('speech is rejected before the device changes when no voice is selected', async () => {
  const { calls, actions } = recorder();
  await assert.rejects(selectOutput('speech', speech({ voiceId: '' }), actions, PLAYBACK), OutputPathError);
  assert.deepEqual(calls, []);
});
