/**
 * Python 子进程管理 — 启动 / 健康轮询 / 优雅退出
 *
 * 启动新三层后端：spawn backend/server.py，轮询 /health 直到 200
 * 退出时 SIGTERM → 等 5s → Windows taskkill /T /F
 */
import { spawn, type ChildProcess } from 'node:child_process'
import path from 'node:path'
import http from 'node:http'
import fs from 'node:fs'
import { app } from 'electron'

const BACKEND_HOST = '127.0.0.1'
const BACKEND_PORT = 8765
const HEALTH_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}/health`
const HEALTH_TIMEOUT_MS = 60000
const HEALTH_INTERVAL_MS = 500
const STOP_FORCE_KILL_MS = 3000

export function getBackendDirectory(): string {
  // 打包模式：便携版布局是 exe 同级的 backend/；RNA_BACKEND_DIR 可显式覆盖。
  if (app.isPackaged) {
    if (process.env.RNA_BACKEND_DIR?.trim()) return path.resolve(process.env.RNA_BACKEND_DIR)
    const exeDirectory = path.dirname(app.getPath('exe'))
    for (const candidate of [path.resolve(exeDirectory, 'backend'), path.resolve(exeDirectory, '..', 'backend')]) {
      if (fs.existsSync(candidate)) return candidate
    }
  }
  // 开发模式：main.ts 编译产物位于 frontend/dist-electron，后端在仓库根的 backend/。
  return path.resolve(__dirname, '..', '..', 'backend')
}

export class PythonBridge {
  private process: ChildProcess | null = null

  /** 启动 Python 后端并等待健康检查通过 */
  async start(): Promise<void> {
    const backendDirectory = getBackendDirectory()
    const serverPath = path.join(backendDirectory, 'server.py')

    // 优先使用项目虚拟环境；不存在时回退到系统 Python。
    const venvPython = path.join(
      backendDirectory, '.venv', 'Scripts', 'python.exe'
    )
    const pythonExe = fs.existsSync(venvPython) ? venvPython : 'python'

    console.log(`[python-bridge] Spawning: ${pythonExe} "${serverPath}"`)
    this.process = spawn(pythonExe, [serverPath], {
      // cwd 固定为 backend，运行数据统一解析到 backend/。
      cwd: backendDirectory,
      stdio: ['pipe', 'pipe', 'pipe'],
      // GUI 进程无可继承控制台，windowsHide 避免后端弹出新黑窗；日志仍走管道。
      windowsHide: true,
      env: {
        ...process.env,
        // 强制 Python 全局使用 UTF-8（stdout/stderr/文件系统），
        // 避免 Windows 默认 GBK 编码导致中文日志在 Electron 管道中乱码
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
      },
    })

    this.process.stdout?.on('data', (data: Buffer) => {
      const text = data.toString().trim()
      if (text) console.log(`[python] ${text}`)
    })

    this.process.stderr?.on('data', (data: Buffer) => {
      const text = data.toString().trim()
      if (text) console.error(`[python] ${text}`)
    })

    this.process.on('exit', (code, signal) => {
      console.log(`[python-bridge] Process exited: code=${code} signal=${signal}`)
      this.process = null
    })

    await this.waitForHealth()
  }

  /** 轮询 /health 直到返回 200 或超时 */
  private waitForHealth(): Promise<void> {
    const startTime = Date.now()

    const check = (): Promise<void> => {
      if (Date.now() - startTime > HEALTH_TIMEOUT_MS) {
        return Promise.reject(new Error('Python backend health check timeout'))
      }

      return new Promise<void>((resolve, reject) => {
        const req = http.get(HEALTH_URL, (res) => {
          res.resume()
          if (res.statusCode === 200) {
            resolve()
          } else {
            setTimeout(() => check().then(resolve, reject), HEALTH_INTERVAL_MS)
          }
        })

        req.on('error', () => {
          setTimeout(() => check().then(resolve, reject), HEALTH_INTERVAL_MS)
        })

        req.setTimeout(2000, () => {
          req.destroy(new Error('Backend health request timed out'))
        })
      })
    }

    return check()
  }

  /** 停止 Python 后端：先调用 /shutdown 保存记忆，再 SIGTERM，5 秒后强制 kill */
  async stop(): Promise<void> {
    if (!this.process) return

    // 1. 先调用 /shutdown 端点，让后端优雅保存 L3 记忆
    await this.requestShutdown()

    const proc = this.process
    const pid = proc.pid

    return new Promise<void>((resolve) => {
      let resolved = false

      const done = () => {
        if (!resolved) {
          resolved = true
          resolve()
        }
      }

      const forceTimer = setTimeout(() => {
        if (!resolved && pid) {
          console.log('[python-bridge] Force killing...')
          try {
            // Windows: taskkill /T /F 递归终止进程树
            spawn('taskkill', ['/PID', String(pid), '/T', '/F'], {
              stdio: 'ignore',
            })
          } catch {
            try {
              proc.kill('SIGKILL')
            } catch {
              /* 忽略 */
            }
          }
        }
        done()
      }, STOP_FORCE_KILL_MS)

      proc.once('exit', () => {
        clearTimeout(forceTimer)
        done()
      })

      try {
        proc.kill('SIGTERM')
      } catch {
        clearTimeout(forceTimer)
        done()
      }
    })
  }

  /** 调用后端 /shutdown 端点，等待记忆系统保存完成 */
  private requestShutdown(): Promise<void> {
    return new Promise<void>((resolve) => {
      const data = JSON.stringify({})

      const req = http.request(
        {
          hostname: BACKEND_HOST,
          port: BACKEND_PORT,
          path: '/shutdown',
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(data),
          },
        },
        (res) => {
          // 消费响应体，无论结果如何都继续关闭流程
          res.resume()
          res.on('end', () => {
            console.log('[python-bridge] /shutdown 响应:', res.statusCode)
            resolve()
          })
        },
      )

      req.on('error', (err) => {
        // 后端可能已经不可达，忽略错误继续关闭
        console.log('[python-bridge] /shutdown 请求失败（可忽略）:', err.message)
        resolve()
      })

      // 最多等待 5 秒：shutdown 仅做索引落盘 + 关闭数据库，无 LLM 调用
      req.setTimeout(5000, () => {
        req.destroy()
        console.log('[python-bridge] /shutdown 超时，继续关闭')
        resolve()
      })

      req.write(data)
      req.end()
    })
  }
}


