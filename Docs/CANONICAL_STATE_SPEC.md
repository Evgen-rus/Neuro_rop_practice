# Canonical State Contract — проект

Статус: **только спецификация**. Engine не реализовывать. Production `main` (`8ca44afa`), snapshot, decision engine, SQLite и FULL/MINI/SKIP не менять.

Фактическая основа в локальном проекте:

- `Docs/final_audit.json`
- `Docs/final_audit_stage_1_5.json`

Машиночитаемые приложения рядом:

- `canonical_state_schema_draft.json`
- `canonical_state_test_matrix.json`

Код, с которым контракт должен быть совместим, но который пока не трогаем:

- `bitrix/context_sync.py` — overlap 15 мин, `merge_rows`, `retained_response` / `retain_failed_sources`
- `bitrix/customer_history.py` — `merge_items_by_id`, activity cursor `>=LAST_UPDATED`, communication `event_id`
- `bitrix/deals/1_fetch_deals_context.py` — `crm.activity.list` + `select FILES,COMMUNICATIONS`; `activity.get` не канонический источник
- `openai_api/change_detection/snapshot.py` — уже почти канон, но **хеширует `FILES.url` и кладёт `LAST_UPDATED` в activity snapshot**
- `openai_api/change_detection/decision_engine.py` — FULL/MINI/SKIP; canonical delta ≠ решение о LLM
- `storage/rop_db.py` — `entity_state` (fingerprint анализа), `crm_context_sync_state` (raw + cursors)

---

## 1. Зачем этот слой

Цепочка:

```text
BITRIX RAW
  → NORMALIZATION
  → CANONICAL STATE N
  → fresh incremental data
  → MERGE
  → CANONICAL STATE N+1
  → SEMANTIC DELTA
```

LLM на этом шаге нет. Canonical Bitrix State ≠ LLM Analysis ≠ FULL/MINI/SKIP.

Текущий production snapshot уже отличает `DATE_MODIFY` (metadata) от stage/amount. Audit 1 / 1.5 доказал, чего не хватает:

1. Сырой `FILES[].url` с `_efd` ломает semantic hash на каждом FULL.
2. `LAST_UPDATED` нужен как cursor, не как смысл.
3. Один Bitrix ID живёт через весь call lifecycle.
4. `crm.activity.get` беднее list (`COMMUNICATIONS` нет).
5. Incremental не означает «всё, чего нет в ответе, удалено».

---

## 2. Архитектурный принцип — максимально просто

Один документ состояния на карточку (deal или lead):

```text
canonical_state.json  =  owner + map[canonical_key → current entity]
```

Нет event store, checkpoint framework, shadow V1/V2, CQRS, графа.

Хранится **только текущая версия** каждого объекта. История revisions остаётся в audit/analysis, куда она уже пишется.

`schema_version: "1"` — это метка контракта, не линейка движков.

---

## 3. Recommended schema

Документ:

```json
{
  "schema_id": "canonical_bitrix_state",
  "schema_version": "1",
  "owner": {"entity_type": "deal", "entity_id": "18733"},
  "observed_at": "2026-09-07T11:46:46+03:00",
  "source_status": {
    "deal": "ok",
    "activities": "ok",
    "timeline_comments": "ok",
    "tasks": "ok",
    "im_messages": "ok"
  },
  "entities": {
    "activity:663125": {}
  },
  "semantic_fingerprint": "...",
  "raw_fingerprint": "...",
  "derived": {
    "communications_index": {
      "crm_activity:663125": "activity:663125",
      "crm_timeline_comment:2910619": "timeline_comment:2910619"
    }
  }
}
```

Одна entity (чуть проще, чем черновик из запроса: без сырого Bitrix-объекта внутри):

```json
{
  "key": "activity:663125",
  "entity_type": "activity",
  "subtype": "call",
  "source_id": "663125",
  "semantic": {},
  "metadata": {
    "last_updated": "2026-09-07T12:21:00+03:00",
    "created": "2026-09-07T12:19:12+03:00",
    "source": "crm.activity.list"
  },
  "semantic_fingerprint": "...",
  "raw_fingerprint": "..."
}
```

