import './styles.css';
import { refreshMonitor, setApiState } from './monitor';

type Mode = 'idle' | 'rt_rvc' | 'file_rvc' | 'rt_zero_shot' | 'file_zero_shot';
type ActiveMode = Exclude<Mode, 'idle'>;

interface Job {
  job_id: string;
  name: string;
  mode: Mode;
  status: 'queued' | 'processing' | 'cancelling' | 'completed' | 'failed' | 'cancelled';
  progress: number;
  error: string;
  download_url?: string;
}

interface Status {
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

interface ModelList {
  models: Array<{ id: string; exists: boolean; current: boolean }>;
}

interface BackendLogEntry {
  timestamp: string;
  level: string;
  message: string;
  mode: string;
}

interface BackendLogs {
  entries: BackendLogEntry[];
}

interface RvcParameters {
  f0_method: string;
  pitch_shift: number;
  index_rate: number;
  filter_radius: number;
  rms_mix_rate: number;
  protect: number;
}

interface Preset { id: string; name: string; parameters: RvcParameters; }

const api = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, init);
  const text = await response.text();
  let body: T & { error?: string };
  try { body = JSON.parse(text) as T & { error?: string }; }
  catch { throw new Error(`Backend returned HTTP ${response.status}`); }
  if (!response.ok) throw new Error(body.error || `Backend returned HTTP ${response.status}`);
  return body;
};

const get = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const selectedMode = (): Mode => (document.querySelector('.mode-row.is-selected') as HTMLElement | null)?.dataset.mode as Mode || 'rt_rvc';
let uploadFile: File | null = null;
let lastStatus: Status | null = null;
let apiOnline = true;
let parameterSyncTimer: number | undefined;
let activeLogFilter = 'all';
let modeSelectedByUser = false;
let enabledMode = (window.localStorage.getItem('mozart-enabled-mode') as ActiveMode | null);
let queuePaused = false;
let presets: Preset[] = [];

const isFileMode = (mode: Mode) => mode === 'file_rvc';
const canSubmitFile = () => selectedMode() === 'file_rvc'
  && enabledMode === 'file_rvc'
  && lastStatus?.mode === 'file_rvc'
  && Boolean(uploadFile)
  && apiOnline;

const updateFileSubmitState = () => {
  get<HTMLButtonElement>('btn-process').disabled = !canSubmitFile();
};

const selectMode = (mode: ActiveMode) => {
  document.querySelectorAll<HTMLElement>('.mode-row').forEach((row) => {
    const selected = row.dataset.mode === mode;
    row.classList.toggle('is-selected', selected);
    row.querySelector<HTMLButtonElement>('.mode-select')?.setAttribute('aria-pressed', String(selected));
  });
  const fileMode = isFileMode(mode);
  get('block-live').classList.toggle('hidden', true);
  get('block-upload').classList.toggle('hidden', !fileMode);
  get('block-processing').classList.toggle('hidden', !fileMode);
  get('block-file-queue').classList.toggle('hidden', !fileMode);
  get('realtime-audio-controls').classList.toggle('hidden', fileMode);
  get('speaker-mode-label').textContent = mode.toUpperCase();
  get('params-mode-label').textContent = mode.toUpperCase();
  get('output-track-label').textContent = fileMode ? 'OUTPUT / FILE RVC' : 'OUTPUT / RVC';
  updateFileSubmitState();
  get<HTMLButtonElement>('btn-global-run').disabled = !enabledMode || lastStatus?.capabilities[mode] !== true;
};

const renderBackendLogs = (entries: BackendLogEntry[]) => {
  const container = get<HTMLDivElement>('log-entries');
  const filtered = activeLogFilter === 'all' ? entries : entries.filter((entry) => entry.mode === activeLogFilter);
  container.replaceChildren(...filtered.slice().reverse().map((entry) => {
    const row = document.createElement('div');
    row.className = 'flex items-start gap-2 min-w-0 leading-4';
    const prompt = document.createElement('span');
    prompt.className = 'text-emerald-400 shrink-0 font-bold';
    prompt.textContent = '>';
    const timestamp = document.createElement('span');
    timestamp.className = 'text-gray-500 shrink-0 tabular-nums';
    timestamp.textContent = entry.timestamp;
    const level = document.createElement('span');
    level.className = 'log-tag text-emerald-300';
    level.textContent = entry.level.toUpperCase();
    const message = document.createElement('span');
    message.className = 'text-gray-300 break-words min-w-0';
    message.textContent = entry.message;
    row.append(prompt, timestamp, level, message);
    return row;
  }));
};

