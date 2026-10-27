from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from src.engineer_service import load_engineers
from src.models import Engineer, OrderTask, SourceRow, WindchillName
from src.source_excel import read_order_tasks
from src.template_writer import (
    TemplateWriterError,
    assemble_order_draft,
    output_path_for_order,
    write_order_draft,
)
from src.windchill_adapter import MockWindchillAdapter


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS = {"m": MAIN_NS}


def _cell_text(sheet_xml: bytes, reference: str) -> str:
    root = ET.fromstring(sheet_xml)
    for cell in root.findall(f".//m:c[@r='{reference}']", NS):
        value = cell.find("m:v", NS)
        return "" if value is None or value.text is None else value.text
    return ""


def _put_q_template_value(template: Path, value: str) -> None:
    with zipfile.ZipFile(template, "r") as source_zip:
        entries = {info.filename: source_zip.read(info.filename) for info in source_zip.infolist()}
    root = ET.fromstring(entries["xl/worksheets/sheet1.xml"])
    cell = root.find(".//m:c[@r='Q4']", NS)
    assert cell is not None
    cell.set("t", "str")
    node = ET.SubElement(cell, f"{{{MAIN_NS}}}v")
    node.text = value
    entries["xl/worksheets/sheet1.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    temp = template.with_suffix(".tmp")
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as target_zip:
        for name, content in entries.items():
            target_zip.writestr(name, content)
    temp.replace(template)


class TemplateWriterTests(unittest.TestCase):
    def test_assemble_order_draft_writes_tk_blank_supplier_as_na_and_keeps_raw(self) -> None:
        values = ["TK99999999001", "PO", "M001", "MAT-001", "物料", ""]
        task = OrderTask(
            order_number="TK99999999001",
            rows=[SourceRow("TK99999999001", "MAT-001", "9月", 4, tuple(values))],
        )
        draft = assemble_order_draft(
            task,
            [WindchillName("MAT-001", "检查单_示例", "SUCCESS")],
            Engineer("", "合成工程师02", "", "合成工程师02"),
        )
        self.assertEqual(draft.rows[0].source.values[5], "NA")
        self.assertEqual(draft.rows[0].source.raw_supplier, "")
        self.assertTrue(draft.rows[0].source.supplier_defaulted)

    def test_assemble_order_draft_maps_source_and_windchill_without_q(self) -> None:
        task = OrderTask(
            order_number="DEMO-ORDER-001",
            rows=[
                SourceRow("DEMO-ORDER-001", "M001", "8月", 4, ("DEMO-ORDER-001", "PO", "M001")),
                SourceRow("DEMO-ORDER-001", "M001", "8月", 5, ("DEMO-ORDER-001", "PO2", "M001")),
            ],
        )
        engineer = Engineer("", "合成工程师02", "", "合成工程师02")
        draft = assemble_order_draft(
            task,
            [WindchillName("M001", "检查单_示例", "SUCCESS")],
            engineer,
        )
        self.assertEqual(len(draft.rows), 2)
        self.assertEqual(draft.rows[0].procedure_name, "检查单_示例")
        self.assertEqual(draft.rows[0].qc_engineer, "合成工程师02")

    def test_assemble_order_draft_normalizes_prefixed_windchill_name(self) -> None:
        task = OrderTask(
            order_number="8478993",
            rows=[SourceRow("8478993", "M001", "3月", 4, ("8478993", "PO", "M001"))],
        )
        draft = assemble_order_draft(
            task,
            [WindchillName("M001", "DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_合成类别06进货检查单", "SUCCESS")],
            Engineer("", "合成工程师02", "", "合成工程师02"),
        )
        self.assertEqual(draft.rows[0].procedure_name, "合成类别06进货检查单")

    def test_assemble_order_draft_normalizes_book_title_marks(self) -> None:
        task = OrderTask(
            order_number="8478993",
            rows=[SourceRow("8478993", "M001", "3月", 4, ("8478993", "PO", "M001"))],
        )
        draft = assemble_order_draft(
            task,
            [WindchillName("M001", "《合成类别06进货检查单》", "SUCCESS")],
            Engineer("", "合成工程师02", "", "合成工程师02"),
        )
        self.assertEqual(draft.rows[0].procedure_name, "合成类别06进货检查单")

    def test_assemble_order_draft_uses_same_normalized_name_for_wrapped_prefix_and_suffix(self) -> None:
        task = OrderTask(
            order_number="8478993",
            rows=[SourceRow("8478993", "M001", "3月", 4, ("8478993", "PO", "M001"))],
        )
        draft = assemble_order_draft(
            task,
            [
                WindchillName(
                    "M001",
                    "《DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_合成类别06进货检查单》满足:DEMO-C-DEMOFORM-DEMOUNIT-TEST#G******-01；",
                    "SUCCESS",
                )
            ],
            Engineer("", "合成工程师02", "", "合成工程师02"),
        )
        self.assertEqual(draft.rows[0].procedure_name, "合成类别06进货检查单")

    def test_write_order_draft_preserves_q_and_template_structure(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        source_path = project_root / "fixtures" / "source_sample.xlsx"
        template_path = project_root / "template" / "iqc_draft_template.xlsx"
        tasks = read_order_tasks(source_path, ["DEMO-ORDER-001"])
        engineer = load_engineers(project_root / "config" / "engineers.example.json")[0]
        names = MockWindchillAdapter({"MAT-001": "检查单_示例", "MAT-002": "检查单_示例"}).query_names(["MAT-001", "MAT-002"])

        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            local_template = temp_root / "template.xlsx"
            local_template.write_bytes(template_path.read_bytes())
            _put_q_template_value(local_template, "模板单位保留")
            output = output_path_for_order(temp_root / "输出", "DEMO-ORDER-001")
            draft = assemble_order_draft(tasks["DEMO-ORDER-001"], names, engineer)
            write_order_draft(local_template, output, draft)

            self.assertTrue(output.exists())
            with zipfile.ZipFile(output, "r") as result_zip:
                workbook_xml = ET.fromstring(result_zip.read("xl/workbook.xml"))
                sheets = workbook_xml.findall("m:sheets/m:sheet", NS)
                self.assertEqual([sheet.attrib["name"] for sheet in sheets], ["进货检验草稿基本信息"])
                sheet_xml = result_zip.read("xl/worksheets/sheet1.xml")
                self.assertEqual(_cell_text(sheet_xml, "A4"), "DEMO-ORDER-001")
                self.assertEqual(_cell_text(sheet_xml, "C4"), "M001")
                self.assertEqual(_cell_text(sheet_xml, "R4"), "检查单_示例")
                self.assertEqual(_cell_text(sheet_xml, "S4"), "合成工程师02")
                self.assertEqual(_cell_text(sheet_xml, "Q4"), "模板单位保留")
                self.assertNotIn(b"measurement_unit", b"".join(result_zip.read(name) for name in result_zip.namelist()))

    def test_output_filename_rejects_path_injection(self) -> None:
        with self.assertRaises(TemplateWriterError):
            output_path_for_order(Path("D:/IQC-Python-Portable/输出"), "../bad")


if __name__ == "__main__":
    unittest.main()
