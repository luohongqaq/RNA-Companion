<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, type Conversation, type Message, type Page } from './companionApi'

const props = defineProps<{ conversation: Conversation; characterName: string; portrait?: string }>()
defineEmits<{ back: []; resume: [] }>()

const messages = ref<Message[]>([])
const cursor = ref<string | null>(null)
const loading = ref(true)
const error = ref('')
const heading = ref<HTMLElement>()
const dateFormat = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'full' })
const timeFormat = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
const days = computed(() => {
  const groups: { date: string; messages: Message[] }[] = []
  for (const message of messages.value) {
    const date = dateFormat.format(new Date(message.created_at))
    const last = groups.at(-1)
    if (last?.date === date) last.messages.push(message)
    else groups.push({ date, messages: [message] })
  }
  return groups
})

async function load(before: string | null = null) {
  loading.value = true
  error.value = ''
  const path = '/conversations/' + encodeURIComponent(props.conversation.conversation_id) + '/messages'
  const query = before ? '?' + new URLSearchParams({ cursor: before }) : ''
  try {
    const result = await api<Page<Message>>(path + query)
    messages.value = before ? [...result.items, ...messages.value] : result.items
    cursor.value = result.next_cursor
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  heading.value?.focus({ preventScroll: true })
  void load()
})
</script>

<template>
  <article class="journal-scroll transcript-view" aria-labelledby="transcript-title">
    <header class="history-heading">
      <div>
        <span class="journal-kicker">{{ characterName }}的对话</span>
        <h1 id="transcript-title" ref="heading" tabindex="-1">{{ conversation.title || '未命名对话' }}</h1>
      </div>
      <button class="journal-stamp" @click="$emit('back')">← 返回聊天记录</button>
    </header>
    <div class="journal-tools">
      <span v-if="conversation.state === 'archived'" class="journal-note">已归档</span>
      <button v-else @click="$emit('resume')">继续聊天 →</button>
      <button v-if="cursor" :disabled="loading" @click="load(cursor)">加载更早的消息</button>
    </div>
    <div class="transcript-body" :aria-busy="loading">
      <p v-if="error" class="journal-note" role="alert">{{ error }} <button v-if="!messages.length" @click="load()">重试</button></p>
      <p v-else-if="loading && !messages.length" class="journal-note" role="status">正在加载…</p>
      <p v-else-if="!messages.length" class="journal-note">这段对话还没有消息。</p>
      <section v-for="day in days" :key="day.messages[0].message_id" class="transcript-day">
        <h2>{{ day.date }}</h2>
        <article v-for="message in day.messages" :key="message.message_id" class="transcript-message" :class="{ 'from-character': message.role === 'character' }">
          <header>
            <span v-if="message.role === 'character'" class="archive-card-icon" aria-hidden="true">
              <img v-if="portrait" :src="portrait" alt="" @error="($event.currentTarget as HTMLImageElement).hidden = true">
              <svg v-else viewBox="0 0 24 24"><use href="#companion-i-spark"/></svg>
            </span>
            <strong>{{ message.role === 'user' ? '你' : characterName }}</strong>
            <time :datetime="message.created_at">{{ timeFormat.format(new Date(message.created_at)) }}</time>
          </header>
          <p v-if="message.japanese" lang="ja" class="transcript-original">{{ message.japanese }}</p>
          <p>{{ message.content }}</p>
        </article>
      </section>
    </div>
  </article>
</template>
