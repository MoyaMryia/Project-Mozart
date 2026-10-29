<script setup lang="ts">
// App.vue — 控制中心主应用。
// 视觉体系：语义化设计令牌（styles.css），亮/暗双主题；圆角两档（卡片 lg、控件 md）。
// 逻辑：组合式状态 + SSE 字幕订阅。新增：主题切换（localStorage: mozart-theme）。
import GlossaryPanel from './GlossaryPanel.vue';
import PrePanel from './PrePanel.vue';
import SpeechPanel from './SpeechPanel.vue';
import StackPanel from './StackPanel.vue';
import { createPoll } from './poll';
import { OutputPathError, selectOutput, type OutputActions, type OutputPath, type SpeechSnapshot } from './output-path';
import { runtimeStages } from './runtime';
import {
  captionSourceNote, captionSpeechStatus, captionText, captionTiming, latestTranslation, originalCaptionText,
  refinedCandidate, upsertSubtitle,
} from './subtitles';
import { subscribeSubtitles, type SubtitleState } from './subtitle-stream';
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import {
  api, ApiRequestError, type ActiveMode, type BackendLogEntry, type Job, type ModelList,
  type Mode, type MonitorSnapshot, type PreProcessorStatus, type Preset, type RvcParameters, type Status,
  type SubtitleEvent, type StackStatus,
} from './api';

// ---- 类型/常量 ----
// zero-shot 两个模式是后端 501 桩（seed-VC 为 TODO.md P2 路线），落地前不在
// 界面提供入口；后端 Mode 枚举与 capabilities 位保持不变以稳定 API 契约。
const MODES: ActiveMode[] = [
  'rt_rvc',
  'file_rvc',
  // 'rt_zero_shot',   // TODO(seed-vc): 实现后恢复
  // 'file_zero_shot', // TODO(seed-vc): 实现后恢复
];
const isFileMode = (mode: string) => mode === 'file_rvc';
const speechOnly = computed(() => !!status.value && !status.value.capabilities.rt_rvc && !status.value.capabilities.file_rvc);
const engineStages = computed(() => status.value ? runtimeStages(status.value.mode, status.value.model) : []);

// ---- 主题（亮/暗）----
// 初始值由 index.html 的首帧脚本写入 documentElement.dataset.theme。
const theme = ref<'light' | 'dark'>(document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
const toggleTheme = () => {
  theme.value = theme.value === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme.value;
  localStorage.setItem('mozart-theme', theme.value);
  document.getElementById('meta-theme-color')?.setAttribute('content', theme.value === 'dark' ? '#0C0E11' : '#FCFDFE');
};

// ---- 响应式状态 ----
const status = ref<Status | null>(null);
const apiOnline = ref(false);
const stackStatus = ref<StackStatus | null>(null);
const stackError = ref('');
const lifecycle = new AbortController();
const appApi = <T,>(path: string, init?: RequestInit) => api<T>(path, { ...init, signal: init?.signal ?? lifecycle.signal });
const switching = ref(false);                         // 模式切换请求进行中
const selectedMode = ref<ActiveMode>('rt_rvc');       // UI 选中的行
const uploadFile = ref<File | null>(null);
const uploadFileName = ref('');
const uploadInputValue = ref('');                     // 清空 input[type=file] 用
const uploading = ref(false);
const models = ref<ModelList['models']>([]);
const selectedModel = ref('');
const presets = ref<Preset[]>([]);
const selectedPresetId = ref('');
const parameters = reactive<RvcParameters>({
  f0_method: 'rmvpe', pitch_shift: 0, index_rate: 0.75,
  filter_radius: 3, rms_mix_rate: 0.25, protect: 0.33,
});
const logs = ref<BackendLogEntry[]>([]);
const logFilter = ref('all');
const monitor = ref<MonitorSnapshot | null>(null);
const toastMessage = ref('');
const subtitleEvents = ref<SubtitleEvent[]>([]);
const subtitleState = ref<SubtitleState>('connecting');


// ---- toast ----
let toastTimer: number | undefined;
const showError = (error: unknown) => {
  if (lifecycle.signal.aborted) return;
  toastMessage.value = error instanceof Error ? error.message : '操作失败';
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { toastMessage.value = ''; }, 4000);
};

// ---- 派生状态 ----
const activeJob = computed<Job | null>(() =>
  status.value?.queue.find((job) => job.status === 'processing') ?? null);
const queuePaused = computed(() => status.value?.file_queue_paused ?? false);
const canSubmitFile = computed(() =>
  selectedMode.value === 'file_rvc'
  && status.value?.mode === 'file_rvc'
  && Boolean(uploadFile.value)
  && apiOnline.value);
const running = computed(() => !!status.value && apiOnline.value && status.value.mode !== 'idle'
  && (status.value.mode !== 'rt_rvc' || status.value.worker_running));
const systemStatusText = computed(() => {
  if (!apiOnline.value) return 'RVC 状态不可用';
  if (!status.value) return '正在读取 RVC 状态';
  if (status.value.last_error) return 'RVC 故障';
  if (status.value.pending_target_mode) return 'RVC 切换中';
  return running.value ? 'RVC 已启用' : 'RVC 已停止';
});
const taskBadgeText = computed(() => {
  if (activeJob.value) return activeJob.value.status.toUpperCase();
  if (queuePaused.value) return 'QUEUE PAUSED';
  return status.value?.mode === 'file_rvc' ? 'IDLE' : 'STOPPED';
});
const filteredLogs = computed(() => {
  const entries = logs.value.slice().reverse();
  return logFilter.value === 'all'
    ? entries
    : entries.filter((entry) => entry.mode === logFilter.value);
});
const latestSubtitle = computed<SubtitleEvent | null>(
  () => subtitleEvents.value[subtitleEvents.value.length - 1] ?? null);
// 主字幕显示翻译实际采用的源文本（见 tools/subtitle_bridge.py 的 translation_source）。
// refined_zh 是复核候选，显示在单独的候选行。
const latestCaptionText = computed(() => captionText(latestSubtitle.value));
const latestOriginalCaption = computed(() => originalCaptionText(latestSubtitle.value));
const latestRefinedCandidate = computed(() => refinedCandidate(latestSubtitle.value));
const latestSourceNote = computed(() => captionSourceNote(latestSubtitle.value));
const latestCaptionTiming = computed(() => captionTiming(latestSubtitle.value));
const latestSpeechDelivery = computed(() => captionSpeechStatus(latestSubtitle.value));
const latestTranslatedSubtitle = computed(() => latestTranslation(subtitleEvents.value));

