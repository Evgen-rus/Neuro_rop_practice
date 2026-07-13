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

Правила работы агента: `AGENTS.md`. Архитектура и инварианты: `ARCHITECTURE.md`.
