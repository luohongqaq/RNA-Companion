const base = import.meta.env.DEV ? '' : 'http://127.0.0.1:8765'
export const endpoint = (path: string) => base + '/api/v1' + path
export const assetUrl = (path: string) => path.startsWith('/') ? base + path : path
export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(endpoint(path), {
    method,
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    throw new Error(typeof error.detail === 'string' ? error.detail : response.status === 409 ? '数据已更新，请重新加载后再保存。' : `请求失败（${response.status}）`)
  }
  return response.json()
}
export interface Page<T> { items: T[]; next_cursor: string | null }
export interface Character { character_id: string; name: string }
export interface Conversation {
  conversation_id: string; character_id: string; title: string | null
  state: 'active' | 'archived'; updated_at: string
  current_session_id: string | null
}
export interface Message {
  message_id: string; content: string; role: 'user' | 'character'; created_at: string
  japanese?: string | null
  visual?: { pose_name: { pose_id: string }; expression_name: { expression_id: string; expression_name: string } } | null
}
export interface Job { job_id: string; status: string; error: { message: string; retryable: boolean } | null }
export interface Appearance { outfit_id: string; pose_id: string; expression_id: string }
export interface Sprite extends Appearance { url: string }
export interface Assets {
  default_background_id: string; default_expression: Appearance
  backgrounds: { id: string; name: string; url: string }[]
  outfits: Record<string, { name: string; default_pose_id: string; default_expression_id: string }>
  poses: Record<string, string>; expressions: Record<string, string>; assets: Sprite[]
}
