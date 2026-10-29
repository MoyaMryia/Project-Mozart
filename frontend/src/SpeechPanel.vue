<script setup lang="ts">
// 参考音色与翻译播报面板。运行状态轮询保留用户正在编辑的选择。
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { api, ApiRequestError, type StackStatus } from './api';
import { createPoll } from './poll';
import { outputMatches, requiredOutputLabel } from './stack';
import { speechJobLabel, speechRuntimeLabel, type SpeechRuntimeStatus } from './speech';
class ReferenceReadError extends Error {}
const props = defineProps<{ stackStatus?: StackStatus | null }>();
const lifecycle = new AbortController();
const speechApi = <T,>(path: string, init?: RequestInit) => api<T>(path, { ...init, signal: init?.signal ?? lifecycle.signal });
interface Voice { id: string; name: string; status: string; duration_seconds: number; identity_approved: boolean }
interface SpeechJob { id: string; text: string; status: string; result_url: string; error?: string; duration_seconds?: number; synthesis_seconds?: number; chunk_count?: number; played_chunks?: number; admission_pending?: boolean }
interface SpeechStatus extends SpeechRuntimeStatus { control_generation?: number; storage_warning?: string | null; engine: string; voices: Voice[]; jobs: SpeechJob[]; languages: string[]; queue_limit: number; delivery_policy?: string; pending_jobs?: number; live_audio_budget_seconds?: number; playback_device: string; session: { enabled: boolean; voice_id: string; language: string; playback: boolean } }
const state = ref<SpeechStatus | null>(null);
const unavailable = ref('');
const error = ref('');
const busy = ref(false);
const stopping = ref(false);
const previewId = ref('');
const previewJob = ref<SpeechJob | null>(null);
const previewError = ref('');
let actionController: AbortController | null = null;
const name = ref('');
const transcript = ref('');
const referenceLanguage = ref('zh');
const selectedVoice = ref('');
const language = ref('en');
const text = ref('Hello. This is a preview of the selected reference voice.');
const reference = ref<File | null>(null);
const playback = ref(false);
let timer: number;
const recentJobs = computed(() => state.value?.jobs.filter(job => job.id !== previewId.value).slice(0, 6) ?? []);
const runtimeReady = computed(() => state.value?.runtime_ready === true);
const preparationPending = computed(() => ['queued', 'preparing'].includes(state.value?.runtime_state ?? ''));
const restartRequired = computed(() => state.value?.worker_error
  ? '语音工作线程已停止，请重启语音服务。'
  : state.value?.runtime_state === 'closed' ? '语音服务已关闭，请重启语音服务。' : '');
const waitingRequirements = computed(() => props.stackStatus?.available && props.stackStatus.phase === 'waiting_for_voice'
  && props.stackStatus.requirements?.voice_required ? props.stackStatus.requirements : null);
const draftMatchesRequirements = computed(() => !waitingRequirements.value
  || outputMatches(waitingRequirements.value, language.value, playback.value));
