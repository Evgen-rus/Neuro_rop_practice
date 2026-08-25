import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  isCallScriptContent,
  type ManagerCallScriptContent,
  type ManagerCompanionRecord,
  type ManagerEmailContent,
  type ManagerFollowupsRecord,
  type ManagerFullScriptBlock,
  type ManagerFullScriptContent,
  type ManagerFullScriptMode,
  type ManagerLifehack,
  type ManagerObjectionHandling,
  type ManagerQuickHelpContent,
  type ManagerQuickHelpEntry,
  type ManagerQuickHelpLegacyContent,
  type ManagerQuickHelpStrategy,
  type ManagerQuickHelpStrategyContent,
  type ManagerQuickHelpStrategyV2Content,
  type ManagerQuickHelpStrategyV3Content,
} from './api'
import { answerModeClassName, pressureLever, strategyLabel, visibleLifehack } from './dealPush'
import { formatMoscowDateTime } from './dateTime'
import { revealClassName } from './quickHelpReveal'

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
  summaryText,
  summaryReady = true,
  showCaret = false,
  animate = false,
  showMessage = true,
  showSecondary = true,
  showFallback = true,
  onEdit,
  onOpenScript,
  activeScriptMode = null,
  showScriptActions = false,
  footer,
}: {
  entry: Pick<ManagerQuickHelpEntry, 'content' | 'created_at'>
  mode: 'push' | 'reanimator'
  selectedStrategy: ManagerQuickHelpStrategy
  onSelectedStrategy: (strategy: ManagerQuickHelpStrategy) => void
  onCopy: (text: string, label: string) => Promise<void>
  summaryText?: string
  summaryReady?: boolean
  showCaret?: boolean
  animate?: boolean
  showMessage?: boolean
  showSecondary?: boolean
  showFallback?: boolean
  onEdit?: () => void
  onOpenScript?: (mode: ManagerFullScriptMode) => Promise<void>
  activeScriptMode?: ManagerFullScriptMode | null
  showScriptActions?: boolean
  footer?: ReactNode
}) {
  const content = entry.content
  const lever = pressureLever(content)
  const summary = summaryText ?? (content.situation_summary || 'Ситуация пока не сформирована.')
  return <article className={answerModeClassName(mode)}>
    <div className="dc-manager-answer-summary">
      <span>◎</span>
      <div>
        <div className="dc-manager-answer-title">
          <h4>Понял ситуацию</h4>
          {entry.created_at ? <time dateTime={entry.created_at}>{dateTime(entry.created_at)}</time> : null}
        </div>
        <p>
          <span>{summary}{showCaret ? <i className="dc-manager-answer-caret" aria-hidden="true" /> : null}</span>
          {summaryReady && content.next_action ? <> <strong>{content.next_action}</strong></> : null}
          {summaryReady && content.expected_result ? <> <span> {content.expected_result}</span></> : null}
        </p>
      </div>
      {onEdit ? <button className="dc-link-button" onClick={onEdit}>Изменить</button> : null}
    </div>
    {showMessage && lever ? <ManagerPressureLeverCard lever={lever} animate={animate} /> : null}
    {showMessage || showSecondary ? <ManagerQuickHelpVariants
      content={content}
      onCopy={onCopy}
      selectedStrategy={selectedStrategy}
      onSelectedStrategy={onSelectedStrategy}
      onOpenScript={onOpenScript}
      activeScriptMode={activeScriptMode}
      showScriptActions={showScriptActions}
      showMessage={showMessage}
      showSecondary={showSecondary}
      animate={animate}
    /> : null}
    {showFallback && (content.answer_contract === 'strategy_v1' || content.answer_contract === 'strategy_v2' || content.answer_contract === 'strategy_v3') && content.fallback_action ? <div className={revealClassName('dc-manager-answer-fallback', animate)}><strong>Если не сработало</strong><span>{content.fallback_action}</span></div> : null}
    {footer}
  </article>
}

export function ManagerPressureLeverCard({ lever, animate = false }: { lever: { title: string; rationale: string }; animate?: boolean }) {
  const [open, setOpen] = useState(false)
  return <section className={revealClassName('dc-manager-lever', animate)}>
    <div>
      <small>Рычаг дожима</small>
      <strong>{lever.title}</strong>
    </div>
    <button type="button" className="dc-link-button" onClick={() => setOpen((value) => !value)}>{open ? 'Скрыть' : 'Подробнее'}</button>
    {open ? <p>{lever.rationale}</p> : null}
  </section>
}

