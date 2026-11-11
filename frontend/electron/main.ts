/**
 * Electron 主进程入口
 *
 * 职责：
 * 1. 启动 Python 后端子进程（python-bridge）
 * 2. 创建主界面 BrowserWindow（左场景 + 右聊天），启动显示
 * 3. 窗口控制 IPC（最小化/最大化/关闭）
 * 4. 系统托盘：图标 + 菜单（显示/隐藏、退出），双击切换显隐
 * 5. 退出时优雅终止 Python 进程并销毁托盘
 */
import { app, BrowserWindow, ipcMain, nativeImage, net, protocol, shell } from 'electron'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { PythonBridge, getBackendDirectory } from './python-bridge'
import { createTray, destroyTray } from './tray'
import {
  loadWindowState,
  updateWindowState,
  saveWindowStateNow,
} from './window-state'

/** 生产环境页面来源：app://rna；file:// 页面的 Origin 是 null，后端 CORS 无法放行 */
const APP_SCHEME = 'app'
const APP_HOST = 'rna'
const DIST_DIR = path.join(__dirname, '..', 'dist')

/** 发布仓库地址（owner/repo）：公开仓库创建后替换此占位符，更新检查与发布页链接都依赖它。 */
const UPDATE_REPO_SLUG = 'your-name/rna-companion'

interface UpdateInfo {
  current: string
  latest: string
  url: string
}

/** 按 数字段 逐段比较语义化版本；left 更新返回正数。 */
function compareVersions(left: string, right: string): number {
  const toParts = (value: string) => value.split('.').map(part => Number.parseInt(part, 10) || 0)
  const leftParts = toParts(left)
  const rightParts = toParts(right)
  const length = Math.max(leftParts.length, rightParts.length)
  for (let index = 0; index < length; index += 1) {
    const diff = (leftParts[index] ?? 0) - (rightParts[index] ?? 0)
    if (diff !== 0) return diff
  }
  return 0
}

/** 查询 GitHub Releases 最新版本；仓库未配置、离线或无新版时返回 null。 */
async function queryLatestRelease(): Promise<UpdateInfo | null> {
  const response = await net.fetch(`https://api.github.com/repos/${UPDATE_REPO_SLUG}/releases/latest`, {
    headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'RNA-Companion' },
    signal: AbortSignal.timeout(8000),
  })
  if (!response.ok) return null
  const release = (await response.json()) as { tag_name?: string; html_url?: string; draft?: boolean; prerelease?: boolean }
  const latest = release.tag_name?.replace(/^v/, '')
  if (!latest || release.draft || release.prerelease) return null
  const current = app.getVersion()
  if (compareVersions(latest, current) <= 0) return null
  return { current, latest, url: release.html_url ?? `https://github.com/${UPDATE_REPO_SLUG}/releases/latest` }
}

// 必须在 app ready 之前声明；standard 让协议拥有正常 Origin，secure 使其成为安全上下文，
// supportFetchAPI/corsEnabled 允许页面内 fetch 跨源请求后端。
protocol.registerSchemesAsPrivileged([
  {
    scheme: APP_SCHEME,
    privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true },
  },
])

/** 把 app://rna/<路径> 映射到 dist 目录下的文件 */
function registerAppProtocol(): void {
  const root = path.resolve(DIST_DIR)
  protocol.handle(APP_SCHEME, (request) => {
    const url = new URL(request.url)
    if (url.host !== APP_HOST) {
      return new Response('Not Found', { status: 404 })
    }
    let relativePath = decodeURIComponent(url.pathname)
    if (relativePath === '/' || relativePath === '') {
      relativePath = '/index.html'
    }
    const target = path.resolve(DIST_DIR, '.' + relativePath)
    // 防止 app://rna/../../ 越出 dist 目录
    if (target !== root && !target.startsWith(root + path.sep)) {
      return new Response('Forbidden', { status: 403 })
    }
    return net.fetch(pathToFileURL(target).toString())
  })
}

const pythonBridge = new PythonBridge()

/** 窗口引用 */
let chatWindow: BrowserWindow | null = null
let backendReady = false

/** 应用图标路径 — 开发期从 assets/icon，生产期从 resourcesPath */
function getIconPath(): string {
  return app.isPackaged
    ? path.join(process.resourcesPath, 'icon', 'icon.png')
    : path.join(__dirname, '..', 'assets', 'icon', 'icon.png')
}

