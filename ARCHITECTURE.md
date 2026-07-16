# ARCHITECTURE

Короткий контекст проекта для старта нового чата с ИИ.
Цель: быстро дать модели рабочую карту проекта без перегруза деталями.

## How AI Should Use This File

- Используй этот файл как карту проекта перед изменениями кода: сначала сверяй контуры, source-of-truth файлы и critical invariants.
- Если документ конфликтует с кодом, доверяй коду и явно отмечай расхождение в ответе пользователю.
- Не превращай этот файл в runbook, changelog или подробную спецификацию. Подробные команды держать в `Docs/*_runbook.md`, практические результаты — в отдельных notes. Краткий запуск UI/API — в корневом `README.md`.
- Обновляй `ARCHITECTURE.md` только когда пользователь прямо просит обновить архитектуру или явно просит зафиксировать новое архитектурное состояние. Не обновляй его автоматически после каждой правки кода.
- При обновлении сохраняй средний размер и убирай явное дублирование: здесь должны быть стабильные правила, карта потоков и ссылки на подробные документы.

Last updated: 2026-07-13

## 1) System At A Glance

`Neuro_rop_practice` — локальный Python MVP ИИ-помощника РОПа для анализа лидов, сделок, звонков и истории коммуникаций из Bitrix24.

- Интерфейсы: CLI (`run_rop_assistant.py`) и локальный UI (`frontend/` React/Vite) через FastAPI-адаптер (`api/`).
- CRM-интеграция: Bitrix24 REST webhook, только read-only операции.
- LLM-интеграция: OpenAI Responses API для JSON-анализа.
- Транскрибация: OpenAI audio transcription, локальная подготовка аудио через `ffmpeg`/`ffprobe`.
- База правил клиента: обработанные Markdown-файлы в `knowledge/clients/praktikm/`.
- Поддерживаемые контуры: `deals` и `leads`.
- Текущий продуктовый статус: локальный MVP с CLI + UI/API-адаптером, SQLite для change detection, feedback РОПа и ручных compact shadow runs; без Telegram-отправки и без записи в Bitrix.

Главная логика:

```text
Bitrix24 -> raw JSON -> customer_path.md -> workspace -> transcript -> LLM JSON -> ROP markdown report
```

Верхний CLI-путь:

```text
run_rop_assistant.py -> lead/deal pipeline -> context diagnostics -> missing audio/transcript actions -> transcribe -> change-aware LLM analysis
```

Локальный UI-путь:

```text
React UI -> FastAPI api/ -> candidates scoring | analyze job -> run_rop_assistant.py / analysis JSON -> UI report + SQLite feedback
```

Ручная ежедневная сводка использует отдельный pre-LLM этап отбора:

```text
именованный analysis profile -> Moscow calendar period -> Bitrix lead/deal preview без LLM
-> lead-to-deal journey deduplication -> signals + lifecycle + workset/capacity
-> immutable daily summary snapshot -> явное подтверждение платных карточек
-> analyze jobs с force_llm=false -> analysis JSON / ROP report
```

Отдельный ручной compact-контур не участвует в штатном pipeline:

```text
сохранённый полный analysis JSON -> recorded input files -> attention_delta shadow -> evidence coverage -> SQLite run/feedback -> UI review
```

Он не перечитывает CRM, не запускает legacy pipeline и не заменяет полный analysis JSON или ROP markdown report.

Расширенный контур истории клиента:

```text
lead/deal -> contact -> related contact deals + deals by LEAD_ID -> duplicate leads by phone/email -> activities/timeline/internal IM -> unified customer_history_bundle -> customer_path.md / LLM context
```

Продуктовый сценарий отчёта: `система -> РОП -> менеджер`. Отчёт читает только РОП; менеджер получает от РОПа отдельное поручение или сообщение, а не доступ к отчёту.

## 2) Source Of Truth

Если есть конфликт между документами и кодом, доверять коду.