const renderQueue = (jobs: Job[]) => {
  const list = get<HTMLDivElement>('file-queue-list');
  get('file-queue-count').textContent = `${jobs.length} 项`;
  if (!jobs.length) {
    list.innerHTML = '<div class="px-3 py-6 text-center text-xs text-gray-400">队列为空</div>';
    return;
  }
  list.replaceChildren(...jobs.map((job) => {
    const row = document.createElement('div');
    row.className = 'grid grid-cols-12 gap-2 items-center px-4 py-3 border-b border-gray-50 last:border-0 text-xs';
    const statusClass = job.status === 'processing' || job.status === 'cancelling' ? 'bg-amber-50 text-amber-700' : job.status === 'completed' ? 'bg-emerald-50 text-emerald-700' : job.status === 'failed' ? 'bg-red-50 text-red-700' : 'bg-blue-50 text-blue-700';
    row.innerHTML = `<div class="col-span-5 min-w-0"><div class="job-name font-mono text-[11px] text-gray-800 truncate"></div><div class="text-[9px] text-gray-400 tabular-nums mt-0.5">${job.progress}%</div></div><div class="col-span-3 font-mono text-[10px] text-gray-500 truncate">FILE_RVC</div><div class="col-span-2 min-w-0"><span class="inline-flex max-w-full truncate rounded-sm px-1.5 py-0.5 font-mono text-[9px] font-bold ${statusClass}">${job.status.toUpperCase()}</span></div><div class="col-span-2 text-right"></div>`;
    row.querySelector<HTMLElement>('.job-name')!.textContent = job.name;
    if (job.status === 'failed' && job.error) {
      const error = document.createElement('div');
      error.className = 'col-span-12 -mt-1 text-[10px] text-red-700 break-words';
      error.textContent = job.error;
      row.append(error);
    }
    const actions = row.lastElementChild!;
    if (job.status === 'completed' && job.download_url) {
      const download = document.createElement('a');
      download.href = job.download_url;
      download.className = 'text-[10px] font-bold text-emerald-700 hover:text-emerald-900 px-1 py-1';
      download.textContent = '下载';
      actions.append(download);
    } else if (job.status === 'queued' || job.status === 'processing') {
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'text-[10px] font-bold text-gray-500 hover:text-red-700 px-1 py-1';
      cancel.textContent = job.status === 'processing' ? '取消此任务' : '移除队列';
      cancel.addEventListener('click', async () => {
        try {
          await api(`/api/file/cancel?job_id=${encodeURIComponent(job.job_id)}`, { method: 'DELETE' });
          await refreshStatus();
        } catch (error) { showError(error); }
      });
      actions.append(cancel);
    } else if (job.status === 'cancelling') {
      const cancelling = document.createElement('span');
      cancelling.className = 'text-[10px] font-bold text-amber-700';
      cancelling.textContent = '取消中';
      actions.append(cancelling);
    } else {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'text-[10px] font-bold text-gray-500 hover:text-red-700 px-1 py-1';
      remove.textContent = '移除';
      remove.addEventListener('click', () => void api(`/api/file/job?job_id=${encodeURIComponent(job.job_id)}`, { method: 'DELETE' }).then(refreshStatus).catch(showError));
      actions.append(remove);
    }
    return row;
  }));
};

