/** 立绘可见框分析 — 场景摆位与手记贴纸相框共用。
 *  各角色画布比例/留白不同，统一按"人物可见框"对齐而非画布。 */

export interface SpriteBox { top: number; bottom: number; left: number; right: number; centerX: number }

/** 立绘摆位所需的完整指标：可见框 + 画布宽高比。 */
export interface SpriteMetrics { box: SpriteBox; ratio: number }

const cache = new Map<string, SpriteBox>()
const metricsCache = new Map<string, SpriteMetrics>()

export const FALLBACK_BOX: SpriteBox = { top: .08, bottom: 1, left: 0, right: 1, centerX: .5 }

export function getCachedSpriteBox(url: string): SpriteBox | undefined {
  return cache.get(url)
}

/** 同步取缓存指标 — 切换立绘时先摆到正确位置，避免闪跳。 */
export function getCachedSpriteMetrics(url: string): SpriteMetrics | undefined {
  return metricsCache.get(url)
}

interface ScaledSprite { source: CanvasImageSource; width: number; height: number; ratio: number; release: () => void }

/** 解码时直接缩到 96px 宽：避免解码整张大图，缓存/切换明显更快。 */
async function loadScaledSprite(url: string): Promise<ScaledSprite> {
  try {
    const response = await fetch(url)
    if (!response.ok) throw new Error('无法读取立绘')
    const bitmap = await createImageBitmap(await response.blob(), { resizeWidth: 96, resizeQuality: 'low' })
    return { source: bitmap, width: bitmap.width, height: bitmap.height, ratio: bitmap.width / bitmap.height, release: () => bitmap.close() }
  } catch {
    const image = new Image()
    image.crossOrigin = 'anonymous'
    image.src = url
    await image.decode()
    const width = 96
    const height = Math.max(24, Math.round(96 * image.naturalHeight / image.naturalWidth))
    return { source: image, width, height, ratio: image.naturalWidth / image.naturalHeight, release: () => undefined }
  }
}

export async function analyzeSpriteBox(url: string): Promise<SpriteBox> {
  const cached = cache.get(url)
  if (cached) return cached
  const sprite = await loadScaledSprite(url)
  const canvas = document.createElement('canvas')
  canvas.width = sprite.width
  canvas.height = sprite.height
  const context = canvas.getContext('2d', { willReadFrequently: true })
  if (!context) throw new Error('无法分析立绘')
  context.drawImage(sprite.source, 0, 0, sprite.width, sprite.height)
  sprite.release()
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data
  let left = 1, right = 0, top = 1, bottom = 0
  for (let index = 0; index < pixels.length; index += 4) {
    if (pixels[index + 3] <= 16) continue
    const pixel = index / 4
    const x = (pixel % canvas.width) / canvas.width
    const y = Math.floor(pixel / canvas.width) / canvas.height
    if (x < left) left = x
    if (x > right) right = x
    if (y < top) top = y
    if (y > bottom) bottom = y
  }
  if (right <= left || bottom <= top) throw new Error('立绘无可见像素')
  const box: SpriteBox = { top, bottom, left, right, centerX: (left + right) / 2 }
  cache.set(url, box)
  metricsCache.set(url, { box, ratio: sprite.ratio })
  return box
}

const idle = () => new Promise<void>(resolve => {
  const timer = setTimeout(resolve, 120)
  if (typeof window !== 'undefined' && window.requestIdleCallback) {
    window.requestIdleCallback(() => { clearTimeout(timer); resolve() }, { timeout: 500 })
  }
})

/** 预计算立绘指标 — 切换角色/表情时直接命中缓存，消除卡顿与位置闪跳。 */
export async function prefetchSpriteMetrics(urls: Iterable<string>, onProgress?: () => void, waitIdle = true): Promise<void> {
  const queue = [...new Set(urls)].filter(url => !metricsCache.has(url))
  let next = 0
  const worker = async () => {
    while (next < queue.length) {
      const url = queue[next++]
      if (waitIdle) await idle()
      try { await analyzeSpriteBox(url) } catch { /* 资源不可用时跳过 */ }
      onProgress?.()
    }
  }
  await Promise.all(Array.from({ length: Math.min(3, queue.length) }, () => worker()))
}

const signatureCache = new Map<string, number[]>()

/** 可见框归一后的灰度签名 — 用于折叠"同一套服装、不同动作"导出的重复立绘。 */
export async function spriteSignature(url: string): Promise<number[]> {
  const cached = signatureCache.get(url)
  if (cached) return cached
  const image = new Image()
  image.crossOrigin = 'anonymous'
  image.src = url
  await image.decode()
  const scan = document.createElement('canvas')
  scan.width = 96
  scan.height = Math.max(24, Math.round(96 * image.naturalHeight / image.naturalWidth))
  const scanContext = scan.getContext('2d', { willReadFrequently: true })
  if (!scanContext) throw new Error('无法分析立绘')
  scanContext.drawImage(image, 0, 0, scan.width, scan.height)
  const pixels = scanContext.getImageData(0, 0, scan.width, scan.height).data
  let left = 1, right = 0, top = 1, bottom = 0
  for (let index = 0; index < pixels.length; index += 4) {
    if (pixels[index + 3] <= 16) continue
    const pixel = index / 4
    const x = (pixel % scan.width) / scan.width
    const y = Math.floor(pixel / scan.width) / scan.height
    if (x < left) left = x
    if (x > right) right = x
    if (y < top) top = y
    if (y > bottom) bottom = y
  }
  if (right <= left || bottom <= top) throw new Error('立绘无可见像素')
  const width = 24, height = 40
  const crop = document.createElement('canvas')
  crop.width = width
  crop.height = height
  const cropContext = crop.getContext('2d', { willReadFrequently: true })
  if (!cropContext) throw new Error('无法分析立绘')
  cropContext.drawImage(
    image,
    left * image.naturalWidth, top * image.naturalHeight,
    (right - left) * image.naturalWidth, (bottom - top) * image.naturalHeight,
    0, 0, width, height,
  )
  const data = cropContext.getImageData(0, 0, width, height).data
  const signature: number[] = []
  for (let index = 0; index < data.length; index += 4) {
    signature.push(data[index] * .3 + data[index + 1] * .59 + data[index + 2] * .11)
  }
  signatureCache.set(url, signature)
  return signature
}
