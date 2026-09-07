# План реализации настоящего инкрементального анализа сделок

## 1. Цель проекта

Текущий FULL-анализ сделок устраивает по качеству.

Проблема — стоимость повторных анализов.

При каждом новом FULL модель снова получает большой объём уже известного контекста:

* историю сделки;
* старые коммуникации;
* старые звонки;
* старые транскрипты;
* CRM-контекст;
* текущую ситуацию;
* knowledge base;
* диагностику;
* другие уже известные данные.

Главная цель:

> сохранить качество существующего FULL, но перестать повторно отправлять модели неизменившуюся историю и evidence.

Экономия должна достигаться не за счёт ослабления анализа, а за счёт уменьшения повторяющегося input context.

---

# 2. Целевая модель

В конечной системе есть два платных LLM-режима:

## INITIAL_FULL / FULL_REBUILD

Полный анализ.

Используется:

* при первом анализе;
* если нет trusted previous analysis;
* если baseline повреждён или несовместим;
* если incremental невозможно безопасно выполнить;
* как fallback после невалидного incremental.

## INCREMENTAL

Основной режим для последующих значимых изменений.

Вместо полной старой истории модель получает:

```text
PREVIOUS TRUSTED COMPLETE ANALYSIS
+
CRM SEMANTIC DELTA
+
NEW / REVISED CLIENT EVIDENCE
+
NEW / REVISED TRANSCRIPTS
+
CURRENT REQUIRED CRM FACTS
+
RELEVANT KNOWLEDGE / POLICY
```

На выходе INCREMENTAL обязан вернуть:

```text
полный актуальный analysis JSON
```

а не patch/diff.

Внешний контракт должен сохраняться:

```text
FULL output schema
==
INCREMENTAL output schema
```

UI, API, reports и downstream logic не должны зависеть от режима анализа.

---

# 3. Главный архитектурный принцип

Нельзя строить LLM incremental поверх ненадёжной инкрементальности данных.

Целевая цепочка:

```text
BITRIX / TRANSCRIPTS / CLIENT EVIDENCE
        ↓
CURRENT CANONICAL / EVIDENCE STATE
        ↓
COMPARE WITH LAST TRUSTED ANALYSIS BASELINE
        ↓
CRM + EVIDENCE + TRANSCRIPT DELTA
        ↓
FULL / MINI / SKIP / INCREMENTAL DECISION
        ↓
LLM
        ↓
VALIDATION
        ↓
PERSISTENCE
        ↓
NEW TRUSTED ANALYSIS BASELINE
```

Главный invariant:

> Ни одно изменение, которого ещё не видел последний успешный trusted analysis, не должно потеряться из будущего incremental input.

---

# 4. Текущее production-состояние

На текущем `main` pipeline сделок примерно:

```text
Bitrix / workspace
→ snapshot
→ compare_snapshots
→ decision engine
→ FIRST_FULL / FULL / MINI / SKIP
```

Существуют статусы:

```text
FIRST_FULL_ANALYSIS
FULL_LLM_ANALYSIS
INCREMENTAL_LLM_ANALYSIS
MINI_RECOMMENDATION_NO_LLM
SKIPPED_NO_CHANGES
ERROR
```

Но настоящий INCREMENTAL сейчас ещё не активен.

Текущий `decide_deal_processing()`:

```text
hard meaningful changes
→ FULL_LLM_ANALYSIS

soft/control changes
→ MINI_RECOMMENDATION_NO_LLM

нет meaningful changes
→ SKIPPED_NO_CHANGES
```

`INCREMENTAL_LLM_ANALYSIS` как константа существует, но текущий deal decision engine сам этот режим фактически не выбирает.

Кроме того, в `analyze_deal_if_changed.py` остаётся защитная ветка:

```text
INCREMENTAL_LLM_ANALYSIS
→ FULL_LLM_ANALYSIS
```

Поэтому будущая реализация требует **двух отдельных изменений**:

1. настоящий incremental analyzer;
2. routing/decision policy, которая сможет безопасно выбирать его.

Недостаточно просто удалить conversion `INCREMENTAL → FULL`.

---

# 5. Существующий FULL — эталон качества

