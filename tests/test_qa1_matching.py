from __future__ import annotations

import unittest

from src.mysql_query import ProcedureModel
from src.qa1_matching import choose_qa1_model, match_qa1_template


def _model(
    model_id: str,
    *,
    procedure_code: str = "",
    apply_range: str = "",
) -> ProcedureModel:
    return ProcedureModel(
        model_id,
        "关键零部件模板" + model_id,
        "-1",
        "-1",
        "1",
        "0",
        "2",
        procedure_code,
        apply_range,
    )


class Qa1MatchingTests(unittest.TestCase):
    def test_apply_range_is_first_level_and_keeps_unique_selection(self) -> None:
        result = match_qa1_template(
            "ABC002",
            (
                _model("1", apply_range="ABC001~ABC003"),
                _model("2", procedure_code="X-TEST#LB#####-21"),
            ),
        )
        self.assertEqual(result.matched_by, "apply_range")
        self.assertEqual(tuple(model.id for model in result.candidates), ("1",))
        self.assertEqual(result.selected_model.id, "1")

    def test_multiple_apply_range_candidates_are_not_silently_selected(self) -> None:
        result = match_qa1_template("ABC002", (_model("1", apply_range="ABC002"), _model("2", apply_range="ABC002")))
        self.assertTrue(result.requires_manual_selection)
        self.assertIsNone(result.selected_model)
        with self.assertRaises(ValueError):
            choose_qa1_model(result, None)

    def test_procedure_code_middle_segment_uses_one_uppercase_alphanumeric_per_hash(self) -> None:
        result = match_qa1_template(
            "TESTALB00001",
            (_model("3", procedure_code="DEMO-C-DEMOFORM-DEMOUNIT-TEST#LB#####-21"),),
        )
        self.assertEqual(result.matched_by, "procedure_code")
        self.assertTrue(result.is_unique)

    def test_legacy_hash_is_also_one_character_wildcard_in_second_level(self) -> None:
        result = match_qa1_template(
            "DEMOHCAA#AKB",
            (_model("31", procedure_code="DEMO-C-DEMOFORM-DEMOUNIT-DEMO#C######-21"),),
            item_code="DEMO-MAT-020",
        )
        self.assertEqual(result.matched_by, "procedure_code")
        self.assertEqual(result.selected_model.id, "31")

        # legacy # 也可以与模板中的固定大写字母相容，而不只是与模板通配位相容。
        fixed_template = match_qa1_template(
            "DEMOHCAA#AKB",
            (_model("32", procedure_code="DEMO-C-DEMOFORM-DEMOUNIT-DEMO#C##A###-21"),),
            item_code="DEMO-MAT-020",
        )
        self.assertTrue(fixed_template.is_unique)

    def test_second_level_wildcards_keep_length_fixed_chars_and_legacy_star_strict(self) -> None:
        model = _model("33", procedure_code="DEMO-C-DEMOFORM-DEMOUNIT-DEMO#C######-21")
        for legacy in (
            "DEMOHCAA#AK",   # 长度不等
            "DEMOHDAA#AKB",  # 固定 C 位冲突
            "DEMOHCAA*AKB",  # legacy * 不新增通配语义
        ):
            self.assertEqual(
                match_qa1_template(legacy, (model,), item_code="DEMO-MAT-020").matched_by,
                "no_match",
            )

    def test_second_level_multiple_candidates_stay_manual_and_apply_range_keeps_priority(self) -> None:
        code_models = (
            _model("34", procedure_code="DEMO-C-DEMOFORM-DEMOUNIT-DEMO#C######-21"),
            _model("35", procedure_code="OTHER-DEMO#C######-01"),
        )
        multiple = match_qa1_template("DEMOHCAA#AKB", code_models, item_code="DEMO-MAT-020")
        self.assertTrue(multiple.requires_manual_selection)
        self.assertIsNone(multiple.selected_model)

        range_model = _model("36", apply_range="适用于物项 DEMO-MAT-033")
        preferred = match_qa1_template(
            "DEMOHCAA#AKB",
            (range_model,) + code_models,
            item_code="DEMO-MAT-033",
        )
        self.assertEqual(preferred.matched_by, "apply_range")
        self.assertEqual(tuple(model.id for model in preferred.candidates), ("36",))

    def test_apply_range_extracts_bounded_codes_from_chinese_description(self) -> None:
        result = match_qa1_template(
            "DEMO-MAT-031",
            (_model("4", apply_range="适用于说明物项 DEMO-MAT-031/DEMO-MAT-009 的检查"),),
        )
        self.assertTrue(result.is_unique)
        # 边界要求避免把短码 9468 命中到更长数字串中。
        self.assertEqual(match_qa1_template("9468", (_model("4", apply_range="DEMO-MAT-031"),)).matched_by, "no_match")

    def test_procedure_code_supports_mixed_hash_and_star_placeholders(self) -> None:
        result = match_qa1_template(
            "TEST1D123456",
            (_model("5", procedure_code="DEMO-C-DEMOFORM-DEMOUNIT-TEST#D******-01"),),
        )
        self.assertTrue(result.is_unique)
        result = match_qa1_template(
            "FAKE39322341",
            (_model("6", procedure_code="DEMO-C-DEMOFORM-DEMOUNIT-FAKE********-01"),),
        )
        self.assertTrue(result.is_unique)

    def test_lowercase_and_star_are_not_guessed_as_hash_wildcards(self) -> None:
        models = (_model("3", procedure_code="DEMO-C-DEMOFORM-DEMOUNIT-TEST#LB#####-21"),)
        self.assertEqual(match_qa1_template("TESTaLB00001", models).matched_by, "no_match")
        self.assertEqual(match_qa1_template("TEST*LB00001", models).matched_by, "no_match")

    def test_business_item_code_precedes_legacy_procedure_code_and_handles_placeholder(self) -> None:
        """Excel D 物料编码命中适用范围时，不因 C 列 N/A 而误报未匹配。"""

        result = match_qa1_template(
            "N/A",
            (_model("7", apply_range="适用于关键零部件 DEMO-MAT-033/DEMO-MAT-007"),),
            item_code="DEMO-MAT-033",
        )
        self.assertEqual(result.matched_by, "apply_range")
        self.assertEqual(result.legacy_item_code, "N/A")
        self.assertEqual(tuple(model.id for model in result.candidates), ("7",))

    def test_legacy_procedure_code_is_second_level_only_after_range_miss(self) -> None:
        model = _model("8", procedure_code="DEMO-C-DEMOFORM-DEMOUNIT-TEST#LB#####-21")
        result = match_qa1_template("TESTALB00001", (model,), item_code="DEMO-MAT-020")
        self.assertEqual(result.matched_by, "procedure_code")
        self.assertEqual(result.selected_model.id, "8")

        # Placeholder C values are not allowed to become a wildcard/fallback key.
        blocked = match_qa1_template("N/A", (model,), item_code="DEMO-MAT-020")
        self.assertEqual(blocked.matched_by, "no_match")


if __name__ == "__main__":
    unittest.main()
