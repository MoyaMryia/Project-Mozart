<script setup lang="ts">
// 参考音色与翻译播报面板。运行状态轮询保留用户正在编辑的选择。
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { api, type StackStatus } from './api';
import { createPoll } from './poll';
import { outputMatches, requiredOutputLabel } from './stack';
import { speechRuntimeLabel, type SpeechRuntimeStatus } from './speech';
const props = defineProps<{ stackStatus?: StackStatus | null }>();
const lifecycle = new AbortController();
const speechApi = <T,>(path: string, init?: RequestInit) => api<T>(path, { ...init, signal: init?.signal ?? lifecycle.signal });
interface Voice { id: string; name: string; status: string; duration_seconds: number; identity_approved: boolean }
interface SpeechJob { id: string; text: string; status: string; result_url: string; error?: string; duration_seconds?: number; synthesis_seconds?: number; chunk_count?: number; played_chunks?: number; admission_pending?: boolean }
interface SpeechStatus extends SpeechRuntimeStatus { storage_warning?: string | null; engine: string; voices: Voice[]; jobs: SpeechJob[]; languages: string[]; queue_limit: number; delivery_policy?: string; pending_jobs?: number; live_audio_budget_seconds?: number; playback_device: string; session: { enabled: boolean; voice_id: string; language: string; playback: boolean } }
const state = ref<SpeechStatus | null>(null);
const unavailable = ref('');
const error = ref('');
const busy = ref(false);
const name = ref('');
const transcript = ref('');
const referenceLanguage = ref('zh');
const selectedVoice = ref('');
const language = ref('en');
const text = ref('Hello. This is a preview of the selected reference voice.');
const reference = ref<File | null>(null);
const playback = ref(false);
let timer: number;
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
const speechPoll = createPoll(signal => speechApi<SpeechStatus>('/api/speech/status', { signal }), value => {
  state.value = value;
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
async function action(fn: () => Promise<unknown>) {
  if (busy.value) return;
  busy.value = true; error.value = '';
  try { await fn(); await refresh(true); }
  catch (e) { if (!lifecycle.signal.aborted) error.value = e instanceof Error ? e.message : 'Speech request failed'; }
  finally { busy.value = false; }
}
async function upload() {
  if (!reference.value) return;
  const file = reference.value;
  if (file.size > 5 * 1024 ** 2) { error.value = 'Reference exceeds 5 MiB'; return; }
  await action(async () => {
    const audio_base64 = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1] ?? '');
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
    const voice = await speechApi<Voice>('/api/voices', jsonPost({ name: name.value || file.name, audio_base64, transcript: transcript.value, language: referenceLanguage.value }));
    selectedVoice.value = voice.id;
  });
}
const synthesize = () => action(() => speechApi('/api/speech/jobs', jsonPost({ text: text.value, language: language.value, voice_id: selectedVoice.value, playback: playback.value })));
const session = (enabled: boolean) => action(() => speechApi('/api/speech/session', jsonPost({ enabled, voice_id: selectedVoice.value, language: language.value, playback: playback.value })));
const prepare = () => action(() => speechApi('/api/speech/prepare', jsonPost({})));
const stop = () => action(() => speechApi('/api/speech/stop', jsonPost({})));
const cancel = (id: string) => action(() => speechApi(`/api/speech/jobs/${id}`, { method: 'DELETE' }));
const remove = (id: string) => action(async () => { await speechApi(`/api/voices/${id}`, { method: 'DELETE' }); if (selectedVoice.value === id) selectedVoice.value = ''; });
onMounted(() => { void refresh(); timer = window.setInterval(() => void refresh(), 1500); });
onUnmounted(() => { clearInterval(timer); speechPoll.stop(); lifecycle.abort(); });
</script>

