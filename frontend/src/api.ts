// api.ts — 后端 API 类型与请求封装（从 vanilla main.ts 平移，逻辑不变）
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
  model: { has_index?: string };
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

export interface MonitorSnapshot {
  cpu_percent: number | null;
  memory: { total_bytes: number; used_bytes: number };
  pipewire: { available: boolean };
  cuda: { available: boolean };
  gpu: { available: boolean; load_percent: number; memory_type: string; memory_total_bytes: number; memory_used_bytes: number };
  vad: { available: boolean; voiced_percent: number; confidence_percent: number };
}

export interface SubtitleEvent {
  seq: number;
  zh: string;
  en: string;
  translate_ms: number;
  ts: string;
}

export const api = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, init);
  const text = await response.text();
  let body: T & { error?: string };
  try { body = JSON.parse(text) as T & { error?: string }; }
  catch { throw new Error(`Backend returned HTTP ${response.status}`); }
  if (!response.ok) throw new Error(body.error || `Backend returned HTTP ${response.status}`);
  return body;
};
