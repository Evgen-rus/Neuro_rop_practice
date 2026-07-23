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
from openai_api.llm.llm_client import ValidatedAnalysisFailure, call_analysis_json, call_validated_analysis_json
from openai_api.llm.prompt_budget import attach_response_metadata, build_prompt_budget, write_prompt_budget
from openai_api.llm.validation import AnalysisValidationError, normalize_analysis_for_validation, validate_lead_analysis
from openai_api.logging_utils import log_model_file_payload, log_model_text_payload
from openai_api.pricing import format_usd_rub
from progress_events import emit_progress, retry_progress_callback
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


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _response_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    response = payload.get("response")
    result = response.get("result") if isinstance(response, dict) else None
    return result if isinstance(result, dict) else {}


def load_lead_crm_state(lead_dir: Path) -> dict[str, Any]:
    """Build deterministic current CRM state for the lead prompt."""
    lead_id = lead_dir.name.removeprefix("lead_")
    bundle = _load_json_object(lead_dir / "raw" / f"lead_{lead_id}_customer_history_bundle.json")
    lead = _response_result(bundle.get("lead"))
    if not lead:
        context = _load_json_object(lead_dir / "raw" / f"lead_{lead_id}_context.json")
        lead = _response_result(context.get("lead"))

    status_id = str(lead.get("STATUS_ID") or "").strip()
    semantic = str(lead.get("STATUS_SEMANTIC_ID") or "").strip().upper()
    status_name = ""
    pipeline_map = _load_json_object(PROJECT_ROOT / "crm_pipeline_map.json")
    lead_pipeline = pipeline_map.get("lead_pipeline") if isinstance(pipeline_map.get("lead_pipeline"), dict) else {}
    for stage in lead_pipeline.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("status_id") or stage.get("STATUS_ID") or stage.get("id") or "").strip()
        if stage_id == status_id:
            status_name = str(stage.get("name") or stage.get("NAME") or "").strip()
            break

    return {
        "status_id": status_id or None,
        "status_name": status_name or None,
        "status_semantic_id": semantic or "unknown",
        "is_closed_lost": semantic == "F",
        "is_converted": status_id.upper() == "CONVERTED" or semantic == "S",
        "status_name_available": bool(status_name),
    }


def validate_lead_analysis_for_crm_state(
    analysis: dict[str, Any],
    crm_state: dict[str, Any],
) -> None:
    """Validate the model contract and bind its CRM claims to deterministic input."""
    validate_lead_analysis(analysis)
    closure_review = analysis.get("closure_review")
    if not isinstance(closure_review, dict):
        raise AnalysisValidationError("closure_review must be an object")

    expected = {
        "crm_status_id": crm_state.get("status_id"),
        "crm_status_name": crm_state.get("status_name"),
        "crm_status_semantic_id": crm_state.get("status_semantic_id") or "unknown",
        "applicable": bool(crm_state.get("is_closed_lost")),
    }
    mismatches = [
        f"closure_review.{field} must match deterministic CRM state: expected {value!r}, got {closure_review.get(field)!r}"
        for field, value in expected.items()
        if closure_review.get(field) != value
    ]
    if mismatches:
        raise AnalysisValidationError("Invalid lead CRM closure review: " + "; ".join(mismatches))


def build_prompt(
    lead_id: str,
    history_text: str,
    transcript_text: str,
    context_diagnostics_text: str,
    okf_sections: list[tuple[Path, str]],
    crm_state: dict[str, Any] | None = None,
) -> str:
    okf_text = "\n\n".join(f"### OKF FILE: {path.name}\n\n{text.strip()}" for path, text in okf_sections)
    crm_state_text = json.dumps(crm_state or {
        "status_id": None,
        "status_name": None,
        "status_semantic_id": "unknown",
        "is_closed_lost": False,
        "is_converted": False,
        "status_name_available": False,
    }, ensure_ascii=False, indent=2)
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
- Блок ТЕКУЩЕЕ СОСТОЯНИЕ CRM сформирован детерминированно из карточки лида и справочника этапов. Используй `is_closed_lost` для определения уже закрытого лида, а `status_name` — только как заявленную CRM-причину закрытия.
</grounding_rules>

