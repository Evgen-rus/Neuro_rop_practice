import { useState, type SyntheticEvent } from 'react'

import {
  fetchDealCallTranscript,
  fetchDealCommunicationContent,
  type DealCallTranscript,
  type DealCommunicationContent,
} from './api'


type LoadedContent = DealCallTranscript | DealCommunicationContent

const contentCache = new Map<string, LoadedContent>()
const TEXT_CHANNELS = new Set(['email', 'message', 'whatsapp', 'telegram', 'max'])


function contentLabel(channel: string) {
  if (channel === 'call') return 'Расшифровка звонка'
  if (channel === 'email') return 'Текст письма'
  if (channel === 'whatsapp') return 'Сообщение WhatsApp'
  if (channel === 'telegram') return 'Сообщение Telegram'
  if (channel === 'max') return 'Сообщение Max'
  return 'Текст сообщения'
}


export function CommunicationContent({
  dealId,
  eventId,
  channel,
}: {
  dealId: string
  eventId: string
  channel: string
}) {
  const normalizedChannel = channel.toLowerCase()
  const canLoad = normalizedChannel === 'call' || TEXT_CHANNELS.has(normalizedChannel)
  const cacheKey = `${dealId}:${eventId}:${normalizedChannel}`
  const cached = contentCache.get(cacheKey) || null
  const [content, setContent] = useState<LoadedContent | null>(cached)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  async function load(event: SyntheticEvent<HTMLDetailsElement>) {
    if (!event.currentTarget.open || content || loading || !canLoad) return
    setLoading(true)
    setError('')
    try {
      const result = normalizedChannel === 'call'
        ? await fetchDealCallTranscript(dealId, eventId)
        : await fetchDealCommunicationContent(dealId, eventId)
      contentCache.set(cacheKey, result)
      setContent(result)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось загрузить текст коммуникации')
    } finally {
      setLoading(false)
    }
  }

  if (!canLoad) return null
  const isExcerpt = content && 'is_excerpt' in content && content.is_excerpt
  return (
    <details className="dc-call-transcript" onToggle={(event) => void load(event)}>
      <summary>{contentLabel(normalizedChannel)}</summary>
      {loading ? <p role="status">Загружаем текст…</p> : null}
      {error ? <p className="error">{error}</p> : null}
      {content ? (
        <>
          <pre>{content.text}</pre>
          {isExcerpt ? <small>Источник содержит только доступный сохранённый фрагмент.</small> : null}
          {content.truncated ? <small>Показан первый 1 000 000 символов.</small> : null}
        </>
      ) : null}
    </details>
  )
}
