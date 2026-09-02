export function canForceFullAnalysis(role?: string | null): boolean {
  return String(role || '').trim() === 'admin'
}

export function shouldConfirmAnalysis(role?: string | null, hasReport?: boolean): boolean {
  return canForceFullAnalysis(role) || Boolean(hasReport)
}

export function analysisConfirmCopy(input: { role?: string | null; hasReport: boolean }) {
  if (!canForceFullAnalysis(input.role)) {
    return {
      title: 'Проверить новые данные?',
      body: 'Система обновит информацию из Bitrix и проверит новые звонки. Если появились существенные изменения, могут потребоваться платная транскрибация и новый AI-анализ.',
      note: 'Без значимых изменений текущий анализ останется актуальным, повторный LLM-анализ не запустится.',
      checkLabel: 'Проверить и обновить',
      fullLabel: null as string | null,
    }
  }
  if (input.hasReport) {
    return {
      title: 'Как обновить анализ?',
      body: 'Можно проверить Bitrix и новые звонки — как сейчас. Либо сразу запустить полный AI-анализ, даже если новых фактов нет.',
      note: 'Полный анализ платный и перезапишет текущий отчёт. Обычная проверка без значимых изменений повторно LLM не вызовет.',
      checkLabel: 'Проверить как сейчас',
      fullLabel: 'Полный анализ',
    }
  }
  return {
    title: 'Как провести анализ?',
    body: 'Можно сначала собрать данные из Bitrix и запустить AI только при необходимости. Либо сразу сделать полный AI-анализ.',
    note: 'Полный анализ платный и запускается сразу. Обычная проверка сначала смотрит, нужен ли новый AI-анализ.',
    checkLabel: 'Проверить и запустить',
    fullLabel: 'Полный анализ',
  }
}
