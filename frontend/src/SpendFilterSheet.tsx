import { useEffect, useRef } from 'react'
import { lockBodyScroll } from './bodyScrollLock'
import {
  SPEND_CHART_METRICS,
  SPEND_DEFAULT_KIND,
  SPEND_DEFAULT_METRIC,
  type SpendChartMetric,
  type SpendKindFilter,
} from './aiSpendView'

/**
 * Мобильная панель фильтров графика.
 *
 * На узком экране одиннадцать плашек занимали три строки и вытесняли сам
 * график, поэтому обе группы — тип операции и единица измерения — свёрнуты
 * в одну кнопку. Панель пишет прямо в те же значения, что и десктопные
 * плашки, иначе мобильный и десктоп разошлись бы по данным.
 *
 * Выбор сразу применяется и закрывает панель: держать её открытой ради
 * кнопки «Готово» значило бы делать лишний тап ради результата, который
 * и так виден сразу под панелью.
 *
 * Тап по уже выбранному пункту тоже закрывает панель и ничего не меняет.
 * Обязательные два тапа — «сначала тип, потом показатель» — здесь были бы
 * ловушкой: чтобы вернуть показатель к расходам, пришлось бы второй раз
 * жать «Расходы», уже стоящие по умолчанию, и по первому тапу казалось бы,
 * что панель зависла.
 */
export function SpendFilterSheet({
  kindOptions,
  kindFilter,
  metric,
  onKind,
  onMetric,
  onClose,
}: {
  kindOptions: { id: SpendKindFilter; label: string }[]
  kindFilter: SpendKindFilter
  metric: SpendChartMetric
  onKind: (value: SpendKindFilter) => void
  onMetric: (value: SpendChartMetric) => void
  onClose: () => void
}) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const restoreFocusRef = useRef<Element | null>(null)

  useEffect(() => {
    restoreFocusRef.current = document.activeElement
    const release = lockBodyScroll()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      // Фокус не должен уходить на страницу под подложкой: на телефоне это
      // означало бы, что шторка закрылась, но экран остался в фокусе у
      // невидимых кнопок.
      if (event.key !== 'Tab') return
      const panel = dialogRef.current
      if (!panel) return
      const focusable = panel.querySelectorAll<HTMLElement>('button:not([disabled])')
      if (!focusable.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      release()
      if (restoreFocusRef.current instanceof HTMLElement) restoreFocusRef.current.focus()
    }
  }, [onClose])

  const changed = kindFilter !== SPEND_DEFAULT_KIND || metric !== SPEND_DEFAULT_METRIC

  return <div
    className="ai-spend-filter-layer"
    onClick={(event) => { if (event.target === event.currentTarget) onClose() }}
  >
    <div
      ref={dialogRef}
      className="ai-spend-filter-sheet"
      role="dialog"
      aria-modal="true"
      aria-label="Что показывать на графике"
    >
      <header>
        <h2>Показать на графике</h2>
        <button type="button" className="ai-spend-filter-close" onClick={onClose} aria-label="Закрыть">×</button>
      </header>
      <div className="ai-spend-filter-body">
        <section>
          <h3>Тип операции</h3>
          <div className="ai-spend-filter-options">
            {kindOptions.map((item) => <button
              key={item.id}
              type="button"
              className={kindFilter === item.id ? 'active' : ''}
              aria-pressed={kindFilter === item.id}
              onClick={() => { onKind(item.id); onClose() }}
            >{item.label}</button>)}
          </div>
        </section>
        <section>
          {/* Единица измерения — не фильтр, а переключатель того, что считаем.
              Без подписи обе группы читались как один список из восьми
              равнозначных пунктов, и было непонятно, что они разного рода. */}
          <h3>Показатель</h3>
          <div className="ai-spend-filter-options">
            {SPEND_CHART_METRICS.map((item) => <button
              key={item.id}
              type="button"
              className={metric === item.id ? 'active' : ''}
              aria-pressed={metric === item.id}
              onClick={() => { onMetric(item.id); onClose() }}
            >{item.label}</button>)}
          </div>
        </section>
        {changed ? <button type="button" className="ai-spend-filter-reset" onClick={() => {
          onKind(SPEND_DEFAULT_KIND)
          onMetric(SPEND_DEFAULT_METRIC)
          onClose()
        }}>Сбросить</button> : null}
      </div>
    </div>
  </div>
}
