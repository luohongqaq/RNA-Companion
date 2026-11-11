<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { assetUrl, type Assets, type Sprite } from './companionApi'
import { analyzeSpriteBox, FALLBACK_BOX, spriteSignature } from './spriteBox'

const props = defineProps<{ character: { character_id: string; name: string }; assets?: Assets; outfit?: string }>()
const emit = defineEmits<{ outfit: [id: string] }>()

const outfit = ref('')
const expression = ref('')

/** 服装 × 姿势 × 表情的精确立绘；缩略图只展示确实存在的组合。 */
function exact(outfitId: string, poseId: string, expressionId: string): Sprite | undefined {
  return props.assets?.assets.find(entry => entry.outfit_id === outfitId && entry.pose_id === poseId && entry.expression_id === expressionId)
}
interface OutfitThumb { id: string; name: string; sprite: Sprite }
const outfitThumbs = ref<OutfitThumb[]>([])
let outfitSequence = 0
const DUPLICATE_THRESHOLD = 20
function meanDifference(a: number[], b: number[]) {
  let total = 0
  for (let index = 0; index < a.length; index++) total += Math.abs(a[index] - b[index])
  return total / a.length
}
/** 同一套服装被不同动作重复导出时只保留第一项，按可见框灰度签名判定。 */
watch(() => props.assets, async assets => {
  const request = ++outfitSequence
  const candidates = Object.entries(assets?.outfits ?? {})
    .map(([id, item]) => ({ id, name: item.name, sprite: exact(id, item.default_pose_id, item.default_expression_id) }))
    .filter((item): item is OutfitThumb => !!item.sprite)
  const kept: OutfitThumb[] = []
  const signatures: number[][] = []
  for (const item of candidates) {
    let signature: number[] | undefined
    try { signature = await spriteSignature(assetUrl(item.sprite.url)) } catch { /* 分析失败时保留该项 */ }
    if (signature && signatures.some(keptSignature => meanDifference(keptSignature, signature) < DUPLICATE_THRESHOLD)) continue
    if (signature) signatures.push(signature)
    kept.push(item)
  }
  if (request === outfitSequence) outfitThumbs.value = kept
}, { immediate: true })
const expressionThumbs = computed(() => {
  const info = props.assets?.outfits[outfit.value]
  const pose = info?.default_pose_id ?? props.assets?.default_expression.pose_id ?? ''
  return Object.entries(props.assets?.expressions ?? {}).map(([id, name]) => ({
    id,
    name,
    sprite: exact(outfit.value, pose, id),
  })).filter(item => item.sprite)
})
const displayed = computed(() => {
  const info = props.assets?.outfits[outfit.value]
  const pose = info?.default_pose_id ?? props.assets?.default_expression.pose_id ?? ''
  return exact(outfit.value, pose, expression.value) ?? exact(outfit.value, pose, info?.default_expression_id ?? '')
})
function chooseOutfit(id: string) {
  outfit.value = id
  expression.value = props.assets?.outfits[id]?.default_expression_id ?? ''
  emit('outfit', id)
}
watch(() => props.outfit, value => { if (value && value !== outfit.value) outfit.value = value })

watch(() => props.assets, assets => {
  outfit.value = props.outfit ?? assets?.default_expression.outfit_id ?? ''
  expression.value = assets?.default_expression.expression_id ?? ''
}, { immediate: true })

/** 大图按可见框归一（不同画布的人物在相框里大小一致）。 */
const stageStyle = ref<Record<string, string>>({})
watch(() => displayed.value?.url, async path => {
  if (!path) { stageStyle.value = {}; return }
  const fallbackH = 88 / (FALLBACK_BOX.bottom - FALLBACK_BOX.top)
  const fallbackTop = 6 - FALLBACK_BOX.top * fallbackH
  try {
    const box = await analyzeSpriteBox(assetUrl(path))
    const height = 88 / (box.bottom - box.top)
    stageStyle.value = { '--photo-h': height.toFixed(2) + '%', '--photo-top': (6 - box.top * height).toFixed(2) + '%' }
  } catch {
    stageStyle.value = { '--photo-h': fallbackH.toFixed(2) + '%', '--photo-top': fallbackTop.toFixed(2) + '%' }
  }
}, { immediate: true })
</script>

<template>
  <section class="companion-panel-frame journal-page character-panel" aria-label="角色照片">
    <aside class="character-index journal-scroll">
      <span class="journal-kicker">角色照片</span>
      <h1>{{ character.name }}</h1>
      <h2 class="sprite-group">服装</h2>
      <div class="sprite-grid">
        <button v-for="item in outfitThumbs" :key="item.id" :class="{ chosen: outfit === item.id }" :aria-pressed="outfit === item.id" :title="item.name" @click="chooseOutfit(item.id)">
          <img loading="lazy" :src="assetUrl(item.sprite.url)" alt="">
        </button>
      </div>
      <h2 class="sprite-group">表情</h2>
      <div class="sprite-grid expression-grid">
        <button v-for="item in expressionThumbs" :key="item.id" :class="{ chosen: expression === item.id }" :aria-pressed="expression === item.id" :title="item.name" @click="expression = item.id"><span class="sprite-name">{{ item.name }}</span></button>
      </div>
    </aside>
    <div class="character-photo-stage" :style="stageStyle">
      <img v-if="displayed" :src="assetUrl(displayed.url)" :alt="character.name">
    </div>
  </section>
</template>
