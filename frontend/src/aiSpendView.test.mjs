import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  buildAiSpendEventsQuery,
  buildAiSpendPeriodQuery,
  formatSpendDelta,
  spendChartIndexFromSvgX,
  spendChartLabelIndexes,
  spendChartLabelPosition,
  spendChartSeries,
  spendChartValue,
  spendDayTitle,
  spendFilterButtonLabel,
  spendFilterKindOptions,
  spendShiftIsoDate,
  SPEND_CHART_LABEL_MIN_LABEL_PX,
  SPEND_CHART_LABEL_REFERENCE_PX,
  SPEND_CHART_PAD_X,
  SPEND_CHART_WIDTH,
} from './aiSpendView.ts'

test('период всегда уходит явными датами, а не пресетом', () => {
  // Относительных пресетов на экране больше нет: единственный вход в период —
  // два поля. Если бы UI снова слал `preset=7`, на экране появились бы даты
  // одного периода, а данные пришли бы за другие 30 дней.
  assert.equal(
    buildAiSpendPeriodQuery('2026-08-11', '2026-09-09').toString(),
    'preset=custom&from=2026-08-11&to=2026-09-09',
  )
  // Даты ещё не разобрались — запрос блокируется на стороне компонента,
  // но сам builder не должен молча выкинуть период и оставить пресет по
  // умолчанию: это выглядело бы как «период принят» с чужими данными.
  assert.equal(buildAiSpendPeriodQuery('', '').get('preset'), 'custom')
  assert.equal(buildAiSpendPeriodQuery('', '').get('from'), null)
})

test('events query adds search and attention without empty filters', () => {
  const query = buildAiSpendEventsQuery({
    fromDate: '2026-09-01',
    toDate: '2026-09-28',
    q: '19021',
    attention: 'errors',
    page: 2,
  })
  assert.equal(query.get('q'), '19021')
  assert.equal(query.get('attention'), 'errors')
  assert.equal(query.get('page'), '2')
  assert.equal(query.get('kind_group'), null)
  assert.equal(query.get('from'), '2026-09-01')
})

test('подпись кнопки всегда показывает выбранное', () => {
  // Кнопка заменила одиннадцать плашек на телефоне. Молчащая кнопка проигрывала
  // бы главному: плашки показывали выбранный тип и единицу сразу.
  assert.equal(spendFilterButtonLabel('Все', 'all', 'cost'), 'Все · Расходы')
  assert.equal(spendFilterButtonLabel('Все', 'all', 'calls'), 'Все · Вызовы')
  assert.equal(spendFilterButtonLabel('Quick Help', 'quick_help', 'cost'), 'Quick Help · Расходы')
  assert.equal(spendFilterButtonLabel('Скрипты', 'scripts', 'tokens'), 'Скрипты · Токены')
  // Подпись не имеет права врать: после выбора «Скрипты» кнопка обязана
  // назвать именно их, а не дефолтное «Все · Расходы».
  assert.notEqual(spendFilterButtonLabel('Скрипты', 'scripts', 'cost'), 'Все · Расходы')
})

test('набор типов операций всегда начинается с «Все»', () => {
  // Список нужен десктопным плашкам и мобильной панели. Без «Все» из панели
  // нельзя было бы вернуться к общему виду графика.
  assert.deepEqual(spendFilterKindOptions([]).map((item) => item.id), ['all'])
  assert.deepEqual(spendFilterKindOptions([
    { id: 'quick_help', label: 'Quick Help' },
    { id: 'other', label: 'Другое' },
  ]), [
    { id: 'all', label: 'Все' },
    { id: 'quick_help', label: 'Quick Help' },
    { id: 'other', label: 'Другое' },
  ])
})

test('delta formatting stays honest and signed', () => {
  assert.equal(formatSpendDelta(null), null)
  assert.deepEqual(formatSpendDelta(0), { text: '0%', tone: 'flat' })
  assert.deepEqual(formatSpendDelta(-12), { text: '↓ 12%', tone: 'down' })
  assert.deepEqual(formatSpendDelta(8.4), { text: '↑ 8,4%', tone: 'up' })
})

