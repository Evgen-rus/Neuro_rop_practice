# BITRIX_INCREMENTAL_AUDIT.md

## Цель

Провести практический аудит того, как реальные данные Bitrix24 меняются между последовательными чтениями.

На этом этапе мы НЕ разрабатываем новый incremental LLM-анализ.

Нужно сначала доказать корректность цепочки:

```text
Bitrix
→ raw state
→ canonical state
→ merge
→ semantic delta
```

Главный вопрос:

**можем ли мы надёжно отличать новое событие, изменение существующего события и технический шум Bitrix.**

---

# Критическое правило

Эксперимент проводится на VPS рядом с production, но production менять нельзя.

Разрешено:

* читать данные Bitrix через существующий read-only API;
* запускать существующие read-only сборщики;
* создавать новые диагностические файлы;
* создавать отдельную audit SQLite;
* создавать отдельные snapshots;
* анализировать полученные данные;
* запускать безопасные read-only команды.

Запрещено:

* менять production SQLite;
* изменять существующие production records;
* менять `.env`;
* менять systemd;
* менять cron;
* менять nginx;
* менять Docker/deployment config;
* устанавливать новые системные пакеты без необходимости;
* перезапускать production services;
* менять production workspace;
* менять tracked source files;
* делать commit;
* делать push;
* делать merge;
* выполнять migrations;
* удалять или перезаписывать существующие production artifacts;
* запускать платный LLM-анализ.

---

# Изоляция эксперимента

Использовать отдельный audit root.

Предпочтительно:

```text
/var/tmp/neuro_rop_bitrix_audit/
```

Внутри:

```text
/var/tmp/neuro_rop_bitrix_audit/
├── raw/
├── snapshots/
├── canonical/
├── diffs/
├── reports/
├── logs/
└── audit.sqlite
```

Если `/var/tmp` по инфраструктурным причинам не подходит, выбрать другой новый отдельный каталог.

Главное условие:

**audit output никогда не должен писать в существующий production workspace.**

---

# Production Bitrix

Разрешено использовать существующие credentials Bitrix только для read-only методов.

Не менять данные в Bitrix.

Все API methods, которые будут вызваны экспериментом, предварительно перечислить.

Если обнаружен метод, который потенциально пишет данные, эксперимент остановить.

---

# ЭТАП A. Проверка существующей архитектуры

Сначала изучить текущий `main`.

Особенно:

* `bitrix/context_sync.py`
* `bitrix/customer_history.py`
* `bitrix/deals/1_fetch_deals_context.py`
* `openai_api/change_detection/snapshot.py`
* связанные workspace builders;
* merge helpers;
* incremental cursors;
* overlap;
* reconciliation;
* storage sync state.

Нужно понять:

1. Какие данные уже умеем получать incremental.
2. Где используется stable ID.
3. Как происходит merge старой и новой версии.
4. Какие источники каждый раз читаются полностью.
5. Какие источники читаются по cursor/watermark.
6. Где используется overlap.
7. Как обнаруживаются updated entities.
8. Какие timestamps участвуют в сравнении.
9. Какие timestamps уже исключены как технический шум.
10. Какие данные сейчас пишутся в production SQLite.

---

# ЭТАП B. Проверка возможности полной изоляции

До запуска сбора определить:

## Output

Можно ли перенаправить все generated files в audit root.

## SQLite

Можно ли передать отдельный:

```text
--db-path /var/tmp/neuro_rop_bitrix_audit/audit.sqlite
```

и гарантировать, что production DB не будет открыта на запись.

## Workspace

Можно ли использовать отдельный audit workspace.

## Environment

Какие функции получают DB path явно, а какие могут скрыто использовать `DEFAULT_DB_PATH`.

Это особенно важно.

Найти все случаи, когда audit-команда потенциально может обратиться к production DB несмотря на переданный `--db-path`.

Если такая возможность существует — до фактического запуска остановиться и описать её.

---

# ЭТАП C. Разработать безопасный runbook

До запуска эксперимента подготовить точные команды.

Пример структуры:

```bash
COMMAND 1 — первый полный snapshot

COMMAND 2 — повторный snapshot

COMMAND 3 — snapshot после события

COMMAND 4 — сравнение
```

Но реальные команды определить по текущему коду.

Для каждой команды указать:

* что она читает;
* куда пишет;
* какую DB использует;
* вызывает ли Bitrix;
* может ли повлиять на production;
* почему запуск безопасен.

---

# ЭТАП D. Что будем исследовать

После отдельного разрешения на запуск собрать реальные последовательные состояния.

Минимальные категории:

## Deal

* без изменений;
* изменение stage;
* изменение amount;
* изменение assigned manager;
* техническое изменение timestamps.

## Activity

* новая activity;
* повторное чтение той же activity;
* изменение существующей activity.