Текущий FULL не переписывать без необходимости.

Он становится:

```text
INITIAL_FULL
```

и

```text
FULL_REBUILD
```

## INITIAL_FULL

Первый анализ сделки.

Получает полный требуемый контекст и создаёт первый complete trusted analysis.

## FULL_REBUILD

Редкий fallback.

Допустимые причины:

```text
trusted analysis missing
trusted baseline missing
canonical integrity failure
evidence coverage untrusted
schema/version incompatibility
incremental validation failure
incremental execution failure
unresolved contradiction
```

Большое количество изменений само по себе не является причиной FULL_REBUILD.

---

# 6. Этап 1 / 1.5 — COMPLETE

Реальное поведение Bitrix исследовано отдельным audit.

Источники:

```text
Docs/final_audit.json
Docs/final_audit_stage_1_5.json
Docs/BITRIX_INCREMENTAL_AUDIT.md
```

Подтверждено:

* Bitrix ID стабилен в наблюдавшихся сущностях;
* новый ID = новая сущность;
* тот же ID = revision;
* merge по ID не создавал дублей;
* накопленный incremental и финальный FULL сошлись по набору объектов;
* activity может дозаполняться после создания;
* timeline comment может измениться при том же ID;
* task deadline/status могут меняться при том же ID;
* `DATE_MODIFY` сделки может меняться без semantic change;
* `LAST_UPDATED` полезен как revision/cursor signal, но не semantic факт сам по себе.

---

# 7. FILES — доказанное правило

Audit 1.5 показал:

```text
FILES = [
    {
        id,
        url
    }
]
```

У старых email менялся только query token `_efd`.

При этом:

```text
file id
количество файлов
порядок
```

оставались стабильными.

Поэтому:

```text
FILES[].id
```

является canonical semantic identity файла.

Запрещено использовать в canonical fingerprints:

```text
FILES[].url
_efd
auth
temporary token
```

Для звонка наблюдалась замена recording:

```text
1012207
→
1012215
```

при том же activity ID.

Это:

```text
revision существующего звонка
```

а не новый звонок.

---

# 8. CALL lifecycle — доказанное правило

Наблюдался один activity ID на нескольких стадиях:

```text
COMPLETED N → Y
STATUS 1 → 2
LAST_UPDATED changed
```

Activity ID и ORIGIN_ID остались теми же.

Для другого answered call позднее сменился recording file ID, и `LAST_UPDATED` также изменился.

Следовательно наблюдавшийся production cursor способен увидеть такие late revisions.

Canonical identity звонка:

```text
activity:<Bitrix activity ID>
```

Recording file ID не является identity звонка.

---

# 9. crm.activity.list — canonical source

Audit показал:

```text
crm.activity.get
```

не является более полным источником.

В частности `COMMUNICATIONS` присутствовали в list и отсутствовали в get.

Поэтому activity canonical source v1:

```text
crm.activity.list
```

`activity.get` может использоваться как дополнительный технический механизм, но не заменяет list.

---

# 10. Неизвестные Bitrix-сценарии

Пока не доказаны:

* empty FILES → первый recording;
* confirmed deletion activity;
* non-VOXIMPLANT providers;
* новый attachment у уже существующего email;
* `name/size/type` FILES на других порталах;
* некоторые hash-based messenger mirrors.

Они не блокируют v1.

Поведение должно оставаться консервативным.

Особенно:

```text
объект отсутствует в incremental response
!=
REMOVED
```

---

# 11. Этап 2A — COMPLETE

Canonical State Contract утверждён:

```text
Docs/CANONICAL_STATE_SPEC.md
Docs/canonical_state_schema_draft.json
Docs/canonical_state_test_matrix.json
```

Canonical State является отдельным Bitrix data layer.

Он не заменяет:

* LLM analysis;
* `entity_memory`;
* FULL/MINI/SKIP state;
* analysis provenance;
* evidence coverage.

---

# 12. Canonical identity

Основное правило:

```text
canonical_key =
entity_type + ":" + Bitrix ID
```

Примеры:

```text
deal:18733
activity:663125
task:49769
timeline_comment:2910619
```

Subtype:

```text
call
email
message
task
```

не входит в identity.

---

