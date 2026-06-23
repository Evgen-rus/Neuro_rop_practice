"""
Analyze a prepared lead workspace using lead history, optional transcript, and processed OKF rules.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.workspace import DEFAULT_LEAD_WORKSPACE_ROOT
from openai_api.config import ANALYSIS_MODEL, logger
from openai_api.llm.analyze_deal import knowledge_files, read_text
from openai_api.llm.llm_client import call_analysis_json
from openai_api.logging_utils import log_model_file_payload, log_model_text_payload
from openai_api.pricing import format_usd_rub
from setup import MSK_TZ


DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge" / "clients" / "praktikm"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze lead history and optional transcript with OpenAI")
    parser.add_argument("--lead-id", required=True, help="Lead ID to analyze")
    parser.add_argument("--transcript", default="latest", help="Transcript path, 'latest', or 'none'. Default: latest if exists.")
    parser.add_argument("--lead-root", default=str(DEFAULT_LEAD_WORKSPACE_ROOT), help="Root folder with prepared lead workspaces.")
    parser.add_argument("--knowledge-dir", default=str(DEFAULT_KNOWLEDGE_DIR), help="Processed OKF knowledge folder.")
    parser.add_argument("--model", default=ANALYSIS_MODEL, help="OpenAI analysis model")
    parser.add_argument("--dry-run", action="store_true", help="Build and log inputs, save prompt, but do not call OpenAI.")
    return parser.parse_args()


def latest_transcript_or_none(transcripts_dir: Path) -> Path | None:
    candidates = sorted(
        [path for path in transcripts_dir.glob("*.md") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def resolve_transcript(value: str, lead_dir: Path) -> Path | None:
    if value.lower() == "none":
        return None
    if value.lower() == "latest":
        return latest_transcript_or_none(lead_dir / "transcripts")
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"Transcript not found: {path}")
    return path


def build_prompt(lead_id: str, history_text: str, transcript_text: str, okf_sections: list[tuple[Path, str]]) -> str:
    okf_text = "\n\n".join(f"### OKF FILE: {path.name}\n\n{text.strip()}" for path, text in okf_sections)
    return f"""Ты ИИ-помощник РОПа ПрактикМ.

Проанализируй лид и текущее состояние обработки. Главный вопрос: является ли лид рабочим, что уже сделано, есть ли риск потери лида и что нужно сделать менеджеру дальше.

Верни только валидный JSON без markdown.

Правила:
1. Не выдумывай факты.
2. Используй OKF-правила только как правила, а не как факты лида.
3. Если нет транскрибации, анализируй карточку лида, комментарии, звонки, задачи и таймлайн.
4. Если были только недозвоны/нет контакта, так и укажи.
5. Готовые тексты должны быть деловыми, конкретными и готовыми к отправке.
6. Не используй служебные пометки и плейсхолдеры вроде "ДОБАВИТЬ", "уточнить", "{{данные}}" в готовых текстах.
7. Если содержательного контакта не было, обязательно примени рекомендацию по дозвону из OKF и заполни call_attempt_recommendation.

Нужная JSON-структура:
{{
  "lead_id": "{lead_id}",
  "lead_state": {{
    "summary": "краткое состояние лида",
    "client": "имя/компания, если есть",
    "need": "потребность клиента",
    "status": "статус лида, если есть",
    "qualification": "A|B|C|D|E|unknown",
    "qualification_reason": "почему такая квалификация"
  }},
  "activity_summary": {{
    "meaningful_contact": true,
    "summary": "что уже произошло по коммуникациям"
  }},
  "main_risk": {{
    "risk_level": "low|medium|medium_high|high",
    "risk_type": "тип риска",
    "description": "описание риска"
  }},
  "manager_quality": {{
    "what_done_well": [],
    "missed_points": [],
    "critical_mistake": null
  }},
  "call_attempt_recommendation": {{
    "applicable": true,
    "contact_status": "meaningful_contact|missed_call|busy|voicemail|unavailable|dropped|unknown",
    "attempts_found": "что видно по попыткам дозвона",
    "recommendation_fit": "follows|partial|does_not_follow|unknown|not_applicable",
    "recommendation_gap": "что не сделано по рекомендации или чего не хватает в истории",
    "next_call_plan": [
      "конкретное действие по следующей попытке дозвона"
    ],
    "rop_control": "что РОПу стоит проверить по дозвону"
  }},
  "manager_action_block": {{
    "recommended_channel": "phone|email|messenger|crm_task",
    "channel_reason": "почему выбран канал",
    "goal": "цель касания",
    "primary_text": {{
      "type": "call_script|messenger|email",
      "subject": "тема, если письмо",
      "text": "готовый текст"
    }},
    "backup_texts": [
      {{"type": "messenger", "title": "Короткое сообщение", "text": "текст"}},
      {{"type": "call_script", "title": "Скрипт звонка", "text": "текст"}}
    ],
    "manager_checklist": []
  }},
  "rop_action": {{
    "required": true,
    "text": "что должен проконтролировать РОП"
  }},
  "memory_update": {{
    "change_summary": "что сохранить в памяти лида",
    "facts_confirmed_add": [],
    "open_questions_update": [],
    "next_actions_update": [],
    "risks_update": []
  }}
}}

