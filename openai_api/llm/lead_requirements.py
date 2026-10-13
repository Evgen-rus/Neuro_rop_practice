"""Lead-only technical checklist and stage review contract."""

from typing import Any


TECHNICAL_ITEMS = {
    "filling_line": {
        "packaging": "Тара / этикетка / крышка: размеры и фото",
        "transparent_label": "Прозрачная этикетка",
        "date_batch": "Дата / номер партии",
        "product": "Вид продукта, вязкость и пенообразование",
        "performance": "Производительность и объём тары",
        "composition": "Состав линии",
        "timing": "Сроки",
        "budget": "Бюджет",
    },
    "labeler": {
        "container": "Вид тары",
        "packaging": "Тара / этикетка: размеры и фото",
        "performance": "Производительность",
        "transparent_label": "Прозрачная этикетка",
        "date_batch": "Дата / номер партии",
        "container_state": "Полная / пустая тара при обкатке",
        "timing": "Сроки",
        "budget": "Бюджет",
    },
}


def requirements_contract() -> dict[str, Any]:
    return {
        "technical_requirements": {
            "required_now": False,
            "readiness": "ready|incomplete|unknown",
            "reason": "Почему полное ТЗ уже нужно или пока идёт первичная квалификация",
            "equipment": [
                {"type": kind, "items": [
                    {"key": key, "label": label, "status": "collected|partial|missing|not_applicable",
                     "summary": "Что известно и чего не хватает", "evidence": [],
                     "next_question": "Конкретный вопрос или null"}
                    for key, label in items.items()
                ]}
                for kind, items in TECHNICAL_ITEMS.items()
            ],
        },
        "stage_review": {
            "current_stage_id": None, "current_stage_name": None,
            "verdict": "confirmed_correct|disputed|insufficient_evidence",
            "reason": "Соответствует ли стадия подтверждённой позиции клиента",
            "evidence": [], "recommended_stage": None,
        },
    }


REQUIREMENTS_RULES = """
Полноту ТЗ проверяй отдельно от категории АвтоBANT. Заполни technical_requirements и stage_review
внутри qualification_assessment по следующему контракту. Оставь только относящиеся к запросу
клиента типы оборудования; при неизвестном типе или отдельных узлах equipment=[] и readiness=unknown.
Для каждого выбранного типа обязательны все восемь пунктов. Не объединяй независимые пункты.
collected означает, что весь пункт подтверждён доступной историей/разговором; partial — часть
составного пункта подтверждена; missing — в доступных материалах нет подтверждения; not_applicable —
клиентский запрос явно исключает этот пункт, с evidence. Ответ «нет» на необходимость прозрачной
этикетки или даты/партии означает collected, а не missing. Для размеров и фото отличай обещание
передать от подтверждения передачи: «пришлю фото» не доказывает получения; «фото получил» в
комментарии менеджера — внутреннее свидетельство передачи, не анализ изображения. Мы не
проверяем содержание вложений и не видим фото; не утверждай, что изображение изучено или размеры
проверены визуально. Для упаковки перечисли каждую недостающую часть. Производительность линии
розлива должна быть привязана к объёму тары, одного числа шт./час недостаточно.
evidence содержит дату, источник и краткий подтверждающий факт, без выдуманных ссылок/таймкодов.
Полное ТЗ требуется перед предметным расчётом/подбором комплектации/КП (required_now=true),
если клиент идёт к этому шагу или менеджер уже начал его. При первичном BANT и согласованном
отложенном спросе required_now=false; пробелы информационные, сами по себе не доказывают плохую
работу. readiness=ready только при всех пунктах collected/not_applicable; иначе incomplete.
При отсутствии оборудования readiness=unknown. Пробелы превращай в конкретный следующий шаг,
не требуй от клиента всю анкету в первом сообщении и не выдумывай срок его обещания.
stage_review проверяет текущую стадию и открытого, и закрытого лида. current_stage_id/name
копируй из состояния CRM. disputed требует конкретного противоречия с evidence; отсутствие
ответа/транскрипта не доказывает неверную стадию. «Неактуально», покупка у другого поставщика и
подтверждённый срок более шести месяцев — разные ситуации. Не угадывай дату покупки из фразы
менеджера «позвоню через полгода». recommended_stage — только рекомендация понятным названием
или null; не выдумывай ID и не меняй CRM/список контролируемых стадий. Для закрытого лида verdict
и reason согласуй с closure_review: подтверждённое/спорное закрытие/недостаток данных.
"""


