# Impeccable + Codex — шпаргалка для project-local установки

Используй команды **из корня нужного проекта** в PowerShell.

Официальный репозиторий: https://github.com/pbakaus/impeccable

---

## 1. Установить Impeccable только в текущий проект

```powershell
npx impeccable install --providers=codex --scope=project
```

Когда спросит:

```text
Install the design hook? (Y/n)
```

ответить:

```text
y
```

После установки перезапусти Codex, если skill не появился сразу.

В Codex проверь skill:

```text
/skills
```

или вызови:

```text
$impeccable
```

После установки/обновления открой в Codex:

```text
/hooks
```

и разреши hook Impeccable, если Codex попросит подтверждение.

---

## 2. Первичная настройка проекта

Запускается **в чате Codex**, не в PowerShell:

```text
/impeccable init
```

Если твоя версия Codex использует skill-вызовы через `$`, можно использовать:

```text
$impeccable init
```

`init` анализирует проект и создаёт/обновляет контекст Impeccable, например `PRODUCT.md`, `DESIGN.md` и связанные файлы.

---

## 3. Обновить Impeccable

Из корня проекта:

```powershell
npx impeccable update
```

После обновления снова проверь:

```text
/hooks
```

Codex может попросить повторно подтвердить hook, если его определение изменилось.

---

## 4. Проверить установку

PowerShell:

```powershell
Test-Path .agents\skills\impeccable
```

Ожидаемый результат:

```text
True
```

Посмотреть содержимое:

```powershell
Get-ChildItem .agents\skills\impeccable
```

Проверить изменения в проекте:

```powershell
git status
```

---

## 5. Проверить Impeccable без изменения UI

В Codex:

```text
/impeccable critique
```

или:

```text
$impeccable critique
```

Технический аудит:

```text
/impeccable audit
```

Финальная визуальная шлифовка:

```text
/impeccable polish
```

Не запускай `polish`, если сначала хочешь только получить список рекомендаций без изменений.

---

# Удаление Impeccable из одного проекта

Важно: в `.agents/skills/` могут находиться **другие skills**. Не удаляй всю `.agents`.

## Вариант A — аккуратно удалить skill и hook

### Шаг 1. Сначала удалить конфигурацию hook

Пока Impeccable ещё установлен, в чате Codex выполни:

```text
$impeccable hooks reset
```

Если в твоей версии используется slash-вызов:

```text
/impeccable hooks reset
```

`hooks reset` удаляет конфигурацию/кэш hook Impeccable и его записи из поддерживаемых hook-манифестов, не затрагивая посторонние hooks.

### Шаг 2. Закрыть Codex

После reset закрой текущую сессию Codex.

### Шаг 3. Удалить только skill Impeccable

В PowerShell из корня проекта:

```powershell
Remove-Item -Recurse -Force .agents\skills\impeccable
```

Не выполнять:

```powershell
Remove-Item -Recurse -Force .agents
```

если в `.agents` есть другие skills.

### Шаг 4. Проверить остатки

```powershell
git status
```

```powershell
Get-ChildItem -Force .agents
```

Если существует `.impeccable`, сначала посмотреть его содержимое:

```powershell
Get-ChildItem -Force .impeccable -Recurse
```

и проверить, есть ли tracked-файлы:

```powershell
git ls-files .impeccable
```

Если команда ничего не выводит и данные Impeccable больше не нужны, runtime-папку можно удалить:

```powershell
Remove-Item -Recurse -Force .impeccable
```

Если `git ls-files .impeccable` показывает файлы, **не удаляй папку целиком автоматически** — там могут быть сохранённые проектные артефакты Impeccable.

---

## Полностью убрать созданный Impeccable-контекст

После `/impeccable init` могли появиться проектные файлы вроде:

```text
PRODUCT.md
DESIGN.md
.impeccable\
```

Перед удалением сначала посмотреть:

```powershell
git status -- PRODUCT.md DESIGN.md .impeccable
```

Удаляй их только если уверен, что они были созданы Impeccable и больше не нужны проекту.

---

# Быстрые команды

## Установка

```powershell
npx impeccable install --providers=codex --scope=project
```

## Обновление

```powershell
npx impeccable update
```

## Инициализация в Codex

```text
/impeccable init
```

## Проверка hook

```text
/hooks
```

## Выключить hook временно

```text
$impeccable hooks off
```

## Включить hook обратно

```text
$impeccable hooks on
```

## Статус hook

```text
$impeccable hooks status
```

## Сбросить hook перед удалением

```text
$impeccable hooks reset
```

## Удалить только Impeccable skill

```powershell
Remove-Item -Recurse -Force .agents\skills\impeccable
```

---

# Рекомендуемый workflow для каждого нового проекта

```text
1. cd <папка проекта>
2. npx impeccable install --providers=codex --scope=project
3. открыть/перезапустить Codex
4. /hooks → подтвердить hook
5. /impeccable init
6. /impeccable critique
7. выбрать, что реально менять
8. /impeccable polish — только когда нужен проход с изменениями
```

Project-local установка удобна тем, что Impeccable действует только в выбранном репозитории и не засоряет остальные проекты.