Почему не ещё проще (один плоский hash на всю сделку без map): audit требует per-ID revision vs NEW. Map по ключу — минимум.

Почему не сложнее (хранить raw blob + semantic): raw уже лежит в workspace/`crm_context_sync_state`. Дублировать PII незачем.

`subtype` — производный ярлык (`call`/`email`/`message`/`task`/`other`), **не часть ключа**.

`observed_at` разрешён только как диагностическое поле документа. Он не является свойством Bitrix entity и не участвует ни в entity-, ни в document-level fingerprint.

---

## 4. Identity rules

```text
canonical_key = entity_type + ":" + Bitrix ID
```

Нормализация ID: `str(id).strip()`, без ведущих нулей отдельно (Bitrix уже отдаёт десятичную строку). Пустой ID не канонизируется.

| entity_type | ключ | источник | audit |
| --- | --- | --- | --- |
| `deal` | `deal:18733` | `crm.deal.get` | ID стабилен |
| `activity` | `activity:663125` | **только** `crm.activity.list` | call T1–T5 тот же ID |
| `task` | `task:49769` | Bitrix tasks API (`bitrix_tasks`) | дедлайн менялся, ID нет |
| `timeline_comment` | `timeline_comment:2910619` | `crm.timeline.comment.list` | ID стабилен, текст нет |
| `im_message` | `im_message:{id}` | `im.dialog.messages.get` | ID стабилен на этапе 1 |

Activity-задача (`TYPE_ID=6` / `CRM_TASKS_TASK`, пример `627763`) — это **`activity:627763`**, не `task:`. Разные API, разные ключи, даже если числа совпадут.

### Communication

Отдельный stored entity type **не нужен**. Это производный индекс поверх тех же Bitrix ID:

```text
crm_activity:{id}            → activity:{id}
crm_timeline_comment:{id}    → timeline_comment:{id}
internal_im_chat:{id}        → im_message:{id}   (если source_id = IM id)
```

Так сохраняется текущая идея `event_id` из `build_normalized_communications`, но не появляется второй объект с тем же смыслом.

Вне scope v1: зеркала `crm_mirror:{hash(content)}` — identity от текста, не от Bitrix ID. В канон не копировать, пока нет стабильного ID.

### Запрещено

- subtype в ключе (`call:663125` — нет);
- file id как identity звонка;
- URL как identity файла;
- считать `crm.activity.get` каноническим источником.

---

## 5. Три слоя — да, они здесь уместны

Текущий `snapshot.py` уже делит сделку на business fields vs `metadata.date_modify`. Для activity этого деления нет: в одном объекте лежат и смысл, и `last_updated`, и `url_hash`.

Рекомендуемое деление **на entity**:

| слой | вопрос | попадает в semantic_fingerprint? |
| --- | --- | --- |
| identity | какой это объект | ключ, не hash |
| semantic | изменился ли смысл для анализа | да |
| revision-relevant metadata | cursor / reconciliation (`LAST_UPDATED`, `DATE_MODIFY`, `MODIFY_BY_ID`) | нет; влияет на `raw_fingerprint` |
| diagnostic metadata | время fetch/normalize (`observed_at` и локальные timestamps) | нет; не влияет ни на один fingerprint |

Проще некуда: два hash (`raw` / `semantic`) + текущая проекция. Третий слой «ignore» — поля, которые не кладём никуда (PII-телефоны, токены URL).

Это **не** решение FULL/MINI/SKIP. Canonical только говорит, *что* изменилось.

---

## 6. Semantic vs metadata vs ignore

### Activity

