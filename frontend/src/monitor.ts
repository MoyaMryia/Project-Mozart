interface MonitorSnapshot {
  cpu_percent: number | null;
  memory: { total_bytes: number; used_bytes: number };
  pipewire: { available: boolean };
  cuda: { available: boolean };
  gpu: { available: boolean; load_percent: number; memory_type: string; memory_total_bytes: number; memory_used_bytes: number };
  vad: { available: boolean; voiced_percent: number; confidence_percent: number };
}

const get = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const gibibytes = (bytes: number) => (bytes / 1024 ** 3).toFixed(1);

const setState = (id: string, text: string, online: boolean | null) => {
  const element = get(id);
  element.textContent = text;
  element.className = `font-mono text-[11px] font-bold ${online === true ? 'text-emerald-600' : online === false ? 'text-red-600' : 'text-gray-500'}`;
};

export const refreshMonitor = async (): Promise<void> => {
  const response = await fetch('/api/monitor');
  if (!response.ok) throw new Error(`Monitor returned HTTP ${response.status}`);
  const monitor = await response.json() as MonitorSnapshot;
  get('stat-latency').innerHTML = monitor.cpu_percent === null
    ? '-- <span class="text-[10px] font-semibold text-gray-500">%</span>'
    : `${monitor.cpu_percent.toFixed(1)} <span class="text-[10px] font-semibold text-gray-500">%</span>`;
  get('stat-cache').innerHTML = `${gibibytes(monitor.memory.used_bytes)} <span class="text-[10px] font-semibold text-gray-500">/ ${gibibytes(monitor.memory.total_bytes)} GB</span>`;
  get('stat-vram').textContent = monitor.gpu.available
    ? `${monitor.gpu.load_percent.toFixed(0)}% / ${gibibytes(monitor.gpu.memory_used_bytes)} GB`
    : 'UNAVAILABLE';
  setState('state-pipewire', monitor.pipewire.available ? 'CONNECTED' : 'OFFLINE', monitor.pipewire.available);
  setState('state-cuda-priority', monitor.cuda.available ? 'AVAILABLE' : 'UNAVAILABLE', monitor.cuda.available);
  setState('state-vad', monitor.vad.available
    ? `${monitor.vad.voiced_percent.toFixed(0)}% VOICED`
    : 'NO STREAM', monitor.vad.available ? true : null);
};

export const setApiState = (online: boolean) => setState('state-api', online ? 'ONLINE' : 'OFFLINE', online);