<length_limits>
- summary/reason/description: максимум 2-3 коротких предложения.
- Списки what_done_well, missed_points, next_call_plan, manager_checklist: максимум 5 пунктов.
- manager_review_text: максимум 500 символов и 2-4 коротких предложения.
- Каждый из трёх готовых текстов клиенту: максимум 1200 символов.
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
11. Если лид требует уточнения, используй прогрессивную квалификацию: один управляемый контакт должен сначала получить ближайший достижимый результат текущего этапа, а после ответа или в продолжении разговора — добрать остальные важные факты и зафиксировать их в CRM. Не требуй от клиента закрыть все пробелы квалификации в первом письменном сообщении и не превращай последовательность в несвязанные действия.
12. rop_manager_message_block.manager_review_text — одно короткое профессиональное сообщение руководителя отдела продаж менеджеру, готовое к отправке без редактирования. Пиши непосредственно менеджеру на «ты», в спокойном рабочем тоне, а не о менеджере в третьем лице и не в формате аналитического отчёта. Сначала покажи понимание текущей ситуации, затем отметь конкретное подтверждённое сильное действие менеджера, если оно есть, и после этого прямо объясни один рекомендуемый следующий шаг и его смысл. Не придумывай похвалу ради формальности. Если квалификация неполная, кратко раздели ближайший результат первого касания и факты, которые менеджер должен добрать после ответа или в разговоре, чтобы он видел весь маршрут работы. Не повторяй списки «Сильные стороны» и «Слабые стороны», не ставь психологические диагнозы и не включай сюда срок/CRM-критерии SMART-задачи или готовые тексты клиенту.
13. rop_manager_message_block.message_to_manager — отдельная готовая SMART-задача менеджеру. Когда `manager_task_required=true`, в одной короткой формулировке должны быть конкретное действие, точный срок YYYY-MM-DD, полный список существенных недостающих квалификационных или технических фактов для этого этапа, ожидаемый факт в CRM и проверяемый результат. Не выдавай внутренний срок РОПа за обещание клиента. Когда `manager_task_required=false`, прямо напиши, что дополнительная задача не требуется, и используй `deadline=null`.
14. Когда `client_contact_required=true`, manager_action_block должен содержать ровно три готовых варианта первого обращения менеджера к клиенту: primary_text и два элемента backup_texts. Во всех трёх обязаны совпадать ближайшая цель касания, ключевые вопросы, факты и требуемый следующий шаг; меняется только тон подачи. Вариант 1 — деловой и прямой, вариант 2 — партнёрский и доброжелательный, вариант 3 — спокойный и консультативный. Оптимизируй каждый вариант как самостоятельное сообщение, которое можно отправить клиенту без редактирования; в разговоре оно служит вступлением, после которого менеджер продолжает квалификацию по manager_review_text и message_to_manager. Не превращай первое сообщение в анкету: выбери минимально достаточный запрос и один основной призыв к действию, который повышает вероятность ответа и продвигает текущий этап. При отложенном спросе или паузе сначала подтверди актуальность и согласуй месяц/дату следующего обсуждения; оставшиеся BANT- и технические вопросы перенеси на ответ клиента или разговор. Если клиент уже активно передаёт данные и следующий этап действительно заблокирован конкретным вводом, можно запросить этот ввод и срок его передачи. Не определяй DISC или иной психотип клиента, не дели варианты на письмо/мессенджер/звонок и не предлагай разные стратегии. Не включай в клиентские тексты инструкции менеджеру вроде «внеси в CRM».
15. manager_action_block.manager_checklist — короткий список CRM-фактов после контакта; не дублируй в нём задачу или тексты клиенту.
16. Если `is_closed_lost=true`, обязательно оцени корректность уже выполненного закрытия в closure_review. Не поручай менеджеру закрыть лид повторно.
17. Для `confirmed_correct` нужны согласующиеся CRM-этап и evidence из истории/коммуникаций. В этом случае `client_contact_required=false`, `manager_task_required=false`, `primary_text=null`, `backup_texts=[]`, `manager_checklist=[]`: не требуй повторного контакта с клиентом и не создавай клиентские тексты. Но это не отменяет отдельный CRM-контур отложенного спроса. Если одновременно категория C, `controlled_return_status=missing_in_crm` и есть `recommended_return_date`, отсутствие задачи возврата не делает закрытие спорным: сохрани `confirmed_correct`, не требуй контакта, но в `rop_manager_message_block` дай обязательное поручение менеджеру создать в CRM задачу возврата на `recommended_return_date`, поставь точный внутренний срок контроля YYYY-MM-DD, укажи ожидаемый CRM-факт и критерий выполнения; в этом специальном случае `rop_action.required=true`. Во всех остальных `confirmed_correct` используй `deadline=null`, `rop_action.required=false` и прямо напиши, что дополнительная задача и постановка на контроль не требуются.
18. Для `disputed` укажи конкретное противоречие между CRM-причиной и evidence. Для `insufficient_evidence` не объявляй закрытие ошибочным: поручи один содержательный контакт и фиксацию результата. В обоих случаях контакт требуется и сохраняются ровно три варианта обращения.
19. Если `is_closed_lost=false`, closure_review.verdict=`not_applicable`; текущая логика анализа, SMART-задачи и трёх клиентских вариантов не меняется.
20. Подтверждённый стоп-фактор важнее отсутствующих BANT-фактов. Если клиент прямо отказался от решения без обязательной функции, не придумывай отдельную технологическую схему или готовность рассматривать альтернативу без evidence.

