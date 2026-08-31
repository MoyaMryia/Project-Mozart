<script setup lang="ts">
// App.vue — 控制中心主应用。
// UI 模板从原 vanilla index.html 1:1 平移（class/结构未动），逻辑从
// main.ts / monitor.ts 移植为组合式状态。新增：SUB 字幕条（SSE 订阅）。
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import {
  api, type ActiveMode, type BackendLogEntry, type Job, type ModelList,
  type Mode, type MonitorSnapshot, type Preset, type RvcParameters, type Status,
  type SubtitleEvent,
} from './api';

// ---- 类型/常量 ----
const MODES: ActiveMode[] = ['rt_rvc', 'file_rvc', 'rt_zero_shot', 'file_zero_shot'];
const isFileMode = (mode: string) => mode === 'file_rvc';

// ---- 响应式状态 ----
const status = ref<Status | null>(null);
const apiOnline = ref(true);
const selectedMode = ref<ActiveMode>('rt_rvc');       // UI 选中的行
const enabledMode = ref<ActiveMode | null>(           // 开关 + localStorage
  localStorage.getItem('mozart-enabled-mode') as ActiveMode | null);
const modeSelectedByUser = ref(false);
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
const subtitleState = ref<'connecting' | 'online' | 'offline'>('connecting');

// live 面板沿用原行为：恒为隐藏（canvas 波形从未接线）
const showLive = ref(false);

// ---- toast ----
let toastTimer: number | undefined;
const showError = (error: unknown) => {
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
  && enabledMode.value === 'file_rvc'
  && status.value?.mode === 'file_rvc'
  && Boolean(uploadFile.value)
  && apiOnline.value);
const running = computed(() => !!status.value && status.value.mode === selectedMode.value);
const systemStatusText = computed(() => {
  if (!status.value) return '已停止';
  if (running.value) return '运行中';
  return status.value.mode === 'idle' ? '已停止' : '模式切换中';
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

const statLatency = computed(() => monitor.value
  ? (monitor.value.cpu_percent === null
    ? '-- <span class="text-[10px] font-semibold text-gray-500">%</span>'
    : `${monitor.value.cpu_percent.toFixed(1)} <span class="text-[10px] font-semibold text-gray-500">%</span>`)
  : '-- <span class="text-[10px] font-semibold text-gray-500">%</span>');
const statCache = computed(() => monitor.value
  ? `${gib(monitor.value.memory.used_bytes)} <span class="text-[10px] font-semibold text-gray-500">/ ${gib(monitor.value.memory.total_bytes)} GB</span>`
  : '-- <span class="text-[10px] font-semibold text-gray-500">/ -- GB</span>');
const statVram = computed(() => {
  if (!monitor.value) return 'N/A';
  return monitor.value.gpu.available
    ? `${monitor.value.gpu.load_percent.toFixed(0)}% / ${gib(monitor.value.gpu.memory_used_bytes)} GB`
    : 'UNAVAILABLE';
});
const gib = (bytes: number) => (bytes / 1024 ** 3).toFixed(1);
const stateClass = (online: boolean | null) =>
  `font-mono text-[11px] font-bold ${online === true ? 'text-emerald-600' : online === false ? 'text-red-600' : 'text-gray-500'}`;

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
  return String(value);
};

// ---- API 动作 ----
const refreshStatus = async () => {
  try {
    status.value = await api<Status>('/api/status');
    apiOnline.value = true;
  } catch {
    apiOnline.value = false;
  }
};

const switchMode = async (mode: Mode): Promise<boolean> => {
  try {
    if (mode !== 'idle' && !selectedModel.value) throw new Error('请先选择可用模型');
    await api('/api/mode/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, model_id: mode === 'idle' ? '' : selectedModel.value }),
    });
    await refreshStatus();
    return true;
  } catch (error) { showError(error); return false; }
};

