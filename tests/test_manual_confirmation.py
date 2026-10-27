from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.generation_service import GenerationService, GenerationError, SnapshotResolvedOrderProvider
from src.manual_confirmation import (
    ManualConfirmationRequired,
    build_confirmation_row,
    rank_template_candidates,
    validate_confirmation_choices,
    write_confirmation_draft,
)
from src.models import Engineer, OrderTask, SourceRow, WindchillName, normalize_procedure_name
from src.mysql_query import ActiveUser, ProcedureContext, ProcedureModel, ReadonlySnapshot
from src.source_excel import read_order_tasks


class ManualConfirmationTests(unittest.TestCase):
    def test_windchill_book_name_colon_document_number_normalizes_to_template_name(self) -> None:
        raw = "《合成类别01进货检查单》：DEMO-C-DEMOFORM-DEMOUNIT-EMBB********-01"
        self.assertEqual(normalize_procedure_name(raw), "合成类别01进货检查单")

    def _models(self):
        return (
            ProcedureModel("42", "合成类别06进货检查单", "-1", "0", "1", "0", "2"),
            ProcedureModel("43", "导线类进货检验单", "-1", "0", "1", "0", "2"),
            ProcedureModel("99", "合成类别06进货检查单（旧）", "-1", "0", "1", "1", "2"),
        )

    def test_candidates_are_active_stable_and_explain_basis(self) -> None:
        candidates = rank_template_candidates(self._models(), "合成类别06进货检查单")
        self.assertEqual([item.model_id for item in candidates], ["42", "43"])
        self.assertEqual(candidates[0].basis, "normalized_exact")
        self.assertTrue(all(item.active for item in candidates))

    def test_validation_requires_candidate_for_checked_row_and_preserves_decline(self) -> None:
        row = build_confirmation_row(
            order_number="3608590",
            item_code="MAT-1",
            raw_name="Windchill原始名",
            normalized_name="合成类别06进货检查单",
            models=self._models(),
            blocking_reason="匹配数量=0",
        )
        self.assertEqual(validate_confirmation_choices((row,), {"MAT-1": None}), {"MAT-1": None})
        selected = validate_confirmation_choices((row,), {"MAT-1": "42"})
        self.assertEqual(selected["MAT-1"].model_id, "42")
        with self.assertRaisesRegex(ValueError, "未选择模板"):
            validate_confirmation_choices((row,), {"MAT-1": ""})
        with self.assertRaisesRegex(ValueError, "无效"):
            validate_confirmation_choices((row,), {"MAT-1": "999"})

    def test_confirmation_json_is_independent_and_auditable(self) -> None:
        row = build_confirmation_row(
            order_number="3608590",
            item_code="MAT-1",
            raw_name="《原始名》：ABC-01",
            normalized_name="合成类别06进货检查单",
            models=self._models(),
            blocking_reason="自动正则/精确匹配未得到唯一有效模板",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = write_confirmation_draft(Path(temp) / "manual.json", (row,), decisions={"MAT-1": None}, status="CONFIRMED")
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], "manual_template_confirmation.v1")
        self.assertEqual(payload["rows"][0]["decision_state"], "declined")
        self.assertEqual(payload["rows"][0]["candidates"][0]["basis"], "normalized_exact")

    def test_confirmation_uses_composite_key_for_duplicate_item_codes(self) -> None:
        rows = tuple(
            build_confirmation_row(
                order_number=order,
                item_code="MAT-DUP",
                raw_name="原始名",
                normalized_name="合成类别06进货检查单",
                models=self._models(),
                blocking_reason="匹配数量=0",
            )
            for order in ("TK001", "TK002")
        )
        choices = {row.confirmation_key: "42" for row in rows}
        decisions = validate_confirmation_choices(rows, choices)
        self.assertEqual(set(decisions), {"TK001::MAT-DUP", "TK002::MAT-DUP"})

    def test_generation_stops_without_artifacts_and_resumes_only_selected_rows(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        snapshot = ReadonlySnapshot(
            procedure_models=self._models(),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        names = (
            WindchillName("MAT-001", "完全无法自动匹配的名称", "SUCCESS"),
            WindchillName("MAT-002", "完全无法自动匹配的名称", "SUCCESS"),
        )
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            first = GenerationService(
                root / "template" / "iqc_draft_template.xlsx", output,
                SnapshotResolvedOrderProvider(snapshot, names),
            ).generate(tasks, engineer)
            self.assertFalse(first.outcomes)
            self.assertTrue(first.manual_confirmation_rows)
            self.assertEqual(list(output.iterdir()), [])
            selected = {row.item_code: row.candidates[0].model_id for row in first.manual_confirmation_rows}
            declined = dict(selected)
            declined["MAT-002"] = None
            resumed = GenerationService(
                root / "template" / "iqc_draft_template.xlsx", output,
                SnapshotResolvedOrderProvider(snapshot, names, manual_confirmations=declined),
            ).generate(tasks, engineer)
            # 未勾选行仅放弃该物项；同一送检单仍可生成其余已确认物项。
            self.assertEqual(resumed.errors, {})
            self.assertEqual(len(resumed.outcomes), 1)
            diagnostic = json.loads(resumed.outcomes[0].artifacts.diagnostic_path.read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["record_count"], 1)

    def test_generation_resumes_when_every_unresolved_row_is_confirmed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("42", "合成类别06进货检查单", "-1", "0", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        names = (
            WindchillName("MAT-001", "完全无法自动匹配的名称", "SUCCESS"),
            WindchillName("MAT-002", "完全无法自动匹配的名称", "SUCCESS"),
        )
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        provider = SnapshotResolvedOrderProvider(snapshot, names)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            first = GenerationService(root / "template" / "iqc_draft_template.xlsx", output, provider).generate(tasks, engineer)
            self.assertEqual(len(first.manual_confirmation_rows), 2)
            # 同一候选 ID 由操作员逐行确认后，才允许写出四类产物。
            decisions = {row.item_code: "42" for row in first.manual_confirmation_rows}
            resumed = GenerationService(
                root / "template" / "iqc_draft_template.xlsx", output,
                SnapshotResolvedOrderProvider(snapshot, names, manual_confirmations=decisions),
            ).generate(tasks, engineer)
            self.assertEqual(resumed.errors, {})
            self.assertEqual(len(resumed.outcomes), 1)
            self.assertTrue(resumed.outcomes[0].artifacts.sql_path.is_file())

    def test_manual_name_alone_resolves_unique_active_model_and_is_written_to_diagnostic(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("42", "合成类别06进货检查单", "-1", "0", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        names = (
            WindchillName("MAT-001", "无法自动匹配的名称", "SUCCESS"),
            WindchillName("MAT-002", "无法自动匹配的名称", "SUCCESS"),
        )
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            decisions = {
                "MAT-001": {"manual_name": "合成类别06进货检查单"},
                "MAT-002": None,
            }
            resumed = GenerationService(
                root / "template" / "iqc_draft_template.xlsx", output,
                SnapshotResolvedOrderProvider(snapshot, names, manual_confirmations=decisions),
            ).generate(tasks, engineer)
            self.assertEqual(resumed.errors, {})
            diagnostic = json.loads(resumed.outcomes[0].artifacts.diagnostic_path.read_text(encoding="utf-8"))
            item = diagnostic["records"][0] if "records" in diagnostic else diagnostic[0]
            self.assertEqual(item["manual_input_basis"], "manual_name_unique_active")
            self.assertEqual(item["manual_input_name"], "合成类别06进货检查单")

    def test_declining_every_row_of_one_order_does_not_block_other_orders(self) -> None:
        """整单放弃不写产物，但同批其他单号仍可按确认结果生成。"""

        root = Path(__file__).resolve().parents[1]
        base = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])["DEMO-ORDER-001"]

        def remap_task(order_number: str, codes: tuple[str, str]) -> OrderTask:
            rows: list[SourceRow] = []
            for source_row, code in zip(base.rows, codes):
                values = list(source_row.values)
                values[0] = order_number
                values[3] = code
                rows.append(replace(source_row, order_number=order_number, item_code=code, values=tuple(values)))
            return OrderTask(order_number, rows=rows)

        tasks = {
            "DEMO-ORDER-001": base,
            "4750393": remap_task("4750393", ("MAT-003", "MAT-004")),
        }
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("42", "合成类别06进货检查单", "-1", "0", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        names = (
            WindchillName("MAT-001", "无法自动匹配的名称", "SUCCESS"),
            WindchillName("MAT-002", "无法自动匹配的名称", "SUCCESS"),
            WindchillName("MAT-003", "无法自动匹配的名称", "SUCCESS"),
            WindchillName("MAT-004", "无法自动匹配的名称", "SUCCESS"),
        )
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        provider = SnapshotResolvedOrderProvider(snapshot, {"DEMO-ORDER-001": names[:2], "4750393": names[2:]})
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            first = GenerationService(root / "template" / "iqc_draft_template.xlsx", output, provider).generate(tasks, engineer)
            self.assertEqual(len(first.manual_confirmation_rows), 4)
            decisions = {
                "MAT-001": "42",
                "MAT-002": "42",
                "MAT-003": None,
                "MAT-004": None,
            }
            resumed = GenerationService(
                root / "template" / "iqc_draft_template.xlsx",
                output,
                SnapshotResolvedOrderProvider(
                    snapshot,
                    {"DEMO-ORDER-001": names[:2], "4750393": names[2:]},
                    manual_confirmations=decisions,
                ),
            ).generate(tasks, engineer)
            self.assertEqual(len(resumed.outcomes), 1)
            self.assertEqual(resumed.outcomes[0].order_number, "DEMO-ORDER-001")
            self.assertEqual(set(resumed.errors), {"4750393"})
            self.assertTrue((output / "iqc_draft_DEMO-ORDER-001.xlsx").is_file())
            self.assertFalse((output / "iqc_draft_4750393.xlsx").exists())


if __name__ == "__main__":
    unittest.main()