| поле | класс | почему |
| --- | --- | --- |
| `ID` | IDENTITY | audit: стабилен на всём call lifecycle |
| `kind` / `TYPE_ID` | SEMANTIC | тип события; не ключ |
| `PROVIDER_ID` / `PROVIDER_TYPE_ID` | SEMANTIC | VOXIMPLANT vs EMAIL vs task |
| `ORIGIN_ID` | SEMANTIC | стабильный VI_externalCall; не ключ звонка |
| `SUBJECT` / `DESCRIPTION` | SEMANTIC | как hash; тела в каноне нет |
| `START_TIME` | SEMANTIC | когда началось |
| `END_TIME` | SEMANTIC | длительность; см. call-правила ниже |
| `DEADLINE` | SEMANTIC | для task-activity доказано; для call часто sentinel `9999-12-31` — всё равно в проекции, потому что смена дедлайна у email/task осмысленна |
| `COMPLETED` / `STATUS` | SEMANTIC | T05: N→Y и 1→2 |
| `DIRECTION` | SEMANTIC | вход/исход; decision engine смотрит incoming |
| `RESPONSIBLE_ID` | SEMANTIC | кто ведёт событие |
| `OWNER_TYPE_ID` / `OWNER_ID` | SEMANTIC | к чему привязано |
| `SETTINGS.MISSED_CALL` | SEMANTIC | на T1 уже true; отличает недозвон от разговора |
| `FILES[].id` → `file_ids` | SEMANTIC | набор вложений / recording revision |
| `COMMUNICATIONS[].ENTITY_TYPE_ID` + `ENTITY_ID` | SEMANTIC | кто участник; сортировать |
| `LAST_UPDATED` | METADATA | cursor; T06 не semantic |
| `CREATED` | METADATA | на call совпадал со START_TIME и не двигался |
| `AUTHOR_ID` / `EDITOR_ID` | METADATA | не клиентский факт |
| `PROVIDER_GROUP_ID` | IGNORE | в audit всегда null |
| `FILES[].url` / `_efd` / `auth` | IGNORE | T02 |
| `COMMUNICATIONS[].VALUE` | IGNORE | телефон/email; PII; для identity не нужен |
| `LOCATION` / `PROVIDER_DATA` / сырой `DESCRIPTION` в metadata | IGNORE | пока не доказаны |
| `name` / `size` / `type` у файла | IGNORE до отдельной проверки | audit их не видел; T18 |

### Deal

| поле | класс |
| --- | --- |
| `ID` | IDENTITY |
| `STAGE_ID`, `CATEGORY_ID`, `OPPORTUNITY`, `CURRENCY_ID`, `ASSIGNED_BY_ID`, `CLOSED`, `MOVED_TIME`, `MOVED_BY_ID`, `LEAD_ID`, `CONTACT_ID`, `COMPANY_ID`, `TITLE` (hash) | SEMANTIC |
| `DATE_MODIFY`, `MODIFY_BY_ID` | METADATA (C_016) |
| `DATE_CREATE` | METADATA |
| пользовательские UF-* | IGNORE, пока не доказаны |

### Timeline comment

| поле | класс |
| --- | --- |
| `ID` | IDENTITY |
| текст (`COMMENT`) как hash, `file_ids` | SEMANTIC |
| `CREATED`, `AUTHOR_ID` | SEMANTIC для комментария (автор и время — часть события) |
| URL файлов | IGNORE |

`CREATED` у комментария оставляем semantic: это часть «какое это сообщение», а не cursor. Cursor timeline — другая механика (страницы до known ID).

### Task (tasks API)

| поле | класс |
| --- | --- |
| `ID` | IDENTITY |
| status, deadline, closedDate, responsible, title/description hashes | SEMANTIC |
| changedDate как единственное поле | METADATA, пока нет отдельного audit, что оно бывает без deadline |

### IM message

| поле | класс |
| --- | --- |
| `id` | IDENTITY |
| `dialog_id`, `date`, `author_id`, `text` hash | SEMANTIC |
| вложения IM | `file_ids` если есть числовой id; URL ignore |

---

## 7. FILES normalization

Доказано audit 1.5:

```text
FILES = [{id, url}]
url query _efd ротируется
id стабилен, пока Bitrix не подменит файл
```

Каноническая проекция **только**:

```json
{ "file_ids": ["1012207"] }
```

Правила:

1. взять `id` или `ID`;
2. отбросить пустые;
3. `str`, unique, **sort**;
4. URL, `_efd`, `auth`, `name`/`size`/`type` не входят.

