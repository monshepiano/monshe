const TOKEN_KEY = 'jarvis_token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}
export function setToken(t: string) {
  localStorage.setItem(TOKEN_KEY, t)
}

async function req(path: string, opts: RequestInit = {}) {
  const headers: Record<string, string> = { ...(opts.headers as any) }
  if (!(opts.body instanceof FormData)) headers['Content-Type'] = 'application/json'
  const t = getToken()
  if (t) headers['X-Jarvis-Token'] = t
  const r = await fetch(path, { ...opts, headers })
  if (r.status === 401) throw new Error('Нужен токен доступа')
  if (!r.ok) throw new Error(`${r.status}: ${(await r.text()).slice(0, 200)}`)
  const ct = r.headers.get('content-type') || ''
  return ct.includes('json') ? r.json() : r.text()
}

export const api = {
  health: () => req('/api/health'),
  status: () => req('/api/status'),
  config: () => req('/api/config'),
  saveConfig: (values: any) => req('/api/config', { method: 'POST', body: JSON.stringify({ values }) }),
  telegramTest: () => req('/api/telegram/test', { method: 'POST' }),

  chats: () => req('/api/chats'),
  newChat: () => req('/api/chats', { method: 'POST' }),
  delChat: (id: string) => req(`/api/chats/${id}`, { method: 'DELETE' }),
  messages: (id: string) => req(`/api/chats/${id}/messages`),
  chat: (text: string, chat_id?: string, attachments?: string[]) =>
    req('/api/chat', { method: 'POST', body: JSON.stringify({ text, chat_id, attachments }) }),

  tasks: () => req('/api/tasks'),
  newTask: (goal: string, title?: string, schedule = 'once', delay_minutes = 0) =>
    req('/api/tasks', { method: 'POST', body: JSON.stringify({ goal, title, schedule, delay_minutes }) }),
  runTask: (id: string) => req(`/api/tasks/${id}/run`, { method: 'POST' }),
  delTask: (id: string) => req(`/api/tasks/${id}`, { method: 'DELETE' }),

  approvals: () => req('/api/approvals'),
  decide: (approval_id: string, approved: boolean) =>
    req('/api/approvals', { method: 'POST', body: JSON.stringify({ approval_id, approved }) }),

  notifications: () => req('/api/notifications'),
  readNotifications: (ids?: string[]) =>
    req('/api/notifications/read', { method: 'POST', body: JSON.stringify({ ids }) }),

  memory: () => req('/api/memory'),
  addMemory: (key: string, value: string, kind = 'fact') =>
    req('/api/memory', { method: 'POST', body: JSON.stringify({ key, value, kind }) }),
  delMemory: (id: string) => req(`/api/memory/${id}`, { method: 'DELETE' }),

  files: () => req('/api/files'),
  fileUrl: (path: string) => {
    const t = getToken()
    return `/api/files/download?path=${encodeURIComponent(path)}${t ? `&token=${t}` : ''}`
  },
  upload: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return req('/api/upload', { method: 'POST', body: fd })
  },
  vision: (blob: Blob, question: string) => {
    const fd = new FormData()
    fd.append('file', blob, 'frame.jpg')
    fd.append('question', question)
    return req('/api/vision', { method: 'POST', body: fd })
  },
  streamUrl: () => {
    const t = getToken()
    return `/api/stream${t ? `?token=${t}` : ''}`
  },
}