<template>
  <section class="card p-4 mb-4 space-y-3" aria-label="Reference voice and translated speech">
    <div class="flex items-center justify-between gap-3">
      <h2 class="text-xs font-extrabold tracking-wider text-ink uppercase section-title">参考音色与英文播报</h2>
      <span class="text-tiny font-mono font-bold" :class="runtimeReady ? 'state-ok' : 'state-unknown'">{{ state ? speechRuntimeLabel(state) : '连接不可用' }}</span>
    </div>
    <p v-if="unavailable" class="text-xs text-ink-2" role="status">{{ unavailable }}</p>
    <template v-if="state">
      <p v-if="state.runtime_error || state.worker_error || state.warmup_error" class="text-xs state-err" role="alert">{{ state.runtime_error || state.worker_error?.message || state.warmup_error }}</p>
      <div v-if="!runtimeReady" class="text-xs space-y-2" role="status">
        <p v-if="restartRequired" class="state-err">{{ restartRequired }}</p>
        <template v-else>
          <p>请先准备语音模型，再生成预览或启用翻译播报。</p>
          <button class="btn btn-outline btn-md" :disabled="busy || preparationPending" @click="prepare">{{ preparationPending ? speechRuntimeLabel(state) : '准备／恢复语音' }}</button>
        </template>
      </div>
      <p v-if="state.storage_warning" class="text-xs state-warn" role="alert">存储异常：{{ state.storage_warning }}</p>
      <div v-if="waitingRequirements" class="text-xs text-ink-2 space-y-2" role="status">
        <p>现场会话要求：{{ waitingRequirements.language === 'en' ? '英文' : waitingRequirements.language }} · {{ requiredOutputLabel(waitingRequirements) }}。</p>
        <p v-if="waitingRequirements.playback && state.playback_device === 'null'" class="state-warn">当前使用无声模拟输出。</p>
        <button v-if="!draftMatchesRequirements" class="btn btn-outline btn-md" :disabled="busy" @click="matchOutput">匹配现场输出设置</button>
        <p v-else>输出设置已匹配。请选择音色，再点击“启用翻译播报”或“应用音色与输出”。</p>
      </div>
      <p class="text-xs text-ink-3">Speak translated text in a reference voice. Preview and listen before using a voice live.</p>

      <details class="text-xs border-b border-edge-soft pb-3">
        <summary class="font-bold text-ink cursor-pointer">Add a reference voice · 3–15 seconds WAV</summary>
        <div class="grid gap-3 mt-3">
          <label class="form-label">Name
            <input v-model="name" maxlength="80" class="field block w-full p-2 mt-1 text-xs font-normal" placeholder="Reference voice name">
          </label>
          <label class="form-label">Reference WAV
            <input type="file" accept=".wav,audio/wav" class="field-file block w-full mt-1 font-normal" @change="reference = ($event.target as HTMLInputElement).files?.[0] ?? null">
          </label>
          <label class="form-label">Reference language
            <select v-model="referenceLanguage" class="field ml-2 px-2 py-1.5 text-xs font-normal"><option value="zh">Chinese</option><option value="en">English</option></select>
          </label>
          <label class="form-label">Exact reference transcript
            <textarea v-model="transcript" maxlength="1000" rows="2" class="field block w-full p-2 mt-1 text-xs font-normal" :placeholder="state.engine === 'zipvoice' ? 'Required for ZipVoice' : 'Optional for PocketTTS'"></textarea>
          </label>
          <div>
            <button class="btn btn-accent btn-md" :disabled="busy || !reference" @click="upload">Save reference</button>
          </div>
        </div>
      </details>

      <div class="flex flex-wrap items-end gap-2">
        <label class="form-label flex-1 min-w-[140px]">Reference voice
          <select v-model="selectedVoice" class="field block w-full p-2 mt-1 text-xs font-normal"><option value="" disabled>Select a voice</option><option v-if="selectedVoice && !selectedVoiceAvailable" :value="selectedVoice" disabled>已选音色不可用，请重新选择</option><option v-for="voice in state.voices" :key="voice.id" :value="voice.id">{{ voice.name }} · {{ voice.status === 'needs_preview' ? 'needs preview' : 'preview available' }}</option></select>
        </label>
        <label class="form-label">Output
          <select v-model="language" class="field block p-2 mt-1 text-xs font-normal"><option v-for="lang in state.languages" :key="lang" :value="lang">{{ lang === 'en' ? 'English' : 'Chinese' }}</option></select>
        </label>
        <button class="btn btn-outline btn-md" :disabled="busy || !selectedVoice" @click="remove(selectedVoice)">Delete voice</button>
      </div>

      <label class="form-label">Preview text
        <textarea v-model="text" maxlength="500" rows="2" class="field block w-full p-2 mt-1 text-xs font-normal"></textarea>
      </label>
      <label class="text-xs text-ink-2 flex gap-2 items-center"><input v-model="playback" type="checkbox" class="accent-[var(--ink)]">{{ state.playback_device === 'null' ? 'Use mock speaker · silent, normal playback timing' : `Play on Jetson output (${state.playback_device})` }}</label>

      <div class="flex flex-wrap gap-2">
        <button class="btn btn-accent btn-md" :disabled="busy || !runtimeReady || !selectedVoiceAvailable || !text.trim()" @click="synthesize">Generate preview</button>
        <button class="btn btn-outline btn-md" :disabled="busy || !runtimeReady || !selectedVoiceAvailable" @click="session(!selectedSessionActive)">{{ selectedSessionActive ? '禁用翻译播报' : state.session.enabled ? '应用音色与输出' : '启用翻译播报' }}</button>
        <button class="btn btn-danger btn-md" :disabled="busy" @click="stop">Stop speech and clear queue</button>
      </div>

      <p class="text-tiny text-ink-3" role="status">{{ state.session.enabled ? `Translated speech enabled · ${sessionVoiceName} · ${state.session.playback ? (state.playback_device === 'null' ? 'Silent mock speaker' : 'Jetson playback') : 'WAV output'}` : 'Translated speech disabled' }} · {{ state.delivery_policy === 'coverage' ? `Coverage priority · ${state.pending_jobs ?? 0} waiting tasks · Delay can increase` : `${state.live_audio_budget_seconds ? `Live audio budget ${state.live_audio_budget_seconds}s` : `Up to ${state.queue_limit} unfinished jobs`} · Speech must start within 30 seconds` }}</p>

      <div v-for="job in state.jobs.slice(0, 6)" :key="job.id" class="border-t border-edge-soft pt-2 space-y-1 text-xs">
        <div class="flex justify-between gap-2">
          <span class="font-bold text-ink">{{ job.status === 'queued' && job.admission_pending ? 'Waiting for speech capacity' : job.status }}</span>
          <button v-if="['queued', 'processing', 'ready', 'playing'].includes(job.status)" class="link-muted underline" @click="cancel(job.id)">Cancel</button>
        </div>
        <p class="text-ink-2">{{ job.text }}</p>
        <p v-if="job.error" class="state-err">{{ job.error }}</p>
        <p v-if="job.status === 'playing' && (job.chunk_count ?? 0) > 1" class="text-ink-3">{{ job.played_chunks ?? 0 }} / {{ job.chunk_count }} parts played</p>
        <template v-if="job.status === 'completed'">
          <audio controls preload="none" class="w-full h-9" :src="job.result_url"></audio>
          <a :href="job.result_url" class="link-muted underline" download>Download WAV</a>
          <span class="ml-3 text-ink-3">{{ job.duration_seconds?.toFixed(1) }}s audio · {{ job.synthesis_seconds?.toFixed(1) }}s synthesis</span>
        </template>
      </div>
    </template>
    <p v-if="error" role="alert" class="text-xs state-err">{{ error }}</p>
  </section>
</template>