Identity файла = `FILES[].id`.  
Identity звонка = `activity:{ID}`.  
`1012207 → 1012215` у того же activity = **semantic revision** (T04), не новый звонок.

Текущий `snapshot.normalize_files` считает `url_hash` — это как раз ложный FULL-шум 9 писем. **Не чинить сейчас**; канон просто не копирует эту ошибку.

---

## 8. LAST_UPDATED

Доказано:

- завершение звонка → `LAST_UPDATED` меняется (663125 T1→T2);
- замена recording file → `LAST_UPDATED` меняется (663127).

Формально:

```text
raw LAST_UPDATED изменился
  → объект = revision candidate (перечитать semantic)

semantic projection та же
  → UPDATED_TECHNICAL   (raw_revision_changed, semantic_revision_changed=false)

semantic projection другая
  → UPDATED_MEANINGFUL  (оба true)
```

`LAST_UPDATED changed` **само по себе** не semantic change (T06).

Fetch cursor production (`>=LAST_UPDATED` − 15 мин) остаётся как есть. Canonical его не заменяет.

Пустой FILES → первый recording **не наблюдался**. Production `refresh_missing_call_files` через `activity.get` — hedge снаружи канона; в v1 engine get не обязателен.

---

## 9. Call-specific semantic rules

Один ключ `activity:663125` на все стадии. Revisions того же объекта:

| факт | semantic revision? | комментарий |
| --- | --- | --- |
| `COMPLETED` N→Y и/или `STATUS` 1→2 | **да** | T05; смысл «звонок завершён» |
| recording file id появился или сменился | **да** | T04; смысл «есть/другая запись» |
| только `LAST_UPDATED` | **нет** | UPDATED_TECHNICAL |
| `END_TIME` изменился относительно предыдущего канона | **да**, если значение реально другое | длительность/конец разговора |
| `END_TIME` == `START_TIME` на missed call и так и остаётся | нет изменения | 663125 |
| `SETTINGS.MISSED_CALL` false→true или true→false | **да** | |
| `ORIGIN_ID` заполнился с пустого | **да** | late provider id; на 663125 был с T1 |

Canonical **не** говорит «надо вызывать LLM».  
`COMPLETED` недозвона — meaningful CRM revision и всё же может остаться soft для decision engine. Это следующий слой.

---

## 10. Activity source

Канонический source activity: **`crm.activity.list`** с `select: ["*", "FILES", "COMMUNICATIONS"]`.

`crm.activity.get`:

- не содержит `COMMUNICATIONS` даже с `select`;
- FILES id те же;
- не делать обязательным per-activity;
- не подменять list.

---

## 11. Merge algorithm

Вход: `state` (canonical document) + `batch` (нормализованные entities одного или нескольких источников + `ok`/`source_status`).

```text
merge(state, batch) → (state', delta)
```

1. Если `state` пустой и batch успешен → все ключи **NEW** (первый FULL = Canonical State 1).
2. Если источник в batch `ok=false` → entities этого источника **не трогать**; выставить `source_status=failed/stale`; delta без wipe (I5). Совпадает по духу с `retain_failed_sources`.
3. Для каждого успешного entity в batch:
   - ключа не было → insert, NEW;
   - ключ был, semantic_fp тот же, raw_fp другой → replace metadata, UPDATED_TECHNICAL;
   - ключ был, semantic_fp тот же, raw_fp тот же → нет entry (идемпотентность I2);
   - ключ был, semantic_fp другой → replace целиком, UPDATED_MEANINGFUL.
4. Ключи, которых нет в успешном incremental batch, **оставить** (I4). Не REMOVED.
5. FULL reconciliation (`force_full`): replace entities этого источника целиком **пришедшим множеством**, но удаление по-прежнему **не подтверждено audit-ом**. Даже на FULL v1 **не эмитит REMOVED**. Расхождение «было в каноне, нет в FULL» — отдельный future `ABSENT_ON_FULL_UNCONFIRMED` только в лог/debug, не в delta contract. Проще и безопаснее: v1 FULL тоже только upsert по ID (как сейчас `merge_items_by_id` на incremental). Для сходимости I8 на deal 18733 этого достаточно: Z не терял ID.
6. Пересчитать агрегатные fingerprint документа.
7. `communications_index` пересобрать из текущих keys.