export function ManagerQuickHelpVariants({ content, onCopy, selectedStrategy, onSelectedStrategy, onOpenScript, activeScriptMode, showScriptActions, showMessage = true, showSecondary = true, animate = false }: {
  content: ManagerQuickHelpContent
  onCopy: (text: string, label: string) => Promise<void>
  selectedStrategy: ManagerQuickHelpStrategy
  onSelectedStrategy: (strategy: ManagerQuickHelpStrategy) => void
  onOpenScript?: (mode: ManagerFullScriptMode) => Promise<void>
  activeScriptMode: ManagerFullScriptMode | null
  showScriptActions: boolean
  showMessage?: boolean
  showSecondary?: boolean
  animate?: boolean
}) {
  return content.answer_contract === 'strategy_v3' || content.answer_contract === 'strategy_v2'
    ? <ManagerStrategyV2QuickHelpVariants content={content} onCopy={onCopy} selectedStrategy={selectedStrategy} onSelectedStrategy={onSelectedStrategy} onOpenScript={onOpenScript} activeScriptMode={activeScriptMode} showScriptActions={showScriptActions} showMessage={showMessage} showSecondary={showSecondary} animate={animate} />
    : content.answer_contract === 'strategy_v1'
    ? <ManagerStrategyQuickHelpVariants content={content} onCopy={onCopy} showMessage={showMessage} showSecondary={showSecondary} animate={animate} />
    : <ManagerLegacyQuickHelpVariants content={content} onCopy={onCopy} showMessage={showMessage} showSecondary={showSecondary} animate={animate} />
}

function ManagerLifehackCarousel({ lifehacks, animate = false }: { lifehacks: ManagerLifehack[]; animate?: boolean }) {
  const [index, setIndex] = useState(0)
  const visible = visibleLifehack(lifehacks, index)
  const item = visible?.item || null
  const total = visible?.total || 0
  const safeIndex = visible?.index || 0
  return <section className={revealClassName('dc-manager-lifehacks', animate)}>
    <div className="dc-manager-lifehacks-head">
      <h4>Лайфхаки</h4>
      {total > 1 ? <nav aria-label="Лайфхаки"><button type="button" disabled={safeIndex === 0} onClick={() => setIndex((value) => Math.max(0, value - 1))}>←</button><span>{safeIndex + 1} из {total}</span><button type="button" disabled={safeIndex >= total - 1} onClick={() => setIndex((value) => Math.min(total - 1, value + 1))}>→</button></nav> : null}
    </div>
    {item ? <article key={item.tactic_id}><strong>{item.title}</strong><p>{item.action}</p><small>{item.why_relevant}</small><em>{item.conditions}</em></article> : <p>Подходящий лайфхак не найден.</p>}
  </section>
}

function ManagerStrategyV2QuickHelpVariants({ content, onCopy, selectedStrategy, onSelectedStrategy, onOpenScript, activeScriptMode, showScriptActions, showMessage = true, showSecondary = true, animate = false }: {
  content: ManagerQuickHelpStrategyV2Content | ManagerQuickHelpStrategyV3Content
  onCopy: (text: string, label: string) => Promise<void>
  selectedStrategy: ManagerQuickHelpStrategy
  onSelectedStrategy: (strategy: ManagerQuickHelpStrategy) => void
  onOpenScript?: (mode: ManagerFullScriptMode) => Promise<void>
  activeScriptMode: ManagerFullScriptMode | null
  showScriptActions: boolean
  showMessage?: boolean
  showSecondary?: boolean
  animate?: boolean
}) {
  const strategies: ManagerQuickHelpStrategy[] = ['primary', 'alternative', 'pattern_break']
  const message = content.client_messages[selectedStrategy]
  const formats: Array<[ManagerFullScriptMode, string, string]> = [['call', '☎', 'Звонок'], ['message', '💬', 'Переписка'], ['email', '✉', 'Email']]
  return <div className="dc-manager-answer-v2">
    {showMessage ? <section className={revealClassName('dc-manager-answer-copy message', animate)}>
      <div><h4>Сообщение клиенту</h4><div className="dc-manager-message-tools">{showScriptActions && onOpenScript ? <div className="dc-manager-format-icons" role="group" aria-label="Формат коммуникации">{formats.map(([mode, icon, label]) => <button key={mode} type="button" className={activeScriptMode === mode ? 'active' : ''} title={label} aria-label={label} aria-pressed={activeScriptMode === mode} onClick={() => void onOpenScript(mode)}>{icon}</button>)}</div> : null}<button className="dc-button" disabled={!message} onClick={() => void onCopy(message, 'Сообщение клиенту')}>Скопировать</button></div></div>
      <div className="dc-manager-tone-tabs labeled" role="tablist" aria-label="Вариант сообщения клиенту">{strategies.map((strategy) => <button key={strategy} type="button" role="tab" aria-selected={selectedStrategy === strategy} className={selectedStrategy === strategy ? 'active' : ''} onClick={() => onSelectedStrategy(strategy)}><span>{strategyLabel(content, strategy)}</span></button>)}</div>
      <pre>{message || 'Сообщение пока не сформировано.'}</pre>
    </section> : null}
    {showSecondary ? <ManagerLifehackCarousel lifehacks={content.lifehacks} animate={animate} /> : null}
  </div>
}

