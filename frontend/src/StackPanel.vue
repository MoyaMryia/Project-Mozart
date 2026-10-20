<script setup lang="ts">
import type { StackStatus } from './api';
import { stackPhaseLabel, voiceWaitingText } from './stack';
defineProps<{ status: StackStatus | null; error: string }>();
</script>

<template>
  <section class="card p-4 mb-4 space-y-2" aria-label="现场会话状态" aria-live="polite">
    <h2 class="text-xs font-extrabold text-ink">现场会话</h2>
    <p v-if="error" class="text-xs state-err" role="alert">现场状态不可用：{{ error }}</p>
    <p v-else-if="!status" class="text-xs text-ink-2">正在读取现场状态…</p>
    <p v-else-if="!status.managed" class="text-xs text-ink-2">当前部署没有现场会话。可单独使用变声和音色试听。</p>
    <template v-else-if="status.available">
      <p class="text-sm font-bold" :class="status.state === 'failed' ? 'state-err' : 'text-ink'">{{ stackPhaseLabel(status) }}</p>
      <p v-if="status.phase === 'waiting_for_voice' && status.requirements" class="text-xs text-ink-2">{{ voiceWaitingText(status.requirements) }}</p>
      <div v-if="status.readiness" class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
        <span>翻译模型：{{ status.readiness.translation ? '就绪' : '未就绪' }}</span>
        <template v-if="status.requirements?.voice_required">
          <span>语音合成：{{ status.readiness.speech_runtime ? '就绪' : '未就绪' }}</span>
          <span>参考音色：{{ status.readiness.voice_session ? '已启用' : '等待选择或应用' }}</span>
        </template>
      </div>
      <p v-for="(reason, service) in status.degraded" :key="service" class="text-xs state-warn">{{ service }}：{{ reason }}</p>
      <p v-if="status.error" class="text-xs state-err" role="alert">{{ status.error.message }}</p>
      <p v-if="status.updated_at" class="text-tiny text-ink-3">记录更新时间：{{ new Date(status.updated_at * 1000).toLocaleTimeString('zh-CN', { hour12: false }) }}</p>
    </template>
    <p v-else class="text-xs state-err">现场状态不可用。</p>
  </section>
</template>