Порядок API не влияет: fingerprint считается по отсортированным ключам и отсортированным `file_ids`.

---

## 12. Delta contract

Типы v1:

```text
NEW
UPDATED_MEANINGFUL
UPDATED_TECHNICAL
```

`UNCHANGED` — классификация внутри merge, **в entries по умолчанию не пишем** (на сделке 134+ activity это шум). В тесте можно включить `include_unchanged`.

`REMOVED` / `REMOVED_UNCONFIRMED` — **нет**. Отсутствие в incremental = нет информации.

Запись:

```json
{
  "key": "activity:663125",
  "entity_type": "activity",
  "subtype": "call",
  "change_type": "UPDATED_MEANINGFUL",
  "changed_semantic_fields": ["completed", "status"],
  "changed_metadata_fields": ["last_updated"],
  "before": { "...semantic..." },
  "after": { "...semantic..." },
  "before_metadata": { "last_updated": "..." },
  "after_metadata": { "last_updated": "..." }
}
```

Правила:

- **NEW**: `before=null`, `after=semantic`; metadata after можно положить.
- **UPDATED_***: оба semantic; так видно, *что* стало. Correctness важнее токенов.
- Тела писем/комментариев в delta нет — только hashes. Текст остаётся в raw workspace; будущий LLM incremental читает raw по `key`.
- Агрегат: `from_semantic_fingerprint` / `to_semantic_fingerprint` (и raw аналоги).

Это **не** prompt и не analysis JSON.

---

## 13. Fingerprints

Два SHA-256 hex.

**raw_fingerprint** entity: hash semantic-проекции вместе только с revision-relevant metadata (`LAST_UPDATED`, `DATE_MODIFY`, `MODIFY_BY_ID` и аналогичными доказанными cursor/revision полями).

```text
semantic_fingerprint = sha256(canonical_json(semantic))
raw_fingerprint      = sha256(canonical_json({semantic, revision_relevant_metadata}))
```

Из обоих fingerprint безусловно исключены `observed_at`, время fetch, локальные timestamps получения/нормализации, `FILES[].url`, `_efd`, `auth` и token-параметры. T02: metadata тот же, semantic тот же → оба fingerprint те же, delta пустая. Это **правильно**: `_efd` не revision.

T06 only LAST_UPDATED: metadata меняется → raw меняется → UPDATED_TECHNICAL.

Третьего `raw_input_fingerprint` в v1 нет: он не нужен для semantic delta и вернул бы transport churn.

Документ:

```text
document.semantic_fingerprint = sha256(sorted map key → entity.semantic_fingerprint)
document.raw_fingerprint      = sha256(sorted map key → entity.raw_fingerprint)
```

### Canonical JSON

```text
json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
UTF-8 → sha256 hex
```

- missing string → `""`
- missing list → `[]`
- missing bool `missed_call` → `null`
- числа файлов и ID — строки
- время — как отдал Bitrix, без пересборки тайзоны

Кириллица как есть, без `\uXXXX`.

---

## 14. Invariants

**I1** Один `canonical_key` → максимум одна current entity.

**I2** `merge(S, X); merge(result, X)` при одинаковом успешном Bitrix payload → тот же state и пустой delta, даже если `observed_at` различается.

**I3** Тот же ID заменяет current, не создаёт второй объект.

**I4** Incremental success без объекта ≠ удаление.

**I5** Неуспешный source не затирает хороший state пустым (как `retained_response`).

**I6** Порядок list/files не влияет на semantic_fingerprint.

**I7** URL / `_efd` / file token не влияют ни на semantic_fingerprint, ни на raw_fingerprint.

**I8** FULL и накопленный INCREMENTAL при одном фактическом Bitrix state → один semantic_fingerprint. На audit C_018 vs Z объектный набор совпал; канон обязан совпасть и по file_ids, несмотря на разные URL.

