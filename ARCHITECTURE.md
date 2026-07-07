# ARCHITECTURE

Короткий контекст проекта для старта нового чата с ИИ.
Цель: быстро дать модели рабочую карту проекта без перегруза деталями.

## How AI Should Use This File

- Используй этот файл как карту проекта перед изменениями кода: сначала сверяй контуры, source-of-truth файлы и critical invariants.
- Если документ конфликтует с кодом, доверяй коду и явно отмечай расхождение в ответе пользователю.
- Не превращай этот файл в runbook, changelog или подробную спецификацию. Подробные команды держать в `Docs/*_runbook.md`, практические результаты — в отдельных notes.
- Обновляй `ARCHITECTURE.md` только когда пользователь прямо просит обновить архитектуру или явно просит зафиксировать новое архитектурное состояние. Не обновляй его автоматически после каждой правки кода.
- При обновлении сохраняй средний размер и убирай явное дублирование: здесь должны быть стабильные правила, карта потоков и ссылки на подробные документы.

Last updated: 2026-07-07

## 1) System At A Glance

`Neuro_rop_practice` — локальный Python MVP ИИ-помощника РОПа для анализа лидов, сделок, звонков и истории коммуникаций из Bitrix24.

- Основной интерфейс сейчас: CLI-скрипты и локальные файлы в `reports/`.
- CRM-интеграция: Bitrix24 REST webhook, только read-only операции.
- LLM-интеграция: OpenAI Responses API для JSON-анализа.
- Транскрибация: OpenAI audio transcription, локальная подготовка аудио через `ffmpeg`.
- База правил клиента: обработанные Markdown-файлы в `knowledge/clients/praktikm/`.
- Поддерживаемые контуры: `deals` и `leads`.
- Текущий продуктовый статус: полуавтоматический файловый MVP с локальной SQLite для change detection, без backend API, frontend и Telegram-отправки.

Главная логика:

```text
Bitrix24 -> raw JSON -> customer_path.md -> workspace -> transcript -> LLM JSON -> ROP markdown report
```

Верхний CLI-путь для пользователя:

```text
run_rop_assistant.py -> lead/deal pipeline -> context diagnostics -> missing audio/transcript actions -> transcribe -> change-aware LLM analysis
```

Расширенный контур истории клиента:

```text
lead/deal -> contact -> related contact deals + deals by LEAD_ID -> duplicate leads by phone/email -> unified customer_history_bundle -> customer_path.md / LLM context
```

Продуктовый сценарий отчёта: `система -> РОП -> менеджер`. Отчёт читает только РОП; менеджер получает от РОПа отдельное поручение или сообщение, а не доступ к отчёту.

## 2) Source Of Truth

Если есть конфликт между документами и кодом, доверять коду.

