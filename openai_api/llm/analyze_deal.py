"""
Analyze a prepared deal workspace using history, one transcript, and processed OKF rules.
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

from bitrix.workspace import DEFAULT_DEAL_WORKSPACE_ROOT
from openai_api.config import ANALYSIS_MODEL, logger
from openai_api.llm.llm_client import call_analysis_json
from openai_api.logging_utils import log_model_file_payload, log_model_text_payload
from setup import MSK_TZ


DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge" / "clients" / "praktikm"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze deal history and transcript with OpenAI")
    parser.add_argument("--deal-id", required=True, help="Deal ID to analyze")
    parser.add_argument(
        "--transcript",
        default="latest",
        help="Transcript path, 'latest', or 'none'. Default: latest transcript in deal workspace.",
    )
    parser.add_argument(
        "--deal-root",
        default=str(DEFAULT_DEAL_WORKSPACE_ROOT),
        help="Root folder with prepared deal workspaces.",
    )
    parser.add_argument(
        "--knowledge-dir",
        default=str(DEFAULT_KNOWLEDGE_DIR),
        help="Processed OKF knowledge folder.",
    )
    parser.add_argument("--model", default=ANALYSIS_MODEL, help="OpenAI analysis model")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and log inputs, save the request prompt, but do not call OpenAI.",
    )
    return parser.parse_args()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def latest_transcript(transcripts_dir: Path) -> Path:
    candidates = sorted(
        [path for path in transcripts_dir.glob("*.md") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No transcript .md files found in {transcripts_dir}")
    return candidates[0]


def resolve_transcript(value: str, deal_dir: Path) -> Path | None:
    if value.lower() == "none":
        return None
    if value.lower() == "latest":
        return latest_transcript(deal_dir / "transcripts")

    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"Transcript not found: {path}")
    return path


def knowledge_files(knowledge_dir: Path) -> list[Path]:
    priority = [
        "index.md",
        "qualification.md",
        "technical_data.md",
        "risk_signals.md",
        "call_attempt_rules.md",
        "commercial_offer_followup.md",
        "manager_texts.md",
        "objections.md",
        "funnel.md",
    ]
    files = [knowledge_dir / name for name in priority if (knowledge_dir / name).exists()]
    extra = sorted(
        path
        for path in knowledge_dir.glob("*.md")
        if path.name not in set(priority) and path.name.lower() != "readme.md"
    )
    return files + extra


def build_prompt(deal_id: str, history_text: str, transcript_text: str, okf_sections: list[tuple[Path, str]]) -> str:
    okf_text = "\n\n".join(
        f"### OKF FILE: {path.name}\n\n{text.strip()}" for path, text in okf_sections
    )
    return f"""Ты ИИ-помощник РОПа ПрактикМ.

Проанализируй сделку и новое событие, если оно есть. Если транскрибация не предоставлена, анализируй текущее состояние сделки по истории, активностям и комментариям. Главный вопрос: продвинулся ли клиент к оплате, КП, договору, передаче данных или следующему конкретному шагу.

Верни только валидный JSON без markdown.

Правила:
1. Не выдумывай факты.
2. Если транскрибация не предоставлена, new_event.type должен быть "unknown", а new_event.summary должен пояснить, что анализ идет без нового события.
3. Если транскрибация является недозвоном, автоответчиком или служебным сообщением, так и укажи.
4. Если разговора с клиентом не было, new_event.type должен быть "missed_call", а new_event.is_meaningful_contact должен быть false.
5. Не оценивай качество переговоров, если разговора с клиентом не было.
6. Используй OKF-правила только как правила, а не как факты сделки.
7. Готовые тексты клиенту должны быть деловыми, конкретными и готовыми к отправке.
8. Не используй служебные пометки и плейсхолдеры вроде "ДОБАВИТЬ", "уточнить", "{{данные}}" в готовых текстах и темах письма.
9. Если не хватает данных, укажи конкретный список и зачем они нужны.
10. Если содержательного контакта не было, обязательно примени рекомендацию по дозвону из OKF и заполни call_attempt_recommendation.
11. Если КП уже отправлено, не ограничивайся формулировкой "обсудить КП": нужно получить критерии выбора, срок решения, ЛПР и следующий шаг к договору, счету, предоплате или согласованию комплектации.
12. В готовом тексте после отправки КП не пиши "направляю КП"; используй формулировки вроде "возвращаюсь к направленному КП".
13. В email или мессенджере после недозвона предлагай 2 конкретных варианта времени для будущего созвона, если это уместно.
14. В live call script не предлагай время будущего созвона: если менеджер дозвонился, разговор уже идет. Завершай скрипт вопросом о следующем шаге, сроке решения, правках, договоре/счете или внутреннем согласовании.