- Общая настройка путей, времени и логирования: `setup.py`
- Краткий запуск CLI/UI/API: `README.md`
- Runtime env для OpenAI: `openai_api/config.py`
- Публичные ссылки на карточки Bitrix: `openai_api/bitrix_links.py`
- Расчёт стоимости OpenAI: `openai_api/pricing.py`
- Верхний CLI ROP assistant: `run_rop_assistant.py`
- FastAPI entrypoint: `api/app.py`
- Analyze jobs / CLI adapter: `api/jobs.py`
- Compact shadow job и evidence lookup: `api/compact_shadow.py`
- Candidates scoring, profile preview, lead-to-deal journey и лимиты workset (без LLM): `api/candidates.py`
- React UI: `frontend/src/App.tsx`, API-клиент `frontend/src/api.ts`
- Визуальный референс демо: `praktikm_rop_assistant_demo.html`
- ТЗ UI (продуктовый ориентир, не source-of-truth кода): `tz_front.md`
- Краткая Markdown-выгрузка свежих лидов: `export_recent_leads_summary.py`
- Bitrix REST client: `bitrix/client.py`
- Полная история клиента / fallback-связки: `bitrix/customer_history.py`
- Внутренние Bitrix IM-чаты CRM-сущностей: `bitrix/internal_im_chat.py`
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
- Short-call / недозвон filter: `openai_api/audio/short_call.py`
- Deal LLM-анализ, BANT/техническая/коммерческая квалификация и полный Markdown-отчёт: `openai_api/llm/analyze_deal.py`
- Lead LLM-анализ, BANT/техническая/коммерческая квалификация и полный Markdown-отчёт: `openai_api/llm/analyze_lead.py`
- OpenAI JSON client: `openai_api/llm/llm_client.py`
- Валидация LLM JSON перед отчётом, включая `qualification_assessment` полного lead- и deal-анализа: `openai_api/llm/validation.py`
- Контракт, prompt и детерминированная материализация compact attention delta: `openai_api/llm/attention_delta.py`
- Compact OKF packs: `openai_api/llm/attention_delta_knowledge.py`, `knowledge/clients/praktikm/attention_delta_*.md`
- Ограничивающая диагностика полноты compact-контекста: `openai_api/llm/context_completeness.py`
- Проверка evidence compact-ответа относительно переданного контекста: `openai_api/llm/evidence_coverage.py`
- Детерминированные playbooks compact-анализа: `openai_api/llm/deal_attention_playbooks.py`, `openai_api/llm/lead_attention_playbooks.py`, `openai_api/llm/lead_playbook_resolver.py`
- SQLite-хранилище change detection, UI feedback, analysis profiles, lifecycle кандидатов и immutable snapshots ежедневных сводок: `storage/rop_db.py`
- Deal change detection snapshot/diff: `openai_api/change_detection/snapshot.py`
- Deal/lead decision engine и mini recommendation: `openai_api/change_detection/decision_engine.py`
- Deal CRM stage policy / closed-lost classification: `openai_api/change_detection/stage_policy.py`
- Deal orchestration CLI с пропуском лишнего LLM: `openai_api/llm/analyze_deal_if_changed.py`
- Lead orchestration CLI с пропуском лишнего LLM: `openai_api/llm/analyze_lead_if_changed.py`
- Локальный benchmark/shadow runner: `benchmarks/run_attention_delta_shadow.py`
- Сравнение и replay локальных shadow-результатов: `benchmarks/compare_attention_delta.py`, `benchmarks/replay_attention_delta_postprocessing.py`
- Правила локальных benchmark-артефактов: `benchmarks/README.md`
- Инструкция ручного запуска change detection: `Docs/change_detection_runbook.md`
- Инструкция запуска полной истории клиента: `Docs/customer_history_runbook.md`
- Практические заметки по проверке customer history: `Docs/customer_history_experiment.md`
- Обработанная OKF-база ПрактикМ: `knowledge/clients/praktikm/index.md`
- Ручная проверка проекта: `Docs/ручная проверка проекта.md`

## 3) Critical Invariants

