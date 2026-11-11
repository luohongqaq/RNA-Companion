<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from './companionApi'
defineProps<{ online: boolean }>()
interface Settings {
  base_url: string; model: string; api_key_configured: boolean; version: string
  effective_base_url: string; effective_model: string; overridden_fields: string[]; restart_required: boolean
}
const purpose = ref<'reply' | 'default'>('reply')
const data = ref<Settings>()
const baseUrl = ref('')
const model = ref('')
const key = ref('')
const clear = ref(false)
const saving = ref(false)
const status = ref('')
const directoryStatus = ref('')
const canOpenDirectory = !!window.electron?.backend?.openDirectory
async function openBackendDirectory() {
  directoryStatus.value = ''
  try {
    const error = await window.electron!.backend.openDirectory()
    if (error) directoryStatus.value = '无法打开后端目录：' + error
  } catch { directoryStatus.value = '无法打开后端目录，请稍后重试。' }
}
const path = () => '/model-settings/' + purpose.value
function show(result: Settings) {
  data.value = result
  baseUrl.value = result.base_url
  model.value = result.model
  key.value = ''
  clear.value = false
}
async function load() {
  const target = path()
  data.value = undefined
  key.value = ''
  status.value = ''
  try {
    const result = await api<Settings>(target)
    if (target === path()) show(result)
  } catch (cause) { if (target === path()) status.value = String(cause) }
}
watch(purpose, load, { immediate: true })
async function save() {
  if (!data.value) return
  saving.value = true
  status.value = ''
  try {
    show(await api<Settings>(path(), 'PATCH', {
      version: data.value.version, base_url: baseUrl.value.trim(), model: model.value.trim(),
      ...(clear.value ? { api_key: null } : key.value ? { api_key: key.value } : {}),
    }))
    status.value = '连接设置已保存'
  } catch (cause) { status.value = String(cause) }
  finally { saving.value = false }
}
</script>
<template>
  <section class="companion-page">
    <header class="page-heading"><div><small>CONNECTION</small><h1>模型设置</h1></div><span>{{ online ? '● 本地服务在线' : '○ 本地服务离线' }}</span></header>
    <div class="settings-layout">
      <div>
        <div class="page-tools"><button :disabled="saving" :class="{ chosen: purpose === 'reply' }" @click="purpose = 'reply'">聊天回复模型</button><button :disabled="saving" :class="{ chosen: purpose === 'default' }" @click="purpose = 'default'">默认模型</button></div>
        <form v-if="data" class="surface settings-form" @submit.prevent="save">
          <small>{{ purpose === 'reply' ? 'REPLY MODEL' : 'DEFAULT MODEL' }}</small>
          <h2>{{ purpose === 'reply' ? '聊天回复连接' : '默认模型连接' }}</h2>
          <label>API 地址<input v-model="baseUrl" type="url" required></label>
          <label>模型名称<input v-model="model" required></label>
          <label>API 密钥<input v-model="key" type="password" autocomplete="new-password" :disabled="clear" :placeholder="data.api_key_configured ? '已配置，留空保留' : '未配置'"></label>
          <label v-if="data.api_key_configured" class="checkbox-label"><input v-model="clear" type="checkbox"> 清除保存的密钥</label>
          <div class="settings-effective">下次启动：{{ data.effective_model }} · {{ data.effective_base_url }}<p v-if="data.overridden_fields.length">环境变量覆盖：{{ data.overridden_fields.join('、') }}</p><p v-if="data.restart_required">需要重启后端后生效。</p></div>
          <button class="primary" :disabled="saving">保存连接设置</button>
        </form>
        <button v-else-if="status" @click="load">重新读取</button>
        <p v-if="status" role="status">{{ status }}</p>
      </div>
      <aside><div v-if="canOpenDirectory" class="backend-directory"><button class="backend-directory-button" type="button" @click="openBackendDirectory"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#e5bb75" d="M2 5.5A2.5 2.5 0 0 1 4.5 3h4.2l2 2H19a3 3 0 0 1 3 3v1H2z"/><path fill="#f1ce91" d="M2 8h20l-1.3 10.3A3 3 0 0 1 17.7 21H5.1a3 3 0 0 1-3-2.6z"/></svg>打开后端目录 <span aria-hidden="true">↗</span></button><p v-if="directoryStatus" role="status">{{ directoryStatus }}</p></div><small>HOW IT WORKS</small><h2>两种模型，各自做好一件事。</h2><h3>聊天回复</h3><p>生成角色台词与表情。</p><h3>默认模型</h3><p>整理日记与主题，并支持聊天时的记忆检索。</p></aside>
    </div>
  </section>
</template>