- Общая настройка путей, времени и логирования: `setup.py`
- Runtime env для OpenAI: `openai_api/config.py`
- Публичные ссылки на карточки Bitrix: `openai_api/bitrix_links.py`
- Расчёт стоимости OpenAI: `openai_api/pricing.py`
- Верхний CLI ROP assistant: `run_rop_assistant.py`
- Краткая Markdown-выгрузка свежих лидов: `export_recent_leads_summary.py`
- Bitrix REST client: `bitrix/client.py`
- Полная история клиента / fallback-связки: `bitrix/customer_history.py`
- Диагностика полноты контекста и ручные действия: `bitrix/context_diagnostics.py`
- Markdown полной истории клиента: `bitrix/customer_history_report.py`
- Структура рабочих папок лидов/сделок: `bitrix/workspace.py`
- Deal pipeline: `bitrix/deals/run_deals_customer_path_pipeline.py`
- Deal raw fetch: `bitrix/deals/1_fetch_deals_context.py`
- Deal markdown history: `bitrix/deals/2_build_deals_customer_path_report.py`
- Deal workspace prepare: `bitrix/deals/3_prepare_deals_workspace.py`
- Deal compact LLM context: `bitrix/deals/4_build_deals_llm_context.py`
- Deal missing-only audio downloader: `bitrix/deals/download_deals_call_audio.py`
- Lead pipeline: `bitrix/leads/run_leads_customer_path_pipeline.py`
- Lead raw fetch: `bitrix/leads/1_fetch_leads_context.py`
- Lead markdown history: `bitrix/leads/2_build_leads_customer_path_report.py`
- Lead workspace prepare: `bitrix/leads/3_prepare_leads_workspace.py`
- Lead missing-only audio downloader: `bitrix/leads/download_leads_call_audio.py`
- Транскрибация аудио CLI: `openai_api/audio/local_file_transcribe.py`
- Общая логика транскрибации: `openai_api/audio/transcribe_core.py`
- Deal LLM-анализ: `openai_api/llm/analyze_deal.py`
- Lead LLM-анализ: `openai_api/llm/analyze_lead.py`
- OpenAI JSON client: `openai_api/llm/llm_client.py`
- Валидация LLM JSON перед отчётом: `openai_api/llm/validation.py`
- SQLite-хранилище состояния ROP assistant: `storage/rop_db.py`
- Deal change detection snapshot/diff: `openai_api/change_detection/snapshot.py`
- Deal/lead decision engine и mini recommendation: `openai_api/change_detection/decision_engine.py`
- Deal CRM stage policy / closed-lost classification: `openai_api/change_detection/stage_policy.py`
- Deal orchestration CLI с пропуском лишнего LLM: `openai_api/llm/analyze_deal_if_changed.py`
- Lead orchestration CLI с пропуском лишнего LLM: `openai_api/llm/analyze_lead_if_changed.py`
- Инструкция ручного запуска change detection: `Docs/change_detection_runbook.md`
- Инструкция запуска полной истории клиента: `Docs/customer_history_runbook.md`
- Практические заметки по проверке customer history: `Docs/customer_history_experiment.md`
- Обработанная OKF-база ПрактикМ: `knowledge/clients/praktikm/index.md`
- Ручная проверка проекта: `Docs/ручная проверка проекта.md`

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
15. SQLite есть только как локальное MVP-хранилище для deal/lead change detection. Полноценной портфельной `deal_memory` / `lead_memory` из спеки пока нет.
16. Финальный ROP markdown report нельзя сохранять, если LLM JSON не прошёл `openai_api/llm/validation.py`.
17. Deal change detection должен считать fingerprint только по normalized snapshot, а не по Markdown и не по полному raw JSON.
18. `DATE_MODIFY` хранится в snapshot metadata, но не является самостоятельной причиной запускать LLM.
19. Прямой запуск `analyze_deal.py` / `analyze_lead.py` без `--allow-direct-llm` заблокирован, чтобы не тратить LLM в обход change detection.
20. Lead change detection использует `STATUS_ID` вместо `STAGE_ID`; задачи и смена ответственного считаются soft diff, новый звонок/email/message/comment/status/transcript — hard diff.
21. Полный `customer_path.md` остается аудитным/UI-артефактом. Для deal LLM prompt используется `history/deal_<id>_llm_context.md`, если он существует; иначе fallback на полный `customer_path.md`.
22. Закрытый провальный CRM-статус сделки определяется кодом до LLM через `stage_policy.py`, а не угадывается моделью по тексту истории.
23. Если сделка закрыта как проваленная, deal-анализ обязан вернуть `closed_deal_review`; клиентский текст в таком случае используется только после решения РОПа вернуть или реанимировать сделку.
24. Главный action-блок нового анализа — `rop_manager_message_block`: что РОПу проверить, почему это важно, какое сообщение отправить менеджеру, какой факт должен появиться в CRM, срок контроля, критерий выполнения и evidence.
25. `manager_action_block` сохранён для обратной совместимости и как черновик клиентского касания для менеджера, но не является главным результатом отчёта.
26. Markdown-отчёт по сделке и лиду должен начинаться с раздела `## Что сделать РОПу сейчас`; клиентские тексты выводятся ниже как вспомогательный блок `## Черновик текста клиенту для менеджера`.
27. Deal-анализ обязан возвращать `money_path_diagnosis`: где застрял путь к деньгам, почему деньги под риском, у кого следующий шаг, какой факт нужен для движения и evidence.
28. Lead-анализ обязан возвращать `loss_diagnosis`: качество лида, качество обработки, сигнал источника, качество дозвона, качество следующего шага, финальный вердикт и evidence.
29. Нельзя уверенно писать `bad_lead`, если не было нормального дозвона, альтернативного канала или конкретного следующего шага; в таком случае использовать `bad_processing`, `data_gap` или другой более точный verdict.
30. Полная история клиента строится read-only: `customer_history_bundle` может читать root lead/deal, контакты, связанные сделки контакта, duplicate leads по телефону/email, активности и timeline comments, но не пишет в Bitrix.
31. `CONTACT_ID` — базовая связка клиента. Если `CONTACT_ID` отсутствует, fallback по телефону/email должен подтверждать совпадение через `crm.contact.get` или `crm.lead.get`; неподтвержденные кандидаты остаются только в diagnostics.
32. Если fallback нашел несколько подтвержденных контактов, автосклейка не применяется: такие совпадения считаются ambiguous и требуют ручной проверки.
33. Если fallback нашел дубль-лид по телефону/email, его история включается в `related_leads`, но контакт не считается найденным, пока нет подтвержденного contact link.
34. Внутренний контекст (`timeline comments`, заметки, служебные комментарии) должен храниться отдельно от клиентских касаний и не должен трактоваться как слова клиента.
35. Открытые линии не являются обязательным источником для ПрактикМ; события openline/chat не должны ломать выгрузку и могут быть проигнорированы с diagnostics.
36. `customer_history_bundle` сохраняет diagnostics: missing contact, fallback candidates/matches, unavailable sources, access denied и предупреждения. Эти diagnostics нужны для ручной проверки прав и связок.
37. Для root lead и duplicate leads связанные сделки нужно искать не только через контакт, но и read-only фильтром `crm.deal.list` по `LEAD_ID`.
38. Если lead имеет `STATUS_ID=CONVERTED` или успешную семантику, а связанная сделка найдена, основной ROP-анализ должен выполняться по сделке; lead-анализ для этого лида пропускается.
39. Если converted lead не имеет найденной связанной сделки, diagnostics должен явно писать ручное действие: открыть лид в Bitrix, найти сделку и проверить REST-доступ/связку по `LEAD_ID`.
40. Если у converted lead найдено несколько сделок, автоматический верхний CLI выбирает самую свежую, но история должна оставаться хронологически понятной через customer history и source lead context.
41. Анализ разрешен при неполном контексте, но diagnostics/LLM prompt/ROP report должны явно указывать ограничения и не делать выводы о содержании отсутствующих звонков.
42. Автоскачивание аудио должно быть missing-only по умолчанию: уже успешно скачанные и существующие локально записи не перекачиваются без явного `--redownload`.
43. Существенный ценовой разрыв не является автоматическим доказательством ошибочного закрытия или окончательной неквалификации. Deal-анализ должен сначала проверить сопоставимость КП: состав решения, обязательные/опциональные узлы, этапность, сервис, запуск, гарантию и то, что именно клиент сравнивает.