<qualification_rules>
Сначала заполни qualification_assessment: четыре независимых критерия BANT, техническую применимость, коммерческую проверку бюджета нового оборудования, категорию лида и маршрут. Только затем продублируй категорию в legacy-поле lead_state.qualification, выбери loss_diagnosis.final_verdict и рекомендацию.

1. BANT — четыре независимых признака:
   - budget: реальный бюджет, финансовая возможность и готовность двигаться к договору с предоплатой;
   - authority: контакт является ЛПР либо подтверждённо влияет на решение;
   - need: конкретная актуальная потребность;
   - timeframe: назван срок закупки или запуска.
   Статусы критериев: confirmed — критерий доказательно подтверждён; not_confirmed — доступные факты не подтверждают критерий, но отрицательного ответа нет; negative — есть подтверждённый отрицательный ответ/стоп-фактор именно по критерию; unknown — данных недостаточно. Отсутствие информации всегда unknown или not_confirmed, но не negative.
   Для timeframe отдельно выбери purchase_window: up_to_60_days, days_61_to_89, months_3_to_12, over_12_months или unknown. Ровно 3 месяца относится к months_3_to_12. Не смешивай два разных срока: decision_timing — когда клиент примет решение, need_or_launch_timing — когда нужно оборудование или запуск. Для каждого укажи status=confirmed|not_confirmed|unknown и значение только по фактам; если срока нет, value=null.
