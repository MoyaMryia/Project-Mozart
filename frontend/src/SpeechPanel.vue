<script setup lang="ts">
// SpeechPanel.vue — Zero-shot 参考音色与翻译播报面板。
// 视觉与 App.vue 卡片体系一致（设计令牌见 styles.css）；逻辑未动。
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { api } from './api';
interface Voice { id: string; name: string; status: string; duration_seconds: number; identity_approved: boolean }
interface SpeechJob { id: string; text: string; status: string; result_url: string; error?: string; duration_seconds?: number; synthesis_seconds?: number; chunk_count?: number; played_chunks?: number; admission_pending?: boolean }
interface SpeechStatus { engine: string; voices: Voice[]; jobs: SpeechJob[]; languages: string[]; queue_limit: number; delivery_policy?: string; pending_jobs?: number; live_audio_budget_seconds?: number; playback_device: string; session: { enabled: boolean; voice_id: string; language: string; playback: boolean } }
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
let initialized = false;
const selectedSessionActive = computed(() => !!state.value?.session.enabled && state.value.session.voice_id === selectedVoice.value && state.value.session.language === language.value && state.value.session.playback === playback.value);
const sessionVoiceName = computed(() => state.value?.voices.find(v => v.id === state.value?.session.voice_id)?.name ?? '');
const jsonPost = (body: unknown) => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
async function refresh() {
  try {
    state.value = await api<SpeechStatus>('/api/speech/status');
    unavailable.value = '';
    if (!initialized) {
      selectedVoice.value = state.value.session.voice_id || state.value.voices[0]?.id || '';
      language.value = state.value.session.language; playback.value = state.value.session.playback; initialized = true;
    }
  } catch (e) { state.value = null; unavailable.value = e instanceof Error ? e.message : 'Speech service unavailable'; }
}
async function action(fn: () => Promise<unknown>) {
  busy.value = true; error.value = '';
  try { await fn(); await refresh(); }
  catch (e) { error.value = e instanceof Error ? e.message : 'Speech request failed'; }
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
    const voice = await api<Voice>('/api/voices', jsonPost({ name: name.value || file.name, audio_base64, transcript: transcript.value, language: referenceLanguage.value }));
    selectedVoice.value = voice.id;
  });
}
const synthesize = () => action(() => api('/api/speech/jobs', jsonPost({ text: text.value, language: language.value, voice_id: selectedVoice.value, playback: playback.value })));
const session = (enabled: boolean) => action(() => api('/api/speech/session', jsonPost({ enabled, voice_id: selectedVoice.value, language: language.value, playback: playback.value })));
const stop = () => action(() => api('/api/speech/stop', jsonPost({})));
const cancel = (id: string) => action(() => api(`/api/speech/jobs/${id}`, { method: 'DELETE' }));
const remove = (id: string) => action(async () => { await api(`/api/voices/${id}`, { method: 'DELETE' }); if (selectedVoice.value === id) selectedVoice.value = ''; });
onMounted(() => { void refresh(); timer = window.setInterval(() => void refresh(), 1500); });
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <section class="card p-4 mb-4 space-y-3" aria-label="Reference voice and translated speech">
    <div class="flex items-center justify-between gap-3">
      <h2 class="text-xs font-extrabold tracking-wider text-ink uppercase section-title">Zero-shot · Reference speech</h2>
      <span class="text-tiny font-mono font-bold" :class="state ? 'state-ok' : 'state-unknown'">{{ state ? state.engine.toUpperCase() : 'OFFLINE' }}</span>
    </div>
    <p v-if="unavailable" class="text-xs text-ink-2" role="status">{{ unavailable }}</p>
    <template v-if="state">
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
          <select v-model="selectedVoice" class="field block w-full p-2 mt-1 text-xs font-normal"><option value="" disabled>Select a voice</option><option v-for="voice in state.voices" :key="voice.id" :value="voice.id">{{ voice.name }} · {{ voice.status === 'needs_preview' ? 'needs preview' : 'preview available' }}</option></select>
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
        <button class="btn btn-accent btn-md" :disabled="busy || !selectedVoice || !text.trim()" @click="synthesize">Generate preview</button>
        <button class="btn btn-outline btn-md" :disabled="busy || !selectedVoice" @click="session(!selectedSessionActive)">{{ selectedSessionActive ? 'Disable translated speech' : state.session.enabled ? 'Apply selected voice and playback' : 'Enable translated speech' }}</button>
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
