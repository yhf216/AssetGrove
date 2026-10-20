import { invoke, isTauri } from '@tauri-apps/api/core'

let connection = { port: 18765, token: 'assetgrove-local-development' }
export async function connect() {
  if (!isTauri()) return
  for (let i = 0; i < 100; i++) {
    const value = await invoke<{ port: number; token: string } | null>('backend_connection')
    if (value) { connection = value; return }
    await new Promise(resolve => setTimeout(resolve, 200))
  }
  throw new Error('本地数据服务未能启动，请查看桌面日志')
}
export async function api(path: string, method = 'GET', body?: unknown) {
  const result = await fetch(`http://127.0.0.1:${connection.port}/api${path}`, {
    method, headers: { Authorization: `Bearer ${connection.token}`, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!result.ok) {
    const error = await result.json().catch(() => ({}))
    throw new Error(typeof error.detail === 'string' ? error.detail : '请求失败，请检查输入内容')
  }
  return result.json()
}
export function download(name: string, content: string, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([content], { type: `${type};charset=utf-8` }))
  const a = document.createElement('a'); a.href = url; a.download = name; a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
export async function quitApplication() {
  if (isTauri()) await invoke('quit_application')
}
