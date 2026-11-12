<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import TitleBar from '../components/TitleBar.vue'
import CompanionMemory from './CompanionMemory.vue'
import CompanionSettings from './CompanionSettings.vue'
import CompanionCharacter from './CompanionCharacter.vue'
import CompanionTranscript from './CompanionTranscript.vue'
import { api, assetUrl, endpoint, type Assets, type Character, type Conversation, type Job, type Message, type Page } from './companionApi'
import { broadcast, WINDOW_ID } from '../shared/broadcast'
import { analyzeSpriteBox, FALLBACK_BOX, getCachedSpriteMetrics, prefetchSpriteMetrics, type SpriteBox } from './spriteBox'
import './companion.css'
import './journal.css'

const tabs = [
  { id: 'chat', label: '对话', icon: 'home' },
  { id: 'memory', label: '记忆', icon: 'memory' },
  { id: 'history', label: '聊天记录', icon: 'calendar' },
  { id: 'character', label: '角色形象', icon: 'person' },
  { id: 'settings', label: '设置', icon: 'settings' },
] as const
type Screen = typeof tabs[number]['id']
const page = ref<Screen>('chat')
const visited = reactive(new Set<Screen>(['chat']))
const sidebarCollapsed = ref(false)
const immersiveMode = ref(false)
function toggleImmersive() {
  immersiveMode.value = !immersiveMode.value
}
const characters = ref<Character[]>([])
const character = ref<Character>({ character_id: '', name: '' })
const characterAssets = ref<Record<string, Assets>>({})
const assets = computed(() => characterAssets.value[character.value.character_id])
const online = ref(false)
const starting = ref(true)
const startupProgress = ref(8)
const startupStatus = ref('正在准备启动…')
const startupPetals: Record<string, string>[] = [
  { '--x': '6%', '--s': '19px', '--t': '14s', '--d': '-8s' },
  { '--x': '18%', '--s': '12px', '--t': '17s', '--d': '-3s' },
  { '--x': '29%', '--s': '16px', '--t': '15s', '--d': '-12s' },
  { '--x': '42%', '--s': '11px', '--t': '19s', '--d': '-7s' },
  { '--x': '59%', '--s': '15px', '--t': '16s', '--d': '-14s' },
  { '--x': '71%', '--s': '21px', '--t': '13s', '--d': '-5s' },
  { '--x': '83%', '--s': '13px', '--t': '18s', '--d': '-10s' },
  { '--x': '93%', '--s': '18px', '--t': '15s', '--d': '-2s' },
]
const error = ref('')
const notice = ref('')
const sealing = ref('')
const archiving = ref('')
const list = ref<Conversation[]>([])
const listCursor = ref<string | null>(null)
const filter = ref<Conversation['state']>('active')
const search = ref('')
const selected = ref<Conversation>()
const reading = ref<Conversation>()
const messages = ref<Message[]>([])
const messageCursor = ref<string | null>(null)
const jobs = ref<Job[]>([])
const draft = ref('')
const backlog = ref(false)
const creating = ref(false)
const backgroundIndex = ref(0)
const renaming = ref('')
const newTitle = ref('')
type Pending = { id: string; content: string; failed: boolean }
const queues = reactive<Record<string, Pending[]>>({})
const draining = reactive(new Set<string>())
const conversationPath = (id: string) => '/conversations/' + encodeURIComponent(id)
const pending = computed(() => selected.value ? queues[selected.value.conversation_id] ?? [] : [])
/** 列表里的状态行：待发送 > 正在回复 > 当前对话标题。 */
const replyState = computed(() => {
  if (pending.value.length) return '发送中…'
  if (jobs.value.some(job => ['need_prepare', 'need_generate', 'need_accept'].includes(job.status))) return '正在回复…'
  return ''
})
const pendingFailed = computed(() => pending.value.some(item => item.failed))
const jobIssues = computed(() => jobs.value.filter(job => job.error || job.status === 'cancelled'))
const latest = computed(() => [...messages.value].reverse().find(message => message.role === 'character'))
const backgrounds = computed(() => {
  const entries = assets.value?.backgrounds ?? []
  const defaultId = assets.value?.default_background_id
  return [...entries.filter(entry => entry.id === defaultId), ...entries.filter(entry => entry.id !== defaultId)]
})
const background = computed(() => backgrounds.value[backgroundIndex.value % Math.max(1, backgrounds.value.length)])
function cycleBackground() {
  if (backgrounds.value.length > 1) backgroundIndex.value = (backgroundIndex.value + 1) % backgrounds.value.length
}
const sprite = computed(() => {
  const visual = latest.value?.visual
  const defaults = assets.value?.default_expression
  const entries = assets.value?.assets ?? []
  return entries.find(entry => entry.outfit_id === defaults?.outfit_id && entry.pose_id === visual?.pose_name.pose_id && entry.expression_id === visual?.expression_name.expression_id)
    ?? entries.find(entry => entry.outfit_id === defaults?.outfit_id && entry.pose_id === defaults?.pose_id && entry.expression_id === defaults?.expression_id)
})
interface SceneLighting { lightX: number; lightColor: string; ambientColor: string; brightness: number; intensity: number; shadowX: number }
const neutralLighting = (): SceneLighting => ({ lightX: 36, lightColor: '255, 241, 218', ambientColor: '150, 174, 205', brightness: 1, intensity: .2, shadowX: 9 })
const sceneLighting = ref<SceneLighting>(neutralLighting())
const spriteRatio = ref(2 / 3)
function updateSpriteRatio(event: Event) {
  const image = event.target as HTMLImageElement
  if (image.naturalWidth && image.naturalHeight) spriteRatio.value = image.naturalWidth / image.naturalHeight
}
/** 立绘按"人物可见框"归一摆位，锚点与设计稿一致（桌面/窄屏两套）。 */
const spriteBox = ref<SpriteBox>(FALLBACK_BOX)
const narrowQuery = matchMedia('(max-width:760px)')
const narrowScene = ref(narrowQuery.matches)
const onNarrowChange = (event: MediaQueryListEvent) => { narrowScene.value = event.matches }
let spriteBoxRequest = 0
watch(() => sprite.value?.url, async path => {
  const request = ++spriteBoxRequest
  if (!path) { spriteBox.value = FALLBACK_BOX; return }
  const url = assetUrl(path)
  const cached = getCachedSpriteMetrics(url)
  if (cached) {
    spriteBox.value = cached.box
    spriteRatio.value = cached.ratio
    return
  }
  spriteBox.value = FALLBACK_BOX
  try {
    const box = await analyzeSpriteBox(url)
    if (request !== spriteBoxRequest) return
    spriteBox.value = box
    const metrics = getCachedSpriteMetrics(url)
    if (metrics) spriteRatio.value = metrics.ratio
  } catch { if (request === spriteBoxRequest) spriteBox.value = FALLBACK_BOX }
}, { immediate: true })
const lightingCache = new Map<string, SceneLighting>()
let lightingRequest = 0
const sceneStyle = computed<Record<string, string>>(() => {
  const anchor = narrowScene.value ? { vh: 123.1, vt: 19.4 } : { vh: 186.7, vt: 1.9 }
  const fill = Math.max(.05, spriteBox.value.bottom - spriteBox.value.top)
  const height = anchor.vh / fill
  return {
    '--light-x': sceneLighting.value.lightX + '%',
    '--light-color': sceneLighting.value.lightColor,
    '--ambient-color': sceneLighting.value.ambientColor,
    '--sprite-brightness': String(sceneLighting.value.brightness),
    '--light-intensity': String(sceneLighting.value.intensity),
    '--shadow-x': sceneLighting.value.shadowX + 'px',
    '--sprite-mask': sprite.value ? `url("${assetUrl(sprite.value.url)}")` : 'none',
    '--sprite-ratio': String(spriteRatio.value),
    '--sprite-h': height.toFixed(2) + '%',
    '--sprite-top': (anchor.vt - spriteBox.value.top * height).toFixed(2) + '%',
    '--sprite-cx': String(spriteBox.value.centerX),
  }
})
const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))
async function analyzeLighting(url: string): Promise<SceneLighting> {
  const image = new Image()
  image.crossOrigin = 'anonymous'
  image.src = url
  await image.decode()
  const canvas = document.createElement('canvas')
  canvas.width = 64
  canvas.height = 36
  const context = canvas.getContext('2d', { willReadFrequently: true })
  if (!context) throw new Error('无法分析背景光照')
  context.drawImage(image, 0, 0, 64, 36)
  const pixels = context.getImageData(0, 0, 64, 36).data
  let count = 0, luminance = 0, left = 0, right = 0, leftCount = 0, rightCount = 0
  let red = 0, green = 0, blue = 0, lightRed = 0, lightGreen = 0, lightBlue = 0, lightWeight = 0
  for (let index = 0; index < pixels.length; index += 4) {
    const pixel = index / 4
    const x = pixel % 64
    const r = pixels[index], g = pixels[index + 1], b = pixels[index + 2]
    const current = (.2126 * r + .7152 * g + .0722 * b) / 255
    luminance += current
    red += r; green += g; blue += b; count++
    if (x < 32) { left += current; leftCount++ } else { right += current; rightCount++ }
    if (current > .56) {
      const weight = current - .48
      lightRed += r * weight; lightGreen += g * weight; lightBlue += b * weight; lightWeight += weight
    }
  }
  const average = luminance / count
  const leftAverage = left / leftCount
  const rightAverage = right / rightCount
  const sourceRed = lightWeight ? lightRed / lightWeight : red / count
  const sourceGreen = lightWeight ? lightGreen / lightWeight : green / count
  const sourceBlue = lightWeight ? lightBlue / lightWeight : blue / count
  const soften = (value: number) => Math.round(value * .78 + 255 * .22)
  const ambient = (value: number) => Math.round(value * .72 + 102 * .28)
  return {
    lightX: leftAverage > rightAverage * 1.035 ? 28 : rightAverage > leftAverage * 1.035 ? 72 : 50,
    lightColor: [soften(sourceRed), soften(sourceGreen), soften(sourceBlue)].join(', '),
    ambientColor: [ambient(red / count), ambient(green / count), ambient(blue / count)].join(', '),
    brightness: clamp(.96 + (average - .5) * .18, .9, 1.06),
    intensity: clamp(.15 + Math.abs(leftAverage - rightAverage) * .42, .15, .28),
    shadowX: leftAverage >= rightAverage ? 10 : -10,
  }
}
watch(() => background.value?.url, async path => {
  const request = ++lightingRequest
  if (!path) { sceneLighting.value = neutralLighting(); return }
  const url = assetUrl(path)
  const cached = lightingCache.get(url)
  if (cached) { sceneLighting.value = cached; return }
  sceneLighting.value = neutralLighting()
  try {
    const result = await analyzeLighting(url)
    lightingCache.set(url, result)
    if (request === lightingRequest) sceneLighting.value = result
  } catch { /* 图片跨域或解码失败时保留中性光照。 */ }
}, { immediate: true })
const visible = computed(() => list.value.filter(item => (item.title || '未命名对话').toLowerCase().includes(search.value.trim().toLowerCase())))
function avatarFor(characterId: string) {
  const current = characterAssets.value[characterId]
  const defaults = current?.default_expression
  return current?.assets.find(entry => entry.outfit_id === defaults?.outfit_id && entry.pose_id === defaults?.pose_id && entry.expression_id === defaults?.expression_id)
}
const portrait = computed(() => {
  const sprite = avatarFor(character.value.character_id)
  return sprite ? assetUrl(sprite.url) : undefined
})
const listTimeFormat = new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
const listTime = (value: string) => listTimeFormat.format(new Date(value)).replace(' ', ' · ')
const report = (cause: unknown) => { error.value = cause instanceof Error ? cause.message : String(cause) }
async function ensureAssets(item: Character) {
  if (characterAssets.value[item.character_id]) return
  const result = await api<Assets>('/characters/' + encodeURIComponent(item.character_id) + '/assets')
  characterAssets.value = { ...characterAssets.value, [item.character_id]: result }
}
let listRequest = 0
async function loadList(more = false) {
  const request = ++listRequest
  const query = new URLSearchParams({ character_id: character.value.character_id, state: filter.value })
  if (more && listCursor.value) query.set('cursor', listCursor.value)
  const result = await api<Page<Conversation>>('/conversations?' + query)
  if (request !== listRequest) return
  list.value = more ? [...list.value, ...result.items] : result.items
  listCursor.value = result.next_cursor
}
let refreshRequest = 0
async function refresh(id: string) {
  const request = ++refreshRequest
  const [history, status] = await Promise.all([
    api<Page<Message>>(conversationPath(id) + '/messages'),
    api<{ items: Job[] }>(conversationPath(id) + '/generation-jobs'),
  ])
  if (selected.value?.conversation_id !== id || refreshRequest !== request) return
  if (messages.value.length) {
    const first = history.items[0]?.message_id
    const overlap = messages.value.findIndex(message => message.message_id === first)
    if (overlap >= 0) messages.value = [...messages.value.slice(0, overlap), ...history.items]
    else { messages.value = history.items; messageCursor.value = history.next_cursor }
  } else {
    messages.value = history.items
    messageCursor.value = history.next_cursor
  }
  jobs.value = status.items
}
async function older() {
  const id = selected.value?.conversation_id
  const cursor = messageCursor.value
  if (!id || !cursor) return
  const result = await api<Page<Message>>(conversationPath(id) + '/messages?cursor=' + encodeURIComponent(cursor))
  if (selected.value?.conversation_id !== id || messageCursor.value !== cursor) return
  messages.value = [...result.items, ...messages.value]
  messageCursor.value = result.next_cursor
}
function open(item: Conversation, notify = true) {
  reading.value = undefined
  if (selected.value?.conversation_id !== item.conversation_id) {
    messages.value = []
    messageCursor.value = null
    jobs.value = []
    draft.value = ''
  }
  selected.value = item
  page.value = 'chat'
  backlog.value = item.state === 'archived'
  refresh(item.conversation_id).catch(report)
  if (notify) broadcast('companion:conversation-selected', { characterId: item.character_id, conversationId: item.conversation_id })
}
function resumeReading() {
  if (reading.value) open(reading.value)
}
let characterRequest = 0
async function selectCharacter(item: Character, notify = true) {
  if (character.value.character_id === item.character_id) {
    navigate('chat')
    return
  }
  const request = ++characterRequest
  character.value = item
  selected.value = undefined
  reading.value = undefined
  backgroundIndex.value = 0
  messages.value = []
  jobs.value = []
  draft.value = ''
  list.value = []
  listCursor.value = null
  filter.value = 'active'
  page.value = 'chat'
  try {
    await Promise.all([ensureAssets(item), loadList()])
    if (request !== characterRequest) return
    const first = list.value[0]
    if (first) open(first, notify)
    else if (notify) broadcast('companion:conversation-selected', { characterId: item.character_id, conversationId: null })
  } catch (cause) { report(cause) }
}
let creation: Promise<Conversation> | undefined
function create(withSession = false) {
  if (!creation) {
    creating.value = true
    creation = api<Conversation>('/conversations', 'POST', {
      character_id: character.value.character_id, with_session: withSession,
    })
      .then(item => {
        filter.value = 'active'
        list.value = [item, ...list.value.filter(entry => entry.conversation_id !== item.conversation_id && entry.state === 'active')]
        listCursor.value = null
        open(item)
        broadcast('companion:conversation-created', { characterId: item.character_id, conversationId: item.conversation_id })
        return item
      })
      .finally(() => { creation = undefined; creating.value = false })
  }
  return creation
}
async function changeState(item: Conversation) {
  const changed = await api<Conversation>(conversationPath(item.conversation_id), 'PATCH', {
    state: item.state === 'active' ? 'archived' : 'active',
  })
  if (selected.value?.conversation_id === changed.conversation_id) selected.value = changed
  if (reading.value?.conversation_id === changed.conversation_id) reading.value = changed
  await loadList()
  broadcast('companion:conversation-updated', { characterId: changed.character_id, conversationId: changed.conversation_id })
}
async function seal(item: Conversation) {
  const session = item.current_session_id
  if (!session || sealing.value) return
  sealing.value = session
  notice.value = ''
  try {
    const result = await api<{ current_session_id: string | null; pending: boolean }>(
      conversationPath(item.conversation_id) + '/sessions/' + encodeURIComponent(session) + '/seal', 'POST')
    await loadList()
    notice.value = result.pending
      ? '这一段已经封存，摘要还在生成，稍后会自动换到新的一段。'
      : '已封存，记忆已落库；新的一段已经开好。'
    const refreshed = list.value.find(entry => entry.conversation_id === item.conversation_id)
    if (selected.value?.conversation_id === item.conversation_id && refreshed) selected.value = refreshed
    broadcast('companion:conversation-updated', { characterId: item.character_id, conversationId: item.conversation_id })
  } finally { sealing.value = '' }
}
function requestArchive(item: Conversation) {
  const isCurrent = selected.value?.conversation_id === item.conversation_id
  if (!isCurrent || !item.current_session_id) { void archive(item, false).catch(report); return }
  notice.value = ''
  archiving.value = item.conversation_id
}
async function archive(item: Conversation, sealFirst: boolean) {
  archiving.value = ''
  if (sealFirst && item.current_session_id) await seal(item)
  await changeState(item)
  if (selected.value?.conversation_id !== item.conversation_id) return
  // 归档当前这段对话：界面清空，另开一条全新对话（带空会话）。
  selected.value = undefined
  reading.value = undefined
  messages.value = []
  jobs.value = []
  draft.value = ''
  await create(true)
}
async function rename(item: Conversation) {
  const title = newTitle.value

  const changed = await api<Conversation>(conversationPath(item.conversation_id), 'PATCH', { title: title.trim() || null })
  if (selected.value?.conversation_id === changed.conversation_id) selected.value = changed
  if (reading.value?.conversation_id === changed.conversation_id) reading.value = changed
  await loadList()
  broadcast('companion:conversation-updated', { characterId: changed.character_id, conversationId: changed.conversation_id })
}
async function send() {
  const content = draft.value.trim()
  if (!content || sealing.value || selected.value?.state === 'archived') return
  draft.value = ''
  const previous = selected.value
  let item: Conversation
  try { item = previous ?? await create() }
  catch (cause) { draft.value = [content, draft.value].filter(Boolean).join('\n'); report(cause); return }
  const id = item.conversation_id
  queues[id] ??= []
  queues[id].push({ id: crypto.randomUUID(), content, failed: false })
  void drain(item)
}
async function drain(item: Conversation) {
  const id = item.conversation_id
  if (draining.has(id)) return
  draining.add(id)
  const queue = queues[id]
  try {
    while (queue?.length) {
      const head = queue[0]
      if (!head) break
      head.failed = false
      try {
        const receipt = await api<{ save_state: string }>(conversationPath(id) + '/messages', 'POST', {
          request_id: head.id, character_id: item.character_id, content: head.content,
        })
        if (receipt.save_state !== 'committed') throw new Error('后端尚未确认消息保存')
        broadcast('companion:messages-changed', { characterId: item.character_id, conversationId: id })
      } catch (cause) { head.failed = true; report(cause); break }
      queue.shift()
      if (selected.value?.conversation_id === id) refresh(id).catch(report)
    }
  } finally { draining.delete(id) }
}
function onEnter(event: KeyboardEvent) {
  if (event.isComposing || event.shiftKey) return
  event.preventDefault()
  void send()
}
async function jobAction(job: Job, action: 'retry' | 'cancel') {
  await api('/generation-jobs/' + encodeURIComponent(job.job_id) + '/' + action, 'POST')
  if (selected.value) await refresh(selected.value.conversation_id)
}
function navigate(id: typeof page.value) {
  if (page.value === id) return
  visited.add(id)
  page.value = id
  error.value = ''
  if (id === 'chat' && !selected.value && list.value[0]) open(list.value[0])
  if (id === 'history') loadList().catch(report)
}
let events: EventSource | undefined
let stopBroadcast: (() => void) | undefined
let timer: ReturnType<typeof setTimeout> | undefined
let closed = false
let startupTimer: ReturnType<typeof setTimeout> | undefined
let connecting = false
let cancelBackendWait: (() => void) | undefined
function waitForBackend(): Promise<void> {
  const backend = window.electron?.backend
  if (!backend) return Promise.resolve()
  startupProgress.value = 18
  startupStatus.value = '正在启动本地服务与模型…'
  return new Promise(resolve => {
    let finished = false
    let timeout: ReturnType<typeof setTimeout> | undefined
    const finish = () => {
      if (finished) return
      finished = true
      if (timeout) clearTimeout(timeout)
      unsubscribe()
      cancelBackendWait = undefined
      resolve()
    }
    const unsubscribe = backend.onReady(finish)
    cancelBackendWait = finish
    void backend.isReady().then(ready => { if (ready) finish() }).catch(finish)
    timeout = setTimeout(finish, 65000)
  })
}
/** 启动时预缓存各角色的场景立绘指标与背景光照，全部就绪后才进入主界面。 */
function sceneAssetUrls() {
  const sprites: string[] = []
  const backgrounds: string[] = []
  for (const item of characters.value) {
    const current = characterAssets.value[item.character_id]
    const defaults = current?.default_expression
    if (!current || !defaults) continue
    for (const entry of current.assets) {
      if (entry.outfit_id === defaults.outfit_id) sprites.push(assetUrl(entry.url))
    }
    for (const entry of current.backgrounds) backgrounds.push(assetUrl(entry.url))
  }
  return { sprites, backgrounds }
}
async function warmSceneAssets() {
  const { sprites, backgrounds } = sceneAssetUrls()
  const total = Math.max(1, sprites.length + backgrounds.length)
  let done = 0
  const advance = () => {
    done++
    if (starting.value) startupProgress.value = Math.round(62 + Math.min(1, done / total) * 26)
  }
  await prefetchSpriteMetrics(sprites, advance, false)
  await prefetchLighting(backgrounds, advance)
}
async function prefetchLighting(urls: string[], onProgress?: () => void) {
  for (const url of new Set(urls)) {
    if (lightingCache.has(url)) continue
    try { lightingCache.set(url, await analyzeLighting(url)) } catch { /* 跳过无法分析的背景 */ }
    onProgress?.()
  }
}
async function initialize() {
  if (closed || connecting || events) return
  connecting = true
  clearTimeout(startupTimer)
  error.value = ''
  if (starting.value) {
    startupProgress.value = Math.max(startupProgress.value, 32)
    startupStatus.value = '正在连接本地服务…'
  }
  try {
    const result = await api<{ items: Character[] }>('/characters')
    if (closed) return
    if (!result.items.length) throw new Error('后端尚未登记角色')
    characters.value = result.items
    character.value = result.items.find(item => item.character_id === character.value.character_id) ?? result.items[0]
    online.value = true
    if (starting.value) {
      startupProgress.value = 62
      startupStatus.value = '正在加载角色、立绘与场景…'
    }
    await Promise.all([...result.items.map(ensureAssets), loadList()])
    if (!selected.value && list.value[0] && page.value === 'chat') open(list.value[0], false)
    if (closed) return
    if (starting.value) {
      startupStatus.value = '正在缓存角色立绘…'
      await warmSceneAssets()
      if (closed) return
    } else {
      void warmSceneAssets()
    }
    if (starting.value) {
      startupProgress.value = 90
      startupStatus.value = '正在恢复对话与回忆…'
    }
    events = new EventSource(endpoint('/events'))
    const update = () => {
      clearTimeout(timer)
      timer = setTimeout(() => { if (selected.value) refresh(selected.value.conversation_id).catch(report) }, 150)
    }
    events.onopen = () => { online.value = true; update() }
    events.onerror = () => { online.value = false }
    for (const type of ['reply_committed', 'generation_failed', 'generation_cancelled']) events.addEventListener(type, update)
    if (starting.value) {
      startupProgress.value = 100
      startupStatus.value = '准备完成'
      await new Promise(resolve => setTimeout(resolve, 250))
      if (!closed) starting.value = false
    }
  } catch (cause) {
    online.value = false
    if (starting.value) startupStatus.value = '服务仍在准备中，完成后会自动进入…'
    report(cause)
    if (!closed) startupTimer = setTimeout(() => { void initialize() }, 1500)
  } finally { connecting = false }
}
async function applyRemoteEvent(type: string, raw: unknown) {
  const data = (raw ?? {}) as Record<string, unknown>
  if (data._source === WINDOW_ID) return
  const characterId = typeof data.characterId === 'string' ? data.characterId : ''
  const conversationId = typeof data.conversationId === 'string' ? data.conversationId : null
  try {
    if (type === 'companion:conversation-selected') {
      const target = characters.value.find(item => item.character_id === characterId)
      if (target && target.character_id !== character.value.character_id) await selectCharacter(target, false)
      if (conversationId) {
        const item = list.value.find(entry => entry.conversation_id === conversationId)
          ?? await api<Conversation>(conversationPath(conversationId))
        open(item, false)
      } else {
        selected.value = undefined
        messages.value = []
        jobs.value = []
        page.value = 'chat'
      }
    } else if (type === 'companion:messages-changed') {
      if (selected.value?.conversation_id === conversationId) await refresh(conversationId!)
      if (character.value.character_id === characterId) await loadList()
    } else if (type === 'companion:conversation-created' || type === 'companion:conversation-updated') {
      if (character.value.character_id === characterId) await loadList()
    }
  } catch (cause) { report(cause) }
}
// ===== 应用更新检查：启动 5 秒后一次，之后每 12 小时一次；失败静默 =====
const updateRelease = ref<{ latest: string; url: string } | null>(null)
let updateTimer: number | undefined
let updateInterval: number | undefined

