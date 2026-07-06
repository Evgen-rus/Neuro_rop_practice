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
from bitrix.context_diagnostics import ensure_context_diagnostics
from openai_api.bitrix_links import bitrix_entity_url
from openai_api.audio.build_deal_transcript_context import build_all_deal_transcript_context
from openai_api.change_detection.stage_policy import build_deal_stage_policy
from openai_api.config import ANALYSIS_MODEL, logger
from openai_api.llm.llm_client import ModelJsonParseError, call_analysis_json
from openai_api.llm.validation import AnalysisValidationError, validate_deal_analysis
from openai_api.logging_utils import log_model_file_payload, log_model_text_payload
from openai_api.pricing import format_usd_rub
from setup import MSK_TZ


DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge" / "clients" / "praktikm"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze deal history and transcript with OpenAI")
    parser.add_argument("--deal-id", required=True, help="Deal ID to analyze")
    parser.add_argument(
        "--transcript",
        default="latest",
        help="Transcript path, 'all', 'latest', or 'none'. Default: latest transcript in deal workspace.",
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
    parser.add_argument(
        "--allow-direct-llm",
        action="store_true",
        help="Allow direct LLM call. Prefer analyze_deal_if_changed.py for normal runs.",
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
    lowered = value.lower()
    if lowered == "none":
        return None
    if lowered == "latest":
        return latest_transcript(deal_dir / "transcripts")
    if lowered == "all":
        deal_id = deal_dir.name.removeprefix("deal_")
        return build_all_deal_transcript_context(deal_dir, deal_id)

    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"Transcript not found: {path}")
    return path


def resolve_history_path(deal_dir: Path, deal_id: str) -> Path:
    compact_path = deal_dir / "history" / f"deal_{deal_id}_llm_context.md"
    if compact_path.exists():
        return compact_path
    return deal_dir / "history" / f"deal_{deal_id}_customer_path.md"


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


def build_prompt(
    deal_id: str,
    history_text: str,
    transcript_text: str,
    context_diagnostics_text: str,
    okf_sections: list[tuple[Path, str]],
    stage_policy: dict[str, Any],
) -> str:
    okf_text = "\n\n".join(
        f"### OKF FILE: {path.name}\n\n{text.strip()}" for path, text in okf_sections
    )
    stage_policy_text = json.dumps(stage_policy, ensure_ascii=False, indent=2)
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

Проанализируй сделку и новое событие, если оно есть. Если транскрибация не предоставлена, анализируй текущее состояние сделки по истории, активностям и комментариям. Главный вопрос: продвинулся ли клиент к оплате, КП, договору, передаче данных или следующему конкретному шагу, и какое поручение РОП должен отправить менеджеру.

Верни только валидный JSON без markdown.

<structured_output_contract>
- Верни только один JSON-объект, без markdown, без ```json, без пояснений до или после JSON.
- Не добавляй поля вне указанной JSON-структуры.
- Если данных нет, используй null, "unknown", "не указано" или пустой массив в зависимости от типа поля.
- Перед финальным ответом проверь, что все фигурные и квадратные скобки закрыты.
- Все строки должны быть корректно экранированы для JSON.
</structured_output_contract>

<grounding_rules>
- Факты сделки бери только из истории сделки и транскрибации/нового события.
- OKF-база — это правила оценки и рекомендации, а не источник фактов о конкретном клиенте.
- CRM_STAGE_POLICY — это детерминированные данные из CRM о текущей стадии и закрытии сделки. Используй их как факт CRM-статуса.
- Если факт есть только в OKF-базе, не записывай его как факт сделки.
- Если нужного факта нет в истории или транскрибации, прямо укажи, каких данных не хватает.
- Если диагностика полноты контекста показывает пробелы, не делай выводы по отсутствующим звонкам/источникам и явно отрази ограничение в выводах.
- Если в истории есть связанные сделки того же контакта, не считай отсутствие действия в текущей карточке отсутствием работы с клиентом.
- Внутренний контекст, комментарии менеджеров и diagnostics не считай словами клиента.
</grounding_rules>

<crm_stage_rules>
Если CRM_STAGE_POLICY.is_closed_lost=true:
- Не анализируй сделку как обычную открытую сделку.
- Сначала оцени корректность закрытия в closed_deal_review.
- Если в истории есть признаки потенциала: потребность, бюджетный ориентир, срок, ЛПР/путь к ЛПР, запрос КП/ТЗ или коммерческий интерес, ставь reopen_candidate=true и rop_decision="needs_manual_review" или "return_to_pipeline".
- Если закрытие выглядит обоснованным, ставь reopen_candidate=false и rop_decision="keep_closed".
- Текст клиенту можно дать только как реактивационный сценарий и только если client_reactivation_allowed=true.
- В manager_action_block.manager_checklist обязательно добавь, что текст использовать только после решения РОПа вернуть/реанимировать сделку.

Правила по типам закрытия:
- duplicate: не реанимировать как продажу; проверить дубль.
- lost_to_competitor: разобрать проигрыш конкуренту; возможен мягкий post-loss follow-up.
- integration_blocker: проверить, это реальный технический стоп или решаемая интеграция.
- price_lost: проверить защиту ценности, комплектацию, сроки, сервис, лизинг или альтернативный состав.
- postponed: нужна контрольная дата и прогрев, не считать окончательной потерей без даты возврата.
- wrong_qualification: искать спорное закрытие, если есть потребность, сумма, срок или путь к ЛПР.
- cannot_produce: не давать клиентский текст без проверки производства/аналога.
- not_relevant: проверить реальную причину неактуальности.
- no_response: проверить качество дозвона и альтернативные каналы.

Если CRM_STAGE_POLICY.is_closed_lost=false, closed_deal_review.applicable=false.
</crm_stage_rules>

<length_limits>
- summary/reason/description: максимум 2-3 коротких предложения.
- Списки what_changed, what_done_well, missed_points, manager_checklist: максимум 5 пунктов.
- Списки allowed_work, blocked_work, defense_points, questions_to_client: максимум 5 пунктов.
- Списки missing_confirmation, next_actions: максимум 5 пунктов.
- Список likely_objections: максимум 3 пункта.
- Готовый email или messenger text: максимум 1200 символов.
- call_script: максимум 900 символов.
- Не повторяй одну и ту же мысль в нескольких полях.
</length_limits>

<management_blocks_rules>
Определи управленческий режим сделки:
- active_sale: клиент вовлечен, есть понятный следующий шаг и движение к КП/счету/договору/оплате.
- payment_control: договор, счет или условия уже согласованы, и главное узкое место сейчас - аванс, оплата, лизинговый платеж, дата поступления денег или подтверждение платежа.
- managed_pause: клиент прямо взял паузу, причина понятна, есть дата возврата или контрольная дата.
- hard_qualification: сделка крупная, но не подтверждены бюджет, ЛПР, срок, критерии выбора или реальность проекта.
- nurture: потребность есть, но срок покупки далеко; нужен прогрев и контрольная дата.
- disqualify: клиент нецелевой, бюджета нет, задача не подходит или нет смысла продолжать.
- lost_risk: сделка близка к потере: конкурент, тишина, пауза без даты, цена не принята, ЛПР не найден.
- unknown: недостаточно данных.

Если договор подписан, счет выставлен, договор согласован или лизинг сообщил, что ждет аванс/оплату, не ставь lost_risk только из-за отсутствия денег. В такой ситуации ставь deal_mode.mode="payment_control", если нет явных признаков отказа или потери. Цель режима payment_control - довести сделку до денег: получить статус "оплачено", конкретную дату оплаты или причину задержки с планом эскалации.

Оцени контроль ресурсов:
- should_spend_engineering_time=false, если клиент ждет конкурента/Китай, бюджет не подтвержден, критерии выбора не зафиксированы, ЛПР неизвестен, нет конкретного запроса на правку КП или сделка в паузе.
- should_spend_engineering_time=true, если клиент запросил конкретную правку, есть срок решения, техническая доработка прямо влияет на следующий шаг к счету/договору/оплате.
- Если данных недостаточно, ставь false и объясняй, какие данные нужны, чтобы оправдать ресурс.

Сформируй shaker_question как один прямой, но деловой квалифицирующий вопрос. Он должен вскрывать бюджет, срок, ЛПР, критерии выбора, конкурента или реальность проекта. Не используй давление, ультиматумы и токсичный тон.

Если есть Китай, конкурент, альтернативный поставщик, тендер, "сравниваем", "ждем другое предложение", "дорого" или "аналог", заполни competitor_defense_checklist. Не выдумывай конкурента, если его нет в истории.

Определи priority_recommendation:
- high: сделка активна, есть деньги/срок/ЛПР и можно двигать к оплате.
- high также ставь, если договор/счет уже согласован и осталось получить аванс или оплату, но дата платежа не подтверждена.
- medium: потенциал есть, но есть риски: конкурент, пауза, неясный ЛПР, неясные критерии.
- low: интерес слабый, срок далеко, бюджет не подтвержден.
- pause: есть явная пауза с датой возврата.
- disqualify: нет бюджета, нецелевой запрос или нет смысла продолжать.

Заполни payment_blocker:
- applicable=true, если сделка находится около денег: счет, договор, аванс, предоплата, лизинг, оплата поставщику, поступление денег, закрывающие документы или внутреннее финансовое согласование.
- blocker_type выбирай по фактическому узкому месту: advance_payment, leasing_payment, invoice_payment, internal_approval, documents, unknown.
- payer - кто должен совершить оплату или действие для оплаты: клиент, лизинг, бухгалтерия, ЛПР, unknown.
- payment_recipient - кому должна поступить оплата: нам, лизингу, поставщику, unknown.
- confirmed_payment_date - только если дата явно есть в истории; иначе null.
- missing_confirmation - что конкретно не подтверждено.
- next_actions - 1-5 действий, которые ведут к статусу "оплачено", дате оплаты или причине задержки.
- escalation_condition - когда РОПу нужно подключиться или усилить контроль.

Заполни money_path_diagnosis:
- stuck_point выбирай по фактическому месту застревания пути к деньгам: источник, дозвон, менеджер, следующий шаг, стадия, оплата, пауза клиента или unknown.
- why_money_is_at_risk объясняет, почему деньги могут быть потеряны или задержаны.
- current_owner_of_next_step показывает, у кого сейчас действие: менеджер, клиент, РОП, финансы, лизинг или unknown.
- next_required_fact — какой один факт нужен в CRM, чтобы сделка реально двинулась к деньгам.
- evidence — только факты из истории, транскрипта или CRM_STAGE_POLICY.

Заполни objection_handling:
- applicable=true только если в истории сделки есть фактические сигналы вероятного возражения: цена, бюджет, Китай, конкурент, пауза, неясный ЛПР, технические сомнения, сроки, внутреннее согласование или задержка оплаты.
- Выбирай 1-3 наиболее вероятных возражения по фактам сделки, не перечисляй весь справочник.
- Каждое возражение должно помогать менеджеру в следующем контакте: мягко ответить, задать следующий вопрос и получить движение к договору, счету, правке КП, оплате, дате решения или честной дисквалификации.
- Не предлагай скидку первым действием. Не спорь с клиентом, не обесценивай конкурента/Китай и не выдумывай факты о бюджете, ЛПР, конкуренте или сроках.
</management_blocks_rules>

Правила:
1. Не выдумывай факты.
2. Если транскрибация не предоставлена, new_event.type должен быть "unknown", а new_event.summary должен пояснить, что анализ идет без нового события.
3. Если транскрибация является недозвоном, автоответчиком или служебным сообщением, так и укажи.
4. Если разговора с клиентом не было, new_event.type должен быть "missed_call", а new_event.is_meaningful_contact должен быть false.
5. Не оценивай качество переговоров, если разговора с клиентом не было.
6. Используй OKF-правила только как правила, а не как факты сделки.
7. Готовые тексты клиенту должны быть деловыми, конкретными и готовыми к отправке.
8. Не используй служебные пометки и плейсхолдеры вроде "ДОБАВИТЬ", "{{данные}}", "todo", "tbd" в готовых текстах и темах письма.
9. Если не хватает данных, укажи конкретный список и зачем они нужны.
10. Если содержательного контакта не было, обязательно примени рекомендацию по дозвону из OKF и заполни call_attempt_recommendation.
11. Если КП уже отправлено, не ограничивайся формулировкой "обсудить КП": нужно получить критерии выбора, срок решения, ЛПР и следующий шаг к договору, счету, предоплате или согласованию комплектации.
12. В готовом тексте после отправки КП не пиши "направляю КП"; используй формулировки вроде "возвращаюсь к направленному КП".
13. В email или мессенджере после недозвона предлагай 2 конкретных варианта времени для будущего созвона, если это уместно.
14. В live call script не предлагай время будущего созвона: если менеджер дозвонился, разговор уже идет. Завершай скрипт вопросом о следующем шаге, сроке решения, правках, договоре/счете или внутреннем согласовании.
15. Если сделка в паузе, не превращай рекомендацию в агрессивный дожим: зафиксируй контрольную дату, критерии сравнения и следующий шаг после паузы.
16. Если есть конкурент/Китай, защита предложения должна сравнивать не только цену, но и комплектацию, сроки запуска, гарантию, сервис, обучение, ответственность поставщика и риски внедрения.
17. Если сделка у оплаты, поручение РОПа менеджеру должно требовать точную дату аванса/оплаты, ответственного за платеж и причину задержки, если дата не подтверждена.
18. Если сделка закрыта как lost / неверный квал, сначала дай решение для РОПа: проверять возврат или оставить закрытой. Клиентский текст допустим только после решения РОПа.

<verification_loop>
Перед финальным JSON проверь:
1. JSON валиден и соответствует указанной структуре.
2. Все факты опираются на историю сделки или транскрибацию.
3. OKF использована только как правила оценки.
4. В готовых текстах нет "ДОБАВИТЬ", "todo", "tbd", плейсхолдеров с фигурными скобками или других служебных пометок.
5. Если контакта не было, сделка не отмечена как продвинутая только из-за звонка.
</verification_loop>

Нужная JSON-структура:
{{
  "deal_id": "{deal_id}",
  "deal_state": {{
    "summary": "краткое состояние сделки",
    "amount": "сумма, если есть",
    "stage": "этап, если есть",
    "client": "клиент/контакт, если есть"
  }},
  "rop_manager_message_block": {{
    "check_for_rop": "что конкретно РОПу проверить по сделке",
    "why_it_matters": "почему это влияет на деньги, потерю сделки или движение к оплате",
    "message_to_manager": "готовый текст поручения, который РОП может отправить менеджеру",
    "expected_crm_update": "какой факт должен появиться в CRM после действия менеджера",
    "deadline": "YYYY-MM-DD или null",
    "success_condition": "как понять, что поручение выполнено",
    "evidence": [
      "факт из истории, звонка, комментария, задачи, стадии или CRM"
    ]
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
  "money_path_diagnosis": {{
    "stuck_point": "source|call_attempt|manager|next_step|stage|payment|client_pause|unknown",
    "why_money_is_at_risk": "почему деньги могут быть потеряны или задержаны",
    "current_owner_of_next_step": "manager|client|rop|finance|leasing|unknown",
    "next_required_fact": "какой факт нужен для движения к деньгам",
    "evidence": []
  }},
  "main_risk": {{
    "risk_level": "low|medium|medium_high|high",
    "risk_type": "тип риска",
    "description": "описание риска"
  }},
  "deal_mode": {{
    "mode": "active_sale|payment_control|managed_pause|hard_qualification|nurture|disqualify|lost_risk|unknown",
    "reason": "почему выбран такой режим",
    "manager_behavior": "как менеджеру вести сделку в этом режиме",
    "rop_focus": "что должен контролировать РОП"
  }},
  "closed_deal_review": {{
    "applicable": true,
    "crm_closed": true,
    "stage_id": "этап CRM, например C15:4",
    "stage_name": "название этапа CRM",
    "closed_reason_type": "duplicate|lost_to_competitor|integration_blocker|price_lost|postponed|wrong_qualification|cannot_produce|not_relevant|no_response|won|unknown|not_applicable",
    "reopen_candidate": true,
    "confidence": "high|medium|low|unknown",
    "why_closed_questionable": [],
    "why_closed_may_be_valid": [],
    "rop_decision": "return_to_pipeline|keep_closed|needs_manual_review|not_applicable",
    "recommended_pipeline_action": "что сделать со стадией/сделкой в CRM",
    "client_reactivation_allowed": true,
    "client_text_usage_note": "использовать текст клиенту только если РОП решил вернуть или реанимировать сделку"
  }},
  "resource_control": {{
    "should_spend_engineering_time": false,
    "reason": "почему можно или нельзя тратить технические ресурсы",
    "allowed_work": [],
    "blocked_work": []
  }},
  "payment_blocker": {{
    "applicable": true,
    "blocker_type": "advance_payment|leasing_payment|invoice_payment|internal_approval|documents|unknown|not_applicable",
    "payer": "кто должен оплатить или подтвердить оплату",
    "payment_recipient": "кому должна поступить оплата",
    "confirmed_payment_date": "YYYY-MM-DD или null",
    "current_status": "текущий статус оплаты по фактам сделки",
    "missing_confirmation": [],
    "next_actions": [],
    "post_payment_next_step": "что нужно сделать сразу после подтверждения оплаты",
    "escalation_condition": "когда РОПу нужно подключиться"
  }},
  "objection_handling": {{
    "applicable": true,
    "summary": "кратко: какие возражения вероятнее всего и почему",
    "likely_objections": [
      {{
        "objection_type": "price|budget|china|competitor|pause|decision_maker|technical_doubt|timing|internal_approval|payment_delay|unknown",
        "probability": "high|medium|low",
        "evidence": "факт из истории сделки, почему это возражение вероятно",
        "client_phrase": "как клиент может сформулировать возражение",
        "manager_reply": "короткая мягкая деловая фраза менеджера",
        "follow_up_question": "вопрос, который двигает сделку дальше",
        "next_step_goal": "какой результат нужно получить после отработки",
        "what_not_to_do": "чего менеджеру не делать"
      }}
    ]
  }},
  "shaker_question": {{
    "question": "один прямой деловой квалифицирующий вопрос клиенту",
    "why_this_question": "что именно должен вскрыть вопрос",
    "when_to_use": "когда и в каком канале использовать"
  }},
  "competitor_defense_checklist": {{
    "applicable": true,
    "competitor_type": "china|direct_competitor|alternative_supplier|internal_solution|unknown|not_applicable",
    "defense_points": [],
    "questions_to_client": [],
    "risk_if_not_defended": "что будет, если не защитить предложение"
  }},
  "priority_recommendation": {{
    "priority": "high|medium|low|pause|disqualify",
    "reason": "почему такой приоритет",
    "time_allocation": "сколько внимания менеджера/РОПа сейчас оправдано",
    "next_review_date": "YYYY-MM-DD или null",
    "what_must_happen_to_raise_priority": "что должно произойти, чтобы поднять приоритет"
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
    "goal": "цель клиентского касания, если менеджеру нужно обратиться к клиенту",
    "primary_text": {{
      "type": "email|messenger|call_script",
      "subject": "тема, если письмо",
      "text": "черновик текста клиенту для менеджера"
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

## ДИАГНОСТИКА ПОЛНОТЫ КОНТЕКСТА

{context_diagnostics_text.strip()}

## CRM_STAGE_POLICY

{stage_policy_text}

## ОБРАБОТАННАЯ OKF-БАЗА ПРАВИЛ

{okf_text}
"""


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


def render_report(
    analysis: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    context_diagnostics: dict[str, Any] | None = None,
) -> str:
    deal_state = analysis.get("deal_state", {}) or {}
    new_event = analysis.get("new_event", {}) or {}
    risk = analysis.get("main_risk", {}) or {}
    rop_manager = analysis.get("rop_manager_message_block", {}) or {}
    deal_mode = analysis.get("deal_mode", {}) or {}
    closed_review = analysis.get("closed_deal_review", {}) or {}
    resource_control = analysis.get("resource_control", {}) or {}
    payment_blocker = analysis.get("payment_blocker", {}) or {}
    money_path = analysis.get("money_path_diagnosis", {}) or {}
    objection_handling = analysis.get("objection_handling", {}) or {}
    shaker_question = analysis.get("shaker_question", {}) or {}
    competitor = analysis.get("competitor_defense_checklist", {}) or {}
    priority = analysis.get("priority_recommendation", {}) or {}
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

    def yes_no(value: Any) -> str:
        if value is True:
            return "да"
        if value is False:
            return "нет"
        return human_value(value)

    backup_texts = manager.get("backup_texts") or []
    backup_md = "\n\n".join(
        f"### {item.get('title') or item.get('type')}\n\n{item.get('text', '')}" for item in backup_texts
    ) or "Нет запасных текстов"

    def render_objections(value: dict[str, Any]) -> str:
        if not value.get("applicable"):
            return ""
        objections = value.get("likely_objections") or []
        if not isinstance(objections, list) or not objections:
            return ""

        parts = [
            "## Возможные возражения и отработка",
            "",
            f"Кратко: {value.get('summary', 'не указано')}",
            "",
        ]
        for index, item in enumerate(objections[:3], start=1):
            if not isinstance(item, dict):
                continue
            parts.extend(
                [
                    f"### {index}. {item.get('objection_type', 'unknown')} ({item.get('probability', 'unknown')})",
                    "",
                    f"- Сигнал из сделки: {item.get('evidence', 'не указано')}",
                    f"- Как может сказать клиент: {item.get('client_phrase', 'не указано')}",
                    f"- Как ответить: {item.get('manager_reply', 'не указано')}",
                    f"- Что спросить дальше: {item.get('follow_up_question', 'не указано')}",
                    f"- Цель следующего шага: {item.get('next_step_goal', 'не указано')}",
                    f"- Чего не делать: {item.get('what_not_to_do', 'не указано')}",
                    "",
                ]
            )
        return "\n".join(parts).strip()

    objections_md = render_objections(objection_handling)
    objections_section = f"\n\n{objections_md}\n" if objections_md else ""

    def render_closed_deal_review(value: dict[str, Any]) -> str:
        if not value.get("applicable"):
            return ""
        return f"""## Проверка закрытой сделки

- CRM-статус: закрыта как проваленная
- Этап закрытия: {value.get('stage_name', 'не указано')} ({value.get('stage_id', 'не указано')})
- Тип причины закрытия: {value.get('closed_reason_type', 'не указано')}
- Кандидат на возврат в воронку: {yes_no(value.get('reopen_candidate'))}
- Уверенность оценки: {value.get('confidence', 'не указано')}
- Решение для РОПа: {value.get('rop_decision', 'не указано')}

Почему закрытие может быть спорным:

{bullet_list(value.get('why_closed_questionable'))}

Почему закрытие может быть обоснованным:

{bullet_list(value.get('why_closed_may_be_valid'))}

Рекомендуемое действие в CRM: {value.get('recommended_pipeline_action', 'не указано')}

Правило для текста клиенту: {value.get('client_text_usage_note', 'не указано')}"""

    closed_review_md = render_closed_deal_review(closed_review)
    closed_review_section = f"\n\n{closed_review_md}\n" if closed_review_md else ""

    manager_action_warning = ""
    if closed_review.get("applicable") and closed_review.get("client_reactivation_allowed"):
        manager_action_warning = (
            "\nВажно: текст ниже использовать только если РОП решил вернуть "
            "или реанимировать сделку. Сейчас сделка закрыта как проваленная.\n"
        )

    deal_id = analysis.get("deal_id", "")
    bitrix_url = bitrix_entity_url("deal", deal_id)
    limitations = render_context_limitations_section(context_diagnostics)
    limitations_section = f"\n\n{limitations}\n" if limitations else ""

    return f"""# Отчет РОПу по сделке {deal_id}

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

## Диагностика пути к деньгам

- Где застряли: {money_path.get('stuck_point', 'не указано')}
- Почему деньги под риском: {money_path.get('why_money_is_at_risk', 'не указано')}
- У кого следующий шаг: {money_path.get('current_owner_of_next_step', 'не указано')}
- Следующий нужный факт: {money_path.get('next_required_fact', 'не указано')}

Основание:

{bullet_list(money_path.get('evidence'))}

## Главный риск

- Уровень: {risk.get('risk_level', 'не указано')}
- Тип: {risk.get('risk_type', 'не указано')}
- Описание: {risk.get('description', 'не указано')}
{closed_review_section}

## Режим сделки

- Режим: {deal_mode.get('mode', 'не указано')}
- Почему: {deal_mode.get('reason', 'не указано')}
- Как вести менеджеру: {deal_mode.get('manager_behavior', 'не указано')}
- Фокус РОПа: {deal_mode.get('rop_focus', 'не указано')}

## Контроль оплаты

- Применимо: {yes_no(payment_blocker.get('applicable'))}
- Узкое место: {payment_blocker.get('blocker_type', 'не указано')}
- Кто должен оплатить/подтвердить: {payment_blocker.get('payer', 'не указано')}
- Кому должна поступить оплата: {payment_blocker.get('payment_recipient', 'не указано')}
- Подтвержденная дата оплаты: {human_value(payment_blocker.get('confirmed_payment_date'))}
- Текущий статус: {payment_blocker.get('current_status', 'не указано')}

Чего не хватает для контроля денег:

{bullet_list(payment_blocker.get('missing_confirmation'))}

Следующие действия:

{bullet_list(payment_blocker.get('next_actions'))}

Шаг после оплаты: {payment_blocker.get('post_payment_next_step', 'не указано')}

Когда подключать РОПа: {payment_blocker.get('escalation_condition', 'не указано')}

## Контроль ресурсов

- Тратить технические ресурсы сейчас: {yes_no(resource_control.get('should_spend_engineering_time'))}
- Почему: {resource_control.get('reason', 'не указано')}

Что можно делать:

{bullet_list(resource_control.get('allowed_work'))}

Что не делать:

{bullet_list(resource_control.get('blocked_work'))}

## Ключевой квалифицирующий вопрос

**Вопрос:** {shaker_question.get('question', 'не указано')}

Зачем: {shaker_question.get('why_this_question', 'не указано')}

Когда использовать: {shaker_question.get('when_to_use', 'не указано')}

## Защита от конкурента / альтернативы

- Применимо: {yes_no(competitor.get('applicable'))}
- Тип конкурента: {competitor.get('competitor_type', 'не указано')}

Что защитить:

{bullet_list(competitor.get('defense_points'))}

Что спросить:

{bullet_list(competitor.get('questions_to_client'))}

Риск, если не защитить: {competitor.get('risk_if_not_defended', 'не указано')}
{objections_section}

## Приоритет сделки

- Приоритет: {priority.get('priority', 'не указано')}
- Почему: {priority.get('reason', 'не указано')}
- Сколько времени тратить: {priority.get('time_allocation', 'не указано')}
- Дата следующего контроля: {human_value(priority.get('next_review_date'))}
- Что должно произойти для повышения приоритета: {priority.get('what_must_happen_to_raise_priority', 'не указано')}

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

## Сообщение менеджеру от РОПа

{rop_manager.get('message_to_manager', 'не указано')}

- Ожидаемый факт в CRM: {rop_manager.get('expected_crm_update', 'не указано')}
- Срок контроля: {human_value(rop_manager.get('deadline'))}
- Критерий выполнения: {rop_manager.get('success_condition', 'не указано')}

## Черновик текста клиенту для менеджера

- Канал: {manager.get('recommended_channel', 'не указано')}
- Почему: {manager.get('channel_reason', 'не указано')}
- Цель: {manager.get('goal', 'не указано')}
{manager_action_warning}

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
            "Direct deal LLM run is blocked to avoid duplicate costs. "
            "Use openai_api/llm/analyze_deal_if_changed.py, or pass --allow-direct-llm intentionally."
        )

    deal_dir = Path(args.deal_root) / f"deal_{args.deal_id}"
    history_path = resolve_history_path(deal_dir, str(args.deal_id))
    transcript_path = resolve_transcript(args.transcript, deal_dir)
    knowledge_dir = Path(args.knowledge_dir)
    analysis_dir = deal_dir / "analysis"
    stage_policy = build_deal_stage_policy(deal_dir, str(args.deal_id))
    context_diagnostics_text, context_diagnostics_payload, context_diagnostics_paths = (
        load_context_diagnostics_for_analysis(
            entity_type="deal",
            entity_id=str(args.deal_id),
            workspace_root=Path(args.deal_root),
        )
    )

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
        context_diagnostics_text,
        okf_sections,
        stage_policy,
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

    generated_at = datetime.now(MSK_TZ).isoformat()
    analysis_path = analysis_dir / f"deal_{args.deal_id}_analysis.json"
    report_path = analysis_dir / f"deal_{args.deal_id}_rop_report.md"
    raw_path = analysis_dir / f"deal_{args.deal_id}_raw_model_output.txt"
    error_path = analysis_dir / f"deal_{args.deal_id}_analysis_error.json"

    try:
        analysis, metadata = call_analysis_json(prompt, model=args.model)
    except ModelJsonParseError as error:
        raw_path.write_text(error.raw_output_text, encoding="utf-8")
        save_json(
            error_path,
            {
                "generated_at": generated_at,
                "deal_id": str(args.deal_id),
                "error": str(error),
                "model_metadata": {
                    key: value for key, value in error.metadata.items() if key != "raw_output_text"
                },
            },
        )
        print(f"Model returned invalid JSON. Raw output saved: {raw_path}")
        print(f"Error details saved: {error_path}")
        raise

    try:
        validate_deal_analysis(analysis)
    except AnalysisValidationError as error:
        raw_path.write_text(metadata.get("raw_output_text", ""), encoding="utf-8")
        save_json(
            error_path,
            {
                "generated_at": generated_at,
                "deal_id": str(args.deal_id),
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
        "deal_id": str(args.deal_id),
        "input_files": {
            "history": str(history_path),
            "transcript": str(transcript_path) if transcript_path else None,
            "context_diagnostics": context_diagnostics_paths,
            "knowledge": [str(path) for path, _text in okf_sections],
        },
        "crm_stage_policy": stage_policy,
        "model_metadata": {
            key: value for key, value in metadata.items() if key != "raw_output_text"
        },
        "analysis": analysis,
    }

    save_json(analysis_path, output_payload)
    report_path.write_text(render_report(analysis, metadata, context_diagnostics_payload), encoding="utf-8")
    raw_path.write_text(metadata.get("raw_output_text", ""), encoding="utf-8")

    logger.info("Saved deal analysis JSON: %s", analysis_path)
    logger.info("Saved ROP report markdown: %s", report_path)
    logger.info("Saved raw model output: %s", raw_path)

    print(f"Analysis saved: {analysis_path}")
    print(f"ROP report saved: {report_path}")
    print(f"Estimated analysis cost: {format_usd_rub(metadata.get('estimated_cost_usd'), metadata.get('estimated_cost_rub'))}")


if __name__ == "__main__":
    main()
