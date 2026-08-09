# Простой CI/CD из GitHub Actions на один VPS

Эта инструкция помогает настроить в другом проекте понятную схему:

```text
push или merge в main
→ GitHub Actions запускает тесты
→ при успехе подключается к VPS по SSH
→ обновляет код до проверенного commit
→ пересобирает и перезапускает Docker-контейнеры
→ проверяет, что приложение запустилось
```

Инструкция рассчитана на один репозиторий и один VPS. Kubernetes, Jenkins,
отдельный Docker Registry и дополнительные серверы не нужны.

В примерах замени значения в угловых скобках:

- `<OWNER>` — владелец репозитория на GitHub;
- `<REPOSITORY>` — имя репозитория;
- `<PROJECT>` — короткое имя проекта без пробелов;
- `<VPS_HOST>` — IP-адрес или DNS-имя VPS;
- `<VPS_PORT>` — SSH-порт, обычно `22`;
- `<VPS_USER>` — пользователь VPS, выполняющий деплой;
- `/opt/<PROJECT>` — папка проекта на VPS.

Не копируй в эту инструкцию реальные ключи, пароли или содержимое `.env`.

## 1. Что должно быть готово заранее

На компьютере разработчика нужны:

- репозиторий на GitHub с веткой `main`;
- Git;
- OpenSSH (`ssh`, `ssh-keygen`, `ssh-keyscan`);
- право добавлять GitHub Actions Secrets в репозиторий.

На VPS нужны:

- Linux;
- Git;
- Docker;
- Docker Compose plugin, если выбран вариант с `docker compose`;
- пользователь, который может работать с папкой проекта и запускать Docker;
- открытый SSH-порт.

Проверка на VPS:

```bash
git --version
docker --version
docker compose version
```

Для самого простого стенда можно выполнять деплой от `root`, но безопаснее
создать отдельного пользователя `deploy`, дать ему права на папку проекта и
разрешить запуск Docker. Важно понимать: доступ к Docker фактически даёт
широкие права на сервере, поэтому CI-ключ надо защищать так же внимательно,
как административный ключ.

## 2. Подготовить проект к автоматическому деплою

### 2.1. Отделить код от runtime-данных

Код обновляется через Git. Данные, которые должны пережить обновление, хранятся
на VPS вне Git и не должны попадать в Docker-образ:

```text
/opt/<PROJECT>/runtime/.env
/opt/<PROJECT>/runtime/data/
/opt/<PROJECT>/runtime/uploads/
/opt/<PROJECT>/runtime/reports/
```

Нужны только те папки, которыми реально пользуется конкретный проект. Например,
если приложение не создаёт загрузки и отчёты, `uploads/` и `reports/` не нужны.

Добавь runtime-пути в `.gitignore`, например:

```gitignore
runtime/
.env
*.sqlite3
```

Добавь их и в `.dockerignore`, чтобы секреты и пользовательские данные случайно
не попали в build context:

```dockerignore
.git
.github
.env
runtime
reports
```

На VPS создай нужные пути вручную:

```bash
sudo mkdir -p /opt/<PROJECT>/runtime/data
sudo mkdir -p /opt/<PROJECT>/runtime/uploads
sudo touch /opt/<PROJECT>/runtime/.env
sudo chmod 600 /opt/<PROJECT>/runtime/.env
sudo chown -R <VPS_USER>:<VPS_USER> /opt/<PROJECT>
```

Заполни `/opt/<PROJECT>/runtime/.env` непосредственно на VPS. Не вставляй его
содержимое в GitHub Actions, Git, issue, чат или скриншот.

### 2.2. Подготовить команду деплоя

Выбери один из двух вариантов ниже.

#### Вариант A — существующий `deploy.sh` (основной)

Если проект уже запускается собственным скриптом, сохрани этот путь. Workflow
должен только обновить код и вызвать версионируемый скрипт:

```bash
./deploy/deploy.sh
```

Минимальные требования к безопасному `deploy.sh`:

- первая строка `#!/usr/bin/env bash`;
- включён строгий режим `set -Eeuo pipefail`;
- проверено наличие обязательного `.env` и runtime-папок;
- runtime-пути монтируются в контейнеры, а не копируются в образ;
- перезапускаются только контейнеры этого проекта;
- после запуска выполняется health-check;
- ненулевой exit code возвращается при любой ошибке;
- скрипт не выполняет `git reset --hard`, `git clean`, удаление `runtime/` или
  глобальную очистку Docker.

Упрощённый каркас для проекта с отдельными Dockerfile:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/opt/<PROJECT>}"
RUNTIME_DIR="${PROJECT_ROOT}/runtime"

[[ -f "${RUNTIME_DIR}/.env" ]] || {
    echo "Не найден ${RUNTIME_DIR}/.env" >&2
    exit 1
}

chmod 600 "${RUNTIME_DIR}/.env"

docker build -t <PROJECT>-api:current -f "${PROJECT_ROOT}/Dockerfile.api" "${PROJECT_ROOT}"
docker build -t <PROJECT>-web:current -f "${PROJECT_ROOT}/Dockerfile.web" "${PROJECT_ROOT}"

# Здесь находятся только команды остановки и запуска контейнеров этого проекта.
# Обязательно монтируй runtime-папки с VPS в новые контейнеры.

# Пример обязательной проверки после запуска:
curl --fail --silent --show-error --retry 10 --retry-delay 2 \
    http://127.0.0.1:<HEALTH_PORT>/health >/dev/null
```

Команды `docker run` зависят от портов, сетей и volume конкретного приложения,
поэтому их нельзя бездумно копировать между проектами. Сначала добейся, чтобы
`./deploy/deploy.sh` надёжно работал при ручном запуске на VPS.

#### Вариант B — Docker Compose

Если контейнеры описаны в `compose.yaml`, простой deploy-скрипт может состоять
из проверки runtime и одной команды Compose:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/opt/<PROJECT>}"
cd "${PROJECT_ROOT}"

[[ -f runtime/.env ]] || {
    echo "Не найден runtime/.env" >&2
    exit 1
}

chmod 600 runtime/.env
docker compose up -d --build --remove-orphans

curl --fail --silent --show-error --retry 10 --retry-delay 2 \
    http://127.0.0.1:<HEALTH_PORT>/health >/dev/null
```

Пример принципа хранения данных в `compose.yaml`:

```yaml
services:
  api:
    build:
      context: .
      dockerfile: Dockerfile.api
    env_file:
      - ./runtime/.env
    volumes:
      - ./runtime/data:/app/data
      - ./runtime/uploads:/app/uploads
    restart: unless-stopped

  web:
    build:
      context: .
      dockerfile: Dockerfile.web
    depends_on:
      - api
    ports:
      - "127.0.0.1:<WEB_PORT>:80"
    restart: unless-stopped
```

`compose.yaml` и Dockerfile хранятся в Git. `runtime/.env`, база данных,
загрузки и другие изменяемые данные остаются на VPS.

Даже при использовании Compose лучше вызывать из workflow именно
`./deploy/deploy.sh`. Тогда проверки, команда запуска и health-check находятся
в одном понятном месте и одинаковы для ручного и автоматического деплоя.

## 3. Подготовить checkout проекта на VPS

Если репозиторий публичный:

```bash
cd /opt
git clone https://github.com/<OWNER>/<REPOSITORY>.git <PROJECT>
cd /opt/<PROJECT>
git switch main
```

Если папка уже существует, не клонируй её заново. Проверь текущее состояние:

```bash
cd /opt/<PROJECT>
git branch --show-current
git status --short
git remote -v
```

Должна быть ветка `main`, а `git status --short` не должен выводить
отслеживаемые изменения. Runtime-папка не должна показываться, если она
правильно добавлена в `.gitignore`.

### Если репозиторий приватный

Здесь нужен отдельный read-only ключ направления **VPS → GitHub**. Это не тот
ключ, которым GitHub Actions входит на VPS.

На VPS от имени пользователя деплоя:

```bash
ssh-keygen -t ed25519 \
    -f ~/.ssh/<PROJECT>_github_readonly \
    -C "<PROJECT>-vps-readonly"
chmod 600 ~/.ssh/<PROJECT>_github_readonly
cat ~/.ssh/<PROJECT>_github_readonly.pub
```

