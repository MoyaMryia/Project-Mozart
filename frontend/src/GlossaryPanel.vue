<script setup lang="ts">
// 术语表配置。保存后翻译服务在下一句生效，不需要重启。
import { onMounted, ref } from 'vue';

interface Entry { term: string; meaning: string }

const entries = ref<Entry[]>([]);
const term = ref('');
const meaning = ref('');
const busy = ref(false);
const error = ref('');
const status = ref('');

async function load() {
  error.value = '';
  try {
    const response = await fetch('/api/glossary');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error ?? `HTTP ${response.status}`);
    entries.value = Object.entries<string>(data.terms ?? {}).map(([term, meaning]) => ({ term, meaning }));
  } catch (failure) {
    error.value = `读取术语表失败：${(failure as Error).message}`;
  }
}

function add() {
  if (!term.value.trim() || !meaning.value.trim()) return;
  if (entries.value.some(entry => entry.term === term.value.trim())) {
    error.value = '该词条已存在';
    return;
  }
  entries.value.push({ term: term.value.trim(), meaning: meaning.value.trim() });
  term.value = '';
  meaning.value = '';
  status.value = '';
}

function remove(index: number) {
  entries.value.splice(index, 1);
  status.value = '';
}

async function save() {
  busy.value = true;
  error.value = '';
  status.value = '';
  try {
    const terms = Object.fromEntries(entries.value.map(entry => [entry.term, entry.meaning]));
    const response = await fetch('/api/glossary', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ terms })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error ?? `HTTP ${response.status}`);
    entries.value = Object.entries<string>(data.terms ?? {}).map(([term, meaning]) => ({ term, meaning }));
    status.value = `已保存 ${entries.value.length} 条`;
  } catch (failure) {
    error.value = `保存失败：${(failure as Error).message}`;
  } finally {
    busy.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section class="space-y-2" aria-label="术语表">
    <h2 class="text-xs font-extrabold tracking-wider text-ink uppercase section-title">术语表</h2>
    <p class="text-xs text-ink-3">源文出现这些词时写入翻译提示。例如 网赌 → online gambling。</p>

    <div v-for="(entry, index) in entries" :key="entry.term" class="flex gap-2 items-center text-xs">
      <span class="font-mono text-ink w-20 shrink-0">{{ entry.term }}</span>
      <input v-model="entry.meaning" class="field flex-1 min-w-0 px-2 py-1 text-xs" maxlength="60">
      <button class="link-muted underline shrink-0" :disabled="busy" @click="remove(index)">删除</button>
    </div>

    <div class="flex gap-2 items-center text-xs">
      <input v-model="term" class="field w-20 shrink-0 px-2 py-1 text-xs" maxlength="8" placeholder="词条">
      <input v-model="meaning" class="field flex-1 min-w-0 px-2 py-1 text-xs" maxlength="60" placeholder="English meaning">
      <button class="btn btn-outline btn-md shrink-0" :disabled="busy || !term.trim() || !meaning.trim()" @click="add">添加</button>
    </div>

    <div class="flex gap-2 items-center">
      <button class="btn btn-accent btn-md" :disabled="busy" @click="save">保存术语表</button>
      <span v-if="status" class="text-tiny state-ok" role="status">{{ status }}</span>
    </div>
    <p v-if="error" class="text-xs state-err" role="alert">{{ error }}</p>
  </section>
</template>
