import {
  automaticAnalysisCountersText,
  automaticAnalysisCurrentText,
  automaticAnalysisStageLabel,
  automaticAnalysisStatusLabel,
  canViewAutomaticAnalysis,
  type AutomaticAnalysisLatest,
} from './automaticAnalysis'
import { formatMoscowDateTime } from './dateTime'

export function AutomaticAnalysisPanel({ snapshot, role }: {
  snapshot: AutomaticAnalysisLatest | null
  role: string
}) {
  if (!snapshot || !canViewAutomaticAnalysis(role)) return null
  const current = automaticAnalysisCurrentText(snapshot)
  const stage = current ? null : automaticAnalysisStageLabel(snapshot.current_stage)
  const updated = snapshot.updated_at || snapshot.started_at
  const details = snapshot.details || []
  return (
    <details className={`dc-auto-analysis ${snapshot.status}`} key={snapshot.started_at}>
      <summary>
        <span className="dc-auto-analysis-summary">
          <strong>
            {snapshot.status === 'running' ? <span className="dc-spinner" /> : null}
            {automaticAnalysisStatusLabel(snapshot.status)}
          </strong>
          {current ? <span className="dc-auto-analysis-current">{current}</span> : null}
          <small>
            {snapshot.business_date ? `${snapshot.business_date} · ` : ''}
            {automaticAnalysisCountersText(snapshot)}
            {stage ? ` · этап: ${stage}` : ''}
            {updated ? ` · обновлено ${formatMoscowDateTime(updated, {
              day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
            }) || '—'}` : ''}
          </small>
          <small>Подробности FULL / MINI</small>
        </span>
      </summary>
      <div className="dc-auto-analysis-details">
        {details.length ? (
          <ul className="dc-auto-analysis-items">
            {details.map(item => (
              <li key={item.deal_id}>
                <strong>
                  {item.decision === 'full' ? 'FULL' : 'MINI'}
                  {item.incremental ? ' (инкрементальный LLM-анализ)' : ''}
                  {` · #${item.deal_id} · ${item.title}`}
                </strong>
                <ul>{item.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>
              </li>
            ))}
          </ul>
        ) : (
          <p>{snapshot.status === 'running'
            ? 'В этом пакете пока нет результатов FULL / MINI.'
            : 'В этом пакете нет результатов FULL / MINI.'}</p>
        )}
      </div>
    </details>
  )
}