/** 应用图标 nativeImage（供 BrowserWindow 和 Windows 任务栏使用） */
function getAppIcon(): Electron.NativeImage {
  return nativeImage.createFromPath(getIconPath())
}

// Windows 任务栏图标：设置 AppUserModelID
if (process.platform === 'win32') {
  app.setAppUserModelId('com.rna.companion')
}

/** 为窗口绑定最大化状态变化推送（渲染进程监听以更新按钮图标） */
function bindMaximizeEvents(win: BrowserWindow): void {
  win.on('maximize', () => {
    win.webContents.send('window:maximize-changed', true)
  })
  win.on('unmaximize', () => {
    win.webContents.send('window:maximize-changed', false)
  })
}

/**
 * 创建一个 BrowserWindow
 * @param devPath  开发模式下加载的路径（相对 Vite dev server URL）
 * @param prodFile 生产模式下加载的文件名
 * @param options  BrowserWindow 额外配置
 */
function createWindow(
  devPath: string,
  prodFile: string,
  options: Electron.BrowserWindowConstructorOptions
): BrowserWindow {
  const win = new BrowserWindow({
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    ...options,
  })

  const devServerUrl = process.env.VITE_DEV_SERVER_URL

  if (devServerUrl) {
    const url = new URL(devPath, devServerUrl).toString()
    win.loadURL(url).catch((err) => console.error(`[main] loadURL 失败 (${devPath}):`, err))
  } else {
    // 生产环境走自定义协议，页面 Origin 为 app://rna，后端 CORS 已放行该来源
    win.loadURL(`${APP_SCHEME}://${APP_HOST}/${prodFile}`)
      .catch((err) => console.error(`[main] loadURL 失败 (app://${APP_HOST}/${prodFile}):`, err))
  }

  return win
}

app.whenReady().then(async () => {
  // 关联便携版 exe 的图标：Windows 任务栏/通知按 AppUserModelID 取图标
  if (app.isPackaged) app.setAppUserModelId('com.rna.companion')

  // 自定义协议处理器必须先于窗口加载注册
  registerAppProtocol()

  // ===== 通用窗口控制 IPC =====
  ipcMain.on('window:close', (event) => {
    BrowserWindow.fromWebContents(event.sender)?.close()
  })
  ipcMain.on('window:minimize', (event) => {
    BrowserWindow.fromWebContents(event.sender)?.minimize()
  })
  // 切换最大化/还原
  ipcMain.on('window:toggle-maximize', (event) => {
    const win = BrowserWindow.fromWebContents(event.sender)
    if (!win) return
    if (win.isMaximized()) {
      win.unmaximize()
    } else {
      win.maximize()
    }
  })
  // 查询当前是否最大化
  ipcMain.handle('window:is-maximized', (event) => {
    const win = BrowserWindow.fromWebContents(event.sender)
    return win ? win.isMaximized() : false
  })
  ipcMain.handle('backend:is-ready', () => backendReady)
  ipcMain.handle('backend:open-directory', () => shell.openPath(getBackendDirectory()))
  // 应用更新检查：渲染进程启动后与每 12 小时各调用一次；失败静默返回 null
  ipcMain.handle('update:check', async () => {
    try {
      return await queryLatestRelease()
    } catch {
      return null
    }
  })
  // 仅放行 https，用系统默认浏览器打开发布页
  ipcMain.handle('update:open-release', (event, url: unknown) => {
    if (typeof url === 'string' && url.startsWith('https://')) return shell.openExternal(url)
    return undefined
  })

  // 主进程转发聊天事件（当前仅主窗口；保留以兼容多窗口广播）
  ipcMain.on('chat:broadcast', (event, type: string, data: unknown) => {
    for (const win of [chatWindow]) {
      if (!win || win.isDestroyed() || win.webContents.id === event.sender.id) continue
      win.webContents.send('chat:broadcast', type, data)
    }
  })

  // ===== 窗口状态持久化 IPC =====
  // 加载保存的窗口状态
  const savedState = loadWindowState()

  // 1. 主界面窗口：左场景 + 右聊天，立即显示（加载进度由前端遮罩层展示）
  chatWindow = createWindow('/', 'index.html', {
    x: savedState.chat.x,
    y: savedState.chat.y,
    width: savedState.chat.width,
    height: savedState.chat.height,
    title: 'RNA Companion',
    frame: false,
    icon: getAppIcon(),
    autoHideMenuBar: true,
    backgroundColor: '#F6F9FC',
  })
  bindMaximizeEvents(chatWindow)
  // 关闭时隐藏而非销毁，便于从托盘重新打开
  chatWindow.on('close', (e) => {
    e.preventDefault()
    chatWindow?.hide()
  })
  // 主界面移动/缩放时保存位置和尺寸
  chatWindow.on('move', () => {
    if (!chatWindow || chatWindow.isDestroyed()) return
    const [x, y] = chatWindow.getPosition()
    updateWindowState('chat', { x, y })
  })
  chatWindow.on('resize', () => {
    if (!chatWindow || chatWindow.isDestroyed()) return
    const [width, height] = chatWindow.getSize()
    updateWindowState('chat', { width, height })
  })

  // 2. 系统托盘：图标 + 菜单（显示/隐藏、退出），双击切换显隐
  createTray([chatWindow])

  // 3. 异步启动 Python 后端 — 不阻塞窗口的创建与显示
  //    主界面等待健康检查通过后再加载数据
  console.log('[main] 正在启动 Python 后端...')
  pythonBridge
    .start()
    .then(() => {
      backendReady = true
      chatWindow?.webContents.send('backend:ready')
      console.log('[main] Python 后端已就绪 ✓')
    })
    .catch((err) => console.error('[main] Python 后端启动失败:', err))
}).catch((err) => {
  console.error('[main] 应用初始化失败:', err)
})

