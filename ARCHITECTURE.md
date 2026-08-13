# Архитектура Neuro ROP Assistant

## Назначение и статус

ROP Assistant помогает руководителю продаж разбирать лиды и сделки Bitrix24: собирает доступный CRM-контекст, при необходимости локально транскрибирует записи, строит валидированный LLM-анализ и показывает его в UI. Пилот поддерживает пользователей `admin`/`rop`/`manager`, серверные SQLite-сессии и ограничение сделок по роли; временная VPS-публикация дополнительно остаётся за Nginx Basic Auth.

Документ — рабочая карта для агента, а не runbook и не API-справочник. Перед изменением прочитай разделы **Source of Truth**, **Critical Invariants** и соответствующую строку в **Where to change code**. Если документ расходится с кодом или конфигурацией, верен код; исправь карту только при изменении архитектурного факта.

## Source of Truth

| Область | Источник истины |
| --- | --- |
| Границы проекта и локальные пути | `setup.py`, `.env.example`, `.gitignore` |
| Московское бизнес-время и его отображение | `setup.py` (`MSK_TZ`), `frontend/src/dateTime.ts` |
| HTTP-входы и сборка ответов | `api/app.py` |
| Фоновые задания и запуск CLI | `api/jobs.py`; для compact — `api/compact_shadow.py` |
| Кандидаты, профили и daily summary | `api/candidates.py`, `storage/rop_db.py` |
| Локальное состояние SQLite | `storage/rop_db.py` |
| Пользователи, сессии и HTTP-авторизация | `storage/rop_db.py`, `api/auth.py`, `api/access.py`, `api/app.py` |
| Bitrix REST, privacy-safe usage trace и customer history | `bitrix/client.py`, `bitrix/usage_trace.py`, `bitrix/customer_history.py` |
| Подготовка lead/deal workspaces | `bitrix/leads/*`, `bitrix/deals/*`, `bitrix/workspace.py`, `bitrix/context_diagnostics.py` |
| Дополнительный CRM-контекст полного анализа сделки | `bitrix/deals/1_fetch_deals_context.py`, `bitrix/deals/4_build_deals_llm_context.py`, `openai_api/llm/analyze_deal.py` |
| Транскрибация | `openai_api/audio/*` |
| Полный LLM-анализ и его рендеринг | `openai_api/llm/analyze_lead.py`, `openai_api/llm/analyze_deal.py` |
| LLM-вызов, JSON-парсинг, validation и стоимость | `openai_api/llm/llm_client.py`, `openai_api/llm/validation.py`, `openai_api/pricing.py` |
| Change detection | `openai_api/change_detection/*`, `openai_api/llm/analyze_*_if_changed.py` |
| Контроль сделки, дневной чек-лист менеджера, исходы задач, дневные коммуникации, Quick Help и полный скрипт разговора | `api/deal_control.py`, `api/deal_task_guidance.py`, `api/deal_manager_quick_help.py`, `api/deal_manager_full_script.py`, `openai_api/llm/deal_task_guidance.py`, `openai_api/llm/deal_manager_*.py`, `storage/rop_db.py` |
| Семантика стадий | `openai_api/change_detection/stage_policy.py` |
| Отображаемые Bitrix воронки и названия стадий | локальный `crm_pipeline_map.json` через `api/candidates.py` |
| Browser UI и клиентские контракты | `frontend/src/App.tsx`, `frontend/src/DealControl.tsx`, `frontend/src/api.ts`, `frontend/src/dealPush.ts` |
| Compact attention-delta shadow | `api/compact_shadow.py`, `openai_api/llm/attention_delta*.py`, `benchmarks/*` |

`README.md` описывает только быстрый запуск. Операционные детали принадлежат runbook-файлам и не должны дублироваться здесь.

## Critical Invariants