## 4) Key Domain Objects

- `Lead` — лид Bitrix24, локально представлен raw JSON, markdown history, workspace и analysis.
- `Deal` — сделка Bitrix24, локально представлена raw JSON, markdown history, audio manifest, workspace и analysis.
- `Customer history bundle` — единый raw JSON `*_customer_history_bundle.json` вокруг root lead/deal: root entity, период, контакты, связанные сделки, дубль-лиды, активности по сущностям, timeline comments, tasks, unified timeline и diagnostics.
- `Related deal` — сделка, найденная через `CONTACT_ID`/`crm.deal.contact.items.get` и `crm.deal.list` по контакту или напрямую через `LEAD_ID`; попадает в общий контекст даже из другой воронки.
- `Related lead` — подтвержденный дубль-лид, найденный fallback-поиском по телефону/email, когда у root lead нет `CONTACT_ID`.
- `Converted lead handoff` — правило верхнего CLI: если лид сконвертирован и сделка найдена, дальнейшая транскрибация/анализ идут по сделке, а не по лиду.
- `Fallback candidate` — телефон/email/company из root lead/deal, сохраненный в diagnostics для ручной проверки; автоматически применяется только после подтверждения.
- `Fallback match` — подтвержденная fallback-связка с contact или duplicate lead через read-only Bitrix методы.
- `Activity` — CRM-активность: звонок, письмо, сообщение, задача или другое событие.
- `Timeline comment` — комментарий/событие таймлайна Bitrix.
- `Client touchpoint` — клиентское касание в customer history: звонок, письмо, сообщение, без смешивания с внутренними комментариями.
- `Internal context` — внутренние комментарии/заметки/таймлайн, используемые как контекст работы менеджера, но не как факт клиентского общения.
- `Tasks and control` — задачи и контрольные активности из CRM activities.
- `System event` — состояние/изменение сущности, например текущий статус связанной сделки или дубль-лида.
- `Transcript` — результат транскрибации звонка, обычно хранится в `transcripts/` в `.md`, `.txt`, `.json`.
- `Knowledge file` — обработанное правило ПрактикМ из `knowledge/clients/praktikm/`.
- `Analysis JSON` — структурированный ответ модели в `analysis/*_analysis.json`.
- `ROP manager message block` — главный action-блок `rop_manager_message_block`: готовое поручение РОПа менеджеру плюс ожидаемый CRM-факт, срок, критерий выполнения и evidence.
- `CRM stage policy` — детерминированный контекст стадии сделки: открыта/закрыта, причина закрытия, тип провала, основание для режима `closed_deal_review`.
- `Deal management blocks` — управленческие поля deal-анализа: `rop_manager_message_block`, `money_path_diagnosis`, `deal_mode`, `closed_deal_review`, `resource_control`, `payment_blocker`, `objection_handling`, `shaker_question`, `competitor_defense_checklist`, `priority_recommendation`.
- `Price comparability check` — блок deal-анализа `price_comparability_check`: проверка, сравниваются ли одинаковые КП/комплектации при существенном ценовом разрыве.
- `Lead loss diagnosis` — блок `loss_diagnosis`, который отделяет качество лида от качества обработки и фиксирует `bad_lead|bad_processing|data_gap|needs_nurture|ready_for_deal|unknown`.
- `Manager action block` — совместимый старый блок `manager_action_block`; сейчас трактуется как черновик клиентского текста и чеклист для менеджера, а не как главный результат отчёта.
- `ROP report` — человекочитаемый Markdown-отчёт в `analysis/*_rop_report.md`.
- `Workspace index` — `index.json`, фиксирует тип сущности, ID и локальные папки.

