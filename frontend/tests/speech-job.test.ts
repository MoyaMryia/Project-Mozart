import assert from 'node:assert/strict';
import test from 'node:test';
import { speechJobLabel } from '../src/speech.ts';

test('job labels distinguish queue, playback, completion and terminal failures', () => {
  assert.equal(speechJobLabel({ status: 'queued', admission_pending: true }), '等待播报容量');
  assert.equal(speechJobLabel({ status: 'queued' }), '排队中');
  assert.equal(speechJobLabel({ status: 'processing' }), '合成中');
  assert.equal(speechJobLabel({ status: 'ready' }), '待播放');
  assert.equal(speechJobLabel({ status: 'playing' }), '播放中');
  assert.equal(speechJobLabel({ status: 'completed' }), '已完成');
  assert.equal(speechJobLabel({ status: 'failed' }), '失败');
  assert.equal(speechJobLabel({ status: 'cancelled' }), '已取消');
  assert.equal(speechJobLabel({ status: 'expired' }), '已过期');
  assert.equal(speechJobLabel({ status: 'future-status' }), 'future-status');
});
