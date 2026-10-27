from __future__ import annotations

import unittest
from datetime import datetime

from src.mysql_query import validate_readonly_sql
from src.pqc_query import PQC_TEMPLATE_NAMES, PqcQueryError, PqcReadonlyQueryService


class FakeReadonlyClient:
    def __init__(self):
        self.calls = []
        self.rows = {}

    def execute(self, sql, query_label=None):
        validate_readonly_sql(sql)
        self.calls.append((query_label, sql))
        return self.rows.get(query_label, [])


class PqcQueryV4Tests(unittest.TestCase):
    def setUp(self):
        self.client = FakeReadonlyClient()
        self.service = PqcReadonlyQueryService(self.client)

    def test_templates_query_exact_names_and_maps_business_key(self):
        self.client.rows["pqc_templates"] = [["1", "CAB-01", PQC_TEMPLATE_NAMES[0], "2", "1", "2", "0"]]
        templates = self.service.load_templates()
        self.assertEqual(templates[0].procedure_name, PQC_TEMPLATE_NAMES[0])
        self.assertEqual(templates[0].template_key, "CABINET_ASSEMBLY")
        sql = self.client.calls[-1][1]
        self.assertIn("procedure_name", sql)
        self.assertNotIn("procedure_code IN", sql)

    def test_text_comparisons_use_one_utf8mb4_collation(self):
        self.client.rows["pqc_templates"] = [["1", "CAB-01", PQC_TEMPLATE_NAMES[0], "2", "1", "2", "0"]]
        self.service.load_templates()
        sql = self.client.calls[-1][1]
        self.assertIn("CONVERT(TRIM(COALESCE(procedure_name, '')) USING utf8mb4) COLLATE utf8mb4_unicode_ci", sql)
        self.assertGreaterEqual(sql.count("COLLATE utf8mb4_unicode_ci"), 2)

        self.service.load_formal_records("P")
        formal_sql = self.client.calls[-1][1]
        self.assertIn("CONVERT(TRIM(COALESCE(r.project_number, '')) USING utf8mb4) COLLATE utf8mb4_unicode_ci", formal_sql)

    def test_ss_is_generated_from_inspection_slot_and_ignores_proof_map(self):
        self.client.rows["pqc_templates"] = [["1", "CAB-01", PQC_TEMPLATE_NAMES[0], "2", "1", "2", "0"]]
        self.service = PqcReadonlyQueryService(self.client, template_suffix_map={"CABINET_ASSEMBLY": "17"})
        templates = self.service.load_templates()
        self.assertEqual(templates[0].form_suffix, "")
        self.client.rows["pqc_templates"] = [["1", "CAB-01", PQC_TEMPLATE_NAMES[0], "2", "1", "2", "0"]]
        self.service = PqcReadonlyQueryService(self.client)
        self.assertEqual(self.service.load_templates()[0].form_suffix, "")

    def test_all_repository_reads_are_select_only(self):
        self.client.rows = {
            "pqc_templates": [["1", "CAB-01", PQC_TEMPLATE_NAMES[0], "2", "1", "2", "0"]],
            "pqc_formal_records": [["10", "P", "D", "CAB-01", PQC_TEMPLATE_NAMES[0], "2", "P-001-01"]],
            "pqc_drafts": [["20", "P", "D", "CAB-02", PQC_TEMPLATE_NAMES[1], "P-001-02", "Engineer", "2026-09-06 01:02:03"]],
            "pqc_tools": [["30", "数字万用表", "SN-1", "2027-01-01"]],
        }
        snapshot = self.service.load_project_snapshot("P")
        self.assertEqual(snapshot.formal_records[0].trigger_basis, "P-001-01")
        self.assertEqual(snapshot.formal_records[0].template_key, "CABINET_ASSEMBLY")
        self.assertEqual(snapshot.formal_records[0].template_name, PQC_TEMPLATE_NAMES[0])
        self.assertEqual(snapshot.drafts[0].qc_engineer, "Engineer")
        self.assertEqual(snapshot.drafts[0].template_key, "CABINET_WIRING")
        for _label, sql in self.client.calls:
            self.assertEqual(sql.lstrip().split(None, 1)[0].upper(), "SELECT")
            self.assertNotRegex(sql.upper(), r"\b(INSERT|UPDATE|DELETE|REPLACE|ALTER|DROP|CREATE)\b")

    def test_business_values_are_hex_encoded(self):
        dangerous = "P' OR 1=1 --"
        self.service.load_formal_records(dangerous)
        sql = self.client.calls[-1][1]
        self.assertNotIn(dangerous, sql)
        self.assertIn(dangerous.encode("utf-8").hex(), sql)
        self.assertIn("CONVERT(0x", sql)

    def test_matching_readback_uses_composite_template_key_and_window(self):
        self.client.rows["pqc_matching_drafts"] = [["d1", " P ", " D ", "CAB-05", "扭矩质量检查记录单", " P-001-05 ", " Engineer ", "2026-09-06 02:03:04"]]
        result = self.service.matching_drafts("P", "D", "CAB-05", "P-001-05", "Engineer", template_key="TORQUE", created_not_before=datetime(2026, 9, 6, 2, 3, 0, 999999))
        self.assertEqual(result[0].draft_id, "d1")
        sql = self.client.calls[-1][1]
        self.assertIn("procedure_name", sql)
        self.assertIn("create_datetime", sql)
        self.assertIn("2026-09-06 02:03:00".encode("utf-8").hex(), sql)

    def test_invalid_values_are_rejected_before_execute(self):
        with self.assertRaises(PqcQueryError):
            self.service.load_drafts("")
        with self.assertRaises(PqcQueryError):
            self.service.matching_drafts("P", "D", "CAB-01", "P-001-01", "")
        with self.assertRaises(PqcQueryError):
            self.service.matching_drafts("P", "D", "CAB-01", "P-001-01", "Engineer", created_not_before="bad")


if __name__ == "__main__":
    unittest.main()
