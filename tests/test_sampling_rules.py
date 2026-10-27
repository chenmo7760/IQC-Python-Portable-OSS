from __future__ import annotations

import unittest

from src.sampling_rules import SamplingDecision, SamplingRuleError, _resolved_static_sample, resolve_acceptance_standard, resolve_sampling


PLAN = "GB2828 正常检查一次抽样方案：IL=Ⅰ"
STANDARD = "□QA1 ：AQL=1.0          □其他：AQL=1.5"


class SamplingRuleTests(unittest.TestCase):
    def test_qa1_item_uses_qa1_aql_and_status_zero(self) -> None:
        self.assertEqual(
            resolve_sampling(
                check_plan=PLAN,
                accept_standard=STANDARD,
                item_name="导线2.5mm²棕色 低烟无卤阻燃QA1",
                quantity="1900.00",
            ),
            SamplingDecision(check_number=50, accept_standard=0),
        )

    def test_non_qa1_items_use_other_aql_and_status_one(self) -> None:
        self.assertEqual(
            resolve_sampling(
                check_plan=PLAN,
                accept_standard=STANDARD,
                item_name="双绞线2×1×1.0mm²低烟无卤阻燃QA4",
                quantity="100.00",
            ),
            SamplingDecision(check_number=8, accept_standard=1),
        )
        self.assertEqual(
            resolve_sampling(
                check_plan=PLAN,
                accept_standard=STANDARD,
                item_name="信号线0.5mm² 黑色 低烟无卤阻燃QA3",
                quantity="1000.00",
            ),
            SamplingDecision(check_number=32, accept_standard=1),
        )

    def test_wdz_rows_use_large_batch_codes(self) -> None:
        self.assertEqual(
            resolve_sampling(
                check_plan=PLAN,
                accept_standard=STANDARD,
                item_name="导线0.75mm² 白 WDZ",
                quantity="8000.00",
            ),
            SamplingDecision(check_number=80, accept_standard=1),
        )
        self.assertEqual(
            resolve_sampling(
                check_plan=PLAN,
                accept_standard=STANDARD,
                item_name="导线0.75mm² 红 WDZ",
                quantity="1500.00",
            ),
            SamplingDecision(check_number=50, accept_standard=1),
        )

    def test_zero_defect_and_non_sampling_paths_keep_legacy_minus_one(self) -> None:
        self.assertEqual(
            resolve_sampling(
                check_plan="抽样方案：正常",
                accept_standard="零缺陷",
                item_name="普通物料",
                quantity=10,
            ),
            SamplingDecision(check_number=None, accept_standard=-1),
        )

    def test_numeric_status_snapshot_remains_compatible(self) -> None:
        self.assertEqual(
            resolve_sampling(
                check_plan=-1,
                accept_standard=0,
                item_name="示例物料",
                quantity=23,
            ),
            SamplingDecision(check_number=None, accept_standard=0),
        )
        self.assertEqual(
            resolve_sampling(
                check_plan=-1,
                accept_standard=0,
                item_name="示例物料",
                quantity=1,
            ),
            SamplingDecision(check_number=1, accept_standard=0),
        )

    def test_quantity_one_with_empty_acceptance_still_has_no_option(self) -> None:
        self.assertEqual(
            resolve_sampling(
                check_plan="不适用",
                accept_standard="",
                item_name="示例物料",
                quantity=1,
            ),
            SamplingDecision(check_number=1, accept_standard=-1),
        )

    def test_single_items_choose_acceptance_independently_of_quantity(self) -> None:
        for name, status in (("机箱|QA4", 1), ("机箱|QA1", 0), ("机箱|QA10", 1)):
            with self.subTest(name=name):
                self.assertEqual(resolve_sampling(
                    check_plan="GB2828 正常检查一次抽样方案：IL=S-1",
                    accept_standard=STANDARD, item_name=name, quantity=1,
                ), SamplingDecision(1, status))

    def test_other_sampling_paths_keep_independent_acceptance(self) -> None:
        for name, status in (("机箱|QA1", 0), ("机箱|QA4", 1)):
            for plan, quantity, count in (
                ("加严", 100, 20), ("放宽", 100, 5), ("不适用", 100, None),
                ("GB2828 首批正常 IL=Ⅰ", 100, None),
                ("GB2828 正常 IL=Ⅲ", 500001, None), (PLAN, 0, None),
            ):
                with self.subTest(name=name, plan=plan, quantity=quantity):
                    self.assertEqual(resolve_sampling(
                        check_plan=plan, accept_standard=STANDARD, item_name=name, quantity=quantity,
                    ), SamplingDecision(count, status))

    def test_acceptance_selection_does_not_require_a_sampling_aql(self) -> None:
        standard = "□QA1：AQL=1.25 □其他：AQL=1.75"
        self.assertEqual(resolve_acceptance_standard(accept_standard=standard, item_name="机箱|QA4"), 1)
        # 单件和特殊抽样不依赖这些不支持的 AQL 数值，保持原检查数量行为。
        for plan, quantity, count in ((PLAN, 1, 1), ("加严", 100, 20)):
            self.assertEqual(resolve_sampling(
                check_plan=plan, accept_standard=standard, item_name="机箱|QA4", quantity=quantity,
            ), SamplingDecision(count, 1))
        # 正常抽样仍必须验证 AQL，不因独立选择而猜测检查数量。
        with self.assertRaises(SamplingRuleError):
            resolve_sampling(check_plan=PLAN, accept_standard=standard, item_name="机箱|QA4", quantity=100)

    def test_no_dual_option_does_not_invent_an_acceptance_selection(self) -> None:
        for standard in (None, "", "零缺陷", "加严", "AQL=4.0", "□QA1：AQL=2.5"):
            with self.subTest(standard=standard):
                self.assertEqual(resolve_acceptance_standard(accept_standard=standard, item_name="机箱|QA4"), -1)
                self.assertEqual(resolve_sampling(
                    check_plan=PLAN, accept_standard=standard, item_name="机箱|QA4", quantity=1,
                ), SamplingDecision(1, -1))

    def test_tight_acceptance_table_uses_every_batch_boundary(self) -> None:
        rows = (
            (0, 1, 1),
            (2, 8, 2),
            (9, 15, 3),
            (16, 25, 5),
            (26, 50, 8),
            (51, 90, 13),
            (91, 150, 20),
            (151, 280, 32),
            (281, 500, 50),
            (501, 1200, 80),
            (1201, 3200, 125),
            (3201, 10000, 200),
            (10001, 35000, 315),
            (35001, 150000, 500),
            (150001, 500000, 800),
            (500001, None, 1250),
        )
        for minimum, maximum, expected in rows:
            quantities = (minimum,) if maximum is None else (minimum, maximum)
            for quantity in quantities:
                with self.subTest(quantity=quantity):
                    self.assertEqual(
                        resolve_sampling(
                            check_plan="不适用",
                            accept_standard="加严",
                            item_name="示例物料",
                            quantity=quantity,
                        ),
                        SamplingDecision(check_number=expected, accept_standard=-1),
                    )

    def test_relaxed_acceptance_table_uses_every_batch_boundary(self) -> None:
        rows = (
            (0, 1, 1),
            (2, 15, 2),
            (16, 50, 3),
            (51, 150, 5),
            (151, 500, 8),
            (501, 3200, 13),
            (3201, 35000, 20),
            (35001, 500000, 32),
            (500001, None, 50),
        )
        for minimum, maximum, expected in rows:
            quantities = (minimum,) if maximum is None else (minimum, maximum)
            for quantity in quantities:
                with self.subTest(quantity=quantity):
                    self.assertEqual(
                        resolve_sampling(
                            check_plan="不适用",
                            accept_standard="放宽",
                            item_name="示例物料",
                            quantity=quantity,
                        ),
                        SamplingDecision(check_number=expected, accept_standard=-1),
                    )

    def test_quantity_one_keeps_legacy_short_circuit_for_special_acceptance(self) -> None:
        self.assertEqual(
            resolve_sampling(
                check_plan="不适用",
                accept_standard="加严",
                item_name="示例物料",
                quantity=1,
            ),
            SamplingDecision(check_number=1, accept_standard=-1),
        )

    def test_ambiguous_special_acceptance_is_rejected(self) -> None:
        with self.assertRaisesRegex(SamplingRuleError, "同时包含加严和放宽"):
            resolve_sampling(
                check_plan="不适用",
                accept_standard="加严后放宽",
                item_name="示例物料",
                quantity=2,
            )

    def test_special_table_marker_in_check_plan_is_supported(self) -> None:
        self.assertEqual(
            resolve_sampling(
                check_plan="抽样方案：加严",
                accept_standard="AQL=1.0",
                item_name="示例物料",
                quantity=9,
            ),
            SamplingDecision(check_number=3, accept_standard=-1),
        )
        self.assertEqual(
            resolve_sampling(
                check_plan="抽样方案：放宽",
                accept_standard="AQL=1.0",
                item_name="示例物料",
                quantity=16,
            ),
            SamplingDecision(check_number=3, accept_standard=-1),
        )

    def test_conflicting_special_markers_across_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(SamplingRuleError, "同时包含加严和放宽"):
            resolve_sampling(
                check_plan="抽样方案：加严",
                accept_standard="放宽",
                item_name="示例物料",
                quantity=2,
            )

    def test_invalid_text_is_rejected_instead_of_guessed(self) -> None:
        with self.assertRaisesRegex(SamplingRuleError, "不在旧 BAS 支持的 AQL 表内"):
            resolve_sampling(
                check_plan=PLAN,
                accept_standard="AQL=1.25",
                item_name="普通物料",
                quantity=10,
            )

    def test_special_inspection_levels_s1_to_s4_are_supported(self) -> None:
        for level, expected in (("S-1", 8), ("S-2", 8), ("S-3", 8), ("S-4", 8)):
            with self.subTest(level=level):
                self.assertEqual(
                    resolve_sampling(
                        check_plan=f"GB2828 正常检查一次抽样方案：IL={level}",
                        accept_standard="AQL=1.0",
                        item_name="普通物料",
                        quantity=8,
                    ),
                    SamplingDecision(check_number=expected, accept_standard=-1),
                )

    def test_special_inspection_level_s1_is_not_confused_with_il_prefix(self) -> None:
        # 501--1200 件、S-1、AQL=2.5 在旧 BAS 静态表中应取 C 方案，即 5 件。
        # 覆盖普通/全角连接号，以及连接号两侧带空格的 Windchill 文本。
        for plan in (
            "GB2828 正常检查一次抽样方案：IL=S-1",
            "GB2828 正常检查一次抽样方案：IL=S－1",
            "GB2828 正常检查一次抽样方案：IL= S - 1",
            "GB2828 正常检查一次抽样方案：IL= S － 1",
        ):
            with self.subTest(plan=plan):
                self.assertEqual(
                    resolve_sampling(
                        check_plan=plan,
                        accept_standard="AQL=2.5",
                        item_name="机柜附件类物料",
                        quantity=518,
                    ),
                    SamplingDecision(check_number=5, accept_standard=-1),
                )

    def test_malformed_ascii_inspection_levels_are_rejected(self) -> None:
        # 不应从更长的非法 token 中截取合法前缀：S-10/S-1x/S-1A，
        # 也不应从 IL=IIII 的尾部截取 III。
        for plan in (
            "GB2828 正常检查一次抽样方案：IL=S-10",
            "GB2828 正常检查一次抽样方案：IL=S-1x",
            "GB2828 正常检查一次抽样方案：IL=S-1A",
            "GB2828 正常检查一次抽样方案：IL=IIII",
            "GB2828 正常检查一次抽样方案：AS-1",
            "GB2828 正常检查一次抽样方案：XYZS-1",
        ):
            with self.subTest(plan=plan), self.assertRaisesRegex(
                SamplingRuleError, "check_plan 未解析出检验水平 IL"
            ):
                resolve_sampling(
                    check_plan=plan,
                    accept_standard="AQL=2.5",
                    item_name="机柜附件类物料",
                    quantity=518,
                )

    def test_legal_ascii_inspection_levels_remain_supported(self) -> None:
        for level, expected in (("I", 32), ("II", 80), ("III", 125)):
            with self.subTest(level=level):
                self.assertEqual(
                    resolve_sampling(
                        check_plan=f"GB2828 正常检查一次抽样方案：IL={level}",
                        accept_standard="AQL=2.5",
                        item_name="机柜附件类物料",
                        quantity=518,
                    ),
                    SamplingDecision(check_number=expected, accept_standard=-1),
                )

    def test_special_inspection_level_s1_can_be_found_without_il_prefix(self) -> None:
        # 与 VBA 的整段 LOCATE 语义一致：中文前置文本后可直接出现 S-1。
        self.assertEqual(
            resolve_sampling(
                check_plan="GB2828 正常检查一次抽样方案：特殊水平 S-1",
                accept_standard="AQL=2.5",
                item_name="机柜附件类物料",
                quantity=518,
            ),
            SamplingDecision(check_number=5, accept_standard=-1),
        )

    def test_inspection_level_can_be_found_without_il_prefix(self) -> None:
        self.assertEqual(
            resolve_sampling(
                check_plan="GB2828 正常一次抽样方案：Ⅱ",
                accept_standard="AQL=1.0",
                item_name="普通物料",
                quantity=36,
            ),
            SamplingDecision(check_number=13, accept_standard=-1),
        )

    def test_quantity_36_il_i_aql_one_follows_down_pointers_to_e(self) -> None:
        self.assertEqual(
            resolve_sampling(
                check_plan="GB2828 正常检查一次抽样方案：IL=I",
                accept_standard="□QA1：AQL=1.0  □其他：AQL=1.5",
                item_name="普通物料 QA1",
                quantity=36,
            ),
            SamplingDecision(check_number=13, accept_standard=0),
        )

    def test_first_batch_and_follow_up_plans_disable_normal_sampling(self) -> None:
        for marker in ("首批", "后续"):
            with self.subTest(marker=marker):
                self.assertEqual(
                    resolve_sampling(
                        check_plan=f"GB2828 {marker}正常检查一次抽样方案：IL=Ⅰ",
                        accept_standard="AQL=1.0",
                        item_name="普通物料",
                        quantity=8,
                    ),
                    SamplingDecision(check_number=None, accept_standard=-1),
                )

    def test_upper_lower_cells_follow_adjacent_static_plan(self) -> None:
        # A 方案 AQL=1.0 的单元格为“下”，递归到 E 方案后取样本量 13，
        # 再受批量 8 的上限约束。
        self.assertEqual(
            resolve_sampling(
                check_plan="GB2828 正常检查一次抽样方案：IL=S-1",
                accept_standard="AQL=1.0",
                item_name="普通物料",
                quantity=8,
            ),
            SamplingDecision(check_number=8, accept_standard=-1),
        )

    def test_stop_cell_returns_current_plan_sample_like_vba(self) -> None:
        # Q 方案 AQL=0 的“止”不是缺失映射；VBA 返回当前 Q 方案样本量。
        from decimal import Decimal

        self.assertEqual(_resolved_static_sample("Q", Decimal("0")), 1250)


if __name__ == "__main__":
    unittest.main()
