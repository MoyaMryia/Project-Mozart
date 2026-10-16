import type { Mode } from './api.ts';

export interface RuntimeStage {
  role: string;
  label: string;
  backend: string;
  asset: string;
  reason: string;
}

export function runtimeStages(mode: Mode, model: Record<string, string | undefined>): RuntimeStage[] {
  const realtime = mode === 'rt_rvc' && !!model.realtime_front_backend;
  const realtimeRoles = [['realtime_hubert', 'HuBERT'], ['realtime_rmvpe', 'RMVPE'], ['realtime_front', 'Generator front'], ['realtime_decoder', 'Generator decoder']];
  const qualityRoles = [['hubert', 'HuBERT'], ['rmvpe', 'RMVPE'], ['generator', 'Generator']];
  const roles = mode === 'idle'
    ? [...qualityRoles.map(([role, label]) => [role, `${label}（quality）`]),
       ...realtimeRoles.map(([role, label]) => [role, `${label}（实时）`])]
    : realtime ? realtimeRoles : qualityRoles;
  const names: Record<string, string> = {
    deferred: '按需加载', tensorrt: 'TensorRT', onnx_cpu: 'ONNX Runtime · CPU',
    onnx_cuda_ep: 'ONNX Runtime · CUDA EP 已配置', unavailable: '不可用',
  };
  return roles.filter(([role]) => model[`${role}_backend`]).map(([role, label]) => ({
    role, label, backend: names[model[`${role}_backend`]!] || '未知执行后端',
    asset: model[`${role}_asset`] || '', reason: model[`${role}_fallback_reason`] || '',
  }));
}