- Код и конфигурация важнее документации. Не придумывай отсутствующие интеграции, таблицы, гарантии или тесты.
- Bitrix-контур только читает CRM. `BitrixReadOnlyClient` допускает HTTP POST как транспорт REST-вызова, но не должен получать CRM write-методы.
- `.env`, webhook, API-ключи, персональные CRM-данные, аудио, транскрипты и содержимое `reports/` — чувствительные локальные данные. Их нельзя печатать, коммитить или публиковать.
- HTTP-идентичность берётся только из серверной сессии; `role`, `manager_id` и `source_role` из тела или query-параметров не являются полномочиями. Токен сессии хранится только в виде digest, cookie — `HttpOnly`, `Secure`, `SameSite=Lax`; чужая строка менеджера остаётся облегчённой и не открывается.
- Все сохраняемые тексты — UTF-8; JSON с кириллицей сохраняется с `ensure_ascii=False`. ASCII-safe допустим только для строки transport-progress до её разбора.
- Lead и deal — разные контракты: у них отдельные context builders, prompts, validators и renderers. Общая механика не разрешает смешивать поля или переиспользовать renderer одного контура в другом.
- Все бизнес-даты и сроки рассчитываются и отображаются в `Europe/Moscow`; локальная временная зона браузера или машины не должна менять день, срок или сортировку.
- Lead с подтверждённой конверсией переводится в deal-flow. Отсутствующий `CONTACT_ID` не доказывает отсутствие связанной сделки.
- CRM-запись о звонке, `COMPLETED=Y` или внутренний комментарий сами по себе не доказывают содержательный контакт с клиентом. Для лида это требует подходящего transcript/contact evidence; попытки, подтверждённый контакт и внутреннюю информацию хранить раздельно.
- В deal-control исполнение поручения, подтверждённый контакт, целевой результат и движение сделки — отдельные состояния. Найденная или закрытая CRM-активность подтверждает не больше исполнения/попытки, пока клиентский результат не зафиксирован отдельно.
- Дневная проекция коммуникаций deal-control строится при read-only синхронизации только из завершённых CRM-активностей звонка, email или сообщения за текущий московский день и сохраняется в SQLite вместе с датой среза. Она показывает выполнение плана касаний (текущая цель — 3), но не превращает звонок или исходящее сообщение в подтверждённый ответ клиента; при неуспешном чтении Bitrix UI обязан показывать недоступность данных, а не нулевой подтверждённый результат.
- Открытые задачи Bitrix (`CRM_TASKS_TASK`) проецируются в deal-control отдельно от локальных поручений РОПа. При первом и последующих обновлениях они доступны для контроля без CRM-записи и без автоматического объявления поручением РОПа; закрытые задачи и прочие CRM-активности не становятся текущей задачей.
- Чек-лист менеджера является дневным по московской дате и хранится отдельно от задач Bitrix, рекомендаций и дневных коммуникаций. В течение дня полный deal-анализ получает текущие устойчивые ID и состояния пунктов и применяет только валидированную дельту, поэтому отметки менеджера не сбрасываются; конфликт revision не позволяет устаревшему анализу снять или возобновить изменённый менеджером пункт. На новый день выполненные пункты остаются в append-only истории, но не показываются, а открытые переносятся как невыполненные. Отметка менеджера является самоотчётом о действии, не клиентским evidence и не подтверждением результата AI-рекомендации; РОП видит то же дневное состояние read-only.
- История стадий отражает движение CRM, а задачи Bitrix, их описания и чаты — внутренний рабочий контекст. Они не доказывают слова клиента, содержательный контакт или согласование условий без отдельного клиентского evidence.
- Дополнительные deal-источники Bitrix являются необязательными: недоступная история стадий, отсутствующие пользовательские поля, задачи или чаты не должны останавливать подготовку и анализ сделки.
- Исход deal-control должен содержать достаточно данных для своего состояния: попытка без ответа и незавершённый результат требуют следующего шага со сроком, подтверждённый контакт — описания ответа, отказ — причины. Перенос срока РОПом требует причины; роль автора сохраняется в истории. Отменённые задачи учитываются отдельно и не входят в знаменатель метрик исходов или сравнение AI/no-AI.
- Полный Markdown-отчёт создаётся только после успешной бизнес-валидации JSON. OKF/knowledge задают правила оценки, но не являются фактами конкретной сущности.
- Обычный запуск полного анализа проходит через `analyze_lead_if_changed.py` или `analyze_deal_if_changed.py`. Прямой `analyze_*` требует явного `--allow-direct-llm`.
- У LLM есть transport retries и не более одного corrective semantic retry после ошибки JSON/валидации. Не добавляй бесконечные или скрытые платные повторы.
- AI-подсказка к задаче РОПа запускается только явно, привязывается к ревизии задачи и последнему полному deal-анализу; устаревшую подсказку нельзя показывать менеджеру как актуальную.
- Quick Help / «Дожим сделки» не запускает повторный полный анализ: он использует подтверждённую manager situation, ограниченную проекцию последнего отчёта и отдельный knowledge playbook. Для актуальной пары `source_report_id` + situation review хранится отдельный ответ режимов `push` и `reanimator`; повторное открытие и переключение вкладки не создают новый LLM-вызов. Уточнение в чате пересобирает оба режима одним ходом и пишет их в общую историю (`turn_id`). Продолжение переписки, сценарий звонка и email создаются отдельными явными LLM-вызовами и переиспользуются независимо только при совпадении `source_report_id`, situation review, Quick Help и выбранной стратегии. Идеи фоллоуапов также запускаются явно, привязаны к текущим `source_report_id` и situation review и не создают сами материалы; дневной checklist и `objection_handling` остаются существующими источниками истины.
- Compact attention-delta — изолированный shadow/review. Ошибка, устаревший snapshot или неуспешное evidence coverage означают `full_fallback_recommended`, а не замену legacy report.

