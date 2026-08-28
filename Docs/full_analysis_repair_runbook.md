# FULL analysis: targeted semantic repair

## Путь обработки

Canonical FULL lead/deal: primary → JSON parsing → существующий deterministic
normalizer → полный canonical validator. При успехе повторного вызова нет.
При распознанной ошибке parsed candidate: Luna repair → проверка envelope и
разрешённых sections → merge с primary → тот же normalizer → полный validator.
Успешный repair не вызывает генерацию полного анализа. Сохранение отчёта по-прежнему
возможно только после успешной validation.

Если repair невозможен или не прошёл, остаётся прежняя полная correction attempt:
исходный FULL prompt + ошибка primary + его raw output. Она использует primary
model/reasoning, а не Luna. При её неудаче — прежний `ValidatedAnalysisFailure`,
error JSON и raw output; новый Markdown не создаётся.

## Настройки и rollout

| Настройка | Default |
| --- | --- |
| `ANALYSIS_MODEL` | `gpt-5.6-terra` |
| `ANALYSIS_REASONING_EFFORT` | `low` |
| `ANALYSIS_REPAIR_MODEL` | `gpt-5.6-luna` |
| `ANALYSIS_REPAIR_REASONING_EFFORT` | `xhigh` |
| `ANALYSIS_REPAIR_MAX_OUTPUT_TOKENS` | `8000` |

Существующий `.env` не переписывается. Для включения именно этой пары моделей
оператор должен отдельно проверить overrides в своём окружении: старый `.env`
имеет приоритет над defaults. `--model` продолжает переопределять primary/fallback.
Изменённый `ANALYSIS_MODEL` также является default для существующих callers,
которые наследуют его (в частности manager settings); явные overrides сохраняются.

Лимит repair включает reasoning и видимый JSON. Если его не хватает, невалидный
JSON пойдёт в full fallback; это может увеличить стоимость неуспешного repair.
`call_analysis_json(reasoning_effort=..., max_output_tokens=...)` поддерживает
параметры отдельного вызова. При их отсутствии сохраняются прежние глобальные
настройки. Полный анализ остаётся на `json_object`.