Публичную часть добавь в GitHub:

```text
Repository → Settings → Deploy keys → Add deploy key
```

Не включай `Allow write access`. Затем на VPS привяжи ключ к этому checkout:

```bash
cd /opt/<PROJECT>
git remote set-url origin git@github.com:<OWNER>/<REPOSITORY>.git
git config core.sshCommand \
    "ssh -i ~/.ssh/<PROJECT>_github_readonly -o IdentitiesOnly=yes"
git fetch origin main
```

При первом соединении с GitHub проверь предлагаемый fingerprint по официальной
документации GitHub. Не подтверждай неизвестный host key вслепую.

## 4. Создать отдельный ключ GitHub Actions → VPS

Этот ключ нужен только для автоматического входа с GitHub Actions на VPS. Не
используй личный SSH-ключ.

В PowerShell на своём компьютере:

```powershell
$ciKey = "$env:USERPROFILE\.ssh\<PROJECT>_github_actions"
ssh-keygen -t ed25519 -f $ciKey -C "<PROJECT>-github-actions"
```

Для полностью автоматического workflow passphrase обычно оставляют пустой.
Поэтому особенно важно использовать отдельный ключ только для одного проекта.

Появятся два файла:

```text
<PROJECT>_github_actions       ← приватный, никому не отправлять
<PROJECT>_github_actions.pub   ← публичный, добавить на VPS
```

Покажи публичную часть:

```powershell
Get-Content "$ciKey.pub"
```

Подключись к VPS личным ключом и добавь эту одну строку в файл
`~/.ssh/authorized_keys` пользователя деплоя. Для современного OpenSSH можно
запретить ненужные возможности ключа префиксом `restrict`:

```text
restrict ssh-ed25519 AAAA... <PROJECT>-github-actions
```

Затем проверь права на VPS:

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
```

Не удаляй существующие строки из `authorized_keys`: они могут принадлежать
личному доступу или другим системам.

Проверь новый ключ с компьютера:

```powershell
ssh -i $ciKey -p <VPS_PORT> -o IdentitiesOnly=yes <VPS_USER>@<VPS_HOST> `
    "echo CI_SSH_OK"
```

Ожидаемый ответ: `CI_SSH_OK`.

## 5. Надёжно получить host key VPS

`known_hosts` не даёт GitHub Actions подключиться к подменённому серверу.
Недостаточно просто выполнить `ssh-keyscan`: fingerprint надо сравнить по
доверенному каналу.

На VPS через уже доверенное соединение узнай fingerprint серверного ключа:

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

На своём компьютере в PowerShell получи публичный host key:

```powershell
ssh-keyscan -p <VPS_PORT> <VPS_HOST> 2>$null | Set-Content -Encoding ascii .\vps_known_hosts
ssh-keygen -lf .\vps_known_hosts
```

Сравни fingerprint с выводом на VPS или с данными панели хостинг-провайдера.
Только после совпадения используй содержимое файла как secret:

```powershell
Get-Content -Raw .\vps_known_hosts
```

После добавления secret локальный временный файл можно удалить. Не публикуй его
в репозитории. Host key не является приватным ключом, но его корректность важна
для защиты SSH-соединения.

## 6. Добавить GitHub Actions Secrets

Открой:

```text
GitHub → нужный репозиторий → Settings
→ Secrets and variables → Actions → New repository secret
```

Создай пять secrets:

| Имя | Что сохранить |
| --- | --- |
| `VPS_HOST` | IP или DNS-имя VPS |
| `VPS_PORT` | SSH-порт, например `22` |
| `VPS_USER` | пользователь деплоя |
| `VPS_SSH_PRIVATE_KEY` | полный текст приватного CI-ключа |
| `VPS_KNOWN_HOSTS` | проверенная строка из `vps_known_hosts` |

Полный текст приватного ключа в PowerShell можно получить так:

```powershell
Get-Content -Raw $ciKey
```

Он начинается строкой `-----BEGIN OPENSSH PRIVATE KEY-----` и заканчивается
`-----END OPENSSH PRIVATE KEY-----`. Скопируй весь блок вместе с первой и
последней строкой только в значение GitHub Secret.

