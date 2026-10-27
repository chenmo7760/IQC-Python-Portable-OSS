from __future__ import annotations

import unittest
import zipfile
from tempfile import TemporaryDirectory
from pathlib import Path
from xml.etree import ElementTree as ET

from src.models import SOURCE_HEADERS
from src.combination_aggregator import aggregate_task
from src.source_excel import SourceExcelError, parse_order_numbers, read_order_tasks


ROOT = Path(__file__).resolve().parents[1]


def _write_monthly_fixture(
    path: Path,
    q3_header: str,
    material_codes: list[str] | None = None,
    *,
    p3_header: str = "",
    sequence_pairs: list[tuple[str, str]] | None = None,
) -> None:
    """写入最小合法月度工作簿，覆盖历史 Q3 表头和多行数据。"""

    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    ET.register_namespace("", main_ns)
    ET.register_namespace("r", rel_ns)
    ET.register_namespace("", pkg_rel_ns)

    workbook = ET.Element(f"{{{main_ns}}}workbook")
    sheets = ET.SubElement(workbook, f"{{{main_ns}}}sheets")
    ET.SubElement(sheets, f"{{{main_ns}}}sheet", {"name": "3月", "sheetId": "1", f"{{{rel_ns}}}id": "rId1"})

    rels = ET.Element(f"{{{pkg_rel_ns}}}Relationships")
    ET.SubElement(rels, f"{{{pkg_rel_ns}}}Relationship", {"Id": "rId1", "Type": "worksheet", "Target": "worksheets/sheet1.xml"})

    sheet = ET.Element(f"{{{main_ns}}}worksheet")
    sheet_data = ET.SubElement(sheet, f"{{{main_ns}}}sheetData")

    def add_row(row_number: int, values: list[str]) -> None:
        row = ET.SubElement(sheet_data, f"{{{main_ns}}}row", {"r": str(row_number)})
        for index, value in enumerate(values, start=1):
            if value == "":
                continue
            column = ""
            number = index
            while number:
                number, remainder = divmod(number - 1, 26)
                column = chr(65 + remainder) + column
            cell = ET.SubElement(row, f"{{{main_ns}}}c", {"r": f"{column}{row_number}", "t": "inlineStr"})
            inline = ET.SubElement(cell, f"{{{main_ns}}}is")
            ET.SubElement(inline, f"{{{main_ns}}}t").text = value

    add_row(2, [""] * 17)
    add_row(3, list(SOURCE_HEADERS[:15]) + [p3_header, q3_header])
    for row_number in range(270, 276):
        suffix = row_number - 269
        add_row(
            row_number,
            [
                "8478993",
                f"PO-{suffix:03d}",
                f"M{suffix:03d}",
                material_codes[suffix - 1] if material_codes is not None else f"MAT-{suffix:03d}",
                f"物料-{suffix}",
                "供应商",
                "合同",
                "项目",
                "项目名称",
                "1",
                "0",
                "2026-03-01",
                "",
                "",
                sequence_pairs[suffix - 1][0] if sequence_pairs is not None else f"S{suffix:03d}",
                sequence_pairs[suffix - 1][1] if sequence_pairs is not None else "",
            ],
        )

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", ET.tostring(workbook, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/_rels/workbook.xml.rels", ET.tostring(rels, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/worksheets/sheet1.xml", ET.tostring(sheet, encoding="utf-8", xml_declaration=True))


class SourceExcelTests(unittest.TestCase):
    def test_parse_semicolon_orders_deduplicates_in_order(self) -> None:
        self.assertEqual(parse_order_numbers(" DEMO-ORDER-001;9926247;;DEMO-ORDER-001 "), ["DEMO-ORDER-001", "9926247"])

    def test_read_known_fixture(self) -> None:
        fixture = ROOT / "fixtures" / "source_sample.xlsx"
        tasks = read_order_tasks(fixture, ["DEMO-ORDER-001", "9926247"])
        self.assertEqual(len(tasks["DEMO-ORDER-001"].rows), 2)
        self.assertEqual(tasks["DEMO-ORDER-001"].rows[0].sheet_name, "8月")
        self.assertEqual(tasks["DEMO-ORDER-001"].rows[0].source_row, 4)
        self.assertTrue(tasks["9926247"].errors)

    def test_read_historical_executor_q3_header_and_multiple_rows(self) -> None:
        with TemporaryDirectory() as temp:
            fixture = Path(temp) / "2026年度IQC统计数据汇总.xlsx"
            _write_monthly_fixture(fixture, "执行者")
            tasks = read_order_tasks(fixture, ["8478993"])

        self.assertEqual(tasks["8478993"].errors, [])
        self.assertEqual(len(tasks["8478993"].rows), 6)
        self.assertEqual([row.source_row for row in tasks["8478993"].rows], list(range(270, 276)))
        self.assertEqual([row.item_code for row in tasks["8478993"].rows], [f"MAT-{n:03d}" for n in range(1, 7)])
        # C 列“物项编码”仍保留在原始 A:P 值中，仅 D 列“物料编号”作为
        # Windchill/SQL 的 item_code，避免把源系统编码误当作物料编号。
        self.assertEqual(tasks["8478993"].rows[0].values[2], "M001")
        self.assertEqual(tasks["8478993"].rows[0].values[3], "MAT-001")

    def test_item_code_uses_d_column_without_prefix_assumption(self) -> None:
        """D 列可包含不同编码体系；只按列位取值，不强制 100 前缀。"""

        material_codes = ["100001", "100002", "DEMO-MAT-016", "100004", "100005", "100006"]
        with TemporaryDirectory() as temp:
            fixture = Path(temp) / "8478993-cd-distinct.xlsx"
            _write_monthly_fixture(fixture, "未完成", material_codes)
            tasks = read_order_tasks(fixture, ["8478993"])

        rows = tasks["8478993"].rows
        self.assertEqual([row.item_code for row in rows], material_codes)
        self.assertEqual([row.values[2] for row in rows], [f"M{n:03d}" for n in range(1, 7)])

    def test_read_completed_q3_header_remains_supported(self) -> None:
        with TemporaryDirectory() as temp:
            fixture = Path(temp) / "completed.xlsx"
            _write_monthly_fixture(fixture, "未完成")
            tasks = read_order_tasks(fixture, ["8478993"])
        self.assertEqual(len(tasks["8478993"].rows), 6)

        with TemporaryDirectory() as temp:
            fixture = Path(temp) / "unsupported.xlsx"
            _write_monthly_fixture(fixture, "其他状态")
            with self.assertRaisesRegex(SourceExcelError, "未找到符合严格月度布局"):
                read_order_tasks(fixture, ["8478993"])

    def test_monthly_o_p_columns_are_m_sequence_start_and_end(self) -> None:
        pairs = [
            ("M99999999001", "M99999999003"),
            ("M99999999004", ""),
            ("", "M99999999005"),
            ("M99999999006", "M99999999006"),
            ("S005", ""),
            ("", ""),
        ]
        with TemporaryDirectory() as temp:
            fixture = Path(temp) / "m-code-range.xlsx"
            _write_monthly_fixture(fixture, "未完成", p3_header="M码结束", sequence_pairs=pairs)
            tasks = read_order_tasks(fixture, ["8478993"])

        rows = tasks["8478993"].rows
        self.assertEqual(rows[0].sequence_start, "M99999999001")
        self.assertEqual(rows[0].sequence_end, "M99999999003")
        self.assertEqual(rows[1].sequence_start, "M99999999004")
        self.assertIsNone(rows[1].sequence_end)
        self.assertIsNone(rows[4].sequence_start)
        self.assertIsNone(rows[4].sequence_end)

        group = aggregate_task(tasks["8478993"])[0]
        self.assertEqual(
            group.sequence_code,
            "M99999999001~M99999999003",
        )
        self.assertEqual(group.source.values[14], group.sequence_code)
        self.assertEqual(group.source.values[15], "")

if __name__ == "__main__":
    unittest.main()
