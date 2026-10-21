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
export async function download(name: string, content: string, type = 'application/json'): Promise<string | null> {
  if (isTauri()) return invoke<string | null>('save_export', { name, content, format:type === 'text/csv' ? 'csv' : 'json' })
  if ('showSaveFilePicker' in window) {
    try {
      const extension = type === 'text/csv' ? '.csv' : '.json'
      const file = await (window as any).showSaveFilePicker({ suggestedName:name, types:[{description:extension.slice(1).toUpperCase(), accept:{[type]:[extension]}}] })
      const writer = await file.createWritable(); await writer.write(content); await writer.close()
      return file.name
    } catch (e: any) { if (e.name === 'AbortError') return null; throw e }
  }
  const url = URL.createObjectURL(new Blob([content], { type: `${type};charset=utf-8` }))
  const a = document.createElement('a'); a.href = url; a.download = name; a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
  return '浏览器下载目录（由浏览器设置决定）'
}
export async function quitApplication() {
  if (isTauri()) await invoke('quit_application')
}
