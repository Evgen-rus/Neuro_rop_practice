# ARCHITECTURE

Короткий контекст проекта для старта нового чата с ИИ.
Цель: быстро дать модели рабочую карту проекта без перегруза деталями.

Last updated: 2026-06-23

## 1) System At A Glance

`Neuro_rop_practice` — локальный Python MVP ИИ-помощника РОПа для анализа лидов, сделок, звонков и истории коммуникаций из Bitrix24.

- Основной интерфейс сейчас: CLI-скрипты и локальные файлы в `reports/`.
- CRM-интеграция: Bitrix24 REST webhook, только read-only операции.
- LLM-интеграция: OpenAI Responses API для JSON-анализа.
- Транскрибация: OpenAI audio transcription, локальная подготовка аудио через `ffmpeg`.
- База правил клиента: обработанные Markdown-файлы в `knowledge/clients/praktikm/`.
- Поддерживаемые контуры: `deals` и `leads`.
- Текущий продуктовый статус: полуавтоматический файловый MVP без БД, backend API, frontend и Telegram-отправки.

Главная логика:

```text
Bitrix24 -> raw JSON -> customer_path.md -> workspace -> transcript -> LLM JSON -> ROP markdown report
```

## 2) Source Of Truth

Если есть конфликт между документами и кодом, доверять коду.

- Общая настройка путей, времени и логирования: `setup.py`
- Runtime env для OpenAI: `openai_api/config.py`
- Публичные ссылки на карточки Bitrix: `openai_api/bitrix_links.py`
- Расчёт стоимости OpenAI: `openai_api/pricing.py`
- Bitrix REST client: `bitrix/client.py`
- Структура рабочих папок лидов/сделок: `bitrix/workspace.py`
- Deal pipeline: `bitrix/deals/run_deals_customer_path_pipeline.py`
- Deal raw fetch: `bitrix/deals/1_fetch_deals_context.py`
- Deal markdown history: `bitrix/deals/2_build_deals_customer_path_report.py`
- Deal workspace prepare: `bitrix/deals/3_prepare_deals_workspace.py`
- Lead pipeline: `bitrix/leads/run_leads_customer_path_pipeline.py`
- Lead raw fetch: `bitrix/leads/1_fetch_leads_context.py`
- Lead markdown history: `bitrix/leads/2_build_leads_customer_path_report.py`
- Lead workspace prepare: `bitrix/leads/3_prepare_leads_workspace.py`
- Транскрибация аудио CLI: `openai_api/audio/local_file_transcribe.py`
- Общая логика транскрибации: `openai_api/audio/transcribe_core.py`
- Deal LLM-анализ: `openai_api/llm/analyze_deal.py`
- Lead LLM-анализ: `openai_api/llm/analyze_lead.py`
- OpenAI JSON client: `openai_api/llm/llm_client.py`
- Валидация LLM JSON перед отчётом: `openai_api/llm/validation.py`
- Обработанная OKF-база ПрактикМ: `knowledge/clients/praktikm/index.md`
- Ручная проверка проекта: `Docs/ручная проверка проекта.md`
- Целевая MVP-спека: `Docs/rop_assistant_spec.md`

## 3) Critical Invariants

