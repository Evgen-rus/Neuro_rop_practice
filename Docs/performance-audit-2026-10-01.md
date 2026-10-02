# Read-only performance-аудит Neuro ROP — 2026-10-01

## Итог и границы достоверности

Доказанный главный расход пользовательского read-path — загрузка поручений Deal Control: 471 поручение для 61 активной сделки, 2 356 SELECT, медиана storage-reader 668,048 мс. Повторный SELECT последнего `system_outcome` делает full scan; отдельные 472 одинаковых по форме SELECT заняли медианно 418,777 мс. Это первый кандидат на улучшение.

**Полная API latency и время появления данных в браузере не измерены.** Все авторизованные GET обновляют `auth_sessions.last_seen_at` (`api/app.py:587`, `api/auth.py:192`, `storage/rop_db.py:1822`, UPDATE на строке 1849). Их запуск противоречил бы прямому запрету production DB writes. `/api/reports/{id}` дополнительно вызывает `get_or_create_ui_report_share_token` (`api/app.py:2728`). Авторизованные API GET, логин и браузерную навигацию не запускали. Нельзя выдавать storage timing за latency API или утверждать, что весь экран уже быстрый.

Код, тесты, production configuration и DB не менялись. Исключённые контуры Bitrix sync/change gate/automatic cycle/FULL-MINI-skip/LLM/audio/jobs/scheduler не исследовались и не запускались. Прочитаны AGENTS, архитектурная карта, deployment README, production runbook и manual operations. Использована методика предыдущего LeadRecord-аудита, его старые числа не перенесены.

## Среда и методика

- Local и VPS checkout SHA: `c00c8cee6c6d21cb61cbdd0be324e40d41d7fb3b`; исходный local/VPS git status чистый.
- Production: host HTTPS → loopback 18081 → `neuro-rop-web` → `neuro-rop-api`; оба контейнера работали, uptime около 8 часов. Runtime вне Git. Рестартов и пересборок не было.
- Измерения выполнялись последовательно, по три одинаковых вызова, без нагрузки параллельными запросами.
- SQLite: отдельный Python `-B` через stdin существующего контейнера; `mode=ro`, `PRAGMA query_only=ON`, без `immutable` (DB живая, WAL должен учитываться). Приложение не импортировалось. Штатный `setup` заменён только в памяти объектом BASE_DIR/MSK_TZ, чтобы не создавать log directories. У storage только в памяти заменены `init_db` на no-op и `connect` на read-only connection. Никакого изменения штатного процесса API.
- Измерены настоящие функции `storage.rop_db`, а не переписанные SQL аналоги. SQLite timing — execute + fetch; total — reader с открытием соединения и Python-проекцией; residual — total минус SQLite, включая connection/инструментацию, поэтому это **не чистое CPU-время**.
- Счётчик строк означает возвращённые/materialized строки, не количество строк, просмотренных SQLite внутри scan. Объём чтения — UTF-8 длина строк/blob плюс условные 8 байт скалярного значения; это оценка полезных данных, не физический disk I/O.
- JSON size — компактная сериализация результата storage helper, **не размер HTTP API ответа**. Сериализация выполнялась после timing. Для статики размер — фактически полученные HTTP bytes.
- Live DB не замораживалась: за аудит число reports изменилось 990 → 992, tasks 924 → 926, trajectory events 72800 → 72805. Это работа уже существующего production, не аудита. Внутри отдельных серий результаты были стабильны; между сериями сравнение ориентировочное.
- Ни секреты, ни значения CRM/отчётов, ни private logs не выводились. Только counts, timing, SQL формы/планы, размер статики.

## Пользовательские сценарии и HTTP по исходникам

### Первоначальное открытие и Deal Control

`App.tsx:503–536`: сначала `/api/auth/me`, затем MainApp с default tab `deals` (`:729`). DealControl делает `/api/deal-control` (`DealControl.tsx:628–654,689`). Одновременно MainApp mount effect (`App.tsx:1041–1082`) загружает `/api/pipelines`, `/api/candidate-filters`, `/api/reports?limit=50`, `/api/analysis-profiles`, хотя текущий default экран — Deal Control.

