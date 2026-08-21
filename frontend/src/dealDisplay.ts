const BITRIX_DEAL_BASE_URL = 'https://obtorg.bitrix24.ru/crm/deal/details'

type DealStage = {
  pipeline_id?: string | null
  pipeline_name?: string | null
  stage_id?: string | null
  stage_name?: string | null
}

export function formatDealPipelineStage(deal: DealStage) {
  const pipelineId = String(deal.pipeline_id || '').trim()
  const pipelineName = String(deal.pipeline_name || '').trim()
    || (pipelineId ? `Воронка ${pipelineId}` : 'Воронка не указана')
  const stageName = String(deal.stage_name || deal.stage_id || '').trim() || 'Стадия не указана'
  return `${pipelineName} → ${stageName}`
}

export function bitrixDealUrl(dealId: string) {
  return `${BITRIX_DEAL_BASE_URL}/${encodeURIComponent(dealId)}/`
}
