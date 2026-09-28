/**
 * Данные каркаса загрузки контроля сделок.
 *
 * Первым кадром приложение показывает не полноэкранный спиннер, а ту же
 * оболочку с заглушками: рейка, заголовок, плитки метрик, фильтры, шапка
 * таблицы и силуэты строк. Реальный контент доезжает на место, поэтому
 * раскладка не прыгает после ответа сервера.
 *
 * Модуль существует отдельно от компонента, потому что здесь лежит
 * проверяемая логика: состав плиток, подписи колонок и число строк. Сам
 * каркас — чистая разметка без состояния и эффектов.
 */

export type DealControlSkeletonRole = 'admin' | 'rop' | 'manager'
/** `dashboard` и `rop`/`manager` различаются колонками и составом плиток. */
export type DealControlSkeletonView = 'dashboard' | 'tasks'

export type DealControlSkeletonRow = {
  /** Ширина силуэта в процентах от ширины колонки. */
  title: number
  control: number
  stage: number
  amount: number
}

const DASHBOARD_COLUMNS = ['Сделка', 'Контроль', 'Этап', 'Сумма и прогноз оплаты'] as const
const TASK_COLUMNS = ['Сделка', 'Этап', 'Текущая задача', 'Срок', 'Выполнение'] as const

/** Метрики повторяют `Kpis` в `DealControl.tsx`, чтобы плитки сели в ту же сетку. */
const DASHBOARD_METRICS: ReadonlyArray<readonly [string, string]> = [
  ['◇', 'Сделок'],
  ['₽', 'Портфель'],
  ['▣', 'Задачи на сегодня'],
  ['◷', 'Просрочено'],
]

const TASK_METRICS: ReadonlyArray<readonly [string, string]> = [
  ['◇', 'Всего задач на контроле'],
  ['◷', 'Просрочено'],
  ['▣', 'На сегодня'],
  ['▤', 'На завтра'],
  ['✓', 'Выполнено сегодня'],
  ['↪', 'Перенесено сегодня'],
]

/**
 * Ширины силуэтов. Список повторяется по кругу, чтобы восемь строк
 * выглядели разными: одинаковые полоски читаются как статичный блоб.
 */
const ROW_WIDTHS: readonly DealControlSkeletonRow[] = [
  { title: 92, control: 74, stage: 68, amount: 78 },
  { title: 78, control: 62, stage: 82, amount: 68 },
  { title: 88, control: 80, stage: 60, amount: 84 },
  { title: 70, control: 68, stage: 74, amount: 70 },
  { title: 95, control: 58, stage: 78, amount: 76 },
  { title: 82, control: 78, stage: 64, amount: 72 },
  { title: 86, control: 66, stage: 86, amount: 80 },
  { title: 74, control: 72, stage: 70, amount: 66 },
]

/** Сколько строк-силуэтов рисуем до первого ответа сервера. */
export const DEAL_CONTROL_SKELETON_ROWS = 8

export function dealControlSkeletonMetrics(view: DealControlSkeletonView): ReadonlyArray<readonly [string, string]> {
  return view === 'dashboard' ? DASHBOARD_METRICS : TASK_METRICS
}

export function dealControlSkeletonColumns(view: DealControlSkeletonView): readonly string[] {
  return view === 'dashboard' ? DASHBOARD_COLUMNS : TASK_COLUMNS
}

export function dealControlSkeletonRows(count = DEAL_CONTROL_SKELETON_ROWS): DealControlSkeletonRow[] {
  const total = Math.max(0, Math.trunc(count))
  return Array.from({ length: total }, (_, index) => ROW_WIDTHS[index % ROW_WIDTHS.length])
}

/** Какие пункты рейки вообще доступны роли — общий ключ с настоящим экраном. */
export const DEAL_CONTROL_SKELETON_NAV: ReadonlyArray<{ view: string; icon: string; label: string }> = [
  { view: 'dashboard', icon: '▦', label: 'Дашборд' },
  { view: 'rop', icon: '◎', label: 'Контроль РОПа' },
  { view: 'daily', icon: '▣', label: 'Ежедневный контроль' },
  { view: 'manager', icon: '✓', label: 'Задачи менеджера' },
  { view: 'trajectory', icon: '⌁', label: 'Траектория' },
  { view: 'shadow', icon: '↯', label: 'Learning Shadow' },
  { view: 'spend', icon: '₽', label: 'Расходы AI' },
  { view: 'team', icon: '◍', label: 'Команда' },
]

/**
 * Иконки и подписи рейки повторяют настоящий экран, чтобы переход был
 * бесшовным. Активный пункт берётся из сохранённого вида: он известен
 * до первого ответа сервера, иначе каркас мигал бы не тем разделом.
 */
export function dealControlSkeletonNav(
  role: DealControlSkeletonRole,
  activeView: string,
): Array<{ icon: string; label: string; active: boolean }> {
  const canOpenRopView = role === 'admin' || role === 'rop'
  const canOpenManagerView = role === 'admin' || role === 'rop' || role === 'manager'
  return DEAL_CONTROL_SKELETON_NAV
    .filter((item) => {
      if (item.view === 'rop' || item.view === 'daily') return canOpenRopView
      if (item.view === 'manager') return canOpenManagerView
      return item.view === 'dashboard' || role === 'admin'
    })
    .map((item) => ({
      icon: item.icon,
      // Менеджер видит свой раздел как «Мои задачи» — так же, как на экране.
      label: item.view === 'manager' && role === 'manager' ? 'Мои задачи' : item.label,
      active: item.view === activeView,
    }))
}

/**
 * Текст статуса меняется по фактическому ожиданию, а не по выдуманному
 * проценту. Синхронный эндпоинт не отдаёт прогресс, поэтому единственный
 * честный сигнал — «ждём давно».
 */
export function dealControlSkeletonStatus(waitMs: number): string {
  if (waitMs < 1500) return 'Загружаем контроль сделок…'
  if (waitMs < 8000) return 'Собираем портфель сделок, это может занять время…'
  return 'Долго собираем портфель. Если так долго, проверьте API — можно попробовать снова.'
}
