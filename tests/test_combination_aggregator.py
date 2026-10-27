from __future__ import annotations

import unittest
from dataclasses import replace

from src.combination_aggregator import (
    CombinationAggregationError,
    aggregate_task,
    build_m_code_display,
    expand_m_range,
)
from src.models import OrderTask, SourceRow


def _row(
    source_row: int,
    item_code: str,
    quantity: object,
    *,
    order: str = "DN001",
    sequence_start: str | None = None,
    sequence_end: str | None = None,
    item_name: str = "物料",
    supplier: str = "供应商",
) -> SourceRow:
    values = [""] * 16
    values[0] = order
    values[3] = item_code
    values[4] = item_name
    values[5] = supplier
    values[9] = quantity
    return SourceRow(
        order,
        item_code,
        "测试",
        source_row,
        tuple(values),
        sequence_start=sequence_start,
        sequence_end=sequence_end,
    )


class CombinationAggregatorTests(unittest.TestCase):
    def test_m_code_display_uses_slash_for_empty_single_multiple_and_range(self) -> None:
        # 空值继续返回 None；单码、非连续多码和范围均只允许用 / 连接
        # M 码，范围压缩的 ~ 与既有语义保持不变。
        self.assertIsNone(build_m_code_display(()))
        self.assertEqual(
            build_m_code_display((("M99999999001", None, True),)),
            "M99999999001",
        )
        self.assertEqual(
            build_m_code_display(
                (
                    ("M99999999001", None, True),
                    ("M99999999003", None, True),
                )
            ),
            "M99999999001/M99999999003",
        )
        self.assertEqual(
            build_m_code_display((("M99999999001", "M99999999003", False),)),
            "M99999999001~M99999999003",
        )

    def test_tk_blank_supplier_defaults_to_na_with_raw_provenance(self) -> None:
        group = aggregate_task(OrderTask("TK001", rows=[_row(4, "MAT-A", 1, order="TK001", supplier="")]))[0]
        self.assertEqual(group.source.values[5], "NA")
        self.assertEqual(group.source.raw_supplier, "")
        self.assertTrue(group.source.supplier_defaulted)

    def test_tk_nonblank_and_non_tk_blank_supplier_are_unchanged(self) -> None:
        tk = aggregate_task(OrderTask("TK002", rows=[_row(4, "MAT-A", 1, order="TK002", supplier="原供应商")]))[0]
        non_tk = aggregate_task(OrderTask("DN002", rows=[_row(4, "MAT-A", 1, order="DN002", supplier="")]))[0]
        self.assertEqual(tk.source.values[5], "原供应商")
        self.assertFalse(tk.source.supplier_defaulted)
        self.assertEqual(non_tk.source.values[5], "")
        self.assertFalse(non_tk.source.supplier_defaulted)

    def test_same_order_and_item_are_merged_and_keep_physical_line_numbers(self) -> None:
        task = OrderTask(
            "DN001",
            rows=[
                _row(4, "MAT-A", "2"),
                _row(5, "MAT-B", "1"),
                _row(6, "MAT-A", "3"),
                _row(7, "MAT-B", "1"),
                _row(8, "MAT-A", "1"),
            ],
        )
        groups = aggregate_task(task)
        self.assertEqual([(group.item_code, group.quantity) for group in groups], [("MAT-A", 6), ("MAT-B", 2)])
        self.assertEqual(groups[0].relative_line_numbers, (1, 3, 5))
        self.assertEqual(groups[0].trigger_basis, "DN001-1/3/5")
        self.assertEqual(groups[0].source.values[9], 6)

    def test_quantity_is_validated_before_sum(self) -> None:
        for value in (None, "", "0", "-1", "1.5", "not-a-number", True):
            with self.subTest(value=value):
                with self.assertRaises(CombinationAggregationError):
                    aggregate_task(OrderTask("DN001", rows=[_row(4, "MAT-A", value)]))
        self.assertEqual(aggregate_task(OrderTask("DN001", rows=[_row(4, "MAT-A", "2.0")]))[0].quantity, 2)

    def test_m_range_is_strict_and_inclusive(self) -> None:
        self.assertEqual(
            expand_m_range("M99999999001", "M99999999003"),
            ("M99999999001", "M99999999002", "M99999999003"),
        )
        for start, end in (
            ("m99999999001", ""),
            ("M99999999003", "M99999999001"),
            ("MDEMO-MAT-003", "M99999999003"),
        ):
            with self.subTest(start=start, end=end):
                with self.assertRaises(CombinationAggregationError):
                    expand_m_range(start, end)

    def test_m_range_with_different_first_eight_digits_warns_and_keeps_endpoints(self) -> None:
        warnings: list[str] = []
        start = "M88000008001"
        end = "M88000019003"
        self.assertEqual(
            expand_m_range(start, end, warning_callback=warnings.append),
            (start, end),
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("固定段（M+前7位）不一致", warnings[0])
        self.assertIn("保留起止端点继续生成", warnings[0])
        self.assertEqual(
            build_m_code_display(((start, end, False),), warning_callback=warnings.append),
            f"{start}/{end}",
        )
        self.assertEqual(len(warnings), 2)

    def test_m_range_keeps_four_digit_sequence_when_first_seven_digits_match(self) -> None:
        # M+7 固定数字 + 4 位序号：第 8 个数字从 0 变为 1 时，
        # 仍按连续四位序号展开，不应截断或伪造前缀。
        start = "M88000000999"
        end = "M88000001002"
        self.assertEqual(
            expand_m_range(start, end),
            (
                "M88000000999",
                "M88000001000",
                "M88000001001",
                "M88000001002",
            ),
        )
        warnings: list[str] = []
        self.assertEqual(
            build_m_code_display(((start, end, False),), warning_callback=warnings.append),
            "M88000000999~M88000001002",
        )
        self.assertEqual(warnings, [])

    def test_m_range_warning_includes_order_and_item_when_aggregating(self) -> None:
        warnings: list[str] = []
        values = [""] * 16
        values[0] = "DN001"
        values[3] = "MAT-A"
        values[4] = "物料"
        values[5] = "供应商"
        values[9] = 1
        values[14] = "M88000008001~M88000019003"
        group = aggregate_task(
            OrderTask("DN001", rows=[SourceRow("DN001", "MAT-A", "测试", 4, tuple(values))]),
            warning_callback=warnings.append,
        )[0]
        self.assertEqual(group.sequence_code, "M88000008001/M88000019003")
        self.assertEqual(len(warnings), 1)
        self.assertIn("送检单 DN001、物料 MAT-A", warnings[0])

    def test_cross_prefix_descending_range_still_blocks(self) -> None:
        with self.assertRaisesRegex(CombinationAggregationError, "起始值不能大于结束值"):
            expand_m_range("M88000011002", "M88000000999")

    def test_m_display_preserves_vba_asymmetric_singleton_deduplication(self) -> None:
        parts = (
            ("M99999999001", None, True),
            ("M99999999002", "M99999999004", False),
            ("M99999999004", None, True),
        )
        self.assertEqual(
            build_m_code_display(parts),
            "M99999999001~M99999999004/M99999999004",
        )

    def test_group_consistency_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(CombinationAggregationError, "item_name"):
            aggregate_task(
                OrderTask(
                    "DN001",
                    rows=[_row(4, "MAT-A", 1, item_name="甲"), _row(5, "MAT-A", 1, item_name="乙")],
                )
            )

    def test_explicit_sequence_fields_are_written_to_aggregate_source(self) -> None:
        group = aggregate_task(
            OrderTask(
                "DN001",
                rows=[
                    _row(4, "MAT-A", 1, sequence_start="M99999999001", sequence_end="M99999999002"),
                ],
            )
        )[0]
        self.assertEqual(group.sequence_code, "M99999999001~M99999999002")
        self.assertEqual(group.source.values[14], group.sequence_code)
        self.assertEqual(group.source.values[15], "")

    def test_o_p_single_m_codes_ignore_internal_spaces_before_validation(self) -> None:
        group = aggregate_task(
            OrderTask(
                "DN001",
                rows=[
                    _row(
                        4,
                        "MAT-A",
                        1,
                        sequence_start="M880 00008001",
                        sequence_end="M880 00008003",
                    )
                ],
            )
        )[0]
        self.assertEqual(group.sequence_code, "M88000008001~M88000008003")

    def test_o_multiple_noncontiguous_m_codes_are_split_instead_of_strict_block(self) -> None:
        row = _row(4, "MAT-A", 1)
        values = list(row.values)
        values[14] = "M99999999001 M99999999003"
        group = aggregate_task(
            OrderTask("DN001", rows=[replace(row, values=tuple(values))])
        )[0]
        self.assertEqual(group.sequence_code, "M99999999001/M99999999003")

    def test_p_multiple_noncontiguous_m_codes_are_also_split(self) -> None:
        row = _row(4, "MAT-A", 1)
        values = list(row.values)
        values[15] = "M99999999005M99999999007"
        group = aggregate_task(
            OrderTask("DN001", rows=[replace(row, values=tuple(values))])
        )[0]
        self.assertEqual(group.sequence_code, "M99999999005/M99999999007")

    def test_legacy_parenthesized_m_expansion_keeps_compressed_head(self) -> None:
        row = _row(4, "MAT-A", 1)
        values = list(row.values)
        values[14] = "M99999999001(M99999999001/M99999999002)"
        group = aggregate_task(
            OrderTask("DN001", rows=[replace(row, values=tuple(values))])
        )[0]
        self.assertEqual(group.sequence_code, "M99999999001")


if __name__ == "__main__":
    unittest.main()
