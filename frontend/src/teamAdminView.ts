import type { AuthAccount, AuthRole } from './api'

export type TeamCreateRole = 'manager' | 'rop'

export const AUTH_ROLE_LABELS: Record<AuthRole, string> = {
  admin: 'Админ',
  rop: 'РОП',
  manager: 'Менеджер',
}

export function roleLabel(role: AuthRole | string): string {
  if (role === 'admin' || role === 'rop' || role === 'manager') return AUTH_ROLE_LABELS[role]
  return role
}

export function appendUniqueIds(current: string[], incoming: string): string[] {
  const next = incoming.trim()
  const ids = current.map((item) => item.trim()).filter(Boolean)
  if (!next || ids.includes(next)) return ids
  return [...ids, next]
}

export function accountInTeamScope(account: AuthAccount, managerIds: string[]): boolean {
  const managerId = String(account.manager_id || '').trim()
  if (!managerId) return false
  return managerIds.map((item) => item.trim()).includes(managerId)
}

export function canDeactivateAccount(account: AuthAccount, currentUserId: number): boolean {
  return account.is_active && account.id !== currentUserId
}

export function createAccountError(input: {
  role: TeamCreateRole
  login: string
  password: string
  confirmPassword: string
  managerId: string
}): string | null {
  if (!input.login.trim()) return 'Нужен логин.'
  if (!input.password) return 'Нужен пароль.'
  if (input.password !== input.confirmPassword) return 'Пароли не совпадают.'
  if (input.role === 'manager' && !input.managerId.trim()) return 'Менеджеру нужен Bitrix ID ответственного.'
  return null
}

export function createAccountNotice(role: TeamCreateRole): string {
  if (role === 'rop') return 'РОП создан. Он видит сделки выборки команды, Bitrix ID не нужен.'
  return 'Менеджер добавлен в выборку команды. Нажмите «Обновить Bitrix», чтобы подтянуть его сделки.'
}

