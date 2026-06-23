# LLM analysis helpers

Здесь будут лежать только вызовы LLM для анализа уже подготовленных данных:

- клиент OpenAI Responses API;
- учет `usage` по каждому запросу;
- сохранение JSON/Markdown ответов в `reports/rop_assistant/.../analysis`.

Сырые документы клиента из `Docs/практик_м_доки` сюда не отправляем. В API должна уходить только обработанная OKF-база из `knowledge/clients/praktikm`.

## Анализ сделки

Проверка без API-запроса:

```powershell
.\venv\Scripts\python.exe .\openai_api\llm\analyze_deal.py --deal-id 18493 --dry-run
```

Dry run сохранит полный промпт в `analysis/deal_<id>_request_prompt.txt`.

Реальный запуск:

```powershell
.\venv\Scripts\python.exe .\openai_api\llm\analyze_deal.py --deal-id 18493
```

По умолчанию берется последняя `.md` транскрибация из папки сделки.

Явный файл:

```powershell
.\venv\Scripts\python.exe .\openai_api\llm\analyze_deal.py `
  --deal-id 18493 `
  --transcript "reports\rop_assistant\deals\deal_18493\transcripts\call_604173_2026-06-17_13-51-19_plus_03-00_transcript.md"
```
