<script setup lang="ts">
// PrePanel.vue — 预处理链路的输入与输出设备选择。
// 设备清单由后端调用脚本枚举；应用后由脚本重启 mozart-pre，后端会话保持不变。
import { computed, ref, watch } from 'vue';
import type { PreProcessorStatus } from './api';

const props = defineProps<{
  status: PreProcessorStatus | null;
  busy: boolean;
  error: string;
}>();
const emit = defineEmits<{ apply: [payload: { capture: string; playback: string }] }>();

const capture = ref('');
const playback = ref('');

watch(() => props.status, (value) => {
  if (!value) return;
  if (value.current?.capture) capture.value = value.current.capture;
  else if (!capture.value && value.devices.capture.length) capture.value = value.devices.capture[0].id;
  if (value.current?.playback) playback.value = value.current.playback;
  else if (!playback.value && value.devices.playback.length) playback.value = value.devices.playback[0].id;
}, { immediate: true, deep: true });

const stateLabel = computed(() => {
  const state = props.status?.state?.state;
  return typeof state === 'string' ? state : 'unknown';
});
const frames = computed(() => {
  const value = props.status?.state?.frames;
  return typeof value === 'number' ? value : null;
});
const channels = computed(() => {
  if (!props.status?.available) return 0;
  return props.status.devices.capture.length + props.status.devices.playback.length;
});
const disabled = computed(() => props.busy || !props.status?.available);
const unchanged = computed(() => !!props.status?.current
  && capture.value === props.status.current.capture
  && playback.value === props.status.current.playback);

const apply = () => {
  if (!capture.value || !playback.value) return;
  emit('apply', { capture: capture.value, playback: playback.value });
};
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
        <option v-for="device in status.devices.capture" :key="device.id" :value="device.id">{{ device.label }} — {{ device.id }}</option>
      </select>

      <label for="pre-playback" class="block text-micro font-bold tracking-wide text-ink-3 mb-1">输出（播放）</label>
      <select id="pre-playback" v-model="playback" class="field w-full min-w-0 text-xs px-3 py-2 mb-2.5" :disabled="disabled" autocomplete="off">
        <option v-for="device in status.devices.playback" :key="device.id" :value="device.id">{{ device.label }} — {{ device.id }}</option>
      </select>

      <button type="button" class="btn btn-accent btn-sm w-full" :disabled="disabled || !capture || !playback || unchanged" @click="apply">
        {{ busy ? '正在切换…' : unchanged ? '已生效' : '应用设备' }}
      </button>

      <div class="grid grid-cols-2 gap-2 mt-2.5 font-mono text-micro text-ink-3">
        <span class="truncate" translate="no">{{ channels }} CHANNELS</span>
        <span class="text-right truncate" translate="no">{{ frames === null ? '无状态' : `frames ${frames}` }}</span>
      </div>
    </template>
  </div>
</template>
