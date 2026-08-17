import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import {
  IcBell, IcBrain, IcCam, IcClip, IcFile, IcGear, IcMenu, IcMic, IcSend, IcTask, IcTerm, IcX,
} from './icons'
import {
  Corners, FilesPanel, MemoryPanel, NotifPanel, TasksPanel, VisionPanel,
} from './panels'
import { Settings } from './settings'

type Trace = { text: string; cls: string; key: number; kind: 'thought' | 'term' }
type Msg = { role: string; content: string; id?: string; trace?: Trace[] }

const TOOL_LABEL: Record<string, string> = {
  web_search: 'Ищу в интернете', open_page: 'Читаю страницу', browser_act: 'Работаю в браузере',
  write_file: 'Пишу файл', read_file: 'Читаю файл', list_files: 'Смотрю песочницу',
  delete_file: 'Удаляю файл', make_zip: 'Собираю архив', shell: 'Выполняю команду',
  run_python: 'Считаю на Python', generate_image: 'Рисую изображение', look: 'Разглядываю картинку',
  send_telegram: 'Пишу в телеграм', send_file_telegram: 'Отправляю файл в телеграм',
  remember: 'Запоминаю', recall: 'Вспоминаю', schedule_task: 'Ставлю задачу в фон',
  notify: 'Отправляю уведомление', now: 'Смотрю на часы',
}

