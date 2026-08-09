# Временный стенд: обновление и работа на VPS

Инструкция для временного защищённого стенда проекта на VPS. Она не меняет
cron-задачи, другие проекты в `/opt` или настройки сервера.

## Что где хранится

Код проекта находится в:

```text
/opt/Neuro_rop_practice
```

Локальные данные стенда находятся отдельно:

```text
/opt/Neuro_rop_practice/runtime/
```

В `runtime/` находятся `.env`, `reports/`, SQLite, локальная база знаний и
пароль доступа. Там же хранится `crm_pipeline_map.json` — справочник воронок и
этапов для UI. Эта папка не входит в Git и не должна удаляться при обновлении
кода.

## Автоматическое обновление из main

Workflow `.github/workflows/deploy-main.yml` запускается на каждый push в
`main`, включая merge pull request. Схема выполнения:

```text
Python unit tests + frontend lint/build
→ SSH на VPS
→ preflight checkout и runtime
→ fast-forward до проверенного commit
→ ./deploy/temporary-tunnel.sh
→ health-check и новая Cloudflare-ссылка
```

Deploy-job зависит от job с проверками. Если тест, lint или build завершился
ошибкой, SSH-подключение и обновление VPS не выполняются. Одновременные workflow
сериализуются через GitHub Actions concurrency, а на VPS дополнительно
используется `runtime/.deploy.lock`.

### GitHub Secrets

До первого push workflow в `main` добавь в настройках репозитория следующие
GitHub Secrets:

| Secret | Назначение |
| --- | --- |
| `VPS_HOST` | DNS-имя или IP VPS |
| `VPS_PORT` | SSH-порт; если оставить пустым, используется `22` |
| `VPS_USER` | Пользователь SSH, который владеет checkout и может запускать Docker |
| `VPS_SSH_PRIVATE_KEY` | Отдельный приватный SSH-ключ только для CI/CD |
| `VPS_KNOWN_HOSTS` | Заранее проверенная строка host key VPS |

Не добавляй в GitHub Secrets `runtime/.env`, OpenAI API key, Bitrix webhook,
пароль Basic Auth или CRM-данные: workflow их не использует. Они остаются на VPS.

Для CI/CD используй отдельную пару SSH-ключей, а не личный ключ разработчика.
Публичную часть добавь в `authorized_keys` пользователя деплоя. Строку
`VPS_KNOWN_HOSTS` получи по доверенному соединению и сверь fingerprint с VPS
или панелью провайдера до сохранения в GitHub. Workflow использует
`StrictHostKeyChecking=yes` и не принимает новый host key автоматически.
Для нестандартного SSH-порта known_hosts должен содержать запись вида
`[host]:port`, соответствующую значениям `VPS_HOST` и `VPS_PORT`.

Если GitHub-репозиторий приватный, самому checkout на VPS также нужен отдельный
read-only deploy key для `git fetch`. Приватный ключ GitHub Actions не должен
использоваться как Git-ключ сервера.

### Что проверяет deploy-job

Перед изменением checkout workflow требует:

- текущую ветку `main`;
- полностью чистое рабочее дерево;
- наличие `.git` и обязательных путей `runtime/`;
- отсутствие другого активного деплоя;
- присутствие проверенного GitHub Actions commit в `origin/main`;
- возможность только fast-forward обновления.

Workflow не использует `git reset`, `git checkout -- .` или `git clean`.
После запуска он проверяет API health, Nginx и состояние трёх контейнеров. Новая
Cloudflare-ссылка появляется в логе deploy step и GitHub Actions Summary.

## Ручное обновление стенда

Ручной путь сохраняется для первоначальной настройки и диагностики.

### 1. Отправить изменения с ноутбука в GitHub

В PowerShell на ноутбуке, из папки проекта:

```powershell
cd D:\My_dev_project\Neuro_rop_practice
git status --short
git add <нужные_файлы>
git commit -m "Краткое описание изменения"
git push origin main
```

Перед `git add` проверь список изменений. Не добавляй `.env`, `reports/`,
`runtime/`, аудио, транскрипты и SQLite.

### 2. Получить код на VPS

Подключись к серверу:

```bash
ssh root@147.45.166.60
cd /opt/Neuro_rop_practice
git status --short
```

Если команда не вывела ничего, рабочая копия чистая. Тогда обнови код:

```bash
git pull --ff-only origin main
```

Если `git status --short` показывает файлы, не выполняй `git reset`,
`git checkout -- .` или `git clean`. Сначала выясни, что это за изменения.
Строка `?? runtime/` на актуальной версии не должна появляться: папка
игнорируется целиком.

### 3. Пересобрать и перезапустить только стенд

Из той же папки на VPS выполни:

```bash
./deploy/temporary-tunnel.sh
```

Скрипт:

- пересобирает образы API и frontend;
- перезапускает только `neuro-rop-api`, `neuro-rop-web` и
  `neuro-rop-tunnel`;
- оставляет `runtime/`, отчёты, SQLite и `.env` на месте;
- проверяет API health, Nginx и состояние контейнеров;
- выводит новую HTTPS-ссылку Cloudflare.

Новая ссылка Cloudflare создаётся при каждом перезапуске. Логин — `rop`.
Пароль сохраняется прежним и находится только на VPS:

```bash
cat /opt/Neuro_rop_practice/runtime/access.txt
```

Не отправляй этот пароль в Git, чаты или скриншоты.

## Проверка после запуска

Проверь, что три контейнера запущены:

```bash
docker ps --filter name=neuro-rop
```

Затем открой ссылку, которую вывел скрипт, и войди под `rop`. Для быстрой
проверки достаточно открыть страницу и запустить один контролируемый анализ
из интерфейса.

## Если стенд не открылся

Сначала посмотри логи нужного контейнера:

```bash
docker logs --tail 100 neuro-rop-tunnel
docker logs --tail 100 neuro-rop-web
docker logs --tail 100 neuro-rop-api
```

Если новая ссылка не появилась в выводе скрипта, обычно её можно найти в:

```bash
docker logs neuro-rop-tunnel
```

Не перезапускай cron и не удаляй Docker-образы других проектов для исправления
этой проблемы.

Если ошибка появилась сразу после автоматического деплоя, сначала открой
GitHub Actions Summary и логи deploy-job. Для возврата к предыдущему коду создай
обычный `git revert` проблемного commit в `main`: новый commit снова пройдёт
проверки и будет развёрнут тем же workflow. Не откатывай checkout VPS через
`git reset --hard`; миграции SQLite и совместимость runtime требуют отдельной
проверки перед возвратом старой версии.

## Место на диске и старые Docker-образы

Каждый повторный запуск `./deploy/temporary-tunnel.sh` пересобирает образы.
После нескольких обновлений Docker может оставить неиспользуемые образы с
именем `<none>`. Они не нужны работающему стенду, но занимают место на диске.

Сначала только проверь состояние:

```bash
df -h /
docker system df
```

Если `docker system df` показывает большой объём `RECLAIMABLE` у образов,
сначала согласуй очистку. После подтверждения можно выполнить:

```bash
docker image prune -f
```

Команда удаляет только неиспользуемые висячие образы (`<none>`). Она не
перезапускает контейнеры и не удаляет `runtime/`, отчёты, SQLite или Docker
volumes. Но она действует на весь сервер, поэтому не выполняй её автоматически
на VPS с другими проектами.

Не используй `docker image prune -a` или `docker system prune -a`: они могут
удалить ещё нужные, но временно не запущенные образы других проектов.

## Остановить временный стенд

Если стенд больше не нужен, останови только его контейнеры и сеть:

```bash
docker rm --force neuro-rop-tunnel neuro-rop-web neuro-rop-api
docker network rm neuro-rop-practice-net
```

`runtime/` при этом остаётся на сервере. Чтобы снова открыть стенд, перейди в
`/opt/Neuro_rop_practice` и снова выполни `./deploy/temporary-tunnel.sh`.

## Чего не делать

Не выполняй для этого проекта следующие команды без отдельного плана и
резервной копии:

```bash
git clean -fdx
rm -rf runtime
docker system prune -a
```

Они могут удалить данные стенда или затронуть контейнеры и образы других
проектов на VPS.
