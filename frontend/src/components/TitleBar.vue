<template>
  <!-- 自定义标题栏 — 无边框窗口拖动 + 右上角窗口控制按钮 -->
  <header class="title-bar">
    <slot name="context"></slot>

    <!-- 右侧：窗口控制按钮（Win11 风格 — 圆角 hover + 精致字形） -->
    <div class="title-controls">
      <button
        class="ctrl-btn ctrl-minimize"
        title="最小化"
        aria-label="最小化"
        @click="onMinimize"
      >
        <svg viewBox="0 0 12 12" fill="none">
          <path d="M2.5 6h7" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
        </svg>
      </button>
      <button
        class="ctrl-btn ctrl-maximize"
        :title="isMaximized ? '向下还原' : '最大化'"
        :aria-label="isMaximized ? '向下还原' : '最大化'"
        @click="onToggleMaximize"
      >
        <!-- 最大化图标 — 圆角方框 -->
        <svg v-if="!isMaximized" viewBox="0 0 12 12" fill="none">
          <rect x="2.75" y="2.75" width="6.5" height="6.5" rx="1.5" stroke="currentColor" stroke-width="1.4"/>
        </svg>
        <!-- 还原图标 — 前后叠层双框 -->
        <svg v-else viewBox="0 0 12 12" fill="none">
          <rect x="2.5" y="4.25" width="5.25" height="5.25" rx="1.25" stroke="currentColor" stroke-width="1.4"/>
          <path d="M4.5 2.75h3.5a1.25 1.25 0 0 1 1.25 1.25v3.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
        </svg>
      </button>
      <button
        class="ctrl-btn ctrl-close"
        title="关闭"
        aria-label="关闭"
        @click="onClose"
      >
        <svg viewBox="0 0 12 12" fill="none">
          <path d="M3.5 3.5l5 5M8.5 3.5l-5 5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
        </svg>
      </button>
    </div>
  </header>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'

const isMaximized = ref(false)
const electron = (window as any).electron

function onMinimize() {
  electron?.window?.minimize?.()
}

function onToggleMaximize() {
  electron?.window?.toggleMaximize?.()
  // 同步状态
  electron?.window?.isMaximized?.().then((val: boolean) => {
    isMaximized.value = val
  })
}

function onClose() {
  electron?.window?.close?.()
}

onMounted(() => {
  // 初始查询最大化状态
  electron?.window?.isMaximized?.().then((val: boolean) => {
    isMaximized.value = val
  })
  // 监听窗口最大化/还原事件（通过 preload IPC）
  if (electron?.window?.onMaximizeChange) {
    electron.window.onMaximizeChange((val: boolean) => {
      isMaximized.value = val
    })
  }
})
</script>

<style scoped>
.title-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 44px;
  padding: 0 8px 0 14px;
  background: rgba(249, 251, 255, 0.96);
  border-bottom: 1px solid rgba(111, 126, 176, 0.13);
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.8);
  flex-shrink: 0;
  user-select: none;
  position: relative;
  z-index: 5;
  -webkit-app-region: drag;
}

/* ===== 右侧控制按钮（Win11 风格 — 圆角 hover 底） ===== */
.title-controls {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-left: auto;
  /* 按钮区域不拖动 */
  -webkit-app-region: no-drag;
}

.ctrl-btn {
  width: 42px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  color: var(--color-text-secondary, #64748B);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.ctrl-btn svg {
  width: 12px;
  height: 12px;
}

.ctrl-btn:hover {
  background: var(--color-bg-hover, rgba(37, 99, 235, 0.06));
  color: var(--color-text-primary, #1E293B);
}

.ctrl-btn:active {
  transform: scale(0.94);
}

.ctrl-btn:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring, 0 0 0 3px rgba(59, 130, 246, 0.12));
}

/* 关闭按钮特殊悬停：红色 */
.ctrl-close:hover {
  background: var(--color-window-close);
  color: var(--color-text-on-primary, #FFFFFF);
}
</style>
