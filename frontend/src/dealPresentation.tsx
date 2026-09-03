import type { DailyControlStatus } from './api'
import { bitrixDealUrl } from './dealDisplay'

const STATUS_SYMBOL: Record<DailyControlStatus, string> = {
  red: '!',
  yellow: '?',
  green: '✓',
  neutral: '–',
}

export function BitrixDealIdLink({ dealId }: { dealId: string }) {
  return (
    <a
      className="dc-deal-id"
      href={bitrixDealUrl(dealId)}
      target="_blank"
      rel="noreferrer"
      aria-label={`Открыть сделку #${dealId} в Bitrix`}
      title="Открыть в Bitrix"
      onClick={(event) => event.stopPropagation()}
    >
      #{dealId}
    </a>
  )
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