1. Bitrix-скрипты и API-адаптер должны оставаться read-only к CRM: только `get/list/fields`-подобные методы и запись локальных файлов/SQLite.
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
15. SQLite — локальное MVP-хранилище: change detection (`entity_state`, `analysis_runs`, …), UI feedback (`ui_reports`, `rop_decisions`, `outcomes`), review state (`candidate_review_state`), legacy-фильтр (`ui_candidate_filters`), именованные профили (`analysis_profiles`), lifecycle journey (`candidate_cases`) и snapshots ручных сводок (`daily_summary_runs`, `daily_summary_items`). Полноценной портфельной `deal_memory` / `lead_memory` из спеки пока нет.
16. Финальный ROP markdown report нельзя сохранять, если LLM JSON не прошёл `openai_api/llm/validation.py`.
17. Deal change detection должен считать fingerprint только по normalized snapshot, а не по Markdown и не по полному raw JSON.
18. `DATE_MODIFY` хранится в snapshot metadata, но не является самостоятельной причиной запускать LLM.
19. Прямой запуск `analyze_deal.py` / `analyze_lead.py` без `--allow-direct-llm` заблокирован, чтобы не тратить LLM в обход change detection.
20. Lead change detection использует `STATUS_ID` вместо `STAGE_ID`; задачи и смена ответственного считаются soft diff, новый звонок/email/message/comment/status/transcript — hard diff.
21. Полный `customer_path.md` остается аудитным/UI-артефактом. Для deal LLM prompt используется `history/deal_<id>_llm_context.md`, если он существует; иначе fallback на полный `customer_path.md`.
22. Закрытый провальный CRM-статус сделки определяется кодом до LLM через `stage_policy.py`, а не угадывается моделью по тексту истории.
23. Если сделка закрыта как проваленная, deal-анализ обязан вернуть `closed_deal_review`. Если для решения не хватает фактов, поручение менеджеру должно предусматривать один содержательный контакт с клиентом и фиксацию результата в CRM, без обещания вернуть сделку в работу.
24. Главный action-блок нового анализа — `rop_manager_message_block`: что РОПу проверить, почему это важно, какое сообщение отправить менеджеру, какой факт должен появиться в CRM, срок контроля, критерий выполнения и evidence.
25. `manager_action_block` сохранён для обратной совместимости: его `primary_text` — готовый текст именно клиенту, а не инструкция менеджеру. Чеклист блока содержит только факты для CRM после контакта.
26. Markdown-отчёт по сделке и лиду должен начинаться с раздела `## Что сделать РОПу сейчас`; текст клиенту выводится ниже как готовый материал менеджеру для одного контакта.
27. Deal-анализ обязан возвращать `money_path_diagnosis`: где застрял путь к деньгам, почему деньги под риском, у кого следующий шаг, какой факт нужен для движения и evidence.
28. Lead-анализ обязан возвращать `loss_diagnosis`: качество лида, качество обработки, сигнал источника, качество дозвона, качество следующего шага, финальный вердикт и evidence. Полный lead- и deal-анализ обязан возвращать `qualification_assessment` с независимыми BANT, технической и коммерческой оценками; deal-оценка не заменяет `deal_mode`, `payment_blocker` или `closed_deal_review`.
29. Нельзя уверенно писать `bad_lead`, если не было нормального дозвона, альтернативного канала или конкретного следующего шага; в таком случае использовать `bad_processing`, `data_gap` или другой более точный verdict. Неполные BANT или технические данные не являются техническим либо бюджетным отказом; `technical_mismatch` и `budget_below_new_equipment_minimum` требуют соответственно подтверждённого стоп-фактора или явно названного бюджета нового оборудования ниже 1 000 000 ₽.
30. Полная история клиента строится read-only: `customer_history_bundle` может читать root lead/deal, контакты, связанные сделки контакта, duplicate leads по телефону/email, активности, timeline comments и внутренние Bitrix IM-чаты CRM-сущностей, но не пишет в Bitrix.
31. `CONTACT_ID` — базовая связка клиента. Если `CONTACT_ID` отсутствует, fallback по телефону/email должен подтверждать совпадение через `crm.contact.get` или `crm.lead.get`; неподтвержденные кандидаты остаются только в diagnostics.
32. Если fallback нашел несколько подтвержденных контактов, автосклейка не применяется: такие совпадения считаются ambiguous и требуют ручной проверки.
33. Если fallback нашел дубль-лид по телефону/email, его история включается в `related_leads`, но контакт не считается найденным, пока нет подтвержденного contact link.
34. Внутренний контекст (`timeline comments`, заметки, служебные комментарии, внутренние Bitrix IM-чаты) должен храниться отдельно от клиентских касаний и не должен трактоваться как слова клиента.
35. Внутренние Bitrix IM-чаты включаются только вместе с internal context, попадают в `internal_context`/`unified_timeline`, а вложения отображаются только строкой `Файл: name.ext` без ссылок и скачивания.
36. Открытые линии не являются обязательным источником для ПрактикМ; события openline/chat не должны ломать выгрузку и могут быть проигнорированы с diagnostics.
37. `customer_history_bundle` сохраняет diagnostics: missing contact, fallback candidates/matches, unavailable sources, access denied и предупреждения. Эти diagnostics нужны для ручной проверки прав и связок.
38. Для root lead и duplicate leads связанные сделки нужно искать не только через контакт, но и read-only фильтром `crm.deal.list` по `LEAD_ID`.
39. Если lead имеет `STATUS_ID=CONVERTED` или успешную семантику, а связанная сделка найдена, основной ROP-анализ должен выполняться по сделке; lead-анализ для этого лида пропускается.
40. Если converted lead не имеет найденной связанной сделки, diagnostics должен явно писать ручное действие: открыть лид в Bitrix, найти сделку и проверить REST-доступ/связку по `LEAD_ID`.
41. Если у converted lead найдено несколько сделок, автоматический верхний CLI выбирает самую свежую, но история должна оставаться хронологически понятной через customer history и source lead context.
42. Анализ разрешен при неполном контексте, но diagnostics/LLM prompt/ROP report должны явно указывать ограничения и не делать выводы о содержании отсутствующих звонков.
43. Автоскачивание аудио должно быть missing-only по умолчанию: уже успешно скачанные и существующие локально записи не перекачиваются без явного `--redownload`.
44. Существенный ценовой разрыв не является автоматическим доказательством ошибочного закрытия или окончательной неквалификации. Deal-анализ должен сначала проверить сопоставимость КП: состав решения, обязательные/опциональные узлы, этапность, сервис, запуск, гарантию и то, что именно клиент сравнивает.
45. Модель видит полный доступный контекст, но перед `validate_deal_analysis` / `validate_lead_analysis` ответ нормализуется до лимитов JSON-контракта; такие изменения фиксируются в `model_metadata.normalization_changes`.
46. FastAPI не должен ломать CLI: analyze job вызывает существующий `run_rop_assistant.py` / те же модули, а не дублирует pipeline.
47. UI читает текущий `*_analysis.json` как есть; отдельный UI-only JSON-контракт поверх LLM пока не вводим. Для converted lead результат job обязан возвращать связанную сделку, если её полный анализ создан штатным handoff, а не пустой lead-результат.
48. `reports/` не раздаётся frontend'у целиком: UI получает analysis/report только через API; полный markdown — по запросу, не на первом экране.
49. Первый экран UI показывает кандидатов на внимание (Bitrix-фильтр + scoring без LLM), а не технический pipeline.
50. Основной candidates preview ручной сводки — дешёвый pre-LLM слой на базе выбранного `analysis_profile`: календарный период `today|yesterday|today_and_yesterday` считается по `Europe/Moscow`; preview читает Bitrix и SQLite, но не вызывает OpenAI. Legacy `/api/candidates` и `ui_candidate_filters` сохраняются для совместимости ручного экрана.
51. Звонки короче 20 секунд считаются `short_no_answer` (недозвон/автоответчик): duration через `ffprobe` пишется в audio manifest, такие файлы по умолчанию не транскрибируются.
52. Локальный UI на первом этапе без авторизации, только localhost.
53. Решения РОПа и outcomes сохраняются локально в SQLite (`rop_decisions`, `outcomes`) и не пишутся в Bitrix.
54. Ручной запуск принимает числовые ID или канонические ссылки Bitrix вида `/crm/deal/details/<id>/` и `/crm/lead/details/<id>/`. Ссылка определяет тип сущности; за один запуск нельзя смешивать ссылки на лиды и сделки.
55. Денежные суммы в UI показываются в читаемом формате: `10 000 000 ₽`, а не как техническое значение Bitrix с дробной частью и `RUB`.
56. Решение РОПа «Закрытие обосновано» исключает только конкретный lead/deal из основной очереди кандидатов, а не целую воронку или этап. Проверенные сущности доступны в режиме `reviewed`, а `Вернуть в контроль` снимает исключение.
57. Проверенная сущность возвращается в основную очередь при изменении стадии, воронки, суммы или наступлении даты контроля. Одно изменение `DATE_MODIFY` не является причиной автоповтора: оно отмечается только в аудите как обновление CRM после решения РОПа.
58. В deal prompt сравнение технических показателей требует нормализации единиц. Например, `150 банок/мин` и `9 000 банок/час` должны рассматриваться как совпадение при одинаковом типе продукта; если единица не указана, модель обязана запросить уточнение, а не объявлять несоответствие.
59. `attention_delta` — изолированный shadow-контур. Он не может перезаписывать legacy `*_analysis.json`, prompt, ROP markdown report, change-detection state или UI report; полный анализ остаётся baseline и fallback.
60. Compact run разрешён только после preflight по входным путям, записанным в завершённом полном `*_analysis.json`. Он не должен неявно запускать Bitrix-fetch, legacy pipeline или повторный полный анализ.
61. Перед сохранением успешного compact-результата обязательны: structured-output contract, entity-specific deterministic playbook/materialization и `validate_evidence_context_coverage`. При непокрытом evidence или ошибке класс fallback — `full_fallback_recommended`; автоматически повторять платный вызов нельзя.
62. Compact diagnostics ограничивают использование evidence, но не являются фактами о клиенте: отсутствие источника нельзя интерпретировать как отсутствие события или намерения клиента.
63. Compact knowledge packs выбираются только по типу сущности (`core + lead` или `core + deal`) и должны оставаться воспроизводимыми. Полная OKF-база legacy-анализа не подменяется этими пакетами.
64. Профиль анализа именованный и версионируемый; UI сохраняет последний выбранный профиль. Изменение профиля после preview инвалидирует создание сводки по старой версии.
65. Lead scope ограничен календарным периодом создания, исключает live-resolved источники DMP/DMP1 и настроенные негативные категории. Входящие звонки остаются частью activity context и сигнала методики дозвона.
66. Deal scope настраивается воронками и этапами профиля и может включать старые активные сделки независимо от даты создания. Источник DMP/DMP1 не исключает уже созданную сделку.
67. Лид и созданная из него сделка образуют один `journey_key`; в основной карточке предпочтительна сделка, а исходный lead сохраняется как origin. Сделка вне выбранного deal scope не подменяет карточку и показывается только как handoff warning.
68. Preview показывает весь найденный список, но `workset_selected` выделяет ограниченный набор по priority, lifecycle и слотам `new/backlog`. Остальные карточки остаются видимыми и могут быть добавлены вручную.
69. `daily_summary_runs` и `daily_summary_items` хранят immutable snapshot профиля и кандидатов на момент создания. Повторное чтение Bitrix не должно незаметно менять уже созданную сводку.
70. Полный LLM-анализ запускается только после явного подтверждения платных карточек и в пределах `paid_per_run`/`paid_per_day`. Штатный запуск сводки передаёт `force_llm=false`; свежий analysis переиспользуется, а changed/missing/failed требуют доступной платной ёмкости.

