from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from src.pqc_excel import PQC_HEADERS, PqcExcelError, projects_for_engineer, read_pqc_workbook


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _xlsx(path: Path, rows, *, headers=PQC_HEADERS, formula_without_cache=None):
    all_rows = [headers, *rows]
    sheet_rows = []
    for row_number, values in enumerate(all_rows, start=1):
        cells = []
        for column, value in enumerate(values, start=1):
            if value is None:
                continue
            ref = f"{_column_name(column)}{row_number}"
            if formula_without_cache == ref:
                cells.append(f'<c r="{ref}"><f>1+1</f></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    sheet = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(sheet_rows) + "</sheetData></worksheet>"
    workbook = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="PQC" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def row(item="D-001", category="盘台", *, ncr="", engineer="Engineer", special=""):
    return (1, "WO", "Project", "P", item, "S", engineer, special, ncr, category)


class PqcExcelV4Tests(unittest.TestCase):
    def test_a_to_j_preserves_business_text_and_reads_category(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pqc.xlsx"
            _xlsx(path, [row(" Ａ０１ ", "风扇盘", ncr=" NCR-1 ")])
            workbook = read_pqc_workbook(path)
        self.assertEqual(workbook.rows[0].item_code, "Ａ０１")
        self.assertEqual(workbook.rows[0].category, "风扇盘")
        self.assertEqual(workbook.rows[0].ncr_code, "NCR-1")

    def test_missing_category_header_is_a_hard_contract_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "old.xlsx"
            _xlsx(path, [row() ], headers=PQC_HEADERS[:9])
            with self.assertRaisesRegex(PqcExcelError, "A:J"):
                read_pqc_workbook(path)

    def test_missing_category_value_is_reported_for_non_jd_and_jd_ignores_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rows.xlsx"
            _xlsx(path, [row("NORMAL-1", ""), row("CAB-JD-1", "")])
            workbook = read_pqc_workbook(path)
        self.assertEqual([item.item_code for item in workbook.rows], ["NORMAL-1", "CAB-JD-1"])
        self.assertIn("类别", workbook.rows[0].errors[0])

    def test_non_jd_duplicate_category_conflict_blocks_but_jd_conflict_is_audit_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.xlsx"
            _xlsx(path, [row("NORMAL-1", "盘台"), row("NORMAL-1", "风扇盘"), row("CAB-JD-1", "盘台"), row("CAB-JD-1", "组件类")])
            workbook = read_pqc_workbook(path)
        normal = next(item for item in workbook.rows if item.item_code == "NORMAL-1")
        cabinet = next(item for item in workbook.rows if item.item_code == "CAB-JD-1")
        self.assertIn("类别", normal.errors[0])
        self.assertEqual(cabinet.errors, ())

    def test_engineer_filter_is_trimmed_and_ordered(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "projects.xlsx"
            _xlsx(path, [row("D2", engineer="Demo Engineer"), row("D1", engineer="Demo Engineer X"), row("D3", engineer="Demo Engineer")])
            projects = projects_for_engineer(read_pqc_workbook(path), " Demo Engineer ")
        self.assertEqual([item.item_code for item in projects[0].rows], ["D2", "D3"])

    def test_uncached_formula_requires_resave(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "formula.xlsx"
            _xlsx(path, [row()], formula_without_cache="B2")
            with self.assertRaisesRegex(PqcExcelError, "没有已保存的计算值"):
                read_pqc_workbook(path)

    def test_uncached_j_formula_requires_resave(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "formula-j.xlsx"
            _xlsx(path, [row()], formula_without_cache="J2")
            with self.assertRaisesRegex(PqcExcelError, "没有已保存的计算值"):
                read_pqc_workbook(path)


if __name__ == "__main__":
    unittest.main()