В эти secrets не надо добавлять `.env`, пароли приложения, API-ключи, базу
данных или пользовательские файлы. Они нужны приложению на VPS, а не runner'у
GitHub Actions.

## 7. Добавить workflow

Создай в репозитории файл:

```text
.github/workflows/deploy-main.yml
```

Ниже шаблон для Python-проекта с frontend на Node.js. Удали ненужные проверки
или замени команды на реальные команды тестирования своего проекта. Нельзя
оставлять фиктивную проверку, которая всегда завершается успешно.

```yaml
name: Test and deploy main

on:
  push:
    branches:
      - main

permissions:
  contents: read

concurrency:
  group: <PROJECT>-production-main
  cancel-in-progress: false

jobs:
  checks:
    name: Tests and checks
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - name: Check out repository
        uses: actions/checkout@v6
        with:
          persist-credentials: false

      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: requirements.txt

      - name: Install Python dependencies
        run: python -m pip install -r requirements.txt

      - name: Run Python tests
        run: python -m unittest discover -s tests

      - name: Set up Node.js
        uses: actions/setup-node@v6
        with:
          node-version: "22"
          cache: npm
          cache-dependency-path: frontend/package-lock.json

      - name: Install frontend dependencies
        working-directory: frontend
        run: npm ci

      - name: Lint frontend
        working-directory: frontend
        run: npm run lint

      - name: Build frontend
        working-directory: frontend
        run: npm run build

  deploy:
    name: Deploy tested commit to VPS
    needs: checks
    runs-on: ubuntu-latest
    timeout-minutes: 30
    env:
      VPS_HOST: ${{ secrets.VPS_HOST }}
      VPS_PORT: ${{ secrets.VPS_PORT }}
      VPS_USER: ${{ secrets.VPS_USER }}
      VPS_SSH_PRIVATE_KEY: ${{ secrets.VPS_SSH_PRIVATE_KEY }}
      VPS_KNOWN_HOSTS: ${{ secrets.VPS_KNOWN_HOSTS }}
      VPS_DEPLOY_PATH: /opt/<PROJECT>

    steps:
      - name: Configure SSH
        shell: bash
        run: |
          set -Eeuo pipefail
          umask 077
          : "${VPS_HOST:?VPS_HOST secret is required}"
          : "${VPS_USER:?VPS_USER secret is required}"
          : "${VPS_SSH_PRIVATE_KEY:?VPS_SSH_PRIVATE_KEY secret is required}"
          : "${VPS_KNOWN_HOSTS:?VPS_KNOWN_HOSTS secret is required}"

          install -d -m 700 "${HOME}/.ssh"
          printf '%s\n' "${VPS_SSH_PRIVATE_KEY}" > "${HOME}/.ssh/deploy_key"
          printf '%s\n' "${VPS_KNOWN_HOSTS}" > "${HOME}/.ssh/known_hosts"
          chmod 600 "${HOME}/.ssh/deploy_key" "${HOME}/.ssh/known_hosts"

      - name: Fast-forward and deploy tested commit
        shell: bash
        run: |
          set -Eeuo pipefail
          ssh_port="${VPS_PORT:-22}"
          ssh_options=(
            -i "${HOME}/.ssh/deploy_key"
            -p "${ssh_port}"
            -o BatchMode=yes
            -o ConnectTimeout=15
            -o IdentitiesOnly=yes
            -o StrictHostKeyChecking=yes
            -o UserKnownHostsFile="${HOME}/.ssh/known_hosts"
          )

          ssh "${ssh_options[@]}" -- "${VPS_USER}@${VPS_HOST}" \
            bash -s -- "${GITHUB_SHA}" "${VPS_DEPLOY_PATH}" <<'REMOTE'
          set -Eeuo pipefail

          deploy_sha="$1"
          project_root="$2"

          fail() {
            echo "Deploy stopped: $*" >&2
            exit 1
          }

          [[ "${deploy_sha}" =~ ^[0-9a-f]{40}$ ]] || fail "invalid commit SHA"
          [[ "${project_root}" == /* ]] || fail "project path must be absolute"
          [[ -d "${project_root}/.git" ]] || fail "Git checkout not found"

          cd "${project_root}"
          [[ "$(git branch --show-current)" == "main" ]] \
            || fail "VPS checkout is not on main"
          [[ -z "$(git status --porcelain)" ]] \
            || fail "VPS checkout has uncommitted changes"
          [[ -f runtime/.env ]] || fail "runtime/.env is missing"

          exec 9>runtime/.deploy.lock
          flock -n 9 || fail "another deployment is already running"

          git fetch --prune origin main
          git merge-base --is-ancestor "${deploy_sha}" origin/main \
            || fail "tested commit is not in origin/main"

          current_sha="$(git rev-parse HEAD)"
          git merge-base --is-ancestor "${current_sha}" "${deploy_sha}" \
            || fail "VPS checkout cannot be fast-forwarded"

          git merge --ff-only "${deploy_sha}"
          [[ "$(git rev-parse HEAD)" == "${deploy_sha}" ]] \
            || fail "VPS HEAD does not match tested commit"

          ./deploy/deploy.sh
          REMOTE

      - name: Remove SSH material
        if: always()
        shell: bash
        run: rm -f "${HOME}/.ssh/deploy_key" "${HOME}/.ssh/known_hosts"
```

