"""Lead AutoBANT 3.0 source catalog, contract validation, and deterministic rules."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
from typing import Any


VERSION = "3.0"
PERF_LIMIT_FILL = 2500

PRODUCT_FAMILIES: dict[str, dict[str, Any]] = {
    "fill": {"name": "Линия розлива", "lead": "70–80 раб. дней", "lead_upper_days": 80},
    "label": {"name": "Этикетировщик", "lead": "50–60 раб. дней", "lead_upper_days": 60},
    "unit": {"name": "Отдельные узлы", "lead": "уточняется", "lead_upper_days": 0},
}

# Product names, displayed prices, and reference prices are copied from AutoBANT 3.0.
PRODUCTS: dict[str, dict[str, Any]] = {
    "fill_chem": {"name": "Линия розлива бытовой химии", "family": "fill", "price": "≈ 5–7 млн ₽", "reference_price_rub": 5_000_000, "range": True},
    "fill_water": {"name": "Линия розлива воды", "family": "fill", "price": "≈ 5 млн ₽", "reference_price_rub": 5_000_000, "range": False},
    "fill_oil": {"name": "Линия розлива масла", "family": "fill", "price": "≈ 5–7 млн ₽", "reference_price_rub": 5_000_000, "range": True},
    "lab_round": {"name": "Этикетировщик круговой этикетки", "family": "label", "price": "от 800 000 ₽", "reference_price_rub": 800_000, "range": False},
    "lab_side": {"name": "Этикетировщик боковых этикеток", "family": "label", "price": "от 1 700 000 ₽", "reference_price_rub": 1_700_000, "range": False},
    "lab_uni": {"name": "Этикетировщик универсальный (круг/бок)", "family": "label", "price": "от 1 800 000 ₽", "reference_price_rub": 1_800_000, "range": False},
    "lab_top": {"name": "Этикетировщик верхнего нанесения", "family": "label", "price": "от 750 000 ₽", "reference_price_rub": 750_000, "range": False},
    "lab_topbot": {"name": "Этикетировщик верхнее/нижнее нанесение", "family": "label", "price": "от 1 200 000 ₽", "reference_price_rub": 1_200_000, "range": False},
    "lab_custom": {"name": "Нестандартный этикетировщик", "family": "label", "price": "от 1 500 000 ₽", "reference_price_rub": 1_500_000, "range": False},
    "lab_chz": {"name": "Честный знак (маркировка)", "family": "label", "price": "от 3 000 000 ₽", "reference_price_rub": 3_000_000, "range": False, "lead": "до 80 раб. дней", "lead_upper_days": 80},
    "un_acc_table": {"name": "Стол накопительный", "family": "unit", "price": "от 170 000 ₽", "reference_price_rub": 170_000, "range": False},
    "un_feed_table": {"name": "Стол подающий", "family": "unit", "price": "от 180 000 ₽", "reference_price_rub": 180_000, "range": False},
    "un_blow_semi": {"name": "Выдувной полуавтомат", "family": "unit", "price": "от 350 000 ₽", "reference_price_rub": 350_000, "range": False},
    "un_blow_auto": {"name": "Выдувной автомат", "family": "unit", "price": "от 1 500 000 ₽", "reference_price_rub": 1_500_000, "range": False},
    "un_capper": {"name": "Укупор", "family": "unit", "price": "от 1 800 000 ₽", "reference_price_rub": 1_800_000, "range": False},
    "un_rinser": {"name": "Ополаскиватель тары", "family": "unit", "price": "от 1 500 000 ₽", "reference_price_rub": 1_500_000, "range": False},
}

# The source PROFILE matrix, reduced to the label and status used by its rules.
PROFILE: dict[str, dict[str, dict[str, str | None]]] = {
    "fill": {
        "f_bev": {"label": "Вода в ПЭТ, соки, морсы, вино, водка, коньяк", "status": "ok", "note": "Основное направление производства. Насосы: все типы (подбор в СС)."},
        "f_dairy": {"label": "Йогурт питьевой, кефир, ряженка (без кусочков)", "status": "ok", "note": "Строго однородная жидкая фракция, без кусочков. Насос: перистальтический или роторный."},
        "f_milk10": {"label": "Пастеризованное молоко (срок до 10 суток)", "status": "ok", "note": "Традиционный розлив. Температура продукта не более +4…+6 °C."},
        "f_oil": {"label": "Масла (подсолнечное, оливковое и др.)", "status": "ok", "note": "Средняя вязкость (100–500 cSt). Насосы: центробежный, шестерённый, роторный, поршневой."},
        "f_thick": {"label": "Джем, мёд, майонез, соусы (густые)", "status": "warn", "note": "Принимаем, но ОБЯЗАТЕЛЬНО запросить точную вязкость, температуру, включения. Насосы: шестерённый, роторный, поршневой."},
        "f_vinegar": {"label": "Уксус (пищевой и технический)", "status": "warn", "note": "Принимаем. Требуется AISI 316, Viton/PTFE, IP65/66, вытяжка обязательна (агрессивная среда)."},
        "f_fert": {"label": "Удобрения, промхимия", "status": "ok", "note": "Полная совместимость. Уточнить конкретный продукт, вязкость, агрессивность — звонок в СС."},
        "f_home": {"label": "Бытовая и автохимия (мыло, шампуни, незамерзайка)", "status": "ok", "note": "Стандартная совместимость, вязкость 50–500 cSt. Звонок в СС для выбора модели."},
        "f_cosm": {"label": "Косметика и ЛКМ (парфюмерия, лаки)", "status": "warn", "note": "ЛКМ — ТОЛЬКО поршневой насос (точность ±1%). Косметика — все типы в зависимости от вязкости."},
        "f_alco": {"label": "Спирты / спиртосодержащие (не растворители)", "status": "ok", "note": "Исключение из «горючих»: спирт и водку принимаем. Уточнить: это спирт или растворитель?"},
        "f_gas": {"label": "Газированные продукты (пиво, квас, газировка)", "status": "rej", "note": "Требуется изобарический тип розлива — не поставляем."},
        "f_esl": {"label": "Молоко ESL (срок 10–30 суток)", "status": "rej", "note": "Требует технологии Ultra-Clean (обработка тары и воздуха) — не поставляем."},
        "f_uht": {"label": "Стерилизованное молоко UHT (срок от 40 суток)", "status": "rej", "note": "Требует полной асептики и стерилизации (UHT). Направить к поставщикам асептического оборудования."},
        "f_jelly": {"label": "Холодец, студень, заливное", "status": "rej", "note": "Это фасовка, а не розлив: включения 10–15 мм и запайка лотков."},
        "f_acid": {"label": "Агрессивная химия (кислоты, щёлочи)", "status": "rej", "note": "Риск повреждения оборудования. Только после тестирования на площадке клиента и заключения специалистов."},
        "f_flam": {"label": "Горючие (бензин, растворители), яды", "status": "rej", "note": "Требует ATEX-оборудование. Исключение — спирты/водка (см. «Принимаем»)."},
        "f_resin": {"label": "Смолы, битум, полимеры (> 10 000 cSt)", "status": "rej", "note": "Вязкость выше 10 000 cSt превышает возможности стандартных насосов — только спецоборудование."},
        "f_glue": {"label": "Клей (ПВА, «Момент» и прочие)", "status": "rej", "note": "Клей густеет и выводит из строя насосы и наливатели. Клей фасуется, а не разливается."},
        "f_20l": {"label": "Розлив в ёмкости более 20 литров", "status": "rej", "note": "Конвейерная линия рассчитана до 20 л. Предложить несколько меньших тар вместо одной большой."},
        "f_med": {"label": "Медицинские препараты", "status": "rej", "note": "Требуются чистые комнаты ISO и сертификация GMP — вне компетенции. Направить к поставщикам фармоборудования."},
        "unknown": {"label": "Не определён", "status": None},
    },
    "label": {
        "l_cyl": {"label": "Цилиндрическая (круглая) тара, до 10 л", "status": "ok", "note": "Отклонение от вертикали до 0,2 мм, диаметр 10–150 мм. Самоклейка: AS-C; горячий клей: ЭМ-62; холодный клей: CG-80."},
        "l_cone": {"label": "Конусная (круглая) тара", "status": "warn", "note": "Только самоклейка (серия AS-C), обязательный анализ чертежей. Для холодного и горячего клея — отказ."},
        "l_sq": {"label": "Квадратная / канистра / овальная, до 20 л", "status": "ok", "note": "Серия AS-S (только самоклейка), обязателен верхний прижим. Ширина 30–90 мм (стандарт), до 300 мм спецзаказ."},
        "l_flat": {"label": "Плоская тара и коробки", "status": "ok", "note": "Серия AS-P (только самоклейка), нанесение сверху. Ширина объекта до 280 мм."},
        "l_unst": {"label": "Неустойчивая тара", "status": "warn", "note": "Принимаем по согласованию: требуется изготовление ложементов и согласование с Сервисной Службой."},
        "l_wet": {"label": "Тара с конденсатом / масляной плёнкой", "status": "warn", "note": "Клей не закрепится на влажной/жирной поверхности — обязательно предложить осушитель (воздушный нож) в КП."},
        "l_dust": {"label": "Пыльные продукты (цемент, строительные смеси)", "status": "rej", "note": "Пыль мешает адгезии и ломает узлы, гарантию не даём. Для этикетировки требуется чистое помещение."},
        "l_long": {"label": "Длинные тары / профили (длинномеры)", "status": "rej", "note": "Габариты превышают возможности стандартных линий, протяжные механизмы серии AS не рассчитаны на длинномеры."},
        "l_groove": {"label": "Этикетка «встык» в пазу (нет зазора 2 мм)", "status": "rej", "note": "Отказ до исправления макета: нужен отступ от краёв паза по 2 мм с каждой стороны."},
        "l_size": {"label": "Этикетка шириной < 10 мм или > 300 мм", "status": "rej", "note": "Стандартные узлы серии AS не справятся с протяжкой."},
        "l_vol": {"label": "Круглая тара > 10 л / канистра > 20 л", "status": "rej", "note": "Ограничение базы конвейера по весу и габаритам."},
        "l_offset": {"label": "Отступ от дна меньше нормы (клей < 20 мм, самоклейка < 5 мм)", "status": "rej", "note": "Для ЭМ-62 и CG-80 отступ строго не менее 20 мм от дна; для серии AS — не менее 5 мм от начала стенки."},
        "unknown": {"label": "Не определён", "status": None},
    },
}

SCREEN_CODES = frozenset({"e1", "e2", "e3", "e4", "d1", "d2", "d3"})
AUTOBANT_E_REASONS = frozenset({"autobant_e1", "autobant_e2", "autobant_e3", "autobant_e4", "autobant_performance_over_limit", "autobant_budget_far_below_reference"})
AUTOBANT_D_REASONS = frozenset({"autobant_d1", "autobant_d2", "autobant_d3", "autobant_profile_fill_rejected", "autobant_profile_label_rejected"})

_SCREEN_LABELS = {
    "e1": "Неверный номер / невозможно дозвониться",
    "e2": "Нет контакта / вообще не отвечает",
    "e3": "Спам / нецелевое обращение",
    "e4": "Бизнеса не существует",
    "d1": "Малый бизнес без потенциала роста",
    "d2": "Наш продукт не подходит под задачу",
    "d3": "Не соответствует профилю клиента",
}
_CATEGORY_INFO = {
    "A": ("Высокий приоритет (P1)", "Выставить КП, назначить встречу, произвести расчёт."),
    "B": ("Средний приоритет (P2)", "Дожим, уточнение деталей и перевод в категорию A."),
    "C": ("Низкий приоритет (P3)", "Зафиксировать дату возврата и триггер. Поставить на автонапоминание с задачей."),
    "D": ("Нецелевой", "Оставить в базе, но не тратить время на личное общение."),
    "E": ("Брак", "Удаление из воронки / очистка базы."),
}
_EVIDENCE_LIMIT = 7
_EVIDENCE_TEXT_LIMIT = 500


def _render_prompt_catalog() -> str:
    product_lines = [
        f"- {item_id}: {item['name']} — {item['price']}; ориентир {item['reference_price_rub']} ₽; "
        f"срок {item.get('lead', PRODUCT_FAMILIES[item['family']]['lead'])}"
        for item_id, item in PRODUCTS.items()
    ]
    profile_lines = [
        f"- {family}.{item_id}: {item['label']} — {item['status'] or 'unknown'}"
        + (f"; {item['note']}" if item.get("note") else "")
        for family, items in PROFILE.items()
        for item_id, item in items.items()
    ]
    return "\n".join((
        "AutoBANT 3.0. Каталог ниже служит только справочником цены и совместимости; он не доказывает факты о конкретном клиенте.",
        "Выбирай продукты и значения профиля только при прямом подтверждении в источниках. На каждый выбранный факт укажи короткие evidence-фрагменты; не выводи отказ из отсутствующих или недоступных данных.",
        "e1: неверный номер можно указать только при прямом подтверждении, что номер неверен; вариант «невозможно дозвониться» требует подтверждённого завершённого цикла связи. e2: нет контакта/ответа только после подтверждённого завершённого цикла связи. Одна неотвеченная попытка, отсутствие записи/транскрипта или недоступный источник не являются e1/e2 и не означают категорию E.",
        "reference_price_rub продукта — только каталожный ориентир. reference_price_rub custom_product — подтверждённая цена по коммерческому предложению/внутренней цене оборудования, не бюджет клиента. Бюджет клиента поступает отдельно из confirmed_budget_rub; не подменяй его ценой продукта.",
        "Категории по порядку: E screening; D screening; D при профиле со status rej; E при производительности розлива > 2500 бут./ч; E при бюджете < 50% суммы ориентиров; A только при бюджете >= 90% и всех пяти условиях: le60, final, concrete, launched или contract, tz=yes; затем C при бюджете < 70%, либо notlaunched, либо 3-12m; иначе B. Пороговые сравнения точные, без округления.",
        "Коды причин детерминированного результата: E screening e1/e2/e3/e4 → autobant_e1/autobant_e2/autobant_e3/autobant_e4; D screening d1/d2/d3 → autobant_d1/autobant_d2/autobant_d3; отклонённый профиль розлива/этикетировки → autobant_profile_fill_rejected/autobant_profile_label_rejected; производительность >2500 → autobant_performance_over_limit; бюджет <50% ориентира → autobant_budget_far_below_reference. reason_codes пуст для A/B/C/unknown.",
        "Если нет содержательного контакта и подтверждённых фактов, верни только unknown-значения и пустые evidence; классификация unknown. Не превращай попытку связи или недоступный транскрипт в плохой лид.",
        "В screening используй code и evidence. В products — уникальные id из каталога и evidence. custom_product — null или {name, reference_price_rub, evidence}. Для profile.fill и profile.label используй код из матрицы или unknown. Все enum-поля имеют evidence-массив; у неизвестного значения он может быть пустым. performance_fill.value — конечное число >= 0 или null.",
        "Production: contract означает подтверждённое контрактное производство и засчитывается как production A; project_signals содержит только подтверждённые сигналы проекта. Не приравнивай наличие сигнала к запуску.",
        "Верни JSON строго по приложенному schema-example: он задаёт все поля и вложенность. Примерные enum-значения и пустые evidence в нём заменяй только подтверждёнными данными; не добавляй поля.",
        "Допустимые значения: timeline le60|1-4m|3-12m|afterkp|unknown; authority final|notfinal|none|unknown; need concrete|uncertain|interest|unknown; production launched|contract|notlaunched|unknown; tz yes|partial|no|unknown; screening e1|e2|e3|e4|d1|d2|d3. Profile использует код из PROFILE для своей семьи или unknown.",
        "PRODUCTS:", *product_lines, "PROFILE:", *profile_lines,
    ))


AUTOBANT_SCHEMA: dict[str, Any] = {
    "version": VERSION,
    "products": [],
    "custom_product": None,
    "screening": [],
    "profile": {
        "fill": {"value": "unknown", "evidence": []},
        "label": {"value": "unknown", "evidence": []},
    },
    "timeline": {"value": "unknown", "evidence": []},
    "authority": {"value": "unknown", "evidence": []},
    "need": {"value": "unknown", "evidence": []},
    "production": {"value": "unknown", "evidence": [], "project_signals": []},
    "tz": {"value": "unknown", "evidence": []},
    "performance_fill": {"value": None, "evidence": []},
}

AUTOBANT_PROMPT = _render_prompt_catalog()


def _append(errors: list[str], message: str) -> None:
    if isinstance(errors, list):
        errors.append(message)


def _expect_object(value: Any, path: str, keys: set[str], errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        _append(errors, f"{path} must be an object")
        return {}
    for key in keys - value.keys():
        _append(errors, f"{path}.{key} is required")
    for key in value.keys() - keys:
        _append(errors, f"{path}.{key} is not allowed")
    return value


def _valid_evidence(value: Any, path: str, errors: list[str], *, required: bool) -> bool:
    if not isinstance(value, list):
        _append(errors, f"{path} must be a list")
        return False
    if len(value) > _EVIDENCE_LIMIT:
        _append(errors, f"{path} may contain at most {_EVIDENCE_LIMIT} items")
    valid = True
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip() or len(item) > _EVIDENCE_TEXT_LIMIT:
            _append(errors, f"{path}[{index}] must be non-empty text of at most {_EVIDENCE_TEXT_LIMIT} characters")
            valid = False
    if required and not value:
        _append(errors, f"{path} must not be empty")
        valid = False
    return valid


def _valid_number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _validate_fact(
    value: Any,
    path: str,
    allowed: set[str],
    errors: list[str],
) -> str | None:
    item = _expect_object(value, path, {"value", "evidence"}, errors)
    choice = item.get("value")
    valid_choice = isinstance(choice, str) and choice in allowed
    if not valid_choice:
        _append(errors, f"{path}.value has an unsupported value")
    _valid_evidence(item.get("evidence"), f"{path}.evidence", errors, required=valid_choice and choice != "unknown")
    return choice if valid_choice else None


def validate_autobant(value: Any, errors: list[str]) -> None:
    """Append structural contract errors without assuming model output is well-shaped."""
    root_keys = {
        "version", "products", "custom_product", "screening", "profile", "timeline",
        "authority", "need", "production", "tz", "performance_fill",
    }
    root = _expect_object(value, "qualification_assessment.autobant", root_keys, errors)
    if root.get("version") != VERSION:
        _append(errors, f"qualification_assessment.autobant.version must be {VERSION}")

    products = root.get("products")
    if not isinstance(products, list):
        _append(errors, "qualification_assessment.autobant.products must be a list")
        products = []
    if len(products) > len(PRODUCTS):
        _append(errors, f"qualification_assessment.autobant.products may contain at most {len(PRODUCTS)} items")
    seen_products: set[str] = set()
    for index, raw in enumerate(products):
        path = f"qualification_assessment.autobant.products[{index}]"
        item = _expect_object(raw, path, {"id", "evidence"}, errors)
        product_id = item.get("id")
        if not isinstance(product_id, str) or product_id not in PRODUCTS:
            _append(errors, f"{path}.id is not in the product catalog")
        elif product_id in seen_products:
            _append(errors, f"{path}.id must be unique")
        else:
            seen_products.add(product_id)
        _valid_evidence(item.get("evidence"), f"{path}.evidence", errors, required=True)

    custom = root.get("custom_product")
    if custom is not None:
        path = "qualification_assessment.autobant.custom_product"
        item = _expect_object(custom, path, {"name", "reference_price_rub", "evidence"}, errors)
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            _append(errors, f"{path}.name must be non-empty text of at most 120 characters")
        if _valid_number(item.get("reference_price_rub")) is None:
            _append(errors, f"{path}.reference_price_rub must be a finite non-negative number")
        _valid_evidence(item.get("evidence"), f"{path}.evidence", errors, required=True)

    screening = root.get("screening")
    if not isinstance(screening, list):
        _append(errors, "qualification_assessment.autobant.screening must be a list")
        screening = []
    if len(screening) > len(SCREEN_CODES):
        _append(errors, f"qualification_assessment.autobant.screening may contain at most {len(SCREEN_CODES)} items")
    seen_screening: set[str] = set()
    for index, raw in enumerate(screening):
        path = f"qualification_assessment.autobant.screening[{index}]"
        item = _expect_object(raw, path, {"code", "evidence"}, errors)
        code = item.get("code")
        if not isinstance(code, str) or code not in SCREEN_CODES:
            _append(errors, f"{path}.code has an unsupported value")
        elif code in seen_screening:
            _append(errors, f"{path}.code must be unique")
        else:
            seen_screening.add(code)
        _valid_evidence(item.get("evidence"), f"{path}.evidence", errors, required=True)

    profile = _expect_object(root.get("profile"), "qualification_assessment.autobant.profile", {"fill", "label"}, errors)
    for family in ("fill", "label"):
        _validate_fact(profile.get(family), f"qualification_assessment.autobant.profile.{family}", set(PROFILE[family]), errors)

    _validate_fact(root.get("timeline"), "qualification_assessment.autobant.timeline", {"le60", "1-4m", "3-12m", "afterkp", "unknown"}, errors)
    _validate_fact(root.get("authority"), "qualification_assessment.autobant.authority", {"final", "notfinal", "none", "unknown"}, errors)
    _validate_fact(root.get("need"), "qualification_assessment.autobant.need", {"concrete", "uncertain", "interest", "unknown"}, errors)
    production = _expect_object(root.get("production"), "qualification_assessment.autobant.production", {"value", "evidence", "project_signals"}, errors)
    production_value = production.get("value")
    production_valid = isinstance(production_value, str) and production_value in {"launched", "contract", "notlaunched", "unknown"}
    if not production_valid:
        _append(errors, "qualification_assessment.autobant.production.value has an unsupported value")
    _valid_evidence(production.get("evidence"), "qualification_assessment.autobant.production.evidence", errors, required=production_valid and production_value != "unknown")
    _valid_evidence(production.get("project_signals"), "qualification_assessment.autobant.production.project_signals", errors, required=False)
    _validate_fact(root.get("tz"), "qualification_assessment.autobant.tz", {"yes", "partial", "no", "unknown"}, errors)

    performance = _expect_object(root.get("performance_fill"), "qualification_assessment.autobant.performance_fill", {"value", "evidence"}, errors)
    performance_value = performance.get("value")
    if performance_value is not None and _valid_number(performance_value) is None:
        _append(errors, "qualification_assessment.autobant.performance_fill.value must be null or a finite non-negative number")
    _valid_evidence(performance.get("evidence"), "qualification_assessment.autobant.performance_fill.evidence", errors, required=performance_value is not None)


def _number_output(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    result = float(value)
    return result if math.isfinite(result) else None


def _empty_result(budget_fit: str = "unknown") -> dict[str, Any]:
    return {
        "category": "unknown",
        "reason_codes": [],
        "reference_price_rub": None,
        "budget_ratio": None,
        "budget_fit": budget_fit,
        "minimum_budget_rub": None,
        "priority": "Не определён",
        "action": "Уточнить подтверждённые данные о лиде.",
        "reason": "Недостаточно подтверждённых данных для оценки.",
    }


def evaluate_autobant(autobant: Any, budget_rub: Any, meaningful_contact: bool) -> dict[str, Any]:
    """Apply AutoBANT 3.0 ordering without rounding threshold comparisons."""
    errors: list[str] = []
    validate_autobant(autobant, errors)
    if errors:
        return _empty_result()

    reference = sum((Decimal(PRODUCTS[item["id"]]["reference_price_rub"]) for item in autobant["products"]), Decimal(0))
    custom = autobant["custom_product"]
    if custom is not None:
        custom_reference = _valid_number(custom["reference_price_rub"])
        if custom_reference is not None and custom_reference > 0:
            reference += custom_reference
    has_product = reference > 0
    reference_output = _number_output(reference) if has_product else None
    minimum_output = _number_output(reference * Decimal("0.9")) if has_product else None

    budget = _valid_number(budget_rub)
    if not has_product:
        budget_fit = "noprod"
        ratio = None
    elif budget is None or budget <= 0:
        # AutoBANT treats a missing/zero budget as empty, not as a far-below-budget stop.
        budget_fit = "unknown"
        ratio = None
    else:
        ratio = budget / reference
        if ratio >= Decimal("0.9"):
            budget_fit = "fit"
        elif ratio >= Decimal("0.7"):
            budget_fit = "slightly"
        elif ratio >= Decimal("0.5"):
            budget_fit = "notably"
        else:
            budget_fit = "far"

    facts = (
        autobant["profile"]["fill"]["value"], autobant["profile"]["label"]["value"],
        autobant["timeline"]["value"], autobant["authority"]["value"], autobant["need"]["value"],
        autobant["production"]["value"], autobant["tz"]["value"],
    )
    has_evidence_fact = bool(autobant["products"] or custom is not None or autobant["screening"])
    has_evidence_fact = has_evidence_fact or any(value != "unknown" for value in facts)
    has_evidence_fact = has_evidence_fact or autobant["performance_fill"]["value"] is not None

    screening = {item["code"] for item in autobant["screening"]}
    e_screen = sorted(code for code in screening if code.startswith("e"))
    d_screen = sorted(code for code in screening if code.startswith("d"))
    rejected_profiles = [
        family for family in ("fill", "label")
        if PROFILE[family][autobant["profile"][family]["value"]]["status"] == "rej"
    ]
    performance = _valid_number(autobant["performance_fill"]["value"])
    over_performance = performance is not None and performance > PERF_LIMIT_FILL
    no_meaningful_contact = meaningful_contact is not True
    if no_meaningful_contact and not has_evidence_fact:
        return _empty_result("noprod" if not has_product else budget_fit)

    category = "B"
    reason_codes: list[str] = []
    reason = "Частичный BANT — дожать до A"
    if e_screen:
        category = "E"
        reason_codes = [f"autobant_{code}" for code in e_screen]
        reason = "Стоп-фактор по контакту"
    elif d_screen:
        category = "D"
        reason_codes = [f"autobant_{code}" for code in d_screen]
        reason = "Отмечен как нецелевой"
    elif rejected_profiles:
        category = "D"
        reason_codes = [f"autobant_profile_{family}_rejected" for family in rejected_profiles]
        labels = [PROFILE[family][autobant["profile"][family]["value"]]["label"] for family in rejected_profiles]
        reason = "Не наш профиль производства: " + "; ".join(labels)
    elif over_performance:
        category = "E"
        reason_codes = ["autobant_performance_over_limit"]
        reason = f"Производительность выше {PERF_LIMIT_FILL} бут./час — вне возможностей"
    elif budget_fit == "far":
        category = "E"
        reason_codes = ["autobant_budget_far_below_reference"]
        reason = "Бюджет сильно ниже цены (менее 50% ориентира)"
    elif (
        budget_fit == "fit"
        and autobant["timeline"]["value"] == "le60"
        and autobant["authority"]["value"] == "final"
        and autobant["need"]["value"] == "concrete"
        and autobant["production"]["value"] in {"launched", "contract"}
        and autobant["tz"]["value"] == "yes"
    ):
        category = "A"
        reason = "Бюджет в вилке, срок до 60 дней, финальный ЛПР, конкретная задача, производство запущено или контрактное, ТЗ готово"
    elif budget_fit == "notably":
        category = "C"
        reason = "Бюджет заметно ниже вилки"
    elif autobant["production"]["value"] == "notlaunched":
        category = "C"
        reason = "Производство ещё не запущено"
    elif autobant["timeline"]["value"] == "3-12m":
        category = "C"
        reason = "Длинный горизонт покупки (3–12 мес)"
    elif budget_fit == "slightly":
        reason = "Бюджет немного ниже вилки — дожать"

    priority, action = _CATEGORY_INFO.get(category, ("Не определён", "Уточнить подтверждённые данные о лиде."))
    return {
        "category": category,
        "reason_codes": reason_codes,
        "reference_price_rub": reference_output,
        "budget_ratio": _number_output(ratio),
        "budget_fit": budget_fit,
        "minimum_budget_rub": minimum_output,
        "priority": priority,
        "action": action,
        "reason": reason,
    }