test('chart series uses kind-group daily data when filtered', () => {
  const analytics = {
    daily_series: [
      { date: '2026-09-09', estimated_cost_rub: 10, paid_calls: 2, total_tokens: 100, unknown_cost_calls: 0 },
    ],
    kind_groups: [
      {
        id: 'quick_help',
        daily: [
          { date: '2026-09-09', estimated_cost_rub: 3, paid_calls: 1, total_tokens: 40, unknown_cost_calls: 0 },
        ],
      },
    ],
  }
  const series = spendChartSeries(analytics, 'quick_help')
  assert.equal(spendChartValue(series[0], 'cost'), 3)
  assert.equal(spendChartValue(series[0], 'calls'), 1)
})

test('chart hover index follows the plotted line, not the empty side padding', () => {
  assert.equal(spendChartIndexFromSvgX(10, 5), 0)
  assert.equal(spendChartIndexFromSvgX(790, 5), 4)
  assert.equal(spendChartIndexFromSvgX(400, 5), 2)
})

test('day labels sit under their own point and never leave the plot area', () => {
  // Крайние точки — на `padX` и `width - padX`, а не на 0% и 100%: подпись
  // обязана повторять ту же вставку, иначе она уезжает от своей точки.
  const first = spendChartLabelPosition(0, 30)
  const last = spendChartLabelPosition(29, 30)
  assert.ok(first > 0, 'первый день не должен стоять в 0%')
  assert.ok(last < 100, 'последний день не должен стоять в 100%')
  assert.equal(first, (SPEND_CHART_PAD_X / SPEND_CHART_WIDTH) * 100)
  assert.equal(last, ((SPEND_CHART_WIDTH - SPEND_CHART_PAD_X) / SPEND_CHART_WIDTH) * 100)

  // Подпись дня обязана попадать ровно в ту же точку, что и `spendChartIndexFromSvgX`
  // по обратной координате. Иначе на 30 днях подпись съезжает с вершины.
  for (const total of [7, 30, 90]) {
    for (const index of [0, Math.floor(total / 2), total - 1]) {
      const percent = spendChartLabelPosition(index, total)
      const svgX = (percent / 100) * SPEND_CHART_WIDTH
      assert.equal(spendChartIndexFromSvgX(svgX, total), index)
    }
  }

  // Единственный день и выход за границы не должны ломать раскладку.
  assert.equal(spendChartLabelPosition(0, 1), spendChartLabelPosition(0, 1))
  assert.ok(spendChartLabelPosition(0, 0) >= 0)
  assert.ok(spendChartLabelPosition(5, 2) <= 100)
})

test('every period keeps both boundary dates and never collides captions', () => {
  // Короткий период — подписываем каждый день, как раньше.
  assert.deepEqual(spendChartLabelIndexes(7), [0, 1, 2, 3, 4, 5, 6])

  for (const total of [1, 2, 14, 30, 60, 90, 180, 365]) {
    const indexes = spendChartLabelIndexes(total)
    const last = total - 1

    // Обе границы периода подписаны: без первой даты не видно, откуда началось
    // «дни», без последней — где период закончился.
    assert.equal(indexes[0], 0, `первый день потерял подпись при ${total}`)
    assert.equal(indexes[indexes.length - 1], last, `последний день потерял подпись при ${total}`)

    // Никаких двух подписей ближе, чем ширина самой даты.
    const dayPx = ((SPEND_CHART_LABEL_REFERENCE_PX / SPEND_CHART_WIDTH)
      * (SPEND_CHART_WIDTH - SPEND_CHART_PAD_X * 2)) / Math.max(total - 1, 1)
    for (let k = 1; k < indexes.length; k++) {
      const gapPx = (indexes[k] - indexes[k - 1]) * dayPx
      assert.ok(
        gapPx >= SPEND_CHART_LABEL_MIN_LABEL_PX,
        `при ${total} днях подписи ${indexes[k - 1]} и ${indexes[k]} слипаются: ${gapPx.toFixed(1)}px`,
      )
    }
  }
})

test('day titles mark today yesterday and the day before', () => {
  assert.equal(spendShiftIsoDate('2026-09-10', -1), '2026-09-09')
  assert.equal(spendDayTitle('2026-09-10', '2026-09-10', '10 сентября'), 'Сегодня · 10 сентября')
  assert.equal(spendDayTitle('2026-09-09', '2026-09-10', '09 сентября'), 'Вчера · 09 сентября')
  assert.equal(spendDayTitle('2026-09-08', '2026-09-10', '08 сентября'), 'Позавчера · 08 сентября')
  assert.equal(spendDayTitle('2026-08-01', '2026-09-10', '01 августа'), '01 августа')
})
