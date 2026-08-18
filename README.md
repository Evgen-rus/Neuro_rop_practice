# Помощник РОПа

Локальный MVP для анализа лидов и сделок Bitrix24 через CLI и UI. Bitrix используется только на чтение.

## Быстрый запуск

Нужны существующие `venv`, `.env` и Node.js.

```powershell
.\venv\Scripts\python.exe -m uvicorn api.app:app --reload --host 127.0.0.1 --port 8000
```

```powershell
cd frontend
npm ci
npm run dev
```

API: http://127.0.0.1:8000/api/health
UI: http://127.0.0.1:5173

После старта API в будни с 08:00 до 18:00 МСК каждые 30 минут (и в 15:50 МСК) синхронизирует Bitrix, копит CRM-факты и запускает существующий FULL/MINI/skip. Ночью и в выходные не запускается. Отключить: `DAYTIME_CYCLE_ENABLED=false`. Оценка трат OpenAI за московский день: `logs/daily_spend/`.

Правила работы агента: `AGENTS.md`. Архитектура и инварианты: `ARCHITECTURE.md`.