# 13. Canonical fingerprints

Используются два fingerprint.

## semantic_fingerprint

```text
hash(semantic)
```

Меняется только при изменении business meaning.

## raw_fingerprint

```text
hash(
    semantic
    +
    revision-relevant metadata
)
```

Revision metadata может включать:

```text
LAST_UPDATED
DATE_MODIFY
MODIFY_BY_ID
```

Не входят ни в один fingerprint:

```text
observed_at
fetch timestamp
local normalization timestamp
FILES URL
_efd
auth/token
transport metadata
```

---

# 14. Canonical delta v1

Типы:

```text
NEW
UPDATED_MEANINGFUL
UPDATED_TECHNICAL
```

`UNCHANGED` обычно не emit.

Пока отсутствуют:

```text
REMOVED
REMOVED_UNCONFIRMED
```

Примеры:

```text
новый email ID
→ NEW

тот же call ID, COMPLETED N→Y
→ UPDATED_MEANINGFUL

тот же call ID, recording file changed
→ UPDATED_MEANINGFUL

только LAST_UPDATED
→ UPDATED_TECHNICAL

только DATE_MODIFY
→ UPDATED_TECHNICAL
```

---

# 15. CURRENT STEP — 2B

```text
CURRENT STEP = 2B
```

## Privacy-safe fixtures + tests

Источник:

```text
Docs/canonical_state_test_matrix.json
```

Тесты T01–T18.

Fixtures не должны содержать customer PII.

Не коммитить:

* полный raw Bitrix bundle;
* SUBJECT;
* DESCRIPTION;
* COMMENT;
* `COMMUNICATIONS.VALUE`;
* реальные phone/email;
* настоящие auth/_efd tokens.

Использовать минимальные synthetic fixtures, воспроизводящие структуру реальных audit cases.

---

# 16. Этап 2C — minimal canonical engine

После 2B.

Минимальные чистые функции:

```text
canonical_key
activity_subtype
normalize_file_ids

project_activity
project_deal
project_task
project_timeline_comment
project_im_message

fingerprints
merge
diff
```

На этом этапе запрещено:

```text
LLM
decision engine wiring
snapshot.py replacement
DB migration
entity_state modification
production integration
```

---

# 17. Этап 2D — mandatory replay

Unit tests недостаточно.

До перехода к LLM необходимо replay сохранённых audit-сценариев.

Минимально проверить:

```text
A/B FILES _efd
→ delta EMPTY

call completion
→ same ID + meaningful revision

recording replacement
→ same call + meaningful revision

email 662983
→ NEW
→ later revision

timeline 2910619
→ same identity + revision

tasks
→ deadline/status revisions

C_018 accumulated incremental
vs
Z FULL
→ same canonical state
```

Только после успешного replay canonical layer считается proven.

---

# 18. Критически важно: CURRENT STATE ≠ TRUSTED ANALYSIS BASELINE

Current canonical state и state, который последний successful LLM реально видел, — разные вещи.

Это обязательный архитектурный invariant.

Опасный сценарий:

```text
FULL A success
        ↓
Bitrix change X
        ↓
canonical обновился
        ↓
MINI / SKIP / LLM error
        ↓
Bitrix change Y
```

Если следующий LLM получит только:

```text
previous fetch → current fetch
```

он может увидеть Y и потерять X.

Поэтому нужны два логических состояния:

```text
CURRENT CANONICAL STATE
```

и

```text
LAST TRUSTED ANALYSIS BASELINE
```

Либо эквивалентный механизм:

```text
PENDING UNANALYZED DELTA
```

---

# 19. Trusted baseline продвигается только после успешного анализа

Baseline нельзя обновлять просто после Bitrix fetch.

Он продвигается только после:

```text
LLM SUCCESS
+
VALIDATION SUCCESS
+
PERSISTENCE SUCCESS
```

Если:

```text
incremental error
validation failed
persistence failed
```

baseline остаётся прежним.

Current canonical state при этом может продолжать обновляться.

Следующий LLM должен получить **все изменения относительно последнего trusted baseline**, а не только последнего fetch.

---

# 20. MINI / SKIP и baseline

MINI и SKIP не означают автоматически:

