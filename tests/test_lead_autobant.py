import copy
import unittest

from openai_api.llm.lead_autobant import (
    AUTOBANT_D_REASONS,
    AUTOBANT_E_REASONS,
    AUTOBANT_PROMPT,
    AUTOBANT_SCHEMA,
    PRODUCTS,
    PROFILE,
    evaluate_autobant,
    validate_autobant,
)


def assessment() -> dict:
    value = copy.deepcopy(AUTOBANT_SCHEMA)
    value["custom_product"] = {
        "name": "Тестовая линия",
        "reference_price_rub": 1_000_000,
        "evidence": ["Клиент запросил тестовую линию"],
    }
    value["profile"]["fill"] = {"value": "f_bev", "evidence": ["Розлив воды в ПЭТ"]}
    value["timeline"] = {"value": "le60", "evidence": ["Нужно в течение 60 дней"]}
    value["authority"] = {"value": "final", "evidence": ["Собственник принимает решение"]}
    value["need"] = {"value": "concrete", "evidence": ["Нужна линия для текущего производства"]}
    value["production"] = {"value": "launched", "evidence": ["Производство уже работает"], "project_signals": []}
    value["tz"] = {"value": "yes", "evidence": ["Передали образец тары"]}
    return value


class LeadAutobantTests(unittest.TestCase):
    def test_catalog_and_schema_cover_the_source_contract(self) -> None:
        self.assertEqual(len(PRODUCTS), 16)
        self.assertEqual(PRODUCTS["fill_oil"]["reference_price_rub"], 5_000_000)
        self.assertEqual(PRODUCTS["fill_chem"]["price"], "≈ 5–7 млн ₽")
        self.assertEqual(PRODUCTS["lab_chz"]["lead"], "до 80 раб. дней")
        self.assertEqual(PRODUCTS["lab_chz"]["reference_price_rub"], 3_000_000)
        self.assertEqual(PROFILE["fill"]["f_gas"]["status"], "rej")
        self.assertIn("изобарический", PROFILE["fill"]["f_gas"]["note"])
        self.assertEqual(PROFILE["label"]["l_cone"]["status"], "warn")
        self.assertIn("анализ чертежей", PROFILE["label"]["l_cone"]["note"])
        self.assertEqual(PROFILE["label"]["unknown"]["status"], None)
        self.assertTrue(all("note" in item for family in PROFILE.values() for code, item in family.items() if code != "unknown"))
        self.assertIn("завершённого цикла связи", AUTOBANT_PROMPT)
        self.assertIn("e1: неверный номер можно указать только при прямом подтверждении", AUTOBANT_PROMPT)
        self.assertIn("e2: нет контакта/ответа только после подтверждённого завершённого цикла связи", AUTOBANT_PROMPT)
        self.assertIn("не бюджет клиента", AUTOBANT_PROMPT)
        self.assertIn("autobant_profile_label_rejected", AUTOBANT_PROMPT)
        self.assertEqual(validate_autobant(copy.deepcopy(AUTOBANT_SCHEMA), errors := []), None)
        self.assertEqual(errors, [])

    def test_all_a_conditions_and_exact_budget_boundaries(self) -> None:
        value = assessment()
        self.assertEqual(evaluate_autobant(value, 900_000, True)["category"], "A")

        cases = [
            (899_999, "slightly", "B"),
            (700_000, "slightly", "B"),
            (699_999, "notably", "C"),
            (500_000, "notably", "C"),
            (499_999, "far", "E"),
        ]
        for budget, expected_fit, expected_category in cases:
            with self.subTest(budget=budget):
                result = evaluate_autobant(value, budget, True)
                self.assertEqual(result["budget_fit"], expected_fit)
                self.assertEqual(result["category"], expected_category)
        self.assertEqual(evaluate_autobant(value, 900_000, True)["minimum_budget_rub"], 900_000)

    def test_budget_unknown_and_no_contact_do_not_create_a_bad_lead(self) -> None:
        value = assessment()
        for budget in (None, 0, -1, True, float("nan"), float("inf")):
            with self.subTest(budget=budget):
                result = evaluate_autobant(value, budget, True)
                self.assertEqual(result["budget_fit"], "unknown")
                self.assertIsNone(result["budget_ratio"])
                self.assertNotEqual(result["category"], "E")

        no_context = copy.deepcopy(AUTOBANT_SCHEMA)
        no_context["timeline"]["evidence"] = ["Транскрипт недоступен"]
        result = evaluate_autobant(no_context, None, False)
        self.assertEqual(result["category"], "unknown")
        self.assertEqual(result["reason_codes"], [])

    def test_category_ordering_keeps_only_the_winning_stop_reason(self) -> None:
        value = assessment()
        value["screening"] = [
            {"code": "e2", "evidence": ["Завершён цикл звонков без контакта"]},
            {"code": "d1", "evidence": ["Нет потенциала роста"]},
        ]
        value["performance_fill"] = {"value": 3000, "evidence": ["Требуется 3000 бутылок в час"]}
        result = evaluate_autobant(value, 100_000, False)
        self.assertEqual(result["category"], "E")
        self.assertEqual(result["reason_codes"], ["autobant_e2"])
        self.assertTrue(set(result["reason_codes"]) <= AUTOBANT_E_REASONS)

        value["screening"] = [{"code": "d2", "evidence": ["Под задачу продукт не подходит"]}]
        self.assertEqual(evaluate_autobant(value, 100_000, False)["category"], "D")
        self.assertTrue({"autobant_d2"} <= AUTOBANT_D_REASONS)

        value["screening"] = []
        value["profile"]["fill"] = {"value": "f_gas", "evidence": ["Нужен розлив газированных напитков"]}
        result = evaluate_autobant(value, 100_000, True)
        self.assertEqual(result["category"], "D")
        self.assertEqual(result["reason_codes"], ["autobant_profile_fill_rejected"])

        value["profile"]["fill"] = {"value": "f_bev", "evidence": ["Розлив воды в ПЭТ"]}
        result = evaluate_autobant(value, 100_000, True)
        self.assertEqual(result["reason_codes"], ["autobant_performance_over_limit"])
        self.assertTrue(set(result["reason_codes"]) <= AUTOBANT_E_REASONS)

    def test_c_reasons_and_contract_production_exception(self) -> None:
        value = assessment()
        value["production"] = {
            "value": "notlaunched", "evidence": ["Площадку ещё готовят"],
            "project_signals": ["Есть помещение"],
        }
        result = evaluate_autobant(value, 900_000, True)
        self.assertEqual((result["category"], result["reason"]), ("C", "Производство ещё не запущено"))

        value["production"] = {"value": "contract", "evidence": ["Производят на контрактной площадке"], "project_signals": []}
        self.assertEqual(evaluate_autobant(value, 900_000, True)["category"], "A")
        value["timeline"] = {"value": "3-12m", "evidence": ["Покупка планируется через 8 месяцев"]}
        result = evaluate_autobant(value, 900_000, True)
        self.assertEqual((result["category"], result["reason"]), ("C", "Длинный горизонт покупки (3–12 мес)"))

    def test_fill_performance_limit_is_inclusive(self) -> None:
        value = assessment()
        value["performance_fill"] = {"value": 2500, "evidence": ["До 2500 бутылок в час"]}
        self.assertEqual(evaluate_autobant(value, 900_000, True)["category"], "A")
        value["performance_fill"]["value"] = 2500.1
        result = evaluate_autobant(value, 900_000, True)
        self.assertEqual(result["category"], "E")
        self.assertEqual(result["reason_codes"], ["autobant_performance_over_limit"])

    def test_validation_rejects_bad_numbers_enums_duplicates_and_evidence(self) -> None:
        value = assessment()
        value["products"] = [
            {"id": "lab_round", "evidence": ["Нужен этикетировщик"]},
            {"id": "lab_round", "evidence": ["Повтор"]},
        ]
        value["profile"]["label"] = {"value": "made_up", "evidence": []}
        value["performance_fill"] = {"value": True, "evidence": ["Булево не число"]}
        value["custom_product"]["reference_price_rub"] = float("nan")
        value["need"]["evidence"] = []
        errors: list[str] = []
        validate_autobant(value, errors)
        self.assertTrue(any("must be unique" in error for error in errors))
        self.assertTrue(any("unsupported value" in error for error in errors))
        self.assertTrue(any("finite non-negative number" in error for error in errors))
        self.assertTrue(any("must not be empty" in error for error in errors))

    def test_validation_and_evaluation_are_safe_for_malformed_values(self) -> None:
        for malformed in (None, [], {"version": "3.0", "products": "bad"}):
            with self.subTest(malformed=type(malformed).__name__):
                errors: list[str] = []
                validate_autobant(malformed, errors)
                self.assertTrue(errors)
                self.assertEqual(evaluate_autobant(malformed, None, False)["category"], "unknown")


if __name__ == "__main__":
    unittest.main()