Поддержка Luna `xhigh` есть в `prompt_lab_models.MODEL_REASONING` и
[официальной документации модели](https://developers.openai.com/api/docs/models/gpt-5.6-luna).
Таблица тарифов проекта соответствует текущим страницам
[Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) и
[Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna).
Доступ конкретного API project и качество на реальных данных не проверялись:
платные вызовы и deployment в эту работу не входят.

## Что получает Luna

`full_analysis_repair.py` извлекает JSON-шаблон из существующего FULL prompt до
отправки repair. Основной prompt не менялся. Builder сохраняет только шаблон и
выбранные статические rule-блоки, не исходную историю.

В `REPAIR_PACKET` входят:

- `entity`, `allowed_sections`;
- отдельные `validation_errors` с path/message без подстановки ошибочного `got`;
- `section_contract` только выбранных sections, enum и ограничения из ошибок;
- применимые list limits и выбранные статические business rules;
- `primary_sections`: выбранные sections candidate вместе с уже имеющимся evidence.

CRM history, transcript, diagnostics и вся OKF-база повторно не отправляются.
Нового evidence retrieval нет. Packet ограничен 32 000 символами и 10 sections;
переполнение вызывает полный fallback, а не обрезание фактов. Если самой Luna
недостаточно информации, prompt требует отказа `{"cannot_repair":true}`, который
также ведёт в fallback.

## Выбор области и ограничения качества

`AnalysisValidationError.errors` сохраняет отдельные сообщения canonical validator;
старый текст exception и все правила проверок сохранены. Resolver использует
консервативные patterns поверх этих сообщений. Старые exceptions без списка,
нераспознанные ошибки, отсутствующий section/контракт и ошибки evidence ведут
сразу в full fallback. JSON parsing errors никогда не приводят к partial merge.

Для deals `DEAL_REPAIR_DOMAINS` связывает qualification, payment, money path,
commercial, risk и communication profile с явными наборами `DEPENDENCIES` из V2.
Transient/new-evidence правила V2 не применяются: новых фактов здесь нет.
Локальные блоки выбираются из `LOCAL_SECTIONS`. `deal_context`, неоднозначные
глобальные ошибки и потребность в новом evidence остаются на full fallback.

Для leads отдельный allowlist: `rop_manager_message_block`, `manager_action_block`,
`memory_update`. BANT/category/route/closure и расхождение с deterministic CRM state
идут в full fallback; deal dependency map к leads не применяется.

Envelope — ровно `{"sections": {...}}` с точным набором разрешённых sections.
Merge берёт deepcopy primary и накладывает sections. Как в V2, пропущенные ключи
вложенных объектов сохраняются из primary; списки заменяются целиком.
Код запрещает лишние sections/неизвестные поля, изменение evidence, sources, quotes,
CRM-полей и идентификаторов, понижение статусов `confirmed`. После normalizer также
проверяется неизменность sections вне разрешённого набора. Затем выполняется весь
canonical validator, для lead — также привязка к текущему deterministic CRM state.

Это ограничивает изменения, но не доказывает истинность всех свободных формулировок.
Совместимый с validator, но смыслово ошибочный текст всё ещё возможен; качество
нужно отдельно измерить на обезличенном корпусе с ручной проверкой.

V2 использует те же helpers сохранения вложенных ключей и merge. Его prompts,
выбор affected sections, normalizer с `truncate_lists=False` и две прежние
semantic attempts не изменены. Legacy incremental тоже не включает FULL repair.

## Transport и invalid JSON

Transport/API retry остаётся в `run_with_retry`, отдельно от semantic attempts.
Исчерпание primary transport retry не запускает Luna и сохраняет прежний тип
API exception. Ошибка API на repair после transport retries ведёт в full fallback;
у такого attempt `transport_error=true`, validation не выполнялась (`null`).
Ошибка transport на fallback по-прежнему выходит как API exception.

Invalid primary JSON → сразу full correction. Invalid repair JSON, отказ,
неверный envelope, нарушение scope или невалидный merged analysis → full correction
на исходном Terra response. Неудачный Luna candidate не становится основой fallback.

## Usage, стоимость и наблюдаемость

`model_metadata.semantic_attempts` содержит model/reasoning, usage, cached/cache-write/
reasoning tokens, latency, отдельную стоимость, `attempt_phase` (`primary`, `repair`,
`fallback`), `repair`, `validation_passed`, transport metadata и private diagnostic ref.
`analysis_attempt_id` связывает attempts одного анализа в usage trace; `final_attempt`
показывает терминальную запись. Usage trace не содержит candidate или prompt.

USD/RUB суммируются из отдельных оценок каждого request по его модели. Общий usage
не тарифицируется как одна модель. У смешанной стоимости нет единой ставки за токен;
`estimated_cost.models` перечисляет модели. Неизвестная стоимость не превращается
в ноль. USD хранится с точностью до шести знаков, чтобы не терять малые Luna-вызовы.
Это оценка по встроенным тарифам, не фактический счёт провайдера.
Существующая таблица использует стандартные ставки: надбавка за контекст более
272K input tokens в этом изменении не реализована. Такие запросы требуют отдельной
проверки оценки; это прежнее ограничение pricing helper, а не свойство repair.

Агрегат сохраняет прежние ключи usage/cost и добавляет `cost_by_phase`,
`primary_validation_failed`, `repair_invoked`, `repair_succeeded`, `fallback_invoked`,
`final_validation_passed`, `final_phase`. При успешном repair model/reasoning,
response_id и raw output верхнего уровня остаются от primary; окончательный результат
— поле `analysis`, а изменяемые sections перечислены в `repaired_sections`.

По trace, сгруппированному по `analysis_attempt_id`, считаются доли primary validation
failure, repair invocation/success, fallback и final failure. Validation rates следует
считать только по attempts, дошедшим до validator, а transport failures — отдельно.
Стоимость успешного анализа — сумма всех его attempts, включая неуспешные. Для
стоимости на один успешный результат с учётом потерь дополнительно включаются
расходы окончательно неуспешных анализов в том же окне.

## Проверки и следующий отдельный этап

Локальные проверки не обращаются к Bitrix/OpenAI: ответы LLM подменяются.

Проверено 2026-08-28: полный `unittest discover -s tests` — 770 tests, OK
(165,872 с); отдельно retry — 27, V2 — 34, API/caching — 9, все OK.
`git diff --check`, Python syntax и UTF-8/whitespace проверены. Frontend lint/build
не запускались: frontend не изменён. Реальные CRM/OpenAI pipelines и deployment
не запускались. Изменения остаются в текущей `main`, без новой ветки и коммита.

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests
git diff --check
```

Следующий отдельный этап — обезличенный eval-корпус с измерением качества,
repair/fallback rates, latency и стоимости; затем решение о расширении resolver
для lead category/route и deal_context. Экономия пока не измерена.

Перевод FULL в `json_schema` Structured Outputs имеет смысл отдельной задачей:
он сокращает ошибки обязательных полей, типов и enum, включённых в поддерживаемую
JSON Schema. Процент снижения repair без корпуса ошибок неизвестен. По
[документации Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
schema adherence отличается от JSON mode; отказ и incomplete response требуют
обработки независимо от schema. Business validators остаются обязательными:
evidence/status consistency, BANT/category/route, бюджетные условия, CRM closure,
контакт против попытки связи, feedback/result, сроки и межсекционные зависимости.

## Изменённые файлы

| Файлы | Назначение |
| --- | --- |
| `openai_api/config.py`, `.env.example` | Независимые primary/repair defaults |
| `openai_api/llm/llm_client.py` | Последовательность attempts, параметры вызова, metadata |
| `openai_api/llm/full_analysis_repair.py` | Entity resolver, packet, scope и evidence guards |
| `openai_api/llm/section_repair.py`, `openai_api/llm/deal_incremental_v2.py` | Общие helpers без изменения поведения V2 |
| `openai_api/llm/analyze_deal.py`, `openai_api/llm/analyze_lead.py` | Подключение к canonical FULL entrypoints |
| `openai_api/llm/validation.py` | Совместимый список сообщений ошибок в exception |
| `openai_api/llm/usage_trace.py`, `openai_api/pricing.py` | Фазы attempts и суммирование стоимости разных моделей |
| `tests/test_validated_analysis_retry.py`, `tests/test_prompt_caching.py` | Регрессии FULL repair, transport, pricing, сохранения и параметров API |
| `ARCHITECTURE.md`, `Docs/full_analysis_repair_runbook.md`, `.gitignore` | Карта, операции и точечное разрешение версионировать новый runbook |