Итого исходная цепочка содержит **6 API GET** после авторизованного открытия: auth + dashboard + четыре запроса MainApp; отдельно HTML/JS/CSS. Это source-derived count, не runtime waterfall, дополнительные provider/polling запросы не пересчитаны. Подтверждён ненужный для первого DealControl экранa eager read списка reports с дорогим storage payload. Доля этой работы в пользовательском ожидании пока неизвестна: запросы идут параллельно.

Dashboard `api/deal_control.py:1534–1665` читает все активные сделки, их поручения, последние отчёты, состояния и события дня; Python группирует/сортирует результаты и строит каждую карточку. List pagination отсутствует. Не предлагается механически добавлять LIMIT: UI использует полный портфель для фильтров и счётчиков.

Обычный выбор сделки использует уже загруженную строку, отдельный detail GET на каждый клик не найден. `/api/deal-control/deals/{id}` используется для обновления конкретной сделки (`DealControl.tsx:656–668`), заменяет одну строку, а не весь портфель. Карточка builder (`api/deal_control.py:1488`) читает одну deal row и её tasks, но `day_events` сначала получает события дня по всему контуру и лишь затем фильтрует конкретный deal. Эта дополнительная часть не замерена, поэтому кандидат по ней не заявлен.

### Daily Control

`DailyControl.tsx:225–266`: два последовательных GET — history, затем выбранный report. Focus/visibility и timer 60 секунд обновляют history (`:274–305`), report меняется только при условиях выбора нового отчёта. Это предусмотренный refresh, а не доказанный duplicate. Метаданные history отделены от snapshot; detail reader bounded к одному report.

`api/daily_control.py:1413` сначала читает metadata выбранного отчёта, затем его snapshot, review marks и history для навигации. Повторное чтение metadata существует, но выигрыш не измерен — оптимизацию не предлагаем.

### Готовый анализ и отчёты

DealControl review/coaching уже содержатся в dashboard. Markdown и analysis trace открываются лениво и кэшируются в компоненте (`DealControl.tsx:3369–3490`). Отдельный legacy reports list читает limit 50 (`api/app.py:2703`); storage предварительно читает `SELECT *`, парсит JSON, API потом удаляет тяжёлые поля. Detail report читает markdown и историю limit 20 (`api/app.py:2728`); фактический размер API ответа не измерен.

Production StrictMode сам по себе не доказывает двойные запросы. React render/re-render по исходникам не оценивался. Не найдено измеренного основания для React memoization/виртуализации.

## Baseline SQLite / Python

Все времена в мс; медианы по трём прогонам. Размеры decimal bytes.

| Реальный storage helper | Три total | Median total | Median SQLite | Median residual | SELECT | Возвращено строк | Прочитано байт | JSON storage-result |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| active deals | 5,379 / 5,677 / 4,674 | 5,379 | 1,905 | 3,124 | 1 | 61 | 133826 | 151793 |
| latest reports 61 active deals | 59,699 / 58,594 / 56,923 | 58,594 | 19,561 | 37,362 | 1 | 61 | 7245595 | 7201950 |
| reports list limit50 | 50,047 / 51,844 / 48,609 | 50,047 | 15,958 | 33,556 | 1 | 50 | 6275814 | 6240333 |
| daily history metadata | 323,056 / 9,734 / 9,315 | 9,734 | 8,671 | 1,185 | 1 | 71 | 16281 | 28865 |
| latest daily snapshot | 3,868 / 3,830 / 4,137 | 3,868 | 1,563 | 2,368 | 1 | 1 | 218779 | 212532 |
| one active deal | 1,669 / 1,299 / 1,205 | 1,299 | 0,976 | 0,323 | 1 | 1 | 827 | 1171 |
| latest ready ui_report | 2,786 / 1,767 / 1,833 | 1,833 | 1,015 | 0,752 | 1 | 1 | 58837 | 58304 |
| portfolio tasks | 845,945 / 668,048 / 558,763 | **668,048** | **579,129** | 89,636 | **2356** | **3738** | 1368768 | 1979482 |
| one selected deal tasks | 3,937 / 3,340 / 3,083 | 3,340 | 2,947 | 0,393 | 6 | 2 | 1502 | 2104 |

Медианы отдельных колонок независимы, не обязаны складываться. Первый daily-history прогон 323 мс оставлен в отчёте: причина не установлена, это не доказательство стабильного bottleneck. Нельзя суммировать независимые helper-medians и назвать результат endpoint latency. Доля N+1 доказана внутри tasks-reader, не внутри полного API.

### Декомпозиция tasks