const renderStatus = (status: Status) => {
  lastStatus = status;
  queuePaused = status.file_queue_paused;
  if (!modeSelectedByUser && status.mode !== 'idle') selectMode(status.mode as ActiveMode);
  renderQueue(status.queue);
  const active = status.queue.find((job) => job.status === 'processing');
  const progress = active?.progress || 0;
  get('task-progress-num').textContent = `${progress}%`;
  get<HTMLDivElement>('task-progress-fill').style.transform = `scaleX(${progress / 100})`;
  get('task-filename-text').textContent = active?.name || uploadFile?.name || '等待选择音频文件…';
  get('task-status-badge').textContent = active?.status.toUpperCase() || (queuePaused ? 'QUEUE PAUSED' : status.mode === 'file_rvc' ? 'IDLE' : 'STOPPED');
  get<HTMLButtonElement>('btn-cancel').disabled = !active;
  updateFileSubmitState();
  const banner = get('pending-switch-banner');
  banner.classList.toggle('hidden', !status.pending_target_mode);
  if (status.pending_target_mode) get('pending-target-mode').textContent = status.pending_target_mode.toUpperCase();
  const selected = selectedMode();
  const running = status.mode === selected;
  document.querySelectorAll<HTMLInputElement>('.toggle-checkbox').forEach((toggle) => {
    const mode = toggle.id.replace('toggle-', '') as ActiveMode;
    const supported = status.capabilities[mode] === true;
    toggle.disabled = !supported;
    const row = toggle.closest<HTMLElement>('.mode-row');
    row?.classList.toggle('opacity-40', !supported);
    row?.classList.toggle('pointer-events-none', !supported);
  });
  const label = get('system-status-text');
  label.textContent = running ? '运行中' : status.mode === 'idle' ? '已停止' : '模式切换中';
  label.className = `text-sm font-extrabold leading-5 tracking-tight ${running ? 'text-emerald-600' : 'text-red-600'}`;
  document.querySelectorAll<HTMLButtonElement>('.transport-button').forEach((button) => {
    const active = status.mode === 'idle'
      ? button.dataset.state === 'stopped'
      : button.dataset.state === 'running';
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  document.querySelectorAll<SVGElement>('.system-state-icon').forEach((icon) => {
    icon.classList.toggle('hidden', icon.id !== (status.mode === 'idle' ? 'status-icon-stopped' : 'status-icon-running'));
  });
  get<HTMLButtonElement>('btn-global-run').disabled = !enabledMode || status.capabilities[enabledMode] !== true;
  const pause = get<HTMLButtonElement>('btn-global-pause');
  pause.disabled = status.mode !== 'file_rvc';
  pause.title = queuePaused ? '恢复文件队列' : '暂停队列（当前任务会完成）';
  pause.setAttribute('aria-label', pause.title);
  pause.classList.toggle('is-active', queuePaused);
};

const refreshStatus = async () => {
  try {
    const status = await api<Status>('/api/status');
    const modelChanged = lastStatus?.model.has_index !== status.model.has_index;
    renderStatus(status);
    if (modelChanged) renderParameters();
    apiOnline = true;
    setApiState(true);
  } catch (error) {
    apiOnline = false;
    setApiState(false);
  }
};

const showError = (error: unknown) => {
  const message = error instanceof Error ? error.message : '操作失败';
  get('toast-message').textContent = message;
  get('toast-notification').classList.remove('-translate-y-1', 'opacity-0');
  get('toast-notification').classList.add('translate-y-0', 'opacity-100');
};

const switchMode = async (mode: Mode): Promise<boolean> => {
  try {
    const modelId = get<HTMLSelectElement>('speaker-select').value;
    if (mode !== 'idle' && !modelId) throw new Error('请先选择可用模型');
    const result = await api<{ status: string; target_mode?: string }>('/api/mode/switch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, model_id: mode === 'idle' ? '' : modelId })
    });
    await refreshStatus();
    return true;
  } catch (error) { showError(error); return false; }
};

const refreshPresets = async () => {
  const response = await api<{ presets: Preset[] }>('/api/presets');
  presets = response.presets;
  const select = get<HTMLSelectElement>('parameter-preset-select');
  select.replaceChildren(new Option('当前参数', ''), ...presets.map((preset) => new Option(preset.name, preset.id)));
  select.disabled = false;
};

const applyPreset = async (id: string) => {
  const preset = presets.find((item) => item.id === id);
  if (!preset) return;
  await api('/api/parameters', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(preset.parameters) });
  await refreshParameters();
};

