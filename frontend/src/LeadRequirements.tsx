import { asRecord, asString, asStringList } from './api'
import './LeadRequirements.css'

const CATEGORY_LABELS: Record<string, string> = {
  A: 'Высокий приоритет',
  B: 'Средний приоритет',
  C: 'Низкий приоритет',
  D: 'Нецелевой',
  E: 'Брак',
  unknown: 'Недостаточно данных',
}

const BUDGET_FIT_LABELS: Record<string, string> = {
  noprod: 'Продукт не определён',
  unknown: 'Нет данных',
  fit: 'Соответствует',
  slightly: 'Небольшое отклонение',
  notably: 'Заметное отклонение',
  far: 'Сильно ниже порога',
}

const STAGE_VERDICTS: Record<string, string> = {
  confirmed_correct: 'Этап подтверждён фактами',
  disputed: 'Этап расходится с фактами',
  insufficient_evidence: 'Недостаточно данных для оценки',
}

const READINESS_LABELS: Record<string, string> = {
  ready: 'Пункты собраны',
  incomplete: 'Есть пробелы',
  unknown: 'Тип оборудования не определён',
}

const ITEM_STATUS_LABELS: Record<string, string> = {
  collected: 'Собрано',
  partial: 'Частично',
  missing: 'Не найдено в доступных материалах',
  not_applicable: 'Не требуется',
}

function formatRubles(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return ''
  return `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(value)} ₽`
}

function formatRatio(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return ''
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(value)
}

function itemRows(group: Record<string, unknown>) {
  return Array.isArray(group.items) ? group.items.map(asRecord) : []
}

function requirementGroups(value: unknown) {
  return Array.isArray(value) ? value.map(asRecord) : []
}