async function runUpdateCheck() {
  try {
    const result = await window.electron?.update?.check()
    if (!result) return
    if (localStorage.getItem('update:dismissed') === result.latest) return
    updateRelease.value = { latest: result.latest, url: result.url }
  } catch { /* 检查失败静默跳过 */ }
}

function dismissUpdate() {
  if (updateRelease.value) localStorage.setItem('update:dismissed', updateRelease.value.latest)
  updateRelease.value = null
}

function openReleasePage() {
  if (updateRelease.value) void window.electron?.update?.openRelease?.(updateRelease.value.url)
}

onMounted(async () => {
  narrowQuery.addEventListener('change', onNarrowChange)
  stopBroadcast = window.electron?.chat.onBroadcast((type, data) => { void applyRemoteEvent(type, data) })
  await waitForBackend()
  if (!closed) {
    void initialize()
    updateTimer = window.setTimeout(() => void runUpdateCheck(), 5000)
    updateInterval = window.setInterval(() => void runUpdateCheck(), 12 * 60 * 60 * 1000)
  }
})
onUnmounted(() => {
  closed = true
  narrowQuery.removeEventListener('change', onNarrowChange)
  cancelBackendWait?.()
  stopBroadcast?.()
  events?.close()
  clearTimeout(timer)
  clearTimeout(startupTimer)
  clearTimeout(updateTimer)
  clearInterval(updateInterval)
})
</script>