Нужная JSON-структура:
{{
  "deal_id": "{deal_id}",
  "deal_state": {{
    "summary": "краткое состояние сделки",
    "amount": "сумма, если есть",
    "stage": "этап, если есть",
    "client": "клиент/контакт, если есть"
  }},
  "new_event": {{
    "type": "call|missed_call|email|unknown",
    "summary": "что произошло",
    "is_meaningful_contact": true
  }},
  "what_changed": ["что изменилось после нового события"],
  "deal_progress": {{
    "progressed": true,
    "reason": "почему сделка продвинулась или нет"
  }},
  "main_risk": {{
    "risk_level": "low|medium|medium_high|high",
    "risk_type": "тип риска",
    "description": "описание риска"
  }},
  "manager_quality": {{
    "what_done_well": ["что менеджер сделал хорошо"],
    "missed_points": ["что менеджер упустил"],
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
    "recommended_channel": "email|phone|messenger|crm_task",
    "channel_reason": "почему выбран канал",
    "goal": "цель касания",
    "primary_text": {{
      "type": "email|messenger|call_script",
      "subject": "тема, если письмо",
      "text": "готовый текст"
    }},
    "backup_texts": [
      {{"type": "messenger", "title": "Короткое сообщение", "text": "текст"}},
      {{"type": "call_script", "title": "Скрипт звонка", "text": "текст"}}
    ],
    "manager_checklist": ["что проверить перед отправкой"]
  }},
  "rop_action": {{
    "required": true,
    "text": "что должен проконтролировать РОП"
  }},
  "memory_update": {{
    "change_summary": "что обновить в памяти сделки",
    "facts_confirmed_add": [],
    "open_questions_update": [],
    "next_actions_update": [],
    "risks_update": []
  }}
}}

## ИСТОРИЯ СДЕЛКИ

{history_text.strip()}

## ТРАНСКРИБАЦИЯ / НОВОЕ СОБЫТИЕ

{transcript_text.strip()}

## ОБРАБОТАННАЯ OKF-БАЗА ПРАВИЛ

{okf_text}
"""


def render_report(analysis: dict[str, Any]) -> str:
    deal_state = analysis.get("deal_state", {}) or {}
    new_event = analysis.get("new_event", {}) or {}
    risk = analysis.get("main_risk", {}) or {}
    progress = analysis.get("deal_progress", {}) or {}
    call_recommendation = analysis.get("call_attempt_recommendation", {}) or {}
    manager = analysis.get("manager_action_block", {}) or {}
    primary = manager.get("primary_text", {}) or {}
    rop = analysis.get("rop_action", {}) or {}

    def bullet_list(values: Any) -> str:
        if not values:
            return "- Нет данных"
        return "\n".join(f"- {item}" for item in values)

    def human_value(value: Any) -> str:
        if value is True:
            return "да"
        if value is False:
            return "нет"
        if value is None:
            return "не указано"
        return str(value)

    backup_texts = manager.get("backup_texts") or []
    backup_md = "\n\n".join(
        f"### {item.get('title') or item.get('type')}\n\n{item.get('text', '')}" for item in backup_texts
    ) or "Нет запасных текстов"

    return f"""# Отчет РОПу по сделке {analysis.get('deal_id', '')}

## Состояние сделки

- Клиент: {deal_state.get('client', 'не указано')}
- Сумма: {deal_state.get('amount', 'не указано')}
- Этап: {deal_state.get('stage', 'не указано')}
- Кратко: {deal_state.get('summary', 'не указано')}