const statUnit = (text: string) => `<span class="stat-unit">${text}</span>`;
const statLatency = computed(() => monitor.value
  ? (monitor.value.cpu_percent === null
    ? `-- ${statUnit('%')}`
    : `${monitor.value.cpu_percent.toFixed(1)} ${statUnit('%')}`)
  : `-- ${statUnit('%')}`);
const statCache = computed(() => monitor.value
  ? `${gib(monitor.value.memory.used_bytes)} ${statUnit(`/ ${gib(monitor.value.memory.total_bytes)} GiB`)}`
  : `-- ${statUnit('/ -- GiB')}`);
const statVram = computed(() => {
  if (!monitor.value) return 'N/A';
  return monitor.value.gpu.available
    ? `${monitor.value.gpu.load_percent.toFixed(0)}%${monitor.value.gpu.memory_type === 'shared' ? ' · SHARED RAM' : ''}`
    : 'UNAVAILABLE';
});
const gib = (bytes: number) => (bytes / 1024 ** 3).toFixed(1);
const stateClass = (online: boolean) =>
  `font-mono text-tiny font-bold ${online ? 'state-ok' : 'state-err'}`;

// ---- 参数行（原 renderParameters 的声明式版本）----
interface ParameterRow {
  key: keyof RvcParameters;
  label: string;
  min?: number; max?: number; step?: number; options?: string[];
}
const parameterRows = computed<ParameterRow[]>(() => [
  { key: 'f0_method', label: 'F0 提取', options: ['rmvpe'] },
  { key: 'pitch_shift', label: '音高偏移', min: -12, max: 12, step: 1 },
  ...(status.value?.model.has_index === 'true'
    ? [{ key: 'index_rate' as const, label: '检索索引比例', min: 0, max: 1, step: 0.05 }]
    : []),
  { key: 'filter_radius', label: '滤波半径', min: 0, max: 7, step: 1 },
  { key: 'rms_mix_rate', label: 'RMS 混合', min: 0, max: 1, step: 0.05 },
  { key: 'protect', label: '辅音保护', min: 0, max: 0.5, step: 0.05 },
]);
const parameterLabel = (key: keyof RvcParameters, value: string | number) => {
  if (key === 'f0_method') return String(value).toUpperCase();
  if (key === 'pitch_shift') return `${value} st`;
  return typeof value === 'number' ? String(Number(value.toFixed(2))) : String(value);
};

// ---- API 动作 ----
const statusPoll = createPoll(signal => appApi<Status>('/api/status', { signal }), value => {
  status.value = value; apiOnline.value = true;
  if (value.capabilities[selectedMode.value] !== true) {
    const available = MODES.find(mode => value.capabilities[mode] === true);
    if (available) selectedMode.value = available;
  }
}, () => { apiOnline.value = false; });
const stackPoll = createPoll(signal => appApi<StackStatus>('/api/stack/status', { signal }), value => {
  stackStatus.value = value; stackError.value = '';
}, error => { stackStatus.value = null; stackError.value = error instanceof Error ? error.message : '读取失败'; });
const refreshStatus = (replace = false) => statusPoll.run(replace);

const preStatus = ref<PreProcessorStatus | null>(null);
const preBusy = ref(false);
const preError = ref('');
const prePoll = createPoll(signal => appApi<PreProcessorStatus>('/api/preprocessor', { signal }), value => {
  preStatus.value = value; preError.value = value.error ?? '';
}, error => { preStatus.value = null; preError.value = error instanceof Error ? error.message : '读取失败'; });
const refreshPre = () => prePoll.run();
const applyPreDevices = async (payload: { capture: string; playback: string }) => {
  if (preBusy.value) return;
  preBusy.value = true;
  preError.value = '';
  try {
    await appApi('/api/preprocessor', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    await refreshPre();
  } catch (error) {
    preError.value = error instanceof Error ? error.message : '切换失败';
  } finally {
    preBusy.value = false;
  }
};

const isModeOn = (mode: ActiveMode) =>
  !!status.value && (status.value.mode === mode || status.value.pending_target_mode === mode);

const switchMode = async (mode: Mode): Promise<boolean> => {
  if (switching.value) return false;
  try {
    switching.value = true;
    if (mode !== 'idle') {
      if (status.value?.capabilities[mode] !== true) throw new Error('此模式尚未实现');
      if (!selectedModel.value) throw new Error('请先选择可用模型');
      localStorage.setItem('mozart-enabled-mode', mode);
    }
    await appApi('/api/mode/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, model_id: mode === 'idle' ? '' : selectedModel.value }),
    });
    await refreshStatus(true);
    return true;
  } catch (error) { showError(error); return false; }
  finally { switching.value = false; }
};

const refreshModels = async () => {
  try {
    const { models: list } = await appApi<ModelList>('/api/models');
    models.value = list.filter((model) => model.exists);
    if (!models.value.length) { selectedModel.value = ''; return; }
    const current = models.value.find((model) => model.current)?.id || selectedModel.value;
    selectedModel.value = models.value.some((model) => model.id === current)
      ? current : models.value[0].id;
  } catch (error) {
    models.value = [];
    selectedModel.value = '';
    showError(error);
  }
};

const refreshParameters = async () => {
  try {
    Object.assign(parameters, await appApi<RvcParameters>('/api/parameters'));
  } catch (error) { showError(error); }
};

const submitParameters = async () => {
  const body: Record<string, string | number> = {
    f0_method: parameters.f0_method,
    pitch_shift: Number(parameters.pitch_shift),
    index_rate: Number(parameters.index_rate),
    filter_radius: Number(parameters.filter_radius),
    rms_mix_rate: Number(parameters.rms_mix_rate),
    protect: Number(parameters.protect),
  };
  try {
    await appApi('/api/parameters', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    await refreshParameters();
  } catch (error) { showError(error); }
};

let parameterSyncTimer: number | undefined;
const onParameterInput = () => {
  clearTimeout(parameterSyncTimer);
  parameterSyncTimer = window.setTimeout(() => void submitParameters(), 200);
};

const refreshPresets = async () => {
  const response = await appApi<{ presets: Preset[] }>('/api/presets');
  presets.value = response.presets;
};
const applyPreset = async (id: string) => {
  const preset = presets.value.find((item) => item.id === id);
  if (!preset) return;
  try {
    await appApi('/api/parameters', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(preset.parameters),
    });
    await refreshParameters();
  } catch (error) { showError(error); }
};
const onPresetChange = () => {
  if (selectedPresetId.value) void applyPreset(selectedPresetId.value).catch(showError);
};
const savePreset = async () => {
  const name = window.prompt('预设名称');
  if (!name?.trim()) return;
  try {
    await appApi('/api/presets', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim(), parameters }),
    });
    await refreshPresets();
  } catch (error) { showError(error); }
};
const deletePreset = async () => {
  const id = selectedPresetId.value;
  if (!id || !window.confirm('删除该预设？')) return;
  try {
    await appApi(`/api/presets/${encodeURIComponent(id)}`, { method: 'DELETE' });
    selectedPresetId.value = '';
    await refreshPresets();
  } catch (error) { showError(error); }
};

