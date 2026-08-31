import { useId, useState } from 'react'

import type { DailyControlDeal, DealControlCommunicationItem, DealControlCommunicationsToday } from './api'
import { CommunicationContent } from './CommunicationContent'
import { formatMoscowDateTime } from './dateTime'
import { formatDealPipelineStage } from './dealDisplay'
import { dailyQualityCaption, snapshotDayText } from './dailyControlView'

const QUALITY_LABELS = {
  next_action: 'Следующий шаг',
  value_development: 'Ценность касаний',
  data_collection: 'Сбор данных',
} as const

const NO_DATA = 'Нет данных'
const DEFAULT_CONTENT_NOTE = 'Содержимое загружается отдельно и не является частью сохранённого снимка.'
const DEFAULT_SCRIPT_HINT = 'Формулировки для разговора с менеджером на планёрке'

const ICON_PATHS = {
  audit: <path d="M5 4h14v16H5zM8 8h8M8 12h5M8 16h7" />,
  briefcase: <><rect x="3" y="7" width="18" height="13" rx="2" /><path d="M8 7V5h8v2M3 12h18M10 12v2h4v-2" /></>,
  check: <path d="m5 12 4 4L19 6" />,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  mail: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m4 7 8 6 8-6" /></>,
  message: <><path d="M4 4h16v13H8l-4 3V4Z" /><path d="M8 9h8M8 13h5" /></>,
  pause: <><circle cx="12" cy="12" r="9" /><path d="M8.5 12h7" /></>,
  phone: <path d="M6.5 3.5 9 8 6.8 10a16 16 0 0 0 7.2 7.2L16 15l4.5 2.5-1 3c-.3.8-1.1 1.3-2 1.2C9 20.7 3.3 15 2.3 6.5c-.1-.9.4-1.7 1.2-2l3-1Z" />,
  script: <><path d="M7 4h10v16H7z" /><path d="M10 8h4M10 12h4M10 16h2" /></>,
  target: <><circle cx="12" cy="12" r="8" /><circle cx="12" cy="12" r="4" /><circle cx="12" cy="12" r="1.4" /></>,
} as const

export function DailyIcon({ name }: { name: keyof typeof ICON_PATHS }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      {ICON_PATHS[name]}
    </svg>
  )
}

function money(value?: string | number | null, currency = 'RUB') {
  const parsed = Number(String(value ?? '').replace(',', '.'))
  if (!Number.isFinite(parsed)) return NO_DATA
  return new Intl.NumberFormat('ru-RU', {
    style: 'currency',
    currency: currency || 'RUB',
    maximumFractionDigits: 0,
  }).format(parsed)
}

function formatClock(value?: string | null) {
  if (!value) return ''
  return formatMoscowDateTime(value, { hour: '2-digit', minute: '2-digit' }) || value
}

function talkTime(seconds?: number | null, empty = '0:00') {
  if (seconds == null || Number.isNaN(Number(seconds))) return empty
  const total = Math.max(0, Math.round(Number(seconds)))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
  return `${minutes}:${String(rest).padStart(2, '0')}`
}

function channelLabel(channel: string) {
  if (channel === 'call') return 'Звонок'
  if (channel === 'email') return 'Письмо'
  if (channel === 'whatsapp') return 'WhatsApp'
  if (channel === 'telegram') return 'Telegram'
  if (channel === 'max') return 'Max'
  if (channel === 'message') return 'Сообщение'
  return channel || NO_DATA
}

function directionLabel(direction: string) {
  if (direction === 'incoming') return 'входящий'
  if (direction === 'outgoing') return 'исходящий'
  return ''
}

const INSUFFICIENT_QUALITY_LABEL = 'Нет данных для оценки'

function humanQualityText(value?: string | null, snapshotDay = false) {
  if (!value) return value
  const cleaned = value
    .replace(/недостаточно evidence/gi, INSUFFICIENT_QUALITY_LABEL)
    .replace(/insufficient evidence/gi, INSUFFICIENT_QUALITY_LABEL)
    .replace(/\bevidence\b/gi, 'доказательств')
  return snapshotDay ? snapshotDayText(cleaned) : cleaned
}

