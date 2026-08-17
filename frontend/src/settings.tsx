import { useEffect, useState } from 'react'
import { api, setToken } from './api'
import { Corners } from './panels'
import { IcTg, IcX } from './icons'

const TABS = [
  ['models', 'Модели'],
  ['telegram', 'Телеграм'],
  ['safety', 'Безопасность'],
  ['me', 'Обо мне'],
  ['server', 'Сервер'],
] as const

export function Settings({ onClose, onSaved }: any) {
  const [cfg, setCfg] = useState<any>(null)
  const [tab, setTab] = useState<string>('models')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => { api.config().then(setCfg) }, [])
  if (!cfg) return null

  const set = (k: string, v: any) => setCfg({ ...cfg, [k]: v })

  const save = async () => {
    setBusy(true)
    try {
      const out: any = {}
      for (const k of Object.keys(cfg)) {
        if (k.endsWith('_set')) continue
        const v = cfg[k]
        if (typeof v === 'string' && v.startsWith('•')) continue
        out[k] = v
      }
      await api.saveConfig(out)
      if (out.auth_token) setToken(out.auth_token)
      setMsg('Сохранено ✓')
      onSaved?.()
      setTimeout(() => setMsg(''), 2500)
    } catch (e: any) { setMsg('Ошибка: ' + e.message) }
    finally { setBusy(false) }
  }

  const testTg = async () => {
    setBusy(true); setMsg('Проверяю…')
    try {
      await api.saveConfig({
        telegram_bot_token: cfg.telegram_bot_token?.startsWith('•') ? undefined : cfg.telegram_bot_token,
        telegram_chat_id: cfg.telegram_chat_id,
      })
      const r = await api.telegramTest()
      setMsg(r.sent ? `Отправлено ✓ бот @${r.bot}` : `Не вышло: ${r.error || 'проверьте токен и chat_id'}`)
    } catch (e: any) { setMsg('Ошибка: ' + e.message) }
    finally { setBusy(false) }
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="panel modal" onClick={e => e.stopPropagation()}>
        <Corners />
        <div className="panel-h"><b>Настройки Джарвиса</b><span style={{ flex: 1 }} />
          <button className="btn sm ghost" onClick={onClose}><IcX size={13} /></button></div>

        <div style={{ padding: '12px 16px 0' }}>
          <div className="tabs">
            {TABS.map(([id, label]) => (
              <div key={id} className={`tab ${tab === id ? 'on' : ''}`} onClick={() => setTab(id)}>
                {label}</div>
            ))}
          </div>
        </div>

        <div className="modal-b">
          {tab === 'models' && <>
            <div className="field">
              <label>Ключ GigaChat (Сбер) — основной, работает в РФ без VPN</label>
              <input value={cfg.gigachat_credentials || ''} placeholder="Authorization key из личного кабинета"
                onChange={e => set('gigachat_credentials', e.target.value)} />
              <div className="hint">
                Где взять бесплатно: developers.sber.ru → GigaChat API → «Получить API-ключ» →
                вход по Сбер ID → создать проект «Для физических лиц» → скопировать <b>Authorization key</b>.
                Даётся ~1&nbsp;млн бесплатных токенов. Сюда вставляется длинная строка вида
                <code> ZDk4M...==</code>
              </div>
            </div>
            <div className="field">
              <label>Область доступа</label>
              <select value={cfg.gigachat_scope} onChange={e => set('gigachat_scope', e.target.value)}>
                <option value="GIGACHAT_API_PERS">Физическое лицо (бесплатно)</option>
                <option value="GIGACHAT_API_B2B">ИП / юрлицо (пакеты)</option>
                <option value="GIGACHAT_API_CORP">Корпоративный</option>
              </select>
            </div>
            <label className="switch">
              <input type="checkbox" checked={!!cfg.auto_route}
                onChange={e => set('auto_route', e.target.checked)} />
              Автоматически переключать модель под сложность задачи
            </label>
            <div className="hint" style={{ fontSize: 11, color: 'var(--text-dim)' }}>
              Простые вопросы → GigaChat (быстро и дёшево), средние → GigaChat-Pro,
              сложные (анализ, код, планирование) → GigaChat-Max. Переключение незаметно.
            </div>

            <div className="field">
              <label>Запасной провайдер: OpenRouter (необязательно)</label>
              <input value={cfg.openrouter_api_key || ''} placeholder="sk-or-v1-…"
                onChange={e => set('openrouter_api_key', e.target.value)} />
              <div className="hint">Много бесплатных моделей, но из РФ может требовать VPN.
                Используется, только если GigaChat недоступен.</div>
            </div>
            <div className="field">
              <label>Локальная модель Ollama (необязательно, полностью бесплатно)</label>
              <input value={cfg.ollama_model || ''} placeholder="qwen2.5:7b"
                onChange={e => set('ollama_model', e.target.value)} />
              <div className="hint">Если на компьютере установлена Ollama — Джарвис подхватит её сам
                и сможет работать даже без интернета.</div>
            </div>
          </>}

          {tab === 'telegram' && <>
            <div className="field">
              <label>Токен бота</label>
              <input value={cfg.telegram_bot_token || ''} placeholder="123456:AA…"
                onChange={e => set('telegram_bot_token', e.target.value)} />
              <div className="hint">В телеграме напишите <b>@BotFather</b> → /newbot → придумайте имя →
                скопируйте токен сюда.</div>
            </div>
            <div className="field">
              <label>Ваш chat_id</label>
              <input value={cfg.telegram_chat_id || ''} placeholder="123456789"
                onChange={e => set('telegram_chat_id', e.target.value)} />
              <div className="hint">Напишите <b>@userinfobot</b> — он пришлёт ваш ID. Затем откройте
                своего бота и нажмите «Старт», иначе он не сможет вам писать.</div>
            </div>
            <label className="switch">
              <input type="checkbox" checked={!!cfg.telegram_enabled}
                onChange={e => set('telegram_enabled', e.target.checked)} />
              Присылать уведомления и принимать команды из телеграма
            </label>
            <div className="hint" style={{ color: 'var(--text-dim)' }}>
              В боте работают команды: <code>/task цель</code> — фоновая задача,
              <code> /status</code> — статус, <code>/yes</code> и <code>/no</code> — подтверждение действий.
            </div>
            <button className="btn primary" disabled={busy} onClick={testTg}>
              <IcTg size={14} /> Проверить связь</button>
          </>}

          {tab === 'safety' && <>
            <label className="switch">
              <input type="checkbox" checked={!!cfg.confirm_dangerous}
                onChange={e => set('confirm_dangerous', e.target.checked)} />
              Спрашивать подтверждение перед опасными действиями
            </label>
            <div className="hint" style={{ color: 'var(--text-dim)' }}>
              Покупки, оплата, удаление файлов, команды в системе, отправка данных наружу и клики
              в браузере на чувствительных страницах — всё это Джарвис выполнит только после вашего «Разрешить».
              Спросить может как в интерфейсе, так и в телеграме.
            </div>
            <label className="switch">
              <input type="checkbox" checked={!!cfg.allow_shell}
                onChange={e => set('allow_shell', e.target.checked)} />
              Разрешить выполнять команды в песочнице
            </label>
            <label className="switch">
              <input type="checkbox" checked={!!cfg.proactive}
                onChange={e => set('proactive', e.target.checked)} />
              Проактивный режим: сам напоминает и предлагает
            </label>
            <div className="field">
              <label>Максимум шагов в одной задаче</label>
              <input type="number" value={cfg.max_agent_steps}
                onChange={e => set('max_agent_steps', Number(e.target.value))} />
            </div>
          </>}

          {tab === 'me' && <>
            <div className="field">
              <label>Как к вам обращаться</label>
              <input value={cfg.user_name || ''} placeholder="сэр"
                onChange={e => set('user_name', e.target.value)} />
            </div>
            <div className="field">
              <label>Характер Джарвиса</label>
              <textarea rows={4} value={cfg.persona || ''}
                onChange={e => set('persona', e.target.value)} />
              <div className="hint">Можно переписать под себя: тон, юмор, длина ответов.</div>
            </div>
          </>}

          {tab === 'server' && <>
            <div className="field">
              <label>Токен доступа (для работы через интернет)</label>
              <input value={cfg.auth_token || ''} placeholder="пусто = без пароля, только для дома"
                onChange={e => set('auth_token', e.target.value)} />
              <div className="hint">Если Джарвис стоит на сервере — задайте здесь пароль-токен,
                иначе доступ к нему получит кто угодно. На домашнем компьютере можно оставить пустым.</div>
            </div>
            <div className="field">
              <label>Порт</label>
              <input type="number" value={cfg.port} onChange={e => set('port', Number(e.target.value))} />
              <div className="hint">После смены порта перезапустите Джарвиса.</div>
            </div>
          </>}
        </div>

        <div className="modal-f">
          {msg && <span style={{ marginRight: 'auto', fontSize: 12.5, color: 'var(--cyan)' }}>{msg}</span>}
          <button className="btn ghost" onClick={onClose}>Закрыть</button>
          <button className="btn primary" disabled={busy} onClick={save}>Сохранить</button>
        </div>
      </div>
    </div>
  )
}