## Новое событие

- Тип: {new_event.get('type', 'unknown')}
- Содержательный контакт: {human_value(new_event.get('is_meaningful_contact'))}
- Кратко: {new_event.get('summary', 'не указано')}

## Изменения

{bullet_list(analysis.get('what_changed'))}

## Продвижение сделки

- Продвинулась: {human_value(progress.get('progressed'))}
- Причина: {progress.get('reason', 'не указано')}

## Главный риск

- Уровень: {risk.get('risk_level', 'не указано')}
- Тип: {risk.get('risk_type', 'не указано')}
- Описание: {risk.get('description', 'не указано')}

## Качество работы менеджера

Что сделано хорошо:

{bullet_list((analysis.get('manager_quality') or {}).get('what_done_well'))}

Что упущено:

{bullet_list((analysis.get('manager_quality') or {}).get('missed_points'))}

Критическая ошибка: {human_value((analysis.get('manager_quality') or {}).get('critical_mistake'))}

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
"""


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    deal_dir = Path(args.deal_root) / f"deal_{args.deal_id}"
    history_path = deal_dir / "history" / f"deal_{args.deal_id}_customer_path.md"
    transcript_path = resolve_transcript(args.transcript, deal_dir)
    knowledge_dir = Path(args.knowledge_dir)
    analysis_dir = deal_dir / "analysis"

    if not history_path.exists():
        raise FileNotFoundError(f"History file not found: {history_path}")
    if not knowledge_dir.exists():
        raise FileNotFoundError(f"Knowledge dir not found: {knowledge_dir}")

    log_model_file_payload(logger, title="deal history input", model=args.model, path=history_path)
    if transcript_path:
        log_model_file_payload(logger, title="deal transcript input", model=args.model, path=transcript_path)
        transcript_text = read_text(transcript_path)
    else:
        transcript_text = "Транскрибация не предоставлена. Анализируй историю сделки, активности, комментарии, текущий этап и риски без нового события."

    okf_sections: list[tuple[Path, str]] = []
    for path in knowledge_files(knowledge_dir):
        log_model_file_payload(logger, title="OKF knowledge input", model=args.model, path=path)
        okf_sections.append((path, read_text(path)))

    prompt = build_prompt(
        args.deal_id,
        read_text(history_path),
        transcript_text,
        okf_sections,
    )
    analysis_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = analysis_dir / f"deal_{args.deal_id}_request_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    logger.info("Saved full analysis request prompt: %s", prompt_path)

    if args.dry_run:
        log_model_text_payload(
            logger,
            title="deal analysis prompt dry run",
            model=args.model,
            text=prompt,
            metadata={"api": "responses.create", "dry_run": True},
        )
        print(f"Dry run complete. Request prompt saved: {prompt_path}")
        return

    analysis, metadata = call_analysis_json(prompt, model=args.model)

    generated_at = datetime.now(MSK_TZ).isoformat()
    output_payload = {
        "generated_at": generated_at,
        "deal_id": str(args.deal_id),
        "input_files": {
            "history": str(history_path),
            "transcript": str(transcript_path) if transcript_path else None,
            "knowledge": [str(path) for path, _text in okf_sections],
        },
        "model_metadata": {
            key: value for key, value in metadata.items() if key != "raw_output_text"
        },
        "analysis": analysis,
    }

    analysis_path = analysis_dir / f"deal_{args.deal_id}_analysis.json"
    report_path = analysis_dir / f"deal_{args.deal_id}_rop_report.md"
    raw_path = analysis_dir / f"deal_{args.deal_id}_raw_model_output.txt"

    save_json(analysis_path, output_payload)
    report_path.write_text(render_report(analysis), encoding="utf-8")
    raw_path.write_text(metadata.get("raw_output_text", ""), encoding="utf-8")

    logger.info("Saved deal analysis JSON: %s", analysis_path)
    logger.info("Saved ROP report markdown: %s", report_path)
    logger.info("Saved raw model output: %s", raw_path)

    print(f"Analysis saved: {analysis_path}")
    print(f"ROP report saved: {report_path}")


if __name__ == "__main__":
    main()
