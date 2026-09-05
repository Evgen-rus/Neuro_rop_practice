# Агент: Neuro ROP Assistant

CLI + FastAPI + React для анализа лидов/сделок Bitrix24; CRM только на чтение.

## Контекст и карта

- `api/` — HTTP/jobs; `bitrix/` — CRM; `openai_api/` — audio/анализ/change detection; `storage/` — SQLite; `frontend/` — UI; `tests/` — проверки.
- Начни с `git status --short`. Чужие изменения не трогай.
- Загружай: этот файл → [карта](ARCHITECTURE.md) (Source of Truth, Critical Invariants, нужная строка Where to change code) → релевантный документ → указанные исходники, callers и тесты. Читай только нужное.
- Код и конфигурация — источники истины; расхождения документации исправляй по фактам. [Рабочий порядок](Docs/agent_workflow.md) содержит маршруты документов, handoff и проверки.

## Критические ограничения

- Никаких write-методов Bitrix. Bitrix/OpenAI/production запускай только по необходимости задачи; платные действия и деплой требуют явного разрешения.
- Не печатай/коммить секреты, webhook URL, CRM-данные, аудио, транскрипты, содержимое `reports/` и приватные логи.
- Текст — UTF-8, кириллица без транслита/Unicode-escape; JSON — `ensure_ascii=False`. Бизнес-время — `Europe/Moscow`.
- Не обходи change detection без явно запрошенного `--allow-direct-llm`; Markdown анализа — только после validation JSON. Knowledge/OKF не являются фактами клиента.
- Lead/deal-контракты раздельны. Попытка связи, подтверждённый контакт и внутренняя CRM-информация — разные факты. Авторизация — только серверная сессия.

## Orchestration

- Малое изменение делай сам. Делегируй только когда экономия превышает передачу контекста и интеграцию; архитектура и приёмка остаются у lead.
- При выборе модели/агентов прочитай [model routing и лимиты](Docs/agent_workflow.md#model-routing). Worker получает цель, область, ограничения, результат и проверки; один владелец записи на файл.
- Минимальное изменение в существующем слое; HTTP/SQLite правила и handoff — в рабочем порядке. Не дублируй бизнес-логику.

## Команды и завершение

- Запуск: [README](README.md); API запускает автоматический CRM/LLM-цикл — для локального smoke отключи `DAYTIME_CYCLE_ENABLED`.
- Сначала targeted checks; после Python-логики один полный `./venv/Scripts/python.exe -m unittest discover -s tests` на интегрированном результате.
- После frontend: в `frontend/` — `npm run lint`, `npm run build`.
- Всегда `git diff --check`, проверка scope, diff и контракта. Не повторяй успешные проверки без новой причины. Укажи пропуски и причины; непроверенный результат не называй завершённым.
