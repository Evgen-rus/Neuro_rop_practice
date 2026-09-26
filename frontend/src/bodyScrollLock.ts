/**
 * Блокировка прокрутки страницы под модальными слоями.
 *
 * Раньше в четырёх компонентах стояла одна и та же связка «запомнить
 * `body.style.overflow` → поставить `hidden` → вернуть запомненное». Она
 * ломается, когда модальные слои закрываются не в порядке открытия:
 *
 *   1. открылся оверлей карточки сделки: запомнил `''`, поставил `hidden`
 *   2. открылся «Дожим» поверх: запомнил `hidden`, поставил `hidden`
 *   3. оверлей закрылся первым: вернул `''` — скролл ожил под «Дожимом»
 *   4. «Дожим» закрылся: вернул `hidden` — страница осталась заблокированной
 *
 * Ровно это и происходило: скролл пропадал на всех экранах с deals и
 * возвращался только после перезагрузки, которая сбрасывает инлайновые
 * стили. Оверлей сделки и «Дожим» открываются одновременно, так что
 * порядок размонтирования задаётся не только кликами.
 *
 * Здесь прокрутка снимается по счётчику: пока хоть один слой держит
 * блокировку, `overflow` остаётся `hidden`, и порядок закрытия не важен.
 */

/** Сколько слоёв сейчас держат прокрутку. */
let holders = 0

/** Значения, которые слои перезаписали: восстанавливаются при отпускании. */
let previousOverflow = ''
let previousPaddingRight = ''

/**
 * Заблокировать прокрутку. Вернуть функцию, которая отпускает блокировку;
 * повторный вызов функции-ответа безопасен и ничего не делает.
 */
export function lockBodyScroll() {
  if (holders === 0) {
    previousOverflow = document.body.style.overflow
    previousPaddingRight = document.body.style.paddingRight
    // Компенсация исчезнувшего скроллбара: без неё страница под оверлеем
    // «прыгает» на ширину полосы при открытии и обратно при закрытии.
    const scrollbarWidth = window.innerWidth - document.documentElement.clientWidth
    document.body.style.overflow = 'hidden'
    if (scrollbarWidth > 0) document.body.style.paddingRight = `${scrollbarWidth}px`
  }
  holders += 1
  let released = false
  return () => {
    if (released) return
    released = true
    holders = Math.max(0, holders - 1)
    if (holders > 0) return
    document.body.style.overflow = previousOverflow
    document.body.style.paddingRight = previousPaddingRight
    previousOverflow = ''
    previousPaddingRight = ''
  }
}

/** Текущее число держателей: используется в тестах. */
export function bodyScrollLockHolders() {
  return holders
}