const selectedVoiceAvailable = computed(() => state.value?.voices.some(voice => voice.id === selectedVoice.value) ?? false);
let initialized = false;
const selectedSessionActive = computed(() => !!state.value?.session.enabled && state.value.session.voice_id === selectedVoice.value && state.value.session.language === language.value && state.value.session.playback === playback.value);
const sessionVoiceName = computed(() => state.value?.voices.find(v => v.id === state.value?.session.voice_id)?.name ?? '');
const jsonPost = (body: unknown) => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const controlPost = (body: Record<string, unknown>) => jsonPost({
  ...body, expected_control_generation: state.value?.control_generation,
});
const speechPoll = createPoll(async signal => {
  const value = await speechApi<SpeechStatus>('/api/speech/status', { signal });
  const id = previewId.value;
  let preview = value.jobs.find(job => job.id === id) ?? null;
  let previewFailure = '';
  if (id && !preview) {
    try { preview = await speechApi<SpeechJob>(`/api/speech/jobs/${id}`, { signal }); }
    catch (failure) {
      if (!(failure instanceof ApiRequestError)) throw failure;
      previewFailure = failure.message;
    }
  }
  return { value, id, preview, previewFailure };
}, ({ value, id, preview, previewFailure }) => {
  state.value = value;
  if (id === previewId.value) { previewJob.value = preview; previewError.value = previewFailure; }
  unavailable.value = '';
  if (!initialized) {
    selectedVoice.value = value.session.voice_id || value.voices[0]?.id || '';
    language.value = value.session.language; playback.value = value.session.playback; initialized = true;
  }
}, failure => { state.value = null; unavailable.value = failure instanceof Error ? failure.message : 'Speech service unavailable'; });
const refresh = (replace = false) => speechPoll.run(replace);
const matchOutput = () => {
  const requirements = waitingRequirements.value;
  if (requirements) { language.value = requirements.language; playback.value = requirements.playback; }
};
async function action(fn: (signal: AbortSignal) => Promise<unknown>) {
  if (busy.value || stopping.value) return;
  const controller = new AbortController();
  actionController = controller;
  busy.value = true; error.value = '';
  try { await fn(controller.signal); if (!controller.signal.aborted) await refresh(true); }
  catch (e) {
    if (!lifecycle.signal.aborted && !controller.signal.aborted) {
      if (!(e instanceof ApiRequestError) && !(e instanceof ReferenceReadError)) throw e;
      error.value = e.message;
    }
  }
  finally {
    if (actionController === controller) { busy.value = false; actionController = null; }
  }
}
async function upload() {
  if (!reference.value) return;
  const file = reference.value;
  if (file.size > 5 * 1024 ** 2) { error.value = 'Reference exceeds 5 MiB'; return; }
  await action(async signal => {
    const audio_base64 = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1] ?? '');
      reader.onerror = () => reject(new ReferenceReadError(reader.error!.message, { cause: reader.error }));
      reader.readAsDataURL(file);
    });
    const voice = await speechApi<Voice>('/api/voices', { ...jsonPost({ name: name.value || file.name, audio_base64, transcript: transcript.value, language: referenceLanguage.value }), signal });
    selectedVoice.value = voice.id;
  });
}
const synthesize = () => action(async signal => {
  const job = await speechApi<SpeechJob>('/api/speech/jobs', { ...controlPost({ text: text.value, language: language.value, voice_id: selectedVoice.value, playback: playback.value }), signal });
  if (!signal.aborted) { previewId.value = job.id; previewJob.value = job; previewError.value = ''; }
});
const session = (enabled: boolean) => action(signal => speechApi('/api/speech/session', { ...controlPost(enabled ? { enabled, voice_id: selectedVoice.value, language: language.value, playback: playback.value } : { enabled }), signal }));
const prepare = () => action(signal => speechApi('/api/speech/prepare', { ...controlPost({}), signal }));
async function stop() {
  if (stopping.value) return;
  stopping.value = true; error.value = '';
  actionController?.abort(); actionController = null; busy.value = false;
  try {
    const result = await speechApi<{ stop_errors?: number }>('/api/speech/stop', jsonPost({}));
    await refresh(true);
    if (result.stop_errors) error.value = '停止操作发生错误，请检查语音状态并重试。';
  } catch (e) {
    if (!lifecycle.signal.aborted) {
      if (!(e instanceof ApiRequestError)) throw e;
      error.value = e.message;
    }
  } finally { stopping.value = false; }
}
const cancel = (id: string) => action(signal => speechApi(`/api/speech/jobs/${id}`, { method: 'DELETE', signal }));
const remove = (id: string) => action(async signal => { await speechApi(`/api/voices/${id}`, { method: 'DELETE', signal }); if (selectedVoice.value === id) selectedVoice.value = ''; });
onMounted(() => { void refresh(); timer = window.setInterval(() => void refresh(), 1500); });
onUnmounted(() => { clearInterval(timer); speechPoll.stop(); actionController?.abort(); lifecycle.abort(); });
</script>

