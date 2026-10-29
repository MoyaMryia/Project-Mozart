<script setup lang="ts">
import type { StackStatus } from './api';
import { stackPhaseLabel, requiredOutputLabel } from './stack';
defineProps<{ status: StackStatus | null; error: string }>();
</script>

<template>
  <section class="card p-3 mb-3 space-y-2" aria-label="现场会话状态" aria-live="polite">
    <p v-if="error" class="text-xs state-err" role="alert">现场状态不可用：{{ error }}</p>
    <p v-else-if="!status" class="text-xs text-ink-2">连接中…</p>
    <p v-else-if="!status.managed" class="text-xs text-ink-2">独立模式</p>
    <template v-else-if="status.available">
      <div class="flex items-center justify-between gap-3">
        <span class="text-sm font-bold" :class="status.state === 'failed' ? 'state-err' : 'text-ink'">{{ stackPhaseLabel(status) }}</span>
        <time v-if="status.updated_at" class="text-tiny text-ink-3" title="记录更新时间" :datetime="new Date(status.updated_at * 1000).toISOString()">{{ new Date(status.updated_at * 1000).toLocaleTimeString('zh-CN', { hour12: false }) }}</time>
      </div>
      <div v-if="status.readiness" class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
        <span>翻译{{ status.readiness.translation ? '就绪' : '未就绪' }}</span>
        <template v-if="status.requirements?.voice_required">
          <span>语音{{ status.readiness.speech_runtime ? '就绪' : '未就绪' }}</span>
          <span>音色{{ status.readiness.voice_session ? '已启用' : '未启用' }}</span>
        </template>
      </div>
      <p v-if="status.phase === 'waiting_for_voice' && status.requirements" class="text-xs text-ink-2">{{ status.requirements.language === 'en' ? '英文' : status.requirements.language }} · {{ requiredOutputLabel(status.requirements) }}</p>
      <p v-for="(reason, service) in status.degraded" :key="service" class="text-xs state-warn">{{ service }}：{{ reason }}</p>
      <p v-if="status.error" class="text-xs state-err" role="alert">{{ status.error.message }}</p>
    </template>
    <p v-else class="text-xs state-err">现场状态不可用。</p>
  </section>
</template>
