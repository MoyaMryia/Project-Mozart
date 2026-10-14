import assert from 'node:assert/strict';
import test from 'node:test';
import { runtimeStages } from '../src/runtime.ts';

test('runtime details follow the active mode and retain fallback evidence', () => {
  const model = {
    generator_backend: 'onnx_cpu', generator_asset: '/models/file.onnx', generator_fallback_reason: 'CUDA error',
    realtime_front_backend: 'tensorrt', realtime_front_asset: '/models/front.engine',
    realtime_decoder_backend: 'tensorrt', realtime_decoder_asset: '/models/decoder.engine',
  };
  assert.equal(runtimeStages('rt_rvc', model)[0].asset, '/models/front.engine');
  const file = runtimeStages('file_rvc', model);
  assert.equal(file.length, 1);
  assert.equal(file[0].backend, 'ONNX Runtime · CPU');
  assert.equal(file[0].reason, 'CUDA error');
});

test('legacy and unavailable runtime states do not imply GPU execution', () => {
  assert.deepEqual(runtimeStages('rt_rvc', {}), []);
  assert.equal(runtimeStages('file_rvc', {generator_backend: 'onnx_cuda_ep'})[0].backend, 'ONNX Runtime · CUDA EP 已配置');
  assert.equal(runtimeStages('rt_rvc', {generator_backend: 'unavailable'})[0].backend, '不可用');
});

test('idle status retains loaded realtime assets and deferred quality stages', () => {
  const idle = runtimeStages('idle', {hubert_backend: 'deferred', realtime_front_backend: 'tensorrt',
    realtime_front_asset: '/models/front.engine', realtime_decoder_backend: 'tensorrt'});
  assert.equal(idle.length, 3);
  assert.equal(idle.find(stage => stage.role === 'hubert')?.backend, '按需加载');
  assert.equal(idle.find(stage => stage.role === 'realtime_front')?.asset, '/models/front.engine');
});