const logsPoll = createPoll(signal => appApi<{ entries: BackendLogEntry[] }>('/api/logs?limit=200', { signal }),
  value => { logs.value = value.entries; }, () => undefined);
const monitorPoll = createPoll(signal => appApi<MonitorSnapshot>('/api/monitor', { signal }),
  value => { monitor.value = value; }, () => { monitor.value = null; });
const refreshLogs = () => logsPoll.run();
const refreshMonitor = () => monitorPoll.run();

const submitFile = async () => {
  if (!uploadFile.value) return showError(new Error('请先选择音频文件'));
  if (selectedMode.value !== 'file_rvc') return showError(new Error('请先选择 FILE_RVC 模式'));
  try {
    if (status.value?.mode !== 'file_rvc') return showError(new Error('FILE_RVC 尚未启动'));
    const data = new FormData();
    data.append('audio_file', uploadFile.value);
    data.append('model_id', selectedModel.value);
    uploading.value = true;
    await appApi<{ job_id: string }>('/api/file/convert', { method: 'POST', body: data });
    uploadFile.value = null;
    uploadFileName.value = '';
    uploadInputValue.value = '';
    await refreshStatus(true);
  } catch (error) {
    showError(error);
  } finally {
    uploading.value = false;
  }
};

const cancelJob = async (job: Job) => {
  try {
    await appApi(`/api/file/cancel?job_id=${encodeURIComponent(job.job_id)}`, { method: 'DELETE' });
    await refreshStatus(true);
  } catch (error) { showError(error); }
};
const removeJob = async (job: Job) => {
  try {
    await appApi(`/api/file/job?job_id=${encodeURIComponent(job.job_id)}`, { method: 'DELETE' });
    await refreshStatus(true);
  } catch (error) { showError(error); }
};
const cancelActive = async () => {
  if (activeJob.value) await cancelJob(activeJob.value);
};
const togglePause = async () => {
  const endpoint = queuePaused.value ? '/api/file/resume' : '/api/file/pause';
  try {
    await appApi(endpoint, { method: 'POST' });
    await refreshStatus(true);
  } catch (error) { showError(error); }
};
const globalRun = async () => {
  await switchMode(selectedMode.value);
};
const globalStop = async () => {
  await switchMode('idle');
  try {
    const result = await appApi<{ stop_errors: number }>('/api/speech/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (result.stop_errors) showError(new Error('播报停止操作部分失败，请检查语音状态。'));
  } catch (error) {
    if (!(error instanceof ApiRequestError)) throw error;
    showError(new Error(`播报停止请求失败：${error.message}`));
  }
};

// ---- 模式选择（原 selectMode / toggle 逻辑）----
const pickMode = (mode: ActiveMode) => { selectedMode.value = mode; };

// ---- 实时快捷控制（静音 / 干声直通，仅 RT_RVC 运行时可用）----
const realtimeAvailable = computed(() =>
  !!status.value && status.value.mode === 'rt_rvc' && (status.value.realtime?.available ?? false));
const bypassOn = computed(() => status.value?.realtime?.bypass ?? false);

// ---- 输出路径：变声 / 翻译播报 / 直出（一次只允许一路占用播放设备）----
// 译文播报的输出语言固定为英文；服务端只支持 en 与 zh。
const SPEECH_LANGUAGE = 'en';
interface SpeechStatusBody {
  session?: { enabled: boolean; voice_id: string };
  runtime_ready?: boolean;
  control_generation?: number;
  voices?: { id: string }[];
}
const speechSession = ref<{ enabled: boolean; voice_id: string; playback: boolean } | null>(null);
const speechServiceAvailable = ref(false);
const speechPoll = createPoll(signal => appApi<{ enabled: boolean; voice_id: string; playback: boolean }>('/api/speech/session', { signal }),
  value => { speechSession.value = value; speechServiceAvailable.value = true; },
  () => { speechSession.value = null; speechServiceAvailable.value = false; });
const refreshSpeech = () => speechPoll.run();
const speechAvailable = computed(() => speechServiceAvailable.value);
const outputPath = computed<OutputPath>(() => speechSession.value?.enabled && speechSession.value.playback
  ? 'speech' : bypassOn.value ? 'direct' : 'voice');
const outputBusy = ref(false);
const outputPlayback = computed(() => {
  const devices = preStatus.value?.devices.playback ?? [];
  const current = preStatus.value?.current?.playback ?? '';
  if (current && current !== 'null') return current;
  return devices.find(device => device.id !== 'null')?.id ?? '';
});
const outputActions = (generation: number): OutputActions => ({
  setPlayback: async playback => {
    const capture = preStatus.value?.current?.capture;
    if (!capture) throw new OutputPathError('读取当前采集设备失败，先刷新设备面板');
    await appApi('/api/preprocessor', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ capture, playback }),
    });
  },
  // 播报进程启动时固定播放设备；面板切换输出时要把它指向同一设备。
  setSpeechPlayback: async device => {
    await appApi('/api/speech/playback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device }),
    });
  },
  setRouting: async patch => {
    await appApi('/api/realtime/routing', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
  },
  stopSpeech: async () => {
    await appApi('/api/speech/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_control_generation: generation }),
    });
  },
  prepareSpeech: async () => {
    await appApi('/api/speech/prepare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_control_generation: generation }),
    });
  },
  enableSpeech: async voiceId => {
    await appApi('/api/speech/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: true, voice_id: voiceId, language: SPEECH_LANGUAGE, playback: true, expected_control_generation: generation }),
    });
  },
});
const loadSpeechSnapshot = async () => {
  try {
    const body = await appApi<SpeechStatusBody>('/api/speech/status');
    return {
      generation: body.control_generation,
      snapshot: {
        sessionEnabled: body.session?.enabled === true,
        runtimeReady: body.runtime_ready === true,
        voiceId: body.session?.voice_id || body.voices?.[0]?.id || '',
      } satisfies SpeechSnapshot,
    };
  } catch (error) {
    // 只有“播报服务未启动”能当成没有会话；其他失败必须上报，
    // 否则可能在没有停止播放进程时重启预处理，争用同一个播放设备。
    if (error instanceof ApiRequestError && error.status === 503) {
      return { generation: undefined, snapshot: null };
    }
    throw error;
  }
};
const runOutput = async (path: OutputPath) => {
  // 已在该路径时不重启预处理，避免无谓的音频断点。
  if (outputBusy.value || outputPath.value === path) return;
  outputBusy.value = true;
  try {
    const { generation, snapshot } = await loadSpeechSnapshot();
    await selectOutput(path, snapshot, outputActions(generation ?? 0), outputPlayback.value);
    await refreshStatus(true);
    // 轮询可能在途中，切换后必须重新读一次。
    await Promise.all([prePoll.run(true), speechPoll.run(true)]);
  } catch (error) { showError(error); }
  finally { outputBusy.value = false; }
};

