<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { api, type Page } from './companionApi'
import JournalArtwork from './JournalArtwork.vue'
const props = defineProps<{ characterId: string; portrait?: string; topicRequest?: { id: string; count: number } }>()
const newKey = () => crypto.randomUUID()
const sections = [
  ['memorable_moments', '难忘时刻'], ['profile_facts', '关于你的小事'], ['intentions', '愿望与计划'],
  ['commitments', '约定'], ['reflections', '角色想法'],
] as const
type Daily = { title: string; memory_date: string } & Record<typeof sections[number][0], { text: string; target_date?: string }[]>
type Topic = { topic: string; topic_identity: { description: string }; current_state: { text: string }; entries: { entry_id: string; statement: string; acquire_time: string }[] }
interface Summary { document_id: string; title: string; memory_date?: string; updated_at: string }
interface Detail { document_id: string; revision: number; document: Daily | Topic }
interface Edit { edit_id: string; revision: number; index_status: string }
const kind = ref<'daily' | 'topic'>(props.topicRequest ? 'topic' : 'daily')
const list = ref<Summary[]>([])
const cursor = ref<string | null>(null)
const selected = ref<Detail>()
const paperRef = ref<HTMLElement>()
const search = ref('')
const loading = ref(false)
const editing = ref(false)
const saving = ref(false)
const status = ref('')
const editKey = ref('')
const visible = computed(() => list.value.filter(item => item.title.toLowerCase().includes(search.value.toLowerCase().trim())))
const daily = computed(() => selected.value && 'memory_date' in selected.value.document ? selected.value.document : undefined)
const topic = computed(() => selected.value && 'topic' in selected.value.document ? selected.value.document : undefined)
const pageNumber = ref(1)
const dailyPages = computed(() => sections.filter(([key]) => daily.value?.[key].length))
const dailySection = computed(() => dailyPages.value[pageNumber.value - 1])
const timelinePageSize = 4
const topicEntries = computed(() => {
  const start = (pageNumber.value - 2) * timelinePageSize
  return topic.value?.entries.slice(start, start + timelinePageSize) ?? []
})
const pageCount = computed(() => topic.value
  ? 1 + Math.ceil(topic.value.entries.length / timelinePageSize)
  : Math.max(1, dailyPages.value.length))
