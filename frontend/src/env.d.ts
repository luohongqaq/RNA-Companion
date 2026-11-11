/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, never>, Record<string, never>, unknown>
  export default component
}

interface Window {
  electron?: {
    window: {
      close: () => void
      minimize: () => void
      toggleMaximize: () => void
      isMaximized: () => Promise<boolean>
      onMaximizeChange: (callback: (isMaximized: boolean) => void) => void
    }
    backend: {
      isReady: () => Promise<boolean>
      openDirectory: () => Promise<string>
      onReady: (callback: () => void) => () => void
    }
    chat: {
      broadcast: (type: string, data: unknown) => void
      onBroadcast: (callback: (type: string, data: unknown) => void) => () => void
    }
    update: {
      check: () => Promise<{ current: string; latest: string; url: string } | null>
      openRelease: (url: string) => Promise<void>
    }
  }
}