## Основные контуры

### 1. UI, API и CLI

`frontend/src/App.tsx` — корневой локальный интерфейс; `frontend/src/DealControl.tsx` — три связанных представления контроля сделок (дашборд, экран РОПа и задачи менеджера); `frontend/src/api.ts` — их HTTP-контракт. Представления используют один набор deal-control данных, общий дневной чек-лист, read-only проекцию открытых задач Bitrix, дневную проекцию коммуникаций и локальные исходы, а не отдельные хранилища. Менеджер изменяет отметки чек-листа, РОП видит то же состояние read-only; дневные коммуникации остаются отдельным индикатором активности. При наличии готового анализа дашборд может по запросу пользователя раскрыть сохранённый Markdown-отчёт через существующий report API; этот просмотр не создаёт новый LLM-вызов и не добавляет Markdown обратно во вход анализа. `api/app.py` валидирует запросы, читает/сохраняет локальное состояние и делегирует доменную работу специализированным модулям.

`api/jobs.py` не дублирует Bitrix, transcription или LLM-логику: он запускает `run_rop_assistant.py`, читает его progress events и материализует готовые результаты в `ui_reports`. Состояние активных jobs находится в памяти процесса. SQLite хранит снимки и результаты daily-summary, но перезапуск API не возобновляет subprocess автоматически.

`run_rop_assistant.py` — общий интерактивный/CLI orchestration layer. Он вызывает lead/deal preparation pipeline, затем при выбранных опциях транскрибацию пропущенных аудио и change-aware анализ. UI использует тот же путь через API jobs, а не отдельную бизнес-реализацию.

### 2. Получение CRM-контекста

`bitrix/client.py` централизует REST-вызовы, пагинацию и transient retry. `bitrix/customer_history.py` строит customer-history bundle для корневой сущности и связанных CRM-сущностей, включая нормализованные коммуникации и отдельно внутренний контекст.

Lead и deal preparation scripts получают raw context, подготавливают workspace, диагностику полноты и LLM context. Первый CRM snapshot получает активности полностью; следующие запуски запрашивают изменения по `LAST_UPDATED` с пятиминутным перекрытием и сливают их по ID. Автоматической периодической полной сверки нет: full повторяется только при отсутствующем или непригодном snapshot. `crm.activity.list` явно получает `FILES` и `COMMUNICATIONS`, а legacy `activity_details` строится локально без отдельных `crm.activity.get`; уже полученный root context переиспользуется в customer-history. Общий audio downloader получает записи только из `FILES` CRM-активности через `disk.file.get`: старые активности с доступным файлом обрабатываются при первом импорте, а пустой `FILES` проверяется по обновлённому CRM-контексту не дольше пяти суток от звонка. Для сделки `1_fetch_deals_context.py` дополнительно и только на чтение получает историю стадий, детали связанных CRM-задач и чаты выбранных открытых задач. В raw bundle сохраняются до трёх ближайших открытых задач; вложения и ссылки на файлы из их чатов отбрасываются. Любая ошибка отдельного дополнительного источника обрабатывается fail-soft. `bitrix/workspace.py` задаёт layout workspace. Локальные выгрузки, manifest, аудио и diagnostics остаются под `reports/`.

### 3. Аудио и полный анализ

`openai_api/audio/*` работает с уже найденными локальными файлами; короткие звонки/недозвоны исключаются до транскрибации, когда это можно установить. Transcript context включается в соответствующий lead/deal workspace.

После успешной транскрибации `run_rop_assistant.py` повторно подготавливает workspace, чтобы скопировать актуальный audio manifest, затем обновляет diagnostics; для сделки также пересобирается компактный LLM context. Анализ не должен продолжаться по устаревшей workspace-копии manifest или контекста.