1. Bitrix-скрипты должны оставаться read-only: только `get/list/fields`-подобные методы и запись локальных файлов.
2. `.env` не коммитить и не переносить секреты в код. В репозитории допустим только шаблон `.env.example`.
3. `reports/` содержит персональные и коммерческие данные: raw JSON, аудио, транскрипты, отчёты. Считать эту папку чувствительной.
4. `knowledge/clients/praktikm/` — обработанная база правил, а не факты конкретной сделки или лида.
5. LLM не должен выдумывать факты: промпты обязаны отделять историю CRM/транскрипт от OKF-правил.
6. Анализ должен возвращать валидный JSON. Markdown-отчёт строится только после успешного JSON-разбора.
7. Для моделей с длинными ответами важен `ANALYSIS_MAX_OUTPUT_TOKENS`: если лимит мал, JSON может оборваться и упасть на `json.loads`.
8. Готовые тексты клиенту не должны содержать плейсхолдеры вроде `ДОБАВИТЬ`, `уточнить`, `{данные}`.
9. Недозвон, автоответчик и служебное сообщение нельзя считать содержательным контактом.
10. Если содержательного контакта нет, анализ должен применять правила дозвона из `call_attempt_rules.md`.
11. После отправки КП анализ должен требовать критерии выбора, срок решения, ЛПР и следующий шаг к договору, счету, предоплате или согласованию комплектации.
12. Workspace-структура должна быть одинаковой по смыслу для `lead_*` и `deal_*`: `history/`, `raw/`, `audio/`, `transcripts/`, `analysis/`, `index.json`.
13. `latest` transcript выбирается по времени изменения файла. Если в папке несколько транскриптов, это может повлиять на анализ.
14. `ffmpeg` и `ffprobe` — внешние зависимости, они не ставятся через `requirements.txt`.
15. Сейчас нет SQLite/deal_memory из спеки. Поле `memory_update` в LLM-ответе пока сохраняется в JSON, но не обновляет отдельное хранилище памяти.
16. Финальный ROP markdown report нельзя сохранять, если LLM JSON не прошёл `openai_api/llm/validation.py`.

## 4) Key Domain Objects

- `Lead` — лид Bitrix24, локально представлен raw JSON, markdown history, workspace и analysis.
- `Deal` — сделка Bitrix24, локально представлена raw JSON, markdown history, audio manifest, workspace и analysis.
- `Activity` — CRM-активность: звонок, письмо, сообщение, задача или другое событие.
- `Timeline comment` — комментарий/событие таймлайна Bitrix.
- `Transcript` — результат транскрибации звонка, обычно хранится в `transcripts/` в `.md`, `.txt`, `.json`.
- `Knowledge file` — обработанное правило ПрактикМ из `knowledge/clients/praktikm/`.
- `Analysis JSON` — структурированный ответ модели в `analysis/*_analysis.json`.
- `ROP report` — человекочитаемый Markdown-отчёт в `analysis/*_rop_report.md`.
- `Workspace index` — `index.json`, фиксирует тип сущности, ID и локальные папки.

## 5) Runtime Flows

### A) Deal Customer Path

Команда:

```powershell
.\venv\Scripts\python.exe .\bitrix\deals\run_deals_customer_path_pipeline.py --deal-ids 18507
```

Поток:

```text
1_fetch_deals_context.py
  -> reports/bitrix_customer_path/raw/deal_18507_context.json
2_build_deals_customer_path_report.py
  -> reports/bitrix_customer_path/markdown/deal_18507_customer_path.md
3_prepare_deals_workspace.py
  -> reports/rop_assistant/deals/deal_18507/
```

Deal-контур также использует audio manifest из `reports/bitrix_customer_path/audio/`, если он уже есть.

### B) Lead Customer Path

Команда:

```powershell
.\venv\Scripts\python.exe .\bitrix\leads\run_leads_customer_path_pipeline.py --lead-ids 227661
```

Поток:

```text
1_fetch_leads_context.py
  -> reports/bitrix_lead_path/raw/lead_227661_context.json
2_build_leads_customer_path_report.py
  -> reports/bitrix_lead_path/markdown/lead_227661_customer_path.md
3_prepare_leads_workspace.py
  -> reports/rop_assistant/leads/lead_227661/
```

Lead-контур может анализироваться без транскрипта: `analyze_lead.py` умеет работать с историей лида и `--transcript none`.

### C) Local Audio Transcription

Команда для сделки:

```powershell
.\venv\Scripts\python.exe .\openai_api\audio\local_file_transcribe.py --deal-id 18507 --audio "C:\path\call.mp3" --activity-id 123 --call-start "2026-06-23T10:00:00+03:00"
```

Команда для лида:

```powershell
.\venv\Scripts\python.exe .\openai_api\audio\local_file_transcribe.py --lead-id 227661 --audio "C:\path\call.mp3" --activity-id 123 --call-start "2026-06-23T10:00:00+03:00"
```

Что происходит:

- аудио при необходимости копируется в workspace;
- `ffmpeg` конвертирует файл в WAV 16 кГц mono;
- длинное аудио режется на сегменты;
- каждый сегмент отправляется в OpenAI transcription;
- итоговый transcript сохраняется в workspace `transcripts/`.

### D) Deal Analysis

Команда:

```powershell
.\venv\Scripts\python.exe .\openai_api\llm\analyze_deal.py --deal-id 18507
```

Входы:

- `reports/rop_assistant/deals/deal_18507/history/deal_18507_customer_path.md`
- latest transcript из `reports/rop_assistant/deals/deal_18507/transcripts/`, если не указан `--transcript none`
- OKF-файлы из `knowledge/clients/praktikm/`

Выходы:

- `deal_18507_request_prompt.txt`
- `deal_18507_analysis.json`
- `deal_18507_rop_report.md`
- `deal_18507_raw_model_output.txt`

### E) Lead Analysis

Команда:

```powershell
.\venv\Scripts\python.exe .\openai_api\llm\analyze_lead.py --lead-id 227661
```

Если транскрипта нет:

```powershell
.\venv\Scripts\python.exe .\openai_api\llm\analyze_lead.py --lead-id 227661 --transcript none
```

Входы:

- `reports/rop_assistant/leads/lead_227661/history/lead_227661_customer_path.md`
- optional transcript из `transcripts/`
- OKF-файлы из `knowledge/clients/praktikm/`

Выходы:

- `lead_227661_request_prompt.txt`
- `lead_227661_analysis.json`
- `lead_227661_rop_report.md`
- `lead_227661_raw_model_output.txt`

### F) Dry Run

Для проверки промпта без траты токенов:

```powershell
.\venv\Scripts\python.exe .\openai_api\llm\analyze_deal.py --deal-id 18507 --dry-run
.\venv\Scripts\python.exe .\openai_api\llm\analyze_lead.py --lead-id 227661 --dry-run
```

Dry-run сохраняет prompt в `analysis/`, но не вызывает OpenAI.

## 6) Where To Change Code By Task Type

- Новый Bitrix-метод или изменение REST-обработки: `bitrix/client.py` и конкретный `1_fetch_*_context.py`.
- Изменение состава raw bundle по сделкам: `bitrix/deals/1_fetch_deals_context.py`.
- Изменение состава raw bundle по лидам: `bitrix/leads/1_fetch_leads_context.py`.
- Изменение Markdown-истории сделки: `bitrix/deals/2_build_deals_customer_path_report.py`.
- Изменение Markdown-истории лида: `bitrix/leads/2_build_leads_customer_path_report.py`.
- Изменение папок, имён файлов и workspace: `bitrix/workspace.py` плюс `3_prepare_*_workspace.py`.
- Изменение транскрибации, chunking, `ffmpeg`: `openai_api/audio/transcribe_core.py`.
- Изменение CLI выбора аудио и сохранения transcript bundle: `openai_api/audio/local_file_transcribe.py`.
- Изменение модели/лимитов/стоимости анализа: `.env`, `.env.example`, `openai_api/config.py`.
- Изменение публичных ссылок на лиды/сделки Bitrix: `BITRIX_PORTAL_URL` и `openai_api/bitrix_links.py`.
- Изменение тарифов моделей и формулы стоимости: `openai_api/pricing.py`.
- Изменение вызова OpenAI Responses API и JSON-парсинга: `openai_api/llm/llm_client.py`.
- Изменение проверки обязательных полей и запрещённых плейсхолдеров в LLM JSON: `openai_api/llm/validation.py`.
- Изменение структуры deal-анализа, правил промпта или Markdown-отчёта: `openai_api/llm/analyze_deal.py`.
- Изменение структуры lead-анализа, правил промпта или Markdown-отчёта: `openai_api/llm/analyze_lead.py`.
- Изменение приоритетного набора OKF-файлов: `knowledge_files()` в `openai_api/llm/analyze_deal.py`.
- Изменение правил клиента ПрактикМ: файлы в `knowledge/clients/praktikm/`.
- Обновление ручной инструкции проверки: `Docs/ручная проверка проекта.md`.
- Изменение целевой архитектуры MVP: `Docs/rop_assistant_spec.md`.

