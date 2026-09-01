import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import {
  addDealControlManagers,
  activateAuthUser,
  createAuthUser,
  deactivateAuthUser,
  fetchAuthUsers,
  setAuthUserPassword,
  type AuthAccount,
  type AuthUser,
  type DealControlDashboard,
} from './api'
import { accountInTeamScope, canDeactivateAccount, createAccountError, createAccountNotice, roleLabel, type TeamCreateRole } from './teamAdminView'

type TeamAdminProps = {
  user: AuthUser
  scope: DealControlDashboard['scope']
  syncing: boolean
  flashError?: string
  flashNotice?: string
  onScopeChanged: () => Promise<void> | void
  onSyncBitrix: () => Promise<void> | void
}

type PasswordModal = {
  account: AuthAccount
  password: string
  confirm: string
}

export function TeamAdmin({ user, scope, syncing, flashError = '', flashNotice = '', onScopeChanged, onSyncBitrix }: TeamAdminProps) {
  const [items, setItems] = useState<AuthAccount[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [login, setLogin] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [managerId, setManagerId] = useState('')
  const [createRole, setCreateRole] = useState<TeamCreateRole>('manager')
  const [passwordModal, setPasswordModal] = useState<PasswordModal | null>(null)
  const [deactivateTarget, setDeactivateTarget] = useState<AuthAccount | null>(null)

  const managerIds = scope.manager_ids || []
  const accounts = useMemo(
    () => [...items].sort((left, right) => {
      if (left.is_active !== right.is_active) return left.is_active ? -1 : 1
      return left.login.localeCompare(right.login, 'ru')
    }),
    [items],
  )

  const reload = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const response = await fetchAuthUsers()
      setItems(Array.isArray(response.items) ? response.items : [])
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void reload() }, [reload])

  function replaceAccount(account: AuthAccount) {
    setItems((current) => current.map((item) => item.id === account.id ? account : item))
  }

  async function createAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const nextLogin = login.trim()
    const nextManagerId = managerId.trim()
    const validationError = createAccountError({
      role: createRole,
      login: nextLogin,
      password,
      confirmPassword,
      managerId: nextManagerId,
    })
    if (validationError) {
      setError(validationError)
      return
    }
    setBusy('create')
    setError('')
    setNotice('')
    try {
      await createAuthUser({
        login: nextLogin,
        password,
        role: createRole,
        manager_id: createRole === 'manager' ? nextManagerId : null,
        is_active: true,
      })
      if (createRole === 'manager') {
        try {
          await addDealControlManagers([nextManagerId])
        } catch (reason) {
          throw new Error(
            `Пользователь создан, но не добавлен в выборку команды: ${reason instanceof Error ? reason.message : String(reason)}`,
          )
        }
        await onScopeChanged()
      }
      setLogin('')
      setPassword('')
      setConfirmPassword('')
      setManagerId('')
      await reload()
      setNotice(createAccountNotice(createRole))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy('')
    }
  }

  async function savePassword() {
    if (!passwordModal) return
    if (!passwordModal.password) {
      setError('Пароль не может быть пустым.')
      return
    }
    if (passwordModal.password !== passwordModal.confirm) {
      setError('Пароли не совпадают.')
      return
    }
    setBusy(`password:${passwordModal.account.id}`)
    setError('')
    setNotice('')
    try {
      const response = await setAuthUserPassword(passwordModal.account.id, passwordModal.password)
      if (response.user) replaceAccount(response.user)
      setPasswordModal(null)
      setNotice(`Пароль для ${passwordModal.account.login} обновлён. Старые сессии этого пользователя закрыты.`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy('')
    }
  }

  async function addToScope(account: AuthAccount) {
    const nextManagerId = String(account.manager_id || '').trim()
    if (!nextManagerId) return
    setBusy(`scope:${account.id}`)
    setError('')
    setNotice('')
    try {
      await addDealControlManagers([nextManagerId])
      await onScopeChanged()
      setNotice(`${account.login} добавлен в выборку команды. Нажмите «Обновить Bitrix», чтобы подтянуть сделки.`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy('')
    }
  }

  async function toggleActive(account: AuthAccount) {
    const action = account.is_active ? 'deactivate' : 'activate'
    setBusy(`${action}:${account.id}`)
    setError('')
    setNotice('')
    try {
      const response = account.is_active
        ? await deactivateAuthUser(account.id)
        : await activateAuthUser(account.id)
      if (response.user) replaceAccount(response.user)
      setDeactivateTarget(null)
      setNotice(account.is_active
        ? `${account.login} выключен. Войти он больше не сможет, сделки в выборке остаются.`
        : `${account.login} снова активен.`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy('')
    }
  }

  return <div className="team-page">
    <header className="team-header">
      <div>
        <span className="dc-eyebrow">Только администратор</span>
        <h1>Команда</h1>
        <p>Менеджеру нужен Bitrix ID — его сделки попадут в выборку. РОПу ID не нужен: он видит всю команду.</p>
      </div>
      <button className="dc-button" disabled={syncing || Boolean(busy)} onClick={() => void onSyncBitrix()}>
        {syncing ? <><span className="dc-spinner" />Обновляем Bitrix…</> : <><span>⟳</span>Обновить Bitrix</>}
      </button>
    </header>

    {error || flashError ? <div className="dc-alert error" role="alert">{error || flashError}</div> : null}
    {notice || flashNotice ? <div className="dc-alert success">{notice || flashNotice}</div> : null}

    <div className="team-layout">
      <form className="team-card" onSubmit={(event) => void createAccount(event)}>
        <div className="team-card-head">
          <h2>{createRole === 'rop' ? 'Новый РОП' : 'Новый менеджер'}</h2>
          <p>{createRole === 'rop'
            ? 'Только логин и пароль. Выборка сделок не меняется.'
            : 'Логин хранится строчными буквами. Bitrix ID — из карточки сотрудника, например 2775.'}</p>
        </div>
        <label>Роль
          <div className="team-role-tabs">
            <button type="button" className={createRole === 'manager' ? 'active' : ''} onClick={() => setCreateRole('manager')}>Менеджер</button>
            <button type="button" className={createRole === 'rop' ? 'active' : ''} onClick={() => setCreateRole('rop')}>РОП</button>
          </div>
        </label>
        <label>Логин для входа
          <input value={login} onChange={(event) => setLogin(event.target.value)} autoComplete="off" placeholder={createRole === 'rop' ? 'rop2' : 'ahramovich'} />
        </label>
        <label>Пароль
          <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" />
        </label>
        <label>Повторите пароль
          <input type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} autoComplete="new-password" />
        </label>
        {createRole === 'manager' ? <label>Bitrix ID ответственного
          <input value={managerId} onChange={(event) => setManagerId(event.target.value)} inputMode="numeric" placeholder="2775" />
        </label> : null}
        <button className="dc-button primary" disabled={busy === 'create'} type="submit">
          {busy === 'create' ? <><span className="dc-spinner" />Создаём…</> : createRole === 'rop' ? 'Добавить РОПа' : 'Добавить менеджера'}
        </button>
      </form>

      <section className="team-card team-list">
        <div className="team-card-head">
          <h2>Учётные записи</h2>
          <p>Выключение закрывает вход, но не убирает человека из выборки сделок.</p>
        </div>
        {loading ? <p className="team-empty"><span className="dc-spinner" />Загружаем пользователей…</p> : null}
        {!loading && !accounts.length ? <p className="team-empty">Пользователей пока нет.</p> : null}
        {accounts.map((account) => {
          const inScope = accountInTeamScope(account, managerIds)
          const toggling = busy === `deactivate:${account.id}` || busy === `activate:${account.id}`
          return <article className={`team-account${account.is_active ? '' : ' inactive'}`} key={account.id}>
            <div>
              <strong>{account.login}</strong>
              <small>
                {roleLabel(account.role)}
                {account.manager_id ? ` · Bitrix ${account.manager_id}` : ''}
                {account.role === 'manager' ? ` · ${inScope ? 'в выборке команды' : 'не в выборке'}` : ''}
                {account.is_active ? '' : ' · выключен'}
              </small>
            </div>
            <div className="team-account-actions">
              {account.role === 'manager' && account.manager_id && !inScope ? (
                <button className="dc-button primary" type="button" disabled={Boolean(busy)} onClick={() => void addToScope(account)}>
                  {busy === `scope:${account.id}` ? <span className="dc-spinner" /> : null}
                  В выборку
                </button>
              ) : null}
              <button
                className="dc-button"
                type="button"
                disabled={Boolean(busy)}
                onClick={() => {
                  setError('')
                  setPasswordModal({ account, password: '', confirm: '' })
                }}
              >
                Сбросить пароль
              </button>
              {account.is_active ? (
                <button
                  className="dc-button"
                  type="button"
                  disabled={Boolean(busy) || !canDeactivateAccount(account, user.id)}
                  onClick={() => {
                    setError('')
                    setDeactivateTarget(account)
                  }}
                >
                  {toggling ? <span className="dc-spinner" /> : null}
                  Выключить
                </button>
              ) : (
                <button className="dc-button primary" type="button" disabled={Boolean(busy)} onClick={() => void toggleActive(account)}>
                  {toggling ? <span className="dc-spinner" /> : null}
                  Включить
                </button>
              )}
            </div>
          </article>
        })}
      </section>
    </div>

    {passwordModal ? <div className="dc-modal-layer" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setPasswordModal(null) }}>
      <section className="dc-modal">
        <h2>Новый пароль</h2>
        <p>Для {passwordModal.account.login}. После смены все его текущие сессии закроются.</p>
        <label>Пароль
          <input type="password" value={passwordModal.password} onChange={(event) => setPasswordModal({ ...passwordModal, password: event.target.value })} autoComplete="new-password" />
        </label>
        <label>Повторите пароль
          <input type="password" value={passwordModal.confirm} onChange={(event) => setPasswordModal({ ...passwordModal, confirm: event.target.value })} autoComplete="new-password" />
        </label>
        <div>
          <button className="dc-button" type="button" disabled={Boolean(busy)} onClick={() => setPasswordModal(null)}>Отмена</button>
          <button className="dc-button primary" type="button" disabled={busy === `password:${passwordModal.account.id}`} onClick={() => void savePassword()}>
            {busy === `password:${passwordModal.account.id}` ? <><span className="dc-spinner" />Сохраняем…</> : 'Сохранить пароль'}
          </button>
        </div>
      </section>
    </div> : null}

    {deactivateTarget ? <div className="dc-modal-layer" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setDeactivateTarget(null) }}>
      <section className="dc-modal">
        <h2>Выключить {deactivateTarget.login}?</h2>
        <p>Он не сможет войти в НейроРОП. Сделки в выборке команды останутся, пока ID не уберут отдельно.</p>
        <div>
          <button className="dc-button" type="button" disabled={Boolean(busy)} onClick={() => setDeactivateTarget(null)}>Отмена</button>
          <button className="dc-button primary" type="button" disabled={busy === `deactivate:${deactivateTarget.id}`} onClick={() => void toggleActive(deactivateTarget)}>
            {busy === `deactivate:${deactivateTarget.id}` ? <><span className="dc-spinner" />Выключаем…</> : 'Выключить'}
          </button>
        </div>
      </section>
    </div> : null}
  </div>
}
