# Помощник РОПа — локальный MVP

Локальный инструмент для РОПа: кандидаты из Bitrix, анализ через существующий CLI, отчёт в UI. В Bitrix ничего не пишем.

## Что уже есть

- CLI-пайплайн: `run_rop_assistant.py`
- API-адаптер: FastAPI в `api/`
- UI: React/Vite в `frontend/`
- Кандидаты: Bitrix-фильтр + scoring без LLM (по умолчанию 15 дней, топ-20)
- Короткие звонки `< 20 сек` не транскрибируются (недозвон/автоответчик)
- Решения РОПа и исходы хранятся в SQLite рядом с change detection

## Запуск

Нужен существующий `venv` и `.env` (`BITRIX_WEBHOOK_URL`, `OPENAI_API_KEY`, …).

### 1. API

```powershell
.\venv\Scripts\python.exe -m uvicorn api.app:app --reload --host 127.0.0.1 --port 8000
```

Проверка: http://127.0.0.1:8000/api/health

### 2. UI

```powershell
cd frontend
npm install
npm run dev
```

Открыть: http://127.0.0.1:5173

UI ходит в API через Vite proxy `/api` → `127.0.0.1:8000`.

## Как пользоваться

1. Вкладка **Кандидаты** — сразу список «требуют внимания».
2. Можно менять окно дней (от 0), тип и приоритет.
3. Выбрать карточку → **Запустить анализ**.
4. Или вкладка **Ручной запуск**: ID через запятую/столбиком и опции как в CLI.
5. После анализа: факты, рекомендация, блок менеджеру, решение РОПа.
6. Полный markdown-отчёт — только по кнопке «Показать полный отчёт».

## Важно

- Bitrix: только чтение.
- CLI не ломан: UI вызывает `run_rop_assistant.py`.
- `reports/` чувствителен и не раздаётся целиком; UI читает analysis/report через API.
- Авторизация пока не нужна (только localhost).

## Полезные пути

- Архитектура: `ARCHITECTURE.md`
- ТЗ UI: `tz_front.md`
- Демо-референс визуала: `praktikm_rop_assistant_demo.html`
- SQLite: `reports/rop_assistant/rop_assistant.sqlite`
