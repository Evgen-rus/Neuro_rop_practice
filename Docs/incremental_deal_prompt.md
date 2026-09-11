# Как собирается промпт анализа сделки

Это учебный разбор, не runbook и не план внедрения.

Источник в коде: `openai_api/llm/analyze_deal.py`, функция `build_prompt`.
Repair: `openai_api/llm/full_analysis_repair.py`.
Данные incremental: `openai_api/llm/analyze_deal_if_changed.py`, функция `incremental_context`.

Ниже **симуляция**. Номера сделок, имена и тексты выдуманы. Реальный промпт после запуска лежит в папке сделки: `deal_<id>_request_prompt.txt` — в чат его не копируй, там живые данные клиента.

---

## Короткий ответ

Incremental **не отдельный маленький промпт вместо большого**.

Это тот же каркас, что у FULL:

1. роль («Ты ИИ-помощник РОПа…»);
2. все правила;
3. тот же JSON-контракт («Нужная JSON-структура»);
4. OKF, диагностика, стадия CRM, прошлая рекомендация.

Меняется **середина с фактами** — всё **после** `## ID СДЕЛКИ`. До этого маркера текст байт-в-байт как у FULL (это общий кэш между сделками и между FULL/incremental).

| | FULL | Incremental |
| --- | --- | --- |
| История сделки | целиком, раздел «ИСТОРИЯ СДЕЛКИ» | не отправляется |
| Старые транскрипты | целиком, раздел «ТРАНСКРИБАЦИИ» | не отправляются |
| Вместо них | — | один JSON `INCREMENTAL INPUT` |
| Доп. правила | нет | блок `<incremental_analysis_rules>` сразу после ID, до `INCREMENTAL INPUT` |

Модель всё равно должна вернуть **полный** analysis JSON той же схемы, не патч.

---

## Из чего склеивается FULL

Порядок в одном большом тексте, сверху вниз:

```text
[1] Роль и правила
    «Ты ИИ-помощник РОПа ПрактикМ.»
    grounding_rules, crm_stage_rules, остальные XML-блоки
    verification_loop

[2] JSON-контракт
    «Нужная JSON-структура:» + огромный шаблон полей
    (одинаковый для FULL и incremental)

[3] OKF-правила
    ## ОБРАБОТАННАЯ OKF-БАЗА ПРАВИЛ

[4] ID сделки
    ## ID СДЕЛКИ
    1001

[5] Факты  ← вот здесь FULL и incremental расходятся
    ## ТРАНСКРИБАЦИИ / НОВЫЕ СОБЫТИЯ
    (все тексты звонков)

    ## ИСТОРИЯ СДЕЛКИ
    (вся выгрузка)

    ## CURRENT_SITUATION_CONTEXT
    (якорь последнего содержательного контакта)

    ## DAILY_QUALITY_CONTEXT   (если аудит качества включён)

[6] Служебное
    ## ДИАГНОСТИКА ПОЛНОТЫ КОНТЕКСТА
    ## CRM_STAGE_POLICY
    ## PRIOR_NEURO_ROP_RECOMMENDATION
```

Блок `[1]+[2]+[3]` у incremental тот же, байт в байт: это общий кэш между сделками. Меняется `[5]`: после ID сначала `<incremental_analysis_rules>`, потом `INCREMENTAL INPUT`. `[6]` тоже общий по смыслу, но в кэш первого breakpoint не входит (он после ID).

---

## Симуляция: кусок FULL (факты)

Так выглядит **середина** полного промпта. Правила и JSON-контракт выше не повторяю — они длинные и те же.

```text
## ID СДЕЛКИ

1001

## ТРАНСКРИБАЦИИ / НОВЫЕ СОБЫТИЯ

### Звонок 2026-09-01
Клиент: нам нужно две линии упаковки, бюджет около 4 млн.
Менеджер: пришлю КП на этой неделе.

### Звонок 2026-09-08
Клиент: КП получили, думаем. По сроку — октябрь, если успеете.
...ещё десять старых звонков...

## ИСТОРИЯ СДЕЛКИ

Сделка 1001, ООО «Пример», стадия «КП отправлено», сумма 4 200 000.
Активности, комментарии, задачи, связанные сделки — весь длинный текст.

## CURRENT_SITUATION_CONTEXT

Последний содержательный контакт клиента: 2026-09-08, звонок call:9001.
Клиент подтвердил получение КП, срок «октябрь».
После этого менеджер отправил WhatsApp 2026-09-10 — ответа нет.
```