## 7) Env Groups (Quick)

Bitrix:

- `BITRIX_WEBHOOK_URL`
- `BITRIX_PORTAL_URL`

OpenAI:

- `OPENAI_API_KEY`
- `TRANSCRIPTION_MODEL`
- `ANALYSIS_MODEL`
- `ANALYSIS_MAX_OUTPUT_TOKENS`
- `USD_RUB_RATE`

Логирование payload preview:

- `OPENAI_LOG_PREVIEW_LINES`
- `OPENAI_LOG_PREVIEW_CHARS`

Важно:

- для `gpt-5.5` и длинных отчётов может понадобиться увеличить `ANALYSIS_MAX_OUTPUT_TOKENS`;
- цены моделей зашиты в `openai_api/pricing.py`, а курс рубля берётся из `USD_RUB_RATE`.

## 8) Encoding / Text Policy

- Текстовые файлы проекта ожидаются в `UTF-8`.
- Кириллица используется намеренно: документы, отчёты, промпты, OKF-база, логи и сообщения.
- Не заменять русский текст транслитом или Unicode escape без крайней необходимости.
- Если терминал показывает битую кириллицу, не копировать её обратно в исходники без проверки файла.
- Markdown-отчёты и JSON сохранять с `ensure_ascii=False`, чтобы русский текст оставался читаемым.

## 9) Known Pitfalls

1. Не запускать анализ до подготовки workspace: `analyze_*` ожидает файлы в `reports/rop_assistant/...`.
2. Не путать старые и новые точки входа: сейчас основные pipeline-файлы имеют имена `run_deals_customer_path_pipeline.py` и `run_leads_customer_path_pipeline.py`.
3. Не считать `reports/` исходным кодом: это рабочие артефакты и чувствительные данные.
4. Не полагаться на `latest` transcript, если в папке несколько звонков. Для точности передавать `--transcript`.
5. Если модель вернула обрезанный JSON, сначала проверить `ANALYSIS_MAX_OUTPUT_TOKENS`.
6. Если OpenAI вернул JSON с плейсхолдерами в клиентском тексте, это не runtime-ошибка, а ошибка качества: нужна пост-валидация или повторный запрос.
7. `text={"format": {"type": "json_object"}}` помогает получить JSON, но не гарантирует бизнес-валидность полей.
8. При ошибке JSON-парсинга текущий код может не сохранить raw output, потому что сохранение происходит после успешного `call_analysis_json`.
9. Для длинных аудио нельзя повышать параллельность бездумно: `max_segment_concurrency` влияет на нагрузку и возможные rate limits.
10. `ffmpeg` должен быть доступен в `PATH`; иначе транскрибация упадёт до вызова OpenAI.
11. Lead и deal похожи, но JSON-структуры анализа разные. Не копировать поля между ними без проверки prompt/report.
12. `ensure_entity_workspace()` исторически deal-oriented; для лидов лучше использовать `ensure_lead_workspace()`, где явно заданы lead history/raw paths.
13. OKF-файлы являются правилами анализа, а не доказательством фактов. Нельзя писать в отчёте, что клиент что-то сказал, если это есть только в OKF.
14. После отправки КП не писать клиенту “направляю КП”, если КП уже было отправлено. Использовать формулировку “возвращаюсь к направленному КП”.
15. При доработке Telegram, SQLite или FastAPI сначала сверяться с `Docs/rop_assistant_spec.md`: эти части описаны как целевая архитектура, но в runtime пока не реализованы.

## 10) Current Gaps

- Нет SQLite и постоянной `deal_memory` / `lead_memory`.
- Нет Telegram Bot API отправки отчётов.
- Нет FastAPI/backend сервиса.
- Нет frontend или личного кабинета.
- Нет автоматического end-to-end сценария `Bitrix -> audio download -> transcribe -> analyze -> send`.
- Нет Pydantic-схем для строгой валидации LLM JSON.
- Нет тестового набора для raw JSON parsing, markdown rendering и report rendering.
- Автоматическое скачивание аудио из Bitrix ограничено: часть сценариев остаётся ручной.

