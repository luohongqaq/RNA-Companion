<script setup lang="ts">
import { ref } from 'vue'
import { analyzeSpriteBox, FALLBACK_BOX } from './spriteBox'

defineProps<{ src?: string }>()

/** 贴纸相框按"人物可见框"的宽高比自适应，配合 contain 保证任何画布都完整显示人物。 */
const frameRatio = ref(0.75)

async function fitFrame(event: Event) {
  const image = event.target as HTMLImageElement
  try {
    const box = await analyzeSpriteBox(image.src)
    const ratio = (box.right - box.left) / (box.bottom - box.top)
    frameRatio.value = Math.min(.9, Math.max(.55, ratio))
  } catch {
    frameRatio.value = (FALLBACK_BOX.right - FALLBACK_BOX.left) / (FALLBACK_BOX.bottom - FALLBACK_BOX.top)
  }
}
</script>

<template>
  <figure class="journal-art" aria-hidden="true">
    <div class="journal-art-frame" :style="{ aspectRatio: String(frameRatio) }">
      <svg viewBox="0 0 120 84" preserveAspectRatio="none">
        <circle cx="74" cy="48" r="29" />
        <path d="M11 55c18-35 61-43 101-30M8 69c32 4 76-11 102-34" />
        <path class="journal-art-stars" d="M22 21v12m-6-6h12m69 25v16m-8-8h16M105 12v8m-4-4h8" />
      </svg>
      <img v-if="src" :key="src" :src="src" alt="" @load="fitFrame" @error="($event.currentTarget as HTMLImageElement).hidden = true">
    </div>
  </figure>
</template>
