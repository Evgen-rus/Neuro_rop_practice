import { useEffect, useState } from 'react'
import { fetchAiSpendDay, fetchAiSpendSummary, type AiSpendDay, type AiSpendEvent, type AiSpendSummary } from './api'

function formatToken(value: number | null | undefined) {
  if (value == null) return '—'
  return new Intl.NumberFormat('ru-RU').format(value)
}

function formatUsd(value: number | null | undefined) {
  if (value == null) return 'оценка недоступна'
  return `$${value.toFixed(4)}`
}

function EventRow({ event }: { event: AiSpendEvent }) {
  const [open, setOpen] = useState(false)
  const entity = event.entity_label || 'Без сущности'
  const model = event.model_label || event.model
  return <article className="ai-spend-event">
    <div className="ai-spend-event-main">
      <div>
        <strong>{event.time} · {entity}</strong>
        <p>{event.kind_label}{model ? ` · ${model}` : ''}</p>
      </div>
      <b>{event.estimated_cost_rub_label}</b>
    </div>
    <button type="button" className="ai-spend-tech-toggle" onClick={() => setOpen((value) => !value)}>
      {open ? 'Скрыть технические детали' : 'Технические детали'}
    </button>
    {open ? <dl className="ai-spend-tech">
      <div><dt>Модель</dt><dd>{model || '—'}</dd></div>
      <div><dt>Тип вызова</dt><dd>{event.kind_label}{event.kind && event.kind !== event.kind_label ? ` · ${event.kind}` : ''}</dd></div>
      <div><dt>Input tokens</dt><dd>{formatToken(event.input_tokens)}</dd></div>
      <div><dt>Cached input</dt><dd>{formatToken(event.cached_input_tokens)}</dd></div>
      <div><dt>Cache hit %</dt><dd>{event.cache_hit_percent == null ? '—' : `${String(event.cache_hit_percent).replace('.', ',')}%`}</dd></div>
      <div><dt>Cache write</dt><dd>{formatToken(event.cache_write_tokens)}</dd></div>
      <div><dt>Output tokens</dt><dd>{formatToken(event.output_tokens)}</dd></div>
      {event.reasoning_tokens != null ? <div><dt>Reasoning tokens</dt><dd>{formatToken(event.reasoning_tokens)}</dd></div> : null}
      {event.duration_seconds != null ? <div><dt>Длительность</dt><dd>{formatToken(Math.round(event.duration_seconds))} сек</dd></div> : null}
      <div><dt>Attempt</dt><dd>{event.attempt == null ? '—' : event.attempt}</dd></div>
      <div><dt>Status</dt><dd>{event.status_label || event.status || '—'}</dd></div>
      {event.run_id ? <div><dt>run_id</dt><dd>{event.run_id}</dd></div> : null}
      {event.job_id ? <div><dt>job_id</dt><dd>{event.job_id}</dd></div> : null}
      <div><dt>Стоимость USD</dt><dd>{formatUsd(event.estimated_cost_usd)}</dd></div>
      <div><dt>Стоимость RUB</dt><dd>{event.estimated_cost_rub_label}</dd></div>
    </dl> : null}
  </article>
}

function DayBlock({
  date,
  label,
  costLabel,
  paidLabel,
  open,
  onToggle,
}: {
  date: string
  label: string
  costLabel: string
  paidLabel: string
  open: boolean
  onToggle: () => void
}) {
  const [day, setDay] = useState<AiSpendDay | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open || day) return
    setLoading(true)
    setError('')
    void fetchAiSpendDay(date)
      .then((payload) => setDay(payload))
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setLoading(false))
  }, [date, day, open])

  return <section className={`ai-spend-day ${open ? 'open' : ''}`}>
    <button type="button" className="ai-spend-day-toggle" onClick={onToggle} aria-expanded={open}>
      <div>
        <strong>{label}</strong>
        <span>{costLabel} · {paidLabel}</span>
      </div>
      <i aria-hidden="true">{open ? '⌃' : '⌄'}</i>
    </button>
    {open ? <div className="ai-spend-day-body">
      {loading ? <p className="ai-spend-empty">Загружаем день…</p> : null}
      {error ? <p className="dc-alert error">{error}</p> : null}
      {day?.events.length ? day.events.map((event, index) => <EventRow key={`${event.at || date}-${event.kind || 'call'}-${event.entity_id || index}`} event={event} />) : null}
      {day && !day.events.length && !loading ? <p className="ai-spend-empty">За этот день платных вызовов нет.</p> : null}
    </div> : null}
  </section>
}

export function AiSpendDashboardCard({ onOpen }: { onOpen: () => void }) {
  const [summary, setSummary] = useState<AiSpendSummary | null>(null)
  useEffect(() => {
    void fetchAiSpendSummary().then(setSummary).catch(() => undefined)
  }, [])
  return <button type="button" className="ai-spend-teaser" onClick={onOpen}>
    <span>₽</span>
    <div>
      <small>Расходы AI сегодня</small>
      <strong>{summary?.today.estimated_cost_rub_label || '…'}</strong>
    </div>
    <em>Подробнее →</em>
  </button>
}

export function AiSpend() {
  const [summary, setSummary] = useState<AiSpendSummary | null>(null)
  const [error, setError] = useState('')
  const [openDate, setOpenDate] = useState('')

  useEffect(() => {
    void fetchAiSpendSummary()
      .then((payload) => setSummary(payload))
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [])

  return <div className="ai-spend-page">
    <header className="dc-header">
      <div className="dc-header-title">
        <h1>Расходы AI</h1>
        <p>Стоимость работы НейроРОПа</p>
      </div>
    </header>
    <p className="ai-spend-note">{summary?.title || 'Расходы AI — оценка'}. {summary?.disclaimer || 'Стоимость рассчитана по тарифам и курсу, настроенным в проекте. Это не счёт OpenAI.'}</p>
    {error ? <p className="dc-alert error">{error}</p> : null}
    {summary ? <>
      <section className="ai-spend-kpis">
        <article>
          <small>Сегодня</small>
          <strong>{summary.today.estimated_cost_rub_label}</strong>
          <span>{summary.today.paid_calls_label}</span>
        </article>
        <article>
          <small>7 дней</small>
          <strong>{summary.last_7_days.estimated_cost_rub_label}</strong>
          <span>{summary.last_7_days.paid_calls_label}</span>
        </article>
        <article>
          <small>30 дней</small>
          <strong>{summary.last_30_days.estimated_cost_rub_label}</strong>
          <span>{summary.last_30_days.paid_calls_label}</span>
        </article>
      </section>
      <section className="ai-spend-days">
        {summary.days.length ? summary.days.map((item) => (
          <DayBlock
            key={item.date}
            date={item.date}
            label={item.label}
            costLabel={item.estimated_cost_rub_label}
            paidLabel={item.paid_calls_label}
            open={openDate === item.date}
            onToggle={() => setOpenDate((current) => current === item.date ? '' : item.date)}
          />
        )) : <p className="ai-spend-empty">За последние 30 дней записей о расходах пока нет.</p>}
      </section>
    </> : error ? null : <p className="ai-spend-empty">Загружаем оценку расходов…</p>}
  </div>
}