export function LeadRequirements({
  assessment: assessmentValue,
  detailed = false,
  onOpenDetails,
}: {
  assessment: unknown
  detailed?: boolean
  onOpenDetails?: () => void
}) {
  const assessment = asRecord(assessmentValue)
  const autobant = asRecord(assessment.autobant)
  const result = asRecord(assessment.autobant_result)
  const technical = asRecord(assessment.technical_requirements)
  const stage = asRecord(assessment.stage_review)
  const version = asString(autobant.version) || asString(assessment.regulation_version)
  const hasAssessment = Object.keys(assessment).length > 0
  const hasResult = Object.keys(result).length > 0
  const groups = requirementGroups(technical.equipment)
  const rows = groups.flatMap(itemRows)
  const applicableRows = rows.filter((item) => asString(item.status) !== 'not_applicable')
  const collected = applicableRows.filter((item) => asString(item.status) === 'collected').length
  const partial = applicableRows.filter((item) => asString(item.status) === 'partial').length
  const category = asString(result.category)
  const budgetFit = asString(result.budget_fit)
  const formattedReference = formatRubles(result.reference_price_rub)
  const formattedMinimum = formatRubles(result.minimum_budget_rub)
  const ratio = formatRatio(result.budget_ratio)

  const technicalSummary = Object.keys(technical).length
    ? technical.required_now === true
      ? 'Полное ТЗ нужно до предметного расчёта, подбора комплектации или КП.'
      : technical.required_now === false
        ? 'Полное ТЗ пока не требуется; пробелы BANT остаются информационными.'
        : 'Не указано, требуется ли полное ТЗ сейчас.'
    : hasAssessment
      ? 'В старом формате отчёта чек-лист ТЗ не сохранялся.'
      : 'В отчёте нет блока квалификации.'

  if (detailed) {
    return (
      <div className="lead-requirements-detail">
        <header>
          <h3>Техническое задание</h3>
          <span className={`lead-requirements-status status-${READINESS_LABELS[asString(technical.readiness)] ? asString(technical.readiness) : 'unknown'}`}>
            {READINESS_LABELS[asString(technical.readiness)] || (Object.keys(technical).length ? 'Готовность не указана' : 'Нет данных старого формата')}
          </span>
        </header>
        <p className="lead-requirements-reason">{asString(technical.reason) || technicalSummary}</p>
        {technical.required_now === true ? <p className="lead-requirements-warning"><strong>Полное ТЗ требуется сейчас.</strong> Не переходите к расчёту, подбору комплектации или КП, пока обязательные пункты не собраны.</p> : null}
        {technical.required_now === false ? <p>Полное ТЗ пока не требуется. Ранние пробелы BANT не считаются отказом.</p> : null}
        {groups.length ? groups.map((group, groupIndex) => {
          const type = asString(group.type)
          const label = type === 'filling_line' ? 'Линия розлива' : type === 'labeler' ? 'Этикетировщик' : type || 'Тип оборудования не указан'
          const items = itemRows(group)
          return (
            <section className="lead-requirements-equipment" key={`${type || 'equipment'}-${groupIndex}`}>
              <h4>{label}</h4>
              {items.map((item, index) => {
                const status = asString(item.status)
                const statusLabel = ITEM_STATUS_LABELS[status] || 'Статус не указан'
                const evidence = asStringList(item.evidence)
                return (
                  <article className="lead-requirement-item" key={`${asString(item.key) || 'item'}-${index}`}>
                    <div className="lead-requirement-item-head">
                      <strong>{asString(item.label) || asString(item.key) || 'Требование'}</strong>
                      <span className={`lead-requirements-status status-${ITEM_STATUS_LABELS[status] ? status : 'unknown'}`}>{statusLabel}</span>
                    </div>
                    <p>{asString(item.summary) || 'Пояснение в отчёте отсутствует.'}</p>
                    <div className="lead-requirement-evidence">
                      <strong>Подтверждения</strong>
                      {evidence.length ? <ul>{evidence.map((fact, factIndex) => <li key={`${fact}-${factIndex}`}>{fact}</li>)}</ul> : <span>Подтверждения не сохранены.</span>}
                    </div>
                    {asString(item.next_question) ? <p className="lead-requirement-question"><strong>Следующий вопрос:</strong> {asString(item.next_question)}</p> : null}
                  </article>
                )
              })}
            </section>
          )
        }) : Object.keys(technical).length ? <p className="lead-requirements-empty">Тип оборудования не определён — перечень требований не сформирован.</p> : null}
        <p className="lead-requirements-note">Оценка основана на доступных разговорах и истории лида. Содержимое вложенных изображений не анализировалось.</p>
      </div>
    )
  }

  return (
    <section className="lead-requirements-summary" aria-label="AutoBANT, техническое задание и проверка этапа">
      <header>
        <div>
          <h3>Квалификация и полнота ТЗ</h3>
          <p>{version ? `AutoBANT ${version}` : hasResult ? 'Версия AutoBANT в отчёте не указана' : hasAssessment ? 'В старом формате AutoBANT 3.0 не сохранён' : 'Данные квалификации не сохранены'}</p>
        </div>
        {onOpenDetails ? <button type="button" onClick={onOpenDetails}>Все пункты ТЗ</button> : null}
      </header>
      <div className="lead-requirements-summary-grid">
        <section>
          <h4>AutoBANT 3.0</h4>
          {hasResult ? <>
            <strong>{category ? `${category} · ${CATEGORY_LABELS[category] || 'Категория'}` : 'Категория не указана'}</strong>
            {asString(result.priority) ? <span>Приоритет: {asString(result.priority)}</span> : null}
            {budgetFit ? <span>Бюджет: {BUDGET_FIT_LABELS[budgetFit] || budgetFit}</span> : null}
            {formattedReference ? <span>Ценовой ориентир, не КП: {formattedReference}</span> : null}
            {formattedMinimum ? <span>90% ценового ориентира: {formattedMinimum}</span> : null}
            {ratio ? <span>Коэффициент бюджета: {ratio}</span> : null}
            {asString(result.action) ? <p>{asString(result.action)}</p> : null}
            {asString(result.reason) ? <p>{asString(result.reason)}</p> : null}
          </> : <span>{hasAssessment ? 'Оценка AutoBANT 3.0 отсутствует в этом старом отчёте.' : 'Нет сохранённых данных квалификации.'}</span>}
        </section>
        <section>
          <h4>Проверка этапа</h4>
          {Object.keys(stage).length ? <>
            <span className={`lead-requirements-status status-${STAGE_VERDICTS[asString(stage.verdict)] ? asString(stage.verdict) : 'unknown'}`}>
              {STAGE_VERDICTS[asString(stage.verdict)] || 'Вердикт не указан'}
            </span>
            <span>Сейчас: {asString(stage.current_stage_name) || asString(stage.current_stage_id) || 'этап не указан'}</span>
            {asString(stage.recommended_stage) ? <span>Рекомендуется: {asString(stage.recommended_stage)}</span> : null}
            {asString(stage.reason) ? <p>{asString(stage.reason)}</p> : null}
            {asStringList(stage.evidence).length ? <ul>{asStringList(stage.evidence).map((fact, index) => <li key={`${fact}-${index}`}>{fact}</li>)}</ul> : <span>Подтверждения этапа отсутствуют.</span>}
            {stage.verdict === 'disputed' || stage.verdict === 'insufficient_evidence' ? <p className="lead-requirements-warning">Сверьте текущий этап с подтверждёнными фактами перед следующим действием.</p> : null}
          </> : <span>{hasAssessment ? 'Проверка этапа не сохранена в старом отчёте.' : 'Нет данных о проверке этапа.'}</span>}
        </section>
        <section>
          <h4>Технические требования</h4>
          {Object.keys(technical).length ? <>
            <strong>{READINESS_LABELS[asString(technical.readiness)] || 'Готовность не указана'}{applicableRows.length ? ` · собрано ${collected} из ${applicableRows.length}` : ''}</strong>
            {partial ? <span>Частично: {partial}</span> : null}
            {groups.length ? <span>Оборудование: {groups.map((group) => asString(group.type) === 'filling_line' ? 'линия розлива' : asString(group.type) === 'labeler' ? 'этикетировщик' : asString(group.type)).filter(Boolean).join(', ')}</span> : <span>Тип оборудования не определён.</span>}
            <p>{technicalSummary}</p>
            {asString(technical.reason) ? <p>{asString(technical.reason)}</p> : null}
            {technical.required_now === true && technical.readiness !== 'ready' ? <p className="lead-requirements-warning">ТЗ неполное: соберите обязательные пункты до расчёта, подбора комплектации или КП.</p> : null}
          </> : <span>{technicalSummary}</span>}
        </section>
      </div>
    </section>
  )
}