function ManagerLegacyQuickHelpVariants({ content, onCopy, showMessage = true, showSecondary = true, animate = false }: {
  content: ManagerQuickHelpLegacyContent
  onCopy: (text: string, label: string) => Promise<void>
  showMessage?: boolean
  showSecondary?: boolean
  animate?: boolean
}) {
  const [clientTone, setClientTone] = useState<ManagerQuickHelpLegacyContent['recommended_client_tone']>(content.recommended_client_tone)
  const [callTone, setCallTone] = useState<ManagerQuickHelpLegacyContent['recommended_call_tone']>(content.recommended_call_tone)
  const clientTones = [
    ['calm', 'Спокойно'],
    ['confident', 'Уверенно'],
    ['direct', 'Прямо'],
  ] as const
  const callTones = [
    ['soft', 'Мягко'],
    ['business', 'Деловой'],
    ['direct', 'Прямой'],
  ] as const
  const clientMessage = content.client_messages[clientTone]
  const callScript = content.call_scripts[callTone]
  return <div className="dc-manager-answer-modules">
      {showMessage ? <section className={revealClassName('dc-manager-answer-copy message', animate)}><div><h4>Сообщение клиенту</h4><button className="dc-button" disabled={!clientMessage} onClick={() => void onCopy(clientMessage, 'Сообщение клиенту')}>Скопировать</button></div><div className="dc-manager-tone-tabs" role="tablist" aria-label="Тон сообщения клиенту">{clientTones.map(([tone, label]) => <button key={tone} type="button" role="tab" aria-selected={clientTone === tone} className={clientTone === tone ? 'active' : ''} onClick={() => setClientTone(tone)}><span>{label}</span>{content.recommended_client_tone === tone ? <small>Рекомендуется</small> : null}</button>)}</div><pre>{clientMessage || 'Сообщение пока не сформировано.'}</pre></section> : null}
      {showSecondary ? <section className={revealClassName('dc-manager-answer-copy speech', animate)}><div><h4>Речевой модуль</h4><button className="dc-button" disabled={!callScript} onClick={() => void onCopy(callScript, 'Речевой модуль')}>Скопировать</button></div><div className="dc-manager-tone-tabs" role="tablist" aria-label="Тон речевого модуля">{callTones.map(([tone, label]) => <button key={tone} type="button" role="tab" aria-selected={callTone === tone} className={callTone === tone ? 'active' : ''} onClick={() => setCallTone(tone)}><span>{label}</span>{content.recommended_call_tone === tone ? <small>Рекомендуется</small> : null}</button>)}</div><pre>{callScript || 'Речевой модуль пока не сформирован.'}</pre></section> : null}
    </div>
}