// ---- 字幕 SSE ----
let stopSubtitles: (() => void) | undefined;
const connectSubtitles = () => {
  stopSubtitles = subscribeSubtitles(
    caption => upsertSubtitle(subtitleEvents.value, caption),
    state => { subtitleState.value = state; },
  );
};

// ---- has_index 变化时刷新参数（行集合会变）----
watch(() => status.value?.model.has_index, () => { void refreshParameters(); });

// ---- 生命周期 ----
const timers: number[] = [];
onMounted(() => {
  // 恢复上次使用的模式；localStorage 里的旧值可能指向已停用的桩模式
  const stored = localStorage.getItem('mozart-enabled-mode') as ActiveMode | null;
  selectedMode.value = stored && MODES.includes(stored) ? stored : 'rt_rvc';
  void refreshStatus();
  void stackPoll.run();
  void refreshPre();
  void refreshSpeech();
  void refreshModels();
  void refreshLogs();
  void refreshParameters();
  void refreshPresets().catch(showError);
  void refreshMonitor().catch(() => undefined);
  connectSubtitles();
  timers.push(window.setInterval(() => void refreshStatus(), 1000));
  timers.push(window.setInterval(() => void stackPoll.run(), 1000));
  timers.push(window.setInterval(() => void refreshPre(), 5000));
  timers.push(window.setInterval(() => void refreshSpeech(), 5000));
  timers.push(window.setInterval(() => void refreshLogs(), 1500));
  timers.push(window.setInterval(() => void refreshMonitor().catch(() => undefined), 2000));
});
onUnmounted(() => {
  timers.forEach(clearInterval);
  clearTimeout(toastTimer);
  clearTimeout(parameterSyncTimer);
  statusPoll.stop(); stackPoll.stop(); logsPoll.stop(); monitorPoll.stop(); prePoll.stop(); speechPoll.stop();
  lifecycle.abort();
  stopSubtitles?.();
});
</script>