async function focusBody() {
  await nextTick()
  const field = paperRef.value?.querySelector<HTMLTextAreaElement>('.memory-copy textarea')
    ?? paperRef.value?.querySelector<HTMLInputElement>('.memory-title-input')
  field?.focus()
  field?.setSelectionRange(0, 0)
}
function startEditing() {
  editing.value = true
  editKey.value = newKey()
  void focusBody()
}
function turnPage(direction: number) {
  pageNumber.value = Math.min(pageCount.value, Math.max(1, pageNumber.value + direction))
  if (paperRef.value) paperRef.value.scrollTop = 0
  if (editing.value) void focusBody()
}
const query = () => new URLSearchParams({ character_id: props.characterId })
const path = () => '/memories/' + kind.value
let sequence = 0
async function load(more = false) {
  const request = ++sequence
  loading.value = true
  status.value = ''
  const params = query()
  if (more && cursor.value) params.set('cursor', cursor.value)
  try {
    const result = await api<Page<Summary>>(path() + '?' + params)
    if (request !== sequence) return
    list.value = more ? [...list.value, ...result.items] : result.items
    cursor.value = result.next_cursor
    const target = kind.value === 'topic' ? props.topicRequest?.id : undefined
    const first = list.value[0]
    if (!selected.value && (target || first)) await open(target ?? first.document_id)
  } catch (cause) { if (request === sequence) status.value = String(cause) }
  finally { if (request === sequence) loading.value = false }
}
async function open(id: string) {
  const request = ++sequence
  status.value = ''
  loading.value = true
  try {
    const result = await api<Detail>(path() + '/' + encodeURIComponent(id) + '?' + query())
    if (request === sequence) {
      selected.value = result
      pageNumber.value = 1
      editing.value = false
      nextTick(() => { if (paperRef.value) paperRef.value.scrollTop = 0 })
    }
  } catch (cause) { if (request === sequence) status.value = String(cause) }
  finally { if (request === sequence) loading.value = false }
}
watch(kind, () => {
  selected.value = undefined
  list.value = []
  cursor.value = null
  void load()
}, { immediate: true })
watch(() => props.topicRequest, request => {
  if (!request || editing.value) return
  if (kind.value !== 'topic') kind.value = 'topic'
  else void open(request.id)
})
async function save() {
  if (!selected.value) return
  const current = selected.value
  saving.value = true
  status.value = ''
  try {
    const result = await api<Edit>(path() + '/' + encodeURIComponent(current.document_id), 'PUT', {
      character_id: props.characterId, expected_revision: current.revision,
      idempotency_key: editKey.value, document: current.document,
    })
    current.revision = result.revision
    const item = list.value.find(entry => entry.document_id === current.document_id)
    if (item) item.title = 'title' in current.document ? current.document.title : current.document.topic
    editing.value = false
    status.value = '正文已保存'
  } catch (cause) { status.value = String(cause) }
  finally { saving.value = false }
}
</script>
<template>
  <section class="companion-panel-frame journal-page memory-panel" aria-label="记忆手记">
    <div class="memory-book">
      <aside class="memory-index" aria-label="记忆目录">
        <span class="journal-kicker">记忆目录</span>
        <h1>记忆手记</h1>
        <div class="journal-tools"><button v-for="tab in (['daily', 'topic'] as const)" :key="tab" :disabled="editing" :aria-pressed="kind === tab" @click="kind = tab">{{ tab === 'daily' ? '日记' : '主题' }}</button></div>
        <input v-model="search" aria-label="筛选已加载的回忆" placeholder="筛选已加载的回忆…">
        <button v-for="item in visible" :key="item.document_id" :disabled="editing" :title="item.title" :aria-current="selected?.document_id === item.document_id ? 'page' : undefined" class="memory-item" @click="open(item.document_id)">
          <span class="archive-card-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><use :href="kind === 'daily' ? '#companion-i-memory' : '#companion-i-spark'"/></svg>
          </span>
          <span class="archive-card-copy">
            <strong>{{ item.title }}</strong><time>{{ item.memory_date ?? item.updated_at.slice(0, 10) }}</time>
          </span>
          <span class="archive-card-arrow" aria-hidden="true">›</span>
        </button>
        <p v-if="search && !visible.length" class="journal-note">没有匹配的回忆。</p>
        <button v-if="cursor" class="journal-more" :disabled="loading || editing" @click="load(true)">翻阅更多 ↓</button>
      </aside>
      <article class="memory-document">
        <header class="memory-toolbar">
                    <span class="journal-note memory-status" role="status">{{ status || (loading ? '正在翻页…' : '') }}</span>
          <button v-if="selected && !editing" class="journal-stamp" :disabled="loading" @click="startEditing">✎ 修订这篇</button>
          <button v-if="selected && editing" :disabled="saving || loading" @click="open(selected.document_id)">取消</button>
          <button v-if="editing" class="journal-stamp" :disabled="saving || loading" @click="save">保存正文</button>
        </header>
        <div v-if="daily" ref="paperRef" class="journal-scroll" @input="editKey = newKey()">
          <header class="journal-heading">
            <div><h2>{{ daily.memory_date }}</h2></div>
            <JournalArtwork :src="portrait" />
          </header>
          <div class="memory-copy">
            <section v-if="dailySection" :key="dailySection[0]">
              <h3>{{ dailySection[1] }}</h3>
              <div v-for="(entry, position) in daily[dailySection[0]]" :key="position" class="memory-entry">
                <textarea v-if="editing" v-model="entry.text" :aria-label="dailySection[1] + (position + 1)" rows="1" :disabled="saving || loading" />
                <p v-else>{{ entry.text }} <time v-if="entry.target_date" class="journal-note">{{ entry.target_date }}</time></p>
              </div>
            </section>
            <p v-else class="journal-note">这篇日记还没有正文。</p>
          </div>
        </div>
        <div v-else-if="topic" ref="paperRef" class="journal-scroll" @input="editKey = newKey()">
          <header class="journal-heading">
            <div><span class="journal-kicker">主题记忆</span>
              <input v-if="editing" v-model="topic.topic" class="memory-title-input" aria-label="主题标题" :disabled="saving || loading"><h2 v-else>{{ topic.topic }}</h2>
            </div>
            <JournalArtwork :src="portrait" />
          </header>
          <div class="memory-copy">
            <template v-if="pageNumber === 1">
              <section>
                <h3>主题说明</h3>
                <textarea v-if="editing" v-model="topic.topic_identity.description" aria-label="主题说明" rows="1" :disabled="saving || loading" /><p v-else>{{ topic.topic_identity.description }}</p>
              </section>
              <section>
                <h3>现在进行到哪里</h3>
                <textarea v-if="editing" v-model="topic.current_state.text" aria-label="当前状态" rows="1" :disabled="saving || loading" /><p v-else>{{ topic.current_state.text }}</p>
              </section>
            </template>
            <section v-else><h3>时间线</h3>
              <div v-for="entry in topicEntries" :key="entry.entry_id" class="memory-entry">
                <time class="journal-note memory-time">{{ entry.acquire_time }}</time>
                <textarea v-if="editing" v-model="entry.statement" aria-label="时间线内容" rows="1" :disabled="saving || loading" /><p v-else>{{ entry.statement }}</p>
              </div>
            </section>
          </div>
        </div>
        <div v-else class="memory-copy"><p v-if="!loading && !status" class="journal-note">这里还没有可翻阅的回忆。</p></div>
        <nav v-if="selected && pageCount > 1" class="memory-pagination" aria-label="手记翻页">
          <button :disabled="pageNumber === 1 || loading || saving" @click="turnPage(-1)">← 上一页</button>
          <span aria-live="polite">第 {{ pageNumber }} / {{ pageCount }} 页</span>
          <button :disabled="pageNumber === pageCount || loading || saving" @click="turnPage(1)">下一页 →</button>
        </nav>
      </article>
    </div>
  </section>
</template>

