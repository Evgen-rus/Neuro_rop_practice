import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { fetchLeadControlHistory, type LeadControlHistory } from './api'
import { lockBodyScroll } from './bodyScrollLock'
import { focusableWithin } from './modalFocus'
import { formatMoscowDateTime } from './dateTime'
import './LeadHistory.css'

const date = (value?: string | null) => value ? formatMoscowDateTime(value, { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) || value : 'Дата не указана'
const channelLabels: Record<string, string> = { call: 'Звонок', phone: 'Звонок', email: 'Письмо', chat: 'Переписка', message: 'Сообщение', task: 'Задача' }
const kindLabels: Record<string, string> = { attempt: 'Попытка связи', contact: 'Контакт', confirmed_contact: 'Контакт', internal: 'Внутренняя запись', internal_information: 'Внутренняя запись' }

export function LeadHistory({ leadId, onClose }: { leadId: string; onClose: () => void }) {
  const [payload, setPayload] = useState<LeadControlHistory | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [request, setRequest] = useState({ revision: 0, refresh: false })
  const dialogRef = useRef<HTMLElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    closeRef.current?.focus({ preventScroll: true })
    const release = lockBodyScroll()
    return () => { release(); if (opener?.isConnected) opener.focus({ preventScroll: true }) }
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    void fetchLeadControlHistory(leadId, request.refresh)
      .then((result) => { if (!cancelled) setPayload(result) })
      .catch((reason) => { if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [leadId, request])

  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); onClose(); return }
      const panel = dialogRef.current
      if (event.key !== 'Tab' || !panel) return
      const items = focusableWithin(panel)
      const first = items[0], last = items.at(-1), active = document.activeElement
      if (first && last && ((event.shiftKey && (active === first || !panel.contains(active))) || (!event.shiftKey && (active === last || !panel.contains(active))))) {
        event.preventDefault(); (event.shiftKey ? last : first).focus()
      }
    }
    window.addEventListener('keydown', keydown)
    return () => window.removeEventListener('keydown', keydown)
  }, [onClose])

  const refresh = () => setRequest((current) => ({ revision: current.revision + 1, refresh: true }))
  return createPortal(<div className="dc-comments-modal" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={dialogRef} className="dc-comments-dialog lead-history-dialog" role="dialog" aria-modal="true" aria-label={`История лида #${leadId}`}>
      <header className="dc-comments-modal-head">
        <div className="dc-comments-modal-title"><strong>История лида · #{leadId}</strong><small>{payload?.fetched_at ? `Данные Bitrix: ${date(payload.fetched_at)}` : 'Комментарии и коммуникации из CRM'}</small></div>
        <button ref={closeRef} type="button" className="dc-comments-close" aria-label="Закрыть историю лида" onClick={onClose}>×</button>
      </header>
      <div className="lead-history-toolbar"><button type="button" className="dc-button" disabled={loading} onClick={refresh}>{loading ? 'Загружаем…' : 'Обновить Bitrix'}</button></div>
      {loading ? <p className="dc-comments-state" role="status">Загружаем историю лида…</p> : null}
      {error ? <div className="dc-comments-state error" role="alert"><p>{error}</p><button type="button" onClick={refresh}>Повторить</button></div> : null}
      {payload ? <>
        {payload.limitations.length ? <div className="lead-history-limitations">{payload.limitations.map((item, index) => <p key={index}>{item}</p>)}</div> : null}
        {!payload.available ? <p className="dc-comments-state">Полная история сейчас недоступна. Ниже показаны доступные данные.</p> : null}
        <div className="dc-comments-modal-body lead-history-body">
          <section className="dc-comments-pane">
            <header className="dc-comments-pane-head"><h3>Комментарии менеджера</h3><p>Внутренние записи Bitrix — отдельно от общения с клиентом</p></header>
            <div className="dc-comments-list">
              <div className="dc-comments-table-head"><span>Дата</span><span>Комментарий</span></div>
              {!payload.comments.length ? <p className="dc-comments-state">В доступных данных комментариев нет.</p> : null}
              {payload.comments.map((item) => <div className="dc-comment-table-row" key={item.id}><time>{date(item.created_at)}</time><div className="dc-comment-table-text">{item.text || 'Текст не доступен'}</div></div>)}
            </div>
          </section>
          <section className="dc-comments-review">
            <h3>Задача Bitrix</h3>
            {payload.current_task ? <div className="lead-history-task"><p>{payload.current_task.subject}</p><small>Срок: {date(payload.current_task.deadline)}</small></div> : <p className="dc-comments-state">В доступных данных открытая задача не найдена.</p>}
            <h3>Коммуникации</h3>
            {!payload.communications.length ? <p className="dc-comments-state">В доступных данных коммуникаций нет.</p> : null}
            {payload.communications.map((item) => <article className="lead-history-event" key={item.id}>
              <header><strong>{kindLabels[item.kind] || 'Запись CRM'} · {channelLabels[item.channel] || 'Коммуникация'}</strong><time>{date(item.occurred_at)}</time></header>
              <p>{item.text || 'Содержание коммуникации не доступно'}</p>
              {item.transcript_available === false ? <small>Готовая транскрипция отсутствует.</small> : null}
            </article>)}
          </section>
        </div>
      </> : null}
    </section>
  </div>, document.body)
}