function ManagerStrategyQuickHelpVariants({ content, onCopy, showMessage = true, showSecondary = true, animate = false }: {
  content: ManagerQuickHelpStrategyContent
  onCopy: (text: string, label: string) => Promise<void>
  showMessage?: boolean
  showSecondary?: boolean
  animate?: boolean
}) {
  const initialMessage = content.recommended_channel === 'message' ? content.recommended_strategy : 'primary'
  const initialCall = content.recommended_channel === 'call' ? content.recommended_strategy : 'primary'
  const [messageStrategy, setMessageStrategy] = useState<ManagerQuickHelpStrategy>(initialMessage)
  const [callStrategy, setCallStrategy] = useState<ManagerQuickHelpStrategy>(initialCall)
  const strategies = [
    ['primary', 'Лучший ход'],
    ['alternative', 'Другой заход'],
    ['pattern_break', 'Смена хода'],
  ] as const
  const clientMessage = content.client_messages[messageStrategy]
  const callScript = content.call_scripts[callStrategy]
  return <div className="dc-manager-answer-modules">
    {showMessage ? <section className={revealClassName('dc-manager-answer-copy message', animate)}><div><h4>Сообщение клиенту</h4><button className="dc-button" disabled={!clientMessage} onClick={() => void onCopy(clientMessage, 'Сообщение клиенту')}>Скопировать</button></div><div className="dc-manager-tone-tabs" role="tablist" aria-label="Стратегия сообщения клиенту">{strategies.map(([strategy, label]) => <button key={strategy} type="button" role="tab" aria-selected={messageStrategy === strategy} className={messageStrategy === strategy ? 'active' : ''} onClick={() => setMessageStrategy(strategy)}><span>{label}</span>{content.recommended_channel === 'message' && content.recommended_strategy === strategy ? <small>Рекомендуется</small> : null}</button>)}</div><pre>{clientMessage || 'Сообщение пока не сформировано.'}</pre></section> : null}
    {showSecondary ? <section className={revealClassName('dc-manager-answer-copy speech', animate)}><div><h4>Речевой модуль</h4><button className="dc-button" disabled={!callScript} onClick={() => void onCopy(callScript, 'Речевой модуль')}>Скопировать</button></div><div className="dc-manager-tone-tabs" role="tablist" aria-label="Стратегия речевого модуля">{strategies.map(([strategy, label]) => <button key={strategy} type="button" role="tab" aria-selected={callStrategy === strategy} className={callStrategy === strategy ? 'active' : ''} onClick={() => setCallStrategy(strategy)}><span>{label}</span>{content.recommended_channel === 'call' && content.recommended_strategy === strategy ? <small>Рекомендуется</small> : null}</button>)}</div><pre>{callScript || 'Речевой модуль пока не сформирован.'}</pre></section> : null}
  </div>
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

function callBlockSpeech(block: ManagerFullScriptBlock) {
  if ('spoken_text' in block) return block.spoken_text
  return block.suggested_phrases.filter(Boolean).join('\n')
}

function CallSpeechIcon() {
  return <span className="dc-call-script-speech-icon" aria-hidden="true">
    <svg viewBox="0 0 24 24"><path d="M5 6h14v10H9l-4 3V6Z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
  </span>
}

export function CallScriptResultView({ script }: { script: ManagerCallScriptContent }) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [activeStep, setActiveStep] = useState(0)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    setCollapsed({})
    setActiveStep(0)
  }, [script.conversation_goal, script.blocks.length])

  useEffect(() => {
    const root = scrollRef.current
    if (!root) return
    const onScroll = () => {
      const marker = root.getBoundingClientRect().top + 120
      const stages = [...root.querySelectorAll('[data-call-step]')]
      let current = 0
      stages.forEach((node, index) => {
        if (node.getBoundingClientRect().top <= marker) current = index
      })
      setActiveStep(current)
    }
    root.addEventListener('scroll', onScroll, { passive: true })
    return () => root.removeEventListener('scroll', onScroll)
  }, [script])

  function toggleStage(id: string) {
    setCollapsed((current) => ({ ...current, [id]: !current[id] }))
  }

  return <div className="dc-call-script-scroll" ref={scrollRef}>
    {script.blocks.map((block, index) => {
      const speech = callBlockSpeech(block)
      const isCollapsed = Boolean(collapsed[block.block_id])
      const extraOpen = Boolean(block.objective?.trim() || block.listen_for.length || block.transition?.trim())
      return <article className={`dc-call-script-stage ${index === activeStep ? 'active' : ''} ${isCollapsed ? 'collapsed' : ''}`} data-call-step={index} key={block.block_id}>
        <div className="dc-call-script-stage-number">{index + 1}</div>
        <div className="dc-call-script-stage-head">
          <h3>{block.title}</h3>
          <button type="button" aria-label={isCollapsed ? 'Развернуть шаг' : 'Свернуть шаг'} onClick={() => toggleStage(block.block_id)}>{isCollapsed ? '⌄' : '⌃'}</button>
        </div>
        {isCollapsed ? null : <div className="dc-call-script-stage-body">
          {speech ? <div className="dc-call-script-speech"><CallSpeechIcon />{speech}</div> : null}
          {extraOpen ? <details className="dc-call-script-more">
            <summary>Подробнее</summary>
            {block.objective?.trim() ? <p><b>Цель шага.</b> {block.objective}</p> : null}
            {block.listen_for.length ? <p><b>Услышать:</b> {block.listen_for.join(' · ')}</p> : null}
            {block.transition?.trim() ? <p><b>Переход:</b> {block.transition}</p> : null}
          </details> : null}
        </div>}
      </article>
    })}
    {script.closing_agreement?.trim() ? <article className={`dc-call-script-stage ${activeStep === script.blocks.length ? 'active' : ''} ${collapsed.__close ? 'collapsed' : ''}`} data-call-step={script.blocks.length}>
      <div className="dc-call-script-stage-number">{script.blocks.length + 1}</div>
      <div className="dc-call-script-stage-head">
        <h3>Резюме и следующий шаг</h3>
        <button type="button" aria-label={collapsed.__close ? 'Развернуть шаг' : 'Свернуть шаг'} onClick={() => toggleStage('__close')}>{collapsed.__close ? '⌄' : '⌃'}</button>
      </div>
      {collapsed.__close ? null : <div className="dc-call-script-stage-body">
        <div className="dc-call-script-speech"><CallSpeechIcon />{script.closing_agreement}</div>
      </div>}
    </article> : null}
  </div>
}