Именно эту пачку (старые звонки + вся история) incremental **не шлёт повторно**.

---

## Симуляция: тот же запуск, но incremental

Роль, правила, JSON-контракт и OKF **те же**, что у FULL — до `## ID СДЕЛКИ` ни одного лишнего символа. Incremental-уточнение начинается **после ID**:

```text
## ID СДЕЛКИ

1001

<incremental_analysis_rules>
- PREVIOUS_TRUSTED_COMPLETE_ANALYSIS — предыдущее проверенное понимание, а не неизменная истина.
- TRUSTED_CONTINUITY_BASELINE — короткий обязательный список stable IDs: сохрани каждый пункт, если новое evidence явно не закрывает его.
- Новые данные могут сохранить, пересмотреть или опровергнуть прежние выводы.
- Используй только CRM_SEMANTIC_DELTA и NEW_OR_REVISED_CLIENT_EVIDENCE как новые evidence.
- Новые доказательные ссылки call/email/message/transcript бери только из AVAILABLE_CLIENT_EVIDENCE_IDS. ID звонка или голосового Max без транскрипта можно оставить как пробел/попытку, но не как NEW_OR_REVISED_CLIENT_EVIDENCE и не для повышения confirmed. Не выдумывай ID. Исходящие текстовые активности не являются evidence разговора.
- Не считай отсутствие старых неизменившихся событий их удалением.
- Не удаляй нерешённые обязательства, риски и противоречия из управленческих блоков, пока новые evidence явно не подтвердят их закрытие; новое событие может изменить приоритет, но не отменяет их молча.
- Повышай basis_status или BANT timing до confirmed только если в этом же поле есть ID из NEW_OR_REVISED_CLIENT_EVIDENCE.
- Понижай уже confirmed только если новое клиентское evidence явно опровергает срок или основание; не меняй confirmed по старому списку.
- Верни полный текущий analysis JSON той же схемы, что FULL, не patch и не список изменений.
</incremental_analysis_rules>

## INCREMENTAL INPUT

{
  "PREVIOUS_TRUSTED_COMPLETE_ANALYSIS": {
    "deal_id": "1001",
    "deal_state": {
      "summary": "КП отправлено, клиент думает, срок ориентир октябрь",
      "amount": "4200000",
      "stage": "КП отправлено",
      "client": "ООО Пример"
    },
    "qualification_assessment": {
      "bant": {
        "timeframe": {
          "decision_timing": "октябрь",
          "decision_timing_status": "needs_confirmation"
        }
      }
    },
    "...": "здесь целиком прошлый проверенный JSON анализа, не diff"
  },
  "TRUSTED_CONTINUITY_BASELINE": {
    "deal_context": {
      "critical_facts": [
        {"stable_id": "cf-budget-4m", "text": "бюджет около 4 млн"}
      ],
      "commitments": [
        {"stable_id": "cm-kp-sent", "text": "менеджер обещал прислать КП"}
      ],
      "turning_points": [],
      "source_conflicts": []
    },
    "main_risk": {
      "text": "срок оплаты не подтверждён"
    }
  },
  "CRM_SEMANTIC_DELTA": [
    {
      "change_type": "UPDATED_MEANINGFUL",
      "path": "deal.stage",
      "before": "КП отправлено",
      "after": "Согласование"
    }
  ],
  "NEW_OR_REVISED_CLIENT_EVIDENCE": [
    {
      "evidence_id": "message:5502",
      "kind": "message",
      "direction": "inbound",
      "occurred_at": "2026-09-11T10:15:00+03:00",
      "text": "Давайте в ноябре, в октябре не успеваем. КП ок, сумму подтверждаем."
    }
  ],
  "AVAILABLE_CLIENT_EVIDENCE_IDS": [
    "call:9001",
    "call:9002",
    "message:5502",
    "email:12"
  ],
  "CURRENT_REQUIRED_CRM_FACTS": {
    "deal": {
      "stage": "Согласование",
      "amount": 4200000,
      "closed": false
    },
    "source_status": {}
  }
}

## CURRENT_SITUATION_CONTEXT

Последний содержательный контакт теперь message:5502 (11 сентября).
Клиент перенёс срок на ноябрь и подтвердил сумму.
```

