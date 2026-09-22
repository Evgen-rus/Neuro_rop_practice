# Ручное обновление и перезапуск production

Эта инструкция относится только к production-проекту:

```text
/opt/Neuro_rop_practice
https://neurorop.leadrecordwh.ru
```

Она не относится к `/opt/Neuro_rop_demo`, контейнерам `neurorop-demo-*`, порту
`18080` и сети `neurorop-demo-net`.

## Что делает штатный deploy

Команда:

```bash
./deploy/deploy-production.sh
```

пересобирает образы и пересоздаёт только:

- `neuro-rop-api`;
- `neuro-rop-web`.

Runtime, SQLite и reports остаются в `/opt/Neuro_rop_practice/runtime/`. API не
публикуется наружу, WEB остаётся доступен только на `127.0.0.1:18081`. Host
Nginx и Certbot штатный deploy не меняет.

## Рекомендуемый способ обновления

Обычное обновление выполняется push в `main`: GitHub Actions запускает tests,
frontend lint/build и затем автоматически разворачивает проверенный commit.

Ручной deploy нужен для диагностики или повторного запуска уже находящегося на
VPS кода.

## Перед любыми действиями

Подключись к VPS и перейди в проект:

```bash
ssh root@147.45.166.60
cd /opt/Neuro_rop_practice
```

Проверь checkout, контейнеры, порт и runtime:

```bash
git branch --show-current
git status --short
git rev-parse HEAD
docker ps -a --filter name=neuro-rop --filter name=neurorop-demo
ss -lntp | grep -E ':(80|443|18080|18081)\b'
test -f runtime/.env
test -d runtime/reports
test -d runtime/logs
test -d runtime/knowledge
test -f runtime/crm_pipeline_map.json
test -s runtime/access.txt
test -s runtime/reports/rop_assistant/rop_assistant.sqlite
```

Продолжай только если текущая ветка — `main`, а `git status --short` ничего не
вывел. Если есть изменения, сначала выясни их происхождение. Не используй
`git reset --hard`, `git clean` или `git checkout -- .`.

## Обновить код вручную

Получить только fast-forward обновление из `main`:

```bash
git fetch --prune origin main
git merge --ff-only origin/main
```

Убедись, что checkout остался чистым:

```bash
git status --short
git rev-parse HEAD
```

## Пересобрать и пересоздать production-контейнеры

```bash
./deploy/deploy-production.sh
```

Скрипт сам проверяет обязательные runtime-пути, API health, конфигурацию Nginx
в WEB-контейнере, localhost-порт и состояние обоих контейнеров. При ошибке он
завершается с ненулевым кодом.

После успешного запуска проверь:

```bash
docker ps --filter name=neuro-rop-api --filter name=neuro-rop-web
docker inspect --format '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}} ports={{json .HostConfig.PortBindings}}' neuro-rop-api neuro-rop-web
curl -I http://127.0.0.1:18081/
curl -I http://neurorop.leadrecordwh.ru/
curl -I https://neurorop.leadrecordwh.ru/
```

Ожидаемый результат:

- localhost без credentials — `401 Unauthorized`;
- HTTP-домен — `301` на HTTPS;
- HTTPS без credentials — `401 Unauthorized`;
- API и WEB — `Up`;
- WEB опубликован только как `127.0.0.1:18081->80`;
- API не имеет опубликованного host-порта.

## Проверить доступ с прежним Basic Auth

Пароль не выводи на экран и не передавай в чат:

```bash
pass=$(cat runtime/access.txt)
curl --user "rop:${pass}" -o /dev/null -sS -w 'frontend=%{http_code}\n' https://neurorop.leadrecordwh.ru/
curl --user "rop:${pass}" -o /dev/null -sS -w 'api_health=%{http_code}\n' https://neurorop.leadrecordwh.ru/api/health
unset pass
```

Оба запроса должны вернуть `200`.

## Только перезапустить текущие контейнеры

Если код, Docker image, `.env` и runtime-файлы не менялись, можно перезапустить
существующие контейнеры без пересборки:

```bash
docker restart neuro-rop-api neuro-rop-web
```

После этого повтори проверки HTTPS и `/api/health` из предыдущего раздела.

Не используй простой `docker restart`, если изменился код, Dockerfile,
`runtime/.env`, `runtime/access.txt` или runtime mounts. В этих случаях запускай
`./deploy/deploy-production.sh`: restart не пересоздаёт container environment и
не обновляет image.

## Изменение runtime/.env

Перед редактированием создай локальную резервную копию только на VPS:

```bash
cp -a runtime/.env "runtime/.env.backup.$(date +%Y%m%d-%H%M%S)"
```

Отредактируй файл, не выводя его содержимое в терминальные логи или чат, затем
пересоздай контейнеры:

```bash
./deploy/deploy-production.sh
```

Проверить scheduler без вывода остальных секретов:

```bash
grep '^DAYTIME_CYCLE_ENABLED=' runtime/.env
```

Для production ожидается `DAYTIME_CYCLE_ENABLED=true`.

## Логи и диагностика

Состояние и последние логи:

```bash
docker ps -a --filter name=neuro-rop-api --filter name=neuro-rop-web
docker logs --tail 100 neuro-rop-api
docker logs --tail 100 neuro-rop-web
```

Проверить API изнутри контейнера:

```bash
docker exec neuro-rop-api python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=3).status)'
```

Проверить внутренний Nginx и host Nginx:

```bash
docker exec neuro-rop-web nginx -t
nginx -t
systemctl status nginx --no-pager
```

Проверить сертификаты и автоматическое продление:

```bash
certbot certificates
systemctl is-enabled certbot.timer
systemctl is-active certbot.timer
```

Не запускай получение сертификата при обычном deploy.

## Если deploy завершился ошибкой

1. Не повторяй deploy вслепую.
2. Прочитай сообщение скрипта и лог соответствующего контейнера.
3. Проверь свободное место без очистки:

```bash
df -h /
docker system df
```

4. Убедись, что demo продолжает работать:

```bash
docker ps --filter name=neurorop-demo
curl -I https://demo-neurorop.leadrecordwh.ru/
```

Ответ demo без credentials должен оставаться `401 Unauthorized`.

## Откат кода

Не откатывай VPS через `git reset --hard`. Создай обычный `git revert` нужного
commit в основном checkout, проверь изменения и отправь новый commit в `main`:

```bash
git revert <commit_sha>
git push origin main
```

Новый commit пройдёт CI/CD и будет развёрнут штатно. SQLite и runtime этим не
откатываются; их восстановление требует отдельного подтверждённого плана.

## Что запрещено делать при обычном обслуживании

```text
docker system prune
docker image prune -a
docker compose down
docker network rm neuro-rop-practice-net
rm -rf /opt/Neuro_rop_practice/runtime
git reset --hard
git clean
```

Не изменяй и не перезапускай:

```text
neurorop-demo-api
neurorop-demo-web
neurorop-demo-net
/opt/Neuro_rop_demo
```

Host Nginx, Certbot и firewall изменяются только для отдельной инфраструктурной
задачи, а не при обновлении приложения.
