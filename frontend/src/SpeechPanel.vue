<script setup lang="ts">
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
  <section class="border border-gray-200 rounded-lg p-4 mb-4 space-y-3" aria-label="Reference voice and translated speech">
    <div class="flex items-center justify-between gap-3">
      <h2 class="text-xs font-extrabold tracking-wider uppercase">Zero-shot · Reference speech</h2>
      <span class="text-xs font-mono" :class="state ? 'text-emerald-700' : 'text-gray-500'">{{ state ? state.engine.toUpperCase() : 'OFFLINE' }}</span>
    </div>
    <p v-if="unavailable" class="text-xs text-gray-600" role="status">{{ unavailable }}</p>
    <template v-if="state">
      <p class="text-xs text-gray-500">Speak translated text in a reference voice. Preview and listen before using a voice live.</p>
      <details class="text-xs border-b border-gray-100 pb-3">
        <summary class="font-bold cursor-pointer">Add a reference voice · 3–15 seconds WAV</summary>
        <div class="grid gap-2 mt-3">
          <label>Name <input v-model="name" maxlength="80" class="block border rounded w-full p-2 mt-1" placeholder="Reference voice name"></label>
          <label>Reference WAV <input type="file" accept=".wav,audio/wav" class="block mt-1" @change="reference = ($event.target as HTMLInputElement).files?.[0] ?? null"></label>
          <label>Reference language <select v-model="referenceLanguage" class="border rounded p-1 ml-2"><option value="zh">Chinese</option><option value="en">English</option></select></label>
          <label>Exact reference transcript <textarea v-model="transcript" maxlength="1000" class="block border rounded w-full p-2 mt-1" :placeholder="state.engine === 'zipvoice' ? 'Required for ZipVoice' : 'Optional for PocketTTS'"></textarea></label>
          <button class="bg-black text-white rounded px-3 py-2 disabled:opacity-40" :disabled="busy || !reference" @click="upload">Save reference</button>
        </div>
      </details>
      <div class="flex flex-wrap items-end gap-2">
        <label class="text-xs flex-1 min-w-[140px]">Reference voice
          <select v-model="selectedVoice" class="block w-full border rounded p-2 mt-1"><option value="" disabled>Select a voice</option><option v-for="voice in state.voices" :key="voice.id" :value="voice.id">{{ voice.name }} · {{ voice.status === 'needs_preview' ? 'needs preview' : 'preview available' }}</option></select>
        </label>
        <label class="text-xs">Output <select v-model="language" class="block border rounded p-2 mt-1"><option v-for="lang in state.languages" :key="lang" :value="lang">{{ lang === 'en' ? 'English' : 'Chinese' }}</option></select></label>
        <button class="text-xs border rounded p-2 disabled:opacity-40" :disabled="busy || !selectedVoice" @click="remove(selectedVoice)">Delete voice</button>
      </div>
      <label class="text-xs block">Preview text <textarea v-model="text" maxlength="500" class="block w-full border rounded p-2 mt-1" rows="2"></textarea></label>
      <label class="text-xs flex gap-2 items-center"><input v-model="playback" type="checkbox">{{ state.playback_device === 'null' ? 'Use mock speaker · silent, normal playback timing' : `Play on Jetson output (${state.playback_device})` }}</label>
      <div class="flex flex-wrap gap-2">
        <button class="bg-black text-white rounded px-3 py-2 text-xs disabled:opacity-40" :disabled="busy || !selectedVoice || !text.trim()" @click="synthesize">Generate preview</button>
        <button class="border rounded px-3 py-2 text-xs disabled:opacity-40" :disabled="busy || !selectedVoice" @click="session(!selectedSessionActive)">{{ selectedSessionActive ? 'Disable translated speech' : state.session.enabled ? 'Apply selected voice and playback' : 'Enable translated speech' }}</button>
        <button class="border border-red-200 text-red-700 rounded px-3 py-2 text-xs disabled:opacity-40" :disabled="busy" @click="stop">Stop speech and clear queue</button>
      </div>
      <p class="text-xs text-gray-500" role="status">{{ state.session.enabled ? `Translated speech enabled · ${sessionVoiceName} · ${state.session.playback ? (state.playback_device === 'null' ? 'Silent mock speaker' : 'Jetson playback') : 'WAV output'}` : 'Translated speech disabled' }} · {{ state.delivery_policy === 'coverage' ? `Coverage priority · ${state.pending_jobs ?? 0} waiting tasks · Delay can increase` : `${state.live_audio_budget_seconds ? `Live audio budget ${state.live_audio_budget_seconds}s` : `Up to ${state.queue_limit} unfinished jobs`} · Speech must start within 30 seconds` }}</p>
      <div v-for="job in state.jobs.slice(0, 6)" :key="job.id" class="border-t border-gray-100 pt-2 space-y-1 text-xs">
        <div class="flex justify-between gap-2"><span class="font-bold">{{ job.status === 'queued' && job.admission_pending ? 'Waiting for speech capacity' : job.status }}</span><button v-if="['queued', 'processing', 'ready', 'playing'].includes(job.status)" class="underline" @click="cancel(job.id)">Cancel</button></div>
        <p>{{ job.text }}</p><p v-if="job.error" class="text-red-700">{{ job.error }}</p>
        <p v-if="job.status === 'playing' && (job.chunk_count ?? 0) > 1" class="text-gray-500">{{ job.played_chunks ?? 0 }} / {{ job.chunk_count }} parts played</p>
        <template v-if="job.status === 'completed'"><audio controls preload="none" class="w-full h-9" :src="job.result_url"></audio><a :href="job.result_url" class="underline" download>Download WAV</a><span class="ml-3 text-gray-500">{{ job.duration_seconds?.toFixed(1) }}s audio · {{ job.synthesis_seconds?.toFixed(1) }}s synthesis</span></template>
      </div>
    </template>
    <p v-if="error" role="alert" class="text-xs text-red-700">{{ error }}</p>
  </section>
</template>
