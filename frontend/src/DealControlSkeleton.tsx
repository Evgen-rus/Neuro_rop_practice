import { useEffect, useState } from 'react'
import {
  DEAL_CONTROL_SKELETON_ROWS,
  dealControlSkeletonColumns,
  dealControlSkeletonMetrics,
  dealControlSkeletonNav,
  dealControlSkeletonRows,
  dealControlSkeletonStatus,
  type DealControlSkeletonRole,
} from './dealControlSkeletonView'
import './dealControlSkeleton.css'

/** `dashboard` и `tasks` рисуют таблицу сделок, `other` — прочие разделы. */
export type DealControlSkeletonSection = 'dashboard' | 'tasks' | 'other'

/**
 * Каркас контроля сделок на время первой загрузки.
 *
 * Раньше экран целиком заменялся спиннером на голом холсте: было видно,
 * что приложение открылось, но не видно, что оно уже почти готово. Здесь
 * первым кадром рисуется та же оболочка — рейка, заголовок, плитки,
 * фильтры, шапка таблицы и силуэты строк, — поэтому контент доезжает на
 * место без скачка раскладки.
 *
 * Каркас неинтерактивен: подписи рейки и фильтры — текст, а не кнопки,
 * чтобы ими нельзя было нажать до прихода данных.
 */
export function DealControlSkeleton(props: {
  role: DealControlSkeletonRole
  section: DealControlSkeletonSection
  activeNav: string
  title: string
  /** Момент старта загрузки: статус текста считается от него. */
  startedAt: number
}) {
  const [waitMs, setWaitMs] = useState(0)

  useEffect(() => {
    if (props.startedAt > Date.now()) {
      // Разница часов или перевод часов: не крутим таймер в пустоту.
      setWaitMs(0)
      return
    }
    const tick = () => setWaitMs(Date.now() - props.startedAt)
    tick()
    const timer = window.setInterval(tick, 1000)
    return () => window.clearInterval(timer)
  }, [props.startedAt])

  const dashboard = props.section === 'dashboard'
  const columns = dealControlSkeletonColumns(dashboard ? 'dashboard' : 'tasks')
  const rows = dealControlSkeletonRows(DEAL_CONTROL_SKELETON_ROWS)
  // Прочие разделы (расходы, команда, траектория) таблицу сделок не
  // показывают — рисовать её в каркасе значило бы соврать о содержании.
  const withDeals = props.section !== 'other'

  return <main className="dc-shell dc-skeleton" aria-busy="true">
    <aside className="dc-sidebar" aria-hidden="true">
      <div className="dc-menu-head">
        <span className="dc-menu-button"><span>☰</span></span>
        <b className="dc-menu-logo">НейроРОП</b>
      </div>      <nav>
        {dealControlSkeletonNav(props.role, props.activeNav).map((item) => <div
          key={item.label}
          className={`dc-skeleton-nav-item ${item.active ? 'active' : ''}`}
        >
          <span>{item.icon}</span><b>{item.label}</b>
        </div>)}
      </nav>
    </aside>

    <section className="dc-content">
      <header className="dc-header">
        <div className="dc-header-title">
          <h1>{props.title}</h1>
          <p className="dc-skeleton-status" role="status">{dealControlSkeletonStatus(waitMs)}</p>
        </div>
        <div className="dc-refresh" aria-hidden="true"><span>Обновляем данные…</span></div>
      </header>

      <section className={`dc-kpis ${props.section === 'other' ? '' : dashboard ? 'dashboard' : 'tasks'}`} aria-hidden="true">
        {dealControlSkeletonMetrics(dashboard ? 'dashboard' : 'tasks').map(([icon, label]) => <article key={label} className="dc-skeleton-tile">
          <span>{icon}</span><div><small>{label}</small><i className="dc-skeleton-bar" style={{ width: '58%' }} /></div>
        </article>)}
      </section>

      {withDeals ? <section className="dc-filters" aria-hidden="true">
        <i className="dc-skeleton-field" />
        <i className="dc-skeleton-field" />
        <i className="dc-skeleton-field wide" />
      </section> : null}

      {withDeals ? <div className="dc-workspace">
        <section className="dc-board" aria-hidden="true">
          <nav className="dc-time-tabs">
            {['Все', 'Просроченные', 'Сегодня', 'Завтра', 'Будущие'].map((label) => <div className="dc-skeleton-tab" key={label}><b>{label}</b><span>·</span></div>)}
          </nav>
          <div className={`dc-table-wrap ${dashboard ? '' : 'task-table'}`}>
            <div className="dc-table-scroll dc-skeleton-table">
              <div className={dashboard ? 'dc-deal-columns' : 'dc-task-columns'}>
                {columns.map((label) => <span key={label}>{label}</span>)}
              </div>
              {rows.map((row, index) => <div
                className={`${dashboard ? 'dc-deal-row' : 'dc-task-row'} dc-skeleton-row`}
                key={index}
                style={{ animationDelay: `${Math.min(index, 5) * 70}ms` }}
              >
                <i className="dc-skeleton-bar" style={{ width: `${row.title}%` }} />
                <i className="dc-skeleton-bar" style={{ width: `${row.control}%` }} />
                <i className="dc-skeleton-bar" style={{ width: `${row.stage}%` }} />
                <i className="dc-skeleton-bar" style={{ width: `${row.amount}%` }} />
                {dashboard ? null : <i className="dc-skeleton-bar" style={{ width: '70%' }} />}
              </div>)}
            </div>
          </div>
        </section>
        <div className="dc-resizer" aria-hidden="true">⋮</div>
        <section className="dc-detail dc-skeleton-detail" aria-hidden="true">
          <div className="dc-skeleton-detail-head">
            <i className="dc-skeleton-bar" style={{ width: '46%' }} />
            <i className="dc-skeleton-bar" style={{ width: '72%' }} />
          </div>
          <i className="dc-skeleton-bar" style={{ width: '100%' }} />
          <i className="dc-skeleton-bar" style={{ width: '92%' }} />
          <i className="dc-skeleton-bar" style={{ width: '78%' }} />
        </section>
      </div> : <section className="dc-skeleton-panel" aria-hidden="true">
        <i className="dc-skeleton-bar" style={{ width: '38%' }} />
        {Array.from({ length: 5 }, (_, index) => <i
          className="dc-skeleton-bar"
          key={index}
          style={{ width: `${94 - index * 6}%`, animationDelay: `${Math.min(index, 5) * 70}ms` }}
        />)}
      </section>}
    </section>
  </main>
}