## 5) Runtime Flows

Этот раздел держит только карту основных потоков. Подробные команды и проверки: `Docs/customer_history_runbook.md` и `Docs/change_detection_runbook.md`.

### A) ROP Assistant Orchestrator

Основной удобный CLI для ручного MVP-запуска:

```powershell
.\venv\Scripts\python.exe .\run_rop_assistant.py --entity lead --ids 228773 --history-days 60 --yes
.\venv\Scripts\python.exe .\run_rop_assistant.py --entity deal --ids 18507 --history-days 60 --yes
```

Для `--entity lead` верхний CLI сначала собирает lead workspace. Если лид сконвертирован и связанная сделка найдена, он печатает handoff `lead -> deal`, пропускает lead-анализ и запускает deal pipeline/транскрибацию/анализ по выбранной сделке. Если сделка не найдена, это должно быть отражено в diagnostics/manual actions.

### B) Lead Intake Overview

Быстрый read-only обзор лидов за период используется для поиска проблемных источников/этапов перед точечным ROP-анализом:

```powershell
.\venv\Scripts\python.exe .\export_recent_leads_summary.py --days 30
```

Выход по умолчанию: `leads_last_30_days_summary.md`. Это обзорный Markdown для triage, не замена `customer_history_bundle` и не вход LLM-анализа.

### C) Build Customer History

Основной read-only путь подготовки контекста:

```text
Bitrix24 -> raw context/customer_history_bundle -> customer_path.md -> workspace -> LLM context
```

Главные команды:

```powershell
.\venv\Scripts\python.exe .\bitrix\deals\run_deals_customer_path_pipeline.py --deal-ids 18507 --history-days 365 --include-related-contact-deals --include-internal-context
.\venv\Scripts\python.exe .\bitrix\leads\run_leads_customer_path_pipeline.py --lead-ids 227661 --history-days 365 --include-related-contact-deals --include-internal-context
```

Без `--include-related-contact-deals` pipeline сохраняет старый режим одиночной карточки. С флагом строится `*_customer_history_bundle.json`, где есть root entity, contact resolution, related deals, deals by `LEAD_ID`, related duplicate leads, activities by entity, unified timeline и diagnostics.

Ключевые файлы в workspace:

- `history/*_customer_path.md` — аудитный человекочитаемый контекст;
- `raw/*_context.json` — старый raw context root-сущности;
- `raw/*_customer_history_bundle.json` — полный raw context клиента;
- `history/deal_<id>_llm_context.md` — compact context для deal LLM, если построен.