Для варианта Docker Compose последнюю удалённую команду всё равно лучше оставить
как `./deploy/deploy.sh`, а внутри скрипта вызвать:

```bash
docker compose up -d --build --remove-orphans
```

Если в проекте нет Python или frontend, удали соответствующие setup/install/test
steps. Но job `deploy` обязательно должен содержать `needs: checks`: именно это
не позволяет деплоить код после проваленных проверок.

Не копируй версии Python и Node.js вслепую. Они должны совпадать с версиями,
которые поддерживает проект и использует Dockerfile.

## 8. Проверить всё до первого push

Сначала вручную на VPS:

```bash
cd /opt/<PROJECT>
git status --short
./deploy/deploy.sh
docker ps
curl --fail http://127.0.0.1:<HEALTH_PORT>/health
```

Затем локально проверь те же тесты, которые будут в workflow. Например:

```powershell
python -m unittest discover -s tests
cd frontend
npm ci
npm run lint
npm run build
```

Вернись в корень проекта и проверь изменения:

```powershell
git status --short
git diff --check
```

До первого push обязательно должны существовать все пять GitHub Secrets. Push
workflow в `main` запускает CI/CD сразу.

## 9. Первый запуск

Создай commit и отправь его в `main` обычным способом:

```powershell
git add .github/workflows/deploy-main.yml deploy/deploy.sh
git commit -m "Add GitHub Actions VPS deployment"
git push origin main
```

Открой вкладку `Actions` репозитория. Сначала должен успешно закончиться job
`Tests and checks`, затем — `Deploy tested commit to VPS`.

После успеха проверь:

```bash
cd /opt/<PROJECT>
git rev-parse HEAD
docker ps
docker logs --tail 100 <PROJECT>-api
```

Открой приложение и выполни короткий smoke test: загрузка главной страницы,
health endpoint и одна безопасная основная операция без изменения production-
данных.

## 10. Как теперь выпускать изменения

Обычный рабочий процесс:

1. Создать отдельную ветку.
2. Внести изменение и локально запустить проверки.
3. Открыть pull request в `main`.
4. Дождаться зелёных проверок.
5. Выполнить merge.
6. GitHub Actions ещё раз проверит commit из `main` и только затем развернёт его.

Прямой push в `main` также запустит workflow. Для командной работы полезно
включить branch protection и требовать успешный job `checks` до merge.

## 11. Если что-то не работает

### Проверки красные

Деплой не должен запускаться. Открой упавший step в GitHub Actions, исправь
тест, lint или build и отправь новый commit. Не отключай проверку только ради
зелёного workflow.

### Ошибка `Permission denied (publickey)`

Проверь:

- приватная часть ключа полностью записана в `VPS_SSH_PRIVATE_KEY`;
- соответствующая публичная часть есть в `authorized_keys` нужного пользователя;
- `VPS_USER`, `VPS_PORT` и `VPS_HOST` верны;
- права `~/.ssh` — `700`, `authorized_keys` — `600`;
- ключ не был случайно скопирован одной строкой без переносов.

