import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

/**
 * Полноэкранный оверлей карточки сделки для мобильной раскладки.
 *
 * Раньше на узком экране `.dc-workspace` становился `display: block`, и
 * карточка — третий элемент DOM — оказывалась после всего списка сделок:
 * на 390px это ~7000px свайпа от тапа по строке до карточки. Здесь карточка
 * открывается поверх списка, а список остаётся под ней: возврат — одно
 * касание, а не прокрутка назад.
 */
export function DealDetailOverlay({ title, subtitle, onClose, children }: {
  title: string
  subtitle?: string
  onClose: () => void
  children: ReactNode
}) {
  const closeRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    // Фокус на кнопке закрытия: с клавиатуры и со скринридера карточка
    // открывается как диалог, а не как блок в конце страницы.
    closeRef.current?.focus()
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return createPortal(
    <div
      className="dc-deal-overlay"
      role="presentation"
      onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}
    >
      <section
        className="dc-deal-overlay-panel"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header className="dc-deal-overlay-head">
          <div className="dc-deal-overlay-heading">
            <strong>{title}</strong>
            {subtitle ? <small>{subtitle}</small> : null}
          </div>
          <button
            ref={closeRef}
            type="button"
            className="dc-deal-overlay-close"
            aria-label="Закрыть карточку сделки"
            onClick={onClose}
          >×</button>
        </header>
        <div className="dc-deal-overlay-body">
          {children}
        </div>
        <footer className="dc-deal-overlay-foot">
          <button
            type="button"
            className="dc-button dc-deal-overlay-back"
            onClick={onClose}
          >Назад к списку</button>
        </footer>
      </section>
    </div>,
    document.body
  )
}
