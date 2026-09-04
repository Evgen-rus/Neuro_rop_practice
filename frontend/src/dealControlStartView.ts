export type DealControlView = 'dashboard' | 'rop' | 'daily' | 'trajectory' | 'shadow' | 'team' | 'manager'
export type DealControlRole = 'admin' | 'rop' | 'manager'
export type DealControlTimeView = 'all' | 'attention' | 'today' | 'tomorrow' | 'future' | 'overdue'

export const DEAL_CONTROL_VIEWS: DealControlView[] = ['dashboard', 'rop', 'daily', 'trajectory', 'shadow', 'team', 'manager']

const VIEW_STORAGE_PREFIX = 'rop-assistant:deal-control-view:'

export function dealControlViewStorageKey(userId: number) {
  return `${VIEW_STORAGE_PREFIX}${userId}`
}

export function viewsAllowedForRole(role: DealControlRole): DealControlView[] {
  if (role === 'manager') return ['dashboard', 'manager']
  if (role === 'rop') return ['dashboard', 'rop', 'daily', 'manager']
  return DEAL_CONTROL_VIEWS
}

export function defaultDealControlView(role: DealControlRole): DealControlView {
  if (role === 'manager') return 'manager'
  if (role === 'rop') return 'rop'
  return 'dashboard'
}

export function isDealControlView(value: string): value is DealControlView {
  return DEAL_CONTROL_VIEWS.includes(value as DealControlView)
}

export function resolveDealControlView(role: DealControlRole, stored: string | null): DealControlView {
  if (stored && isDealControlView(stored) && viewsAllowedForRole(role).includes(stored)) return stored
  return defaultDealControlView(role)
}

export function initialDealControlFilters(
  role: DealControlRole,
  view: DealControlView,
  managerId: string | null,
): { managerFilter: string; timeView: DealControlTimeView } {
  if (view === 'manager') {
    return {
      managerFilter: role === 'manager' ? managerId || '' : '',
      timeView: 'today',
    }
  }
  return { managerFilter: '', timeView: 'all' }
}

export function readStoredDealControlView(userId: number): string | null {
  if (typeof window === 'undefined') return null
  try {
    return window.sessionStorage.getItem(dealControlViewStorageKey(userId))
  } catch {
    return null
  }
}

export function writeStoredDealControlView(userId: number, view: DealControlView) {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.setItem(dealControlViewStorageKey(userId), view)
  } catch {
    /* private mode / blocked storage */
  }
}

export function clearStoredDealControlView(userId: number | null | undefined) {
  if (!userId || typeof window === 'undefined') return
  try {
    window.sessionStorage.removeItem(dealControlViewStorageKey(userId))
  } catch {
    /* private mode / blocked storage */
  }
}
