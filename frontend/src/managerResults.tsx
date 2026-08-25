import { useState } from 'react'
import {
  isCallScriptContent,
  type ManagerCompanionRecord,
  type ManagerEmailContent,
  type ManagerFollowupsRecord,
  type ManagerFullScriptContent,
  type ManagerLifehack,
  type ManagerQuickHelpEntry,
  type ManagerQuickHelpStrategy,
} from './api'
import { answerModeClassName, pressureLever, strategyLabel, visibleLifehack } from './dealPush'
import { formatMoscowDateTime } from './dateTime'

function dateTime(value?: string | null) {
  if (!value) return ''
  return formatMoscowDateTime(value, {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }) || value
}

export function QuickHelpResultView({
  entry,
  mode,
  selectedStrategy,
  onSelectedStrategy,
  onCopy,
}: {
  entry: Pick<ManagerQuickHelpEntry, 'content' | 'created_at'>
  mode: 'push' | 'reanimator'
  selectedStrategy: ManagerQuickHelpStrategy
  onSelectedStrategy: (strategy: ManagerQuickHelpStrategy) => void
  onCopy: (text: string, label: string) => Promise<void>
}) {
  const content = entry.content
  const lever = pressureLever(content)
  const strategies: ManagerQuickHelpStrategy[] = ['primary', 'alternative', 'pattern_break']
  const message = 'client_messages' in content
    ? String((content.client_messages as Record<string, string>)[selectedStrategy] || '')
    : ''
  const lifehacks: ManagerLifehack[] = 'lifehacks' in content ? content.lifehacks : []
  return <article className={answerModeClassName(mode)}>
    <div className="dc-manager-answer-summary">
      <span>◎</span>
      <div>
        <div className="dc-manager-answer-title">
          <h4>Понял ситуацию</h4>
          {entry.created_at ? <time dateTime={entry.created_at}>{dateTime(entry.created_at)}</time> : null}
        </div>
        <p>
          <span>{content.situation_summary || 'Ситуация пока не сформирована.'}</span>
          {content.next_action ? <> <strong>{content.next_action}</strong></> : null}
          {content.expected_result ? <> <span> {content.expected_result}</span></> : null}
        </p>
      </div>
    </div>
    {lever ? <section className="dc-manager-lever">
      <div><small>Рычаг дожима</small><strong>{lever.title}</strong></div>
      <p>{lever.rationale}</p>
    </section> : null}
    {'client_messages' in content ? <section className="dc-manager-answer-copy message">
      <div><h4>Сообщение клиенту</h4><button className="dc-button" disabled={!message} onClick={() => void onCopy(String(message || ''), 'Сообщение клиенту')}>Скопировать</button></div>
      <div className="dc-manager-tone-tabs labeled" role="tablist">
        {strategies.map((strategy) => (
          <button key={strategy} type="button" role="tab" aria-selected={selectedStrategy === strategy} className={selectedStrategy === strategy ? 'active' : ''} onClick={() => onSelectedStrategy(strategy)}>
            <span>{strategyLabel(content, strategy)}</span>
          </button>
        ))}
      </div>
      <pre>{message || 'Сообщение пока не сформировано.'}</pre>
    </section> : null}
    {lifehacks.length ? <LifehackStrip lifehacks={lifehacks} /> : null}
    {'fallback_action' in content && content.fallback_action ? <div className="dc-manager-answer-fallback"><strong>Если не сработало</strong><span>{content.fallback_action}</span></div> : null}
  </article>
}

function LifehackStrip({ lifehacks }: { lifehacks: ManagerLifehack[] }) {
  const [index, setIndex] = useState(0)
  const visible = visibleLifehack(lifehacks, index)
  const item = visible?.item
  const total = visible?.total || 0
  const safeIndex = visible?.index || 0
  return <section className="dc-manager-lifehacks">
    <div className="dc-manager-lifehacks-head">
      <h4>Лайфхаки</h4>
      {total > 1 ? <nav><button type="button" disabled={safeIndex === 0} onClick={() => setIndex((value) => Math.max(0, value - 1))}>←</button><span>{safeIndex + 1} из {total}</span><button type="button" disabled={safeIndex >= total - 1} onClick={() => setIndex((value) => Math.min(total - 1, value + 1))}>→</button></nav> : null}
    </div>
    {item ? <article><strong>{item.title}</strong><p>{item.action}</p><small>{item.why_relevant}</small><em>{item.conditions}</em></article> : <p>Подходящий лайфхак не найден.</p>}
  </section>
}