## ИСТОРИЯ ЛИДА

{history_text.strip()}

## ТРАНСКРИБАЦИЯ / НОВОЕ СОБЫТИЕ

{transcript_text.strip()}

## ОБРАБОТАННАЯ OKF-БАЗА ПРАВИЛ

{okf_text}
"""


def human_value(value: Any) -> str:
    if value is True:
        return "да"
    if value is False:
        return "нет"
    if value is None:
        return "не указано"
    return str(value)


def bullet_list(values: Any) -> str:
    if not values:
        return "- Нет данных"
    return "\n".join(f"- {item}" for item in values)


def render_cost_section(metadata: dict[str, Any] | None) -> str:
    cost = (metadata or {}).get("estimated_cost") or {}
    if not cost:
        return "## Стоимость анализа\n\n- Стоимость: не рассчитана"

    return f"""## Стоимость анализа

- Модель: {cost.get('model', 'не указано')}
- Токены: input {cost.get('input_tokens', 0)}, cached input {cost.get('cached_input_tokens', 0)}, output {cost.get('output_tokens', 0)}
- Стоимость: {format_usd_rub(cost.get('estimated_cost_usd'), cost.get('estimated_cost_rub'))}
- Курс: 1 USD = {cost.get('usd_rub_rate', 'не указан')} руб."""


def render_report(analysis: dict[str, Any], metadata: dict[str, Any] | None = None) -> str:
    lead_state = analysis.get("lead_state", {}) or {}
    activity = analysis.get("activity_summary", {}) or {}
    risk = analysis.get("main_risk", {}) or {}
    manager_quality = analysis.get("manager_quality", {}) or {}
    call_recommendation = analysis.get("call_attempt_recommendation", {}) or {}
    manager = analysis.get("manager_action_block", {}) or {}
    primary = manager.get("primary_text", {}) or {}
    rop = analysis.get("rop_action", {}) or {}
    backup_texts = manager.get("backup_texts") or []
    backup_md = "\n\n".join(f"### {item.get('title') or item.get('type')}\n\n{item.get('text', '')}" for item in backup_texts) or "Нет запасных текстов"

    return f"""# Отчет РОПу по лиду {analysis.get('lead_id', '')}

## Состояние лида

- Клиент: {lead_state.get('client', 'не указано')}
- Потребность: {lead_state.get('need', 'не указано')}
- Статус: {lead_state.get('status', 'не указано')}
- Квалификация: {lead_state.get('qualification', 'unknown')}
- Почему: {lead_state.get('qualification_reason', 'не указано')}
- Кратко: {lead_state.get('summary', 'не указано')}

## Коммуникации

- Содержательный контакт: {human_value(activity.get('meaningful_contact'))}
- Кратко: {activity.get('summary', 'не указано')}

## Главный риск

- Уровень: {risk.get('risk_level', 'не указано')}
- Тип: {risk.get('risk_type', 'не указано')}
- Описание: {risk.get('description', 'не указано')}

## Качество работы менеджера

Что сделано хорошо:

{bullet_list(manager_quality.get('what_done_well'))}

Что упущено:

{bullet_list(manager_quality.get('missed_points'))}

Критическая ошибка: {human_value(manager_quality.get('critical_mistake'))}

## Рекомендация по дозвону