function qualityLabel(criterion?: string | null) {
  if (criterion && criterion in QUALITY_LABELS) {
    return QUALITY_LABELS[criterion as keyof typeof QUALITY_LABELS]
  }
  return criterion || 'Критерий'
}

function meetingScript(deal: DailyControlDeal) {
  return String(deal.ai_context.manager_coaching || '').trim()
}

export function DealSituation(props: {
  situation?: string | null
  mode: 'live' | 'snapshot'
  cutoffAt?: string | null
}) {
  const situation = String(props.situation || '').trim()
  if (!situation) return null
  const date = props.cutoffAt
    ? formatMoscowDateTime(props.cutoffAt, { day: '2-digit', month: '2-digit', year: 'numeric' })
    : null
  const time = props.cutoffAt
    ? formatMoscowDateTime(props.cutoffAt, { hour: '2-digit', minute: '2-digit' })
    : null
  const cutoffLabel = date && time ? `${date} · ${time} МСК` : null
  return (
    <article className="dc-daily-block dc-deal-situation">
      <header className="dc-daily-block-title">
        <span className="dc-daily-title-with-icon">
          <span className="dc-daily-ico"><DailyIcon name={props.mode === 'snapshot' ? 'clock' : 'target'} /></span>
          <span className="dc-deal-situation-heading">
            <h3>{props.mode === 'snapshot' ? 'Ситуация на момент' : 'Текущая ситуация'}</h3>
            {props.mode === 'snapshot' && cutoffLabel ? <small>{cutoffLabel}</small> : null}
          </span>
        </span>
      </header>
      <p className="dc-deal-situation-text">{situation}</p>
    </article>
  )
}

function channelIcon(channel: string) {
  if (channel === 'call') return 'phone'
  if (channel === 'email') return 'mail'
  return 'message'
}

const TEXT_CHANNELS = new Set(['email', 'message', 'whatsapp', 'telegram', 'max'])

function eventStatus(item: DealControlCommunicationItem) {
  if (item.status_label) return item.status_label
  if (item.channel === 'call') {
    if (item.call_outcome === 'connected') return 'Разговор'
    if (item.call_outcome === 'no_answer') return item.direction === 'incoming' ? 'Пропущенный' : 'Не дозвонились'
    if (item.call_outcome === 'unknown') return 'Исход не определён'
  }
  if (item.channel === 'email') return item.direction === 'incoming' ? 'Получено' : 'Отправлено'
  if (item.direction === 'incoming') return 'Входящее'
  if (item.direction === 'outgoing') return 'Исходящее'
  return 'Доставлено'
}

function statusIsAttempt(item: DealControlCommunicationItem) {
  return item.channel === 'call' && item.call_outcome !== 'connected'
}

function eventDurationLabel(item: DealControlCommunicationItem) {
  if (item.channel !== 'call') return ''
  if (item.call_outcome === 'connected') {
    return item.talk_duration_seconds == null ? '—' : talkTime(item.talk_duration_seconds)
  }
  if (item.call_outcome === 'no_answer') return '0:00'
  return '—'
}

function canOpenContent(item: DealControlCommunicationItem) {
  if (TEXT_CHANNELS.has(item.channel)) return true
  return item.channel === 'call' && item.call_outcome === 'connected'
}

function displayEvents(items: DealControlCommunicationItem[]) {
  return [...items].sort((left, right) => {
    const byTime = String(right.occurred_at || '').localeCompare(String(left.occurred_at || ''))
    if (byTime) return byTime
    return String(right.event_id || '').localeCompare(String(left.event_id || ''))
  })
}

