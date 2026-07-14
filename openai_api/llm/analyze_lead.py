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
from bitrix.context_diagnostics import ensure_context_diagnostics
from openai_api.bitrix_links import bitrix_entity_url
from openai_api.audio.build_lead_transcript_context import build_all_lead_transcript_context
from openai_api.config import ANALYSIS_MODEL, logger
from openai_api.llm.analyze_deal import knowledge_files, read_text
from openai_api.llm.llm_client import ModelJsonParseError, call_analysis_json
from openai_api.llm.prompt_budget import attach_response_metadata, build_prompt_budget, write_prompt_budget
from openai_api.llm.validation import AnalysisValidationError, normalize_analysis_for_validation, validate_lead_analysis
from openai_api.logging_utils import log_model_file_payload, log_model_text_payload
from openai_api.pricing import format_usd_rub
from setup import MSK_TZ


DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge" / "clients" / "praktikm"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze lead history and optional transcript with OpenAI")
    parser.add_argument("--lead-id", required=True, help="Lead ID to analyze")
    parser.add_argument("--transcript", default="latest", help="Transcript path, 'all', 'latest', or 'none'. Default: latest if exists.")
    parser.add_argument("--lead-root", default=str(DEFAULT_LEAD_WORKSPACE_ROOT), help="Root folder with prepared lead workspaces.")
    parser.add_argument("--knowledge-dir", default=str(DEFAULT_KNOWLEDGE_DIR), help="Processed OKF knowledge folder.")
    parser.add_argument("--model", default=ANALYSIS_MODEL, help="OpenAI analysis model")
    parser.add_argument("--dry-run", action="store_true", help="Build and log inputs, save prompt, but do not call OpenAI.")
    parser.add_argument(
        "--allow-direct-llm",
        action="store_true",
        help="Allow direct LLM call. Lead change detection is planned next; use intentionally.",
    )
    return parser.parse_args()