## Call

Особенно важно.

Поймать один call ID в разных состояниях:

```text
создан
→ идёт
→ завершён
→ появились дополнительные CRM-поля
→ появилась recording
→ появилась transcript
```

Проверить:

* остаётся ли ID одним и тем же;
* какие поля меняются;
* меняется ли `LAST_UPDATED`;
* меняются ли `START_TIME`;
* `END_TIME`;
* `COMPLETED`;
* `STATUS`;
* `FILES`;
* provider-specific fields;
* recording refs.

## Messenger

* новое входящее сообщение;
* исходящее сообщение;
* повторное чтение без изменений.

## Email

То же.

## Task

* создание;
* изменение deadline;
* завершение;
* повторное чтение.

## Timeline

* новый comment;
* повторное чтение;
* изменение существующего comment, если Bitrix это позволяет.

---

# ЭТАП E. Что сохранять для каждой итерации

Каждый snapshot должен иметь уникальный ID.

Например:

```text
2026-09-07T140000_snapshot_001
```

Сохранять:

## Raw

Полный ответ источника Bitrix, который реально использует текущая система.

## Normalized

Текущее normalized представление.

## Canonical candidate

Будущее предполагаемое полное состояние после merge.

Пока это диагностическая сущность, не production implementation.

## Previous vs Current

Для каждой сущности:

```text
entity_type
stable_id
previous_raw
current_raw
previous_normalized
current_normalized
changed_fields_raw
changed_fields_normalized
```

## Fingerprints

Если возможно:

```text
raw_fingerprint
normalized_fingerprint
semantic_fingerprint_candidate
```

---

# ЭТАП F. Классификация изменений

Пока не использовать это как production logic.

Агент должен аналитически классифицировать изменения:

```text
NEW
UPDATED_MEANINGFUL
UPDATED_TECHNICAL
UNCHANGED
REMOVED_OR_MISSING
UNCERTAIN
```

Главное — не скрывать неопределённость.

---

# Особая проверка timestamps

Для каждого типа сущности проверить:

* creation time;
* update time;
* business event time;
* start/end time;
* modified time;
* sync time.

Нужно выяснить:

**может ли изменение только timestamp создавать ложный semantic delta.**

В текущем snapshot logic уже есть пример, где `DATE_MODIFY` сделки исключается из fingerprint.

Нужно найти аналогичные случаи для activity и остальных сущностей.

---

# Особая проверка merge

Ключевой invariant:

```text
previous full state
+
new incremental fetch
=
new correct full state
```

Нужно доказать на реальных данных, что:

* старые сущности не теряются;
* новые добавляются;
* одинаковый ID обновляет существующую сущность;
* старый объект не остаётся одновременно с новой версией;
* неполный API response не превращается в массовое удаление;
* API failure не заменяет хороший старый state пустотой.

---

# Отдельно проверить reconciliation

В проекте уже есть periodic reconciliation.

Нужно установить:

* что именно она перечитывает;
* как часто;
* действительно ли может поймать позднее изменение старой сущности;
* какие сущности reconciliation не покрывает.

---

# Результат исследования

В конце создать:

```text
/var/tmp/neuro_rop_bitrix_audit/reports/final_audit.json
```

и

```text
/var/tmp/neuro_rop_bitrix_audit/reports/final_audit.md
```

JSON — основной источник для дальнейшего анализа агентом.

Markdown — краткий summary.

---

# Структура final_audit.json

Ориентировочно:

```json
{
  "sources": {},
  "entities": {},
  "stable_identity_findings": {},
  "mutable_fields": {},
  "technical_noise_fields": {},
  "meaningful_fields": {},
  "late_filling_fields": {},
  "merge_findings": {},
  "reconciliation_findings": {},
  "false_positive_cases": [],
  "false_negative_risks": [],
  "unknowns": [],
  "recommended_canonical_rules": [],
  "recommended_next_experiments": []
}
```

Не подгонять реальные данные под эту schema, если исследование покажет более подходящий формат.

---

# STOP CONDITION

После завершения Bitrix audit:

**НЕ начинать реализацию нового incremental LLM.**

Не менять production snapshot/delta logic автоматически.

Не исправлять найденные проблемы «по ходу».

Сначала предоставить результаты исследования.

По этим результатам отдельно проектируется:

```text
canonical state
→ semantic delta engine
```

И только после него:

```text
LLM incremental
```

---

# Главный критерий успеха

После исследования мы должны с высокой уверенностью ответить:

> Если Bitrix сегодня вернул сущность X, а через 10 минут снова вернул ту же X, можем ли мы точно определить, произошло новое бизнес-событие, содержательно изменилась существующая сущность или изменился только технический metadata?

Пока ответ не близок к однозначному — к LLM incremental не переходить.