## 4) Key Domain Objects

- `Lead` — лид Bitrix24, локально представлен raw JSON, markdown history, workspace и analysis.
- `Deal` — сделка Bitrix24, локально представлена raw JSON, markdown history, audio manifest, workspace и analysis.
- `Customer history bundle` — единый raw JSON `*_customer_history_bundle.json` вокруг root lead/deal: root entity, период, контакты, связанные сделки, дубль-лиды, активности по сущностям, timeline comments, internal IM chats, tasks, unified timeline и diagnostics.
- `Related deal` — сделка, найденная через `CONTACT_ID`/`crm.deal.contact.items.get` и `crm.deal.list` по контакту или напрямую через `LEAD_ID`; попадает в общий контекст даже из другой воронки.
- `Related lead` — подтвержденный дубль-лид, найденный fallback-поиском по телефону/email, когда у root lead нет `CONTACT_ID`.
- `Converted lead handoff` — правило верхнего CLI: если лид сконвертирован и сделка найдена, дальнейшая транскрибация/анализ идут по сделке, а не по лиду.
- `Fallback candidate` — телефон/email/company из root lead/deal, сохраненный в diagnostics для ручной проверки; автоматически применяется только после подтверждения.
- `Fallback match` — подтвержденная fallback-связка с contact или duplicate lead через read-only Bitrix методы.
- `Activity` — CRM-активность: звонок, письмо, сообщение, задача или другое событие.
- `Timeline comment` — комментарий/событие таймлайна Bitrix.
- `Client touchpoint` — клиентское касание в customer history: звонок, письмо, сообщение, без смешивания с внутренними комментариями.
- `Internal context` — внутренние комментарии/заметки/таймлайн, используемые как контекст работы менеджера, но не как факт клиентского общения.
- `Internal IM chat` — привязанный к CRM lead/deal Bitrix IM-чат команды, читается read-only через `im.*`, хранится как `internal_context`, не как клиентское касание.
- `Tasks and control` — задачи и контрольные активности из CRM activities.
- `System event` — состояние/изменение сущности, например текущий статус связанной сделки или дубль-лида.
- `Transcript` — результат транскрибации звонка, обычно хранится в `transcripts/` в `.md`, `.txt`, `.json`.
- `Short no-answer call` — локальный звонок с `duration_seconds < 20`; в manifest помечается как `short_no_answer` / `skip_transcribe`.
- `Knowledge file` — обработанное правило ПрактикМ из `knowledge/clients/praktikm/`.
- `Analysis JSON` — структурированный ответ модели в `analysis/*_analysis.json`.
- `ROP manager message block` — главный action-блок `rop_manager_message_block`: готовое поручение РОПа менеджеру плюс ожидаемый CRM-факт, срок, критерий выполнения и evidence.
- `CRM stage policy` — детерминированный контекст стадии сделки: открыта/закрыта, причина закрытия, тип провала, основание для режима `closed_deal_review`.
- `Deal management blocks` — управленческие поля deal-анализа: `rop_manager_message_block`, `money_path_diagnosis`, `deal_mode`, `closed_deal_review`, `resource_control`, `payment_blocker`, `objection_handling`, `shaker_question`, `competitor_defense_checklist`, `priority_recommendation`.
- `Price comparability check` — блок deal-анализа `price_comparability_check`: проверка, сравниваются ли одинаковые КП/комплектации при существенном ценовом разрыве.
- `Lead loss diagnosis` — блок `loss_diagnosis`, который отделяет качество лида от качества обработки и фиксирует `bad_lead|bad_processing|data_gap|needs_nurture|ready_for_deal|unknown`.
- `Manager action block` — совместимый блок `manager_action_block`: `primary_text` содержит готовое сообщение клиенту, а `manager_checklist` — CRM-факты, которые менеджер фиксирует после контакта. Это вспомогательный материал, не главный результат отчёта.
- `ROP report` — человекочитаемый Markdown-отчёт в `analysis/*_rop_report.md`.
- `Workspace index` — `index.json`, фиксирует тип сущности, ID и локальные папки.
- `Candidate` — pre-LLM карточка внимания из `api/candidates.py`: entity, journey/origin, статус/стадия, priority, reason codes, analysis freshness, lifecycle, Bitrix URL и признак попадания в workset.
- `Candidate journey` — единый путь клиента с устойчивым `journey_key`; после конвертации одна карточка связывает origin lead с текущей deal и не допускает двойного анализа одного пути.
- `Analysis profile` — именованная версионируемая конфигурация в `analysis_profiles`: московский период, lead/deal scope, сигналы, review view, workset/paid limits и параметры анализа. Последний выбранный ID хранится в `ui_preferences`.
- `Workset` — ограниченный выделенный набор кандидатов внутри полного preview; overflow остаётся видимым и доступным для ручного выбора.
- `Daily summary snapshot` — неизменяемая запись `daily_summary_runs` + `daily_summary_items` с версией/снимком профиля, периодом, scope, кандидатами, выбранными journey и зарезервированной платной ёмкостью.
- `Candidate case` — локальный lifecycle journey в `candidate_cases`: `new`, `backlog` или `reactivation`, определяемый по first seen и signal hash.
- `Candidate filter` — legacy-сохранённый фильтр ручного экрана в `ui_candidate_filters`: тип lead/deal, окна дат, multi-select воронок/этапов, priority, limit.
- `Candidate review state` — текущее локальное состояние конкретного lead/deal после решения РОПа: `reviewed`, `snoozed` или `active`, снимок стадии/суммы и дата следующего контроля. Не является записью в Bitrix и не переносит правило на похожие сущности.
- `Manual input` — значение ручного запуска: один или несколько числовых ID либо канонических ссылок Bitrix. UI извлекает ID из ссылки и подставляет тип `lead` или `deal`.
- `Analyze job` — фоновая задача UI/API (`api/jobs.py`): опции как в CLI, live stages, results, `report_ids`.
- `UI report` — запись в `ui_reports`: ссылка на analysis/report paths + snapshot `report_json` для истории UI.
- `ROP decision` — локальное решение РОПа по отчёту (`rop_decisions`), без записи в Bitrix.
- `Outcome` — локальный исход после рекомендации (`outcomes`), включая отрицательные исходы.
- `Attention delta` — компактный структурированный shadow-вывод для одного существующего full analysis: необходимость внимания, причина, ROP action с typed evidence IDs, memory patch и entity-specific review. Не является штатным ROP report.
- `Compact shadow run` — сохранённый в `compact_shadow_runs` результат ручного compact-вызова: snapshot hash входного контекста, статус, модель, usage/cost, evidence coverage и fallback class.
- `Compact shadow feedback` — один локальный отзыв РОПа на compact run в `compact_shadow_feedback`: исход, причина, комментарий, исходный и финальный playbook. Не обучает модель и не изменяет CRM.
- `Evidence coverage` — детерминированная проверка, что typed evidence IDs compact-ответа присутствуют в переданных history/transcript/stage-policy данных.

