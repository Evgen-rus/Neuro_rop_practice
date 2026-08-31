import {
  useCallback,
  useEffect,
  useReducer,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'

import { formatMoscowDateTime } from './dateTime'
import {
  communicationContentLabel,
  communicationDialogReducer,
  communicationErrorMessage,
  communicationRequestKey,
  getCachedCommunication,
  initialCommunicationDialogState,
  loadCachedCommunication,
  type CommunicationDialogTarget,
} from './communicationDialog'
import { CommunicationDialogContext, useCommunicationDialog } from './communicationDialogContext'

const TEXT_CHANNELS = new Set(['email', 'message', 'whatsapp', 'telegram', 'max'])


export function CommunicationDialogProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(communicationDialogReducer, initialCommunicationDialogState)
  const openCommunication = useCallback((target: CommunicationDialogTarget) => {
    dispatch({ type: 'OPEN', target, cached: getCachedCommunication(target) })
  }, [])
  const closeCommunication = useCallback(() => dispatch({ type: 'CLOSE' }), [])

  useEffect(() => {
    if (!state.open || state.phase !== 'loading' || !state.target) return
    let active = true
    const target = state.target
    const targetKey = communicationRequestKey(target)
    void loadCachedCommunication(target)
      .then((payload) => {
        if (active) dispatch({ type: 'FETCH_OK', targetKey, payload })
      })
      .catch((reason) => {
        if (active) dispatch({
          type: 'FETCH_ERROR',
          targetKey,
          error: communicationErrorMessage(reason),
        })
      })
    return () => { active = false }
  }, [state.open, state.phase, state.target])

  useEffect(() => {
    if (!state.open) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeCommunication()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [closeCommunication, state.open])

  return (
    <CommunicationDialogContext.Provider value={{ openCommunication, closeCommunication }}>
      {children}
      {state.open && state.target ? createPortal(
        <div
          className="dc-modal-layer dc-communication-dialog-layer"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeCommunication()
          }}
        >
          <section
            className="dc-modal dc-communication-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="dc-communication-dialog-title"
          >
            <header>
              <div>
                <small>{state.target.occurredAt
                  ? formatMoscowDateTime(state.target.occurredAt, { dateStyle: 'long', timeStyle: 'short' })
                  : `Сделка #${state.target.dealId}`}</small>
                <h2 id="dc-communication-dialog-title">
                  {state.target.title || communicationContentLabel(state.target.channel)}
                </h2>
              </div>
              <button type="button" autoFocus onClick={closeCommunication} aria-label="Закрыть">×</button>
            </header>
            <div className="dc-communication-dialog-body">
              {state.phase === 'loading' ? (
                <div className="dc-communication-loading" role="status">
                  <span className="dc-spinner" />
                  <span>Загружаем диалог…</span>
                  <i /><i /><i />
                </div>
              ) : null}
              {state.phase === 'error' ? <p className="error" role="alert">{state.error}</p> : null}
              {state.payload?.kind === 'transcript' ? (
                <>
                  <pre className="dc-communication-transcript">{state.payload.value.text}</pre>
                  {state.payload.value.truncated ? <small>Расшифровка показана не полностью.</small> : null}
                </>
              ) : null}
              {state.payload?.kind === 'thread' ? (
                <>
                  <p className="dc-communication-thread-meta">
                    Диалог за {state.payload.value.date} · {state.payload.value.timezone}
                  </p>
                  <ol className="dc-communication-thread">
                    {state.payload.value.messages.map((message) => (
                      <li
                        key={message.event_id}
                        className={`is-${message.direction === 'outgoing' ? 'outgoing' : 'incoming'}`}
                      >
                        <div>
                          <strong>{message.participant_name
                            || (message.direction === 'outgoing' ? 'Менеджер' : 'Клиент')}</strong>
                          <time>{formatMoscowDateTime(message.occurred_at, {
                            hour: '2-digit',
                            minute: '2-digit',
                          })}</time>
                        </div>
                        <p>{message.text}</p>
                        {message.is_excerpt ? <small>Показан доступный сохранённый фрагмент.</small> : null}
                        {message.truncated ? <small>Сообщение показано не полностью.</small> : null}
                      </li>
                    ))}
                  </ol>
                  {state.payload.value.truncated ? <small>Диалог показан не полностью.</small> : null}
                </>
              ) : null}
            </div>
          </section>
        </div>,
        document.body,
      ) : null}
    </CommunicationDialogContext.Provider>
  )
}


export function CommunicationContent({
  dealId,
  eventId,
  channel,
  allowLoad = true,
}: {
  dealId: string
  eventId: string
  channel: string
  allowLoad?: boolean
}) {
  const normalizedChannel = channel.toLowerCase()
  const canLoad = allowLoad && (normalizedChannel === 'call' || TEXT_CHANNELS.has(normalizedChannel))
  const { openCommunication } = useCommunicationDialog()
  if (!canLoad) return null
  return (
    <button
      type="button"
      className="dc-communication-open"
      aria-haspopup="dialog"
      onClick={() => openCommunication({
        dealId,
        eventId,
        channel: normalizedChannel,
      })}
    >
      {communicationContentLabel(normalizedChannel)}
    </button>
  )
}
