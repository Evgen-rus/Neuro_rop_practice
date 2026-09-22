# Production deployment: neurorop.leadrecordwh.ru

Постоянный production работает по схеме:

```text
Internet → host Nginx :443 → 127.0.0.1:18081 → neuro-rop-web → neuro-rop-api:8000
```

`neuro-rop-api` не публикует порт на host. Basic Auth остаётся внутри
`neuro-rop-web`; пароль хранится только в `runtime/access.txt`.

## Runtime

Код расположен в `/opt/Neuro_rop_practice`, persistent runtime — в
`/opt/Neuro_rop_practice/runtime/`. Не удаляй и не переноси в Git `.env`,
`reports/`, `logs/`, `knowledge/`, `crm_pipeline_map.json` и `access.txt`.
SQLite находится внутри persistent `reports/` и переживает пересоздание
контейнеров.

## Обычный deploy

Push в `main` сначала проходит Python tests и frontend lint/build. Затем CI по
SSH проверяет clean checkout, runtime, tested SHA, fast-forward и deploy lock и
запускает:

```bash
cd /opt/Neuro_rop_practice
./deploy/deploy-production.sh
```

Скрипт меняет только `neuro-rop-api` и `neuro-rop-web`. Host Nginx, Certbot,
demo deployment и другие контейнеры он не меняет.

## Первичная настройка host Nginx и HTTPS

До изменений сохрани baseline demo и проверь, что порт `18081` свободен:

```bash
docker ps -a
docker networks
ss -lntp
nginx -T
certbot certificates
systemctl status nginx
```

Запусти production containers, установи отдельный HTTP server block и проверь
конфигурацию:

```bash
cd /opt/Neuro_rop_practice
./deploy/deploy-production.sh
install -m 644 deploy/nginx/neurorop.leadrecordwh.ru.conf /etc/nginx/sites-available/neurorop.leadrecordwh.ru
ln -s /etc/nginx/sites-available/neurorop.leadrecordwh.ru /etc/nginx/sites-enabled/neurorop.leadrecordwh.ru
nginx -t
systemctl reload nginx
curl -I http://neurorop.leadrecordwh.ru
```

Ответ `401 Unauthorized` до передачи Basic Auth подтверждает доступность web и
сохранённую защиту. После успешного HTTP получи сертификат один раз:

```bash
certbot --nginx -d neurorop.leadrecordwh.ru
certbot renew --dry-run
```

Certbot добавит HTTP → HTTPS и renewal; обычный CI больше эти настройки не
трогает.

## Приёмка и удаление Quick Tunnel

Перед cleanup проверь:

```bash
docker exec neuro-rop-api python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=3).read()'
curl -I http://127.0.0.1:18081
curl -I http://neurorop.leadrecordwh.ru
curl -I https://neurorop.leadrecordwh.ru
docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' neuro-rop-api neuro-rop-web
```

Дополнительно войди через прежний Basic Auth, проверь `/api/` через тот же
hostname, scheduler-флаг в `runtime/.env`, наличие SQLite/reports и неизменность
demo URL. Только после этого удали старый tunnel:

```bash
docker rm --force neuro-rop-tunnel
```

Не удаляй `neuro-rop-practice-net`, не выполняй `docker system prune`, `git
reset --hard`, `git clean` и не меняй `/opt/Neuro_rop_demo`.

Если deploy сломался, сначала смотри `docker logs --tail 100 neuro-rop-api` или
`neuro-rop-web`. Откат кода делай через обычный `git revert` в `main`, чтобы он
снова прошёл CI; runtime и SQLite отдельно не откатывай.
