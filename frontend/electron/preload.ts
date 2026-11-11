/**
 * Electron 预加载脚本 — 通过 contextBridge 安全暴露 API
 *
 * - window：通用窗口控制（关闭/最小化/最大化）
 * - backend：后端就绪状态与目录入口
 * - chat：跨窗口聊天事件广播
 */
import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('electron', {
  window: {
    close: () => ipcRenderer.send('window:close'),
    minimize: () => ipcRenderer.send('window:minimize'),
    /** 切换最大化/还原 */
    toggleMaximize: () => ipcRenderer.send('window:toggle-maximize'),
    /** 查询当前是否最大化 */
    isMaximized: () => ipcRenderer.invoke('window:is-maximized'),
    /** 监听最大化状态变化（主进程推送） */
    onMaximizeChange: (callback: (isMaximized: boolean) => void) =>
      ipcRenderer.on('window:maximize-changed', (_event, val: boolean) => callback(val)),
  },
  backend: {
    /** 主进程健康检查是否已通过 */
    isReady: () => ipcRenderer.invoke('backend:is-ready'),
    openDirectory: (): Promise<string> => ipcRenderer.invoke('backend:open-directory'),
    /** 后端就绪通知；返回取消监听函数 */
    onReady: (callback: () => void) => {
      const listener = () => callback()
      ipcRenderer.on('backend:ready', listener)
      return () => ipcRenderer.removeListener('backend:ready', listener)
    },
  },
  chat: {
    /** 广播聊天事件给其他窗口（主进程转发，不回传给发送者） */
    broadcast: (type: string, data: unknown) =>
      ipcRenderer.send('chat:broadcast', type, data),
    /** 监听其他窗口广播的聊天事件；返回取消监听函数 */
    onBroadcast: (callback: (type: string, data: unknown) => void) => {
      const listener = (_event: Electron.IpcRendererEvent, type: string, data: unknown) => callback(type, data)
      ipcRenderer.on('chat:broadcast', listener)
      return () => ipcRenderer.removeListener('chat:broadcast', listener)
    },
  },
  update: {
    /** 查询 GitHub Releases 最新版本；无新版、离线或仓库未配置时返回 null */
    check: (): Promise<{ current: string; latest: string; url: string } | null> =>
      ipcRenderer.invoke('update:check'),
    /** 用系统默认浏览器打开发布页 */
    openRelease: (url: string) => ipcRenderer.invoke('update:open-release', url),
  },
})
