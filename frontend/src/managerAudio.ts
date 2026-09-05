import type { ManagerUploadedAudioJob } from './api'

export function readyManagerAudioJobId(job: ManagerUploadedAudioJob | null) {
  return job?.status === 'done' && job.attachment ? job.job_id : undefined
}

export function canRefineManagerSituation(context: string, job: ManagerUploadedAudioJob | null) {
  return context.trim().length > 0 || Boolean(readyManagerAudioJobId(job))
}
