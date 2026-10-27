from __future__ import annotations

import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.sax.saxutils import escape

from src.paper_standard_exclusion import (
    MATCH_EXCLUDED,
    MATCH_NOT_FOUND,
    MATCH_REVIEW_REQUIRED,
    MATCH_UNDETERMINED,
    PARSE_VALID,
    PaperStandardCatalog,
    PaperStandardEntry,
    PaperStandardExclusionError,
    match_paper_standard,
    parse_paper_standard_entry,
    parse_windchill_document_reference,
    read_paper_standard_workbook,
)


WECHAT_BASELINE = Path(
    r"D:\xwe\xwechat_files\tanlili1980_05c0\msg\file\2026-09\进货检查线下规范清单.xlsx"
)


def _entry(key: str, *, row: int = 2, raw: str = "") -> PaperStandardEntry:
    return PaperStandardEntry(
        sheet_name="Sheet1",
        source_row=row,
        raw_value=raw or key,
        document_code=key,
        exclude_key=key,
        parse_status=PARSE_VALID,
    )


def _catalog(*keys: str) -> PaperStandardCatalog:
    return PaperStandardCatalog(entries=tuple(_entry(key, row=index + 2) for index, key in enumerate(keys)))


def _write_xlsx(path: Path, rows: list[tuple[int, str]]) -> None:
    cells = "\n".join(
        f'<row r="{row}"><c r="B{row}" t="inlineStr"><is><t>{escape(value)}</t></is></c></row>'
        for row, value in rows
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>{cells}</sheetData>
</worksheet>""",
        )


class PaperStandardExclusionTests(unittest.TestCase):
    def test_rt_paper_01_current_attachment_has_40_valid_unique_entries(self) -> None:
        if not WECHAT_BASELINE.exists():
            self.skipTest("current WeChat attachment is not present on this machine")
        catalog = read_paper_standard_workbook(WECHAT_BASELINE)

        self.assertEqual(len(catalog.entries), 40)
        self.assertEqual(len({entry.exclude_key for entry in catalog.entries}), 40)
        self.assertTrue(all(entry.parse_status == PARSE_VALID for entry in catalog.entries))

    def test_rt_paper_02_03_04_05_06_parse_code_revision_and_z_boundaries(self) -> None:
        non_z = parse_paper_standard_entry("DEMO07-DOC102-DEMOFORM-003_0_合成通信模块进货检验规范.pdf")
        self.assertEqual(non_z.exclude_key, "DEMO07-DOC102-DEMOFORM-003")
        self.assertEqual(non_z.revision, "0")
        self.assertEqual(non_z.extension, ".pdf")

        r1 = parse_paper_standard_entry("DEMO04-52-DEMOFORM-003_R1_专项检验规范（签字版）.pdf")
        self.assertEqual(r1.exclude_key, "DEMO04-52-DEMOFORM-003")
        self.assertEqual(r1.revision, "R1")

        z = parse_paper_standard_entry("DEMO-CABINET-DOC102-001-Z-08_7_控制柜进货检验规范.pdf")
        self.assertEqual(z.exclude_key, "DEMO-CABINET-DOC102-001")
        self.assertEqual(z.z_suffix, "Z-08")
        self.assertEqual(z.revision, "7")

        z_hash = parse_paper_standard_entry("DEMO05-51-Z0#-001_显控一体机进货检验规范.pdf")
        z_plain = parse_paper_standard_entry("DEMO05-51-Z0-001_显控一体机进货检验规范.pdf")
        self.assertEqual(z_hash.exclude_key, "DEMO05-51")
        self.assertEqual(z_hash.z_suffix, "Z0#-001")
        self.assertEqual(z_plain.exclude_key, "DEMO05-51")
        self.assertEqual(z_plain.z_suffix, "Z0-001")

        szcc = parse_paper_standard_entry("DEMO03-SZCC-T-DEMOFORM-SPRPS-N-01-04_七寸显控进货检验规范书")
        self.assertEqual(szcc.exclude_key, "DEMO03-SZCC-T-DEMOFORM-SPRPS-N-01-04")
        self.assertEqual(szcc.z_suffix, "")

    def test_rt_paper_07_signed_extension_and_title_do_not_affect_matching(self) -> None:
        catalog = _catalog("DEMO04-52-DEMOFORM-003")
        variants = (
            "DEMO04-52-DEMOFORM-003_R1_专项检验规范（签字版）.pdf",
            "DEMO04-52-DEMOFORM-003_1_专项检验规范SIGNED.doc",
            "专项检验规范 DEMO04-52-DEMOFORM-003_R1.docx",
        )
        for raw in variants:
            with self.subTest(raw=raw):
                self.assertEqual(match_paper_standard(raw, catalog).status, MATCH_EXCLUDED)

    def test_rt_paper_08_windchill_code_positions_are_parsed(self) -> None:
        examples = (
            "DEMO04-52-DEMOFORM-003_R1_专项检验规范.pdf",
            "专项检验规范：DEMO04-52-DEMOFORM-003",
            "DEMO04-52-DEMOFORM-003",
        )
        for raw in examples:
            with self.subTest(raw=raw):
                parsed = parse_windchill_document_reference(raw)
                self.assertEqual(parsed.parse_status, PARSE_VALID)
                self.assertEqual(parsed.exclude_key, "DEMO04-52-DEMOFORM-003")

    def test_rt_paper_09_n100_and_n200_do_not_cross_match(self) -> None:
        catalog = _catalog("DEMO-DEMO17-DEMOSYS-T-DEMOFORM-DOC102-001")
        match = match_paper_standard("DEMO-DEMO18-DEMOSYS-T-DEMOFORM-DOC102-001-Z-05_4_检验规范", catalog)

        self.assertEqual(match.status, MATCH_NOT_FOUND)

    def test_rt_paper_10_hash_and_well_are_review_required_not_equivalent(self) -> None:
        catalog = _catalog("JD02#-002")
        match = match_paper_standard("JD02井-002_0_检验规范", catalog)

        self.assertEqual(match.status, MATCH_REVIEW_REQUIRED)
        self.assertIn("需人工检查", match.reason)

    def test_rt_paper_16_catalog_missing_empty_bad_header_and_illegal_rows_fail_closed(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            missing = root / "missing.xlsx"
            with self.assertRaises(PaperStandardExclusionError):
                read_paper_standard_workbook(missing)

            empty = root / "empty.xlsx"
            _write_xlsx(empty, [(1, "文件编号/文件名")])
            with self.assertRaises(PaperStandardExclusionError):
                read_paper_standard_workbook(empty)

            bad_header = root / "bad_header.xlsx"
            _write_xlsx(bad_header, [(1, "错误表头"), (2, "DEMO04-52-DEMOFORM-003_R1_专项检验规范.pdf")])
            with self.assertRaises(PaperStandardExclusionError):
                read_paper_standard_workbook(bad_header)

            illegal = root / "illegal.xlsx"
            _write_xlsx(illegal, [(1, "文件编号/文件名"), (2, "这不是编码")])
            with self.assertRaises(PaperStandardExclusionError):
                read_paper_standard_workbook(illegal)

    def test_rt_paper_17_unparseable_windchill_reference_is_undetermined(self) -> None:
        match = match_paper_standard("只有中文规范名称，没有编码", _catalog("DEMO04-52-DEMOFORM-003"))

        self.assertEqual(match.status, MATCH_UNDETERMINED)


class SyntheticGroupNuconIdentityTests(unittest.TestCase):
    KEY = "DEMO-DEMO17-DEMOSYS-T-DEMOFORM-DOC102-001"
    NAMES = ("DemoControl定型电路板进货检验规范", "DemoControl定型电路板卡进货检验规范")

    @classmethod
    def catalog(cls):
        return PaperStandardCatalog(entries=(parse_paper_standard_entry(
            cls.KEY + "-Z-04_3_" + cls.NAMES[1],
        ),))

    def test_approved_names_unique_paper_without_fabricated_document_code(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                match = match_paper_standard(name, self.catalog())
                self.assertEqual(match.status, MATCH_EXCLUDED)
                self.assertEqual(match.exclude_key, self.KEY)
                self.assertEqual(match.document_code, "")
                self.assertEqual(match.identity_source, "exact_name")
        raw = self.KEY + "-Z-04_3_" + self.NAMES[0]
        match = match_paper_standard(raw, self.catalog())
        self.assertEqual(match.status, MATCH_EXCLUDED)
        self.assertEqual(match.document_code, self.KEY + "-Z-04")
        self.assertEqual(match.identity_source, "document_code")

    def test_explicit_conflicting_multiple_and_hash_well_codes_require_review(self):
        for raw in (
            self.KEY + " OTHER-DOC-001_" + self.NAMES[0],
            "DEMO-DEMO17-DEMOSYS-T-DEMOFORM-DOC102-002_" + self.NAMES[0],
            "DEMO-DEMO18-DEMOSYS-T-DEMOFORM-DOC102-001_" + self.NAMES[1],
            "DEMO-DEMO17-DEMOSYS-T-DEMOFORM-DOC102#-001_" + self.NAMES[0],
            "DEMO-DEMO17-DEMOSYS-T-DEMOFORM-DOC102井-001_" + self.NAMES[0],
        ):
            with self.subTest(raw=raw):
                match = match_paper_standard(raw, self.catalog())
                self.assertEqual(match.status, MATCH_UNDETERMINED)
                self.assertTrue(match.identity_rule_id)
                self.assertEqual(match.identity_source, "document_code")

    def test_missing_duplicate_and_wrong_catalog_identity_fail_closed(self):
        entry = self.catalog().entries[0]
        catalogs = (
            None, PaperStandardCatalog(), PaperStandardCatalog(entries=(entry, entry)),
            _catalog(self.KEY),
            PaperStandardCatalog(entries=(parse_paper_standard_entry(self.KEY + "_0_别的规范"),)),
        )
        for catalog in catalogs:
            with self.subTest(catalog=catalog):
                match = match_paper_standard(self.NAMES[0], catalog)
                self.assertEqual(match.status, MATCH_UNDETERMINED)
                self.assertTrue(match.identity_rule_id)

    def test_unapproved_near_names_remain_ordinary_undetermined(self):
        for name in ("DemoControl定型电路板检查规范", self.NAMES[0] + "补充版", "DemoControl 定型电路板进货检验规范", "普通纯名称"):
            with self.subTest(name=name):
                match = match_paper_standard(name, self.catalog())
                self.assertEqual(match.status, MATCH_UNDETERMINED)
                self.assertEqual(match.identity_rule_id, "")


if __name__ == "__main__":
    unittest.main()