`storage/rop_db.py:7956–8000`: 1 SELECT списка и по 5 SELECT на каждое поручение: baseline, outcome, CRM facts, guidance, system outcome. В серии 471 task → 1 + 5×471 = 2356 SELECT. Читается также история неактивных поручений.

На третьем прогоне: список 7,463 мс; baselines 6,462; outcomes 6,713; facts 28,836; guidance 7,332; system outcome **412,298 мс**. Последняя группа — около 88% измеренного SQLite-времени этого прогона. Повторный отдельный замер этой группы с актуальными 472 tasks: **410,559 / 418,777 / 435,202 мс**, 342 найденных результата.

Query:

```sql
SELECT payload_json FROM deal_control_task_events
WHERE task_id = ? AND event_type = 'system_outcome'
ORDER BY id DESC LIMIT 1;
```

Production plan: `SCAN deal_control_task_events`. Таблица 17527 строк; единственный перечисленный индекс `idx_deal_control_task_events_key` не используется этим планом. LIMIT ограничивает возвращённый результат, но не устраняет scan. Не утверждаем, что каждый scan просматривает все 17527 строк: поиск может закончиться раньше.

Другие планы: baseline использует integer PK; outcome — `idx_deal_control_task_outcomes_task`; facts — индекс по task + temp B-tree сортировки. Active deals: scan + temp B-tree, но таблица только 133 строки и SQL около 1,9 мс — это не кандидат на первоочередной индекс.

## Статика и compression

Без session cookie, Basic Auth только в памяти; три безопасных GET каждого ресурса с `Accept-Encoding: gzip`, тела discard.

| Ресурс | Три HTTP total, мс | Median | HTTP bytes |
|---|---|---:|---:|
| HTML / | 94,179 / 56,193 / 57,002 | 57,002 | 345 |
| JS index-BwS33Psn.js | 58,209 / 60,582 / 57,683 | 58,209 | 648808 |
| CSS index-CaQ6JlzB.css | 54,933 / 51,863 / 51,885 | 51,885 | 254434 |

GET выполнялись **с самого VPS через публичный HTTPS**; это не RTT пользовательского компьютера и не cold browser load. JS/CSS пришли целиком, Content-Encoding отсутствует. В effective container Nginx gzip закомментирован. Assets имеют `public,max-age=31536000,immutable`; повторное открытие с browser cache не измерено. Для JSON gzip фактически не измерен, поскольку API GET запрещён по write-effect.

## Кандидаты: baseline → минимальное изменение → guardrail

### 1. Первым: убрать repeated full scan последнего system outcome

- Baseline: 418,777 мс для 472 lookup; 2356 SELECT / 668,048 мс для portfolio tasks в предыдущей серии. Это намного больше измеренных остальных SQL-групп.
- Минимальный кандидат: индекс, подходящий существующему WHERE/ORDER BY `(task_id,event_type,id)` либо частичный `(task_id,id)` для `event_type='system_outcome'`. **Ничего не создавалось**. Выбор точной формы — после отдельного разрешения, проверки migration-порядка и before/after на копии; альтернативой является один bulk lookup последних outcomes для нужных task IDs.
- Почему первым: план подтверждает scan и измерение локализует основную стоимость. Индекс позволяет сохранить текущую семантику без изменения projection. Он не решит весь N+1.
- Риск: index build блокировки/нагрузка, размер БД и стоимость последующих записей; live rollout требует отдельного окна/разрешения. Bulk-альтернатива несёт риск неверного выбора MAX(id)/смешения задач.
- Before/after: одна и та же snapshot-копия и набор task IDs, три последовательных runs; планы, helper total/SQL, полное равенство recommendation_state/attention_priority/needs_follow_up. Потом отдельно авторизованный endpoint/browser замер, если разрешён session touch. Нельзя обещать выигрыш полного API на основании 418 мс.
- Guardrail после успешного изменения: fixture с несколькими event types/tasks и последним system outcome; проверка неизменности результата и использования SEARCH вместо SCAN для этого query (если выбран индекс), bounded performance benchmark на фиксированном объёме без нестабильного production threshold.

### 2. Вторым, только после повторного замера: N+1 и широкое чтение