def validate_requirements(assessment: dict[str, Any], errors: list[str]) -> None:
    def text(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip()) and len(value) <= 2000

    def evidence(value: Any) -> bool:
        return isinstance(value, list) and len(value) <= 7 and all(text(item) for item in value)

    technical = assessment.get("technical_requirements")
    if not isinstance(technical, dict):
        errors.append("qualification_assessment.technical_requirements must be an object")
        return
    if not isinstance(technical.get("required_now"), bool) or not text(technical.get("reason")):
        errors.append("technical_requirements requires boolean required_now and non-empty reason")
    equipment = technical.get("equipment")
    if not isinstance(equipment, list) or len(equipment) > 2:
        errors.append("technical_requirements.equipment must be a list of at most two types")
        return
    kinds = []
    statuses = []
    for group in equipment:
        if not isinstance(group, dict) or not isinstance(group.get("type"), str) or group["type"] not in TECHNICAL_ITEMS:
            errors.append("technical_requirements has an invalid equipment type")
            continue
        kind = group["type"]
        kinds.append(kind)
        items = group.get("items")
        if not isinstance(items, list) or len(items) != 8:
            errors.append(f"technical_requirements.{kind} requires all eight items")
            continue
        keys = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                errors.append(f"technical_requirements.{kind} item must be an object with key")
                continue
            key = item["key"]
            keys.append(key)
            if key not in TECHNICAL_ITEMS[kind] or item.get("label") != TECHNICAL_ITEMS[kind].get(key):
                errors.append(f"technical_requirements.{kind} has an invalid key/label")
            status = item.get("status")
            if not isinstance(status, str) or status not in {"collected", "partial", "missing", "not_applicable"}:
                errors.append(f"technical_requirements.{kind}.{key} has an invalid status")
            statuses.append(status)
            if not text(item.get("summary")) or not evidence(item.get("evidence")):
                errors.append(f"technical_requirements.{kind}.{key} requires summary and evidence list")
            if status in {"collected", "partial", "not_applicable"} and not item.get("evidence"):
                errors.append(f"technical_requirements.{kind}.{key} requires supporting evidence")
            question = item.get("next_question")
            if question is not None and not text(question):
                errors.append(f"technical_requirements.{kind}.{key} has an invalid next_question")
            if status in {"partial", "missing"} and not text(question):
                errors.append(f"technical_requirements.{kind}.{key} requires a next_question")
        if len(set(keys)) != 8 or set(keys) != set(TECHNICAL_ITEMS[kind]):
            errors.append(f"technical_requirements.{kind} keys must be unique and complete")
    if len(set(kinds)) != len(kinds):
        errors.append("technical_requirements equipment types must be unique")
    expected = "unknown" if not equipment else "ready" if statuses and all(
        isinstance(status, str) and status in {"collected", "not_applicable"} for status in statuses
    ) else "incomplete"
    if technical.get("readiness") != expected:
        errors.append(f"technical_requirements.readiness must be {expected}")
    review = assessment.get("stage_review")
    if not isinstance(review, dict):
        errors.append("qualification_assessment.stage_review must be an object")
        return
    verdict = review.get("verdict")
    if not isinstance(verdict, str) or verdict not in {"confirmed_correct", "disputed", "insufficient_evidence"}:
        errors.append("stage_review has an invalid verdict")
    if not text(review.get("reason")) or not evidence(review.get("evidence")):
        errors.append("stage_review requires reason and evidence list")
    if verdict in {"confirmed_correct", "disputed"} and not review.get("evidence"):
        errors.append("stage_review verdict requires supporting evidence")
    for field in ("current_stage_id", "current_stage_name", "recommended_stage"):
        if review.get(field) is not None and not text(review[field]):
            errors.append(f"stage_review.{field} must be text or null")