### D) Audio And Transcripts

Автоскачивание аудио встроено в lead/deal pipelines и отдельно доступно через missing-only downloader-скрипты. По умолчанию они не перекачивают уже успешно скачанные локальные файлы; для принудительной перекачки используется `--redownload`.

```powershell
.\venv\Scripts\python.exe .\bitrix\deals\download_deals_call_audio.py --deal-ids 18507
.\venv\Scripts\python.exe .\bitrix\leads\download_leads_call_audio.py --lead-ids 227661
```

Ручная транскрибация локального аудио сохраняет transcript bundle в workspace `transcripts/`.

```powershell
.\venv\Scripts\python.exe .\openai_api\audio\local_file_transcribe.py --deal-id 18507 --audio "C:\path\call.mp3" --activity-id 123
```

Для лида используется тот же CLI с `--lead-id`. Внешние зависимости: `ffmpeg` и `ffprobe`.

### E) LLM Analysis

Штатно запускать через change detection, чтобы не тратить LLM без смысловых изменений:

```powershell
.\venv\Scripts\python.exe .\openai_api\llm\analyze_deal_if_changed.py --deal-id 18507
.\venv\Scripts\python.exe .\openai_api\llm\analyze_lead_if_changed.py --lead-id 227661 --transcript none
```

Прямые `analyze_deal.py` / `analyze_lead.py` без `--allow-direct-llm` заблокированы, но `--dry-run` можно использовать для проверки prompt без вызова OpenAI.

Основные LLM входы:

- deal: `history/deal_<id>_llm_context.md`, fallback на `history/deal_<id>_customer_path.md`, transcript, OKF, CRM stage policy;
- lead: `history/lead_<id>_customer_path.md`, optional transcript, OKF;
- если full customer history включен, prompt должен учитывать связанные сделки/дубль-лиды и не трактовать тишину в root-карточке как отсутствие работы.
- для converted lead штатный путь анализа — deal prompt/report, потому что управленческий разбор должен видеть движение после конвертации.

Основные LLM выходы:

- `analysis/*_request_prompt.txt`;
- `analysis/*_analysis.json`;
- `analysis/*_rop_report.md`;
- `analysis/*_raw_model_output.txt`.

## 6) Where To Change Code By Task Type

- Новый Bitrix-метод или изменение REST-обработки: `bitrix/client.py` и конкретный `1_fetch_*_context.py`.
- Изменение верхнего CLI, выбора lead/deal, handoff converted lead в сделку, включения transcribe/analyze: `run_rop_assistant.py`.
- Изменение обзорной выгрузки свежих лидов по этапам/датам/источникам: `export_recent_leads_summary.py`.
- Изменение полной истории клиента, fallback по телефону/email, duplicate leads, поиск сделок по `LEAD_ID`, diagnostics или разделения событий: `bitrix/customer_history.py`.
- Изменение диагностики полноты контекста, `manual_actions.md`, ссылок Bitrix для ручного добора звонков/сделок: `bitrix/context_diagnostics.py`.
- Изменение Markdown полной истории клиента: `bitrix/customer_history_report.py`.
- Изменение CLI полного customer history: `bitrix/deals/run_deals_customer_path_pipeline.py`, `bitrix/leads/run_leads_customer_path_pipeline.py`, `1_fetch_*_context.py`.
- Изменение состава raw bundle по сделкам: `bitrix/deals/1_fetch_deals_context.py`.
- Изменение состава raw bundle по лидам: `bitrix/leads/1_fetch_leads_context.py`.
- Изменение Markdown-истории сделки: `bitrix/deals/2_build_deals_customer_path_report.py`.
- Изменение Markdown-истории лида: `bitrix/leads/2_build_leads_customer_path_report.py`.
- Изменение compact LLM context сделки, включая чтение `customer_history_bundle`: `bitrix/deals/4_build_deals_llm_context.py`.
- Изменение скачивания аудио Bitrix и manifest/missing-only логики: `bitrix/deals/download_deals_call_audio.py`, `bitrix/leads/download_leads_call_audio.py`.
- Изменение папок, имён файлов и workspace: `bitrix/workspace.py` плюс `3_prepare_*_workspace.py`.
- Изменение транскрибации, chunking, `ffmpeg`: `openai_api/audio/transcribe_core.py`.
- Изменение CLI выбора аудио и сохранения transcript bundle: `openai_api/audio/local_file_transcribe.py`.
- Изменение модели/лимитов/стоимости анализа: `.env`, `.env.example`, `openai_api/config.py`.
- Изменение публичных ссылок на лиды/сделки Bitrix: `BITRIX_PORTAL_URL` и `openai_api/bitrix_links.py`.
- Изменение тарифов моделей и формулы стоимости: `openai_api/pricing.py`.
- Изменение вызова OpenAI Responses API и JSON-парсинга: `openai_api/llm/llm_client.py`.
- Изменение проверки обязательных полей и запрещённых плейсхолдеров в LLM JSON: `openai_api/llm/validation.py`.
- Изменение структуры deal-анализа, правил промпта или Markdown-отчёта: `openai_api/llm/analyze_deal.py`.
- Изменение классификации закрытых/провальных стадий сделки: `openai_api/change_detection/stage_policy.py`.
- Изменение структуры lead-анализа, правил промпта или Markdown-отчёта: `openai_api/llm/analyze_lead.py`.
- Изменение mini recommendation, повторного использования последнего поручения РОПа менеджеру или сохранённого клиентского текста: `openai_api/change_detection/decision_engine.py`.
- Изменение приоритетного набора OKF-файлов: `knowledge_files()` в `openai_api/llm/analyze_deal.py`.
- Изменение правил клиента ПрактикМ: файлы в `knowledge/clients/praktikm/`.
- Обновление ручной инструкции проверки: `Docs/ручная проверка проекта.md`.
- Обновление инструкции по полной истории клиента: `Docs/customer_history_runbook.md`.

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