Дополнительно:

**I9** `crm.activity.get` не является входом merge.

**I10** Canonical state не пишет analysis JSON / не меняет `entity_state.current_fingerprint` decision engine.

**I11** Один Bitrix payload при разных `observed_at` имеет одинаковые entity- и document-level fingerprint.

---

## 15. Storage recommendation

Сейчас **никаких migrations**.

| место | плюс | минус | вердикт |
| --- | --- | --- | --- |
| `entity_state.snapshot_json` | уже есть per deal/lead | Это fingerprint FULL/MINI/SKIP. Подмена сломает production | **нельзя** |
| `crm_context_sync_state.payload` | CAS вместе с raw | Смешивает raw и канон; раздувает каждый fetch; баг engine затронет sync | не в 2C |
| `deal_semantic_checkpoints` | таблица есть | Это LLM semantic, другой контракт | **нельзя** |
| JSON в workspace рядом с `deal_{id}_context.json` | как raw: atomic_json, изолируется `--output-dir`, тесты без DB | не единственный source of truth при краше до записи sqlite | **2C: да, рядом, opt-in, не читается decision engine** |
| новая таблица | чисто | migration | только после review, не сейчас |

Рекомендация: канон — **отдельный data layer**. Первый код (2C) — чистые функции + фикстуры. Запись на диск — только в audit/output-dir, не в production `entity_state`.

---

## 16. Совместимость

FULL / MINI / SKIP продолжают жить от `snapshot.py` + `decision_engine.py` + `entity_state`.

Canonical рядом. Не меняет analysis JSON, API, UI.

Canonical state остаётся только Bitrix data layer: он не хранит и не дублирует LLM analysis, `entity_memory`, semantic checkpoints или FULL/MINI/SKIP state. Будущий evidence/transcript delta — отдельный слой поверх activity identity: неизменившиеся старые transcripts не отправляются, новый transcript или revision transcript того же звонка передаётся вместе с Semantic Delta.

Пока engine не подключён, production поведение байтово прежнее.

---

## 17. Первый FULL vs incremental

Первый успешный полный контекст → Canonical State 1 = все keys NEW. Это **не** первый FULL LLM.

Дальше:

```text
State N + incremental batch → State N+1 + Semantic Delta
```

Delta — будущий вход LLM incremental. Не сейчас.

---

## 18. Test matrix

Полная спецификация: `canonical_state_test_matrix.json`.

Обязательные (реальные audit-кейсы):

| ID | смысл | ожидание |
| --- | --- | --- |
| T01 | один payload при разных `observed_at` | оба fingerprint SAME; delta EMPTY; I2/I11 |
| T02 | email `_efd` | оба fingerprint SAME; delta EMPTY |
| T03 | порядок file id | оба fingerprint SAME; delta EMPTY |
| T04 | recording `1012207→1012215` | same key, MEANINGFUL |
| T05 | call COMPLETED/STATUS | same key, MEANINGFUL |
| T06 | только LAST_UPDATED | TECHNICAL |
| T07 | deal DATE_MODIFY/MODIFY_BY_ID | semantic SAME; raw DIFFERENT; TECHNICAL |
| T08 | email `662983` новый | NEW |
| T09 | тот же `662983` дозрел | revision, не NEW |
| T10 | comment `2910619` | same identity, MEANINGFUL |
| T11 | deadline задачи | MEANINGFUL |
| T12 | повтор incremental | без дубля |
| T13 | неполный incremental | старые не исчезают |
| T14 | failed read | предыдущий state жив |
| T15 | C_018 vs Z deal `18733` | при одинаковом Bitrix state оба fingerprint SAME; delta EMPTY |
| T16 | list vs get | get не вход |
| T17 | subtype ≠ identity | `activity:663125` |
| T18 | name/size на FILES | игнор |

Тесты **пока не писать** (этап 2B).

### Privacy-safe fixtures

Не класть в git:

- `deal_18733_context.json` целиком;
- SUBJECT/DESCRIPTION/COMMENT;
- телефоны `COMMUNICATIONS.VALUE`;
- query `auth`/`_efd`.

