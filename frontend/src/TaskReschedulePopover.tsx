import { useEffect, useId, useRef, useState } from 'react'
import type { DailyTaskResult } from './api'
import { formatRescheduleDeadline, newestReschedules } from './taskReschedules'

export function TaskReschedulePopover({ task }: { task?: DailyTaskResult }) {
  const id = useId()
  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const changes = task?.reschedules || []

  useEffect(() => {
    if (!open) return
    const close = (event: Event) => {
      const panel = panelRef.current
      if (event.type === 'scroll' && event.target instanceof Node && panel?.contains(event.target)) return
      panel?.hidePopover()
    }
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => {
      window.removeEventListener('scroll', close, true)
      window.removeEventListener('resize', close)
    }
  }, [open])

  function positionPanel() {
    const panel = panelRef.current
    const trigger = triggerRef.current
    if (!panel || !trigger || !panel.matches(':popover-open')) return
    const anchor = trigger.getBoundingClientRect()
    const bounds = panel.getBoundingClientRect()
    const margin = 12
    const gap = 6
    const below = anchor.bottom + gap
    const top = below + bounds.height <= window.innerHeight - margin ? below : anchor.top - gap - bounds.height
    panel.style.left = `${Math.max(margin, Math.min(anchor.right - bounds.width, window.innerWidth - bounds.width - margin))}px`
    panel.style.top = `${Math.max(margin, Math.min(top, window.innerHeight - bounds.height - margin))}px`
  }

  if (!changes.length) return null
  return <>
    <button
      ref={triggerRef}
      type="button"
      className="dc-reschedule-trigger"
      popoverTarget={id}
      aria-expanded={open}
      aria-controls={id}
      aria-haspopup="dialog"
      onClick={(event) => {
        event.stopPropagation()
        requestAnimationFrame(positionPanel)
      }}
    ><span aria-hidden="true">↪</span> Перенесена · {changes.length}</button>
    <div
      ref={panelRef}
      id={id}
      popover="auto"
      role="dialog"
      aria-labelledby={`${id}-title`}
      className="dc-reschedule-popover"
      onClick={(event) => event.stopPropagation()}
      onToggle={(event) => setOpen(event.newState === 'open')}
    >
      <header>
        <strong id={`${id}-title`}>Переносы срока · МСК</strong>
        <button type="button" popoverTarget={id} popoverTargetAction="hide" aria-label="Закрыть историю переносов">×</button>
      </header>
      <ol aria-label="Последний перенос сверху">
        {newestReschedules(changes).map((change, index) => <li key={`${change.occurred_at}-${index}`}>
          <span className="dc-reschedule-from">{formatRescheduleDeadline(change.from_deadline)}</span>
          <span className="dc-reschedule-arrow" aria-label="перенесена на">→</span>
          <strong>{formatRescheduleDeadline(change.to_deadline)}</strong>
        </li>)}
      </ol>
    </div>
  </>
}
