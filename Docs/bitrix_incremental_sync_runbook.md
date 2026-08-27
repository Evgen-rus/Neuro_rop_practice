# Этап 2: накопительный CRM context и ранний automatic gate

Дата проверки: 2026-08-27. Изменения локальные, без deploy, Bitrix write и платных API-вызовов.

## A. Исходная проблема

Automatic daytime cycle ставил analysis job на каждую свободную сделку active work pool. Решение FULL / MINI / skip принималось после CRM/audio preparation. Даже повторный анализ без новой клиентской информации перечитывал историю, связанные сущности, chat discovery и коммерческие источники.

В старой реализации аргумент `created_after` у timeline не превращался в ограничение API. Activities имели incremental merge с перекрытием 5 минут, но timeline мог перечитываться полностью. Суточной полной сверки накопленного deal context не было; schema из предыдущего raw snapshot могла оставаться без TTL.

## B. Новая архитектура

Существующие deal-control sync и manager trajectory collector сохранены. После них, до создания jobs, `api/crm_change_gate.py` выбирает режим для каждой свободной сделки:

| Режим | Когда | Действие |
| --- | --- | --- |
| `full` | Нет snapshot/ack/cursor; наступила суточная сверка | Полное чтение источников, merge с накопленной историей |
| `incremental` | Изменилась сделка, trajectory, activity, task, timeline, связанная сущность или известный чат; требуется recovery | CRM delta, merge, обычный audio и существующий decision engine |
| `audio` | CRM без изменений, но запись требует discovery/readiness/growth recheck | Материализация сохранённого CRM context, audio pipeline без heavy CRM fetch |
| `local` | Новый локальный transcript или пересечение временного порога | Существующий анализ по локальному context без CRM fetch |
| `skip` | Нет сигналов и обязательных проверок | Job не создаётся; automatic item завершается с `publication_status=reused` |

Признаки сделки берутся из уже обновлённого portfolio: `DATE_MODIFY`, stage, manager, amount. Trajectory watermark — локальный ingestion ID, а не время самого события. Поздно загруженное старое событие поэтому не теряется. Activity-probe использует на сделку `max(activity_probe_at, activity_cursor) - 15 минут`; успешный skip двигает только `activity_probe_at`. Сделки с разным окном не делят один `min()`-фильтр. Прочие probes: версии связанных сущностей, первая страница timeline, последние сообщения известных чатов, выбранные открытые задачи. Shared entity сравнивается с snapshot каждой сделки отдельно. Отсутствие строки в `crm.*.list` само по себе не считается изменением.

После delta остаются существующие FULL / MINI / skip правила. Добавлен один технический soft signal `operational_context_changed`, чтобы изменение task/internal context дошло до MINI. Он не превращает внутреннюю запись в клиентский контакт и сам по себе не запускает платный FULL. Prompts, lead/deal JSON-контракты и UI не менялись.

## C. Incremental-источники и ограничения API

| Источник | Стратегия | Полная сверка |
| --- | --- | --- |
| Activities | `>=LAST_UPDATED`, 15 минут overlap, merge по ID; FILES/COMMUNICATIONS сохранены | Чтение без incremental boundary |
| Timeline comments | DESC paging от новых записей до известного ID старше overlap; merge по ID | Все страницы, включая старые исправленные comments |
| Stage history | `>=CREATED_TIME` с overlap, merge по ID | Вся доступная история |
| Task state | Изменённые/new/open/failed задачи; закрытые неизменённые берутся из snapshot | Повторный task get; trajectory task history остаётся в прежнем collector |
| IM/task messages | Известный dialog ID, LIMIT 50, paging через LAST_ID до известной границы; накопление messages/users/files | Все доступные страницы |
| Customer history | Merge root/source lead/related entities; уже известная история не удаляется из-за сдвига периода | Повторное чтение текущих связей; прежняя история исчезнувших связей помечается `historical_link` |