ROP assistant state:

- `ROP_DB_PATH`

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

1. Перед анализом всегда готовить workspace через lead/deal pipeline; `analyze_*` ожидает файлы в `reports/rop_assistant/...`.
2. `reports/` содержит чувствительные рабочие артефакты, а не исходный код.
3. `latest` transcript выбирается по mtime; если звонков несколько, передавать конкретный `--transcript` или использовать `--transcript all`.
4. Deal и lead похожи по pipeline, но имеют разные JSON contracts, prompts и renderer requirements.
5. OKF-файлы — правила оценки, а не evidence фактов сделки/лида.
6. `customer_history_bundle.related_leads` без контакта означает найденную продолженную историю по телефону/email, но не найденный контакт.
7. `fallback_match_used=false` при `fallback_related_leads_used=true` — нормальная ситуация: дубль-лид найден, contact link отсутствует.
8. `crm.timeline.comment.list` по contact может вернуть `Access denied`, даже если timeline по сделкам доступен; это права webhook/user, не ошибка renderer.
9. `crm_pipeline_map.json` — локальная выгрузка CRM-карты; продуктовую классификацию закрытых стадий менять в `stage_policy.py`.
10. Если JSON модели обрезан, первым делом проверять `ANALYSIS_MAX_OUTPUT_TOKENS`; JSON mode не заменяет бизнес-валидацию.
11. `ffmpeg`/`ffprobe` должны быть в `PATH`; длинные аудио не ускорять параллельностью без учета rate limits.
12. У converted lead без `CONTACT_ID` сделка может быть найдена только через `LEAD_ID`; не считать отсутствие contact link доказательством отсутствия сделки.
13. По сделке, созданной из лида, звонки могут физически лежать в source lead; deal context/diagnostics должны сохранять source entity и Bitrix-ссылку на исходную активность.

## 10) Current Gaps

- Нет Telegram Bot API, backend/FastAPI, frontend и кабинета.
- Нет автоматической отправки отчётов. Локальный CLI уже может собрать контекст, скачать доступное аудио, транскрибировать и запустить анализ, но отправка РОПу/менеджеру не реализована.
- SQLite change detection есть, но полноценная портфельная `deal_memory` / `lead_memory` пока не реализована.
- Нет Pydantic-схем и нормального тестового набора для raw parsing, markdown rendering и report rendering.
- Автоматическое скачивание аудио и contact timeline зависят от прав Bitrix webhook/user; часть сценариев остается ручной.
- Комментарии к задачам не выгружаются отдельным API-методом.
- Company fallback пока только diagnostic candidate, без автосклейки.