## 5) Runtime Flows

Этот раздел держит только карту основных потоков. Подробные команды и проверки: `Docs/customer_history_runbook.md`, `Docs/change_detection_runbook.md`, краткий UI/API запуск — `README.md`.

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

Без `--include-related-contact-deals` pipeline сохраняет старый режим одиночной карточки. С флагом строится `*_customer_history_bundle.json`, где есть root entity, contact resolution, related deals, deals by `LEAD_ID`, related duplicate leads, activities by entity, unified timeline и diagnostics. При `--include-internal-context` дополнительно проверяются timeline comments и привязанные внутренние Bitrix IM-чаты.

Ключевые файлы в workspace:

- `history/*_customer_path.md` — аудитный человекочитаемый контекст;
- `raw/*_context.json` — старый raw context root-сущности;
- `raw/*_customer_history_bundle.json` — полный raw context клиента;
- `history/deal_<id>_llm_context.md` — compact context для deal LLM, если построен.

### D) Audio And Transcripts

Автоскачивание аудио встроено в lead/deal pipelines и отдельно доступно через missing-only downloader-скрипты. По умолчанию они не перекачивают уже успешно скачанные локальные файлы; для принудительной перекачки используется `--redownload`.

После успешного download/manifest duration измеряется через `ffprobe` (`openai_api/audio/short_call.py`). Звонки `< 20 сек` помечаются как `short_no_answer` и пропускаются оркестратором транскрибации в `run_rop_assistant.py`.

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

Перед сохранением итогового отчёта JSON проходит нормализацию и валидацию: длинные списки, для которых контракт задаёт максимум, ужимаются до лимита, а факт изменения пишется в `model_metadata.normalization_changes`. Сырой ответ модели всё равно сохраняется в `analysis/*_raw_model_output.txt`.

### F) Local UI And API Adapter

Локальный экран РОПа поверх текущего backend/CLI:

```powershell
.\venv\Scripts\python.exe -m uvicorn api.app:app --reload --host 127.0.0.1 --port 8000
cd frontend
npm run dev
```

UI: `http://127.0.0.1:5173` (Vite proxy `/api` → API).

Основные API:

- `GET /api/health`
- `GET /api/pipelines` — справочник воронок/этапов из `crm_pipeline_map.json`
- `GET|PUT /api/candidate-filters` — сохранённый фильтр кандидатов в SQLite, включая режим очереди `active|reviewed|all`
- `GET|POST /api/candidates` / `candidates/search` — Bitrix list + scoring без LLM; без выбранных этапов (и воронок для сделок) возвращает `ready=false`; применяет локальный `candidate_review_state`
- `GET|POST|PUT|DELETE /api/analysis-profiles` — именованные версионируемые настройки периода, lead/deal scope, сигналов и лимитов; последний выбранный профиль хранится в SQLite
- `POST /api/analysis-profiles/{id}/preview` — read-only Bitrix/SQLite preview без LLM: journeys, сигналы, lifecycle, workset и предварительная стоимость
- `POST /api/daily-summaries`, `GET /api/daily-summaries[/{id}]` — immutable snapshot выбранного preview и история ручных сводок
- `POST /api/daily-summaries/{id}/start` — явное подтверждение платных карточек, применение дневного/разового лимита и запуск lead/deal jobs с `force_llm=false`
- `POST /api/analyze` — фоновый job с опциями как в CLI; `force_llm=true` требует `confirm_paid=true`; auto = сначала lead, потом deal
- `GET /api/jobs/{job_id}` — live stages/progress
- `GET /api/reports`, `GET /api/reports/{id}` — история UI-отчётов
- `GET /api/reports/{id}/markdown` — полный markdown по запросу
- `POST /api/reports/{id}/rop-decision`, `POST /api/reports/{id}/outcome`

UI экраны: ручная ежедневная сводка с профилями и preview, legacy-кандидаты, ручной запуск, прогресс, отчёт из текущего analysis JSON, история, решение/исход РОПа. Preview показывает весь список и визуально выделяет ограниченный workset; платный старт требует отдельного подтверждения. Очередь кандидатов имеет режимы `На проверку`, `Проверенные РОПом` и `Все`; решение `Закрытие обосновано` скрывает сущность до значимого изменения. Ручной запуск принимает ID и ссылки Bitrix; для распознанной ссылки тип сущности выбирается автоматически.

### G) Candidates Scoring

Основной pre-LLM слой ручной сводки (`api/candidates.py`):

```text
analysis_profile + Moscow period -> live CRM source/status resolution + crm_pipeline_map stages
-> fresh leads + configured deal portfolio + activities
-> lead-to-deal journey deduplication
-> deterministic signals + analysis freshness + lifecycle
-> priority ordering -> highlighted workset + visible overflow + paid capacity preview
```

Default profile использует период `today_and_yesterday` по Москве, исключает DMP/DMP1 только для лидов, сохраняет входящие звонки, выбирает настроенные deal pipelines/stages и выделяет workset до 15 карточек. Сигналы включают просроченную задачу, сомнительное закрытие, негативный свежий лид, отсутствие датированного следующего шага, post-proposal/payment control и мягкий `call_method_gap`. Это triage до LLM, не замена analysis JSON. Legacy saved-filter scorer остаётся отдельным совместимым маршрутом.

### H) Compact Attention-Delta Shadow

Это отдельная ручная проверка компактного JSON-контракта, а не второй штатный pipeline. Benchmark runner и UI получают входы только из `input_files`, записанных полным анализом; до платного запроса выполняется локальный preflight. Для benchmark API-вызов требует `--allow-api`, а legacy paid-run — отдельное явное подтверждение.

```text
full analysis envelope
-> load recorded history/transcript/diagnostics/stage policy
-> compact prompt (static core + entity pack)
-> Structured Outputs
-> deterministic playbook normalization/materialization
-> contract + evidence coverage
-> compact_safe | full_fallback_recommended
```

В UI доступны read-only review, запуск одного compact job для сущности, polling статуса, раскрытие исходного evidence и один feedback на run. Run хранится в SQLite, привязан к hash фактически поданных inputs; `is_current=false` означает, что исходный контекст с тех пор изменился. Compact job не делает Bitrix-вызовов, но может вызвать OpenAI после успешного preflight. Ошибка или coverage failure сохраняются, автоматически не перезапускаются и не меняют legacy report.

## 6) Where To Change Code By Task Type