function summaryView(communications: DealControlCommunicationsToday) {
  const items = communications.items || []
  const callsTotal = communications.calls_total ?? communications.calls ?? 0
  const emails = communications.emails ?? items.filter((item) => item.channel === 'email').length
  const messenger = communications.messenger_messages
    ?? items.filter((item) => ['message', 'whatsapp', 'telegram', 'max'].includes(item.channel)).length
  return {
    callsTotal,
    attempts: communications.calls_no_answer ?? 0,
    unknown: communications.calls_unknown ?? 0,
    emails,
    messenger,
    emailSuffix: communications.email_suffix || (emails ? 'всего' : 'всего'),
    messageSuffix: communications.message_suffix || (messenger ? 'всего' : 'всего'),
    talkSeconds: communications.conversation_duration_seconds,
    lastActivity: communications.last_activity || null,
  }
}

function QualityArgumentation({ quality, snapshotDay = false }: { quality: DailyControlDeal['quality']; snapshotDay?: boolean }) {
  return (
    <div className="dc-daily-argument-body">
      {quality.status === 'assessed' && quality.confirmed_count === 3 ? (
        <p className="dc-daily-argument-ok">
          <span className="dc-daily-argument-ok-mark" aria-hidden="true">✓</span>
          AI подтвердил 3/3 за указанный день.
        </p>
      ) : null}
      <div className="dc-daily-argument-scope">
        <span className="dc-daily-argument-label">Основание анализа</span>
        <p>{humanQualityText(quality.scope_summary, snapshotDay) || NO_DATA}</p>
      </div>
      {quality.insufficient_reason && quality.insufficient_reason !== quality.scope_summary ? <p>{humanQualityText(quality.insufficient_reason, snapshotDay)}</p> : null}
      {quality.zero_reasons.length ? (
        <div className="dc-daily-argument-reasons">
          {quality.zero_reasons.map((reason, index) => (
            <div className="dc-daily-argument-reason" key={`${reason.criterion}-${index}`}>
              <strong>{qualityLabel(reason.criterion)}</strong>
              <p>{humanQualityText(reason.explanation, snapshotDay)}</p>
              {reason.quote ? <blockquote>{reason.quote}</blockquote> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}

function DealQualityBlock({ deal, snapshotDay = false }: { deal: DailyControlDeal; snapshotDay?: boolean }) {
  const quality = deal.quality
  const qualityCaption = dailyQualityCaption(quality, snapshotDay)
  const tipId = useId()
  // Аргументация больше не отдельный блок: один и тот же текст всплывает на любой карточке.
  return (
    <article className="dc-daily-block dc-daily-quality-block">
      <header className="dc-daily-block-title">
        <span className="dc-daily-title-with-icon">
          <span className="dc-daily-ico"><DailyIcon name="audit" /></span>
          <h3>Контроль качества ведения сделки</h3>
        </span>
        <small>{qualityCaption}</small>
      </header>
      <div className="dc-daily-quality-body">
        {quality.business_date ? (
          <p className="dc-daily-meta">
            За {quality.business_date.split('-').reverse().join('.')} · {quality.source === 'ai' ? 'AI-оценка' : 'Программный контроль'}
            {quality.cutoff_at ? ` · на ${formatClock(quality.cutoff_at)} МСК` : ''}
          </p>
        ) : null}
        {quality.status !== 'assessed' ? <p>{humanQualityText(quality.scope_summary, snapshotDay) || qualityCaption}</p> : null}
        <ul className={`dc-daily-criteria${['assessed', 'no_work'].includes(quality.status) ? '' : ' compact'}`}>
          {(Object.keys(QUALITY_LABELS) as Array<keyof typeof QUALITY_LABELS>).map((key) => {
            const item = quality.criteria[key]
            const tone = item.score === 1 ? 'good' : item.score === 0 ? 'bad' : 'neutral'
            const describedBy = `${tipId}-${key}`
            return (
              <li key={key} className={`dc-daily-criterion ${tone}`} tabIndex={0} aria-describedby={describedBy}>
                <span className="dc-daily-criterion-icon" aria-hidden="true">{item.score === 1 ? '✓' : item.score === 0 ? '!' : '–'}</span>
                <span className="dc-daily-criterion-name">
                  {QUALITY_LABELS[key]}
                  <span className="dc-daily-criterion-hint" aria-hidden="true">i</span>
                </span>
                <strong className="dc-daily-criterion-score">{item.score == null ? '—' : `${item.score}/1`}</strong>
                {item.score != null ? (
                  <span className="dc-daily-criterion-state">{humanQualityText(item.verdict, snapshotDay)}</span>
                ) : null}
                <div id={describedBy} role="tooltip" className="dc-daily-criterion-tip">
                  <QualityArgumentation quality={quality} snapshotDay={snapshotDay} />
                </div>
              </li>
            )
          })}
        </ul>
      </div>
    </article>
  )
}

function DealFocusBlock(props: {
  deal: DailyControlDeal
  asked: [boolean, boolean]
  onToggleAsked: (index: 0 | 1) => void
  snapshotDay?: boolean
}) {
  const deal = props.deal
  return (
    <article className="dc-daily-block">
      <header className="dc-daily-block-title">
        <span className="dc-daily-title-with-icon">
          <span className="dc-daily-ico"><DailyIcon name="target" /></span>
          <h3>Фокус разбора</h3>
        </span>
      </header>
      <div className="dc-daily-focus">
        <div className={`dc-daily-focus-step risk ${deal.status}`}>
          <span className="dc-daily-focus-icon" aria-hidden="true">!</span>
          <div>
            <small>Вывод для РОПа</small>
            <p>{humanQualityText(deal.summary_for_rop || deal.quality.insufficient_reason, props.snapshotDay) || NO_DATA}</p>
          </div>
        </div>
        <div className="dc-daily-focus-step question">
          <span className="dc-daily-focus-icon" aria-hidden="true">?</span>
          <div className="dc-daily-questions">
            <small>Спросить менеджера</small>
            <label className={props.asked[0] ? 'done' : ''}>
              <input type="checkbox" checked={props.asked[0]} onChange={() => props.onToggleAsked(0)} />
              <span>{deal.generic_question}</span>
            </label>
            <label className={props.asked[1] ? 'done' : ''}>
              <input type="checkbox" checked={props.asked[1]} onChange={() => props.onToggleAsked(1)} />
              <span>{deal.direct_question}</span>
            </label>
          </div>
        </div>
      </div>
    </article>
  )
}

export function DealQualityAndFocus(props: {
  deal: DailyControlDeal
  asked: [boolean, boolean]
  onToggleAsked: (index: 0 | 1) => void
  snapshotDay?: boolean
}) {
  return (
    <>
      <DealQualityBlock deal={props.deal} snapshotDay={props.snapshotDay} />
      <DealFocusBlock deal={props.deal} asked={props.asked} onToggleAsked={props.onToggleAsked} snapshotDay={props.snapshotDay} />
    </>
  )
}

export function DealReviewCard(props: {
  deal: DailyControlDeal | null
  asked: [boolean, boolean]
  onToggleAsked: (index: 0 | 1) => void
  onCopyScript: () => void
  copyNotice: string
  openEventId: string
  onToggleEvent: (eventId: string) => void
  showHeader?: boolean
  emptyText?: string
  contentNote?: string
  scriptHint?: string
  snapshotDay?: boolean
  snapshotCutoffAt?: string | null
}) {
  const [eventsOpen, setEventsOpen] = useState(false)
  const deal = props.deal
  if (!deal) {
    return <section className="dc-daily-card"><p className="dc-daily-empty-list">{props.emptyText || 'Выберите другую категорию, чтобы открыть сделку.'}</p></section>
  }
  const communications = deal.communications_today
  const summary = summaryView(communications)
  const events = displayEvents(communications.items || [])
  const contentNote = props.contentNote || DEFAULT_CONTENT_NOTE
  const scriptHint = props.scriptHint ?? DEFAULT_SCRIPT_HINT
  const script = meetingScript(deal)
  return (
    <section className="dc-daily-card">
      {props.showHeader !== false ? (
        <header className="dc-daily-card-head">
          <div>
            <h2>{deal.title || `Сделка #${deal.deal_id}`}</h2>
            <p>#{deal.deal_id} · {money(deal.amount, deal.currency_id || 'RUB')} · {formatDealPipelineStage(deal)}</p>
          </div>
          <span className={`dc-daily-pill ${deal.status}`}>{deal.status_label}</span>
        </header>
      ) : null}

      <DealSituation
        situation={deal.ai_context.current_situation}
        mode={props.snapshotDay ? 'snapshot' : 'live'}
        cutoffAt={props.snapshotCutoffAt || deal.day_scope?.cutoff_at}
      />

      <DealQualityBlock deal={deal} snapshotDay={props.snapshotDay} />

      <article className="dc-daily-block">
        <header className="dc-daily-block-title">
          <span className="dc-daily-title-with-icon">
            <span className="dc-daily-ico"><DailyIcon name="phone" /></span>
            <h3>{props.snapshotDay ? 'Коммуникации за этот день' : 'Коммуникации за сегодня'}</h3>
          </span>
          <small>Сделка #{deal.deal_id} · {deal.title || NO_DATA}</small>
        </header>
        {communications.unavailable ? (
          <p className="dc-daily-block-note">Данные коммуникаций недоступны. Это не нулевая активность.</p>
        ) : (
          <>
            <div className="dc-daily-comm-summary" aria-label="Сводка коммуникаций">
              <div className="dc-daily-stat">
                <span className="dc-daily-stat-label">Звонки</span>
                <span className="dc-daily-stat-value">
                  <strong>{summary.callsTotal}</strong>
                  <span>всего{summary.unknown > 0 ? ` · ${summary.unknown} исход не ясен` : ''}</span>
                </span>
              </div>
              <div className="dc-daily-stat">
                <span className="dc-daily-stat-label">Попытки дозвона</span>
                <span className="dc-daily-stat-value"><strong>{summary.attempts}</strong><span>без ответа</span></span>
              </div>
              <div className="dc-daily-stat">
                <span className="dc-daily-stat-label">Письма</span>
                <span className="dc-daily-stat-value"><strong>{summary.emails}</strong><span>{summary.emailSuffix}</span></span>
              </div>
              <div className="dc-daily-stat">
                <span className="dc-daily-stat-label">Сообщения</span>
                <span className="dc-daily-stat-value"><strong>{summary.messenger}</strong><span>{summary.messageSuffix}</span></span>
              </div>
              <div className="dc-daily-stat">
                <span className="dc-daily-stat-label">Длительность</span>
                <span className="dc-daily-stat-value">
                  {summary.talkSeconds == null ? (
                    <span className="dc-daily-stat-empty">нет данных</span>
                  ) : (
                    <>
                      <strong>{talkTime(summary.talkSeconds)}</strong>
                      <span>разговоров</span>
                    </>
                  )}
                </span>
              </div>
              <div className="dc-daily-stat last">
                <span className="dc-daily-stat-label">Последняя активность</span>
                <span className="dc-daily-stat-value">
                  {summary.lastActivity?.occurred_at ? (
                    <>
                      <strong>{formatClock(summary.lastActivity.occurred_at)}</strong>
                      <span>{summary.lastActivity.label || ''}</span>
                    </>
                  ) : (
                    <span className="dc-daily-stat-empty">нет данных</span>
                  )}
                </span>
              </div>
            </div>
            <details className="dc-daily-comm-events" onToggle={(event) => setEventsOpen(event.currentTarget.open)}>
              <summary>{eventsOpen ? 'Скрыть' : 'Показать'} события · {events.length}</summary>
              {events.length ? (
                <div className="dc-daily-event-list">
                  {events.map((item) => {
                    const open = props.openEventId === item.event_id
                    const attempt = statusIsAttempt(item)
                    return (
                      <div className={`dc-daily-comm-event ${open ? 'open' : ''}`} key={item.event_id}>
                        <button type="button" aria-expanded={open} onClick={() => props.onToggleEvent(item.event_id)}>
                          <time>{formatClock(item.occurred_at)}</time>
                          <span className="dc-daily-event-icon"><DailyIcon name={channelIcon(item.channel)} /></span>
                          <span className="dc-daily-event-main">
                            <span>{[channelLabel(item.channel), directionLabel(item.direction)].filter(Boolean).join(' · ')}</span>
                            <span className={`dc-daily-event-status${attempt ? ' attempt' : ''}`}>{eventStatus(item)}</span>
                          </span>
                          <span className="dc-daily-event-contact">{item.participant_name || item.subject || ''}</span>
                          <em className="dc-daily-event-duration">{eventDurationLabel(item)}</em>
                          <i className="dc-daily-event-chevron" aria-hidden="true">{open ? '▴' : '▾'}</i>
                        </button>
                        {open ? (
                          <div className="dc-daily-comm-event-detail">
                            <p className="dc-daily-event-meta">
                              {item.subject ? <span>Тема: <b>{item.subject}</b></span> : null}
                              <span>ID события: {item.event_id}</span>
                            </p>
                            {attempt ? (
                              <div className="dc-daily-attempt-note">
                                {item.call_outcome === 'unknown'
                                  ? 'Исход звонка не доказан — событие не считается разговором.'
                                  : 'Соединение не установлено — расшифровки разговора нет.'}
                              </div>
                            ) : null}
                            {canOpenContent(item) ? (
                              <CommunicationContent
                                dealId={deal.deal_id}
                                eventId={item.event_id}
                                channel={item.channel}
                                allowLoad={item.channel !== 'call' || item.call_outcome === 'connected'}
                              />
                            ) : null}
                            <small>{contentNote}</small>
                          </div>
                        ) : null}
                      </div>
                    )
                  })}
                </div>
              ) : <p className="dc-daily-block-note">Событий за день не зафиксировано.</p>}
            </details>
          </>
        )}
      </article>

      <DealFocusBlock deal={deal} asked={props.asked} onToggleAsked={props.onToggleAsked} snapshotDay={props.snapshotDay} />

      <div className="dc-daily-tiles">
        <details className="dc-daily-tile tone-ai">
          <summary>
            <span className="dc-daily-tile-icon" aria-hidden="true">AI</span>
            <span>
              <b>Контекст и вывод AI</b>
              <small>Почему сделка требует внимания</small>
            </span>
          </summary>
          <div className="dc-daily-tile-body">
            {deal.ai_context.rop_focus ? <p><b>Фокус РОПа.</b> {deal.ai_context.rop_focus}</p> : null}
            {deal.ai_context.what_to_check_now ? <p><b>Проверить сейчас.</b> {deal.ai_context.what_to_check_now}</p> : null}
            {deal.ai_context.manager_coaching ? <p><b>Сообщение менеджеру.</b> {deal.ai_context.manager_coaching}</p> : null}
            {deal.ai_context.known.length ? <ul>{deal.ai_context.known.map((item) => <li key={item}>{item}</li>)}</ul> : null}
            {deal.ai_context.unknowns.length ? <p><b>Неизвестно:</b> {deal.ai_context.unknowns.join('; ')}</p> : null}
          </div>
        </details>
        <details className="dc-daily-tile tone-script">
          <summary>
            <span className="dc-daily-tile-icon" aria-hidden="true"><DailyIcon name="script" /></span>
            <span>
              <b>Готовый сценарий разговора</b>
              <small>{scriptHint}</small>
            </span>
          </summary>
          <div className="dc-daily-tile-body">
            {script ? <pre>{script}</pre> : <p>{NO_DATA}</p>}
            {script ? <button type="button" className="dc-button" onClick={props.onCopyScript}>Скопировать сценарий</button> : null}
            {props.copyNotice ? <small>{props.copyNotice}</small> : null}
          </div>
        </details>
      </div>

    </section>
  )
}