`analyze_lead.py` и `analyze_deal.py` формируют разные prompts, вызывают общий Responses API wrapper, нормализуют и валидируют JSON, затем записывают JSON, raw output и Markdown в workspace. Deal context builder компактно добавляет движение по стадиям, до трёх ближайших открытых задач, не более двух содержательных сообщений из чата каждой выбранной задачи и доступные поля модели оборудования/срока изготовления. Пустые разделы не создаются; внутренние CRM-факты явно отделяются от клиентского evidence. `llm_client.py` считает usage; `pricing.py` формирует локальную оценку стоимости. Вызовы OpenAI и транскрибация требуют ключа и могут создавать стоимость.

### 4. Change detection

`snapshot.py` извлекает стабильный, компактный снимок CRM-фактов; длинные тексты в нём хэшируются. `decision_engine.py` выбирает первый полный анализ, полный анализ при значимом изменении, локальную mini-рекомендацию при детерминированном риске без изменения или пропуск без изменений. Не подменяй эту логику одной лишь `DATE_MODIFY` и не обходи её прямым LLM-вызовом.

`stage_policy.py` определяет семантику стадий для решения. `crm_pipeline_map.json` — только локальная карта реальных Bitrix IDs и имён для UI/фильтров; изменения в ней не меняют бизнес-семантику closed stages.

### 5. SQLite, кандидаты и daily summary

`storage/rop_db.py` — единственный слой доступа к SQLite `reports/rop_assistant/rop_assistant.sqlite`. Он хранит change state, запуски и отчёты, решения и workflow лида, настройки/профили UI, candidate lifecycle, daily-summary, deal-control baselines/outcomes/reschedules/events, read-only срез дневных коммуникаций и compact shadow runs/feedback. Срез коммуникаций хранится в `deal_control_deals.communications_today_json`, обновляется вместе с Bitrix-sync и считается актуальным только для записанной московской даты. Исходы и переносы deal-control сохраняют роль автора; отменённые задачи выводятся отдельной метрикой. Миграции выполняются idempotently в `init_db()`; не добавляй обращения к таблицам мимо этого модуля.

`api/candidates.py` читает Bitrix и локальное состояние, ранжирует кандидатов и строит preview профиля без LLM. `daily_summary_runs` сохраняет snapshot профиля и scope; оплачиваемая обработка начинается только после явного подтверждения пользователя. Journey/candidate lifecycle учитывает переход лида в сделку, но решение по одной сущности не должно скрывать остальные кандидаты воронки.

### 6. Compact attention-delta

Compact run доступен только для уже сохранённых full-analysis inputs. Он строит отдельный строгий schema/prompt, валидирует evidence IDs против ровно тех источников, что были в prompt, и сохраняет результат отдельно от legacy analysis. Запуск — явный и платный; автоматических batch/retry нет. `benchmarks/` служит для isolated replay/сравнения, а не для production-пайплайна; локальные cases и results игнорируются Git.

## Границы lead и deal

| Вопрос | Lead | Deal |
| --- | --- | --- |
| Workspace и pipeline | `bitrix/leads/*` | `bitrix/deals/*` |
| Полный анализ | `analyze_lead.py` + lead validator | `analyze_deal.py` + deal validator |
| Специальное состояние UI | `lead_workflow_state`, qualification и manager review | общие reports/decisions/outcomes и deal-specific analysis |
| Смена сущности | конвертированный лид передаётся сделке | может включать source lead context |
| Компактный сценарий | lead playbooks и contact-aware rules | deal playbooks и deal review rules |

Не переносить lead workflow, BANT-контракт, manager/client текст или lead playbook в deal-контур без отдельного решения. Аналогично не переносить deal qualification/closed-deal правила в lead.

## Where to change code