- Новый Bitrix-метод или изменение REST-обработки: `bitrix/client.py` и конкретный `1_fetch_*_context.py`.
- Изменение верхнего CLI, выбора lead/deal, handoff converted lead в сделку, включения transcribe/analyze, skip short calls: `run_rop_assistant.py`.
- Изменение обзорной выгрузки свежих лидов по этапам/датам/источникам: `export_recent_leads_summary.py`.
- Изменение полной истории клиента, fallback по телефону/email, duplicate leads, поиск сделок по `LEAD_ID`, diagnostics или разделения событий: `bitrix/customer_history.py`.
- Изменение поиска/чтения внутренних Bitrix IM-чатов и преобразования сообщений в `internal_context`: `bitrix/internal_im_chat.py`.
- Изменение диагностики полноты контекста, `manual_actions.md`, ссылок Bitrix для ручного добора звонков/сделок: `bitrix/context_diagnostics.py`.
- Изменение Markdown полной истории клиента: `bitrix/customer_history_report.py`.
- Изменение CLI полного customer history: `bitrix/deals/run_deals_customer_path_pipeline.py`, `bitrix/leads/run_leads_customer_path_pipeline.py`, `1_fetch_*_context.py`.
- Изменение состава raw bundle по сделкам: `bitrix/deals/1_fetch_deals_context.py`.
- Изменение состава raw bundle по лидам: `bitrix/leads/1_fetch_leads_context.py`.
- Изменение Markdown-истории сделки: `bitrix/deals/2_build_deals_customer_path_report.py`.
- Изменение Markdown-истории лида: `bitrix/leads/2_build_leads_customer_path_report.py`.
- Изменение compact LLM context сделки, включая чтение `customer_history_bundle`: `bitrix/deals/4_build_deals_llm_context.py`.
- Изменение скачивания аудио Bitrix, manifest/missing-only и duration enrichment: `bitrix/deals/download_deals_call_audio.py`, `bitrix/leads/download_leads_call_audio.py`, `openai_api/audio/short_call.py`.
- Изменение папок, имён файлов и workspace: `bitrix/workspace.py` плюс `3_prepare_*_workspace.py`.
- Изменение транскрибации, chunking, `ffmpeg`: `openai_api/audio/transcribe_core.py`.
- Изменение CLI выбора аудио и сохранения transcript bundle: `openai_api/audio/local_file_transcribe.py`.
- Изменение модели/лимитов/стоимости анализа: `.env`, `.env.example`, `openai_api/config.py`.
- Изменение Responses API reasoning/structured output или поддержки модели: `openai_api/llm/llm_client.py`, `openai_api/pricing.py`, `openai_api/config.py`.
- Изменение публичных ссылок на лиды/сделки Bitrix: `BITRIX_PORTAL_URL` и `openai_api/bitrix_links.py`.
- Изменение тарифов моделей и формулы стоимости: `openai_api/pricing.py`.
- Изменение вызова OpenAI Responses API и JSON-парсинга: `openai_api/llm/llm_client.py`.
- Изменение проверки обязательных полей, запрещённых плейсхолдеров и нормализации LLM JSON перед валидацией: `openai_api/llm/validation.py`.
- Изменение структуры deal-анализа, правил промпта или Markdown-отчёта: `openai_api/llm/analyze_deal.py`.
- Изменение классификации закрытых/провальных стадий сделки: `openai_api/change_detection/stage_policy.py`.
- Изменение структуры lead-анализа, правил промпта или Markdown-отчёта: `openai_api/llm/analyze_lead.py`.
- Изменение mini recommendation, повторного использования последнего поручения РОПа менеджеру или сохранённого клиентского текста: `openai_api/change_detection/decision_engine.py`.
- Изменение приоритетного набора OKF-файлов: `knowledge_files()` в `openai_api/llm/analyze_deal.py`.
- Изменение правил клиента ПрактикМ: файлы в `knowledge/clients/praktikm/`.
- Изменение FastAPI routes / CORS / report endpoints: `api/app.py`.
- Изменение analyze job, auto lead→deal resolve, progress stages: `api/jobs.py`.
- Изменение candidates scoring / фильтров / приоритетов и применения review state: `api/candidates.py`.
- Изменение analysis profiles, Moscow period, lead/deal scope, journey deduplication, сигналов, lifecycle, workset или cost preview: `api/candidates.py`, `storage/rop_db.py`, `api/app.py`.
- Изменение immutable daily summary snapshot, платных лимитов или запуска сводки: `storage/rop_db.py`, `api/app.py`, `api/jobs.py`.
- Изменение состояния «проверено РОПом», отложенного контроля или возврата в очередь: `storage/rop_db.py` (`candidate_review_state`) и `api/app.py`.
- Изменение UI экранов, copy-блока менеджеру, истории, решений РОПа и режима очереди: `frontend/src/App.tsx`, `frontend/src/api.ts`, стили `frontend/src/index.css`.
- Изменение распознавания ID и ссылок Bitrix в ручном запуске: `parseManualInput()` в `frontend/src/App.tsx`.
- Изменение UI feedback / saved candidate filter tables: `storage/rop_db.py` (`ui_reports`, `rop_decisions`, `outcomes`, `ui_candidate_filters`).
- Изменение compact JSON-контракта, prompt или детерминированных playbooks: `openai_api/llm/attention_delta.py`, `openai_api/llm/deal_attention_playbooks.py`, `openai_api/llm/lead_attention_playbooks.py`, `openai_api/llm/lead_playbook_resolver.py`.
- Изменение compact knowledge packs или их воспроизводимого выбора: `knowledge/clients/praktikm/attention_delta_*.md`, `openai_api/llm/attention_delta_knowledge.py`.
- Изменение диагностики/валидации compact evidence: `openai_api/llm/context_completeness.py`, `openai_api/llm/evidence_coverage.py`.
- Изменение benchmark/shadow preflight, telemetry или ручного сравнения: `benchmarks/run_attention_delta_shadow.py`, `benchmarks/compare_attention_delta.py`, `benchmarks/replay_attention_delta_postprocessing.py`.
- Изменение API/UI compact review или фонового compact job: `api/compact_shadow.py`, `api/app.py`, `frontend/src/App.tsx`, `frontend/src/api.ts`.
- Изменение хранения compact run/feedback: `storage/rop_db.py` (`compact_shadow_runs`, `compact_shadow_feedback`).
- Обновление краткого запуска: `README.md`.
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
- `ANALYSIS_REASONING_EFFORT`
- `ANALYSIS_MAX_OUTPUT_TOKENS`
- `ATTENTION_DELTA_MAX_OUTPUT_TOKENS`
- `USD_RUB_RATE`

ROP assistant state:

- `ROP_DB_PATH`

Логирование payload preview:

- `OPENAI_LOG_PREVIEW_LINES`
- `OPENAI_LOG_PREVIEW_CHARS`

Важно:

- для настроенной модели и длинных отчётов может понадобиться увеличить `ANALYSIS_MAX_OUTPUT_TOKENS`;
- fallback legacy-модели задаёт `openai_api/config.py` (сейчас `gpt-5.4-mini`), но реальная модель определяется `ANALYSIS_MODEL` из `.env`; в `pricing.py` также описаны тарифы `gpt-5.6-terra` и `gpt-5.6-luna`;
- `ATTENTION_DELTA_MAX_OUTPUT_TOKENS` относится только к compact shadow и включает reasoning + видимый JSON; не использовать его как лимит legacy report;
- цены моделей зашиты в `openai_api/pricing.py`, а курс рубля берётся из `USD_RUB_RATE`;
- UI/API используют тот же `.env` и существующий `venv`.

## 8) Encoding / Text Policy

