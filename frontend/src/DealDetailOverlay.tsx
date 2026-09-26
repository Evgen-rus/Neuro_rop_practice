import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { lockBodyScroll } from './bodyScrollLock'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/** Фокусируемые элементы панели в порядке обхода. */
function focusableWithin(panel: HTMLElement) {
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
    .filter((node) => node.offsetParent !== null || node === document.activeElement)
}

/**
    * Слои, которые могут перекрыть карточку сделки. Все они `position: fixed`
    * и порталятся в `document.body`.
    */
const MODAL_LAYERS = [
  '.dc-deal-overlay',
  '.dc-manager-assistant-layer',
  '.dc-manager-full-script-layer',
  '.dc-modal-layer',
  '.dc-comments-modal',
].join(',')

/** Вычисленный z-index слоя; `auto` и мусор трактуем как 0. */
function layerZIndex(node: HTMLElement) {
  const value = Number.parseInt(getComputedStyle(node).zIndex, 10)
  return Number.isNaN(value) ? 0 : value
}

/**
    * Есть ли модальный слой выше карточки.
    *
    * Сравнение идёт по z-index, а не по порядку DOM: карточка смонтирована
    * раньше «Дожима», но «Дожим» её перекрывает и должен первым получать
    * Escape и фокус. Порядок задаётся токенами `--dc-layer-*` в `index.css`,
    * и это единственное место, где он записан.
    */
function hasModalAbove(panel: HTMLElement) {
  const overlay = panel.closest<HTMLElement>('.dc-deal-overlay')
  if (!overlay) return false
  const own = layerZIndex(overlay)
  return Array.from(document.querySelectorAll<HTMLElement>(MODAL_LAYERS))
    .some((node) => node !== overlay && node.isConnected && layerZIndex(node) > own)
}

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
  const panelRef = useRef<HTMLElement | null>(null)
  const closeRef = useRef<HTMLButtonElement | null>(null)
  // Инициатор — то, что открыло карточку. Фокус возвращается на него при
  // закрытии, иначе после 60+ интерактивных элементов внутри карточки
  // навигация начинается заново от шапки.
  const openerRef = useRef<HTMLElement | null>(null)
  // `onClose` приходит новой стрелкой на каждом рендере. Если оставить его
  // в зависимостях, эффект перезапускается, а его cleanup возвращает
  // `overflow` и фокус — оверлей «моргает» и страница под ним оживает.
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    openerRef.current = document.activeElement as HTMLElement | null
    const panel = panelRef.current

    // Фокус на кнопке закрытия: с клавиатуры и со скринридера карточка
    // открывается как диалог, а не как блок в конце страницы.
    closeRef.current?.focus()

    // Страница под оверлеем оставалась прокручиваемой: свайп по карточке
    // уводил список, и «Назад к списку» возвращал не туда. Блокируем
    // прокрутку и возвращаем позицию при закрытии.
    //
    // Через общий счётчик, а не «запомнить и вернуть»: оверлей открывается
    // вместе с «Дожимом», и при закрытии в обратном порядке старая схема
    // оставляла `body` заблокированным навсегда.
    const releaseScroll = lockBodyScroll()

    const onKeyDown = (event: KeyboardEvent) => {
      if (!panel) return
      // Над карточкой открыт «Дожим», сценарий или подтверждение анализа.
      // Их клавиши обрабатывают их же компоненты; если оверлей продолжит
      // ловить клавиши, Escape закроет карточку вместо верхнего диалога,
      // а ловушка фокуса вытащит фокус из «Дожима» обратно в оверлей.
      if (hasModalAbove(panel)) return
      if (event.key === 'Escape') {
        event.preventDefault()
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab') return
      // Ловушка фокуса: `aria-modal="true"` объявляет фон неактивным, но
      // без неё Tab уводил на элементы страницы под оверлеем.
      const items = focusableWithin(panel)
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || !panel.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      releaseScroll()
      // Возвращаем фокус инициатору, только если он ещё в документе: после
      // смены вида или фильтра React мог размонтировать строку, и фокус на
      // отсоединённый узел оставлял страницу без активного элемента.
      const opener = openerRef.current
      if (opener && opener.isConnected) opener.focus()
    }
  }, [])

  return createPortal(
    <div
      className="dc-deal-overlay"
      role="presentation"
      // `onPointerDown`, а не `onMouseDown`: тот же приём уже используется
      // для ресайзера панелей, и он одинаково работает мышью и пальцем.
      onPointerDown={(event) => { if (event.target === event.currentTarget) onClose() }}
    >
      <section
        ref={panelRef}
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