- Применима: {human_value(call_recommendation.get('applicable'))}
- Статус контакта: {call_recommendation.get('contact_status', 'не указано')}
- Попытки в истории: {call_recommendation.get('attempts_found', 'не указано')}
- Соответствие рекомендации: {call_recommendation.get('recommendation_fit', 'не указано')}
- Что усилить: {call_recommendation.get('recommendation_gap', 'не указано')}
- Что РОПу проверить: {call_recommendation.get('rop_control', 'не указано')}

План дозвона:

{bullet_list(call_recommendation.get('next_call_plan'))}

## Что сделать менеджеру

- Канал: {manager.get('recommended_channel', 'не указано')}
- Почему: {manager.get('channel_reason', 'не указано')}
- Цель: {manager.get('goal', 'не указано')}

### Основной текст

Тема: {primary.get('subject', '')}

{primary.get('text', '')}

### Запасные варианты

{backup_md}

### Чеклист менеджера

{bullet_list(manager.get('manager_checklist'))}

## Контроль РОПа

- Требуется: {human_value(rop.get('required'))}
- Что проконтролировать: {rop.get('text', 'не указано')}

{render_cost_section(metadata)}
"""


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    lead_dir = Path(args.lead_root) / f"lead_{args.lead_id}"
    history_path = lead_dir / "history" / f"lead_{args.lead_id}_customer_path.md"
    transcript_path = resolve_transcript(args.transcript, lead_dir)
    knowledge_dir = Path(args.knowledge_dir)
    analysis_dir = lead_dir / "analysis"

    if not history_path.exists():
        raise FileNotFoundError(f"Lead history file not found: {history_path}")
    if not knowledge_dir.exists():
        raise FileNotFoundError(f"Knowledge dir not found: {knowledge_dir}")

    log_model_file_payload(logger, title="lead history input", model=args.model, path=history_path)
    if transcript_path:
        log_model_file_payload(logger, title="lead transcript input", model=args.model, path=transcript_path)
        transcript_text = read_text(transcript_path)
    else:
        transcript_text = "Транскрибация не предоставлена. Анализируй историю лида, активности и комментарии."

    okf_sections: list[tuple[Path, str]] = []
    for path in knowledge_files(knowledge_dir):
        log_model_file_payload(logger, title="OKF knowledge input", model=args.model, path=path)
        okf_sections.append((path, read_text(path)))

    prompt = build_prompt(args.lead_id, read_text(history_path), transcript_text, okf_sections)
    analysis_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = analysis_dir / f"lead_{args.lead_id}_request_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    logger.info("Saved full lead analysis request prompt: %s", prompt_path)

    if args.dry_run:
        log_model_text_payload(
            logger,
            title="lead analysis prompt dry run",
            model=args.model,
            text=prompt,
            metadata={"api": "responses.create", "dry_run": True},
        )
        print(f"Dry run complete. Request prompt saved: {prompt_path}")
        return

    analysis, metadata = call_analysis_json(prompt, model=args.model)
    output_payload = {
        "generated_at": datetime.now(MSK_TZ).isoformat(),
        "lead_id": str(args.lead_id),
        "input_files": {
            "history": str(history_path),
            "transcript": str(transcript_path) if transcript_path else None,
            "knowledge": [str(path) for path, _text in okf_sections],
        },
        "model_metadata": {key: value for key, value in metadata.items() if key != "raw_output_text"},
        "analysis": analysis,
    }

    analysis_path = analysis_dir / f"lead_{args.lead_id}_analysis.json"
    report_path = analysis_dir / f"lead_{args.lead_id}_rop_report.md"
    raw_path = analysis_dir / f"lead_{args.lead_id}_raw_model_output.txt"

    save_json(analysis_path, output_payload)
    report_path.write_text(render_report(analysis, metadata), encoding="utf-8")
    raw_path.write_text(metadata.get("raw_output_text", ""), encoding="utf-8")

    logger.info("Saved lead analysis JSON: %s", analysis_path)
    logger.info("Saved lead ROP report markdown: %s", report_path)
    logger.info("Saved raw model output: %s", raw_path)

    print(f"Analysis saved: {analysis_path}")
    print(f"ROP report saved: {report_path}")
    print(f"Estimated analysis cost: {format_usd_rub(metadata.get('estimated_cost_usd'), metadata.get('estimated_cost_rub'))}")


if __name__ == "__main__":
    main()