const refreshModels = async () => {
  const select = get<HTMLSelectElement>('speaker-select');
  try {
    const { models } = await api<ModelList>('/api/models');
    const available = models.filter((model) => model.exists);
    if (!available.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = '未找到可用模型';
      select.replaceChildren(option);
      select.disabled = true;
      return;
    }
    const selected = available.find((model) => model.current)?.id || select.value;
    select.replaceChildren(...available.map((model) => {
      const option = document.createElement('option');
      option.value = model.id;
      option.textContent = model.id;
      return option;
    }));
    select.value = available.some((model) => model.id === selected) ? selected : available[0].id;
    select.disabled = false;
  } catch (error) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '模型加载失败';
    select.replaceChildren(option);
    select.disabled = true;
    showError(error);
  }
};

const submitFile = async () => {
  if (!uploadFile) return showError(new Error('请先选择音频文件'));
  if (selectedMode() !== 'file_rvc') return showError(new Error('请先选择 FILE_RVC 模式'));
  if (enabledMode !== 'file_rvc') return showError(new Error('请先打开 FILE_RVC 开关并点击全局启动'));
  try {
    if (lastStatus?.mode !== 'file_rvc') return showError(new Error('FILE_RVC 尚未启动'));
    const data = new FormData();
    data.append('audio_file', uploadFile);
    data.append('model_id', get<HTMLSelectElement>('speaker-select').value);
    get<HTMLButtonElement>('btn-process').disabled = true;
    get('btn-process-text').textContent = '正在上传…';
    const result = await api<{ job_id: string }>('/api/file/convert', { method: 'POST', body: data });
    uploadFile = null;
    get<HTMLInputElement>('audio-file-input').value = '';
    get('selected-file-info').classList.add('hidden');
    get('btn-process-text').textContent = '上传并转换';
    await refreshStatus();
  } catch (error) {
    get('btn-process-text').textContent = '上传并转换';
    updateFileSubmitState();
    showError(error);
  }
};

const parameterInputMap: Record<string, keyof RvcParameters> = {
  f0Method: 'f0_method',
  pitch: 'pitch_shift',
  indexRate: 'index_rate',
  filterRadius: 'filter_radius',
  rmsMixRate: 'rms_mix_rate',
  protect: 'protect'
};

const parameterLabel = (key: keyof RvcParameters, value: string | number) => {
  if (key === 'f0_method') return String(value).toUpperCase();
  if (key === 'pitch_shift') return `${value} st`;
  return String(value);
};

const renderParameters = () => {
  const parameters: Array<{ key: keyof typeof parameterInputMap; label: string; min?: number; max?: number; step?: number; options?: string[] }> = [
    { key: 'f0Method', label: 'F0 提取', options: ['rmvpe'] },
    { key: 'pitch', label: '音高偏移', min: -12, max: 12, step: 1 },
    ...(lastStatus?.model.has_index === 'true' ? [{ key: 'indexRate' as const, label: '检索索引比例', min: 0, max: 1, step: 0.05 }] : []),
    { key: 'filterRadius', label: '滤波半径', min: 0, max: 7, step: 1 },
    { key: 'rmsMixRate', label: 'RMS 混合', min: 0, max: 1, step: 0.05 },
    { key: 'protect', label: '辅音保护', min: 0, max: 0.5, step: 0.05 }
  ];
  const container = get('params-container');
  container.replaceChildren(...parameters.map((parameter) => {
    const wrapper = document.createElement('div');
    wrapper.className = 'space-y-1.5 pb-2.5 border-b border-gray-200 last:border-b-0 last:pb-0';
    const header = document.createElement('div');
    header.className = 'flex items-center justify-between gap-3';
    const label = document.createElement('label');
    label.className = 'text-[10px] font-bold tracking-wide text-gray-500';
    label.textContent = parameter.label;
    const output = document.createElement('output');
    output.className = 'font-mono text-base font-extrabold leading-none text-gray-950 tabular-nums';
    const input = document.createElement(parameter.options ? 'select' : 'input');
    input.dataset.parameterKey = parameter.key;
    input.className = parameter.options
      ? 'w-full h-7 text-[10px] font-bold text-gray-700 bg-white border border-gray-200 rounded-sm px-2'
      : 'parameter-range';
    if (input instanceof HTMLSelectElement) {
      input.append(...parameter.options!.map((value) => new Option(value.toUpperCase(), value)));
    } else {
      input.type = 'range';
      input.min = String(parameter.min);
      input.max = String(parameter.max);
      input.step = String(parameter.step);
    }
    header.append(label, output);
    wrapper.append(header, input);
    return wrapper;
  }));
  get<HTMLButtonElement>('btn-reset-parameters').disabled = false;
  get<HTMLButtonElement>('btn-save-parameter-preset').disabled = false;
  get<HTMLButtonElement>('btn-delete-parameter-preset').disabled = true;
};