<template>
  <a href="#main-content" class="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 skip-link focus:px-4 focus:py-2 focus:z-50 rounded-sm text-xs font-mono shadow-md">
    Skip to main content
  </a>

  <!-- 顶部状态导航条 -->
  <header class="app-header flex flex-wrap md:flex-nowrap items-center gap-3 px-4 md:px-8 py-3.5 border-b border-edge-soft bg-surface shrink-0">
    <h1 class="text-lg font-extrabold tracking-tight text-ink" translate="no">MOZART</h1>

    <div class="order-3 md:order-none w-full md:w-auto md:ml-auto min-w-0 flex flex-wrap md:flex-nowrap items-center gap-2" role="group" aria-label="连接与资源">
      <div data-status="api" class="hardware-stat flex items-center gap-2 shrink-0 px-3 py-1">
        <svg class="hardware-icon" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><rect width="20" height="8" x="2" y="2" rx="2" ry="2"/><rect width="20" height="8" x="2" y="14" rx="2" ry="2"/><path d="M6 6h.01M6 18h.01M10 6h8m-8 12h8"/></svg>
        <span class="hw-label">API</span>
        <span :class="stateClass(apiOnline)">{{ apiOnline ? '在线' : '离线' }}</span>
      </div>
      <div class="hardware-stat flex items-center gap-2 shrink-0 px-3 py-1">
        <svg class="hardware-icon" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>
        <span class="hw-label">CPU</span>
        <strong class="font-mono text-xs text-ink tabular-nums" v-html="statLatency"></strong>
      </div>
      <div class="hardware-stat flex items-center gap-2 shrink-0 px-3 py-1">
        <svg class="hardware-icon" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/></svg>
        <span class="hw-label">RAM</span>
        <strong class="font-mono text-xs text-ink tabular-nums" v-html="statCache"></strong>
      </div>
      <div class="hardware-stat hidden md:flex items-center gap-2 shrink-0 px-3 py-1">
        <svg class="hardware-icon" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M9 1v3m6-3v3M9 20v3m6-3v3M20 9h3m-3 6h3M1 9h3m-3 6h3"/></svg>
        <span class="hw-label">GPU</span>
        <strong class="font-mono text-xs text-ink tabular-nums">{{ statVram }}</strong>
      </div>
    </div>

    <!-- 亮/暗主题切换 -->
    <button type="button" class="theme-toggle order-2 md:order-none ml-auto md:ml-1" :aria-label="theme === 'dark' ? '切换到亮色主题' : '切换到暗色主题'" :aria-pressed="theme === 'dark'" :title="theme === 'dark' ? '切换到亮色主题' : '切换到暗色主题'" @click="toggleTheme">
      <svg v-if="theme === 'dark'" class="w-4 h-4" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
      <svg v-else class="w-4 h-4" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"/></svg>
    </button>
  </header>

  <div v-if="toastMessage" class="fixed bottom-4 right-4 z-50 max-w-sm mx-4 banner-warn text-xs p-3 shadow-lg" role="alert" aria-atomic="true">{{ toastMessage }}</div>

  <!-- 主布局 -->
  <main id="main-content" :class="['flex-1 min-h-0 grid grid-cols-1 bg-app overflow-visible md:overflow-hidden', speechOnly ? 'md:grid-cols-1' : 'md:grid-cols-[200px_1fr_340px]']">

    <!-- 左栏：全局控制、系统运行模式与导航 -->
    <section v-if="!speechOnly" class="left-sidebar bg-panel border-r border-edge-soft overflow-visible md:overflow-hidden md:h-full" aria-label="RVC 控制">
      <div class="sidebar-controls space-y-4 p-4 md:px-5 md:py-3 md:overflow-y-auto">
        <!-- RVC 运行控制 -->
        <div class="relative">
          <div class="flex items-center gap-2 min-h-[24px] mb-2" aria-live="polite">
            <div class="relative w-5 h-5 flex items-center justify-center" aria-hidden="true">
              <svg :class="['system-state-icon', status?.mode !== 'idle' ? 'state-ok' : 'hidden']" aria-hidden="true" fill="currentColor" viewBox="0 0 48 48"><path d="M24 2a22 22 0 1 0 0 44 22 22 0 0 0 0-44Z"/><path fill="#fff" d="m20 15 14 9-14 9V15Z"/></svg>
              <svg :class="['system-state-icon', status?.mode === 'idle' ? 'state-err' : 'hidden']" aria-hidden="true" fill="currentColor" viewBox="0 0 48 48"><path d="M24 2a22 22 0 1 0 0 44 22 22 0 0 0 0-44Z"/><path fill="#fff" d="M16 16h16v16H16V16Z"/></svg>
            </div>
            <div class="min-w-0">
              <div :class="['text-sm font-extrabold leading-5 tracking-tight', running ? 'state-ok' : 'state-err']">{{ systemStatusText }}</div>
            </div>
          </div>
          <div class="transport-control" role="group" aria-label="RVC 运行控制">
            <button type="button" data-state="paused" class="transport-button" aria-label="暂停文件队列" :title="queuePaused ? '恢复文件队列' : '暂停队列（当前任务会完成）'" :aria-pressed="queuePaused" :class="{ 'is-active': queuePaused }" :disabled="status?.mode !== 'file_rvc'" @click.prevent="togglePause">
              <svg class="w-5 h-5" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24"><path d="M6 5h4v14H6V5Zm8 0h4v14h-4V5Z"/></svg>
            </button>
            <button type="button" data-state="running" class="transport-button" aria-label="启动 RVC 处理" title="启动" :aria-pressed="!!status && status.mode !== 'idle'" :class="{ 'is-active': !!status && status.mode !== 'idle' }" :disabled="switching || status?.capabilities[selectedMode] !== true" @click.prevent="globalRun">
              <svg class="w-5 h-5" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7L8 5Z"/></svg>
            </button>
            <button type="button" data-state="stopped" class="transport-button" aria-label="停止 RVC 和语音播报" title="停止 RVC 和播报；识别采集继续（文件任务会完成）" :aria-pressed="status?.mode === 'idle'" :class="{ 'is-active': !status || status.mode === 'idle' }" @click.prevent="globalStop">
              <svg class="w-5 h-5" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24"><path d="M6 6h12v12H6V6Z"/></svg>
            </button>
          </div>

        </div>

        <!-- 系统模式列表 -->
        <div>
          <div class="space-y-2" role="group" aria-label="RVC 模式">
            <button v-for="mode in MODES" :key="mode" type="button"
              :class="['mode-row w-full rounded-md p-3.5 text-left flex items-center justify-between gap-2 border border-transparent hover-subtle focus-visible:ring-2 ring-ink', selectedMode === mode && 'is-selected']"
              :data-mode="mode" :aria-pressed="selectedMode === mode"
              :disabled="switching || status?.capabilities[mode] !== true" @click="pickMode(mode)">
              <span class="mode-name text-sm text-ink-2">{{ mode === 'rt_rvc' ? '实时变声' : '文件变声' }}</span>
              <span v-if="isModeOn(mode)" class="chip chip-ok">运行中</span>
            </button>
          </div>
        </div>

        <!-- 挂起切换状态槽 -->
        <div :class="['banner-warn flex items-start gap-2.5 p-3.5', !status?.pending_target_mode && 'hidden']" aria-live="polite">
          <span class="w-2 h-2 bg-amber-500 rounded-full mt-1.5 animate-pulse motion-reduce:animate-none shrink-0"></span>
          <div class="min-w-0 flex-1">
            <div class="text-tiny font-bold" data-i18n="pendingTitle">等待切换中 (DEFERRED)</div>
            <div class="text-xs mt-0.5 truncate opacity-80">
              <span data-i18n="pendingDesc">当前转换结束后切换至</span> <strong class="font-mono" translate="no">{{ status?.pending_target_mode?.toUpperCase() }}</strong>
            </div>
          </div>
        </div>
      </div>

    </section>

    <!-- 中栏：主操作面板 -->
    <section class="px-3 py-3 md:px-5 md:py-4 w-full flex flex-col flex-1 min-h-0 gap-3 overflow-visible md:overflow-y-auto md:h-full" :class="{ 'max-w-5xl mx-auto': speechOnly }" aria-label="会话与播报">

      <div class="shrink-0 flex flex-col">

      <StackPanel :status="stackStatus" :error="stackError" />
      <SpeechPanel id="reference-speech" :stack-status="stackStatus" />

      <!-- FILE 模式：音频文件上传 -->
      <div :class="['mb-6', (!status?.capabilities.file_rvc || !isFileMode(selectedMode)) && 'hidden']">
        <label class="dropzone relative p-8 flex flex-col items-center justify-center cursor-pointer">
          <input type="file" accept="audio/*" aria-label="Upload Audio File" class="absolute inset-0 w-full h-full opacity-0 cursor-pointer" :key="uploadInputValue" @change="uploadFile = ($event.target as HTMLInputElement).files?.[0] ?? null; uploadFileName = uploadFile?.name ?? ''; uploadInputValue = ($event.target as HTMLInputElement).value">
          <svg class="w-8 h-8 text-ink-4 mb-3" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24">
            <path d="M5 16h2v2h10v-2h2v2.25A2.75 2.75 0 0 1 16.25 21h-8.5A2.75 2.75 0 0 1 5 18.25V16Zm7-13a1 1 0 0 1 1 1v8.59l2.29-2.3 1.42 1.42-4.42 4.42-4.42-4.42 1.42-1.42 2.29 2.3V4a1 1 0 0 1 1-1Z"/>
          </svg>
          <span class="block text-xs font-bold text-ink mb-1" data-i18n="clickToUpload">点击或拖拽音频文件至此</span>
          <span class="block text-tiny text-ink-4" data-i18n="uploadFormats">Format WAV, MP3, M4A, AAC, FLAC, OGG, max 100MB</span>

          <span :class="['mt-3 text-xs font-bold text-ink bg-surface border border-edge px-3.5 py-1.5 rounded-md card-shadow flex items-center gap-2 max-w-full min-w-0', !uploadFile && 'hidden']">
            <span class="shrink-0 text-ink-3">待提交:</span>
            <span class="font-mono text-ink-2 truncate min-w-0 flex-1">{{ uploadFileName }}</span>
          </span>
        </label>
      </div>

      <!-- FILE 模式：处理任务与进度 -->
      <div :class="['mb-6', (!status?.capabilities.file_rvc || !isFileMode(selectedMode)) && 'hidden']" aria-live="polite">
        <div class="space-y-4">
          <div class="flex justify-between items-center min-w-0 gap-3">
            <span class="font-mono text-2xl font-bold text-ink tabular-nums shrink-0">{{ activeJob?.progress ?? 0 }}%</span>
            <span class="text-xs text-ink-2 font-semibold truncate min-w-0 text-right">{{ activeJob?.name || uploadFileName || '等待选择音频文件…' }}</span>
          </div>

          <div class="w-full h-1.5 bg-subtle rounded-full overflow-hidden">
            <div class="h-full bg-accent transition-transform duration-300 origin-left scale-x-0" :style="{ transform: `scaleX(${(activeJob?.progress ?? 0) / 100})` }"></div>
          </div>

          <div class="flex justify-between items-center text-xs">
            <span class="chip chip-subtle text-tiny">{{ taskBadgeText }}</span>
          </div>

          <div class="flex gap-3 pt-2">
            <button type="button" class="btn btn-accent btn-lg" :disabled="!canSubmitFile || uploading" @click.prevent="submitFile">
              <svg :class="['motion-reduce:hidden animate-spin h-3.5 w-3.5', !uploading && 'hidden']" fill="currentColor" viewBox="0 0 24 24">
                <path class="opacity-25" d="M12 2a10 10 0 1 0 10 10h-3a7 7 0 1 1-7-7V2Z"/>
                <path class="opacity-75" d="M12 2v3a7 7 0 0 1 7 7h3A10 10 0 0 0 12 2Z"/>
              </svg>
              <span>{{ uploading ? '正在上传…' : '上传并转换' }}</span>
            </button>
            <button type="button" class="btn btn-outline btn-lg" :disabled="!activeJob" @click.prevent="cancelActive">取消当前任务</button>
          </div>
        </div>
      </div>
      </div>

      <!-- 实时字幕条（文字路：STT → Qwen 翻译，SSE 订阅 /api/subtitles） -->
      <div id="subtitle-panel" role="region" aria-label="实时字幕" class="shrink-0 bg-surface border border-edge rounded-lg px-4 py-2.5 flex items-start gap-3 min-h-[52px]">
        <div class="flex flex-col items-start shrink-0">
          <span class="text-micro font-extrabold tracking-wider text-ink-4 font-mono">SUB / 字幕</span>
          <span :class="['text-micro font-mono font-bold', subtitleState === 'online' ? 'state-ok' : subtitleState === 'offline' ? 'state-err' : 'text-ink-4']">{{ subtitleState.toUpperCase() }}</span>
        </div>
        <div class="min-w-0 flex-1 text-left">
          <div class="text-base font-bold text-ink leading-relaxed whitespace-pre-wrap break-words">{{ latestCaptionText || '等待语音输入…' }}</div>
          <div class="text-sm text-ink-2 leading-relaxed whitespace-pre-wrap break-words">{{ latestSubtitle?.en ? `${latestSubtitle.translation_status === 'failed' ? '待核对译文：' : ''}${latestSubtitle.en}` : latestSubtitle?.translation_status === 'failed' ? '译文未通过校验' : latestSubtitle?.translation_status === 'skipped' ? '未送出翻译' : latestSubtitle?.translation_status === 'recognizing' ? '识别中…' : latestSubtitle?.translation_status === 'pending' ? '翻译中…' : '' }}</div>
          <div v-if="latestTranslatedSubtitle && latestTranslatedSubtitle.utterance_id !== latestSubtitle?.utterance_id" class="text-tiny text-ink-3 leading-4" :title="latestTranslatedSubtitle.translation_error || latestTranslatedSubtitle.zh">{{ latestTranslatedSubtitle.translation_status === 'failed' ? '前文待核对译文：' : '前文译文：' }}{{ latestTranslatedSubtitle.en }}</div>
          <div v-if="latestRefinedCandidate" class="text-tiny text-ink-3 leading-4" :title="latestSourceNote">复核候选：{{ latestRefinedCandidate }}</div>
          <div v-if="latestOriginalCaption" class="text-tiny text-ink-3 leading-4">原始识别：{{ latestOriginalCaption }}</div>
          <p v-if="latestSubtitle?.speech_degraded" class="text-tiny state-warn leading-4" role="status">译文检查未通过，已尝试播报。</p>
          <p v-if="latestSubtitle?.translation_error" class="text-tiny state-warn leading-4" role="status">{{ latestSubtitle.translation_error }}</p>
          <p v-if="latestSpeechDelivery" class="text-tiny text-ink-3 leading-4" role="status">{{ latestSpeechDelivery }}</p>
          <p v-if="latestSubtitle?.speech_error" class="text-tiny state-warn leading-4" role="status">播报失败：{{ latestSubtitle.speech_error }}</p>
          <p v-if="latestSubtitle?.source_revision_warning" class="text-tiny state-warn leading-4" role="status">{{ latestSubtitle.source_revision_warning }}</p>
          <p v-if="latestSubtitle?.boundary_classifier_warning" class="text-tiny state-warn leading-4" role="status">{{ latestSubtitle.boundary_classifier_warning }}</p>
        </div>
        <span v-if="latestCaptionTiming" class="shrink-0 font-mono text-micro text-ink-4 tabular-nums self-center" :title="latestCaptionTiming.title">{{ latestCaptionTiming.label }}</span>
      </div>

      <!-- 主界面下方：终端式系统日志 -->
      <details id="log-panel" class="console-log shrink-0">
        <summary class="console-header cursor-pointer px-3 py-2 text-xs font-bold">系统日志</summary>
        <div class="console-header flex items-center justify-between gap-2 px-3 py-2 font-mono">
          <div class="flex items-center gap-1 min-w-0">
            <select v-model="logFilter" class="terminal-select min-w-0 text-tiny border-0 px-1.5 py-1 focus:outline-none" autocomplete="off" aria-label="Filter logs by mode">
              <option value="all" data-i18n="logFilterAll">全部模式</option>
              <option value="rt_rvc" translate="no">RT_RVC</option>
              <option value="file_rvc" translate="no">FILE_RVC</option>
              <!-- zero-shot 模式为 501 桩，不会产生日志；实现后恢复
              <option value="rt_zero_shot" translate="no">RT_ZERO_SHOT</option>
              <option value="file_zero_shot" translate="no">FILE_ZERO_SHOT</option>
              -->
            </select>
            <button type="button" class="console-action text-tiny px-1.5 py-1 rounded-sm shrink-0" @click="void appApi('/api/logs', { method: 'DELETE' }).then(refreshLogs).catch(showError)">清空日志</button>
          </div>
        </div>
        <div class="console-entries max-h-80 overflow-y-auto p-3 space-y-2 text-[12px] leading-5 font-mono" role="log" aria-live="polite" aria-label="System logs">
          <div v-for="(entry, index) in filteredLogs" :key="`${entry.timestamp}-${index}`" class="flex items-start gap-2 min-w-0 leading-4">
            <span class="text-emerald-400 shrink-0 font-bold">&gt;</span>
            <span class="text-gray-500 shrink-0 tabular-nums">{{ entry.timestamp }}</span>
            <span class="log-tag text-emerald-300">{{ entry.level.toUpperCase() }}</span>
            <span class="text-gray-300 break-words min-w-0">{{ entry.message }}</span>
          </div>
        </div>
      </details>

      <GlossaryPanel class="card p-4 shrink-0" />
    </section>

    <!-- 右栏：参数设置与硬件状态 -->
    <section v-if="!speechOnly" class="p-3 md:p-4 border-l border-edge-soft bg-surface flex flex-col gap-5 overflow-y-auto md:h-full" aria-label="Mode parameters and speaker selection">

      <PrePanel :status="preStatus" :busy="preBusy" :error="preError" @apply="applyPreDevices" />

      <!-- 参数设置（按模式切换） -->
      <div class="card p-3">
        <div class="flex items-center justify-between mb-2">
          <h2 class="text-xs font-extrabold tracking-wider text-ink uppercase section-title" data-i18n="paramsTitle">变声参数</h2>
          <span class="text-micro font-mono font-bold text-ink-2 bg-surface border border-edge rounded-sm px-2 py-1" translate="no">{{ selectedMode.toUpperCase() }}</span>
        </div>
        <div class="border-y border-edge py-2.5 mb-3">
          <div class="flex items-center gap-2">
            <label for="parameter-preset-select" class="sr-only">选择参数配置方案</label>
            <select id="parameter-preset-select" v-model="selectedPresetId" @change="onPresetChange" class="field parameter-profile-select min-w-0 flex-1 h-8 text-tiny font-bold px-2" autocomplete="off" aria-label="选择参数配置方案" :disabled="!presets.length && !selectedPresetId">
              <option value="">当前参数</option>
              <option v-for="preset in presets" :key="preset.id" :value="preset.id">{{ preset.name }}</option>
            </select>
            <button type="button" class="btn btn-outline btn-icon" aria-label="恢复后端默认参数" title="恢复默认参数" @click="void appApi('/api/parameters/reset', { method: 'POST' }).then(refreshParameters).catch(showError)">
              <svg class="w-3.5 h-3.5" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg>
            </button>
            <button type="button" class="btn btn-accent btn-sm" @click="savePreset">保存预设</button>
            <button type="button" class="btn btn-danger btn-sm" :disabled="!selectedPresetId" @click="deletePreset">删除预设</button>
          </div>
        </div>
        <div class="space-y-3">
          <div v-for="row in parameterRows" :key="row.key" class="space-y-1.5 pb-2.5 border-b border-edge last:border-b-0 last:pb-0">
            <div class="flex items-center justify-between gap-3">
              <label :for="`param-${row.key}`" class="text-micro font-bold tracking-wide text-ink-3">{{ row.label }}</label>
              <output :for="`param-${row.key}`" class="font-mono text-base font-extrabold leading-none text-ink tabular-nums">{{ parameterLabel(row.key, parameters[row.key]) }}</output>
            </div>
            <select v-if="row.options" :id="`param-${row.key}`" v-model="parameters[row.key]" class="field w-full h-7 text-micro font-bold px-2" @change="submitParameters">
              <option v-for="option in row.options" :key="option" :value="option">{{ option.toUpperCase() }}</option>
            </select>
            <input v-else :id="`param-${row.key}`" v-model.number="parameters[row.key]" type="range" class="parameter-range" :min="row.min" :max="row.max" :step="row.step" :aria-label="row.label" @input="onParameterInput">
          </div>
        </div>
      </div>

      <!-- 当前模式的单一音色选择，不保留队列 -->
      <div class="card p-3">
        <label for="speaker-select" class="block text-xs font-bold text-ink mb-2">变声音色</label>
        <select id="speaker-select" v-model="selectedModel" name="speaker-selection" class="field w-full min-w-0 text-xs px-3 py-2" autocomplete="off" aria-label="选择当前音色" :disabled="!models.length">
          <option v-if="!models.length" value="" selected disabled>{{ models.length === 0 && status === null ? '正在加载模型...' : '未找到可用模型' }}</option>
          <option v-for="model in models" :key="model.id" :value="model.id">{{ model.id }}</option>
        </select>
        <details v-if="engineStages.length" class="mt-3 text-micro text-ink-3">
          <summary class="cursor-pointer">推理运行状态</summary>
          <dl class="mt-2 space-y-2">
            <div v-for="stage in engineStages" :key="stage.role">
              <dt class="font-bold">{{ stage.label }}：{{ stage.backend }}</dt>
              <dd v-if="stage.asset" class="break-all font-mono mt-1">{{ stage.asset }}</dd>
              <dd v-if="stage.reason" class="state-err mt-1">回退原因：{{ stage.reason }}</dd>
            </div>
          </dl>
          <p v-if="status?.model.cached_models" class="mt-2">缓存模型：{{ status.model.cached_models }}；保留上限：{{ status.model.model_cache_limit }}。正在使用的模型继续保留。</p>
          <p v-if="status?.mode === 'rt_rvc' && status.stream?.playout_delay_ms !== undefined" class="mt-2">启动缓冲预算：{{ status.stream.playout_delay_ms }} ms；当前输出缓冲：{{ Math.round(status.stream.queued_output_ms ?? 0) }} ms。</p>
        </details>
      </div>

      <!-- FILE 模式：共享队列位于右侧状态区 -->
      <section :class="['border-t border-edge pt-4', !isFileMode(selectedMode) && 'hidden']" aria-label="文件处理队列">
        <div class="flex items-center justify-between mb-2">
          <h2 class="text-xs font-extrabold tracking-wider text-ink uppercase section-title" data-i18n="fileQueueTitle">文件处理队列</h2>
          <div class="flex items-center gap-2"><span class="text-micro font-mono text-ink-3">{{ status?.queue.length ?? 0 }} 项</span><button type="button" class="link-muted text-micro" @click="void appApi('/api/file/finished', { method: 'DELETE' }).then(() => refreshStatus(true)).catch(showError)">清理完成项</button></div>
        </div>
        <div class="bg-surface rounded-lg border border-edge card-shadow overflow-hidden">
          <div class="grid grid-cols-12 gap-1 px-3 py-2 bg-subtle text-micro font-extrabold text-ink-3 uppercase tracking-wider border-b border-edge-soft">
            <div class="col-span-5" data-i18n="qColFile">文件</div>
            <div class="col-span-3" data-i18n="qColMode">目标模式</div>
            <div class="col-span-2" data-i18n="qColStatus">状态</div>
            <div class="col-span-2 text-right" data-i18n="qColAction">操作</div>
          </div>
          <div class="max-h-[168px] overflow-y-auto">
            <div v-if="!status?.queue.length" class="px-3 py-6 text-center text-xs text-ink-4" data-i18n="emptyQueue">队列为空</div>
            <div v-for="job in status?.queue ?? []" :key="job.job_id" class="grid grid-cols-12 gap-2 items-center px-4 py-3 border-b border-edge-soft last:border-0 text-xs">
              <div class="col-span-5 min-w-0">
                <div class="font-mono text-tiny text-ink-2 truncate">{{ job.name }}</div>
                <div class="text-micro text-ink-4 tabular-nums mt-0.5">{{ job.progress }}%</div>
              </div>
              <div class="col-span-3 font-mono text-micro text-ink-3 truncate">FILE_RVC</div>
              <div class="col-span-2 min-w-0"><span :class="['chip truncate', job.status === 'processing' || job.status === 'cancelling' ? 'chip-warn' : job.status === 'completed' ? 'chip-ok' : job.status === 'failed' ? 'chip-err' : 'chip-info']">{{ job.status.toUpperCase() }}</span></div>
              <div class="col-span-2 text-right">
                <a v-if="job.status === 'completed' && job.download_url" :href="job.download_url" class="text-micro font-bold state-ok hover:underline px-1 py-1">下载</a>
                <button v-else-if="job.status === 'queued' || job.status === 'processing'" type="button" class="link-muted text-micro px-1 py-1 hover:!text-[color:var(--err-text)]" @click="cancelJob(job)">{{ job.status === 'processing' ? '取消此任务' : '移除队列' }}</button>
                <span v-else-if="job.status === 'cancelling'" class="text-micro font-bold state-warn">取消中</span>
                <button v-else type="button" class="link-muted text-micro px-1 py-1" @click="removeJob(job)">移除</button>
              </div>
              <div v-if="job.status === 'failed' && job.error" class="col-span-12 -mt-1 text-micro state-err break-words">{{ job.error }}</div>
            </div>
          </div>
        </div>
      </section>

      <!-- 实时音频快捷控制固定在整个右栏底部：一次只允许一路占用播放设备。 -->
      <div :class="['grid grid-cols-3 gap-2 mt-auto pt-4 border-t border-edge', isFileMode(selectedMode) && 'hidden']">
        <button type="button" :class="['routing-btn', outputPath === 'voice' && 'is-on']" :aria-pressed="outputPath === 'voice'" :disabled="!realtimeAvailable || outputBusy || !outputPlayback" :title="realtimeAvailable ? '输出 RVC 变声，字幕与翻译继续运行' : '仅在 RT_RVC 运行时可用'" @click.prevent="void runOutput('voice')">
          <svg :class="['routing-icon absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 shrink-0', outputPath === 'voice' && 'routing-icon--ok']" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24">
            <path d="M12 2a4 4 0 0 0-4 4v6a4 4 0 0 0 8 0V6a4 4 0 0 0-4-4Zm-6 9H4v1a8 8 0 0 0 7 7.94V22H8v2h8v-2h-3v-2.06A8 8 0 0 0 20 12v-1h-2v1a6 6 0 0 1-12 0v-1Z"/>
          </svg>
          <span class="w-full pl-7 text-left" data-i18n="btnStartVoice">启动变声</span>
        </button>
        <button type="button" :class="['routing-btn', outputPath === 'speech' && 'is-on']" :aria-pressed="outputPath === 'speech'" :disabled="!speechAvailable || outputBusy || !outputPlayback" :title="speechAvailable ? '输出译文播报，变声暂停' : '播报服务未启动：先用 OUTPUT=both 启动链路'" @click.prevent="void runOutput('speech')">
          <svg :class="['routing-icon absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 shrink-0', outputPath === 'speech' && 'routing-icon--ok']" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24">
            <path d="M4 3h16a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H9l-5 4v-4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm3 6v2h10V9H7Zm0 4v2h6v-2H7Z"/>
          </svg>
          <span class="w-full pl-7 text-left" data-i18n="btnStartSpeech">启动翻译</span>
        </button>
        <button type="button" :class="['routing-btn', outputPath === 'direct' && 'is-on']" :aria-pressed="outputPath === 'direct'" :disabled="!realtimeAvailable || outputBusy || !outputPlayback" :title="realtimeAvailable ? '输出原始人声（不推理）' : '仅在 RT_RVC 运行时可用'" @click.prevent="void runOutput('direct')">
          <svg :class="['routing-icon absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 shrink-0', outputPath === 'direct' && 'routing-icon--ok']" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24">
            <path d="m13.6 2-8.2 10.5a1 1 0 0 0 .8 1.6h4.5l-.7 7.9a1 1 0 0 0 1.8.6L19 11.5a1 1 0 0 0-.8-1.6h-4.5L14.6 3a1 1 0 0 0-1-1Z"/>
          </svg>
          <span class="w-full pl-7 text-left" data-i18n="btnDirect">直出</span>
        </button>
      </div>

    </section>

  </main>
</template>
