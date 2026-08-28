import type { DailyTaskResult } from './api'
import { formatMoscowDateTime } from './dateTime'
import { sortDayTasks, taskDeadlineLabel, taskStripStatus, tasksStripSummary } from './dailyControlView'

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

export function TaskDayResults({ tasks, cutoffAt }: { tasks?: DailyTaskResult[]; cutoffAt?: string }) {
  const summary = tasksStripSummary(tasks, cutoffAt)
  if (!tasks) return <small className="dc-daily-work-note">{summary.text}</small>
  return (
    <details className={`dc-daily-task-fold ${summary.kind}`} onClick={(event) => event.stopPropagation()}>
      <summary>
        <span>{summary.text}</span>
      </summary>
      <div className="dc-daily-task-fold-body">
        {!tasks.length ? <small>Открытых задач нет</small> : sortDayTasks(tasks).map((task) => {
          const status = taskStripStatus(task)
          const label = taskDeadlineLabel(task, cutoffAt)
          return (
            <div key={task.key}>
              <span>{task.subject}</span>
              <b className={label.kind}>{label.text}</b>
              {task.status === 'completed' ? <small>Срок: {deadline(task.deadline)}</small> : null}
              {status.kind === 'rescheduled' ? <small>Задача перенесена</small> : null}
              {task.completed_today && task.status !== 'completed' ? <small>Выполнялась в этот день</small> : null}
              <TaskReschedules task={task} />
            </div>
          )
        })}
      </div>
    </details>
  )
}