<template>
  <section class="card shrink-0 p-4 space-y-3" aria-label="翻译播报">
    <div class="flex items-center justify-between gap-3">
      <h2 class="text-sm font-extrabold text-ink">翻译播报</h2>
      <span class="text-xs font-bold" :class="runtimeReady ? 'state-ok' : 'state-unknown'">{{ state ? speechRuntimeLabel(state) : '连接不可用' }}</span>
    </div>
    <p v-if="unavailable" class="text-xs text-ink-2" role="status">{{ unavailable }}</p>
    <template v-if="state">
      <p v-if="state.runtime_error || state.worker_error || state.warmup_error" class="text-xs state-err" role="alert">{{ state.runtime_error || state.worker_error?.message || state.warmup_error }}</p>
      <p v-if="state.storage_warning" class="text-xs state-warn" role="alert">存储异常：{{ state.storage_warning }}</p>
      <p v-if="restartRequired" class="text-xs state-err" role="status">{{ restartRequired }}</p>
      <div class="flex flex-wrap items-end gap-2">
        <label class="form-label flex-1 min-w-[140px]">音色
          <select v-model="selectedVoice" class="field block w-full p-2 mt-1 text-xs font-normal">
            <option value="" disabled>请选择音色</option>
            <option v-if="selectedVoice && !selectedVoiceAvailable" :value="selectedVoice" disabled>所选音色不可用</option>
            <option v-for="voice in state.voices" :key="voice.id" :value="voice.id">{{ voice.name }}{{ voice.status === 'needs_preview' ? ' · 待试听' : '' }}</option>
          </select>
        </label>
        <label class="form-label">语言
          <select v-model="language" class="field block p-2 mt-1 text-xs font-normal"><option v-for="lang in state.languages" :key="lang" :value="lang">{{ lang === 'en' ? '英文' : lang === 'zh' ? '中文' : lang }}</option></select>
        </label>
      </div>
      <label class="text-xs text-ink-2 flex gap-2 items-center"><input v-model="playback" type="checkbox" class="accent-[var(--ink)]">{{ state.playback_device === 'null' ? '无声模拟播放' : 'Jetson 扬声器播放' }}</label>
      <p v-if="waitingRequirements?.playback && state.playback_device === 'null'" class="text-xs state-warn" role="status">当前使用无声模拟输出。</p>
      <button v-if="waitingRequirements && !draftMatchesRequirements" class="btn btn-outline btn-md" :disabled="busy || stopping" @click="matchOutput">使用现场输出：{{ waitingRequirements.language === 'en' ? '英文' : waitingRequirements.language }} · {{ requiredOutputLabel(waitingRequirements) }}</button>
      <div class="sticky top-0 z-10 bg-surface py-2 flex flex-wrap gap-2" role="group" aria-label="播报控制">
        <button v-if="!runtimeReady && !restartRequired" class="btn btn-outline btn-md" :disabled="busy || stopping || preparationPending" @click="prepare">{{ preparationPending ? speechRuntimeLabel(state) : '准备／恢复语音' }}</button>
        <button class="btn btn-accent btn-md" :disabled="busy || stopping || (!state.session.enabled && (!runtimeReady || !selectedVoiceAvailable))" @click="session(!state.session.enabled)">{{ state.session.enabled ? '禁用翻译播报' : '启用翻译播报' }}</button>
        <button v-if="state.session.enabled && !selectedSessionActive" class="btn btn-outline btn-md" :disabled="busy || stopping || !runtimeReady || !selectedVoiceAvailable" @click="session(true)">应用音色与输出</button>
        <button class="btn btn-danger btn-md" :disabled="stopping" @click="stop">{{ stopping ? '正在停止…' : '停止播报并清空队列' }}</button>
      </div>
      <p v-if="state.session.enabled && !selectedSessionActive" class="text-xs text-ink-3" role="status">当前：{{ sessionVoiceName }} · {{ state.session.language === 'en' ? '英文' : state.session.language }} · {{ state.session.playback ? '设备播放' : '仅生成文件' }}</p>
      <p v-if="state.pending_jobs" class="text-xs text-ink-3" role="status">排队 {{ state.pending_jobs }} 项</p>
      <p v-if="error" role="alert" class="text-xs state-err">{{ error }}</p>

      <div class="border-t border-edge-soft pt-3 space-y-2" aria-label="音色试听">
        <label for="speech-preview-text" class="sr-only">试听文本</label>
        <div class="flex items-end gap-2">
          <textarea id="speech-preview-text" v-model="text" maxlength="500" rows="2" class="field block flex-1 min-w-0 p-2 text-xs font-normal"></textarea>
          <button class="btn btn-outline btn-md shrink-0" :disabled="busy || stopping || !runtimeReady || !selectedVoiceAvailable || !text.trim()" @click="synthesize">生成试听</button>
        </div>
        <p v-if="previewError" class="text-xs state-err" role="alert">{{ previewError }}</p>
        <div v-if="previewJob" class="text-xs space-y-2" aria-label="试听结果">
          <div class="flex items-center justify-between gap-2">
            <span class="text-ink-2" role="status">{{ speechJobLabel(previewJob) }}</span>
            <button v-if="['queued', 'processing', 'ready', 'playing'].includes(previewJob.status)" class="link-muted underline" :disabled="busy || stopping" @click="cancel(previewJob.id)">取消试听</button>
          </div>
          <p v-if="previewJob.error" class="state-err" role="alert">{{ previewJob.error }}</p>
          <template v-if="previewJob.status === 'completed'">
            <audio controls preload="none" class="w-full h-9" :src="previewJob.result_url"></audio>
            <a :href="previewJob.result_url" class="link-muted underline" download>下载试听</a>
          </template>
        </div>
      </div>

      <details class="text-xs border-t border-edge-soft pt-3" aria-label="音色管理" :open="state.voices.length === 0">
        <summary class="font-bold text-ink cursor-pointer">音色管理</summary>
        <div class="grid gap-3 mt-3">
          <label class="form-label">音色名称
            <input v-model="name" maxlength="80" class="field block w-full p-2 mt-1 text-xs font-normal">
          </label>
          <label class="form-label">参考录音（WAV，3–15 s）
            <input type="file" accept=".wav,audio/wav" class="field-file block w-full mt-1 font-normal" @change="reference = ($event.target as HTMLInputElement).files?.[0] ?? null">
          </label>
          <label class="form-label">录音语言
            <select v-model="referenceLanguage" class="field ml-2 px-2 py-1.5 text-xs font-normal"><option value="zh">中文</option><option value="en">英文</option></select>
          </label>
          <label class="form-label">{{ state.engine === 'zipvoice' ? '录音原文（必填）' : '录音原文（选填）' }}
            <textarea v-model="transcript" maxlength="1000" rows="2" class="field block w-full p-2 mt-1 text-xs font-normal"></textarea>
          </label>
          <div class="flex flex-wrap gap-2">
            <button class="btn btn-accent btn-md" :disabled="busy || stopping || !reference" @click="upload">保存音色</button>
            <button class="btn btn-danger btn-md" :disabled="busy || stopping || !selectedVoice" @click="remove(selectedVoice)">删除所选音色</button>
          </div>
        </div>
      </details>

      <details class="text-xs border-t border-edge-soft pt-3" aria-label="播报任务">
        <summary class="font-bold text-ink cursor-pointer">最近任务（{{ recentJobs.length }}）</summary>
        <div class="space-y-3 mt-3">
          <p v-if="!recentJobs.length" class="text-ink-3">暂无其他任务。</p>
          <div v-for="job in recentJobs" :key="job.id" class="border-t border-edge-soft pt-2 space-y-1">
            <div class="flex justify-between gap-2">
              <span class="font-bold text-ink">{{ speechJobLabel(job) }}</span>
              <button v-if="['queued', 'processing', 'ready', 'playing'].includes(job.status)" class="link-muted underline" :disabled="busy || stopping" @click="cancel(job.id)">取消任务</button>
            </div>
            <p class="text-ink-2 break-words">{{ job.text }}</p>
            <p v-if="job.error" class="state-err">{{ job.error }}</p>
            <p v-if="job.status === 'playing' && (job.chunk_count ?? 0) > 1" class="text-ink-3">{{ job.played_chunks ?? 0 }} / {{ job.chunk_count }} 段</p>
            <template v-if="job.status === 'completed'">
              <audio controls preload="none" class="w-full h-9" :src="job.result_url"></audio>
              <a :href="job.result_url" class="link-muted underline" download>下载 WAV</a>
              <span v-if="job.duration_seconds !== undefined" class="ml-3 text-ink-3">{{ job.duration_seconds.toFixed(1) }} s</span>
            </template>
          </div>
        </div>
      </details>
    </template>
    <p v-if="error && !state" role="alert" class="text-xs state-err">{{ error }}</p>
  </section>
</template>