<template>
  <div class="companion" :class="{ 'friends-collapsed': sidebarCollapsed, 'is-immersive': immersiveMode, 'is-starting': starting }">
    <svg class="companion-svg-library" aria-hidden="true">
      <symbol id="companion-i-home" viewBox="0 0 24 24"><path d="m3 11 8.2-7a1.2 1.2 0 0 1 1.6 0L21 11"/><path d="M5.5 9.5V20h13V9.5M9.5 20v-6h5v6"/></symbol>
      <symbol id="companion-i-memory" viewBox="0 0 24 24"><path d="M5 3h12a2 2 0 0 1 2 2v16H7a3 3 0 0 1-3-3V5a2 2 0 0 1 2-2z"/><path d="M8 3v18M11 8h5M11 12h5"/></symbol>
      <symbol id="companion-i-calendar" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="16" rx="4"/><path d="M8 3v4M16 3v4M3 10h18M8 14h.01M12 14h.01M16 14h.01M8 17h.01M12 17h.01"/></symbol>
      <symbol id="companion-i-person" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M5 21a7 7 0 0 1 14 0"/></symbol>
      <symbol id="companion-i-settings" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="m12 2 1.2 2.7 2.8.5 2-1.4 2 2-1.4 2 .5 2.8L22 12l-2.9 1.4-.5 2.8 1.4 2-2 2-2-1.4-2.8.5L12 22l-1.2-2.7-2.8-.5-2 1.4-2-2 1.4-2-.5-2.8L2 12l2.9-1.4.5-2.8-1.4-2 2-2 2 1.4 2.8-.5z"/></symbol>
      <symbol id="companion-i-compose" viewBox="0 0 24 24"><path d="M20 14.5V19a2 2 0 0 1-2 2H6a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3h7"/><path d="m14 4 6 6M12.5 11.5 19 5l-1-1-6.5 6.5-.5 3zM7 16h5"/></symbol>
      <symbol id="companion-i-dialogue" viewBox="0 0 24 24"><path d="M5 5h14a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-8l-5 3v-3H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z"/><path d="M7 9h10M7 13h6"/></symbol>
      <symbol id="companion-i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/></symbol>
      <symbol id="companion-i-spark" viewBox="0 0 24 24"><path d="m12 2 1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8z"/></symbol>
    </svg>
    <TitleBar class="companion-titlebar"></TitleBar>
    <div v-if="updateRelease" class="companion-update-bar">
      <span>新版本 {{ updateRelease.latest }} 已发布，功能与修复见发布页</span>
      <button @click="openReleasePage">查看与下载</button>
      <button @click="dismissUpdate">忽略此版本</button>
    </div>
    <section v-if="starting" class="startup-screen" aria-live="polite" aria-busy="true">
      <div class="startup-art" aria-hidden="true"></div>
      <div class="startup-petals" aria-hidden="true"><i v-for="(petal, index) in startupPetals" :key="index" class="startup-petal" :style="petal"></i></div>
      <section class="startup-center">
        <h1 class="startup-title">RNA Companion</h1>
        <p class="startup-note">AI虽好，但也要更重视身边人哦</p>
        <div class="startup-progress" role="progressbar" aria-label="应用加载进度" aria-valuemin="0" aria-valuemax="100" :aria-valuenow="startupProgress">
          <div class="startup-progress-labels"><span>{{ startupProgress }}%</span></div>
          <div class="startup-bar"><i :style="{ width: startupProgress + '%' }"></i></div>
        </div>
        <p class="startup-description">{{ startupStatus }}</p>
      </section>
    </section>
    <div v-else class="companion-shell">
      <aside class="companion-friends" aria-label="联系人列表">
        <div class="friends-heading"><span>联系人 <b>{{ characters.length }}</b></span><button class="sidebar-toggle" :aria-expanded="!sidebarCollapsed" :title="sidebarCollapsed ? '展开联系人栏' : '收起联系人栏'" :aria-label="sidebarCollapsed ? '展开联系人栏' : '收起联系人栏'" @click="sidebarCollapsed = !sidebarCollapsed"><svg viewBox="0 0 24 24"><path d="m14 7-5 5 5 5"/></svg></button></div>
        <button v-for="item in characters" :key="item.character_id" class="friend-item" :class="{ chosen: character.character_id === item.character_id }" @click="selectCharacter(item)">
          <span class="friend-avatar"><img v-if="avatarFor(item.character_id)" :src="assetUrl(avatarFor(item.character_id)?.url ?? '')" :alt="item.name"><i></i></span>
          <span class="friend-copy"><strong>{{ item.name }}</strong><em v-if="character.character_id === item.character_id" :class="{ replying: replyState }">{{ replyState || selected?.title || '当前对话' }}</em></span>
        </button>
      </aside>
      <nav class="utility-rail" aria-label="快捷工具">
        <button class="main-view" :class="{ chosen: page === 'chat' }" title="对话" aria-label="对话" :aria-current="page === 'chat' ? 'page' : undefined" @click="navigate('chat')"><svg><use href="#companion-i-dialogue"/></svg></button>
        <i class="rail-divider"></i>
        <button v-for="tab in tabs.filter(tab => tab.id !== 'chat')" :key="tab.id" :class="[{ chosen: page === tab.id }, `utility-${tab.id}`]" :title="tab.label" :aria-label="tab.label" :aria-current="page === tab.id ? 'page' : undefined" @click="navigate(tab.id)">
          <svg :class="`icon-${tab.id}`"><use :href="`#companion-i-${tab.icon}`"/></svg>
        </button>
      </nav>
      <div class="companion-window">
      <main class="companion-main">
        <div v-if="error" class="companion-error" role="alert">{{ error }} <button @click="error = ''">关闭</button></div>
        <section v-show="page === 'chat'" class="companion-scene companion-panel-frame" :style="sceneStyle">
          <img v-for="entry in assets?.backgrounds" :key="entry.id" class="scene-background-layer"
            :class="{ 'scene-background-base': entry.id === assets?.default_background_id, 'scene-background-visible': entry.id === background?.id }"
            :src="assetUrl(entry.url)" alt="" aria-hidden="true">
          <div class="scene-top">
            <div class="scene-side">
              <div class="scene-actions">
                <button aria-label="切换背景" title="切换背景" :disabled="backgrounds.length < 2" @click="cycleBackground"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8.5" cy="10" r="1.6"/><path d="m3 16 4.5-4 3.5 3 3-2.5L21 18"/></svg></button>
                <button aria-label="聊天记录" title="聊天记录" :aria-expanded="backlog" @click="backlog = !backlog"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="3" width="12" height="18" rx="1"/><path d="M9 8h6m-6 4h6m-6 4h3"/></svg></button>
                <button :aria-label="immersiveMode ? '退出沉浸模式' : '进入沉浸模式'" :title="immersiveMode ? '退出沉浸模式' : '进入沉浸模式'" :aria-pressed="immersiveMode" @click="toggleImmersive"><svg viewBox="0 0 24 24" aria-hidden="true"><path v-if="immersiveMode" d="M4 9h5V4m11 5h-5V4M4 15h5v5m11-5h-5v5"/><path v-else d="M9 4H4v5m11-5h5v5M4 15v5h5m11-5v5h-5"/></svg></button>
              </div>
            </div>
          </div>
          <div class="scene-light scene-light-back" aria-hidden="true"></div>
          <div class="scene-contact-shadow" aria-hidden="true"></div>
          <div v-if="sprite" class="scene-sprite-frame">
            <img class="scene-sprite" :src="assetUrl(sprite.url)" :alt="character.name" @load="updateSpriteRatio">
            <div class="scene-sprite-light" aria-hidden="true"></div>
          </div>
          <aside v-if="backlog" class="scene-log">
            <h2>{{ selected?.title || '聊天记录' }}</h2>
            <button v-if="messageCursor" @click="older().catch(report)">加载更早消息</button>
            <p v-if="!messages.length">还没有消息</p>
            <p v-for="message in messages" :key="message.message_id" class="log-message"><strong>{{ message.role === 'user' ? '你' : character.name }}</strong>{{ message.content }}</p>
          </aside>
          <div class="scene-bottom">
            <div class="speaker">
              <span class="speaker-wave" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
              <strong>{{ character.name }}</strong>
            </div>
            <div class="scene-rule" aria-hidden="true"></div>
            <div class="scene-dialogue">
              <div v-if="latest?.japanese" class="scene-dialogue-ja">{{ latest.japanese }}</div>
              <div class="scene-dialogue-zh">{{ latest?.content ?? '' }}<span v-if="latest" class="dialogue-next" aria-hidden="true">▼</span></div>
            </div>
            <div v-if="pendingFailed || jobIssues.length" class="scene-status" aria-live="polite">
              <span v-if="pendingFailed">{{ pending.length }} 条待保存</span>
              <button v-if="pendingFailed && selected" @click="drain(selected)">重试发送</button>
              <div v-for="job in jobIssues" :key="job.job_id">
                {{ job.error?.message ?? '回复已取消' }}
                <button v-if="job.error?.retryable || job.status === 'cancelled'" @click="jobAction(job, 'retry').catch(report)">重试回复</button>
              </div>
            </div>
            <form v-if="selected?.state !== 'archived'" class="scene-input" :class="{ 'has-draft': draft.trim() }" @submit.prevent="send">
              <span class="composer-label" aria-hidden="true">你</span>
              <textarea v-model="draft" aria-label="消息" rows="1" maxlength="100000" :disabled="!!sealing" :placeholder="sealing ? '正在整理这一段，稍候…' : '输入消息'" @keydown.enter="onEnter" />
              <small class="composer-hint">Enter 发送 · Shift + Enter 换行</small>
              <button class="primary" :disabled="!draft.trim() || !online" aria-label="发送消息"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 4l18 8-18 8 4-8-4-8Zm4 8h14"/></svg><span>发送</span></button>
            </form>
            <div v-else class="scene-input"><span>对话已归档</span><button @click="changeState(selected).catch(report)">恢复对话</button></div>
          </div>
        </section>
        <section v-if="visited.has('history')" v-show="page === 'history'" class="companion-panel-frame journal-page history-panel" aria-label="聊天记录">
          <div class="history-book">
            <CompanionTranscript v-if="reading" :key="reading.conversation_id" :conversation="reading" :character-name="character.name" :portrait="portrait" @back="reading = undefined" @resume="resumeReading" />
            <div v-else class="journal-scroll">
              <header class="history-heading">
                <div><span class="journal-kicker">{{ character.name }}的对话</span><h1>聊天记录</h1></div>
                <button class="journal-stamp" :disabled="creating" @click="create().catch(report)">＋ 新建对话</button>
              </header>
              <div class="journal-tools">
                <button v-for="state in (['active', 'archived'] as const)" :key="state" :aria-pressed="filter === state" @click="filter = state; list = []; listCursor = null; loadList().catch(report)">{{ state === 'active' ? '进行中' : '已归档' }}</button>
                <input v-model="search" aria-label="筛选已加载的对话" placeholder="筛选已加载的对话…">
              </div>
              <p v-if="notice" class="journal-note">{{ notice }}</p>
              <div class="history-entries">
                <p v-if="!visible.length" class="journal-note">{{ search.trim() ? '没有匹配的对话。' : '这里还没有对话。' }}</p>
                <ol v-else class="history-list">
                  <li v-for="item in visible" :key="item.conversation_id" class="history-entry">
                    <button class="history-title" @click="reading = item">
                      <span class="archive-card-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#companion-i-dialogue"/></svg></span>
                      <span class="archive-card-copy"><strong>{{ item.title || '未命名对话' }}</strong><time :datetime="item.updated_at">{{ listTime(item.updated_at) }}</time></span>
                      <span class="archive-card-arrow" aria-hidden="true">›</span>
                    </button>
                    <div class="history-actions">
                      <button v-if="item.state === 'active'" @click="open(item)">继续聊天</button>
                      <button v-else @click="reading = item">查看历史</button>
                      <button v-if="renaming !== item.conversation_id" @click="renaming = item.conversation_id; newTitle = item.title ?? ''">改名</button>
                      <button v-if="item.state === 'active' && item.current_session_id" :disabled="sealing === item.current_session_id" title="封存这一段对话，交给记忆生产线整理，并开启新的一段" @click="seal(item).catch(report)">{{ sealing === item.current_session_id ? '封存中…' : '封存' }}</button>
                      <button v-if="item.state === 'active'" @click="requestArchive(item)">归档</button>
                      <button v-else @click="changeState(item).catch(report)">恢复</button>
                    </div>
                    <div v-if="archiving === item.conversation_id" class="history-rename">
                      <span>归档后不再显示；未封存的内容不会进入记忆。</span>
                      <button @click="archive(item, true).catch(report)">先封存再归档</button>
                      <button @click="archive(item, false).catch(report)">直接归档</button>
                      <button type="button" @click="archiving = ''">取消</button>
                    </div>
                    <form v-if="renaming === item.conversation_id" class="history-rename" @submit.prevent="rename(item).then(() => renaming = '').catch(report)"><input v-model="newTitle" aria-label="对话标题" maxlength="200"><button>保存</button><button type="button" @click="renaming = ''">取消</button></form>
                  </li>
                </ol>
                <button v-if="listCursor" class="journal-more" @click="loadList(true).catch(report)">加载更多 ↓</button>
              </div>
            </div>
          </div>
        </section>
        <CompanionMemory v-if="visited.has('memory')" v-show="page === 'memory'" :key="character.character_id" :character-id="character.character_id" :portrait="portrait" />
        <CompanionCharacter v-if="visited.has('character')" v-show="page === 'character'" :character="character" :assets="assets" />
        <CompanionSettings v-if="visited.has('settings')" v-show="page === 'settings'" :online="online" />
      </main>
      </div>
    </div>
  </div>
</template>