```text
LLM видел это изменение
```

Поэтому нужно чётко разделять:

```text
canonical state advanced
```

и:

```text
trusted LLM baseline advanced
```

Если событие было классифицировано как не требующее LLM, routing layer должен явно определить, должно ли оно считаться covered для будущего анализа.

По умолчанию нельзя молча считать его увиденным LLM.

---

# 21. Evidence / transcript delta — отдельный слой

Canonical state знает identity/hashes бизнес-событий.

Но будущему LLM нужны реальные тексты новых evidence.

В репозитории уже существует полезный фундамент:

```text
openai_api/llm/deal_evidence.py
```

В нём используются identity:

```text
call:<activity_id>
email:<activity_id>
message:<activity_id>
```

и:

```text
content_hash
revision
evidence coverage
evidence_ids_included
```

Эти primitives нужно переиспользовать, если они проходят тесты.

Не возрождать старую сложную V1/V2 incremental architecture.

---

# 22. Evidence identity

Для звонка:

```text
call:<activity_id>
```

Для входящего email:

```text
email:<activity_id>
```

Для входящего message:

```text
message:<activity_id>
```

Filename, filesystem path и mtime не являются business identity evidence.

---

# 23. Evidence revision

Если:

```text
evidence_id тот же
content_hash тот же
```

→ evidence уже известно, повторно не отправлять.

Если:

```text
evidence_id тот же
content_hash изменился
```

→ revision.

Передать новое содержимое LLM.

Если:

```text
evidence_id новый
```

→ NEW EVIDENCE.

---

# 24. Transcript delta

Особенно важно для стоимости.

Старые неизменившиеся транскрипты не должны каждый раз возвращаться в prompt.

Правила:

```text
new call transcript
→ send

same call + changed transcript content_hash
→ send revision

same call + same content_hash
→ don't send
```

Activity ID связывает:

```text
CRM call
↔
transcript evidence
```

---

# 25. Evidence coverage последнего trusted analysis

Недостаточно знать:

```text
evidence когда-то существовал
```

Нужно знать:

```text
какую revision/content_hash
последний trusted analysis реально получил
```

То есть baseline должен отражать actual LLM coverage.

Не считать evidence covered только потому, что оно лежит локально.

---

# 26. Этап 3 — CRM + evidence delta

После canonical replay.

Нужно получить два логических delta:

```text
CRM SEMANTIC DELTA
```

и

```text
CLIENT EVIDENCE / TRANSCRIPT DELTA
```

CRM delta строится относительно baseline состояния, соответствующего последнему trusted LLM analysis.

Evidence delta строится относительно trusted evidence coverage.

---

# 27. UPDATED_TECHNICAL и LLM

`UPDATED_TECHNICAL` нужен для диагностики/cursor/reconciliation.

Обычно его не требуется передавать LLM.

Например:

```text
LAST_UPDATED changed
semantic unchanged
```

не должен увеличивать платный prompt.

---

# 28. Этап 4 — Trusted analysis contract

Перед созданием incremental analyzer определить минимальные условия trusted baseline.

Например:

```text
previous complete analysis exists
last analysis run successful
analysis schema valid
schema/prompt version compatible
baseline reference/fingerprint known
evidence coverage trustworthy
```

Не создавать новый сложный semantic checkpoint framework.

Максимально переиспользовать текущий persisted complete analysis и provenance.

---

# 29. Этап 5 — настоящий INCREMENTAL LLM

Вход:

```text
PREVIOUS TRUSTED COMPLETE ANALYSIS

CRM SEMANTIC DELTA

NEW/REVISED CLIENT EVIDENCE

NEW/REVISED TRANSCRIPTS

CURRENT REQUIRED CRM FACTS

CURRENT STAGE POLICY

RELEVANT KNOWLEDGE
```

Не отправлять повторно:

```text
old unchanged history
old unchanged emails
old unchanged messages
old unchanged transcripts
```

---

# 30. Previous analysis — не абсолютная истина

Incremental prompt должен явно объяснять:

> Previous analysis — это предыдущее проверенное понимание сделки, а не immutable truth.

Новая evidence может:

```text
preserve
revise
invalidate
```

старые выводы.

Модель должна уметь:

* сохранять старые факты, если они всё ещё актуальны;
* пересматривать устаревшие выводы;
* разрешать contradictions;
* обновлять risk/next action/qualification;
* не придумывать evidence.

---

# 31. Incremental output

Output всегда полный.

Не:

```text
patch
diff
changed sections only
```

А:

```text
complete current analysis JSON
```

После normalizer/validator:

```text
FULL schema
==
INCREMENTAL schema
```

---

# 32. Knowledge Base

Не урезать KB без доказанной необходимости.

Основная экономия должна идти от исключения старой evidence/history.

На первом incremental MVP допустимо передавать тот же небольшой набор обязательных rules/policies, если его стоимость невелика.

Relevant retrieval/section selection можно оптимизировать позже.

---

# 33. Validation

Incremental проходит существующий production validator.

Не создавать отдельный слабый validator.

Проверяются те же обязательные поля и ограничения, что и для FULL.

---

# 34. Fallback

Если incremental:

```text
invalid
incomplete
schema incompatible
execution error
unsafe baseline
unresolved contradiction
```

→ выполнить один:

```text
FULL_REBUILD
```

Без бесконечной цепочки paid retries.

Причина fallback обязательно логируется.

Успешный FULL_REBUILD становится новым trusted baseline.

---

# 35. Decision policy

Routing подключается только после готового incremental analyzer.

Целевая логика примерно:

```text
нет trusted baseline
→ INITIAL_FULL

нет meaningful изменений
→ MINI / SKIP по текущей логике

meaningful analyzable change
+ trusted baseline
→ INCREMENTAL_LLM_ANALYSIS

incremental unsafe
→ FULL_REBUILD

--force-llm
→ FULL
```

Важно:

не каждое `UPDATED_MEANINGFUL` автоматически требует LLM.

Нужно сохранить существующее разделение business importance / hard / soft / MINI.

---

# 36. analyze_deal_if_changed.py

Текущую защиту:

```text
INCREMENTAL_LLM_ANALYSIS
→ FULL_LLM_ANALYSIS
```

не удалять заранее.

Удалять/заменять её только после:

```text
canonical tests green
audit replay green
evidence delta tests green
incremental analyzer tests green
validation/fallback tests green
```

---

# 37. Persistence

По возможности переиспользовать существующий successful persistence path:

```text
save_analysis_run
update_entity_memory
upsert_entity_state
analysis provenance
evidence_ids_included
```

FULL и INCREMENTAL должны иметь максимально общий final persistence flow.

Новая DB architecture допускается только если текущего storage объективно недостаточно.

---

# 38. Transactional invariant

Новый trusted baseline можно сохранить только после полного успешного цикла:

```text
LLM
↓
validation
↓
files/state persistence
↓
analysis run persisted
↓
baseline advancement
```

Нельзя сначала продвинуть baseline, а потом пытаться сохранить analysis.

---

# 39. Observability

Для каждого paid analysis желательно сохранять:

```text
analysis_mode

decision_reason

baseline_run_id
baseline_fingerprint

canonical_from_fingerprint
canonical_to_fingerprint

changed_entity_count
change types

evidence_delta_count
transcript_delta_count

input_tokens
cached_tokens
output_tokens

cost_usd
cost_rub

model
prompt_version

validation_result

fallback
fallback_reason
```

Только реальные usage metrics API.

Ничего не вычислять «на глаз», если метрики доступны напрямую.

---

# 40. Главная cost metric

Сравнивать:

```text
FULL input tokens
vs
INCREMENTAL input tokens
```

и:

```text
FULL cost
vs
INCREMENTAL cost
```

на одинаковых реальных состояниях сделки.

Не задавать заранее искусственный процент экономии.

Цель:

```text
значительно меньше input
при сопоставимом business-quality output
```

---

# 41. Этап 6 — FULL vs INCREMENTAL evaluation

Метод:

```text
STATE A
↓
FULL(A)
↓
ANALYSIS A

STATE B
```

Контроль:

```text
FULL(B)
```

Эксперимент:

```text
INCREMENTAL(
    ANALYSIS A,
    DELTA A→B
)
```

---

# 42. Что сравнивать

Не буквальный текст.