export function ConversationScriptResultView({
  script,
  objections = [],
}: {
  script: Exclude<ManagerFullScriptContent, ManagerEmailContent>
  objections?: ManagerObjectionHandling['items']
}) {
  return <>
    <section className="dc-manager-full-script-goal"><small>Цель разговора</small><strong>{script.conversation_goal}</strong></section>
    <ol>{script.blocks.map((block, index) => {
      const linkedObjections = objections.filter((item) => block.relevant_objection_ids?.includes(item.objection_id))
      return <li key={block.block_id}>
        <span>{index + 1}</span>
        <article>
          <h3>{block.title}</h3>
          <p>{block.objective}</p>
          <div className="dc-manager-full-script-phrases">{'spoken_text' in block ? <><blockquote>{block.spoken_text}</blockquote>{block.clarifying_question?.trim() ? <div className="dc-manager-full-script-clarify"><small>Если нужно уточнить</small><blockquote className="clarify">{block.clarifying_question}</blockquote></div> : null}</> : block.suggested_phrases.map((phrase) => <blockquote key={phrase}>{phrase}</blockquote>)}</div>
          {block.listen_for.length ? <div className="dc-manager-full-script-listen"><strong>Услышать:</strong> {block.listen_for.join(' · ')}</div> : null}
          {linkedObjections.length ? <div className="dc-manager-block-objections"><strong>Если возникнет возражение</strong>{linkedObjections.map((item) => <div key={item.objection_id}><b>{item.objection}</b><span>{item.manager_reply}</span><small>{item.follow_up_question}</small></div>)}</div> : null}
          <footer><strong>Переход:</strong> {block.transition}</footer>
        </article>
      </li>
    })}</ol>
    <section className="dc-manager-full-script-close"><small>{isCallScriptContent(script) ? 'Резюме разговора и следующий шаг' : 'Завершить договорённостью'}</small><strong>{script.closing_agreement}</strong></section>
  </>
}

export function EmailScriptResultView({ script }: { script: ManagerEmailContent }) {
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
  </section>
}

export function FullScriptResultView({
  script,
  onCopy,
  objections = [],
  showCopy = true,
}: {
  script: ManagerFullScriptContent | ManagerEmailContent
  onCopy: (text: string, label: string) => Promise<void>
  objections?: ManagerObjectionHandling['items']
  showCopy?: boolean
}) {
  if ('email_contract' in script) {
    const copyText = [script.subject, '', script.greeting, script.context, ...script.questions.map((question, index) => `${index + 1}. ${question}`), script.value_point, script.call_to_action, script.closing].join('\n\n')
    return <>
      <EmailScriptResultView script={script} />
      {showCopy ? <button className="dc-button" onClick={() => void onCopy(copyText, 'Email клиенту')}>Скопировать</button> : null}
    </>
  }
  if (isCallScriptContent(script)) {
    return <>
      {script.conversation_goal ? <section className="dc-manager-full-script-goal"><small>Цель разговора</small><strong>{script.conversation_goal}</strong></section> : null}
      <CallScriptResultView script={script} />
      {showCopy ? <button className="dc-button" onClick={() => void onCopy(script.conversation_goal, 'Сценарий звонка')}>Скопировать цель</button> : null}
    </>
  }
  return <>
    <ConversationScriptResultView script={script} objections={objections} />
    {showCopy ? <button className="dc-button" onClick={() => void onCopy(script.conversation_goal, 'Продолжение переписки')}>Скопировать цель</button> : null}
  </>
}
