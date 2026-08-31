import {
  ApiError,
  fetchDealCallTranscript,
  fetchDealCommunicationThread,
  type DealCallTranscript,
  type DealCommunicationThread,
} from './api'

export type CommunicationDialogTarget = {
  dealId: string
  eventId: string
  channel: string
  title?: string | null
  occurredAt?: string | null
}

export function communicationContentLabel(channel: string) {
  if (channel === 'call') return 'Расшифровка звонка'
  if (channel === 'email') return 'Текст письма'
  if (channel === 'whatsapp') return 'Диалог WhatsApp'
  if (channel === 'telegram') return 'Диалог Telegram'
  if (channel === 'max') return 'Диалог Max'
  return 'Диалог сообщений'
}

export type CommunicationPayload =
  | { kind: 'transcript'; value: DealCallTranscript }
  | { kind: 'thread'; value: DealCommunicationThread }

export type CommunicationDialogState = {
  open: boolean
  target: CommunicationDialogTarget | null
  phase: 'idle' | 'loading' | 'ready' | 'error'
  payload: CommunicationPayload | null
  error: string
}

export type CommunicationDialogAction =
  | { type: 'OPEN'; target: CommunicationDialogTarget; cached?: CommunicationPayload | null }
  | { type: 'CLOSE' }
  | { type: 'FETCH_OK'; targetKey: string; payload: CommunicationPayload }
  | { type: 'FETCH_ERROR'; targetKey: string; error: string }

export const initialCommunicationDialogState: CommunicationDialogState = {
  open: false,
  target: null,
  phase: 'idle',
  payload: null,
  error: '',
}

export function communicationRequestKey(target: CommunicationDialogTarget) {
  return `${target.dealId}:${target.eventId}:${target.channel.toLowerCase()}`
}

const payloadCache = new Map<string, CommunicationPayload>()
const anchorIndex = new Map<string, string>()
const inflight = new Map<string, Promise<CommunicationPayload>>()

function payloadKey(target: CommunicationDialogTarget, payload: CommunicationPayload) {
  if (payload.kind === 'thread') {
    return `${target.dealId}:${payload.value.conversation_key}:${payload.value.date}`
  }
  return communicationRequestKey(target)
}

export function getCachedCommunication(target: CommunicationDialogTarget) {
  const requestKey = communicationRequestKey(target)
  return payloadCache.get(anchorIndex.get(requestKey) || requestKey) || null
}

export function storeCommunicationPayload(
  target: CommunicationDialogTarget,
  payload: CommunicationPayload,
) {
  const requestKey = communicationRequestKey(target)
  const key = payloadKey(target, payload)
  payloadCache.set(key, payload)
  anchorIndex.set(requestKey, key)
  if (payload.kind === 'thread') {
    for (const message of payload.value.messages) {
      anchorIndex.set(
        communicationRequestKey({
          dealId: target.dealId,
          eventId: message.event_id,
          channel: message.channel || payload.value.channel,
        }),
        key,
      )
    }
  }
}

export function communicationDialogReducer(
  state: CommunicationDialogState,
  action: CommunicationDialogAction,
): CommunicationDialogState {
  if (action.type === 'CLOSE') return initialCommunicationDialogState
  if (action.type === 'OPEN') {
    return {
      open: true,
      target: action.target,
      phase: action.cached ? 'ready' : 'loading',
      payload: action.cached || null,
      error: '',
    }
  }
  if (!state.target || communicationRequestKey(state.target) !== action.targetKey) return state
  if (action.type === 'FETCH_OK') {
    return { ...state, phase: 'ready', payload: action.payload, error: '' }
  }
  return { ...state, phase: 'error', payload: null, error: action.error }
}

export function communicationErrorMessage(reason: unknown) {
  if (reason instanceof ApiError && reason.status === 404) return 'Текст или диалог недоступны'
  return reason instanceof Error ? reason.message : 'Текст или диалог недоступны'
}

type CommunicationFetchers = {
  transcript: (dealId: string, eventId: string) => Promise<DealCallTranscript>
  thread: (dealId: string, eventId: string) => Promise<DealCommunicationThread>
}

const defaultFetchers: CommunicationFetchers = {
  transcript: fetchDealCallTranscript,
  thread: fetchDealCommunicationThread,
}

export async function loadCommunicationPayload(
  target: CommunicationDialogTarget,
  fetchers: CommunicationFetchers = defaultFetchers,
): Promise<CommunicationPayload> {
  return target.channel.toLowerCase() === 'call'
    ? { kind: 'transcript', value: await fetchers.transcript(target.dealId, target.eventId) }
    : { kind: 'thread', value: await fetchers.thread(target.dealId, target.eventId) }
}

export function loadCachedCommunication(
  target: CommunicationDialogTarget,
  fetchers: CommunicationFetchers = defaultFetchers,
) {
  const cached = getCachedCommunication(target)
  if (cached) return Promise.resolve(cached)
  const requestKey = communicationRequestKey(target)
  const current = inflight.get(requestKey)
  if (current) return current
  const request = loadCommunicationPayload(target, fetchers)
    .then((payload) => {
      storeCommunicationPayload(target, payload)
      return payload
    })
    .finally(() => inflight.delete(requestKey))
  inflight.set(requestKey, request)
  return request
}
