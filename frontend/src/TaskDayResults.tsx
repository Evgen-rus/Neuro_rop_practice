import type { DailyTaskResult } from './api'
import { formatMoscowDateTime } from './dateTime'

function deadline(value?: string | null) {
  return value ? formatMoscowDateTime(value, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) || value : 'без срока'
}

export function TaskReschedules({ task }: { task?: DailyTaskResult }) {
  return task?.reschedules.length ? <div className="dc-task-reschedules">
    {task.reschedules.map((change, index) => <small key={`${change.occurred_at}-${index}`}>
      Перенесена: {deadline(change.from_deadline)} → {deadline(change.to_deadline)}
    </small>)}
  </div> : null
}

export function TaskDayResults({ tasks }: { tasks?: DailyTaskResult[] }) {
  if (!tasks) return <small className="dc-daily-work-note">Подробности задач в этом старом срезе не сохранялись</small>
  return <div className="dc-daily-task-results" aria-label="Задачи на момент среза">
    <strong>Задачи</strong>
    {!tasks.length ? <small>Задачи в срезе не зафиксированы</small> : tasks.map((task) => <div key={task.key}>
      <span>{task.subject}</span>
      <b className={task.overdue ? 'overdue' : task.status}>
        {task.status === 'completed' ? `Выполнена${task.completion_source === 'local' ? ' · отметка в НейроРОПе' : ''}`
          : task.status === 'open' ? task.overdue ? 'Не выполнена · просрочена' : 'Не выполнена' : 'Текущий статус не сохранён'}
      </b>
      {task.completed_today && task.status !== 'completed' ? <small>Выполнялась в этот день</small> : null}
      <TaskReschedules task={task} />
    </div>)}
  </div>
}