- Остаточный N+1: пять query на task, все tasks включая исторические. Если после устранения scan он остаётся значимым — bulk-read baseline/outcome/facts/guidance/latest-system-outcome в существующем helper. Не удалять историю и не включать active_only вслепую: UI её потребляет.
- Wide reports: 58,594 мс / 7,25 МБ для portfolio reports; 50,047 мс / 6,28 МБ для legacy list. Минимальный кандидат для list — metadata SELECT без JSON/text, который API всё равно удаляет. Для dashboard нужно сначала установить используемые поля: full report требуется существующему coaching, нельзя просто убрать report_json.
- Eager legacy list: mount MainApp читает эти 50 reports даже на default DealControl. Минимальный кандидат — перенести загрузку legacy данных на первое открытие соответствующей вкладки. Это убирает доказанную работу, но улучшение critical path ещё не доказано.
- Риски: role filtering, order/limit, report metadata, отчётная история, работа при первом переключении tab. Before/after: одинаковая DB/роль/limit, reader timing и bytes, browser waterfall до данных; equality DTO и отсутствие eager GET на default tab. Guardrail: один сценарный check lazy-loading и contract-equivalence, не отдельная performance платформа.

### 3. Compression — измеренный объём, неизвестный UX выигрыш

- Baseline: JS/CSS 903242 bytes, gzip отсутствует даже с Accept-Encoding. Минимальный кандидат — штатный Nginx gzip для JS/CSS (JSON только после отдельного измерения).
- Риск: CPU и корректность cache/encoding headers. Before/after: фактический Content-Encoding/transfer bytes и cold browser load на одинаковой сети, три runs; тёплый cache отдельно. Guardrail: статический response/encoding/cache check. Не ставить впереди SQL bottleneck без замера пользовательской сети.

## Финальная таблица

| Сценарий | API | SQLite | Ответ | Главный расход | Кандидат |
|---|---|---|---|---|---|
| Первое открытие | 6 source-derived API GET; latency не измерена | dashboard tasks 2356 SELECT; eager reports storage 50,047 мс | HTTP HTML 345 B + JS/CSS 903242 B; API bytes неизвестны | repeated event scans; дополнительно eager legacy reads | outcome lookup первым; eager loading/gzip после before/after |
| Deal Control / список | GET /api/deal-control; HTTP не запускался | tasks 668,048 мс, SQL 579,129; latest reports 58,594 мс / 61 rows | endpoint размер неизвестен; tasks storage JSON 1,98 МБ | N+1, scan task events | подходящий индекс или bulk latest outcomes |
| Карточка сделки | обычный клик из dashboard: нового detail GET не найден; refetch GET /deals/{id} | one-deal row 1,299 мс + выбранные tasks 3,340 мс; полный builder не измерен | storage row 1171 B, tasks 2104 B; HTTP неизвестен | для выбранной сделки storage быстрый; day-events/projection неизвестны | пока не менять |
| Daily Control | history → detail, 2 GET; HTTP не запускался | history 9,734 мс /71 metadata rows; snapshot 3,868 мс /1 row | storage history 28865 B; snapshot 212532 B; HTTP неизвестен | нет устойчивого доказанного bottleneck; первый history outlier | пока не менять |
| Готовый анализ Deal Control | review встроен; Markdown/trace lazy GET | чтение одного ready report 1,833 мс /1 row; полная сборка/файлы не измерены | storage 58304 B, HTTP неизвестен | нет доказанного standalone bottleneck | пока не менять |
| Legacy список отчётов | GET /api/reports?limit=50, eager на mount | 1 SELECT /50 rows, 50,047 мс, читает 6,28 МБ | HTTP неизвестен, storage JSON 6,24 МБ удаляется/облегчается в API | wide SELECT/JSON decoding до drop полей | metadata reader и lazy mount после повторного замера |

## Пять кратких выводов

1. Главный доказанный bottleneck: повторный full scan system_outcome в tasks-reader Deal Control, median 418,777 мс отдельно от HTTP.
2. Первым оптимизировать именно этот lookup; реализация/индекс требуют отдельного разрешения и before/after.
3. Вторым после повторного замера — оставшийся N+1, затем wide/eager reports. Gzip оценивать по cold browser на пользовательской сети.
4. Уже быстрые измеренные **части**: one-deal row/tasks, один готовый report, latest daily snapshot и warm metadata history. Целые экраны быстрыми не объявляем и пока их не трогаем.
5. Не удалось безопасно измерить: авторизованный API latency/HTTP JSON bytes, полный route SQL count, auth overhead, полную Python projection, browser waterfall/time-to-data/React render и сетевой эффект gzip. Причина API — подтверждённые DB writes на GET, причина отказа от полного server import — write-capable schema/setup и риск запуска запрещённых контуров. Нужен отдельный разрешённый isolated snapshot benchmark либо разрешение штатного session touch; текущий запрет не обойдён.