Класть:

- реальные ID (они уже в audit reports);
- структурные поля COMPLETED/STATUS/LAST_UPDATED/file ids;
- синтетический текст той же длины-класса или готовые hashes;
- URL вида `https://example.invalid/bitrix/tools/crm_show_file.php?fileId=964461&_efd=TESTA`.

Для T15: строить фикстуру из audit raw **локально** в `/var/tmp/...`, в git — список ID + file_ids + hashes, не тела.

---

## 19. Minimal implementation plan

После review этой спецификации, **не раньше**:

### 2B — tests/fixtures

- Privacy-safe фикстуры T01–T18.
- Пока без production import, если можно: чистые expected JSON.
- Не копировать customer PII в репозиторий.

### 2C — engine (после зелёной матрицы или вместе с ней, но не в production flow)

Минимальный модуль, например `bitrix/canonical_state.py` (имя согласовать на 2C):

- `canonical_key`
- `activity_subtype`
- `normalize_file_ids`
- `project_activity` / `project_deal` / …
- `fingerprints`
- `merge`
- `diff`

Без: LLM, snapshot.py правок, decision_engine, migrations, записи в `entity_state`.

### Потом отдельно

- opt-in запись `canonical_state.json` в audit/output;
- только после доказанной I8 на 18733 — обсуждать, подмешать file_ids в production snapshot (это уже **изменение** FULL/MINI и отдельный разрешённый шаг).

STOP после 2C, пока нет явного «подключай к анализу».

---

## 20. Risks / remaining unknowns

- Empty FILES → первый recording vs LAST_UPDATED не пойман. Cursor скорее всего ок (хедж get в audio). Канон всё равно semantic по `file_ids`, когда они появятся в list.
- Новый реальный email-файл не наблюдался; T08 покрывает NEW activity, не «+1 file к старому письму».
- `name`/`size`/`type` на FILES не проверены (T18 freeze).
- Не-VOXIMPLANT звонки не смотрели.
- Удаление activity не смотрели → нет REMOVED.
- Зеркала мессенджера с hash-id вне v1.
- `DEADLINE=9999-12-31` у call шумит меньше, чем URL, но поле остаётся semantic — если Bitrix начнёт его дёргать без смысла, понадобится sentinel-нормализация.
- Два пространства ID: `task:*` vs `activity:*`. Путаница в фикстурах — явный риск тестов, не runtime.

---

## 21. Confidence

**8 / 10**

Минус: empty→first recording; communication-mirrors; FULL deletion semantics.

---

## SPEC_CONSISTENT_FOR_2B

```text
SPEC_CONSISTENT_FOR_2B = YES
```

Это готовность к **2B (privacy-safe fixtures/tests)**, затем 2C (чистые функции).

Не делать сейчас: production engine wiring, snapshot.py, migrations, LLM incremental.

---

## Краткая сводка для review

### Recommended schema

Документ-map `entities[key]`, две проекции, два fingerprint. Без raw blob и без event log.

### Identity rules

`entity_type:BitrixId`. Subtype не ключ. Communication — индекс, не вторая сущность.

### Semantic vs metadata

Смысл vs cursor. `LAST_UPDATED` / `DATE_MODIFY` / `_efd` не semantic.

### FILES normalization

Отсортированные `file_ids`. URL запрещён.

### Merge algorithm

Upsert по ключу; failed retain; incremental не удаляет.

### Delta contract

`NEW` / `UPDATED_MEANINGFUL` / `UPDATED_TECHNICAL`. Без REMOVED. before/after = semantic.

### Invariants

I1–I11 как выше. Ключевые I8 и I11.

### Storage recommendation

Не `entity_state`. Не checkpoint-таблицы. Сначала функции; JSON рядом с workspace только opt-in.

### Test matrix

T01–T18 в `canonical_state_test_matrix.json`.

### Minimal implementation plan

2B → 2C рядом с production, без смены FULL/MINI/SKIP.

### Risks

См. §20.

### Confidence

8/10
