/**
 * 系统托盘。
 *
 * 职责：
 * - 创建托盘图标（assets/icon/icon.png）
 * - 托盘菜单：显示/隐藏、退出
 * - 双击托盘图标切换窗口显隐
 */
import { app, Tray, Menu, nativeImage, BrowserWindow } from 'electron'
import path from 'node:path'

let tray: Tray | null = null

/**
 * 创建系统托盘
 * @param windows 需要切换显隐的窗口列表（主界面）
 */
export function createTray(windows: BrowserWindow[]): Tray {
  // 图标路径：开发期从 assets/icon，生产期从 dist/icon
  const iconPath = app.isPackaged
    ? path.join(process.resourcesPath, 'icon', 'icon.png')
    : path.join(__dirname, '..', 'assets', 'icon', 'icon.png')

  const icon = nativeImage.createFromPath(iconPath)

  tray = new Tray(icon)
  tray.setToolTip('RNA Companion')

  // 托盘菜单
  const contextMenu = Menu.buildFromTemplate([
    {
      label: '显示/隐藏',
      click: () => toggleVisibility(windows),
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        app.quit()
      },
    },
  ])

  tray.setContextMenu(contextMenu)

  // 双击托盘图标 → 切换显隐
  tray.on('double-click', () => {
    toggleVisibility(windows)
  })

  return tray
}

/**
 * 切换窗口显隐
 */
function toggleVisibility(windows: BrowserWindow[]): void {
  // 如果任一窗口可见，则全部隐藏
  const anyVisible = windows.some(
    (w) => !w.isDestroyed() && w.isVisible()
  )

  if (anyVisible) {
    for (const w of windows) {
      if (!w.isDestroyed() && w.isVisible()) {
        w.hide()
      }
    }
  } else {
    for (const w of windows) {
      if (w.isDestroyed()) continue
      if (w.isMinimized()) w.restore()
      w.show()
      w.focus()
    }
  }
}

/** 销毁托盘 */
export function destroyTray(): void {
  if (tray) {
    tray.destroy()
    tray = null
  }
}
