import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import electron from 'vite-plugin-electron/simple'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig(({ mode }) => {
  const isElectron = mode === 'electron'

  return {
    // 字体/图标等静态资源经 publicDir 映射到站点根
    // 开发：/fonts/xxx.woff2、/icon/icon.png
    // 生产：复制到 dist 根，由 Electron 的 app://rna 协议经 BASE_URL('./') 加载
    publicDir: fileURLToPath(new URL('./assets', import.meta.url)),
    plugins: [
      vue(),
      isElectron &&
        electron({
          main: {
            entry: 'electron/main.ts',
            vite: {
              build: {
                outDir: 'dist-electron',
                rollupOptions: {
                  external: ['electron'],
                },
              },
            },
          },
          preload: {
            input: 'electron/preload.ts',
            vite: {
              build: {
                outDir: 'dist-electron',
                rollupOptions: {
                  external: ['electron'],
                },
              },
            },
          },
          // v1.x: renderer 功能内置于 simple API
          renderer: {},
        }),
    ].filter(Boolean),
    base: './',
    server: {
      port: 5173,
      proxy: {
        '/api': 'http://127.0.0.1:8765',
        '/health': 'http://127.0.0.1:8765',
      },
    },
  }
})