const refreshModels = async () => {
  try {
    const { models: list } = await api<ModelList>('/api/models');
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
    Object.assign(parameters, await api<RvcParameters>('/api/parameters'));
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
    await api('/api/parameters', {
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
  const response = await api<{ presets: Preset[] }>('/api/presets');
  presets.value = response.presets;
};
const applyPreset = async (id: string) => {
  const preset = presets.value.find((item) => item.id === id);
  if (!preset) return;
  try {
    await api('/api/parameters', {
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
    await api('/api/presets', {
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
    await api(`/api/presets/${encodeURIComponent(id)}`, { method: 'DELETE' });
    selectedPresetId.value = '';
    await refreshPresets();
  } catch (error) { showError(error); }
};

const refreshLogs = async () => {
  try {
    logs.value = (await api<{ entries: BackendLogEntry[] }>('/api/logs?limit=200')).entries;
  } catch { /* status 轮询负责连通性提示 */ }
};

const refreshMonitor = async () => {
  const response = await fetch('/api/monitor');
  if (!response.ok) throw new Error(`Monitor returned HTTP ${response.status}`);
  monitor.value = await response.json() as MonitorSnapshot;
};

const submitFile = async () => {
  if (!uploadFile.value) return showError(new Error('请先选择音频文件'));
  if (selectedMode.value !== 'file_rvc') return showError(new Error('请先选择 FILE_RVC 模式'));
  if (enabledMode.value !== 'file_rvc') return showError(new Error('请先打开 FILE_RVC 开关并点击全局启动'));
  try {
    if (status.value?.mode !== 'file_rvc') return showError(new Error('FILE_RVC 尚未启动'));
    const data = new FormData();
    data.append('audio_file', uploadFile.value);
    data.append('model_id', selectedModel.value);
    uploading.value = true;
    await api<{ job_id: string }>('/api/file/convert', { method: 'POST', body: data });
    uploadFile.value = null;
    uploadFileName.value = '';
    uploadInputValue.value = '';
    await refreshStatus();
  } catch (error) {
    showError(error);
  } finally {
    uploading.value = false;
  }
};

const cancelJob = async (job: Job) => {
  try {
    await api(`/api/file/cancel?job_id=${encodeURIComponent(job.job_id)}`, { method: 'DELETE' });
    await refreshStatus();
  } catch (error) { showError(error); }
};
const removeJob = async (job: Job) => {
  try {
    await api(`/api/file/job?job_id=${encodeURIComponent(job.job_id)}`, { method: 'DELETE' });
    await refreshStatus();
  } catch (error) { showError(error); }
};
const cancelActive = async () => {
  if (activeJob.value) await cancelJob(activeJob.value);
};
const togglePause = async () => {
  const endpoint = queuePaused.value ? '/api/file/resume' : '/api/file/pause';
  try {
    await api(endpoint, { method: 'POST' });
    await refreshStatus();
  } catch (error) { showError(error); }
};
const globalRun = async () => {
  if (!enabledMode.value) return showError(new Error('请先打开一个已实现模式的开关'));
  selectedMode.value = enabledMode.value;
  await switchMode(enabledMode.value);
};
const globalStop = async () => { await switchMode('idle'); };

// ---- 模式选择（原 selectMode / toggle 逻辑）----
const pickMode = (mode: ActiveMode) => {
  if (status.value?.capabilities[mode] !== true) {
    showError(new Error('此模式尚未实现'));
    return;
  }
  modeSelectedByUser.value = true;
  selectedMode.value = mode;
};
const onToggle = (mode: ActiveMode, checked: boolean | null) => {
  if (checked) {
    modeSelectedByUser.value = true;
    enabledMode.value = mode;
    localStorage.setItem('mozart-enabled-mode', mode);
    selectedMode.value = mode;
  } else if (enabledMode.value === mode) {
    enabledMode.value = null;
    localStorage.removeItem('mozart-enabled-mode');
  }
};

// ---- 字幕 SSE ----
let eventSource: EventSource | null = null;
const connectSubtitles = () => {
  try {
    eventSource = new EventSource('/api/subtitles');
    eventSource.onopen = () => { subtitleState.value = 'online'; };
    eventSource.onmessage = (event) => {
      try {
        subtitleEvents.value.push(JSON.parse(event.data) as SubtitleEvent);
        if (subtitleEvents.value.length > 50) subtitleEvents.value.shift();
      } catch { /* 非 JSON 行忽略 */ }
    };
    eventSource.onerror = () => { subtitleState.value = 'offline'; };
  } catch {
    subtitleState.value = 'offline';
  }
};

// ---- has_index 变化时刷新参数（行集合会变）----
watch(() => status.value?.model.has_index, () => { void refreshParameters(); });

// ---- 生命周期 ----
const timers: number[] = [];
onMounted(() => {
  selectedMode.value = enabledMode.value || 'rt_rvc';
  void refreshStatus();
  void refreshModels();
  void refreshLogs();
  void refreshParameters();
  void refreshPresets().catch(showError);
  void refreshMonitor().catch(() => undefined);
  connectSubtitles();
  timers.push(window.setInterval(() => void refreshStatus(), 1000));
  timers.push(window.setInterval(() => void refreshLogs(), 1500));
  timers.push(window.setInterval(() => void refreshMonitor().catch(() => undefined), 2000));
});
onUnmounted(() => {
  timers.forEach(clearInterval);
  eventSource?.close();
});
</script>

<template>
  <a href="#main-content" class="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:bg-black focus:text-white focus:px-4 focus:py-2 focus:z-50 rounded-sm text-xs font-mono shadow-md">
    Skip to main content
  </a>

  <!-- 顶部状态导航条 -->
  <header class="flex flex-wrap md:flex-nowrap items-center gap-3 px-4 md:px-8 py-3.5 border-b border-gray-100 bg-white shrink-0 shadow-[0_1px_2px_rgba(0,0,0,0.02)]">
    <div class="flex items-center gap-3">
      <h1 class="text-lg font-extrabold tracking-tight text-gray-900" translate="no">MOZART</h1>
      <span class="hidden sm:inline-flex font-mono text-[11px] bg-gray-100 text-gray-600 px-2.5 py-0.5 font-bold rounded-sm border border-gray-200/50" translate="no">Jetson Orin Nano</span>
    </div>

    <!-- 全局硬件状态：桌面端右对齐，移动端独占一行并可横向查看 -->
    <div class="order-3 md:order-none w-full md:w-auto md:ml-auto min-w-0 flex items-center justify-start md:justify-end gap-2 overflow-x-auto no-scrollbar" role="group" aria-label="Global hardware status">
      <div class="hardware-stat flex items-center gap-2 shrink-0 px-3 py-1">
        <svg class="hardware-icon" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>
        <span class="text-[10px] font-bold text-gray-400 tracking-wider">CPU</span>
        <strong class="font-mono text-xs text-gray-900 tabular-nums" v-html="statLatency"></strong>
      </div>
      <div class="hardware-stat flex items-center gap-2 shrink-0 px-3 py-1">
        <svg class="hardware-icon" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/></svg>
        <span class="text-[10px] font-bold text-gray-400 tracking-wider">RAM</span>
        <strong class="font-mono text-xs text-gray-900 tabular-nums" v-html="statCache"></strong>
      </div>
      <div class="hardware-stat flex items-center gap-2 shrink-0 px-3 py-1">
        <svg class="hardware-icon" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M9 1v3m6-3v3M9 20v3m6-3v3M20 9h3m-3 6h3M1 9h3m-3 6h3"/></svg>
        <span class="text-[10px] font-bold text-gray-400 tracking-wider">GPU</span>
        <strong class="font-mono text-xs text-gray-900 tabular-nums">{{ statVram }}</strong>
      </div>
      <div class="hardware-stat flex items-center gap-2 shrink-0 px-3 py-1">
        <svg class="hardware-icon" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M2 10v3m4-7v11m4-14v18m4-13v7m4-10v13m4-8v3"/></svg>
        <span class="text-[10px] font-bold text-gray-500 tracking-wider">PIPEWIRE</span>
        <span :class="stateClass(monitor?.pipewire.available ?? null)">{{ monitor?.pipewire.available === undefined ? 'UNKNOWN' : monitor.pipewire.available ? 'CONNECTED' : 'OFFLINE' }}</span>
      </div>
      <div class="hardware-stat flex items-center gap-2 shrink-0 px-3 py-1">
        <svg class="hardware-icon" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M2 12h2l2-6 4 12 3-8 2 4h5"/></svg>
        <span class="text-[10px] font-bold text-gray-400 tracking-wider">VAD</span>
        <span :class="stateClass(monitor?.vad.available ? true : monitor?.vad.available === undefined ? null : false)">{{ monitor?.vad.available ? `${monitor.vad.voiced_percent.toFixed(0)}% VOICED` : monitor?.vad.available === undefined ? 'N/A' : 'NO STREAM' }}</span>
      </div>
      <div class="hardware-stat flex items-center gap-2 shrink-0 px-3 py-1">
        <svg class="hardware-icon" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="m13 2-9 12h7l-1 8 10-13h-7z"/></svg>
        <span class="text-[10px] font-bold text-gray-400 tracking-wider">CUDA</span>
        <span :class="stateClass(monitor?.cuda.available ?? null)">{{ monitor?.cuda.available === undefined ? 'UNKNOWN' : monitor.cuda.available ? 'AVAILABLE' : 'UNAVAILABLE' }}</span>
      </div>
      <div class="hardware-stat flex items-center gap-2 shrink-0 px-3 py-1">
        <svg class="hardware-icon" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><rect width="20" height="8" x="2" y="2" rx="2" ry="2"/><rect width="20" height="8" x="2" y="14" rx="2" ry="2"/><path d="M6 6h.01M6 18h.01M10 6h8m-8 12h8"/></svg>
        <span class="text-[10px] font-bold text-gray-500 tracking-wider">API</span>
        <span :class="stateClass(apiOnline)">{{ apiOnline ? 'ONLINE' : 'OFFLINE' }}</span>
      </div>
    </div>
  </header>

  <!-- 三栏主布局 -->
  <main id="main-content" class="flex-1 min-h-0 grid grid-cols-1 md:grid-cols-[300px_1fr_340px] bg-[#FCFDFE] overflow-y-auto md:overflow-hidden">

    <!-- 左栏：全局控制、系统运行模式与导航 -->
    <section class="left-sidebar bg-[#FAFAFA] border-r border-gray-100 overflow-visible md:overflow-hidden md:h-full" aria-label="System Navigation">
      <div class="sidebar-controls space-y-4 p-4 md:px-5 md:py-3 md:overflow-y-auto">
        <!-- 全局运行控制 -->
        <div class="relative">
          <div class="flex items-center gap-2 min-h-[24px] mb-2" aria-live="polite">
            <div class="relative w-5 h-5 flex items-center justify-center" aria-hidden="true">
              <svg :class="['system-state-icon', status?.mode !== 'idle' ? 'text-emerald-600' : 'hidden']" aria-hidden="true" fill="currentColor" viewBox="0 0 48 48"><path d="M24 2a22 22 0 1 0 0 44 22 22 0 0 0 0-44Z"/><path fill="#fff" d="m20 15 14 9-14 9V15Z"/></svg>
              <svg :class="['system-state-icon', 'hidden', 'text-gray-500']" aria-hidden="true" fill="currentColor" viewBox="0 0 48 48"><path d="M24 2a22 22 0 1 0 0 44 22 22 0 0 0 0-44Z"/><path fill="#fff" d="M17 15h5v18h-5V15Zm9 0h5v18h-5V15Z"/></svg>
              <svg :class="['system-state-icon', status?.mode === 'idle' ? 'text-red-600' : 'hidden']" aria-hidden="true" fill="currentColor" viewBox="0 0 48 48"><path d="M24 2a22 22 0 1 0 0 44 22 22 0 0 0 0-44Z"/><path fill="#fff" d="M16 16h16v16H16V16Z"/></svg>
            </div>
            <div class="min-w-0">
              <div :class="['text-sm font-extrabold leading-5 tracking-tight', running ? 'text-emerald-600' : 'text-red-600']">{{ systemStatusText }}</div>
            </div>
          </div>
          <div class="transport-control" role="group" aria-label="全局运行控制">
            <button type="button" data-state="paused" class="transport-button" aria-label="暂停文件队列" :title="queuePaused ? '恢复文件队列' : '暂停队列（当前任务会完成）'" :aria-pressed="queuePaused" :class="{ 'is-active': queuePaused }" :disabled="status?.mode !== 'file_rvc'" @click.prevent="togglePause">
              <svg class="w-5 h-5" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24"><path d="M6 5h4v14H6V5Zm8 0h4v14h-4V5Z"/></svg>
            </button>
            <button type="button" data-state="running" class="transport-button" aria-label="启动全局处理" title="启动" :aria-pressed="!!status && status.mode !== 'idle'" :class="{ 'is-active': !!status && status.mode !== 'idle' }" :disabled="!enabledMode || status?.capabilities[enabledMode] !== true" @click.prevent="globalRun">
              <svg class="w-5 h-5" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7L8 5Z"/></svg>
            </button>
            <button type="button" data-state="stopped" class="transport-button" aria-label="停止当前模式" title="停止当前模式（当前文件任务会完成）" :aria-pressed="status?.mode === 'idle'" :class="{ 'is-active': !status || status.mode === 'idle' }" @click.prevent="globalStop">
              <svg class="w-5 h-5" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24"><path d="M6 6h12v12H6V6Z"/></svg>
            </button>
          </div>
          <div :class="['absolute top-full left-0 right-0 z-20 mt-2 transition-transform transition-opacity duration-200 bg-amber-50 text-amber-950 text-xs p-3 rounded-md shadow-lg border border-amber-200 flex items-start gap-2.5 pointer-events-none', toastMessage ? 'translate-y-0 opacity-100' : '-translate-y-1 opacity-0']" role="alert" aria-atomic="true">
            <svg class="w-4 h-4 mt-0.5 shrink-0 text-amber-700" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="m21.73 18-8-14a2 2 0 0 0-3.46 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4m0 4h.01"/></svg>
            <div class="min-w-0 leading-relaxed"><span class="mr-1 font-mono font-bold text-[10px] text-amber-700">WARN</span><span>{{ toastMessage }}</span></div>
          </div>
        </div>

        <!-- 系统模式列表 -->
        <div>
          <h2 class="text-xs font-extrabold tracking-wider text-gray-900 uppercase mb-3 section-title" data-i18n="systemModesTitle">系统模式</h2>
          <div class="space-y-2" role="group" aria-label="System modes">
            <div v-for="mode in MODES" :key="mode" :class="['mode-row w-full rounded-md transition-colors duration-150 hover:bg-gray-100 border border-transparent group flex items-stretch overflow-hidden', selectedMode === mode && 'is-selected']" :data-mode="mode">
              <button type="button" class="flex-1 p-3.5 min-w-0 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-gray-900" :data-mode="mode" :aria-pressed="selectedMode === mode" @click="pickMode(mode)">
                <span class="mode-name block font-mono text-sm text-gray-700" translate="no">{{ mode.toUpperCase().replace('_', '_') }}</span>
                <span class="mode-desc hidden block text-[11px] font-bold text-gray-500 mt-1 truncate" aria-live="polite"></span>
              </button>
              <div class="w-px bg-gray-300/50"></div>
              <div class="w-14 flex items-center justify-center p-2 shrink-0 bg-white/40">
                <div class="relative inline-block w-9 h-5 align-middle select-none">
                  <input type="checkbox" :id="`toggle-${mode}`" :checked="enabledMode === mode" :disabled="status?.capabilities[mode] !== true" class="toggle-checkbox absolute block w-4 h-4 rounded-full bg-white border-2 border-gray-300 appearance-none cursor-pointer transition-[left,right,border-color] duration-150 ease-in-out top-0.5 left-0.5 checked:left-auto checked:right-0.5 checked:border-gray-900" :aria-label="`Toggle ${mode.toUpperCase()}`" @change="onToggle(mode, ($event.target as HTMLInputElement).checked)">
                  <label :for="`toggle-${mode}`" class="toggle-label block overflow-hidden h-5 rounded-full bg-gray-300 cursor-pointer transition-colors duration-150 ease-in-out"></label>
                </div>
              </div>
            </div>
          </div>
          <button type="button" class="w-full mt-3 bg-white hover:bg-gray-100 text-gray-900 border border-gray-200 hover:border-gray-900 px-3 py-2.5 rounded-2xl transition-colors duration-150 flex items-center gap-2 cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 disabled:cursor-not-allowed" disabled aria-haspopup="dialog">
            <svg class="w-4 h-4 shrink-0 text-emerald-700" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/><path d="M8 7h8m-8 4h8"/></svg>
            <span class="flex-1 text-left text-[11px] font-extrabold">音色管理库</span>
            <span class="font-mono text-[10px] font-bold text-gray-500 tabular-nums">4</span>
          </button>
        </div>

        <!-- 挂起切换状态槽 -->
        <div :class="['flex items-start gap-2.5 bg-amber-50/80 border-l-2 border-amber-500 p-3.5 rounded-r-sm', !status?.pending_target_mode && 'hidden']" aria-live="polite">
          <span class="w-2 h-2 bg-amber-500 rounded-full mt-1.5 animate-pulse motion-reduce:animate-none shrink-0"></span>
          <div class="min-w-0 flex-1">
            <div class="text-[11px] font-bold text-amber-800" data-i18n="pendingTitle">等待切换中 (DEFERRED)</div>
            <div class="text-xs text-gray-600 mt-0.5 truncate">
              <span data-i18n="pendingDesc">当前转换结束后切换至</span> <strong class="font-mono" translate="no">{{ status?.pending_target_mode?.toUpperCase() }}</strong>
            </div>
          </div>
        </div>
      </div>

    </section>

    <!-- 中栏：主操作面板 -->
    <section class="px-3 py-3 md:px-5 md:py-4 w-full flex flex-col flex-1 min-h-0 gap-3 overflow-y-auto md:grid md:grid-rows-2 md:gap-3 md:overflow-hidden md:h-full no-scrollbar">

      <div class="min-h-0 flex flex-col overflow-y-auto no-scrollbar">

      <!-- RT 模式：实时音频控制面板（沿用原行为：当前恒隐藏） -->
      <div :class="['flex-1 flex flex-col min-h-0 gap-3', !showLive && 'hidden']">
        <div class="flex-1 flex flex-col min-h-0 gap-2">
          <div class="flex-1 flex flex-col min-h-0 gap-2">
            <!-- 输入音轨 -->
            <div class="flex-1 flex flex-col min-h-[140px] pb-3 border-b border-gray-200 gap-2">
              <div class="flex justify-between items-center shrink-0 border-b border-gray-100 pb-2">
                <span class="text-xs font-extrabold tracking-wider text-gray-700 font-mono">INPUT / MIC</span>
                <div class="flex items-center gap-2 font-mono text-[10px] text-gray-500" role="group" aria-label="波形坐标轴步进">
                  <span class="hidden sm:inline font-bold tracking-wider text-gray-400">网格步进</span>
                  <div class="inline-flex items-center gap-1 border-b border-gray-300 focus-within:border-gray-900">
                    <span class="font-bold text-gray-700">X</span>
                    <input type="number" class="axis-step-input w-9 bg-transparent text-right font-bold text-gray-900 focus:outline-none" min="0.25" max="2" step="0.25" value="0.5" aria-label="X 轴步进，单位秒">
                    <span>s</span>
                    <button type="button" class="w-5 h-5 inline-flex items-center justify-center text-gray-500 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 rounded-sm" aria-label="减小 X 轴步进" title="减小 X 轴步进">
                      <svg class="w-3 h-3" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24"><path d="M5 12h14"/></svg>
                    </button>
                    <button type="button" class="w-5 h-5 inline-flex items-center justify-center text-gray-500 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 rounded-sm" aria-label="增大 X 轴步进" title="增大 X 轴步进">
                      <svg class="w-3 h-3" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24"><path d="M12 5v14m-7-7h14"/></svg>
                    </button>
                  </div>
                  <div class="inline-flex items-center gap-1 border-b border-gray-300 focus-within:border-gray-900">
                    <span class="font-bold text-gray-700">Y</span>
                    <input type="number" class="axis-step-input w-9 bg-transparent text-right font-bold text-gray-900 focus:outline-none" min="0.25" max="1" step="0.25" value="0.5" aria-label="Y 轴步进">
                    <button type="button" class="w-5 h-5 inline-flex items-center justify-center text-gray-500 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 rounded-sm" aria-label="减小 Y 轴步进" title="减小 Y 轴步进">
                      <svg class="w-3 h-3" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24"><path d="M5 12h14"/></svg>
                    </button>
                    <button type="button" class="w-5 h-5 inline-flex items-center justify-center text-gray-500 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 rounded-sm" aria-label="增大 Y 轴步进" title="增大 Y 轴步进">
                      <svg class="w-3 h-3" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24"><path d="M12 5v14m-7-7h14"/></svg>
                    </button>
                  </div>
                </div>
              </div>
              <canvas class="w-full flex-1 min-h-[110px]" role="img" aria-label="麦克风实时波形，横轴为最近 2 秒时间，纵轴为标准化振幅负 1 到正 1"></canvas>
            </div>

            <!-- 输出音轨 -->
            <div class="flex-1 flex flex-col min-h-[140px] gap-2">
              <div class="flex justify-between items-center shrink-0 border-b border-gray-100 pb-2">
                <span class="text-xs font-extrabold tracking-wider text-emerald-700 font-mono">{{ isFileMode(selectedMode) ? 'OUTPUT / FILE RVC' : 'OUTPUT / RVC' }}</span>
              </div>
              <canvas class="w-full flex-1 min-h-[110px]" role="img" aria-label="模型输出实时波形，横轴为最近 2 秒时间，纵轴为标准化振幅负 1 到正 1"></canvas>
            </div>
          </div>
        </div>

      </div>

      <!-- FILE 模式：音频文件上传 -->
      <div :class="['mb-6', !isFileMode(selectedMode) && 'hidden']">
        <h2 class="text-xs font-extrabold tracking-wider text-gray-900 uppercase mb-3 section-title" data-i18n="uploadTitle">上传音频文件</h2>
        <label class="relative border border-dashed border-gray-300 bg-gray-50/50 rounded-lg p-8 flex flex-col items-center justify-center cursor-pointer hover:border-gray-900 hover:bg-gray-100/40 transition-colors duration-150 focus-within:ring-2 focus-within:ring-gray-900">
          <input type="file" accept="audio/*" aria-label="Upload Audio File" class="absolute inset-0 w-full h-full opacity-0 cursor-pointer focus-visible:ring-2 focus-visible:ring-gray-900" :key="uploadInputValue" @change="uploadFile = ($event.target as HTMLInputElement).files?.[0] ?? null; uploadFileName = uploadFile?.name ?? ''; uploadInputValue = ($event.target as HTMLInputElement).value">
          <svg class="w-8 h-8 text-gray-400 mb-3" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24">
            <path d="M5 16h2v2h10v-2h2v2.25A2.75 2.75 0 0 1 16.25 21h-8.5A2.75 2.75 0 0 1 5 18.25V16Zm7-13a1 1 0 0 1 1 1v8.59l2.29-2.3 1.42 1.42-4.42 4.42-4.42-4.42 1.42-1.42 2.29 2.3V4a1 1 0 0 1 1-1Z"/>
          </svg>
          <span class="block text-xs font-bold text-gray-900 mb-1" data-i18n="clickToUpload">点击或拖拽音频文件至此</span>
          <span class="block text-[11px] text-gray-400" data-i18n="uploadFormats">Format WAV, MP3, M4A, AAC, FLAC, OGG, max 100MB</span>

          <span :class="['mt-3 text-xs font-bold text-gray-900 bg-white border border-gray-200 px-3.5 py-1.5 rounded-md shadow-xs flex items-center gap-2 max-w-full min-w-0', !uploadFile && 'hidden']">
            <span class="shrink-0 text-gray-500">待提交:</span>
            <span class="font-mono text-gray-800 truncate min-w-0 flex-1">{{ uploadFileName }}</span>
          </span>
        </label>
      </div>

      <!-- FILE 模式：处理任务与进度 -->
      <div :class="['mb-6', !isFileMode(selectedMode) && 'hidden']" aria-live="polite">
        <h2 class="text-xs font-extrabold tracking-wider text-gray-900 uppercase mb-3 section-title" data-i18n="processingTitle">Audio Processing</h2>
        <div class="space-y-4">
          <div class="flex justify-between items-center min-w-0 gap-3">
            <span class="font-mono text-2xl font-bold text-gray-900 tabular-nums shrink-0">{{ activeJob?.progress ?? 0 }}%</span>
            <span class="text-xs text-gray-600 font-semibold truncate min-w-0 text-right">{{ activeJob?.name || uploadFileName || '等待选择音频文件…' }}</span>
          </div>

          <div class="w-full h-1.5 bg-gray-100 rounded-full overflow-hidden">
            <div class="h-full bg-black transition-transform duration-300 origin-left scale-x-0" :style="{ transform: `scaleX(${(activeJob?.progress ?? 0) / 100})` }"></div>
          </div>

          <div class="flex justify-between items-center text-xs">
            <span class="font-mono font-bold bg-gray-100 text-gray-600 px-2.5 py-0.5 rounded-sm text-[11px]">{{ taskBadgeText }}</span>
            <span class="font-mono text-gray-400 tabular-nums text-[11px]">0:00 / 0:00</span>
          </div>

          <div class="flex gap-3 pt-2">
            <button type="button" class="bg-black hover:bg-gray-800 text-white text-xs font-bold tracking-wide px-6 py-3 rounded-sm disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 flex items-center gap-2" :disabled="!canSubmitFile || uploading" @click.prevent="submitFile">
              <svg :class="['motion-reduce:hidden animate-spin h-3.5 w-3.5 text-white', !uploading && 'hidden']" fill="currentColor" viewBox="0 0 24 24">
                <path class="opacity-25" d="M12 2a10 10 0 1 0 10 10h-3a7 7 0 1 1-7-7V2Z"/>
                <path class="opacity-75" d="M12 2v3a7 7 0 0 1 7 7h3A10 10 0 0 0 12 2Z"/>
              </svg>
              <span>{{ uploading ? '正在上传…' : '上传并转换' }}</span>
            </button>
            <button type="button" class="bg-white text-gray-900 border border-gray-200 hover:border-gray-900 text-xs font-bold tracking-wide px-6 py-3 rounded-sm disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900" :disabled="!activeJob" @click.prevent="cancelActive">取消当前任务</button>
          </div>
        </div>
      </div>
      </div>

      <!-- 实时字幕条（文字路：STT → Qwen 翻译，SSE 订阅 /api/subtitles） -->
      <div class="shrink-0 bg-white border border-gray-200 rounded-lg px-4 py-2.5 flex items-start gap-3 min-h-[52px]">
        <div class="flex flex-col items-start shrink-0">
          <span class="text-[10px] font-extrabold tracking-wider text-gray-400 font-mono">SUB / 字幕</span>
          <span :class="['text-[9px] font-mono font-bold', subtitleState === 'online' ? 'text-emerald-600' : subtitleState === 'offline' ? 'text-red-500' : 'text-gray-400']">{{ subtitleState.toUpperCase() }}</span>
        </div>
        <div class="min-w-0 flex-1 text-left">
          <div class="text-xs font-bold text-gray-900 leading-5 truncate">{{ latestSubtitle?.zh || '等待语音…（mozart-pre -b 127.0.0.1 18100 双发开启字幕路）' }}</div>
          <div class="text-[11px] text-gray-500 leading-4 truncate">{{ latestSubtitle?.en || '' }}</div>
        </div>
        <span v-if="latestSubtitle" class="shrink-0 font-mono text-[9px] text-gray-400 tabular-nums self-center">{{ latestSubtitle.translate_ms }}ms</span>
      </div>

      <!-- 主界面下方：终端式系统日志 -->
      <div id="log-panel" class="console-log md:flex-none md:min-h-0 overflow-hidden flex flex-col bg-[#050806]">
        <div class="flex items-center justify-between gap-2 px-3 py-2 bg-[#42D879] text-[#052E16] font-mono">
          <h2 class="text-[11px] font-extrabold tracking-[0.16em] uppercase section-title shrink-0" data-i18n="logsTitle">系统日志</h2>
          <div class="flex items-center gap-1 min-w-0">
            <select v-model="logFilter" class="terminal-select min-w-0 text-[11px] border-0 px-1.5 py-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-[#052E16]" autocomplete="off" aria-label="Filter logs by mode">
              <option value="all" data-i18n="logFilterAll">全部模式</option>
              <option value="rt_rvc" translate="no">RT_RVC</option>
              <option value="file_rvc" translate="no">FILE_RVC</option>
              <option value="rt_zero_shot" translate="no">RT_ZERO_SHOT</option>
              <option value="file_zero_shot" translate="no">FILE_ZERO_SHOT</option>
            </select>
            <button type="button" class="text-[11px] font-extrabold text-[#052E16] hover:text-black px-1.5 py-1 rounded-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[#052E16] shrink-0" @click="void api('/api/logs', { method: 'DELETE' }).then(refreshLogs).catch(showError)">清空日志</button>
          </div>
        </div>
        <div class="flex-1 overflow-y-auto p-3 space-y-2 text-[12px] leading-5 font-mono" role="log" aria-live="polite" aria-label="System logs">
          <div v-for="(entry, index) in filteredLogs" :key="`${entry.timestamp}-${index}`" class="flex items-start gap-2 min-w-0 leading-4">
            <span class="text-emerald-400 shrink-0 font-bold">&gt;</span>
            <span class="text-gray-500 shrink-0 tabular-nums">{{ entry.timestamp }}</span>
            <span class="log-tag text-emerald-300">{{ entry.level.toUpperCase() }}</span>
            <span class="text-gray-300 break-words min-w-0">{{ entry.message }}</span>
          </div>
        </div>
      </div>

    </section>

    <!-- 右栏：参数设置与硬件状态 -->
    <section class="p-3 md:p-4 border-l border-gray-100 bg-white flex flex-col gap-5 overflow-y-auto md:h-full" aria-label="Mode parameters and speaker selection">

      <!-- 参数设置（按模式切换） -->
      <div class="bg-[#FAFAFA] border border-gray-200 rounded-lg p-3 shadow-[0_2px_8px_rgba(0,0,0,0.03)]">
        <div class="flex items-center justify-between mb-2">
          <h2 class="text-xs font-extrabold tracking-wider text-gray-900 uppercase section-title" data-i18n="paramsTitle">Parameters</h2>
          <span class="text-[10px] font-mono font-bold text-gray-700 bg-white border border-gray-200 rounded-sm px-2 py-1" translate="no">{{ selectedMode.toUpperCase() }}</span>
        </div>
        <div class="border-y border-gray-200 py-2.5 mb-3">
          <div class="flex items-center gap-2">
            <label for="parameter-preset-select" class="sr-only">选择参数配置方案</label>
            <select id="parameter-preset-select" v-model="selectedPresetId" @change="onPresetChange" class="parameter-profile-select min-w-0 flex-1 h-8 text-[11px] font-bold text-gray-800 bg-white border border-gray-200 rounded-sm px-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900" autocomplete="off" aria-label="选择参数配置方案" :disabled="!presets.length && !selectedPresetId">
              <option value="">当前参数</option>
              <option v-for="preset in presets" :key="preset.id" :value="preset.id">{{ preset.name }}</option>
            </select>
            <button type="button" class="w-8 h-8 shrink-0 inline-flex items-center justify-center text-gray-500 bg-white hover:bg-gray-100 hover:text-gray-900 border border-gray-200 rounded-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900" aria-label="恢复后端默认参数" title="恢复默认参数" @click="void api('/api/parameters/reset', { method: 'POST' }).then(refreshParameters).catch(showError)">
              <svg class="w-3.5 h-3.5" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg>
            </button>
            <button type="button" class="h-8 shrink-0 text-[10px] font-extrabold text-white bg-gray-900 hover:bg-gray-800 px-2.5 rounded-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900" @click="savePreset">保存预设</button>
            <button type="button" class="h-8 shrink-0 text-[10px] font-extrabold text-red-700 bg-white hover:bg-red-50 border border-red-200 px-2.5 rounded-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-red-700" :disabled="!selectedPresetId" @click="deletePreset">删除预设</button>
          </div>
          <div class="grid grid-cols-3 gap-2 mt-2.5 font-mono text-[9px] text-gray-500">
            <span class="truncate" translate="no">RVC · RMVPE</span>
            <span class="text-center">{{ parameterRows.length }} CONTROLS</span>
            <span class="text-right truncate">{{ selectedPresetId ? 'PRESET' : 'DEFAULT' }}</span>
          </div>
        </div>
        <div class="space-y-3">
          <div v-for="row in parameterRows" :key="row.key" class="space-y-1.5 pb-2.5 border-b border-gray-200 last:border-b-0 last:pb-0">
            <div class="flex items-center justify-between gap-3">
              <label :for="`param-${row.key}`" class="text-[10px] font-bold tracking-wide text-gray-500">{{ row.label }}</label>
              <output :for="`param-${row.key}`" class="font-mono text-base font-extrabold leading-none text-gray-950 tabular-nums">{{ parameterLabel(row.key, parameters[row.key]) }}</output>
            </div>
            <select v-if="row.options" :id="`param-${row.key}`" v-model="parameters[row.key]" class="w-full h-7 text-[10px] font-bold text-gray-700 bg-white border border-gray-200 rounded-sm px-2" @change="submitParameters">
              <option v-for="option in row.options" :key="option" :value="option">{{ option.toUpperCase() }}</option>
            </select>
            <input v-else :id="`param-${row.key}`" v-model.number="parameters[row.key]" type="range" class="parameter-range" :min="row.min" :max="row.max" :step="row.step" :aria-label="row.label" @input="onParameterInput">
          </div>
        </div>
      </div>

      <!-- 当前模式的单一音色选择，不保留队列 -->
      <div class="bg-[#FAFAFA] border border-gray-200 rounded-lg p-3 shadow-[0_2px_8px_rgba(0,0,0,0.03)]">
        <div class="flex items-center justify-between gap-3 mb-2">
          <h2 class="text-xs font-extrabold tracking-wider text-gray-900 uppercase section-title" data-i18n="speakerTitle">声音角色选择</h2>
          <span class="text-[10px] font-mono font-bold text-gray-700 bg-white border border-gray-200 rounded-sm px-2 py-1" translate="no">{{ selectedMode.toUpperCase() }}</span>
        </div>
        <label for="speaker-select" class="block text-[10px] font-bold tracking-wide text-gray-500 mb-2">当前音色</label>
        <select id="speaker-select" v-model="selectedModel" name="speaker-selection" class="w-full min-w-0 text-xs text-gray-800 bg-white border border-gray-200 rounded-md px-3 py-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900" autocomplete="off" aria-label="选择当前音色" :disabled="!models.length">
          <option v-if="!models.length" value="" selected disabled>{{ models.length === 0 && status === null ? '正在加载模型...' : '未找到可用模型' }}</option>
          <option v-for="model in models" :key="model.id" :value="model.id">{{ model.id }}</option>
        </select>
      </div>

      <!-- FILE 模式：共享队列位于右侧状态区 -->
      <section :class="['border-t border-gray-200 pt-4', !isFileMode(selectedMode) && 'hidden']" aria-label="文件处理队列">
        <div class="flex items-center justify-between mb-2">
          <h2 class="text-xs font-extrabold tracking-wider text-gray-900 uppercase section-title" data-i18n="fileQueueTitle">文件处理队列</h2>
          <div class="flex items-center gap-2"><span class="text-[10px] font-mono text-gray-500">{{ status?.queue.length ?? 0 }} 项</span><button type="button" class="text-[10px] font-bold text-gray-500 hover:text-gray-900" @click="void api('/api/file/finished', { method: 'DELETE' }).then(refreshStatus).catch(showError)">清理完成项</button></div>
        </div>
        <div class="bg-white rounded-lg border border-gray-200 shadow-[0_2px_8px_rgba(0,0,0,0.03)] overflow-hidden">
          <div class="grid grid-cols-12 gap-1 px-3 py-2 bg-gray-50 text-[9px] font-extrabold text-gray-500 uppercase tracking-wider border-b border-gray-100">
            <div class="col-span-5" data-i18n="qColFile">文件</div>
            <div class="col-span-3" data-i18n="qColMode">目标模式</div>
            <div class="col-span-2" data-i18n="qColStatus">状态</div>
            <div class="col-span-2 text-right" data-i18n="qColAction">操作</div>
          </div>
          <div class="max-h-[168px] overflow-y-auto">
            <div v-if="!status?.queue.length" class="px-3 py-6 text-center text-xs text-gray-400" data-i18n="emptyQueue">队列为空</div>
            <div v-for="job in status?.queue ?? []" :key="job.job_id" class="grid grid-cols-12 gap-2 items-center px-4 py-3 border-b border-gray-50 last:border-0 text-xs">
              <div class="col-span-5 min-w-0">
                <div class="font-mono text-[11px] text-gray-800 truncate">{{ job.name }}</div>
                <div class="text-[9px] text-gray-400 tabular-nums mt-0.5">{{ job.progress }}%</div>
              </div>
              <div class="col-span-3 font-mono text-[10px] text-gray-500 truncate">FILE_RVC</div>
              <div class="col-span-2 min-w-0"><span :class="['inline-flex max-w-full truncate rounded-sm px-1.5 py-0.5 font-mono text-[9px] font-bold', job.status === 'processing' || job.status === 'cancelling' ? 'bg-amber-50 text-amber-700' : job.status === 'completed' ? 'bg-emerald-50 text-emerald-700' : job.status === 'failed' ? 'bg-red-50 text-red-700' : 'bg-blue-50 text-blue-700']">{{ job.status.toUpperCase() }}</span></div>
              <div class="col-span-2 text-right">
                <a v-if="job.status === 'completed' && job.download_url" :href="job.download_url" class="text-[10px] font-bold text-emerald-700 hover:text-emerald-900 px-1 py-1">下载</a>
                <button v-else-if="job.status === 'queued' || job.status === 'processing'" type="button" class="text-[10px] font-bold text-gray-500 hover:text-red-700 px-1 py-1" @click="cancelJob(job)">{{ job.status === 'processing' ? '取消此任务' : '移除队列' }}</button>
                <span v-else-if="job.status === 'cancelling'" class="text-[10px] font-bold text-amber-700">取消中</span>
                <button v-else type="button" class="text-[10px] font-bold text-gray-500 hover:text-red-700 px-1 py-1" @click="removeJob(job)">移除</button>
              </div>
              <div v-if="job.status === 'failed' && job.error" class="col-span-12 -mt-1 text-[10px] text-red-700 break-words">{{ job.error }}</div>
            </div>
          </div>
        </div>
      </section>

      <!-- 实时音频快捷控制固定在整个右栏底部 -->
      <div :class="['grid grid-cols-2 gap-2 mt-auto pt-4 border-t border-gray-200', isFileMode(selectedMode) && 'hidden']">
        <button type="button" class="relative bg-white hover:bg-gray-100 text-gray-900 text-[11px] font-extrabold tracking-wide px-3 py-3 rounded-2xl border-2 border-gray-900 transition-colors duration-150 flex items-center cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 whitespace-nowrap disabled:cursor-not-allowed" aria-pressed="false" disabled>
          <svg class="absolute left-3 top-1/2 -translate-y-1/2 w-6 h-6 text-emerald-600 shrink-0" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24">
            <path d="M12 2a4 4 0 0 0-4 4v6a4 4 0 0 0 8 0V6a4 4 0 0 0-4-4Zm-6 9H4v1a8 8 0 0 0 7 7.94V22H8v2h8v-2h-3v-2.06A8 8 0 0 0 20 12v-1h-2v1a6 6 0 0 1-12 0v-1Z"/>
          </svg>
          <span class="w-full pl-8 text-left" data-i18n="btnMute">静音麦克风</span>
        </button>
        <button type="button" class="relative bg-white hover:bg-gray-100 text-gray-900 text-[11px] font-extrabold tracking-wide px-3 py-3 rounded-2xl border-2 border-gray-900 transition-colors duration-150 flex items-center cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 whitespace-nowrap disabled:cursor-not-allowed" aria-pressed="false" disabled>
          <svg class="absolute left-3 top-1/2 -translate-y-1/2 w-6 h-6 text-gray-400 shrink-0" aria-hidden="true" fill="currentColor" viewBox="0 0 24 24">
            <path d="m13.6 2-8.2 10.5a1 1 0 0 0 .8 1.6h4.5l-.7 7.9a1 1 0 0 0 1.8.6L19 11.5a1 1 0 0 0-.8-1.6h-4.5L14.6 3a1 1 0 0 0-1-1Z"/>
          </svg>
          <span class="w-full pl-8 text-left" data-i18n="btnBypass">旁路直通</span>
        </button>
      </div>

    </section>

  </main>
</template>
