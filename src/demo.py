"""Offline, standard-library-only demonstration using synthetic records."""
from __future__ import annotations

import argparse
from pathlib import Path
import zipfile
from xml.etree import ElementTree as ET

from .engineer_service import load_engineers
from .source_excel import read_order_tasks
from .template_writer import assemble_order_draft, write_order_draft
from .windchill_adapter import MockWindchillAdapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("outputs/demo"))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    task = read_order_tasks(root / "fixtures/source_sample.xlsx", ["DEMO-ORDER-001"])["DEMO-ORDER-001"]
    engineer = load_engineers(root / "config/engineers.example.json")[0]
    names = MockWindchillAdapter({"MAT-001": "合成检查单", "MAT-002": "合成检查单"})
    draft = assemble_order_draft(task, names.query_names(["MAT-001", "MAT-002"]), engineer)
    destination = args.output.resolve() / "synthetic_inspection_draft.xlsx"
    write_order_draft(root / "template/iqc_draft_template.xlsx", destination, draft)
    with zipfile.ZipFile(destination) as archive:
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    for cell, expected in {"A4": "DEMO-ORDER-001", "D4": "MAT-001", "D5": "MAT-002", "R4": "合成检查单", "S4": engineer.display_name}.items():
        node = sheet.find(f".//m:c[@r='{cell}']/m:v", ns)
        if node is None or node.text != expected:
            raise RuntimeError(f"Saved synthetic workbook verification failed at {cell}")
    print(f"Synthetic demo passed: {len(draft.rows)} rows; {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
