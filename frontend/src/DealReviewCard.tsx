import type { DailyControlDeal } from './api'
import { formatMoscowDateTime } from './dateTime'

const QUALITY_LABELS = {
  next_action: 'Следующий шаг',
  value_development: 'Ценность касаний',
  data_collection: 'Сбор данных',
} as const

const NO_DATA = 'Нет данных'
const DEFAULT_CONTENT_NOTE = 'Текст письма, исходное сообщение и транскрипт в снимке отсутствуют.'
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

function talkTime(seconds?: number | null) {
  const total = Math.max(0, Math.round(Number(seconds || 0)))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}`
  return `${minutes}:${String(rest).padStart(2, '0')}`
}

function channelLabel(channel: string) {
  if (channel === 'call') return 'Звонок'
  if (channel === 'email') return 'Письмо'
  if (channel === 'message') return 'Сообщение'
  return channel || NO_DATA
}

function directionLabel(direction: string) {
  if (direction === 'incoming') return 'входящий'
  if (direction === 'outgoing') return 'исходящий'
  return NO_DATA
}

const INSUFFICIENT_QUALITY_LABEL = 'Нет данных для оценки'

function humanQualityText(value?: string | null) {
  if (!value) return value
  return value
    .replace(/недостаточно evidence/gi, INSUFFICIENT_QUALITY_LABEL)
    .replace(/insufficient evidence/gi, INSUFFICIENT_QUALITY_LABEL)
    .replace(/\bevidence\b/gi, 'доказательств')
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

function channelIcon(channel: string) {
  if (channel === 'call') return 'phone'
  if (channel === 'email') return 'mail'
  return 'message'
}

export function DealQualityAndFocus(props: {
  deal: DailyControlDeal
  asked: [boolean, boolean]
  onToggleAsked: (index: 0 | 1) => void
}) {
  const deal = props.deal
  const quality = deal.quality
  const qualityCaption = quality.status === 'assessed' && quality.confirmed_count != null
    ? `${quality.confirmed_count} из ${quality.total} подтверждены`
    : quality.status === 'insufficient_evidence'
      ? INSUFFICIENT_QUALITY_LABEL
      : NO_DATA
  return (
    <>
      <article className="dc-daily-block">
        <header className="dc-daily-block-title">
          <span className="dc-daily-title-with-icon">
            <span className="dc-daily-ico"><DailyIcon name="audit" /></span>
            <h3>Контроль качества ведения сделки</h3>
          </span>
          <small>{qualityCaption}</small>
        </header>
        <div className="dc-daily-quality-body">
          <ul className={`dc-daily-criteria${quality.status === 'assessed' ? '' : ' compact'}`}>
            {(Object.keys(QUALITY_LABELS) as Array<keyof typeof QUALITY_LABELS>).map((key) => {
              const item = quality.criteria[key]
              const tone = item.score === 1 ? 'good' : item.score === 0 ? 'bad' : 'neutral'
              return (
                <li key={key} className={`dc-daily-criterion ${tone}`}>
                  <span className="dc-daily-criterion-icon" aria-hidden="true">{item.score === 1 ? '✓' : item.score === 0 ? '!' : '–'}</span>
                  <span className="dc-daily-criterion-name">{QUALITY_LABELS[key]}</span>
                  <strong className="dc-daily-criterion-score">{item.score == null ? '—' : `${item.score}/1`}</strong>
                  {item.score != null ? (
                    <span className="dc-daily-criterion-state">{humanQualityText(item.verdict)}</span>
                  ) : null}
                </li>
              )
            })}
          </ul>
          <details className="dc-daily-argument">
            <summary>
              <span className="dc-daily-argument-icon" aria-hidden="true">i</span>
              Аргументация
            </summary>
            <div className="dc-daily-argument-body">
              {quality.status === 'assessed' && quality.confirmed_count === 3 ? (
                <p className="dc-daily-argument-ok">
                  <span className="dc-daily-argument-ok-mark" aria-hidden="true">✓</span>
                  Оценки 3/3 подтверждены.
                </p>
              ) : null}
              <div className="dc-daily-argument-scope">
                <span className="dc-daily-argument-label">Основание анализа</span>
                <p>{humanQualityText(quality.scope_summary) || NO_DATA}</p>
              </div>
              {quality.insufficient_reason ? <p>{humanQualityText(quality.insufficient_reason)}</p> : null}
              {quality.zero_reasons.length ? (
                <div className="dc-daily-argument-reasons">
                  {quality.zero_reasons.map((reason, index) => (
                    <div className="dc-daily-argument-reason" key={`${reason.criterion}-${index}`}>
                      <strong>{qualityLabel(reason.criterion)}</strong>
                      <p>{humanQualityText(reason.explanation)}</p>
                      {reason.quote ? <blockquote>{reason.quote}</blockquote> : null}
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          </details>
        </div>
      </article>

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
              <p>{humanQualityText(deal.summary_for_rop || quality.insufficient_reason) || NO_DATA}</p>
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
}) {
  const deal = props.deal
  if (!deal) {
    return <section className="dc-daily-card"><p className="dc-daily-empty-list">{props.emptyText || 'Выберите другую категорию, чтобы открыть сделку.'}</p></section>
  }
  const communications = deal.communications_today
  const lastTouch = communications.items[communications.items.length - 1]
  const checklist = deal.checklist
  const contentNote = props.contentNote || DEFAULT_CONTENT_NOTE
  const scriptHint = props.scriptHint ?? DEFAULT_SCRIPT_HINT
  const script = meetingScript(deal)
  return (
    <section className="dc-daily-card">
      {props.showHeader !== false ? (
        <header className="dc-daily-card-head">
          <div>
            <h2>{deal.title || `Сделка #${deal.deal_id}`}</h2>
            <p>#{deal.deal_id} · {money(deal.amount, deal.currency_id || 'RUB')} · {deal.stage_name || NO_DATA}</p>
          </div>
          <span className={`dc-daily-pill ${deal.status}`}>{deal.status_label}</span>
        </header>
      ) : null}

      <DealQualityAndFocus deal={deal} asked={props.asked} onToggleAsked={props.onToggleAsked} />

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
            <p><b>Ситуация.</b> {deal.ai_context.current_situation || NO_DATA}</p>
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

      <article className="dc-daily-block">
        <header className="dc-daily-block-title">
          <span className="dc-daily-title-with-icon">
            <span className="dc-daily-ico"><DailyIcon name="phone" /></span>
            <h3>Коммуникации за сегодня</h3>
          </span>
        </header>
        {communications.unavailable ? (
          <p className="dc-daily-block-note">Данные коммуникаций недоступны. Это не нулевая активность.</p>
        ) : (
          <>
            <div className="dc-daily-comm-summary">
              <span><strong>{communications.calls}</strong> зв.</span>
              <span className="dc-daily-comm-sep" aria-hidden="true">·</span>
              <span><strong>{communications.messages}</strong> сообщ.</span>
              <span className="dc-daily-comm-sep" aria-hidden="true">·</span>
              <span><strong>{talkTime(communications.duration_seconds)}</strong></span>
              <span className="dc-daily-comm-sep" aria-hidden="true">·</span>
              <span>последнее касание {lastTouch ? formatClock(lastTouch.occurred_at) : NO_DATA}</span>
            </div>
            <details className="dc-daily-comm-events">
              <summary>Показать события · {communications.items.length}</summary>
              {communications.items.length ? communications.items.map((item) => {
                const open = props.openEventId === item.event_id
                return (
                  <button type="button" key={item.event_id} className={open ? 'open' : ''} onClick={() => props.onToggleEvent(item.event_id)}>
                    <time>{formatClock(item.occurred_at)}</time>
                    <span className="dc-daily-event-icon"><DailyIcon name={channelIcon(item.channel)} /></span>
                    <span>
                      {channelLabel(item.channel)} · {directionLabel(item.direction)}
                    </span>
                    <em>{item.duration_seconds ? talkTime(item.duration_seconds) : ''}</em>
                    <i aria-hidden="true">{open ? '▴' : '▾'}</i>
                    {open ? (
                      <div>
                        <p>{item.subject || NO_DATA}</p>
                        <small>ID события: {item.event_id}. {contentNote}</small>
                      </div>
                    ) : null}
                  </button>
                )
              }) : <p className="dc-daily-block-note">Событий за день не зафиксировано.</p>}
            </details>
          </>
        )}
      </article>

      <article className="dc-daily-block">
        <header className="dc-daily-block-title">
          <span className="dc-daily-title-with-icon">
            <span className="dc-daily-ico"><DailyIcon name="check" /></span>
            <h3>Чек-лист на сегодня</h3>
          </span>
          <small>{checklist.completed} из {checklist.total} выполнено</small>
        </header>
        <div className="dc-daily-checklist">
          {checklist.items.length ? checklist.items.map((item) => (
            <div key={item.id} className={item.completed ? 'done' : ''}>
              <span aria-hidden="true">{item.completed ? '✓' : ''}</span>
              <div>
                <strong>{item.text}</strong>
                {item.why ? <small>Почему: {item.why}</small> : null}
              </div>
            </div>
          )) : <p className="dc-daily-block-note">{NO_DATA}</p>}
        </div>
      </article>
    </section>
  )
}