export default function App() {
  const [messages, setMessages] = useState<Msg[]>([])
  const [chatId, setChatId] = useState<string>('')
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [trace, setTrace] = useState<Trace[]>([])
  const [showTerm, setShowTerm] = useState(false)
  const [tasks, setTasks] = useState<any[]>([])
  const [notifs, setNotifs] = useState<any[]>([])
  const [approvals, setApprovals] = useState<any[]>([])
  const [files, setFiles] = useState<any[]>([])
  const [memory, setMemory] = useState<any[]>([])
  const [status, setStatus] = useState<any>({})
  const [side, setSide] = useState<string>('')
  const [showSettings, setShowSettings] = useState(false)
  const [toasts, setToasts] = useState<any[]>([])
  const [attachments, setAttachments] = useState<string[]>([])
  const [listening, setListening] = useState(false)
  const [connected, setConnected] = useState(false)

  const streamRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const recogRef = useRef<any>(null)
  const keyRef = useRef(0)
  const traceRef = useRef<Trace[]>([])

  const toast = useCallback((title: string, body = '') => {
    const id = Date.now() + Math.random()
    setToasts(t => [...t, { id, title, body }])
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 6000)
  }, [])

  /* мысли и терминал живут одним потоком прямо в ответе Джарвиса */
  const push = useCallback((kind: 'thought' | 'term', text: string, cls = '') => {
    const item: Trace = { kind, text, cls, key: keyRef.current++ }
    traceRef.current = [...traceRef.current.slice(-220), item]
    setTrace(traceRef.current)
  }, [])

  const markLast = useCallback((cls: string) => {
    const arr = traceRef.current.slice()
    for (let i = arr.length - 1; i >= 0; i--) {
      if (arr[i].kind === 'thought') { arr[i] = { ...arr[i], cls }; break }
    }
    traceRef.current = arr
    setTrace(arr)
  }, [])

  const refreshAll = useCallback(() => {
    api.tasks().then(r => setTasks(r.tasks)).catch(() => {})
    api.notifications().then(r => setNotifs(r.notifications)).catch(() => {})
    api.approvals().then(r => setApprovals(r.approvals)).catch(() => {})
    api.files().then(r => setFiles(r.files || [])).catch(() => {})
    api.memory().then(r => setMemory(r.memory)).catch(() => {})
    api.status().then(setStatus).catch(() => {})
  }, [])

  /* ---------------------------------------------------------- инициализация */
  useEffect(() => {
    api.chats().then(async r => {
      const id = r.chats?.[0]?.id || (await api.newChat()).chat_id
      setChatId(id)
      const m = await api.messages(id)
      setMessages(m.messages.map((x: any) => ({ role: x.role, content: x.content, id: x.id })))
    }).catch(() => {})
    refreshAll()
    const iv = setInterval(refreshAll, 15000)
    return () => clearInterval(iv)
  }, [refreshAll])

  /* ---------------------------------------------------------- поток событий */
  useEffect(() => {
    const es = new EventSource(api.streamUrl())
    es.onopen = () => setConnected(true)
    es.onerror = () => setConnected(false)
    es.onmessage = e => {
      let ev: any
      try { ev = JSON.parse(e.data) } catch { return }
      switch (ev.kind) {
        case 'router':
          push('term', `[модель] ${ev.provider}/${ev.model} · сложность: ${ev.tier}`, 't-dim')
          break
        case 'router_fallback':
          push('term', `[модель] ${ev.provider} не ответил: ${ev.error}`, 't-err')
          break
        case 'thinking':
          push('thought', 'Думаю…', 'think')
          break
        case 'tool_start': {
          const label = TOOL_LABEL[ev.tool] || ev.tool
          const arg = ev.args?.query || ev.args?.url || ev.args?.path || ev.args?.title ||
            ev.args?.command || ev.args?.prompt || ''
          push('thought', `${label}${arg ? ': ' + String(arg).slice(0, 80) : ''}`)
          push('term', `$ ${ev.tool} ${JSON.stringify(ev.args).slice(0, 200)}`, 't-in')
          break
        }
        case 'tool_end': {
          const ok = ev.result?.ok !== false
          push('term', `  → ${JSON.stringify(ev.result).slice(0, 320)}`, ok ? 't-ok' : 't-err')
          markLast(ok ? 'ok' : 'fail')
          if (ev.tool === 'write_file' || ev.tool === 'generate_image') {
            api.files().then(r => setFiles(r.files || [])).catch(() => {})
          }
          break
        }
        case 'approval_request':
          setApprovals(a => [...a.filter(x => x.id !== ev.approval_id),
            { id: ev.approval_id, tool: ev.tool, args: ev.args, reason: ev.reason }])
          setSide('notif')
          toast('Нужно подтверждение', `${ev.tool}: ${ev.reason}`)
          push('thought', `Жду вашего разрешения: ${TOOL_LABEL[ev.tool] || ev.tool}`, 'wait')
          break
        case 'approval_resolved':
          setApprovals(a => a.filter(x => x.id !== ev.approval_id))
          break
        case 'notification':
          setNotifs(n => [{ ...ev, id: ev.id || String(keyRef.current++) }, ...n])
          setSide('notif')
          toast(ev.title, ev.body?.slice(0, 140))
          break
        case 'task_queued':
        case 'task_scheduled':
          push('thought', `Поставил задачу в фон: ${ev.title || ''}`)
          refreshAll(); break
        case 'task_start':
          push('term', `[задача] старт: ${ev.title}`, 't-in'); refreshAll(); break
        case 'task_plan':
          push('term', `[план] ${ev.steps?.join(' → ')}`, 't-dim'); refreshAll(); break
        case 'step_start':
          push('term', `  ▸ шаг ${ev.idx + 1}: ${ev.title}`, ''); break
        case 'step_end':
          push('term', `  ✓ шаг ${ev.idx + 1} готов`, 't-ok'); refreshAll(); break
        case 'task_done':
          push('term', '[задача] выполнена', 't-ok'); refreshAll(); break
        case 'task_failed':
          push('term', `[задача] ошибка: ${ev.error}`, 't-err'); refreshAll(); break
        case 'memory':
          push('term', `[память] ${ev.key} = ${ev.value}`, 't-dim'); break
        case 'telegram_in':
          push('term', `[telegram] ${ev.text}`, 't-in'); break
      }
    }
    return () => es.close()
  }, [push, markLast, refreshAll, toast])

  useEffect(() => {
    streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, trace])

  /* ---------------------------------------------------------- отправка */
  const send = async (textOverride?: string) => {
    const text = (textOverride ?? input).trim()
    if (!text || busy) return
    setInput('')
    setBusy(true)
    traceRef.current = []; setTrace([])
    setMessages(m => [...m, { role: 'user', content: text }])
    const atts = attachments.slice()
    setAttachments([])
    try {
      const r = await api.chat(text, chatId, atts)
      setChatId(r.chat_id)
      setMessages(m => [...m, { role: 'assistant', content: r.answer, trace: traceRef.current }])
    } catch (e: any) {
      setMessages(m => [...m, { role: 'assistant', content: '⚠️ ' + e.message, trace: traceRef.current }])
    } finally {
      setBusy(false)
      traceRef.current = []; setTrace([])
      refreshAll()
    }
  }

  /* ---------------------------------------------------------- голос */
  const toggleVoice = () => {
    const SR = (window as any).webkitSpeechRecognition || (window as any).SpeechRecognition
    if (!SR) { toast('Голос недоступен', 'Откройте Джарвиса в Safari или Chrome'); return }
    if (listening) { recogRef.current?.stop(); setListening(false); return }
    const r = new SR()
    r.lang = 'ru-RU'; r.interimResults = true; r.continuous = false
    r.onresult = (e: any) => {
      const t = Array.from(e.results).map((x: any) => x[0].transcript).join('')
      setInput(t)
      if (e.results[e.results.length - 1].isFinal) { setListening(false); send(t) }
    }
    r.onerror = () => setListening(false)
    r.onend = () => setListening(false)
    r.start(); recogRef.current = r; setListening(true)
  }

  /* ---------------------------------------------------------- файлы */
  const onFiles = async (fl: FileList | null) => {
    if (!fl?.length) return
    for (const f of Array.from(fl)) {
      try {
        const r = await api.upload(f)
        setAttachments(a => [...a, r.path])
        toast('Файл загружен', r.path)
      } catch (e: any) { toast('Ошибка загрузки', e.message) }
    }
    api.files().then(r => setFiles(r.files || [])).catch(() => {})
  }

  const decide = async (id: string, ok: boolean) => {
    await api.decide(id, ok)
    setApprovals(a => a.filter(x => x.id !== id))
    push('term', ok ? '[✓] действие разрешено' : '[×] действие отклонено', ok ? 't-ok' : 't-err')
  }

  const unread = notifs.filter(n => !n.read).length + approvals.length
  const online = !!status.primary
  const liveThoughts = trace.filter(t => t.kind === 'thought')
  const liveTerm = trace.filter(t => t.kind === 'term')

  const SIDE_TABS: any[] = [
    ['notif', <IcBell size={18} />, 'Уведомления'],
    ['tasks', <IcTask size={18} />, 'Фоновые задачи'],
    ['files', <IcFile size={18} />, 'Песочница'],
    ['memory', <IcBrain size={18} />, 'Память'],
    ['vision', <IcCam size={18} />, 'Зрение'],
  ]

  return (
    <>
      <div className="backdrop">
        <div className="grid-lines" /><div className="scanline" />
      </div>

      <div className="app">
        {/* ---------------------------------------------------- шапка */}
        <div className="topbar">
          <div className="brand">
            <span className="dot live" /><b>J.A.R.V.I.S.</b>
          </div>
          <span className={`chip ${online ? 'on' : 'warn'} hide-sm`}>
            <span className={`dot ${online ? 'live' : 'off'}`} />
            {online ? status.primary : 'модель не подключена'}
          </span>
          <span className={`chip ${connected ? 'on' : ''} hide-sm`}>
            <span className={`dot ${connected ? 'live' : 'off'}`} /> поток
          </span>
          {status.telegram && <span className="chip on hide-sm">telegram</span>}
          {busy && <span className="chip warn"><i className="thinker" /> работаю</span>}
          <span className="spacer" />
          <div className={`iconbtn ${side === 'notif' ? 'active' : ''}`} title="Уведомления"
            onClick={() => setSide(s => (s === 'notif' ? '' : 'notif'))}>
            <IcBell size={17} />
            {unread > 0 && <span className="badge">{unread}</span>}
          </div>
          <div className="iconbtn" title="Настройки" onClick={() => setShowSettings(true)}>
            <IcGear size={17} /></div>
          <div className={`iconbtn ${side ? 'active' : ''}`} title="Боковое меню"
            onClick={() => setSide(s => (s ? '' : 'notif'))}><IcMenu size={17} /></div>
        </div>

        <div className="body">
          {/* -------------------------------------------------- центр */}
          <div className="center">
            <div className="stream" ref={streamRef}>
              {!messages.length && (
                <div>
                  <div className="reactor">
                    <div className={`core ${busy ? 'busy' : ''}`}>
                      <i className="ring r1" /><i className="ring r2 seg" /><i className="ring r3" />
                      <div className="glow" />
                    </div>
                  </div>
                  <div className="hero-title">JARVIS ONLINE</div>
                  <div className="hero-sub">
                    Спросите что угодно, покажите картинку через камеру<br />
                    или дайте цель — разобью на шаги и выполню в фоне.
                  </div>
                  {!online && (
                    <div className="hero-sub" style={{ color: 'var(--gold)', marginTop: 14 }}>
                      Сначала вставьте бесплатный ключ GigaChat в «Настройки → Модели».
                    </div>
                  )}
                </div>
              )}

              {messages.map((m, i) => (
                <div className={`msg ${m.role === 'user' ? 'me' : ''}`} key={m.id || i}>
                  <div className="avatar">{m.role === 'user' ? 'ВЫ' : 'J'}</div>
                  <div className="bubble-wrap">
                    {!!m.trace?.length && <TraceBlock trace={m.trace} done />}
                    <div className="bubble">{m.content}</div>
                  </div>
                </div>
              ))}

              {busy && (
                <div className="msg">
                  <div className="avatar">J</div>
                  <div className="bubble-wrap">
                    <div className="bubble live">
                      <div className="live-head">
                        <i className="thinker" />
                        <span>{liveThoughts.length ? liveThoughts[liveThoughts.length - 1].text : 'Думаю…'}</span>
                        {!!liveTerm.length && (
                          <button className="mini-btn" onClick={() => setShowTerm(v => !v)}>
                            <IcTerm size={12} /> {showTerm ? 'скрыть терминал' : 'терминал'}
                          </button>
                        )}
                      </div>
                      {liveThoughts.length > 1 && (
                        <div className="thoughts">
                          {liveThoughts.slice(0, -1).map(t => (
                            <div className={`thought ${t.cls}`} key={t.key}>
                              <span className="tool">▸</span><span>{t.text}</span>
                            </div>
                          ))}
                        </div>
                      )}
                      {showTerm && !!liveTerm.length && (
                        <div className="term inline">
                          {liveTerm.map(l => <div className={`ln ${l.cls}`} key={l.key}>{l.text}</div>)}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* ------------------------------------------------ ввод */}
            <div className="composer">
              {!!attachments.length && (
                <div className="attach-row">
                  {attachments.map(a => <span className="attach" key={a}>📎 {a}</span>)}
                </div>
              )}
              <textarea rows={1} value={input} placeholder="Спросите Джарвиса…"
                onChange={e => {
                  setInput(e.target.value)
                  e.target.style.height = 'auto'
                  e.target.style.height = Math.min(e.target.scrollHeight, 180) + 'px'
                }}
                onKeyDown={e => {
                  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
                }} />
              <div className="composer-row">
                <div className="iconbtn" title="Прикрепить файл" onClick={() => fileRef.current?.click()}>
                  <IcClip size={16} /></div>
                <div className={`iconbtn ${listening ? 'rec' : ''}`} title="Голос" onClick={toggleVoice}>
                  <IcMic size={16} /></div>
                <div className={`iconbtn ${side === 'vision' ? 'active' : ''}`} title="Камера"
                  onClick={() => setSide(s => (s === 'vision' ? '' : 'vision'))}>
                  <IcCam size={16} /></div>
                <button className="send" disabled={busy || !input.trim()} onClick={() => send()}>
                  <IcSend size={14} /> {busy ? 'Работаю…' : 'Отправить'}
                </button>
              </div>
              <input type="file" ref={fileRef} hidden multiple
                onChange={e => { onFiles(e.target.files); e.target.value = '' }} />
            </div>
          </div>

          {/* -------------------------------------------------- боковая панель */}
          {side && (
            <div className="side">
              <Corners />
              <div className="side-tabs">
                {SIDE_TABS.map(([id, icon, title]) => (
                  <div key={id} title={title}
                    className={`iconbtn ${side === id ? 'active' : ''}`}
                    onClick={() => setSide(id)}>
                    {icon}
                    {id === 'notif' && unread > 0 && <span className="badge">{unread}</span>}
                  </div>
                ))}
                <span style={{ flex: 1 }} />
                <div className="iconbtn" title="Закрыть" onClick={() => setSide('')}><IcX size={16} /></div>
              </div>
              <div className="side-body">
                {side === 'notif' && <NotifPanel items={notifs} approvals={approvals} onDecide={decide}
                  onRead={() => api.readNotifications().then(() =>
                    setNotifs(n => n.map(x => ({ ...x, read: 1 }))))} />}
                {side === 'tasks' && <TasksPanel tasks={tasks} onRefresh={refreshAll} />}
                {side === 'files' && <FilesPanel files={files}
                  onRefresh={() => api.files().then(r => setFiles(r.files || []))} />}
                {side === 'memory' && <MemoryPanel memory={memory}
                  onRefresh={() => api.memory().then(r => setMemory(r.memory))} />}
                {side === 'vision' && <VisionPanel onClose={() => setSide('')}
                  onResult={(text: string) =>
                    setMessages(m => [...m, { role: 'assistant', content: '👁 ' + text }])} />}
              </div>
            </div>
          )}
        </div>
      </div>

      {showSettings && <Settings onClose={() => setShowSettings(false)} onSaved={refreshAll} />}

      <div className="toasts">
        {toasts.map(t => (
          <div className="toast" key={t.id} onClick={() => setSide('notif')}>
            <b>{t.title}</b>{t.body}</div>
        ))}
      </div>
    </>
  )
}

/* ------------------------------------------------ свёрнутый след прошлого ответа */
function TraceBlock({ trace, done }: { trace: Trace[]; done?: boolean }) {
  const [open, setOpen] = useState(false)
  const thoughts = trace.filter(t => t.kind === 'thought')
  const term = trace.filter(t => t.kind === 'term')
  if (!thoughts.length && !term.length) return null
  return (
    <div className={`trace ${open ? 'open' : ''}`}>
      <button className="trace-head" onClick={() => setOpen(v => !v)}>
        <span className="tool">▸</span>
        <span>{done ? `Ход мыслей · ${thoughts.length} шаг(ов)` : 'Ход мыслей'}</span>
        <span className="trace-caret">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <div className="trace-body">
          {thoughts.map(t => (
            <div className={`thought ${t.cls}`} key={t.key}>
              <span className="tool">▸</span><span>{t.text}</span>
            </div>
          ))}
          {!!term.length && (
            <div className="term inline">
              {term.map(l => <div className={`ln ${l.cls}`} key={l.key}>{l.text}</div>)}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
