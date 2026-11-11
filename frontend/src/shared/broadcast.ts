/**
 * 跨窗口广播工具
 *
 * 通过 Electron IPC 主进程中转广播：
 *   窗口A → ipcRenderer.send('chat:broadcast') → 主进程 → 其他窗口 webContents.send
 *
 * 防回环机制：每个窗口有唯一 WINDOW_ID，广播数据带 _source 标记，接收方据此忽略自己发出的消息。
 */

/** 当前窗口唯一标识，用于防回环 */
export const WINDOW_ID = Math.random().toString(36).slice(2, 9)

/** 广播聊天事件给其他窗口 */
export function broadcast(
  type: string,
  data: Record<string, unknown> = {}
): void {
  window.electron?.chat?.broadcast(type, { ...data, _source: WINDOW_ID })
}
