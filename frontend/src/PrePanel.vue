<script setup lang="ts">
// PrePanel.vue — 预处理链路的输入与输出设备选择。
// 设备清单由后端调用脚本枚举；应用后由脚本重启 mozart-pre，后端会话保持不变。
import { computed, ref, watch } from 'vue';
import type { PreDevice, PreProcessorStatus } from './api';

const props = defineProps<{
  status: PreProcessorStatus | null;
  busy: boolean;
  error: string;
}>();
const emit = defineEmits<{ apply: [payload: { capture: string; playback: string }] }>();

const capture = ref('');
const playback = ref('');
let baselineCapture = '', baselinePlayback = '';

watch(() => props.status, (value) => {
  if (!value) return;
  const nextCapture = value.current?.capture || value.devices.capture[0]?.id || '';
  const nextPlayback = value.current?.playback || value.devices.playback[0]?.id || '';
  if (capture.value === baselineCapture) capture.value = nextCapture;
  if (playback.value === baselinePlayback) playback.value = nextPlayback;
  baselineCapture = nextCapture;
  baselinePlayback = nextPlayback;
}, { immediate: true, deep: true });

const stateLabel = computed(() => {
  const state = props.status?.state?.state;
  return typeof state === 'string' ? state : 'unknown';
});
const disabled = computed(() => props.busy || !props.status?.available);
const unchanged = computed(() => !!props.status?.current
  && capture.value === props.status.current.capture
  && playback.value === props.status.current.playback);

const apply = () => {
  if (!capture.value || !playback.value) return;
  emit('apply', { capture: capture.value, playback: playback.value });
};

interface DeviceGroup { key: string; label: string; devices: PreDevice[] }

const GROUP_LABELS: Record<string, string> = { local: '本机声卡', usb: 'USB 协议' };

// 下拉框按 group 分组；后端没有 group 时归入本机声卡。
const groupDevices = (devices: PreDevice[]): DeviceGroup[] => {
  const groups: DeviceGroup[] = [];
  for (const device of devices) {
    const key = device.group ?? 'local';
    let group = groups.find(item => item.key === key);
    if (!group) {
      group = { key, label: GROUP_LABELS[key] ?? key, devices: [] };
      groups.push(group);
    }
    group.devices.push(device);
  }
  return groups;
};

const captureGroups = computed(() => groupDevices(props.status?.devices.capture ?? []));
const playbackGroups = computed(() => groupDevices(props.status?.devices.playback ?? []));

// 选中 USB 协议项时显示桥状态。桥未运行时给出原因与启动提示。
const bridgeNote = computed(() => {
  const devices = props.status?.devices;
  if (!devices) return '';
  const usbIds = [...devices.capture, ...devices.playback]
    .filter(device => device.group === 'usb').map(device => device.id);
  if (!usbIds.includes(capture.value) && !usbIds.includes(playback.value)) return '';
  const bridge = devices.bridge;
  if (!bridge) return 'USB 桥：状态未知';
  if (!bridge.active) {
    return `USB 桥未运行：${bridge.reason ?? bridge.last_error ?? 'USB 链路不可用'}`;
  }
  const error = bridge.last_error ? ` · 最近错误 ${bridge.last_error}` : '';
  return `USB 桥：收 ${bridge.received ?? 0} 帧 · 丢 ${bridge.dropped ?? 0}${error}`;
});
</script>

<template>
  <div class="card p-3" aria-label="音频输入与输出设备">
    <div class="flex items-center justify-between mb-2">
      <h2 class="text-xs font-extrabold tracking-wider text-ink uppercase section-title">音频通路</h2>
      <span class="text-micro font-mono font-bold text-ink-2 bg-surface border border-edge rounded-sm px-2 py-1" translate="no">
        {{ stateLabel.toUpperCase() }}
      </span>
    </div>

    <p v-if="error" class="text-xs state-err mb-2" role="alert">{{ error }}</p>
    <p v-if="!status" class="text-xs text-ink-2">正在读取设备…</p>
    <p v-else-if="!status.available" class="text-xs text-ink-2">
      {{ status.error ?? '后端未配置预处理控制' }}
    </p>
    <template v-else>
      <label for="pre-capture" class="block text-micro font-bold tracking-wide text-ink-3 mb-1">输入（采集）</label>
      <select id="pre-capture" v-model="capture" class="field w-full min-w-0 text-xs px-3 py-2 mb-2.5" :disabled="disabled" autocomplete="off">
        <optgroup v-for="group in captureGroups" :key="group.key" :label="group.label">
          <option v-for="device in group.devices" :key="device.id" :value="device.id">{{ device.label }} — {{ device.id }}</option>
        </optgroup>
      </select>

      <label for="pre-playback" class="block text-micro font-bold tracking-wide text-ink-3 mb-1">输出（播放）</label>
      <select id="pre-playback" v-model="playback" class="field w-full min-w-0 text-xs px-3 py-2 mb-2.5" :disabled="disabled" autocomplete="off">
        <optgroup v-for="group in playbackGroups" :key="group.key" :label="group.label">
          <option v-for="device in group.devices" :key="device.id" :value="device.id">{{ device.label }} — {{ device.id }}</option>
        </optgroup>
      </select>

      <p v-if="bridgeNote" class="text-micro text-ink-3 mb-2.5" role="status">{{ bridgeNote }}</p>

      <button type="button" class="btn btn-accent btn-sm w-full" :disabled="disabled || !capture || !playback || unchanged" @click="apply">
        {{ busy ? '正在切换…' : unchanged ? '已生效' : '应用设备' }}
      </button>


    </template>
  </div>
</template>