const refreshLogs = async () => {
  try { renderBackendLogs((await api<BackendLogs>('/api/logs?limit=200')).entries); }
  catch { /* Status polling surfaces API connectivity once without injecting client logs. */ }
};

const refreshParameters = async () => {
  try {
    const parameters = await api<RvcParameters>('/api/parameters');
    document.querySelectorAll<HTMLInputElement | HTMLSelectElement>('[data-parameter-key]').forEach((input) => {
      const key = input.dataset.parameterKey || '';
      const backendKey = parameterInputMap[key];
      if (!backendKey) {
        input.disabled = true;
        input.title = '当前 FILE_RVC worker 不支持此参数';
        return;
      }
      input.value = String(parameters[backendKey]);
      const output = input.previousElementSibling?.querySelector('output');
      if (output) output.textContent = parameterLabel(backendKey, parameters[backendKey]);
    });
  } catch (error) { showError(error); }
};

const submitParameters = async () => {
  const parameters: Record<string, string | number> = {};
  document.querySelectorAll<HTMLInputElement | HTMLSelectElement>('[data-parameter-key]').forEach((input) => {
    const backendKey = parameterInputMap[input.dataset.parameterKey || ''];
    if (!backendKey) return;
    parameters[backendKey] = input instanceof HTMLSelectElement ? input.value : Number(input.value);
  });
  try {
    await api('/api/parameters', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(parameters)
    });
    await refreshParameters();
  } catch (error) { showError(error); }
};