Проверки: diff scope и `git diff --check`; тесты/lint/build не запускались, код не менялся и пользователь прямо запретил добавление тестов. Новый файл — только этот отчёт.

## Локальное улучшение — после аудита

По отдельному разрешению пользователя добавлен только локальный частичный индекс:

```sql
CREATE INDEX idx_deal_control_task_events_system_outcome
ON deal_control_task_events(task_id, id DESC)
WHERE event_type = 'system_outcome';
```

Та же декларация добавлена в штатный `storage/rop_db.py` init schema. Приложение и scheduler не запускались; локально выполнен только CREATE INDEX, без полного init/migrations. Production и push не выполнялись.

Локальная БД отличается по масштабу: 127 deals, 21 task, 387 task events; активный портфель даёт только 9 tasks. Поэтому production-выигрыш по этой базе не прогнозируем.

| Метрика | До, три прогона мс | После, три прогона мс | Медиана до → после |
|---|---|---|---|
| portfolio tasks reader | 27,583 / 14,233 / 17,528 | 5,292 / 12,901 / 11,546 | 17,528 → 11,546 |
| 9 system_outcome lookups | 10,855 / 11,197 / 11,313 | 10,195 / 9,835 / 10,130 | 11,197 → 10,130 |

План сменился с SCAN на SEARCH USING INDEX idx_deal_control_task_events_system_outcome. Сериализованные полные результаты reader во всех шести прогонах имеют одинаковый SHA-256 (без вывода содержимого). Построение индекса заняло 27,398 мс. Разброс большой относительно малого локального baseline; ускорение полного сайта не измерялось.

Перед изменением SQLite backup API сохранил консистентную копию в `reports/rop_assistant/rop_assistant.before-system-outcome-index-2026-10-01.sqlite` (приватная, не для Git). Откат без потери новых данных: удалить добавленные четыре строки schema, затем выполнить **только локально** `DROP INDEX IF EXISTS idx_deal_control_task_events_system_outcome`. Не восстанавливать всю старую БД поверх новой ради удаления индекса. Если оставить декларацию в коде, следующий init создаст индекс заново.

Targeted readpath: 6 tests OK. Новых тестов не добавлено по ранее заданному ограничению; runnable guardrail при замере проверил equality результатов и indexed query plan.

Полный suite: 934 tests за 261,918 с, 33 errors (`sqlite3.OperationalError: attempt to write a readonly database` при API import) и 1 failure timeout в `test_automatic_jobs_are_limited_to_two_workers_by_default`. Targeted readpath при этом прошёл. Повтор полного suite с расширенным filesystem доступом отклонён auto-review из-за риска внешних Bitrix/LLM/automatic-cycle side effects; повтор не запускался, обход не выполнялся. Полная regression-проверка не подтверждена, изменение нельзя считать полностью проверенным для deployment.

## Дополнительная проверка по разрешению пользователя

Проверка на копии production по решению пользователя пропущена. Разрешены повтор полного suite с доступом к локальным файлам и необходимые внешние вызовы; deployment не выполнялся.

На временной копии локального backup до индекса проверен настоящий `init_db`: первый init 33,744 мс, cached init 0,556 мс, init после сброса cache (имитация нового процесса) 13,577 мс. Индекс один, query plan использует его. API/lifespan/scheduler для этой проверки не запускались. Это локальные timings, не прогноз production.

Deployment code review: `api/app.py:217` выполняет init до создания app/lifespan. CREATE INDEX IF NOT EXISTS входит в штатный schema script, поэтому при старте с обновлённым кодом создаст индекс до health readiness; при повторном старте не пересоздаст существующий. Штатный deploy пересоздаёт API и WEB, health loop около 30 попыток по 1 секунде (каждый health timeout до 2 секунд). Если построение/другой init задержится, readiness может не пройти; длительность production без замера не подтверждена. Deployment script для health проверки не менялся.

