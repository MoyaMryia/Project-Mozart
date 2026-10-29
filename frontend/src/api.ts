// api.ts — 后端 API 类型与请求错误分类。
export type Mode = 'idle' | 'rt_rvc' | 'file_rvc' | 'rt_zero_shot' | 'file_zero_shot';
export type ActiveMode = Exclude<Mode, 'idle'>;

export interface Job {
  job_id: string;
  name: string;
  mode: Mode;
  status: 'queued' | 'processing' | 'cancelling' | 'completed' | 'failed' | 'cancelled';
  progress: number;
  error: string;
  download_url?: string;
}

export interface Status {
  mode: Mode;
  pending_target_mode: Mode | null;
  queue: Job[];
  active_model_id: string;
  worker_running: boolean;
  last_error: string;
  capabilities: Partial<Record<ActiveMode, boolean>>;
  file_queue_paused: boolean;
  model: { has_index?: string; [key: string]: string | undefined };
  realtime?: { available: boolean; mic_muted: boolean; bypass: boolean };
  stream?: { playout_delay_ms?: number; queued_output_ms?: number };
}

export interface StackRequirements { language: string; playback: boolean; voice_required: boolean; }
export interface StackStatus {
  managed: boolean;
  available: boolean;
  instance_id?: string;
  state?: string;
  phase?: string;
  updated_at?: number;
  requirements?: StackRequirements;
  readiness?: { translation: boolean; speech_runtime: boolean; voice_session: boolean };
  degraded?: Record<string, string>;
  error?: { type?: string; message: string };
}

export interface ModelList {
  models: Array<{ id: string; exists: boolean; current: boolean }>;
}

export interface BackendLogEntry {
  timestamp: string;
  level: string;
  message: string;
  mode: string;
}

export interface RvcParameters {
  f0_method: string;
  pitch_shift: number;
  index_rate: number;
  filter_radius: number;
  rms_mix_rate: number;
  protect: number;
}

export interface Preset { id: string; name: string; parameters: RvcParameters; }

/** USB 桥状态：由 usb-audio 守护进程写入，控制面读后透传。 */
export interface BridgeStatus {
  active: boolean;
  role?: string;
  sent?: number;
  received?: number;
  dropped?: number;
  gaps?: number;
  errors?: number;
  last_error?: string;
  age_seconds?: number;
  /** 桥不可用的原因；由设备清单脚本给出。 */
  reason?: string;
}

export interface PreDevice {
  id: string;
  card?: number;
  label: string;
  /** local=本机声卡，usb=USB 协议对；缺省按 local 处理。 */
  group?: 'local' | 'usb';
}
export interface PreProcessorState {
  state?: string;
  mode?: string;
  frames?: number;
  send_errors?: number;
  [key: string]: unknown;
}
export interface PreProcessorStatus {
  available: boolean;
  devices: { capture: PreDevice[]; playback: PreDevice[]; bridge?: BridgeStatus };
  current: { capture?: string; playback?: string };
  state?: PreProcessorState;
  error?: string;
}

export interface MonitorSnapshot {
  cpu_percent: number | null;
  memory: { total_bytes: number; used_bytes: number };
  pipewire: { available: boolean };
  cuda: { available: boolean };
  gpu: { available: boolean; load_percent: number; memory_type: string; memory_total_bytes: number; memory_used_bytes: number };
  vad: { available: boolean; voiced_percent: number; confidence_percent: number };
}

// 译文源审计：selected=refined 表示翻译采用了复核文本，online 表示采用流式识别文本。
export interface TranslationSourceAudit {
  selected: 'online' | 'refined';
  reason: string;
  issues: string[];
  changed_terms?: string[];
}

export interface SubtitleEvent {
  seq: number;
  utterance_id?: string;
  revision?: number;
  final?: boolean;
  translation_status?: 'recognizing' | 'pending' | 'completed' | 'failed' | 'skipped';
  // 复核候选文本。它是候选，主字幕使用 translation_source_text。
  refined_zh?: string;
  refinement_status?: 'disabled' | 'pending' | 'completed' | 'failed' | 'skipped';
  zh: string;
  en: string;
  translate_ms?: number;
  // 翻译实际使用的源文本；旧事件没有该字段，界面回退到 zh。
  translation_source_text?: string;
  translation_source_reason?: string;
  translation_source_audit?: TranslationSourceAudit;
  // 磁盘恢复的记录没有排队起点，该字段为 null。
  translation_queue_wait_ms?: number | null;
  translation_refinement_wait_ms?: number;
  translation_after_asr_ms?: number | null;
  translation_attempts?: number;
  translation_error?: string;
  speech_degraded?: boolean;
  speech_error?: string;
  speech_submission_status?: 'queued' | 'sending' | 'retrying' | 'submitted' | 'skipped' | 'failed';
  speech_submission_attempts?: number;
  speech_last_error?: string;
  speech_retry_at?: number | null;
  source_revision_warning?: string;
  boundary_classifier_warning?: string;
  ts: string;
}

export class ApiRequestError extends Error {
  override name = 'ApiRequestError';
  /** HTTP 状态码；网络失败或正文无法解析时为 0。 */
  status = 0;
}

export const api = async <T>(path: string, init?: RequestInit): Promise<T> => {
  let response: Response;
  let text: string;
  try {
    response = await fetch(path, init);
    text = await response.text();
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
    throw new ApiRequestError(error.message, { cause: error });
  }
  let body: T & { error?: string };
  try { body = JSON.parse(text) as T & { error?: string }; }
  catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    throw new ApiRequestError(`服务返回了无法解析的数据（HTTP ${response.status}）`, { cause: error });
  }
  if (!response.ok) {
    const failure = new ApiRequestError(typeof body?.error === 'string' && body.error
      ? body.error : `Backend returned HTTP ${response.status}`);
    failure.status = response.status;
    throw failure;
  }
  return body;
};