| Задача | Первое место для проверки | Затронуть также, если меняется контракт |
| --- | --- | --- |
| Bitrix REST, pagination, retry | `bitrix/client.py` | callers и tests внешнего API |
| Customer history, связанная сущность, контакт/внутренний контекст | `bitrix/customer_history.py` | конкретный lead/deal builder, diagnostics и UI metadata |
| Workspace, raw context или audio manifest | соответствующий `bitrix/leads/*` или `bitrix/deals/*`, `bitrix/workspace.py` | `run_rop_assistant.py` только при изменении orchestration |
| История стадий, открытые задачи, чаты задач или технические поля в полном deal-анализе | `bitrix/deals/1_fetch_deals_context.py`, `bitrix/deals/4_build_deals_llm_context.py` | `openai_api/llm/analyze_deal.py` и deal tests |
| Транскрибация и short-call policy | `openai_api/audio/*` | diagnostics и tests транскриптов |
| Prompt, JSON contract, validation или renderer | нужный `analyze_lead.py` либо `analyze_deal.py`, `validation.py` | второй контур проверить на несовместимость, но не менять автоматически |
| Стоимость, Responses API, retries | `llm_client.py`, `pricing.py`, `reliability/retry.py` | progress events и tests retry/validation |
| Change detection или семантика стадий | `openai_api/change_detection/*` | `analyze_*_if_changed.py`, state storage и tests |
| Ранжирование кандидатов, профили, daily summary | `api/candidates.py`, `api/app.py`, `storage/rop_db.py` | `frontend/src/api.ts`, `App.tsx` при изменении API |
| Пользователи, сессии, роли и доступ к сущностям | `api/auth.py`, `api/access.py`, `storage/rop_db.py` | `api/app.py`, `frontend/src/api.ts`, `App.tsx`, `DealControl.tsx`, auth tests |
| Ручной анализ, job status или report projection | `api/jobs.py`, `api/app.py` | `frontend/src/api.ts`, `App.tsx`, `DealControl.tsx` |
| Чек-лист дожима, задача контроля сделки, её baseline/исходы/CRM-факты, дневные коммуникации, Quick Help / «Дожим сделки» и полный скрипт | `api/deal_control.py`, `api/deal_task_guidance.py`, `api/deal_manager_quick_help.py`, `api/deal_manager_full_script.py`, `storage/rop_db.py` | `openai_api/llm/deal_task_guidance.py`, `openai_api/llm/deal_manager_*.py`, `api/app.py`, `frontend/src/api.ts`, `frontend/src/DealControl.tsx`, `frontend/src/dealPush.ts` |
| Lead workflow, manager review или qualification feedback | `api/app.py`, `storage/rop_db.py`, lead analysis contract | UI и regression tests workflow |
| Compact UI/run/feedback | `api/compact_shadow.py`, `openai_api/llm/attention_delta*.py` | `storage/rop_db.py`, UI API types и evidence tests |
| Frontend-only поведение | `frontend/src/App.tsx`, `frontend/src/DealControl.tsx`, `frontend/src/api.ts`, `frontend/src/dealPush.ts` | FastAPI только если HTTP-contract меняется |
| Московское форматирование дат в UI | `frontend/src/dateTime.ts` | компоненты должны использовать общий helper, а не локальный `Date` formatter |

## Интеграционные границы и данные

- Bitrix webhook и `OPENAI_API_KEY` читаются из окружения. Не выводи их значение и не помещай в тестовые фикстуры.
- Каждая физическая попытка Bitrix REST фиксируется одной privacy-safe JSONL-строкой в `logs/bitrix_usage_daily/YYYY-MM-DD.jsonl`; trace содержит только метод, форму запроса без значений, длительность и технический результат, но не URL/webhook, CRM ID, payload, тексты ошибок или содержимое ответа.
- `reports/` содержит локальные CRM exports, аудио, transcripts, analysis, Markdown и SQLite; это runtime data, не исходный код.
- Тексты задач и выбранных сообщений их чатов могут входить в локальный deal context; вложения задач/чатов не скачиваются и не передаются в полный анализ.
- `crm_pipeline_map.json` также является локальной CRM-выгрузкой и не должен пополняться персональными данными вручную.
- `knowledge/clients/*` может участвовать в prompt; knowledge определяет правила, а источники CRM/transcript — факты.
- Для аудио нужны `ffmpeg`/`ffprobe` в `PATH`; их отсутствие — ограничение среды, а не повод угадать длительность звонка.

## Known gaps and pitfalls

- Для пилота есть role-based auth и manager scope, но нет frontend-админки пользователей: учётные записи управляются через `scripts/manage_user.py`.
- Неполные или недоступные Bitrix источники фиксируются в diagnostics. `Access denied` на конкретном REST-методе обычно отражает права webhook/user, а не renderer failure.
- `latest` transcript выбирается по времени файла. При нескольких записях предпочитай явно заданный режим/список transcript, если задача требует определённого звонка.
- `not_confirmed`, `unknown` и `negative` — разные состояния. Не превращай недостаток evidence в отказ клиента.
- Прогресс job — наблюдение за subprocess, а не оценка процента времени. После рестарта API незавершённые daily items требуют явного повторного запуска.
- Compact evidence coverage и fallback не являются доказательством готовности заменить legacy flow. Такое решение требует отдельной валидации и продуктового решения.
- В проекте нет отдельной Pydantic-схемы для полного model output: его контракты реализованы в Python validation. Не объявляй строгую API-схему существующей, пока она не добавлена в код.
