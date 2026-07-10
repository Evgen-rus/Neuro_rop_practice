# План безопасной оптимизации LLM-контекста и памяти

Статус: утверждён для поэтапной реализации. Рабочая ветка: `feature/context-memory-optimization`.

## Правило проекта

**Качество важнее экономии.** Снижение токенов, числа вызовов или стоимости не является критерием готовности само по себе. Новая механика принимается только если на эталонных кейсах не ухудшает определение внимания РОПа, главный риск, управленческое действие, evidence и безопасность рекомендации.

## Текущее состояние

- Bitrix24 используется только read-only; raw JSON, полная история и транскрипты сохраняются локально.
- Для лида и сделки есть normalized snapshot, fingerprint, diff, SQLite `entity_state`, `analysis_runs`, `entity_memory` и deterministic mini recommendation.
- `DATE_MODIFY` не запускает LLM сам по себе; direct LLM-запуск защищён `--allow-direct-llm`; `--force-llm` сохраняет ручной full fallback.
- Hard diff запускает прежний full-анализ. Он строит большой JSON, валидирует его бизнес-правилами и затем создаёт Markdown-отчёт.
- `entity_memory` сейчас сохраняет старый `memory_update`, но не имеет version/migration/patch-contract и не добавляется в следующий prompt.
- Полная customer history остаётся аудитным артефактом. Deal при наличии использует compact LLM context; lead пока использует полную history.

## Обнаруженные причины расхода

1. При hard diff снова отправляются полная history, transcript, diagnostics, CRM stage policy и вся OKF-база, а не фактическая дельта.
2. `knowledge_files()` каждый раз собирает все девять Markdown-файлов OKF.
3. Diff знает ID новых событий, но не формирует ограниченный event-delta payload для модели.
4. Legacy JSON-контракт и полный Markdown требуют повторяемые необязательные блоки и увеличивают output tokens.
5. Постоянный префикс запроса не отделён от динамического контекста и ранее не измерялся по блокам.
6. В `pricing.py` цена `gpt-5.4-mini` была равна GPT-5.4; её нужно сверять с официальной таблицей до любых финансовых выводов.

## Целевая архитектура

```text
raw Bitrix context -> normalized snapshot + completeness check
  -> deterministic router
     L0: skip
     L1: local deterministic attention delta
     L2: cheap strict triage
     L3: GPT-5.4 strict attention delta
  -> validate delta + validate entity-memory patch
  -> atomic local persistence
  -> deterministic Markdown + legacy-compatible UI projection
```

- Сущность получает versioned memory с подтверждёнными фактами, `evidence_ids`, текущим риском, вопросами, next step, прошлым ROP action и `last_event_cursor`.
- Event delta состоит из стабильных ID, даты, источника, типа, hash и изменённых CRM-полей. Неполная выгрузка, потерянный курсор или неоднозначная связка обязаны включать полный fallback.
- Deterministic policy router всегда добавляет `core`, а остальные packs выбирает по типу сущности, стадии, причине закрытия, event delta и явным сигналам. Embeddings и vector DB на первом этапе не применяются.
- Новый LLM-результат — компактная attention delta; память меняется только после strict и business validation. Полный legacy-анализ и отчёт сохраняются до прохождения benchmark.
- Feature flags: `CONTEXT_MEMORY_OPTIMIZATION_ENABLED=false`, `CONTEXT_MEMORY_OPTIMIZATION_SHADOW_MODE=false`, `CONTEXT_MEMORY_OPTIMIZATION_FORCE_FULL_FALLBACK=true`.

## Инварианты безопасности

- Bitrix остаётся read-only; `.env`, raw exports, аудио, транскрипты, телефоны, ФИО и отчёты не коммитятся.
- Legacy prompt, JSON, Markdown, decision engine, stage policy и SQLite-данные не удаляются и не ломаются.
- Изменения SQLite — только add/migration, без destructive rewrite; memory обновляется атомарно после успеха.
- Неполный контекст — это ограничение анализа, а не доказательство отсутствия фактов.
- Feature flag по умолчанию сохраняет идентичное текущему поведение; есть ручной full fallback.
- Telemetry содержит размеры, hashes, usage и стоимость, но не полный чувствительный текст.
- Платные API-вызовы выполняются только с отдельным явным разрешением.