Повтор полного suite с явно разрешённым расширенным локальным доступом завершился успешно: **1025 tests, 239,133 s, OK, exit 0**. DAYTIME_CYCLE_ENABLED=false задан для процесса проверки, штатный API/server не запускался. В этом запуске прежние readonly errors и timeout не повторились. Большее число тестов связано с успешным импортом ранее недоступных test modules. Таким образом, локальные contract/regression и startup-init проверки пройдены; production-copy measurement сознательно пропущен пользователем. Коммит/push/deployment не выполнялись.

## Проверка production после deployment пользователя

На VPS подтверждён SHA `95c45e667d84292321796019751700f49e91ae58`, checkout чистый, API/WEB Up (7–9 минут на момент проверки). Production не перезапускался и не изменялся этой проверкой. Штатный API health из контейнера и через HTTPS: 200.

Индекс `idx_deal_control_task_events_system_outcome` существует; EXPLAIN использует `SEARCH ... USING INDEX ... (task_id=?)`.

Измерения тем же изолированным read-only storage методом (mode=ro, query_only, без api.app/import/init, три последовательных runs):

| Метрика | До, median мс | После, три runs мс | После, median мс |
|---|---:|---|---:|
| Portfolio tasks reader | 668,048 | 124,661 / 135,502 / 124,874 | 124,874 |
| SQLite внутри reader | 579,129 | 58,836 / 62,622 / 55,672 | 58,836 |
| Отдельные system_outcome lookups | 418,777 | 4,076 / 2,787 / 2,570 | 2,787 |

Reader быстрее примерно в 5,35 раза (−81,3% времени), конкретная lookup-группа — примерно в 150 раз (−99,3%). Это эффект измеренной storage части, не доказанная latency всего сайта. Между before/after данные живые и немного изменились: reader after 473 tasks, 2366 SELECT, 3751 materialized rows, 1374676 read bytes, storage-result JSON 1987396 bytes. Events 17534 против 17527 before; tasks во время baseline 471–472. N+1 остался, но scan bottleneck устранён. Третья серия query groups: system outcome 4,455 мс, CRM facts 26,143 мс; следующую оптимизацию в рамках проверки не делали.

Дополнительно в одной read transaction сравнили результаты существующего запроса с `NOT INDEXED` по всем актуальным 474 tasks: полное равенство payload результата, 344 найденных результата. Значения не выводились. После проверки rollback read transaction, записей в DB не выполняли.

HTTPS /api/health: 131,950 / 79,752 / 60,475 мс, median 79,752 мс, 200, response 582 bytes. Содержимое health не выводилось. Это запросы с VPS через публичный HTTPS, не user browser network.

Авторизованные бизнес-API и browser waterfall в этой проверке не измерены: доступна Basic Auth, но нет доступной application session. Никакие auth credentials/token digest не извлекались, login/session не создавались. Несмотря на разрешение пользователя server testing, авторизованные GET фактически не выполнялись; DB writes не было. Для полной API latency нужна пользовательская application session. Не подменять её health latency.

Локально обновлён только отчёт; `git diff --check` прошёл. Production checkout после проверки чистый, контейнеры продолжали работать.

## Production gzip acceptance — 2026-10-02

Deployment SHA `50584fd18645d0480c54e5fc59f8df092a102a36`, GitHub workflow 36946993479 success; API/WEB Up. Проверка только HTTP GET, без изменения production.

| Asset | identity bytes | gzip bytes | Экономия |
|---|---:|---:|---:|
| index-DeRIVvJl.js | 648850 | 176151 | 72,9% |
| index-CaQ6JlzB.css | 254434 | 49496 | 80,5% |
| Всего | 903284 | 225647 | 75,0% |

По три последовательных identity/gzip пары для каждого файла: все 200, `Content-Encoding: gzip`, распакованные bytes полностью совпадают с identity. MIME корректны, `Vary: Accept-Encoding`, immutable cache и security headers сохранены. Без Basic Auth HTML и assets возвращают 401; приватный API без application session также 401. API health 200, gzip на API не включился.

С VPS через публичный HTTPS median JS identity/gzip 9,803/29,318 мс, CSS 8,793/14,013 мс. На серверной быстрой сети gzip добавляет расходы сжатия; эти цифры не свидетельствуют об ускорении браузера. Подтверждена экономия transfer bytes, пользовательский waterfall/render с application session не измерялся. Нельзя считать этой проверкой все сценарии интерфейса проверенными.