2. Для каждого критерия верни заметное русское label, краткий summary, evidence, missing_facts и один конкретный next_question_or_action при нехватке данных. Общий bant.next_question оставь как один главный вопрос для обратной совместимости.
3. Техническая применимость оценивается отдельно от BANT по типу оборудования и известным параметрам из OKF technical_data.md. Ожидание заключения технического специалиста и нехватка параметров означают needs_technical_data/insufficient, а не техническую несовместимость и не категорию D. technical_mismatch допустим только при подтверждённом конкретном стоп-факторе из CRM-истории или транскрибации.
4. Коммерческая проверка относится только к явно названному бюджету нового оборудования. below_minimum и budget_below_new_equipment_minimum допустимы только при явно названной числовой сумме менее 1000000 рублей. Формулировки без точного числа вроде «десятки тысяч», «сотни тысяч», «дорого» или отказ от озвученной менеджером цены не являются названным бюджетом клиента: в таком случае ставь budget_named=false, new_equipment_budget_status=unknown и confirmed_budget_rub=null. Не извлекай, не округляй и не предполагай бюджет. Лизинг, рассрочка, аренда, б/у или более доступная комплектация могут быть рекомендацией, но не меняют категорию сами по себе.
5. Категория A: одновременно полный confirmed BANT, compatible, явно подтверждённый бюджет нового оборудования от 1000000 рублей и timeframe up_to_60_days.
6. Категория B: проект реален (need=confirmed), срок up_to_60_days или days_61_to_89, не хватает части BANT либо технических данных, и нет подтверждённого стоп-фактора.
7. Категория C: подтверждённая отложенная потребность со сроком months_3_to_12. Категория остаётся C, даже если менеджер не создал CRM-дело или задачу возврата. Существующий возврат подтверждай только CRM-evidence и храни его дату в controlled_return_date. Если действия в CRM нет, ставь controlled_return_status=missing_in_crm, controlled_return_date=null, маршрут violation и предлагай recommended_return_date как рекомендацию, а не как существующий факт. Отсутствие CRM-задачи возврата — это нарушение маршрута и обязательное CRM-поручение, но само по себе не повод объявлять корректное закрытие спорным или требовать новый контакт с клиентом. Даже если срок более 6 месяцев соответствует браковочной стадии текущей воронки, возврат должен остаться управляемым.
8. Категория D допустима только для одной или нескольких подтверждённых причин: timeframe_over_12_months, technical_mismatch, budget_below_new_equipment_minimum. Причины храни в lead_category.reason_codes; нехватка технических данных не является D.
9. Категория E допустима только для подтверждённой причины: spam, invalid_contact или call_cycle_completed_no_contact. Используй существующую рекомендацию поставщика из call_attempt_rules.md как цикл дозвона и не придумывай новый норматив. Пока цикл не завершён, ставь unknown, не E.
10. Категория unknown: нет содержательного контакта, реальность проекта не доказана, цикл дозвона не завершён или данных недостаточно для A–E. Обязательны missing_facts и конкретный next_step. Для категорий A, B, C и unknown поле lead_category.reason_codes всегда должно быть пустым списком []; оно предназначено только для подтверждённых причин D/E.
11. Категория и маршрут различаются. ordinary_deal разрешён только при полном confirmed BANT. op2 разрешён при ровно одном not_confirmed/unknown критерии BANT и отсутствии negative. Иначе используй clarification, auto_reminder, deferred_demand, disqualified или unknown. Если текущий маршрут нарушает правило, lead_route.status=violation.
12. Не смешивай качество лида с качеством обработки: loss_diagnosis независимо оценивает lead_quality, processing_quality, call_attempt_quality, next_step_quality и route_quality. Если менеджер не выяснил BANT, это не означает отрицательный BANT.
13. Для любого доуточнения сформируй одно поручение менеджеру: один контакт, конкретные вопросы, срок и CRM-факты для фиксации.
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
      "budget": {{"label": "Бюджет и финансовая готовность", "status": "confirmed|not_confirmed|negative|unknown", "summary": "краткий вывод", "evidence": ["факт из CRM или коммуникации"], "missing_facts": [], "next_question_or_action": "конкретный вопрос или null"}},
      "authority": {{"label": "ЛПР и влияние на решение", "status": "confirmed|not_confirmed|negative|unknown", "summary": "краткий вывод", "evidence": [], "missing_facts": [], "next_question_or_action": "конкретный вопрос или null"}},
      "need": {{"label": "Актуальная потребность", "status": "confirmed|not_confirmed|negative|unknown", "summary": "краткий вывод", "evidence": [], "missing_facts": [], "next_question_or_action": "конкретный вопрос или null"}},
      "timeframe": {{"label": "Срок решения и потребности", "status": "confirmed|not_confirmed|negative|unknown", "summary": "краткий вывод", "purchase_window": "up_to_60_days|days_61_to_89|months_3_to_12|over_12_months|unknown", "decision_timing_status": "confirmed|not_confirmed|unknown", "decision_timing": "срок/дата решения клиента или null", "need_or_launch_timing_status": "confirmed|not_confirmed|unknown", "need_or_launch_timing": "срок/дата потребности или запуска или null", "evidence": [], "missing_facts": [], "next_question_or_action": "конкретный вопрос или null"}},
      "overall_status": "confirmed|incomplete|negative|unknown",
      "missing_facts": ["что именно нужно выяснить"],
      "next_question": "один конкретный вопрос клиенту или null"
    }},
    "solution_fit": {{
      "equipment_type": "labeler|filling_line|block|unknown",
      "status": "compatible|not_compatible|needs_technical_data|unknown",
      "technical_data_status": "sufficient|insufficient|unknown",
      "reason_code": "technical_mismatch|unknown|null",
      "evidence": ["краткий факт из CRM или транскрибации"],
      "missing_facts": ["недостающий технический параметр"],
      "next_question_or_action": "конкретный вопрос или null"
    }},
    "commercial_fit": {{
      "new_equipment_budget_status": "sufficient|below_minimum|unknown",
      "budget_named": true,
      "applies_to_new_equipment": "JSON boolean true, JSON boolean false или строка unknown",
      "confirmed_budget_rub": "число или null",
      "new_equipment_minimum_rub": 1000000,
      "reason_code": "budget_below_new_equipment_minimum|unknown|null",
      "evidence": ["краткий факт из CRM или транскрибации"],
      "missing_facts": [],
      "next_question_or_action": "конкретный вопрос или null"
    }},
    "lead_category": {{
      "value": "A|B|C|D|E|unknown",
      "reason": "почему присвоена категория",
      "reason_codes": [],
      "bant_factors": ["как BANT повлиял на категорию"],
      "technical_factors": ["как техническая проверка повлияла на категорию"],
      "budget_factors": ["как бюджетная проверка повлияла на категорию"],
      "missing_facts": ["чего не хватает"],
      "next_step": "одно конкретное следующее действие"
    }},
    "lead_route": {{
      "current_route": "ordinary_deal|op2|clarification|auto_reminder|deferred_demand|disqualified|unknown",
      "recommended_route": "ordinary_deal|op2|clarification|auto_reminder|deferred_demand|disqualified|unknown",
      "status": "allowed|violation|needs_clarification|unknown",
      "reason": "почему маршрут корректен или нарушен",
      "controlled_return_required": false,
      "controlled_return_status": "confirmed_in_crm|missing_in_crm|needs_clarification|not_required",
      "controlled_return_date": "существующая дата CRM-дела/задачи или null",
      "recommended_return_date": "предлагаемая дата или null",
      "evidence": ["факт о текущем маршруте или следующем CRM-действии"]
    }}
  }},
  "activity_summary": {{
    "meaningful_contact": true,
    "summary": "что уже произошло по коммуникациям"
  }},
  "closure_review": {{
    "applicable": false,
    "crm_status_id": "код текущего этапа или null",
    "crm_status_name": "название текущего этапа или null",
    "crm_status_semantic_id": "F|S|P|unknown",
    "verdict": "confirmed_correct|disputed|insufficient_evidence|not_applicable",
    "reason": "почему закрытие подтверждено, спорно или не может быть проверено",
    "client_contact_required": true,
    "manager_task_required": true,
    "evidence": ["факты CRM и коммуникаций, подтверждающие вывод"]
  }},
  "rop_manager_message_block": {{
    "check_for_rop": "что конкретно РОПу проверить по лиду",
    "why_it_matters": "почему это влияет на потерю лида, скорость обработки или деньги",
    "manager_review_text": "краткий комментарий РОПа: понимание ситуации, подтверждённое хорошее действие и рекомендуемый следующий шаг",
    "message_to_manager": "короткая SMART-задача менеджеру: действие, точный срок, CRM-факт и проверяемый результат",
    "expected_crm_update": "какой факт должен появиться в CRM после действия менеджера",
    "deadline": "YYYY-MM-DD или null, если закрытие подтверждено и задача не требуется",
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
    "call_attempt_quality": "enough|not_enough|wrong_channel|not_applicable|unknown",
    "next_step_quality": "clear|missing|too_generic|unknown",
    "route_quality": "correct|violation|needs_clarification|unknown",
    "final_verdict": "bad_lead|bad_processing|data_gap|needs_nurture|ready_for_deal|technical_mismatch|budget_below_new_equipment_minimum|timeframe_over_12_months|no_contact_after_full_cycle|unknown",
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
    "cycle_status": "not_started|in_progress|completed|not_applicable|unknown",
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
    "channel_reason": "почему выбран канал для фактического контакта; варианты текста при этом остаются универсальными",
    "goal": "цель клиентского касания, если менеджеру нужно обратиться к клиенту",
    "primary_text": {{
      "type": "call_script|messenger|email",
      "subject": "тема, если она действительно нужна, иначе пустая строка",
      "title": "Деловой и прямой",
      "text": "готовый вариант 1 клиенту"
    }},
    "backup_texts": [
      {{"type": "messenger", "title": "Партнёрский и доброжелательный", "text": "готовый вариант 2 клиенту с тем же содержанием"}},
      {{"type": "messenger", "title": "Спокойный и консультативный", "text": "готовый вариант 3 клиенту с тем же содержанием"}}
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

## ТЕКУЩЕЕ СОСТОЯНИЕ CRM

{crm_state_text}

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
    lead_category = _report_dict(assessment.get("lead_category"))
    lead_route = _report_dict(assessment.get("lead_route"))
    lead_state = _report_dict(analysis.get("lead_state"))

    bant_items: list[str] = []
    for label, name in (
        ("Бюджет", "budget"),
        ("Полномочия", "authority"),
        ("Потребность", "need"),
        ("Срок", "timeframe"),
    ):
        item = _report_dict(bant.get(name))
        display_label = item.get("label") or label
        detail_lines = [
            f"- {display_label}: {_report_value(item.get('status'))}",
            f"  - Вывод: {_report_value(item.get('summary'))}",
            f"  - Доказательства:\n{indented_bullet_list(item.get('evidence'))}",
            f"  - Чего не хватает:\n{indented_bullet_list(item.get('missing_facts'))}",
            f"  - Вопрос/действие: {_report_value(item.get('next_question_or_action'))}",
        ]
        if name == "timeframe":
            detail_lines.insert(2, f"  - Горизонт: {_report_value(item.get('purchase_window'))}")
            detail_lines.insert(3, f"  - Решение клиента: {_report_value(item.get('decision_timing'))} ({_report_value(item.get('decision_timing_status'))})")
            detail_lines.insert(4, f"  - Оборудование/запуск нужны: {_report_value(item.get('need_or_launch_timing'))} ({_report_value(item.get('need_or_launch_timing_status'))})")
        bant_items.append("\n".join(detail_lines))

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
- Достаточность технических данных: {_report_value(solution_fit.get('technical_data_status'))}
- Причина: {_report_value(solution_fit.get('reason_code'))}
- Доказательства:
{bullet_list(solution_fit.get('evidence'))}
- Недостающие параметры:
{bullet_list(solution_fit.get('missing_facts'))}
- Вопрос/действие: {_report_value(solution_fit.get('next_question_or_action'))}

### Бюджет нового оборудования

- Подтверждённый бюджет: {_report_value(commercial_fit.get('confirmed_budget_rub'))}
- Бюджет назван: {_report_value(commercial_fit.get('budget_named'))}
- Относится к новому оборудованию: {_report_value(commercial_fit.get('applies_to_new_equipment'))}
- Минимальный порог: {_report_value(commercial_fit.get('new_equipment_minimum_rub'))}
- Статус: {_report_value(commercial_fit.get('new_equipment_budget_status'))}
- Причина: {_report_value(commercial_fit.get('reason_code'))}
- Доказательства:
{bullet_list(commercial_fit.get('evidence'))}
- Чего не хватает:
{bullet_list(commercial_fit.get('missing_facts'))}

### Категория лида

- Категория: {_report_value(lead_category.get('value') or lead_state.get('qualification'))}
- Причина: {_report_value(lead_category.get('reason') or lead_state.get('qualification_reason'))}
- Причины D/E: {', '.join(str(item) for item in lead_category.get('reason_codes', [])) or 'нет'}
- BANT-факторы:
{bullet_list(lead_category.get('bant_factors'))}
- Технические факторы:
{bullet_list(lead_category.get('technical_factors'))}
- Бюджетные факторы:
{bullet_list(lead_category.get('budget_factors'))}
- Недостающие факты:
{bullet_list(lead_category.get('missing_facts'))}
- Следующий шаг: {_report_value(lead_category.get('next_step'))}

### Маршрут лида

- Текущий маршрут: {_report_value(lead_route.get('current_route'))}
- Рекомендуемый маршрут: {_report_value(lead_route.get('recommended_route'))}
- Проверка маршрута: {_report_value(lead_route.get('status'))}
- Причина: {_report_value(lead_route.get('reason'))}
- Контролируемый возврат: {_report_value(lead_route.get('controlled_return_required'))}
- Статус возврата в CRM: {_report_value(lead_route.get('controlled_return_status'))}
- Существующая дата возврата в CRM: {_report_value(lead_route.get('controlled_return_date'))}
- Рекомендуемая дата возврата: {_report_value(lead_route.get('recommended_return_date'))}{next_action}"""


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
    closure_review = analysis.get("closure_review", {}) or {}
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
    client_message_options = [
        str(primary.get("text") or "").strip(),
        *[str(item.get("text") or "").strip() for item in backup_texts if isinstance(item, dict)],
    ]
    client_options_md = "\n".join(
        f"{index}. {item}" for index, item in enumerate(client_message_options, 1) if item
    )
    if closure_review.get("verdict") == "confirmed_correct" and not client_options_md:
        client_material = "- Контакт с клиентом: не требуется — закрытие подтверждено фактами."
    else:
        client_material = f"- Три варианта обращения менеджера к клиенту:\n{client_options_md or 'не указано'}"

    closure_section = ""
    if closure_review:
        closure_section = f"""

## Проверка закрытия лида

- Применима: {human_value(closure_review.get('applicable'))}
- CRM-этап: {closure_review.get('crm_status_name') or closure_review.get('crm_status_id') or 'не указано'}
- Вердикт: {closure_review.get('verdict', 'не указано')}
- Причина: {closure_review.get('reason', 'не указано')}
- Контакт с клиентом требуется: {human_value(closure_review.get('client_contact_required'))}
- Задача менеджеру требуется: {human_value(closure_review.get('manager_task_required'))}
- Основание:
{bullet_list(closure_review.get('evidence'))}
"""

    return f"""# Отчет РОПу по лиду {lead_id}

Ссылка в Bitrix: {bitrix_url or 'не указана'}
{limitations_section}

## Что сделать РОПу сейчас

- Проверить: {rop_manager.get('check_for_rop') or rop.get('text', 'не указано')}
- Почему это важно: {rop_manager.get('why_it_matters', 'не указано')}
- Комментарий РОПа менеджеру: {rop_manager.get('manager_review_text', 'не указано')}
{client_material}
- SMART-задача менеджеру: {rop_manager.get('message_to_manager', 'не указано')}
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
{closure_section}

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
- Корректность маршрута: {loss.get('route_quality', 'не указано')}
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
- Статус цикла дозвона: {call_recommendation.get('cycle_status', 'не указано')}
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
    crm_state = load_lead_crm_state(lead_dir)
    prompt = build_prompt(
        args.lead_id,
        history_text,
        transcript_text,
        context_diagnostics_text,
        okf_sections,
        crm_state,
    )
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

    emit_progress("lead", str(args.lead_id), "llm_analysis", detail="Анализирует OpenAI")
    try:
        analysis, metadata = call_validated_analysis_json(
            prompt,
            validator=lambda value: validate_lead_analysis_for_crm_state(value, crm_state),
            normalizer=normalize_analysis_for_validation,
            validation_error_types=(AnalysisValidationError,),
            model=args.model,
            retry_callback=retry_progress_callback(
                "lead", str(args.lead_id), "llm_analysis", detail="Запрос OpenAI"
            ),
            semantic_callback=retry_progress_callback(
                "lead", str(args.lead_id), "validation", detail="Проверяет ответ модели"
            ),
            analysis_caller=call_analysis_json,
        )
    except ValidatedAnalysisFailure as error:
        write_prompt_budget(prompt_budget_path, attach_response_metadata(prompt_budget, error.metadata))
        raw_path.write_text(error.raw_output_text, encoding="utf-8")
        error_payload = {
            "generated_at": generated_at,
            "lead_id": str(args.lead_id),
            "error": str(error),
            "model_metadata": {
                key: value for key, value in error.metadata.items() if key != "raw_output_text"
            },
        }
        if error.analysis is not None:
            error_payload["analysis"] = error.analysis
        save_json(
            error_path,
            error_payload,
        )
        print(f"Model analysis failed after correction attempt. Raw output saved: {raw_path}")
        print(f"Error details saved: {error_path}")
        raise

    write_prompt_budget(prompt_budget_path, attach_response_metadata(prompt_budget, metadata))
    emit_progress("lead", str(args.lead_id), "validation", status="done", detail="Ответ прошёл проверку")

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

    emit_progress("lead", str(args.lead_id), "report", detail="Формирует отчёт")
    save_json(analysis_path, output_payload)
    report_path.write_text(render_report(analysis, metadata, context_diagnostics_payload), encoding="utf-8")
    raw_path.write_text(metadata.get("raw_output_text", ""), encoding="utf-8")
    emit_progress("lead", str(args.lead_id), "done", status="done", detail="Отчёт готов")

    logger.info("Saved lead analysis JSON: %s", analysis_path)
    logger.info("Saved lead ROP report markdown: %s", report_path)
    logger.info("Saved raw model output: %s", raw_path)

    print(f"Analysis saved: {analysis_path}")
    print(f"ROP report saved: {report_path}")
    print(f"Estimated analysis cost: {format_usd_rub(metadata.get('estimated_cost_usd'), metadata.get('estimated_cost_rub'))}")


if __name__ == "__main__":
    main()
