# Временный защищённый стенд

Скрипт `temporary-tunnel.sh` запускает три изолированных контейнера:

- `neuro-rop-api` — FastAPI, без опубликованного внешнего порта;
- `neuro-rop-web` — frontend и обратный proxy к API с Basic Auth;
- `neuro-rop-tunnel` — временный HTTPS-туннель Cloudflare.

Перед запуском на сервере должны существовать только локальные runtime-данные:

```text
/opt/Neuro_rop_practice/runtime/.env
/opt/Neuro_rop_practice/runtime/reports/
/opt/Neuro_rop_practice/runtime/knowledge/
/opt/Neuro_rop_practice/runtime/crm_pipeline_map.json
```

Они не входят в Git и не попадают в Docker-образы. База правил `knowledge/` монтируется
и карта воронок `crm_pipeline_map.json` монтируются в API только на чтение. Скрипт создаёт временный пароль
в `/opt/Neuro_rop_practice/runtime/access.txt` с правами только для root. Его можно
сменить, удалив этот файл и повторно запустив скрипт.

После запуска скрипт проверяет `/api/health`, конфигурацию Nginx и состояние
всех трёх контейнеров. Обычное обновление пересобирает только `neuro-rop-api` и
`neuro-rop-web`. Живой `neuro-rop-tunnel` не останавливается и не пересоздаётся,
поэтому текущий `trycloudflare.com` URL сохраняется. Скрипт сообщает этот URL;
если туннель пришлось запустить впервые или после остановки, ссылка будет новой.

Текущий URL без перезапуска туннеля:

```bash
./deploy/temporary-tunnel.sh --show-url
```

## Автоматическое обновление из main

Workflow `.github/workflows/deploy-main.yml` запускается после push или merge в
`main`. Сначала он выполняет Python unit tests, frontend lint и production build.
Только после успешных проверок workflow подключается к VPS по SSH, проверяет
чистоту checkout и наличие runtime-данных, делает fast-forward до проверенного
commit и запускает этот же `temporary-tunnel.sh`. Workflow не вызывает
`docker compose down` и не перезапускает `neuro-rop-tunnel`.

Workflow не передаёт на GitHub application-секреты. `.env`, отчёты, SQLite,
knowledge, карта воронок и пароль Basic Auth остаются только в `runtime/` на VPS.
Для SSH используются GitHub Secrets; их настройка описана в runbook.

Остановка временного стенда:

```bash
docker rm --force neuro-rop-tunnel neuro-rop-web neuro-rop-api
docker network rm neuro-rop-practice-net
```

`/opt`-проекты, cron-задачи, системные сервисы и firewall скрипт не изменяет.

Подробное обновление, проверка и безопасная остановка стенда описаны в
[`Docs/temporary_tunnel_runbook.md`](../Docs/temporary_tunnel_runbook.md).