def requirements_summary(assessment: dict[str, Any]) -> dict[str, Any]:
    technical = assessment.get("technical_requirements")
    stage = assessment.get("stage_review")
    if not isinstance(technical, dict) or not isinstance(stage, dict):
        return {}
    items = [item for group in technical.get("equipment", []) if isinstance(group, dict)
             for item in group.get("items", []) if isinstance(item, dict) and item.get("status") != "not_applicable"]
    collected = sum(item.get("status") == "collected" for item in items)
    partial = sum(item.get("status") == "partial" for item in items)
    return {
        "regulation_version": "3.0",
        "technical_collected_count": collected, "technical_total_count": len(items),
        "technical_partial_count": partial, "technical_required_now": technical.get("required_now") is True,
        "technical_readiness": technical.get("readiness"),
        "stage_verdict": stage.get("verdict"), "stage_reason": stage.get("reason"),
        "requires_attention": stage.get("verdict") in {"disputed", "insufficient_evidence"}
        or (technical.get("required_now") is True and technical.get("readiness") != "ready"),
    }


def render_requirements(assessment: dict[str, Any]) -> str:
    if not isinstance(assessment.get("autobant"), dict):
        return ""
    technical = assessment.get("technical_requirements", {})
    stage = assessment.get("stage_review", {})
    result = assessment.get("autobant_result", {})
    labels = {"collected": "Собрано", "partial": "Частично", "missing": "Не найдено в доступных материалах",
              "not_applicable": "Не применимо"}
    verdicts = {"confirmed_correct": "Стадия подтверждена", "disputed": "Стадия противоречит фактам",
                "insufficient_evidence": "Недостаточно данных для проверки стадии"}
    lines = ["## АвтоBANT 3.0", f"- Приоритет: {result.get('priority', 'не определён')}",
             f"- Основание: {result.get('reason', 'не указано')}",
             f"- Ценовой ориентир, не КП клиента: {result.get('reference_price_rub') or 'не определён'} ₽",
             f"- Следующее действие по регламенту: {result.get('action', 'не указано')}",
             "", "## Полнота ТЗ", f"- Полное ТЗ требуется сейчас: {'да' if technical.get('required_now') else 'нет'}",
             f"- Основание: {technical.get('reason', 'не указано')}"]
    for group in technical.get("equipment", []):
        lines.extend(["", "### " + ("Линия розлива" if group["type"] == "filling_line" else "Этикетировщик")])
        for item in group["items"]:
            lines.extend([f"- {item['label']} — {labels.get(item['status'], item['status'])}",
                          f"  {item['summary']}"])
            lines.extend(f"  Основание: {fact}" for fact in item["evidence"])
            if item.get("next_question"):
                lines.append(f"  Уточнить: {item['next_question']}")
    if not technical.get("equipment"):
        lines.append("Тип оборудования для чек-листа не определён; полнота ТЗ недоступна.")
    lines.extend(["", "## Соответствие стадии", f"- {verdicts.get(stage.get('verdict'), 'Нет данных')}",
                  f"- Причина: {stage.get('reason', 'не указано')}"])
    lines.extend(f"- Основание: {fact}" for fact in stage.get("evidence", []))
    if stage.get("recommended_stage"):
        lines.append(f"- Рекомендуемая стадия (без записи в CRM): {stage['recommended_stage']}")
    return "\n".join(lines)