document.addEventListener('DOMContentLoaded', () => {
  renderParameters();
  selectMode(enabledMode || 'rt_rvc');
  document.querySelectorAll<HTMLInputElement>('.toggle-checkbox').forEach((toggle) => {
    toggle.checked = toggle.id === `toggle-${enabledMode}`;
  });
  for (const id of ['btn-voice-library', 'btn-rt-mute', 'btn-rt-bypass']) {
    const button = get<HTMLButtonElement>(id);
    button.disabled = true;
    button.classList.add('hidden');
  }
  get<HTMLInputElement>('audio-file-input').addEventListener('change', (event) => {
    const input = event.currentTarget as HTMLInputElement;
    uploadFile = (input.files || [])[0] || null;
    const selected = get('selected-file-info');
    selected.classList.toggle('hidden', !uploadFile);
    if (uploadFile) get('file-name-text').textContent = uploadFile.name;
    updateFileSubmitState();
  }, { capture: true });
  get('btn-process').addEventListener('click', (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    void submitFile();
  }, { capture: true });
  get('btn-global-run').addEventListener('click', (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    if (!enabledMode) return showError(new Error('请先打开一个已实现模式的开关'));
    selectMode(enabledMode);
    void switchMode(enabledMode).then(updateFileSubmitState);
  }, { capture: true });
  get('btn-global-pause').addEventListener('click', (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    const endpoint = queuePaused ? '/api/file/resume' : '/api/file/pause';
    void api(endpoint, { method: 'POST' }).then(refreshStatus).catch(showError);
  }, { capture: true });
  get('btn-global-stop').addEventListener('click', (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    void switchMode('idle').then(updateFileSubmitState);
  }, { capture: true });
  get('btn-cancel').addEventListener('click', (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    const active = lastStatus?.queue.find((job) => job.status === 'processing');
    if (!active) return;
    void api(`/api/file/cancel?job_id=${encodeURIComponent(active.job_id)}`, { method: 'DELETE' })
      .then(refreshStatus)
      .catch(showError);
  }, { capture: true });
  get('params-container').addEventListener('input', (event) => {
    const input = (event.target as HTMLElement).closest<HTMLInputElement | HTMLSelectElement>('[data-parameter-key]');
    if (!input || !(input instanceof HTMLInputElement) || !parameterInputMap[input.dataset.parameterKey || '']) return;
    event.stopImmediatePropagation();
    window.clearTimeout(parameterSyncTimer);
    parameterSyncTimer = window.setTimeout(() => void submitParameters(), 200);
  }, { capture: true });
  get('params-container').addEventListener('change', (event) => {
    const input = (event.target as HTMLElement).closest<HTMLInputElement | HTMLSelectElement>('[data-parameter-key]');
    if (!input || !(input instanceof HTMLSelectElement) || !parameterInputMap[input.dataset.parameterKey || '']) return;
    event.stopImmediatePropagation();
    void submitParameters();
  }, { capture: true });
  get<HTMLSelectElement>('log-mode-filter').addEventListener('change', (event) => {
    activeLogFilter = (event.currentTarget as HTMLSelectElement).value;
    void refreshLogs();
  });
  get<HTMLButtonElement>('btn-clear-logs').addEventListener('click', () => void api('/api/logs', { method: 'DELETE' }).then(refreshLogs).catch(showError));
  get<HTMLButtonElement>('btn-clear-finished').addEventListener('click', () => void api('/api/file/finished', { method: 'DELETE' }).then(refreshStatus).catch(showError));
  get<HTMLSelectElement>('parameter-preset-select').addEventListener('change', (event) => {
    const id = (event.currentTarget as HTMLSelectElement).value;
    get<HTMLButtonElement>('btn-delete-parameter-preset').disabled = !id;
    if (id) void applyPreset(id).catch(showError);
  });
  get<HTMLButtonElement>('btn-reset-parameters').addEventListener('click', () => void api('/api/parameters/reset', { method: 'POST' }).then(refreshParameters).catch(showError));
  get<HTMLButtonElement>('btn-save-parameter-preset').addEventListener('click', () => {
    const name = window.prompt('预设名称');
    if (!name?.trim()) return;
    const parameters: Record<string, string | number> = {};
    document.querySelectorAll<HTMLInputElement | HTMLSelectElement>('[data-parameter-key]').forEach((input) => {
      const backendKey = parameterInputMap[input.dataset.parameterKey || ''];
      if (backendKey) parameters[backendKey] = input instanceof HTMLSelectElement ? input.value : Number(input.value);
    });
    void api('/api/presets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim(), parameters }) })
      .then(refreshPresets).catch(showError);
  });
  get<HTMLButtonElement>('btn-delete-parameter-preset').addEventListener('click', () => {
    const id = get<HTMLSelectElement>('parameter-preset-select').value;
    if (!id || !window.confirm('删除该预设？')) return;
    void api(`/api/presets/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(refreshPresets).catch(showError);
  });
  document.querySelectorAll<HTMLButtonElement>('.mode-select').forEach((button) => {
    button.addEventListener('click', () => {
      const mode = button.dataset.mode as ActiveMode;
      if (lastStatus?.capabilities[mode] !== true) {
        showError(new Error('此模式尚未实现'));
        return;
      }
      modeSelectedByUser = true;
      selectMode(mode);
    });
  });
  document.querySelectorAll<HTMLInputElement>('.toggle-checkbox').forEach((toggle) => {
    toggle.addEventListener('change', (event) => {
      event.stopImmediatePropagation();
      if (toggle.checked) {
        const mode = toggle.id.replace('toggle-', '') as ActiveMode;
        modeSelectedByUser = true;
        document.querySelectorAll<HTMLInputElement>('.toggle-checkbox').forEach((other) => {
          if (other !== toggle) other.checked = false;
        });
        enabledMode = mode;
        window.localStorage.setItem('mozart-enabled-mode', mode);
        selectMode(mode);
      } else if (enabledMode === toggle.id.replace('toggle-', '')) {
        enabledMode = null;
        window.localStorage.removeItem('mozart-enabled-mode');
      }
      get<HTMLButtonElement>('btn-global-run').disabled = !enabledMode;
      updateFileSubmitState();
    }, { capture: true });
  });
  void refreshStatus();
  void refreshModels();
  void refreshLogs();
  void refreshParameters();
  void refreshPresets().catch(showError);
  void refreshMonitor().catch(() => undefined);
  window.setInterval(() => void refreshStatus(), 1000);
  window.setInterval(() => void refreshLogs(), 1500);
  window.setInterval(() => void refreshMonitor().catch(() => undefined), 2000);
});
