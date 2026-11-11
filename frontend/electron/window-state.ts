/**
 * 窗口状态持久化 — 保存/恢复窗口位置、尺寸
 *
 * 使用 JSON 文件存储在 Electron userData 目录下，
 * 独立于 Python 后端的 app.yaml，避免读写冲突。
 *
 * 持久化内容：
 * - chatWindow: { x, y, width, height }
 */
import { app } from 'electron'
import path from 'node:path'
import fs from 'node:fs'

/** 窗口状态数据结构 */
export interface WindowState {
  x: number
  y: number
  width: number
  height: number
}

/** 所有窗口的状态集合 */
export interface AllWindowState {
  chat: WindowState
}

/** 默认窗口状态 */
const DEFAULT_STATE: AllWindowState = {
  chat: { x: 247, y: 133, width: 1000, height: 664 },
}

/** 状态文件路径 */
function getStatePath(): string {
  return path.join(app.getPath('userData'), 'window-state.json')
}

/** 当前缓存的完整状态 */
let cachedState: AllWindowState | null = null

/** 防抖定时器 */
let saveTimer: NodeJS.Timeout | null = null

/** 保存防抖延迟 */
const SAVE_DEBOUNCE_MS = 500

/**
 * 加载窗口状态（启动时调用）
 * 读取失败时返回默认值
 */
export function loadWindowState(): AllWindowState {
  try {
    const filePath = getStatePath()
    if (!fs.existsSync(filePath)) {
      return { ...DEFAULT_STATE }
    }

    const raw = fs.readFileSync(filePath, 'utf-8')
    const parsed = JSON.parse(raw) as Partial<AllWindowState>

    // 合并默认值，确保字段完整
    const state: AllWindowState = {
      chat: { ...DEFAULT_STATE.chat, ...parsed.chat },
    }

    cachedState = state
    return state
  } catch (err) {
    console.error('[window-state] 加载失败:', err)
    return { ...DEFAULT_STATE }
  }
}

/**
 * 更新单个窗口的状态（触发防抖保存）
 * @param key 窗口标识
 * @param partial 新的状态字段
 */
export function updateWindowState(
  key: keyof AllWindowState,
  partial: Partial<WindowState>
): void {
  if (!cachedState) {
    cachedState = loadWindowState()
  }
  cachedState[key] = { ...cachedState[key], ...partial }
  scheduleSave()
}

/**
 * 立即保存当前状态到磁盘
 * 退出时调用
 */
export function saveWindowStateNow(): void {
  if (saveTimer) {
    clearTimeout(saveTimer)
    saveTimer = null
  }
  if (!cachedState) return
  doSave()
}

/**
 * 防抖保存
 */
function scheduleSave(): void {
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    saveTimer = null
    doSave()
  }, SAVE_DEBOUNCE_MS)
}

/** 实际写入文件 */
function doSave(): void {
  if (!cachedState) return
  try {
    const filePath = getStatePath()
    const dir = path.dirname(filePath)
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true })
    }
    fs.writeFileSync(filePath, JSON.stringify(cachedState, null, 2), 'utf-8')
  } catch (err) {
    console.error('[window-state] 保存失败:', err)
  }
}
