import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import { IcCam, IcFile, IcPlay, IcShield, IcTrash, IcX } from './icons'

export function Corners() {
  return <>
    <i className="corner tl" /><i className="corner tr" />
    <i className="corner bl" /><i className="corner br" />
  </>
}

export function statusClass(s: string) {
  return 'status-tag st-' + (s || 'pending')
}

/* ------------------------------------------------------------------ Задачи */
export function TasksPanel({ tasks, onRefresh }: any) {
  return (
    <>
      <div className="side-h"><IcPlay size={14} /> Фоновые задачи</div>
      <div className="scrolly">
        {!tasks.length && <div className="empty">
          Задач пока нет.<br /><br />
          Просто напишите Джарвису в чате, например:<br />
          <i>«каждое утро собирай сводку новостей по ИИ»</i><br /><br />
          он сам поставит задачу в фон, разобьёт её на шаги и выполнит.
        </div>}

        {tasks.map((t: any) => (
          <div className="card" key={t.id}>
            <div className="card-t">
              <span style={{ flex: 1 }}>{t.title}</span>
              <span className={statusClass(t.status)}>{t.status}</span>
            </div>
            <div className="card-s">{t.goal}</div>
            {t.schedule && t.schedule !== 'once' && (
              <div className="card-s" style={{ marginTop: 4, color: 'var(--cyan)' }}>⟳ {t.schedule}</div>
            )}
            {!!t.steps?.length && (
              <div className="steps">
                {t.steps.filter((s: any) => s.status !== 'stale').map((s: any) => (
                  <div className={`step ${s.status}`} key={s.id}>
                    <i className="step-dot" /><span>{s.title}</span>
                  </div>
                ))}
              </div>
            )}
            {t.result && <div className="card-s" style={{ marginTop: 8, paddingTop: 8,
              borderTop: '1px solid var(--line-soft)', whiteSpace: 'pre-wrap' }}>
              {t.result.slice(0, 600)}</div>}
            <div style={{ display: 'flex', gap: 6, marginTop: 9 }}>
              <button className="btn sm ghost" onClick={() => api.runTask(t.id).then(onRefresh)}>
                <IcPlay size={12} /> Перезапустить</button>
              <button className="btn sm danger" onClick={() => api.delTask(t.id).then(onRefresh)}>
                <IcTrash size={12} /></button>
            </div>
          </div>
        ))}
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ Песочница */
export function FilesPanel({ files, onRefresh }: any) {
  return (
    <>
      <div className="side-h" style={{ justifyContent: 'space-between' }}>
        <span><IcFile size={14} /> Песочница</span>
        <button className="btn sm ghost" onClick={onRefresh}>Обновить</button>
      </div>
      <div className="scrolly">
        {!files.length && <div className="empty">Файлы, которые создаст Джарвис,<br />появятся здесь.</div>}
        {files.map((f: any) => {
          const img = /\.(png|jpe?g|gif|webp)$/i.test(f.path)
          return (
            <div key={f.path}>
              <div className="file-row">
                <IcFile size={14} />
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {f.path}</span>
                <a href={api.fileUrl(f.path)} download>{(f.size / 1024).toFixed(1)} КБ ↓</a>
              </div>
              {img && <img className="art-img" src={api.fileUrl(f.path)} alt={f.path} />}
            </div>
          )
        })}
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ Память */
export function MemoryPanel({ memory, onRefresh }: any) {
  const [k, setK] = useState(''); const [v, setV] = useState('')
  const add = async () => {
    if (!k.trim() || !v.trim()) return
    await api.addMemory(k.trim(), v.trim(), 'profile'); setK(''); setV(''); onRefresh()
  }
  return (
    <>
      <div className="side-h">Память о вас</div>
      <div className="scrolly">
        <div className="card">
          <div className="field"><label>Что запомнить</label>
            <input value={k} onChange={e => setK(e.target.value)} placeholder="город" /></div>
          <div className="field" style={{ marginTop: 7 }}><label>Значение</label>
            <input value={v} onChange={e => setV(e.target.value)} placeholder="Москва" /></div>
          <button className="btn primary sm" style={{ marginTop: 9 }} onClick={add}>Запомнить</button>
        </div>
        {!memory.length && <div className="empty">Джарвис сам запоминает важное<br />из разговоров.</div>}
        {memory.map((m: any) => (
          <div className="card" key={m.id}>
            <div className="card-t"><span style={{ flex: 1 }}>{m.key}</span>
              <button className="btn sm ghost" onClick={() => api.delMemory(m.id).then(onRefresh)}>
                <IcX size={12} /></button></div>
            <div className="card-s">{m.value}</div>
            <div className="card-s" style={{ opacity: .6, marginTop: 3 }}>{m.kind}</div>
          </div>
        ))}
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ Уведомления */
export function NotifPanel({ items, onRead }: any) {
  return (
    <>
      <div className="side-h" style={{ justifyContent: 'space-between' }}>
        <span>Уведомления</span>
        <button className="btn sm ghost" onClick={onRead}>Прочитано</button>
      </div>
      <div className="scrolly">
        {!items.length && <div className="empty">
          Пока тихо.<br />Здесь появятся сообщения<br />о выполненных задачах.
        </div>}
        {items.map((n: any) => (
          <div className={`card notif ${n.level} ${n.read ? '' : 'unread'}`} key={n.id}>
            <div className="card-t">{n.title}</div>
            {n.body && <div className="card-s" style={{ whiteSpace: 'pre-wrap' }}>{n.body}</div>}
            <div className="card-s" style={{ opacity: .55, marginTop: 4 }}>
              {new Date(n.created * 1000).toLocaleString('ru-RU')}</div>
          </div>
        ))}
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ Санкции */
export function ApprovalsPanel({ approvals, onDecide }: any) {
  return (
    <>
      <div className="side-h"><IcShield size={14} /> Санкции</div>
      <div className="scrolly">
        {!approvals.length && <div className="empty">
          Опасных действий на подтверждении нет.<br /><br />
          Перед тем как что-то удалить, купить<br />или отправить от вашего имени,<br />
          Джарвис спросит разрешение здесь.
        </div>}
        {approvals.map((a: any) => (
          <div className="approve-box" key={a.id}>
            <div className="card-t" style={{ color: 'var(--gold)' }}>
              <IcShield size={14} /> Нужно ваше разрешение</div>
            <div className="card-s">{a.reason}</div>
            <div className="card-s mono-box">
              {a.tool}({JSON.stringify(a.args).slice(0, 260)})
            </div>
            <div style={{ display: 'flex', gap: 7, marginTop: 10 }}>
              <button className="btn primary sm" onClick={() => onDecide(a.id, true)}>Разрешить</button>
              <button className="btn danger sm" onClick={() => onDecide(a.id, false)}>Отклонить</button>
            </div>
          </div>
        ))}
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ Зрение */
export function VisionPanel({ onResult, onClose }: any) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [shot, setShot] = useState('')
  const [q, setQ] = useState('Что тут изображено? Если это товар или картина — назови и подскажи, где купить похожее.')

  useEffect(() => {
    let stream: MediaStream | undefined
    navigator.mediaDevices?.getUserMedia({ video: { facingMode: 'environment' } })
      .then(s => { stream = s; if (videoRef.current) videoRef.current.srcObject = s })
      .catch(e => setErr('Нет доступа к камере: ' + e.message))
    return () => { stream?.getTracks().forEach(t => t.stop()) }
  }, [])

  const shoot = () => {
    const v = videoRef.current
    if (!v || !v.videoWidth) return
    setBusy(true)
    const c = document.createElement('canvas')
    c.width = v.videoWidth; c.height = v.videoHeight
    c.getContext('2d')!.drawImage(v, 0, 0)
    setShot(c.toDataURL('image/jpeg', 0.8))
    c.toBlob(async blob => {
      try {
        const r = await api.vision(blob!, q)
        onResult(r.ok ? r.description : 'Не удалось распознать: ' + r.error)
      } catch (e: any) { onResult('Не удалось распознать: ' + e.message) }
      finally { setBusy(false) }
    }, 'image/jpeg', 0.9)
  }

  return (
    <>
      <div className="side-h" style={{ justifyContent: 'space-between' }}>
        <span><IcCam size={14} /> Зрение Джарвиса</span>
        <button className="btn sm ghost" onClick={onClose}><IcX size={12} /></button>
      </div>
      <div className="scrolly">
        {err
          ? <div className="empty">{err}<br /><br />Разрешите доступ к камере в браузере и откройте панель снова.</div>
          : <video ref={videoRef} className="cam" autoPlay playsInline muted />}
        {shot && <img className="art-img" src={shot} alt="кадр" />}
        <div className="field">
          <label>Что спросить о том, что видит камера</label>
          <textarea rows={3} value={q} onChange={e => setQ(e.target.value)} />
        </div>
        <button className="btn primary" disabled={busy || !!err} onClick={shoot}>
          <IcCam size={14} /> {busy ? 'Смотрю…' : 'Снять и распознать'}
        </button>
        <div className="hint" style={{ fontSize: 11, color: 'var(--text-dim)' }}>
          Ответ придёт прямо в чат — дальше можно попросить, например, найти такую же вещь в магазине.
        </div>
      </div>
    </>
  )
}