export function FollowupsResultView({ record }: { record: ManagerFollowupsRecord }) {
  return <section className="dc-manager-followups">
    <p className="summary">{record.content.context_summary}</p>
    <div>{record.content.items.map((item) => (
      <article key={item.item_id}>
        <header><strong>{item.concern_or_scenario}</strong><span>{item.basis_status === 'confirmed' ? 'Подтверждено' : item.basis_status === 'inferred' ? 'Гипотеза' : 'Условный сценарий'}</span></header>
        <h4>{item.idea}</h4>
        <p>{item.why_it_may_help}</p>
        <dl>
          <div><dt>Формат</dt><dd>{item.followup_type}</dd></div>
          <div><dt>Канал</dt><dd>{item.suggested_channel}</dd></div>
          <div><dt>Когда</dt><dd>{item.timing}</dd></div>
          <div><dt>Цель</dt><dd>{item.target_micro_conversion}</dd></div>
        </dl>
        <small>Основание: {item.evidence_summary}</small>
        <em>{item.caution}</em>
      </article>
    ))}</div>
  </section>
}

export function CompanionResultView({
  companion,
  onCopy,
}: {
  companion: ManagerCompanionRecord
  onCopy: (text: string) => void
}) {
  const message = String(companion.content.message_text || '').trim()
  const understood = companion.content.understood || []
  if (!message) {
    return <p className="empty">{companion.content.insufficient_reason || 'Сопроводительный текст ещё не сформирован'}</p>
  }
  return <>
    {understood.length ? <div className="understood"><small>Что система поняла</small><ul>{understood.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
    <pre>{message}</pre>
    <button type="button" className="dc-button" onClick={() => onCopy(message)}>Скопировать</button>
  </>
}

export function FullScriptResultView({
  script,
  onCopy,
}: {
  script: ManagerFullScriptContent | ManagerEmailContent
  onCopy: (text: string, label: string) => Promise<void>
}) {
  if ('email_contract' in script) {
    const copyText = [script.subject, '', script.greeting, script.context, ...script.questions.map((question, index) => `${index + 1}. ${question}`), script.value_point, script.call_to_action, script.closing].join('\n\n')
    return <section className="dc-manager-email">
      <div><small>Тема</small><strong>{script.subject}</strong></div>
      <article>
        <p>{script.greeting}</p>
        <p>{script.context}</p>
        {script.questions.length ? <ol>{script.questions.map((question) => <li key={question}>{question}</li>)}</ol> : null}
        <p>{script.value_point}</p>
        <p>{script.call_to_action}</p>
        <p>{script.closing}</p>
      </article>
      <button className="dc-button" onClick={() => void onCopy(copyText, 'Email клиенту')}>Скопировать</button>
    </section>
  }
  const title = isCallScriptContent(script) ? 'Сценарий звонка' : 'Продолжение переписки'
  return <div>
    <section className="dc-manager-full-script-goal"><small>Цель разговора</small><strong>{script.conversation_goal}</strong></section>
    <ol className="dc-prompt-lab-script-blocks">{script.blocks.map((block, index) => (
      <li key={block.block_id}>
        <span>{index + 1}</span>
        <article>
          <h3>{block.title}</h3>
          <p>{block.objective}</p>
          {'spoken_text' in block ? <blockquote>{block.spoken_text}</blockquote> : block.suggested_phrases.map((phrase) => <blockquote key={phrase}>{phrase}</blockquote>)}
          {block.listen_for.length ? <div><strong>Услышать:</strong> {block.listen_for.join(' · ')}</div> : null}
          <footer><strong>Переход:</strong> {block.transition}</footer>
        </article>
      </li>
    ))}</ol>
    <section className="dc-manager-full-script-close"><small>{isCallScriptContent(script) ? 'Резюме разговора и следующий шаг' : 'Завершить договорённостью'}</small><strong>{script.closing_agreement}</strong></section>
    <button className="dc-button" onClick={() => void onCopy(script.conversation_goal, title)}>Скопировать цель</button>
  </div>
}
