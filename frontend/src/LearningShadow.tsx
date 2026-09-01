import { useEffect, useMemo, useState } from 'react'
import { asRecord, asString, fetchLearningShadowRun, fetchLearningShadowRuns, startLearningShadowRun, type LearningShadowCase, type LearningShadowRun } from './api'
import { formatMoscowDateTime } from './dateTime'

function moscowToday(): string {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
}

const STATUS: Record<LearningShadowCase['status'], string> = {
  pending: 'Ожидает', no_action_observed: 'Нет коммуникации после просмотра',
  analyzing: 'Luna анализирует', completed: 'Разобрано', failed: 'Ошибка',
}

function CaseCard({ item }: { item: LearningShadowCase }) {
  const result = asRecord(item.llm_result)
  const correlations = Array.isArray(result.recommendation_correlations) ? result.recommendation_correlations.map(asRecord) : []
  const managerEvents = item.timeline.filter((event) => event.actor === 'manager')
  const clientEvents = item.timeline.filter((event) => event.actor === 'client')
  return <article className="ls-case">
    <header><div><h3>Сделка #{item.deal_id}</h3><p>Менеджер {item.manager_id || 'не указан'}</p></div><span>{STATUS[item.status]}</span></header>
    <div className="ls-flow">
      <section><b>Просмотренные рекомендации</b><p>{item.unique_recommendation_ids.join(', ')}</p><small>{item.view_count} просмотр(а), {formatMoscowDateTime(item.first_view_at, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })} — {formatMoscowDateTime(item.last_view_at, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}</small></section>
      <i>↓</i><section><b>Что менеджер сделал</b>{managerEvents.length ? managerEvents.map((event) => <p key={event.event_id}>{event.channel}: {event.content || event.event_type}</p>) : <p>Коммуникаций не найдено</p>}</section>
      <i>↓</i><section><b>Что ответил клиент</b>{clientEvents.length ? clientEvents.map((event) => <p key={event.event_id}>{event.content || event.event_type}</p>) : <p>Ответ не зафиксирован</p>}</section>
      <i>↓</i><section><b>Что произошло со сделкой</b><p>{asString(result.business_result_summary, item.status === 'no_action_observed' ? 'Действие менеджера после просмотра не наблюдалось.' : 'Результат ещё не получен.')}</p></section>
      {correlations.length ? <><i>↓</i><section><b>Возможное влияние рекомендаций</b>{correlations.map((row, index) => <p key={`${asString(row.recommendation_id)}-${index}`}><strong>{asString(row.recommendation_id)}</strong>: {asString(row.application)} · confidence {asString(row.confidence)}<br />{asString(row.explanation)}</p>)}</section></> : null}
    </div>
    <details><summary>Исходные рекомендации</summary><pre>{JSON.stringify(item.recommendations, null, 2)}</pre></details>
    <details><summary>Полная хронология</summary><pre>{JSON.stringify(item.timeline, null, 2)}</pre></details>
    <details><summary>Structured result Luna</summary><pre>{JSON.stringify(item.llm_result, null, 2)}</pre></details>
    {item.error ? <p className="dc-alert error">{item.error}</p> : null}
  </article>
}

export function LearningShadow() {
  const today = useMemo(moscowToday, [])
  const [fromDate, setFromDate] = useState(today)
  const [toDate, setToDate] = useState(today)
  const [run, setRun] = useState<LearningShadowRun | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const runId = run?.id
  const runStatus = run?.status
  useEffect(() => { void fetchLearningShadowRuns().then((payload) => { const latest = payload.items[0]; if (latest) void fetchLearningShadowRun(latest.id).then(setRun) }).catch(() => undefined) }, [])
  useEffect(() => {
    if (!runId || !runStatus || !['queued', 'running'].includes(runStatus)) return
    const timer = window.setInterval(() => { void fetchLearningShadowRun(runId).then((next) => { setRun(next); if (!['queued', 'running'].includes(next.status)) setRunning(false) }).catch((reason) => setError(reason instanceof Error ? reason.message : String(reason))) }, 1500)
    return () => window.clearInterval(timer)
  }, [runId, runStatus])
  async function start() {
    setError(''); setRunning(true)
    try { setRun(await startLearningShadowRun({ from_date: fromDate, to_date: toDate, confirm_paid: true })) }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); setRunning(false) }
  }
  return <div className="ls-page">
    <header className="dc-header"><div className="dc-header-title"><h1>Learning Shadow</h1><p>Корреляции между рекомендациями, действиями менеджера и результатом сделки</p></div></header>
    <section className="ls-controls"><label>С даты<input type="date" value={fromDate} max={toDate} onChange={(event) => setFromDate(event.target.value)} /></label><label>По дату<input type="date" value={toDate} min={fromDate} onChange={(event) => setToDate(event.target.value)} /></label><button className="dc-button" onClick={() => { setFromDate(today); setToDate(today) }}>Сегодня</button><button className="dc-button primary" disabled={running} onClick={() => void start()}>{running ? 'Анализируем…' : 'Проанализировать'}</button></section>
    {error ? <p className="dc-alert error">{error}</p> : null}
    {run ? <><section className="ls-kpis"><div><b>{run.total_cases}</b><span>сделок с просмотрами</span></div><div><b>{run.no_action_cases}</b><span>без действия</span></div><div><b>{run.llm_cases}</b><span>отправлено Luna</span></div><div><b>{run.completed_cases}</b><span>успешно разобрано</span></div></section><p className="ls-run-status">Run #{run.id}: {run.status} · {run.model} / {run.reasoning_effort}</p><section className="ls-cases">{run.cases?.map((item) => <CaseCard key={item.id} item={item} />)}</section></> : <p className="ls-empty">Запустите первый анализ выбранного периода.</p>}
  </div>
}