- Текстовые файлы проекта ожидаются в `UTF-8`.
- Кириллица используется намеренно: документы, отчёты, промпты, OKF-база, логи и сообщения.
- Не заменять русский текст транслитом или Unicode escape без крайней необходимости.
- Если терминал показывает битую кириллицу, не копировать её обратно в исходники без проверки файла.
- Markdown-отчёты и JSON сохранять с `ensure_ascii=False`, чтобы русский текст оставался читаемым.

## 9) Known Pitfalls

1. Перед анализом всегда готовить workspace через lead/deal pipeline; `analyze_*` ожидает файлы в `reports/rop_assistant/...`.
2. `reports/` содержит чувствительные рабочие артефакты, а не исходный код; UI не должен монтировать всю папку статикой.
3. `latest` transcript выбирается по mtime; если звонков несколько, передавать конкретный `--transcript` или использовать `--transcript all`.
4. Deal и lead похожи по pipeline, но имеют разные JSON contracts, prompts и renderer requirements.
5. OKF-файлы — правила оценки, а не evidence фактов сделки/лида.
6. `customer_history_bundle.related_leads` без контакта означает найденную продолженную историю по телефону/email, но не найденный контакт.
7. `fallback_match_used=false` при `fallback_related_leads_used=true` — нормальная ситуация: дубль-лид найден, contact link отсутствует.
8. `crm.timeline.comment.list` по contact может вернуть `Access denied`, даже если timeline по сделкам доступен; это права webhook/user, не ошибка renderer.
9. `crm_pipeline_map.json` — локальная выгрузка CRM-карты; продуктовую классификацию закрытых стадий менять в `stage_policy.py`. UI-фильтр кандидатов и имена стадий читаются из `deal_pipelines` / `lead_pipeline` этой карты (реальные Bitrix STATUS_ID / STAGE_ID), а не из семантики `stage_policy`.
10. Если JSON модели обрезан, первым делом проверять `ANALYSIS_MAX_OUTPUT_TOKENS`; JSON mode не заменяет бизнес-валидацию.
11. Если модель вернула больше evidence/list items, чем разрешает контракт, это не должно валить pipeline: sanitizer ужимает списки перед валидацией, но prompt всё равно должен явно задавать лимиты.
12. `ffmpeg`/`ffprobe` должны быть в `PATH`; без `ffprobe` short-call filter не сможет надёжно измерить duration.
13. У converted lead без `CONTACT_ID` сделка может быть найдена только через `LEAD_ID`; не считать отсутствие contact link доказательством отсутствия сделки.
14. По сделке, созданной из лида, звонки могут физически лежать в source lead; deal context/diagnostics должны сохранять source entity и Bitrix-ссылку на исходную активность.
15. Profile preview может давать много high-карточек при широком deal scope: это triage, его нужно калибровать по живым примерам, а не принимать за LLM-вердикт. Ограничение workset выделяет приоритет, но не скрывает overflow.
16. Analyze job из UI может быть долгим: это полный CLI-пайплайн, прогресс смотреть через `/api/jobs/{id}`.
17. Ссылка в ручном запуске должна содержать путь `/crm/lead/details/<id>/` или `/crm/deal/details/<id>/`; смешанные ссылки на лиды и сделки UI отклоняет до старта job.
18. `candidate_review_state` применяется только к конкретной сущности. Нельзя скрывать всех кандидатов этапа после одного подтверждённого закрытия; перенос решения на похожие сущности требует отдельного накопленного feedback-паттерна.
19. Текущий candidates scorer видит изменение стадии/воронки/суммы напрямую. Новый звонок, письмо, комментарий или внутренний чат после решения РОПа требуют semantic diff полного контекста; пока они не должны автоматически возвращать сущность только по `DATE_MODIFY`.
20. Compact review не является доказательством готовности заменить legacy analysis: он доступен лишь при сохранённых full-analysis inputs, а `full_fallback_recommended`, error или устаревший snapshot требуют вернуться к полному контуру.
21. Запуск compact run из UI платный и ручной; API preflight не гарантирует достаточность фактов, а только проверяет наличие зафиксированных входов. Не добавлять автоматические retry или batch-вызовы без отдельного продуктового решения.
22. `benchmarks/local/` и `benchmarks/results/` намеренно игнорируются Git. В них допустимы только локальные чувствительные артефакты; в tracked manifest нельзя добавлять реальные CRM-выгрузки, транскрипты, телефоны, имена или отчёты.

## 10) Current Gaps

- Нет Telegram Bot API и автоматической отправки отчётов/поручений менеджеру.
- Нет auth/multi-user кабинета: UI локальный, только localhost.
- Daily candidate engine учитывает задачи, следующий шаг и детерминированные сигналы, но счета/оплаты пока не загружаются как отдельный надёжный CRM-источник; payment-сигнал зависит от доступных полей/активностей.
- SQLite-контур включает profiles, candidate lifecycle, daily summary snapshots и feedback, но нет аналитики качества рекомендаций, полноценного semantic diff всех CRM-активностей и портфельной `deal_memory` / `lead_memory`.
- Compact attention-delta работает только как isolated shadow/review: он не включён в штатный CLI/change-detection flow и не может заменить legacy report без отдельной валидации и продуктового решения.
- Нет автоматического отбора безопасных compact cases, batch-режима и автоматического принятия compact-результата; coverage failure ведёт только к `full_fallback_recommended`.
- Нет Pydantic-схем и нормального тестового набора для raw parsing, markdown rendering и report rendering.
- Автоматическое скачивание аудио и contact timeline зависят от прав Bitrix webhook/user; часть сценариев остается ручной.
- Комментарии к задачам не выгружаются отдельным API-методом.
- Company fallback пока только diagnostic candidate, без автосклейки.
- Wazzup-переписка не выгружается через текущий Bitrix REST-контур; отдельная интеграция потребует Wazzup API и связку по телефону клиента.