Сравнивать business meaning:

* deal state;
* main risk;
* qualification;
* new event;
* what changed;
* manager next action;
* ROP action;
* payment blocker;
* commitments;
* source conflicts;
* critical facts;
* communication quality;
* contradictions;
* hallucinations;
* lost important facts.

---

# 43. Multi-step drift test

Одного A→B сравнения недостаточно.

Нужно проверить:

```text
FULL A
→ INCREMENTAL B
→ INCREMENTAL C
→ INCREMENTAL D
→ INCREMENTAL E
```

Затем сравнить:

```text
INCREMENTAL CHAIN(E)
```

с:

```text
FRESH FULL(E)
```

Это защита от постепенного telephone-game drift.

---

# 44. Periodic FULL rebuild

Не вводить заранее фиксированный rebuild:

```text
каждые N запусков
```

без данных.

Если multi-step evaluation покажет накопление drift — определить rebuild policy экспериментально.

---

# 45. Production rollout

Только после локальных сравнений.

## Шаг 1

Небольшая выборка сделок.

## Шаг 2

Логировать:

```text
FULL
INCREMENTAL
fallback
cost
validation
```

## Шаг 3

Сравнивать реальные выводы РОПом.

## Шаг 4

Расширять постепенно.

Не создавать permanent shadow framework.

---

# 46. Совместимость

Не менять внешний contract:

```text
analysis JSON
API
UI
reports
```

Для пользователя FULL и INCREMENTAL должны давать один продуктовый результат.

Mode может присутствовать только как диагностическая metadata.

---

# 47. Не делать

Не создавать:

```text
Incremental V2
Incremental V3
Incremental V4
```

Не строить:

```text
сложный semantic checkpoint framework
event sourcing
CQRS
новый permanent shadow framework
```

Не делать migrations без необходимости.

Не переписывать существующий FULL.

Не ослаблять validation.

Не оптимизировать JSON ради нескольких токенов раньше основной экономии.

---

# 48. Актуальный roadmap

## COMPLETE

```text
Stage 1
Bitrix behavior audit

Stage 1.5
FILES + CALL lifecycle + list/get audit

Stage 2A
Canonical State Contract
```

## CURRENT

```text
Stage 2B
privacy-safe fixtures + tests
```

## NEXT

```text
Stage 2C
minimal canonical engine

Stage 2D
audit replay

Stage 3
CRM semantic delta
+
evidence/transcript delta

Stage 4
trusted baseline contract

Stage 5
incremental analyzer

Stage 5.1
decision routing

Stage 6
FULL vs INCREMENTAL evaluation
+
multi-step drift evaluation

Stage 7
controlled production rollout
```

---

# 49. Ключевой критерий готовности

Настоящий INCREMENTAL можно считать успешным только если одновременно выполняются:

## DATA CORRECTNESS

Новые/revised Bitrix/evidence данные не теряются.

## QUALITY

Business conclusions сопоставимы с свежим FULL.

## COST

Input context и стоимость существенно ниже повторного FULL.

## RELIABILITY

Обычные обновления не превращаются постоянно в FULL_REBUILD.

## COMPATIBILITY

UI/API/report contract не меняется.

---

# 50. Главный принцип проекта

Мы не делаем более дешёвый «обрезанный анализ».

Мы делаем тот же качественный актуальный анализ, но модель больше не получает каждый раз всю старую историю.

Формула:

```text
FIRST FULL
=
всё необходимое

NEXT ANALYSIS
=
previous trusted understanding
+
только то, что действительно изменилось
```

Главный технический invariant:

> Current state может двигаться постоянно, но trusted analysis baseline двигается только после успешного валидированного и сохранённого LLM analysis.

Главный продуктовый принцип:

> Сначала надёжная инкрементальность данных, затем инкрементальность LLM.

---

# CURRENT STATUS

```text
STAGE_1_BITRIX_AUDIT = COMPLETE
STAGE_1_5_FILES_CALL_AUDIT = COMPLETE
STAGE_2A_CANONICAL_SPEC = COMPLETE

CURRENT_STEP = 2B

NEXT_STEP = PRIVACY_SAFE_FIXTURES_AND_TESTS
```