### Ошибка проверки host key

Повтори получение `VPS_KNOWN_HOSTS` и снова сравни fingerprint по доверенному
каналу. Не отключай `StrictHostKeyChecking`.

### `git fetch` на VPS не имеет доступа

Для приватного репозитория проверь отдельный read-only deploy key направления
VPS → GitHub и команду:

```bash
cd /opt/<PROJECT>
git fetch origin main
```

### Workflow сообщает о грязном checkout

На VPS выполни только диагностику:

```bash
cd /opt/<PROJECT>
git status --short
git diff
```

Не применяй автоматически `git reset --hard` или `git clean`. Сначала выясни,
кому принадлежат изменения и не являются ли они runtime-данными, которые
ошибочно хранятся внутри Git checkout.

### Контейнер запустился, но приложение не работает

Проверь status и логи только этого проекта:

```bash
docker ps -a
docker logs --tail 100 <CONTAINER_NAME>
```

Health-check должен находиться внутри `deploy.sh` и завершать его с ошибкой,
если новая версия не готова принимать запросы.

## 12. Откат

Простой Git-ориентированный откат — создать новый commit, отменяющий проблемное
изменение:

```powershell
git revert <BAD_COMMIT_SHA>
git push origin main
```

Revert снова пройдёт тесты и обычный deploy. Не выполняй на VPS
`git reset --hard` без отдельного плана.

Важно: эта минимальная схема не гарантирует автоматический rollback. Если
deploy-скрипт сначала удаляет старые контейнеры, а затем новая версия не
проходит health-check, сервис может оставаться недоступным до исправления или
ручного запуска предыдущей версии. Для важного production-проекта следующий
разумный шаг — сохранять предыдущий тег образа и возвращать его при неуспешном
health-check, не добавляя новую инфраструктуру.

Перед откатом версии, которая меняла схему базы данных, сначала проверь
совместимость и наличие резервной копии. Git revert не откатывает данные.

## 13. Риски простой схемы

- Во время пересборки и перезапуска возможен короткий простой.
- Ошибка после остановки старого контейнера может потребовать ручного отката.
- Миграции базы данных могут быть необратимыми.
- Docker build на слабом VPS расходует CPU, RAM и место на диске.
- Старые неиспользуемые образы постепенно занимают диск.
- Компрометация CI SSH-ключа даёт права пользователя деплоя на VPS.
- Пользователь с доступом к Docker фактически имеет широкие права на сервере.
- Ручные изменения кода на VPS блокируют безопасный fast-forward deploy.
- Одновременные деплои опасны без `concurrency` в Actions и `flock` на VPS.
- Проверка `/health` подтверждает запуск, но не заменяет smoke test бизнес-функций.

Не запускай автоматически `docker system prune -a`: на одном VPS могут быть
образы и контейнеры других проектов. Сначала вручную проверь `docker system df`.

## 14. Минимальный контрольный список безопасности

- [ ] Runtime-данные и `.env` исключены из Git и Docker build context.
- [ ] На VPS есть резервная копия важных данных.
- [ ] `deploy.sh` вручную проверен на VPS.
- [ ] Есть настоящий health-check с ненулевым exit code при ошибке.
- [ ] Для GitHub Actions создан отдельный SSH-ключ.
- [ ] Публичный CI-ключ добавлен только нужному пользователю VPS.
- [ ] `VPS_KNOWN_HOSTS` получен со сверкой fingerprint.
- [ ] Все пять GitHub Secrets добавлены до первого push workflow.
- [ ] Для приватного репозитория VPS имеет отдельный read-only deploy key GitHub.
- [ ] Workflow запускает минимально необходимые тесты, lint и build.
- [ ] Deploy-job содержит `needs: checks`.
- [ ] На VPS разрешён только fast-forward проверенного commit.
- [ ] Одновременные деплои блокируются через `concurrency` и `flock`.
- [ ] Известен ручной способ просмотра логов и отката через `git revert`.
- [ ] Первый автоматический деплой проверен в GitHub Actions и в приложении.

После выполнения этого списка проект получает минимальный понятный CI/CD для
одного VPS без отдельного registry и без изменения общей архитектуры деплоя.
