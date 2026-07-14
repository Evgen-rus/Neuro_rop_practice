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

Остановка временного стенда:

```bash
docker rm --force neuro-rop-tunnel neuro-rop-web neuro-rop-api
docker network rm neuro-rop-practice-net
```

`/opt`-проекты, cron-задачи, системные сервисы и firewall скрипт не изменяет.

Подробное обновление, проверка и безопасная остановка стенда описаны в
[`Docs/temporary_tunnel_runbook.md`](../Docs/temporary_tunnel_runbook.md).