Timeline API документирует фильтры ENTITY_TYPE и ENTITY_ID, но не фильтр CREATED. Поэтому не передаётся вымышленный фильтр даты. [Официальный timeline API](https://apidocs.bitrix24.ru/api-reference/crm/timeline/comments/crm-timeline-comment-list).

Stage history поддерживает CREATED_TIME и операторы фильтрации. [Официальный stage history API](https://apidocs.bitrix24.com/api-reference/crm/crm-stage-history-list.html).

IM API ограничивает LIMIT значением 50. Используется чтение от новых сообщений назад через LAST_ID, а не FIRST_ID с предположением, что старый anchor всегда существует. [Официальный IM messages API](https://apidocs.bitrix24.ru/api-reference/chats/messages/im-dialog-messages-get.html).

Первое чтение и full reconciliation сохраняют прежние правила периода/доступности источников. Этап не расширяет права webhook или продуктовый период истории. Ранее накопленная история не обрезается при движении периода.

## D. Cache и discovery

`*.fields` хранится в SQLite на сутки; ключ включает hash credential scope. Ошибки не становятся cache hit. Даже manual/full использует свежую schema в пределах TTL: полный CRM fetch не означает повторный schema request для каждой сделки.

Deal/contact/company/user/task entity get и одинаковые list requests переиспользуются внутри одного context job. Entity records не переносятся между jobs вслепую: изменившаяся сделка перечитывает их. Между idle ticks сохраняется целый подтверждённый snapshot.

Chat discovery отделён от чтения известных dialog IDs. Успешный discovery, включая пустой, имеет суточный срок; новый discovery вызывается также по activity/timeline/chat/trajectory/deal-field сигналам и при full refresh. Ошибка не продвигает discovery timestamp. Уже известные чаты не удаляются из-за пустого поиска. Новый чат без иных сигналов будет найден при следующей суточной сверке, если он доступен используемому API поиска.

По решению пользователя от 2026-08-27 сбор структурированных invoices, smart invoices и product rows отключён для текущего портала: `crm.invoice.list`, `crm.item.list` (entityTypeId=31) и `crm.deal.productrows.get` не вызываются ни при incremental, ни при initial/full/manual/force/reconciliation. Ранее использовавшаяся negative-cache policy заменена этим отключением.

Raw сохраняет `product_rows=None`, `invoice_attempts=[]` и `structured_commercial_sources_enabled=False`. Это признак отключения сбора, а не утверждение, что счетов в CRM нет. Старые snapshot продолжают читаться; переход на отключённые источники не создаёт `commercial_refs_changed` по счетам/товарам. Сумма сделки, коммерческие вложения и упоминания счетов/оплат в activities, письмах, комментариях и transcripts не отключаются. LLM context явно сообщает об отключённых структурированных источниках. API/lead-контракт и аудио не меняются.

## E. Независимый audio-путь

Gate отдельно проверяет сохранённые звонки и manifest. Отсутствие DATE_MODIFY не отменяет:

- текущую recording recheck window, включая уже транскрибированную запись;
- FILES discovery для звонков в пределах прежних 5 дней;
- ожидание загрузки, readiness и транскрибации;
- pending Max voice из существующего manifest.

Известные звонки без FILES перепроверяются точечным `crm.activity.get`; это работает и в audio-only, и в обычном heavy pipeline. Успешный FILES refresh сохраняется в канонический snapshot, не продвигая CRM history cursors.

Существующие disk.file.get, сравнение размера, stable observations, готовность записи, previous-workday evening → morning, `transcribed_and_purged`, short/no-answer и stale transcript при росте сохранены. Для незавершённой загрузки readiness recheck продолжается и после обычного окна свежего звонка.

После audio-only анализ запускается только при изменении transcript signature либо уже обнаруженном local analysis signal. Если транскрипт не изменился, результат `audio_idle`: прежний отчёт не публикуется повторно. Обнаружение ещё неизвестного Max voice остаётся задачей CRM/timeline обновления.

## F. Reconciliation и manual

Сверка планируется спустя сутки после последней full-попытки на очередном рабочем scheduler tick. Это не отдельный ночной job: в выходные/вне окна scheduler задержка может быть больше 24 часов. `full_success_at` отражает успешность всех источников отдельно от `full_attempt_at`.

При сбое источников сохраняется retry state с интервалами 25, 50, 100 минут и далее, максимум сутки. Known failed probes не вызывают heavy storm до retry deadline; восстановившийся источник может вызвать refresh раньше. Ошибка deal-control/trajectory collection обрабатывается консервативным refresh. Отсутствующий корневой activity cursor требует full retry.

Manual API/CLI по умолчанию получает `context_refresh_mode=full`; force_llm также принудительно выбирает full. Internal automatic flags не добавлены в публичный HTTP request contract. Автоматические jobs используют `force_llm=False`.

## G. Сохранность, конкуренция и recovery

В `storage/rop_db.py` добавлена таблица `crm_context_sync_state`. Состояние сделки содержит raw context, customer history и cursors в одном JSON payload. Запись пары snapshot выполняется SQLite compare-and-swap по revision. Старый writer не может затереть новую revision. Destructive migration отсутствует.

Только после успешного commit создаются файловые JSON projections через temporary file, fsync и os.replace. Падение до commit не двигает cursors; падение между заменой двух файлов восстанавливается из канонической пары SQLite. Два отдельных JSON-файла не являются общей атомарной транзакцией для произвольного внешнего reader.

OS locks защищают scheduler sync/collect/enqueue между локальными процессами и deal workspace на время pipeline/publication/acknowledgement. Lock освобождается ОС при аварии, без опасного перехвата по TTL. Прямой низкоуровневый fetch дополнительно защищён CAS; штатный entrypoint — API job или `run_rop_assistant.py`.

Ack записывается после `audio_idle` или terminal `publish_ready` со статусом skip/mini/full, с event watermark, зафиксированным при enqueue. Событие, появившееся во время job, остаётся сигналом следующего цикла. Неуспешный анализ (`error`, нет publish_ready) не подтверждает новый context. Ошибка источника сохраняет старую информацию и cursor; списки чатов восстанавливаются по ID, а не по позиции.

Эти механизмы защищают от локальной потери данных при сбоях и гонках, но не дают абсолютной гарантии восстановления удалённого до чтения события или данных, недоступных webhook. Ограничения перечислены в M.

## H. OLD vs NEW benchmark

OLD — предоставленный пользователем live RUN 2: 10 сделок, 396 physical Bitrix HTTP, FULL 0, MINI 9, SKIP 1, OpenAI 0. Указанное в исходной диагностике среднее ~36,2 не совпадает с 396/10; без исходного scope его не используем для расчёта экономии.

NEW — **локальный synthetic transport benchmark, не live Bitrix**. Выполняет production gate/context fetch через реальный read-only client, batch и pagination с подменой HTTP transport синтетическим сервером. В fixture по 120 timeline comments на сделку, связанные контакты, пустые commercial sources; нет pending audio. Context-only ack не имитирует успешный реальный LLM-анализ. Таблица ниже сохраняет исторический замер до отключения счетов/товаров.

| Метрика | A initial/full | B immediate idle | C одна новая activity |
| --- | ---: | ---: | ---: |
| Сделок | 10 | 10 | 10 |
| Моделируемые physical transport calls | 184 | 4 | 20 |
| Реальные Bitrix HTTP | 0 | 0 | 0 |
| Logical commands | 184 | 23 | 39 |
| Heavy context fetch | 10 | 0 | 1 |
| Skipped before heavy fetch | 0 | 10 | 9 |
| Audio checks | 0 | 0 | 0 |
| LLM requests | 0 | 0 | 0 |
| Timeline logical requests | 40 | 20 | 22 |
| Chat discovery requests | 30 | 0 | 3 |
| Invoice/product requests | 30 | 0 | 3 |
| Wall seconds | 1,905 | 0,266 | 0,592 |

Benchmark не включает стоимость существующих deal-control/trajectory collectors. Нельзя трактовать 396 → 4 как измеренное снижение реального полного цикла. Live full-cycle не запускался: он может запустить платный OpenAI и обновить рабочие отчёты; требуется отдельное подтверждение.

Воспроизведение без сети и секретов из корня репозитория:

```powershell
.\venv\Scripts\python.exe scripts/bitrix_context_sync_benchmark.py --output logs/phase2_context_sync/fixture_benchmark.json
```

Артефакт — `logs/phase2_context_sync/fixture_benchmark.json`, без CRM payload. Wall time меняется между запусками.

После отключения счетов/товаров повторён тот же локальный fixture: initial **154 HTTP** вместо 184, idle **4** без изменений, одна изменённая сделка **17** вместо 20. Во всех трёх сценариях `invoice_product_requests=0`; остальные счётчики методов сохранены. Это моделируемые транспортные вызовы, реальные Bitrix/OpenAI запросы не запускались. Артефакт — `logs/phase2_context_sync/no_commercial_benchmark.json`. Отдельные regression tests проверяют full/incremental без этих запросов, безопасный переход с непустых legacy данных и сохранение коммерческих фактов из activities/вложений.

Проверка этого изменения: полный `python -m unittest discover -s tests` — **740 tests, OK, 153,710 s**; `git diff --check` и UTF-8 проверены. Frontend не изменён, lint/build не запускались. Изменение локальное, без deploy и без запуска внешних API. Разбор причин долгого итогового SKIP остаётся отдельной задачей.

## I. Что осталось на idle

В данном fixture immediate idle: два activity list, один contact versions list и один batch с 20 timeline head commands — 4 моделируемых физических вызова. Это окно сразу после full, не live-цикл через несколько часов. Повторные skip двигают `activity_probe_at`, поэтому окно не растёт до полной history. Discovery/invoices/products/heavy jobs отсутствуют. В живом цикле добавятся неизменённые collectors, связанные сущности, известные чаты/open tasks, пагинация, retries и независимые audio checks. **Точное число live Bitrix HTTP после изменения пока не измерено.**

## J. Оставшиеся дорогие операции и tracing

Полная сверка длинных timeline/chat histories, discovery по сигналам, коллекция trajectory и audio size/download checks остаются основными кандидатами на следующий замер. Structured commercial fetch отключён. Gate ограничивает head probes одной страницей на источник, но число logical commands растёт с числом связанных сущностей/чатов/задач. Activity version list может иметь несколько страниц.

Privacy-safe physical usage trace сохранён: в него не добавляются CRM payload, IDs или содержимое сообщений. Добавлены допустимые component labels для gate/delta/cache/discovery/reconciliation; активные source scopes позволяют отделять cheap detection, timeline/stage, chat updates/discovery и entity cache. Старые invoice/product/audio/trajectory labels сохранены. Один физический batch может содержать много logical commands. Наличие label в allowlist само по себе не означает отдельный запрос или cache hit; причины full reconciliation хранятся в sync plan.

## K. Изменённые файлы

| Файл | Ответственность |
| --- | --- |
| `api/crm_change_gate.py` (новый) | План режимов, cheap probes, независимые local/audio signals, ack |
| `api/daytime_cycle.py` | Gate перед jobs, persisted ранние skips, scheduler OS lock |
| `api/jobs.py` | Internal mode routing, workspace lock, acknowledgement, audio-idle publication |
| `run_rop_assistant.py` | CLI modes, local/audio-only orchestration, lock прямого запуска |
| `bitrix/context_sync.py` (новый) | Safe paging/merge, TTL cache, CAS projections, locks, failure retention |
| `bitrix/deals/1_fetch_deals_context.py` | Накопительный deal snapshot и cursors, tasks, atomic commit |
| `bitrix/deals/run_deals_customer_path_pipeline.py` | Full/incremental/audio preparation |
| `bitrix/deals/download_deals_call_audio.py` | Независимый FILES recheck, pending readiness, атомарный manifest |
| `bitrix/customer_history.py` | Overlap 15 минут, timeline paging, сохранение накопленной истории и чатов |
| `bitrix/internal_im_chat.py` | Discovery cache, known dialogs, накопление сообщений |
| `bitrix/usage_trace.py` | Дополнительные component labels |
| `storage/rop_db.py` | Additive sync table, CAS, trajectory ingestion watermarks |
| `openai_api/change_detection/snapshot.py` | Deal-only fingerprint внутреннего context |
| `openai_api/change_detection/decision_engine.py` | Soft MINI signal для этого fingerprint |
| `scripts/bitrix_context_sync_benchmark.py` (новый) | Offline A/B/C transport benchmark |
| `tests/crm_sync_fixture.py` (новый) | Синтетический Bitrix и temporary DB/workspaces |
| `tests/test_crm_change_gate.py` (новый) | Gate/sync/audio/recovery integration tests |
| `tests/test_crm_incremental_sync.py` | Обновлён overlap contract |
| `tests/test_deal_change_decision.py` | Проверка нового soft MINI signal |
| `ARCHITECTURE.md` | Обновлены устойчивые факты и source-of-truth pointers |
| `.gitignore` | Точное исключение для versioned runbook, остальные локальные Docs закрыты |
| `Docs/bitrix_incremental_sync_runbook.md` (новый) | Этот отчёт и порядок проверки |

## L. Проверки

Новый `test_crm_change_gate.py` покрывает 27 сценариев: idle без job; новая activity и refresh пустых commercial sources; stage/contact changes; timeline comment/overlap/dedup; failed timeline/backoff; failed activity cursor; failed analysis acknowledgement; reconciliation старого edit; новый чат и 130 сообщений; failed discovery; failed chat update; независимый FILES; FILES в heavy path; local transcript; поздний trajectory event; stale writer; workspace lock; shared schema cache; manual force; task description; commit failure; audio idle/new transcript; schema TTL/entity memo; failed invoice; shared contact с разными snapshot versions; reordered failed chats; сохранение чата исчезнувшей связи.

`test_crm_incremental_sync.py` проверяет 15-минутное overlap и существующие batch/preloaded-root/failure сценарии. `test_deal_change_decision.py` добавляет soft MINI для operational context. Существующие `test_audio_retention.py` проверяют growth, stale transcript, readiness, purge и evening/morning window; они не заменены новыми упрощёнными правилами.

Команда полного набора:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests
git diff --check
```

Финальный полный набор: **710 tests, OK, 142,131 s**. Исходный baseline до реализации: 682 tests, OK. `git diff --check` проходит; предупреждения LF → CRLF относятся к настройке рабочего дерева, ошибок whitespace нет. В полном наборе есть ResourceWarning незакрытого temporary file из существующего prompt-lab теста; suite завершился успешно. Сообщения об ошибках API/trace в negative tests ожидаемы и не означают реальные внешние вызовы.

Frontend lint/build не нужны: frontend не изменён. Публичный HTTP analyze request сохранён, `JobState.options` допускает внутренние поля. Проверены существующие API/job/lead/deal/audio тесты полным набором. Реальные CRM/OpenAI pipelines не запускались; fixture не доказывает качество живых рекомендаций.

## M. Остаточные риски

- Старые исправления вне head/overlap, удаление сообщений и новый чат без иных сигналов видны не немедленно; помогает следующая полная сверка. Недоступное/удалённое до первого чтения восстановить невозможно.
- История сохраняется накопительно: отсутствие записи в ответе не считается удалением. Tombstones и отдельная retention policy не реализованы; SQLite/context могут расти. Это осознанный выбор против потери evidence.
- CRM источник с постоянной ошибкой остаётся stale, retries продолжаются. Доступность API/permissions и провайдерская задержка не исправляются локальной синхронизацией.
- На большой базе возрастут logical head probes и объём activity delta; live профиль ещё не измерен. Суточные initial/full jobs могут быть сгруппированы по времени; отдельного распределителя reconciliation нет.
- OS lock предназначен для процессов одного хоста и локального filesystem. Распределённая очередь/сетевой filesystem не проверены. Произвольные внешние readers JSON должны учитывать materialization window.
- Смена credential scope инвалидирует schema cache, но не переносит автоматически все рабочие deal snapshots между разными порталами; рабочая DB по-прежнему относится к одному настроенному CRM.
- Полнота и корректность реальных LLM-рекомендаций после изменения требуют разрешённого live прогона; offline тесты подтверждают маршрутизацию и сохранение fixture evidence.

## N. Следующий этап, не реализован

После отдельного разрешения — сопоставимый live A/B/C замер на тех же 10 сделках с отдельными collector/context/audio/LLM counters, затем сравнение evidence и рекомендаций. По результату — распределение reconciliation по времени, метрики возраста каждого cursor/stale source, оценка webhook/event-driven invalidation и безопасная политика объёма локального архива. Presence, VPS и prompts этим этапом не менялись.
