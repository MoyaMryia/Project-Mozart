export interface SpeechRuntimeStatus {
  runtime_ready?: boolean;
  runtime_state?: string;
  runtime_error?: string | null;
  worker_error?: { type: string; message: string; at: number } | null;
  warmup_error?: string | null;
}
export function speechRuntimeLabel(status: SpeechRuntimeStatus): string {
  if (status.runtime_ready === true) return '语音已就绪';
  if (status.worker_error) return '语音工作线程已停止';
  if (status.runtime_state === 'closed') return '语音已关闭';
  if (status.runtime_state === 'stopped') return '语音已停止';
  if (status.runtime_state === 'unprepared') return '语音待准备';
  if (status.runtime_error || status.warmup_error || status.runtime_state === 'failed') return '语音准备失败';
  if (status.runtime_state === 'queued') return '语音准备已排队';
  if (status.runtime_state === 'preparing') return '语音预热中';
  return '语音就绪状态未知';
}