def latest_transcript_or_none(transcripts_dir: Path) -> Path | None:
    candidates = sorted(
        [path for path in transcripts_dir.glob("*.md") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def resolve_transcript(value: str, lead_dir: Path) -> Path | None:
    lowered = value.lower()
    if lowered == "none":
        return None
    if lowered == "latest":
        return latest_transcript_or_none(lead_dir / "transcripts")
    if lowered == "all":
        lead_id = lead_dir.name.removeprefix("lead_")
        return build_all_lead_transcript_context(lead_dir, lead_id)
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"Transcript not found: {path}")
    return path


def build_prompt(
    lead_id: str,
    history_text: str,
    transcript_text: str,
    context_diagnostics_text: str,
    okf_sections: list[tuple[Path, str]],
) -> str:
    okf_text = "\n\n".join(f"### OKF FILE: {path.name}\n\n{text.strip()}" for path, text in okf_sections)
    return f"""Ты ИИ-помощник РОПа ПрактикМ.

Отчет читает только РОП. Менеджер не видит систему и не читает отчет.
Главный результат анализа — управленческое действие РОПа:
1. что проверить;
2. какое поручение отправить менеджеру;
3. какой факт должен появиться в CRM;
4. до какого срока проверить выполнение.

Не делай общий AI-отчет.
Не ограничивайся советами вроде "проконтролировать", "обратить внимание", "связаться с клиентом".
Каждая рекомендация должна иметь срок, ожидаемый CRM-факт, критерий выполнения и evidence из истории.

Проанализируй лид и текущее состояние обработки. Главный вопрос: является ли лид рабочим, что уже сделано, есть ли риск потери лида и какое поручение РОП должен отправить менеджеру.

Верни только валидный JSON без markdown.

<structured_output_contract>
- Верни только один JSON-объект, без markdown, без ```json, без пояснений до или после JSON.
- Не добавляй поля вне указанной JSON-структуры.
- Если данных нет, используй null, "unknown", "не указано" или пустой массив в зависимости от типа поля.
- Перед финальным ответом проверь, что все фигурные и квадратные скобки закрыты.
- Все строки должны быть корректно экранированы для JSON.
</structured_output_contract>

<grounding_rules>
- Факты лида бери только из истории лида и транскрибации/нового события.
- OKF-база — это правила оценки и рекомендации, а не источник фактов о конкретном клиенте.
- Если факт есть только в OKF-базе, не записывай его как факт лида.
- Если нужного факта нет в истории или транскрибации, прямо укажи, каких данных не хватает.
- Если диагностика полноты контекста показывает пробелы, не делай выводы по отсутствующим звонкам/источникам и явно отрази ограничение в выводах.
- Если в истории есть связанные сделки того же контакта, не считай отсутствие действия в текущей карточке лида отсутствием работы с клиентом.
- Внутренний контекст и комментарии менеджеров можно использовать как evidence для контроля РОПа, но не считай их словами клиента.
- Diagnostics используй только как сведения о полноте/ограничениях выгрузки, не как факты лида.
</grounding_rules>

<length_limits>
- summary/reason/description: максимум 2-3 коротких предложения.
- Списки what_done_well, missed_points, next_call_plan, manager_checklist: максимум 5 пунктов.
- Любой массив evidence: максимум 7 пунктов. Если фактов больше 7, выбери самые важные и объединяй близкие факты в один пункт.
- При выборе evidence приоритет такой: клиентские факты и транскрипт, затем CRM-статус/задачи/комментарии, затем внутренний чат как источник управленческого контекста.
- Готовый email или messenger text: максимум 1200 символов.
- call_script: максимум 900 символов.
- Не повторяй одну и ту же мысль в нескольких полях.
</length_limits>

Правила:
1. Не выдумывай факты.
2. Используй OKF-правила только как правила, а не как факты лида.
3. Если нет транскрибации, анализируй карточку лида, комментарии, звонки, задачи и таймлайн.
4. Если были только недозвоны/нет контакта, так и укажи.
5. Готовые тексты должны быть деловыми, конкретными и готовыми к отправке.
6. Не используй служебные пометки и плейсхолдеры вроде "ДОБАВИТЬ", "{{данные}}", "todo", "tbd" в готовых текстах.
7. Если содержательного контакта не было, обязательно примени рекомендацию по дозвону из OKF и заполни call_attempt_recommendation.
8. Не пиши "лид плохой", если не было нормального дозвона, альтернативного канала или следующего шага. В таком случае отделяй проблему обработки от качества лида.
9. Квалификацию A/B/C/D/E давай только по фактам BANT, сроку, ЛПР, производству и техническим данным. Если данных мало, ставь unknown и фиксируй data_gap в loss_diagnosis.
10. Рекомендация должна быть управленческой: что РОП поручает менеджеру и какой факт должен появиться в CRM.
11. Если лид требует уточнения, поручай менеджеру один контакт с клиентом: получить недостающие факты и сразу зафиксировать результат в CRM. Не превращай это в два независимых действия.
12. rop_manager_message_block.message_to_manager — готовое поручение менеджеру. В нём должны быть срок, конкретные вопросы клиенту и результат, который нужно внести в CRM.
13. manager_action_block.primary_text — готовый текст именно для клиента, без инструкций менеджеру вроде "внеси в CRM". Для лида он должен уточнять потребность, параметры задачи, бюджетный ориентир, срок, ЛПР и следующий шаг только в той части, которая ещё не известна.
14. manager_action_block.manager_checklist — короткий список CRM-фактов после контакта; не дублируй в нём задачу или текст клиенту.

<qualification_rules>
Сначала заполни qualification_assessment: BANT, техническую применимость и коммерческую реализуемость нового оборудования. Только затем выбери lead_state.qualification, loss_diagnosis.final_verdict и рекомендацию.

1. BANT — четыре независимых признака:
   - budget: реальный бюджет и готовность двигаться к договору с предоплатой;
   - authority: контакт ЛПР либо влияет на решение;
   - need: конкретная актуальная потребность;
   - timeframe: назван срок закупки или запуска.
   Для этикетировщика готовность к предоплате в срок до 30 дней поддерживает timeframe=confirmed; для блока или линии розлива — до 60 дней. Это не заменяет остальные признаки BANT.
2. Техническая применимость оценивается по типу оборудования и известным параметрам из OKF technical_data.md. technical_mismatch допустим только при факте из CRM-истории или транскрибации о конкретном техническом стоп-факторе. Если параметров не хватает, укажи needs_technical_data, недостающие параметры и вопрос клиенту; это не технический отказ.
3. Коммерческая реализуемость относится только к новому оборудованию. below_minimum и verdict budget_below_new_equipment_minimum допустимы только когда клиент явно назвал бюджет менее 1000000 рублей. Не извлекай, не округляй и не предполагай бюджет. Не делай выводов о б/у оборудовании, аренде, лизинге, скидках или кредитной истории без отдельного подтвержденного правила.
4. Если BANT подтверждён, решение совместимо и подтверждённый бюджет нового оборудования не ниже 1000000 рублей: qualification=A, final_verdict=ready_for_deal.
5. Если проект реален, но BANT или технических данных не хватает: qualification=B или unknown по фактам, final_verdict=data_gap. При неполном BANT рекомендуй «Проявлен интерес» для доуточнения; при отложенной потребности — «Отправлено в автонапоминание», не финальный отказ.
6. Если потребность отложена, но не отклонена: qualification=C, final_verdict=needs_nurture.
7. При подтверждённом техническом стоп-факторе: qualification=D, final_verdict=technical_mismatch. При явно названном бюджете нового оборудования ниже 1000000 рублей: qualification=D, final_verdict=budget_below_new_equipment_minimum. Для D укажи ровно одну машиночитаемую причину в соответствующем reason_code; не скрывай отдельно подтвержденную плохую обработку в processing_quality/call_attempt_quality и при необходимости bad_processing.
8. Спам, явный нецелевой лид или брак оценивай по существующим правилам как E/bad_lead. Если содержательного контакта ещё не было и данных недостаточно: qualification=unknown, final_verdict=data_gap или bad_processing только по фактам обработки.
9. Для любого доуточнения сформируй одно поручение менеджеру: один контакт, конкретные вопросы, срок и CRM-факты для фиксации. В bant.next_question верни один конкретный вопрос клиенту или null.
</qualification_rules>

<verification_loop>
Перед финальным JSON проверь:
1. JSON валиден и соответствует указанной структуре.
2. Все факты опираются на историю лида или транскрибацию.
3. OKF использована только как правила оценки.
4. В готовых текстах нет "ДОБАВИТЬ", "todo", "tbd", плейсхолдеров с фигурными скобками или других служебных пометок.
5. Если содержательного контакта не было, это отражено в activity_summary и call_attempt_recommendation.
</verification_loop>

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
  "qualification_assessment": {{
    "bant": {{
      "budget": {{"status": "confirmed|missing|unknown", "evidence": ["краткий факт из CRM или транскрибации"]}},
      "authority": {{"status": "confirmed|missing|unknown", "evidence": []}},
      "need": {{"status": "confirmed|missing|unknown", "evidence": []}},
      "timeframe": {{"status": "confirmed|missing|unknown", "evidence": []}},
      "overall_status": "confirmed|incomplete|unknown",
      "missing_facts": ["что именно нужно выяснить"],
      "next_question": "один конкретный вопрос клиенту или null"
    }},
    "solution_fit": {{
      "equipment_type": "labeler|filling_line|block|unknown",
      "status": "compatible|not_compatible|needs_technical_data|unknown",
      "reason_code": "technical_mismatch|unknown|null",
      "evidence": ["краткий факт из CRM или транскрибации"],
      "missing_facts": ["недостающий технический параметр"]
    }},
    "commercial_fit": {{
      "new_equipment_budget_status": "sufficient|below_minimum|unknown",
      "confirmed_budget_rub": "число или null",
      "new_equipment_minimum_rub": 1000000,
      "reason_code": "budget_below_new_equipment_minimum|unknown|null",
      "evidence": ["краткий факт из CRM или транскрибации"]
    }}
  }},
  "activity_summary": {{
    "meaningful_contact": true,
    "summary": "что уже произошло по коммуникациям"
  }},
  "rop_manager_message_block": {{
    "check_for_rop": "что конкретно РОПу проверить по лиду",
    "why_it_matters": "почему это влияет на потерю лида, скорость обработки или деньги",
    "message_to_manager": "готовый текст поручения, который РОП может отправить менеджеру",
    "expected_crm_update": "какой факт должен появиться в CRM после действия менеджера",
    "deadline": "YYYY-MM-DD или null",
    "success_condition": "как понять, что поручение выполнено",
    "evidence": [
      "1-7 самых важных фактов из истории, звонка, комментария, задачи, статуса, CRM или внутреннего чата"
    ]
  }},
  "main_risk": {{
    "risk_level": "low|medium|medium_high|high",
    "risk_type": "тип риска",
    "description": "описание риска"
  }},
  "loss_diagnosis": {{
    "lead_quality": "good|weak|bad|unknown",
    "processing_quality": "good|weak|bad|unknown",
    "source_signal": "good_source|weak_source|unknown",
    "call_attempt_quality": "enough|not_enough|wrong_channel|unknown",
    "next_step_quality": "clear|missing|too_generic|unknown",
    "final_verdict": "bad_lead|bad_processing|data_gap|needs_nurture|ready_for_deal|technical_mismatch|budget_below_new_equipment_minimum|unknown",
    "evidence": ["1-7 самых важных фактов"]
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
    "goal": "цель клиентского касания, если менеджеру нужно обратиться к клиенту",
    "primary_text": {{
      "type": "call_script|messenger|email",
      "subject": "тема, если письмо",
      "text": "черновик текста клиенту для менеджера"
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

## ДИАГНОСТИКА ПОЛНОТЫ КОНТЕКСТА

{context_diagnostics_text.strip()}

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


def indented_bullet_list(values: Any) -> str:
    return "\n".join(f"  {line}" for line in bullet_list(values).splitlines())


def render_cost_section(metadata: dict[str, Any] | None) -> str:
    cost = (metadata or {}).get("estimated_cost") or {}
    if not cost:
        return "## Стоимость анализа\n\n- Стоимость: не рассчитана"

    return f"""## Стоимость анализа

- Модель: {cost.get('model', 'не указано')}
- Токены: input {cost.get('input_tokens', 0)}, cached input {cost.get('cached_input_tokens', 0)}, output {cost.get('output_tokens', 0)}
- Стоимость: {format_usd_rub(cost.get('estimated_cost_usd'), cost.get('estimated_cost_rub'))}
- Курс: 1 USD = {cost.get('usd_rub_rate', 'не указан')} руб."""


def render_context_limitations_section(context_diagnostics: dict[str, Any] | None) -> str:
    if not context_diagnostics:
        return ""
    summary = context_diagnostics.get("summary") or {}
    gaps = context_diagnostics.get("gaps") or []
    if not gaps:
        return ""
    manual_actions_path = context_diagnostics.get("manual_actions_path")
    return f"""## Ограничения анализа

- Контекст: {context_diagnostics.get('context_completeness', 'не указано')}
- Критичные пробелы: {summary.get('critical_gaps', 0)}
- Звонков без транскрипта: {summary.get('calls_without_transcript', 0)}
- Подробный список для добора: {manual_actions_path or 'diagnostics/manual_actions.md'}"""


def _report_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _report_value(value: Any) -> str:
    if value is None or value == "":
        return "нет данных"
    return str(value)


def render_qualification_assessment_section(analysis: dict[str, Any]) -> str:
    assessment = analysis.get("qualification_assessment")
    if not isinstance(assessment, dict):
        return "## Квалификация и применимость\n\nНет данных"

    bant = _report_dict(assessment.get("bant"))
    solution_fit = _report_dict(assessment.get("solution_fit"))
    commercial_fit = _report_dict(assessment.get("commercial_fit"))
    lead_state = _report_dict(analysis.get("lead_state"))

    bant_items: list[str] = []
    for label, name in (
        ("Бюджет", "budget"),
        ("Полномочия", "authority"),
        ("Потребность", "need"),
        ("Срок", "timeframe"),
    ):
        item = _report_dict(bant.get(name))
        bant_items.append(
            f"- {label}: {_report_value(item.get('status'))}\n"
            f"  - Доказательства:\n{indented_bullet_list(item.get('evidence'))}"
        )

    gaps_present = (
        bant.get("overall_status") == "incomplete"
        or solution_fit.get("status") == "needs_technical_data"
        or commercial_fit.get("new_equipment_budget_status") == "unknown"
    )
    next_action = ""
    if gaps_present:
        next_action = f"\n\n- Одно следующее действие: {_report_value(bant.get('next_question'))}"

    return f"""## Квалификация и применимость

### BANT

{chr(10).join(bant_items)}

- Общий статус: {_report_value(bant.get('overall_status'))}
- Недостающие факты:
{bullet_list(bant.get('missing_facts'))}
- Вопрос клиенту: {_report_value(bant.get('next_question'))}

### Техническая применимость

- Тип оборудования: {_report_value(solution_fit.get('equipment_type'))}
- Статус: {_report_value(solution_fit.get('status'))}
- Причина: {_report_value(solution_fit.get('reason_code'))}
- Доказательства:
{bullet_list(solution_fit.get('evidence'))}
- Недостающие параметры:
{bullet_list(solution_fit.get('missing_facts'))}

### Бюджет нового оборудования

- Подтверждённый бюджет: {_report_value(commercial_fit.get('confirmed_budget_rub'))}
- Минимальный порог: {_report_value(commercial_fit.get('new_equipment_minimum_rub'))}
- Статус: {_report_value(commercial_fit.get('new_equipment_budget_status'))}
- Причина: {_report_value(commercial_fit.get('reason_code'))}
- Доказательства:
{bullet_list(commercial_fit.get('evidence'))}

### Категория и причина

- Категория: {_report_value(lead_state.get('qualification'))}
- Причина: {_report_value(lead_state.get('qualification_reason'))}{next_action}"""


def render_report(
    analysis: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    context_diagnostics: dict[str, Any] | None = None,
) -> str:
    lead_state = analysis.get("lead_state", {}) or {}
    activity = analysis.get("activity_summary", {}) or {}
    rop_manager = analysis.get("rop_manager_message_block", {}) or {}
    risk = analysis.get("main_risk", {}) or {}
    loss = analysis.get("loss_diagnosis", {}) or {}
    manager_quality = analysis.get("manager_quality", {}) or {}
    call_recommendation = analysis.get("call_attempt_recommendation", {}) or {}
    manager = analysis.get("manager_action_block", {}) or {}
    primary = manager.get("primary_text", {}) or {}
    rop = analysis.get("rop_action", {}) or {}
    backup_texts = manager.get("backup_texts") or []
    backup_md = "\n\n".join(f"### {item.get('title') or item.get('type')}\n\n{item.get('text', '')}" for item in backup_texts) or "Нет запасных текстов"

    lead_id = analysis.get("lead_id", "")
    bitrix_url = bitrix_entity_url("lead", lead_id)
    limitations = render_context_limitations_section(context_diagnostics)
    limitations_section = f"\n\n{limitations}\n" if limitations else ""
    qualification_assessment = render_qualification_assessment_section(analysis)

    return f"""# Отчет РОПу по лиду {lead_id}

Ссылка в Bitrix: {bitrix_url or 'не указана'}
{limitations_section}

## Что сделать РОПу сейчас

- Проверить: {rop_manager.get('check_for_rop') or rop.get('text', 'не указано')}
- Почему это важно: {rop_manager.get('why_it_matters', 'не указано')}
- Сообщение менеджеру: {rop_manager.get('message_to_manager', 'не указано')}
- Ожидаемый факт в CRM: {rop_manager.get('expected_crm_update', 'не указано')}
- Срок контроля: {human_value(rop_manager.get('deadline'))}
- Критерий выполнения: {rop_manager.get('success_condition', 'не указано')}
- Основание:
{bullet_list(rop_manager.get('evidence'))}

## Состояние лида

- Клиент: {lead_state.get('client', 'не указано')}
- Потребность: {lead_state.get('need', 'не указано')}
- Статус: {lead_state.get('status', 'не указано')}
- Квалификация: {lead_state.get('qualification', 'unknown')}
- Почему: {lead_state.get('qualification_reason', 'не указано')}
- Кратко: {lead_state.get('summary', 'не указано')}

{qualification_assessment}

## Коммуникации

- Содержательный контакт: {human_value(activity.get('meaningful_contact'))}
- Кратко: {activity.get('summary', 'не указано')}

## Главный риск

- Уровень: {risk.get('risk_level', 'не указано')}
- Тип: {risk.get('risk_type', 'не указано')}
- Описание: {risk.get('description', 'не указано')}

## Диагностика потери лида

- Качество лида: {loss.get('lead_quality', 'не указано')}
- Качество обработки: {loss.get('processing_quality', 'не указано')}
- Сигнал источника: {loss.get('source_signal', 'не указано')}
- Качество дозвона: {loss.get('call_attempt_quality', 'не указано')}
- Качество следующего шага: {loss.get('next_step_quality', 'не указано')}
- Вердикт: {loss.get('final_verdict', 'не указано')}

Основание:

{bullet_list(loss.get('evidence'))}

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

## Сообщение менеджеру от РОПа

{rop_manager.get('message_to_manager', 'не указано')}

- Ожидаемый факт в CRM: {rop_manager.get('expected_crm_update', 'не указано')}
- Срок контроля: {human_value(rop_manager.get('deadline'))}
- Критерий выполнения: {rop_manager.get('success_condition', 'не указано')}

## Черновик текста клиенту для менеджера

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


def load_context_diagnostics_for_analysis(
    *,
    entity_type: str,
    entity_id: str,
    workspace_root: Path,
) -> tuple[str, dict[str, Any] | None, dict[str, str]]:
    try:
        paths = ensure_context_diagnostics(entity_type, entity_id, workspace_root)
        llm_text = read_text(paths["llm_context"])
        payload = json.loads(paths["context_gaps"].read_text(encoding="utf-8"))
        payload["manual_actions_path"] = str(paths["manual_actions_md"])
        return llm_text, payload, {key: str(value) for key, value in paths.items()}
    except Exception as error:
        logger.warning("Could not build context diagnostics for %s %s: %s", entity_type, entity_id, error)
        return (
            "Диагностика полноты контекста не построена. Не считай это доказательством полной истории.",
            None,
            {},
        )


def main() -> None:
    args = parse_args()
    if not args.allow_direct_llm and not args.dry_run:
        raise SystemExit(
            "Direct lead LLM run is blocked to avoid duplicate costs. "
            "Use openai_api/llm/analyze_lead_if_changed.py, or pass --allow-direct-llm intentionally."
        )

    lead_dir = Path(args.lead_root) / f"lead_{args.lead_id}"
    history_path = lead_dir / "history" / f"lead_{args.lead_id}_customer_path.md"
    transcript_path = resolve_transcript(args.transcript, lead_dir)
    knowledge_dir = Path(args.knowledge_dir)
    analysis_dir = lead_dir / "analysis"
    context_diagnostics_text, context_diagnostics_payload, context_diagnostics_paths = (
        load_context_diagnostics_for_analysis(
            entity_type="lead",
            entity_id=str(args.lead_id),
            workspace_root=Path(args.lead_root),
        )
    )

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

    history_text = read_text(history_path)
    prompt = build_prompt(args.lead_id, history_text, transcript_text, context_diagnostics_text, okf_sections)
    analysis_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = analysis_dir / f"lead_{args.lead_id}_request_prompt.txt"
    prompt_budget_path = analysis_dir / f"lead_{args.lead_id}_prompt_budget.json"
    prompt_path.write_text(prompt, encoding="utf-8")
    prompt_budget = build_prompt_budget(
        prompt=prompt,
        model=args.model,
        history_text=history_text,
        transcript_text=transcript_text,
        diagnostics_text=context_diagnostics_text,
        okf_sections=okf_sections,
    )
    write_prompt_budget(prompt_budget_path, prompt_budget)
    logger.info("Saved full lead analysis request prompt: %s", prompt_path)
    logger.info("Saved privacy-preserving prompt budget: %s", prompt_budget_path)

    if args.dry_run:
        log_model_text_payload(
            logger,
            title="lead analysis prompt dry run",
            model=args.model,
            text=prompt,
            metadata={"api": "responses.create", "dry_run": True},
        )
        print(f"Dry run complete. Request prompt saved: {prompt_path}")
        print(f"Prompt budget saved: {prompt_budget_path}")
        return

    generated_at = datetime.now(MSK_TZ).isoformat()
    analysis_path = analysis_dir / f"lead_{args.lead_id}_analysis.json"
    report_path = analysis_dir / f"lead_{args.lead_id}_rop_report.md"
    raw_path = analysis_dir / f"lead_{args.lead_id}_raw_model_output.txt"
    error_path = analysis_dir / f"lead_{args.lead_id}_analysis_error.json"

    try:
        analysis, metadata = call_analysis_json(prompt, model=args.model)
    except ModelJsonParseError as error:
        write_prompt_budget(prompt_budget_path, attach_response_metadata(prompt_budget, error.metadata))
        raw_path.write_text(error.raw_output_text, encoding="utf-8")
        save_json(
            error_path,
            {
                "generated_at": generated_at,
                "lead_id": str(args.lead_id),
                "error": str(error),
                "model_metadata": {
                    key: value for key, value in error.metadata.items() if key != "raw_output_text"
                },
            },
        )
        print(f"Model returned invalid JSON. Raw output saved: {raw_path}")
        print(f"Error details saved: {error_path}")
        raise

    write_prompt_budget(prompt_budget_path, attach_response_metadata(prompt_budget, metadata))

    normalization_changes = normalize_analysis_for_validation(analysis)
    if normalization_changes:
        metadata["normalization_changes"] = normalization_changes
        logger.warning("Normalized lead analysis before validation: %s", normalization_changes)

    try:
        validate_lead_analysis(analysis)
    except AnalysisValidationError as error:
        raw_path.write_text(metadata.get("raw_output_text", ""), encoding="utf-8")
        save_json(
            error_path,
            {
                "generated_at": generated_at,
                "lead_id": str(args.lead_id),
                "error": str(error),
                "model_metadata": {
                    key: value for key, value in metadata.items() if key != "raw_output_text"
                },
                "analysis": analysis,
            },
        )
        print(f"Model analysis failed validation. Raw output saved: {raw_path}")
        print(f"Error details saved: {error_path}")
        raise

    output_payload = {
        "generated_at": generated_at,
        "lead_id": str(args.lead_id),
        "input_files": {
            "history": str(history_path),
            "transcript": str(transcript_path) if transcript_path else None,
            "context_diagnostics": context_diagnostics_paths,
            "knowledge": [str(path) for path, _text in okf_sections],
        },
        "model_metadata": {key: value for key, value in metadata.items() if key != "raw_output_text"},
        "analysis": analysis,
    }

    save_json(analysis_path, output_payload)
    report_path.write_text(render_report(analysis, metadata, context_diagnostics_payload), encoding="utf-8")
    raw_path.write_text(metadata.get("raw_output_text", ""), encoding="utf-8")

    logger.info("Saved lead analysis JSON: %s", analysis_path)
    logger.info("Saved lead ROP report markdown: %s", report_path)
    logger.info("Saved raw model output: %s", raw_path)

    print(f"Analysis saved: {analysis_path}")
    print(f"ROP report saved: {report_path}")
    print(f"Estimated analysis cost: {format_usd_rub(metadata.get('estimated_cost_usd'), metadata.get('estimated_cost_rub'))}")


if __name__ == "__main__":
    main()
