export const WINDOW_EVENT_CATEGORIES = [
  'deals',
  'leads',
  'communications',
  'tasks',
  'crm',
  'neurorop',
] as const

export type WindowEventCategory = (typeof WINDOW_EVENT_CATEGORIES)[number]
export type PageEventCategory = 'all' | WindowEventCategory

export type WindowEventLike = {
  entity_type?: string | null
  entity_id?: string | null
  category: string
}

export function defaultWindowCategories(pageCategory: PageEventCategory): WindowEventCategory[] {
  if (pageCategory === 'all') return [...WINDOW_EVENT_CATEGORIES]
  return [pageCategory]
}

export function allWindowCategoriesSelected(selected: readonly WindowEventCategory[]): boolean {
  return selected.length === WINDOW_EVENT_CATEGORIES.length
}

export function toggleAllWindowCategories(selected: readonly WindowEventCategory[]): WindowEventCategory[] {
  return allWindowCategoriesSelected(selected) ? [] : [...WINDOW_EVENT_CATEGORIES]
}

export function toggleWindowCategory(
  selected: readonly WindowEventCategory[],
  category: WindowEventCategory,
): WindowEventCategory[] {
  const next = new Set(selected)
  if (next.has(category)) next.delete(category)
  else next.add(category)
  return WINDOW_EVENT_CATEGORIES.filter((item) => next.has(item))
}

export function eventMatchesWindowCategory(event: WindowEventLike, category: WindowEventCategory): boolean {
  if (category === 'deals') return event.entity_type === 'deal'
  if (category === 'leads') return event.entity_type === 'lead'
  return event.category === category
}

export function filterWindowEvents<T extends WindowEventLike>(
  events: readonly T[],
  selected: readonly WindowEventCategory[],
): T[] {
  if (allWindowCategoriesSelected(selected)) return [...events]
  if (!selected.length) return []
  return events.filter((event) => selected.some((category) => eventMatchesWindowCategory(event, category)))
}

export function windowVisibleSummary(events: readonly WindowEventLike[]): { events: number; entities: number } {
  const keys = new Set(
    events
      .filter((item) => item.entity_id)
      .map((item) => `${item.entity_type || ''}:${item.entity_id}`),
  )
  return { events: events.length, entities: keys.size }
}

export function windowExportFilename(managerId: string, fromIso: string, toIso: string): string {
  const date = fromIso.slice(0, 10)
  const from = fromIso.slice(11, 16).replace(':', '-')
  const to = toIso.slice(11, 16).replace(':', '-')
  return `trajectory-manager-${managerId}-${date}-${from}_${to}.json`
}

export function filenameFromContentDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback
  const utfMatch = /filename\*=UTF-8''([^;]+)/i.exec(header)
  if (utfMatch) {
    try {
      return decodeURIComponent(utfMatch[1])
    } catch {
      return utfMatch[1]
    }
  }
  const quoted = /filename="([^"]+)"/i.exec(header)
  if (quoted) return quoted[1]
  const plain = /filename=([^;]+)/i.exec(header)
  return plain ? plain[1].trim() : fallback
}

export function buildWindowExport(params: {
  manager_id: string
  manager_name: string
  period: { from: string; to: string }
  categories: readonly WindowEventCategory[]
  q?: string
  events: unknown[]
}) {
  const summary = windowVisibleSummary(params.events as WindowEventLike[])
  return {
    manager_id: params.manager_id,
    manager_name: params.manager_name,
    period: params.period,
    categories: [...params.categories],
    q: (params.q || '').trim(),
    events: params.events,
    event_count: summary.events,
    entities: summary.entities,
  }
}

export function saveBlobDownload(filename: string, blob: Blob) {
  const url = URL.createObjectURL(blob)
  try {
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

export function downloadJsonObject(filename: string, payload: unknown) {
  const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: 'application/json;charset=utf-8' })
  saveBlobDownload(filename, blob)
}