## Критерии качества

Для каждого benchmark-кейса вручную проверяются: необходимость внимания РОПа, главный риск, квалификация, конкретность поручения, ожидаемый CRM-факт, evidence, отсутствие выдуманных фактов, отсутствие опасной рекомендации и сохранение важных legacy-деталей. Измеряются input/output/cached tokens, стоимость и время. Критическая регрессия по любому качественному пункту блокирует rollout независимо от экономии.

## Как трактовать `approx_tokens`

`approx_tokens` вычисляется как `ceil(chars / 4)` без новой tokenizer-зависимости. Это ориентир для сравнения состава блоков между запусками, а не расчёт стоимости или лимита модели: русский текст и Markdown могут заметно отклоняться от такой оценки. Стоимость и фактическое число input/output/cached tokens берутся только из `response.usage` после ответа API.

## Известные проблемы, не входящие в этот этап

- На локальном запуске может возникать `PermissionError` при ротации занятого `logs/transcription.log`. Эта существующая проблема не меняется в данной ветке и не блокирует prompt budget/benchmark этап.

## Порядок включения

1. Новый код работает только как telemetry и benchmark-infrastructure.
2. Затем shadow mode создаёт параллельные артефакты, не меняя UI, SQLite state или рабочий отчёт.
3. РОП вручную сравнивает legacy и shadow на фиксированном наборе.
4. После прохождения quality gate включение ограничивается малой выборкой с `FORCE_FULL_FALLBACK=true`.
5. Полный rollout возможен только после повторного сравнения; legacy fallback не удаляется.

## Этапы

| № | Этап | Изменения и новые файлы | Тесты, риск, критерий готовности |
|---:|---|---|---|
| 1 | Prompt budget | `llm_client.py`, analyzers, logging; новый `openai_api/llm/prompt_budget.py` | Unit-тест composition; риск PII в telemetry; готово, когда сохранены только size/hash/usage/cost каждого блока без изменения prompt |
| 2 | Benchmark infrastructure | новые `benchmarks/`, локальный gitignored cases directory | Fixture/runner tests; риск коммита клиентских данных; готово, когда legacy baseline можно оценивать без повторного вызова |
| 3 | Strict schemas | новый `schemas.py`, изменения client/validation | Valid/invalid schema и legacy regression; готово, когда есть отдельные schemas lead/deal delta, triage и memory patch |
| 4 | Attention delta | новые `attention_delta.py`, `report_builder.py` | Contract/render snapshots; риск UI incompatibility; готово, когда delta превращается кодом в совместимый отчёт |
| 5 | Entity memory v2 | `rop_db.py`, migration registry, новый `entity_memory.py` | Migration/atomicity tests; готово, когда память versioned и меняется только после успеха |
| 6 | Event delta/cursor | `snapshot.py`, change-aware wrappers, новый `event_delta.py` | Stable ID/update/remove/fallback tests; готово, когда model input ограничен новыми фактами либо выбран full fallback |
| 7 | Policy packs | policy manifest/router и compact packs | Router matrix tests; готово, когда `core` обязателен, а выбор и причина залогированы |
| 8 | Model router | `model_router.py`, config, pricing, wrappers | Boundary/fallback tests; готово, когда L0-L3 выбираются детерминированно, без автоматического выбора только по цене |
| 9 | Prompt caching | `llm_client.py`, prompt layout, config | Exact-prefix/cache-key tests; готово, когда stable prefix и cache telemetry включены без PII key |
| 10 | Вечерний отчёт | reporting module, append-only run metadata, read API при необходимости | Aggregation/dedup tests; готово, когда РОП видит только новые значимые изменения |
| 11 | Batch API (опционально) | isolated exporter/importer | Dry-run/correlation/retry/benchmark tests; готово только после quality gate, с обычным fallback |

## Что реализовано сейчас

Выполняются только этапы 1–2 в подготовительном объёме, feature flags и корректировка стандартной short-context цены моделей. Не подключаются новая память, event delta, schemas, policy/model routers, prompt caching или новый анализатор.
