import { useEffect, useState } from 'react'

/**
 * Точка перелома, ниже которой карточка сделки показывается оверлеем.
 * Ровно то же значение, что у `@media (max-width: 1050px)` в index.css,
 * где `.dc-workspace` перестаёт быть двухколоночной сеткой. Держим одну
 * точку: если JS и CSS разойдутся, карточка откроется поверх вёрстки.
 */
export const DEAL_OVERLAY_MOBILE_QUERY = '(max-width: 1050px)'

/** Читает текущее состояние точки перелома по требованию. */
export function isNarrowDealLayout() {
  return typeof window !== 'undefined' && window.matchMedia(DEAL_OVERLAY_MOBILE_QUERY).matches
}

/** Подписка на мобильную ширину: рендер переключается на границе вьюпорта. */
export function useNarrowDealLayout() {
  const [narrow, setNarrow] = useState(() => isNarrowDealLayout())

  useEffect(() => {
    const query = window.matchMedia(DEAL_OVERLAY_MOBILE_QUERY)
    const sync = () => setNarrow(query.matches)
    sync()
    query.addEventListener('change', sync)
    return () => query.removeEventListener('change', sync)
  }, [])

  return narrow
}
