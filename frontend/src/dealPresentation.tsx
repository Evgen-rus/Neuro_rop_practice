import type { DailyControlStatus } from './api'

const STATUS_SYMBOL: Record<DailyControlStatus, string> = {
  red: '!',
  yellow: '?',
  green: '✓',
}

export function DealStatusIndicator(props: {
  status: DailyControlStatus
  label: string
}) {
  return (
    <span
      className={`dc-deal-status-indicator ${props.status}`}
      aria-label={props.label}
      title={props.label}
    >
      {STATUS_SYMBOL[props.status]}
    </span>
  )
}