Что модель должна понять из этого примера:

- старый анализ уже есть, его не надо угадывать заново;
- новое клиентское доказательство одно: `message:5502`;
- срок можно **подтвердить или переписать только с этим ID**;
- обязательство «КП отправлено» (`cm-kp-sent`) нельзя молча выкинуть;
- на выходе снова полный JSON, как у FULL.

Истории на 20 страниц и старых звонков в этом запросе нет. Они «сидят» внутри `PREVIOUS_TRUSTED_COMPLETE_ANALYSIS`.

---

## Если incremental чуть сломался: continuity_correction

Это **не отдельный файл промпта**. Тот же incremental-текст, плюс ещё один короткий блок **после ID**, рядом с `<incremental_analysis_rules>`:

```text
<continuity_correction>
- Предыдущий incremental-кандидат не прошёл deterministic continuity gate.
- Верни все baseline stable IDs. Не меняй confirmation или BANT без ID из NEW_OR_REVISED_CLIENT_EVIDENCE.
- Не удаляй unresolved item и не закрывай его только по старому evidence.
</continuity_correction>
```

Смысл: «ты только что ответил, но выкинул stable ID или поменял confirmed без нового evidence — пересобери JSON». Один такой повтор, потом запасной FULL.

---

## Repair схемы — уже другой промпт

Если JSON не проходит валидатор полей (не continuity, а форма: null не там, enum не тот), уходит **короткий другой** текст. Его собирает `build_full_repair_builder`. И FULL, и incremental используют его одинаково.

Симуляция:

```text
Ты выполняешь узкий repair уже готового FULL analysis JSON, а не новый анализ.
Не анализируй лид или сделку заново. Исправь только validation_errors.
Верни только JSON {"sections": {...}} ровно для allowed_sections, без пояснений и JSON Patch.
...ещё ограничения: не менять evidence, quotes, CRM-id...

REPAIR_PACKET
{"entity":"deal","allowed_sections":["qualification_assessment"],"validation_errors":[{"path":"qualification_assessment.bant.timeframe.decision_timing","message":"must be null unless decision_timing_status=confirmed"}],"section_contract":{"qualification_assessment":{"...":"кусок контракта только этой секции"}},"primary_sections":{"qualification_assessment":{"...":"то, что модель уже вернула"}}}
```

Сюда **не** кладут историю сделки и **не** кладут `INCREMENTAL INPUT`. Только сломанный кусок JSON + ошибка валидатора + вырезанные правила этой секции.

Если узкий repair нельзя сделать, повтор идёт **тем же основным промптом** (FULL или incremental), не новым третьим.

---

## Три картинки рядом

```text
FULL
  правила + JSON-контракт + OKF
  + все звонки
  + вся история
  + текущая ситуация
  → полный analysis JSON

INCREMENTAL
  те же правила + тот же JSON-контракт + OKF   ← общий кэш с FULL, до ## ID СДЕЛКИ
  + ## ID СДЕЛКИ и номер сделки
  + <incremental_analysis_rules>
  + JSON INCREMENTAL INPUT (прошлый анализ + дельта + новое evidence)
  + текущая ситуация
  → полный analysis JSON той же схемы

REPAIR
  короткий текст «почини секции»
  + REPAIR_PACKET
  → {"sections": {...}} только для сломанных блоков
```

---

## Где смотреть глазами в коде

1. Склейка: `openai_api/llm/analyze_deal.py` → `build_prompt`.
2. Incremental-правила: в той же функции, тег `<incremental_analysis_rules>` — после `## ID СДЕЛКИ`, не в общем префиксе.
3. Кэш OpenAI: `deal_analysis_cache_options` — ключ тот же, что у FULL; incremental режет только по `## ID СДЕЛКИ`.
4. Состав JSON на вход: `analyze_deal_if_changed.py` → `incremental_context`.
5. Repair: `openai_api/llm/full_analysis_repair.py`, строка «Ты выполняешь узкий repair…».
6. После живого запуска: `deal_<id>_request_prompt.txt` в workspace сделки.

План внедрения (не промпт): `Docs/INCREMENTAL_ANALYSIS_PLAN.md`.