// 退出时优雅终止 Python 进程
let isQuitting = false

/** 创建关闭程序时的数据整理提示小窗口 */
function createShutdownOverlay(): BrowserWindow | null {
  const overlay = new BrowserWindow({
    width: 320,
    height: 140,
    frame: false,
    resizable: false,
    minimizable: false,
    maximizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    transparent: true,
    focusable: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  // 居中显示
  overlay.center()

  // 内联 HTML — 不依赖外部文件，避免打包路径问题
  const html = `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    width: 320px;
    height: 140px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(255, 255, 255, 0.95);
    border-radius: 16px;
    border: 1px solid rgba(59, 130, 246, 0.15);
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.12);
    font-family: -apple-system, "Microsoft YaHei", "PingFang SC", sans-serif;
    -webkit-app-region: no-drag;
    user-select: none;
  }
  .card {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 14px;
  }
  .spinner {
    width: 32px;
    height: 32px;
    border: 3px solid rgba(59, 130, 246, 0.12);
    border-top-color: #3B82F6;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .text {
    font-size: 14px;
    color: #475569;
    font-weight: 500;
  }
</style>
</head>
<body>
  <div class="card">
    <div class="spinner"></div>
    <div class="text">正在整理数据，请稍候...</div>
  </div>
</body>
</html>`

  overlay.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html))
  overlay.setIgnoreMouseEvents(true)
  return overlay
}

app.on('before-quit', (event) => {
  if (isQuitting) return
  event.preventDefault()
  isQuitting = true
  console.log('[main] 正在关闭 Python 后端...')

  // 显示数据整理提示窗口
  const overlay = createShutdownOverlay()

  // 立即保存窗口状态（跳过防抖延迟）
  saveWindowStateNow()
  destroyTray()
  pythonBridge.stop()
    .catch((err) => console.error('[main] Python 停止失败:', err))
    .finally(() => {
      // 销毁提示窗口后退出
      if (overlay && !overlay.isDestroyed()) {
        overlay.destroy()
      }
      app.exit(0)
    })
})

// macOS: 点击 dock 图标时重新创建窗口
app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    const savedState = loadWindowState()
    chatWindow = createWindow('/', 'index.html', {
      x: savedState.chat.x,
      y: savedState.chat.y,
      width: savedState.chat.width,
      height: savedState.chat.height,
      title: 'RNA Companion',
      frame: false,
      icon: getAppIcon(),
      autoHideMenuBar: true,
    })
    bindMaximizeEvents(chatWindow)
  }
})
