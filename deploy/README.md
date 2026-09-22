# Production deployment

`deploy-production.sh` пересобирает и пересоздаёт только два контейнера:

- `neuro-rop-api` — FastAPI без опубликованного host-порта;
- `neuro-rop-web` — frontend, Basic Auth и proxy `/api/`, опубликованный только как `127.0.0.1:18081`.

Оба контейнера работают в существующей сети `neuro-rop-practice-net` с
`restart: unless-stopped`. Host Nginx принимает
`https://neurorop.leadrecordwh.ru` и проксирует запросы на loopback-порт web.

Runtime остаётся вне Git и Docker image:

```text
/opt/Neuro_rop_practice/runtime/.env
/opt/Neuro_rop_practice/runtime/reports/
/opt/Neuro_rop_practice/runtime/logs/
/opt/Neuro_rop_practice/runtime/knowledge/
/opt/Neuro_rop_practice/runtime/crm_pipeline_map.json
/opt/Neuro_rop_practice/runtime/access.txt
```

Обычный deploy не меняет host Nginx, Certbot, runtime, demo deployment или
`neuro-rop-tunnel` и не выполняет Docker prune. Старый tunnel удаляется вручную
только после полной проверки постоянного HTTPS-домена.

## CI/CD

`.github/workflows/deploy-main.yml` на push в `main` выполняет Python tests,
frontend lint/build, затем по SSH проверяет чистый checkout и runtime, делает
fast-forward до проверенного SHA под deploy lock и запускает:

```bash
./deploy/deploy-production.sh
```

Application-секреты, CRM-данные и Basic Auth остаются только на VPS. Существующие
SSH secrets и `StrictHostKeyChecking=yes` сохраняются.

Первичная настройка host Nginx и HTTPS описана в
[`Docs/temporary_tunnel_runbook.md`](../Docs/temporary_tunnel_runbook.md).
Готовые команды для ручного обновления, перезапуска и диагностики находятся в
[`Docs/manual_production_operations.md`](../Docs/manual_production_operations.md).
