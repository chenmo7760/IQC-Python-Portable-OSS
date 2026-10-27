"""允收水准诊断：只使用本地夹具，不连接 API/数据库。"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from src.audit_logger import AuditLogger
from src.entry_api import build_entry_draft_payload
from src.generation_service import GenerationError, GenerationService, RuntimeGenerationProvider, SnapshotResolvedOrderProvider
from src.models import Engineer, OrderTask, SourceRow, WindchillName
from src.mysql_query import ActiveUser, ProcedureContext, ProcedureModel, ReadonlySnapshot
from src.sampling_rules import SamplingDecision, SamplingRuleError, resolve_sampling
from src.sql_generator import render_order_sql


ROOT = Path(__file__).resolve().parents[1]
PLAN = "GB2828 正常检查一次抽样方案：IL=Ⅰ"
STANDARD = "□QA1：AQL=2.5 □其他：AQL=4.0"
ITEM = "振动分析机箱|24VDC 12通道 ±0.3%|6卡|QA4"
ENGINEER = Engineer("7", "测试工程师", "1001", "测试工程师")


def fixture(quantity=100, plan=PLAN, standard=STANDARD, item_name=ITEM):
    values = [""] * 16
    values[0], values[3], values[4], values[5], values[9] = "TRACE-TEST", "MAT-TRACE", item_name, "供应商", quantity
    tasks = {"TRACE-TEST": OrderTask("TRACE-TEST", rows=[SourceRow("TRACE-TEST", "MAT-TRACE", "测试", 4, tuple(values))])}
    snapshot = ReadonlySnapshot(
        procedure_models=(ProcedureModel("19", "测试检查单", plan, standard, "1", "0", "2"),),
        procedure_contexts=(ProcedureContext("19", "1", "外观", "无裂纹"),),
        attached_models=(),
        active_users=(ActiveUser("7", "测试工程师", "", "", "1001"),),
    )
    names = (WindchillName("MAT-TRACE", "测试检查单", "SUCCESS"),)
    return tasks, snapshot, names


class AcceptanceDiagnosticsTests(unittest.TestCase):
    def test_qa4_normal_and_single_item_have_distinct_reasons(self):
        for quantity, expected, reason in ((100, 1, "normal_sampling"), (1, 1, "quantity_one")):
            with self.subTest(quantity=quantity):
                trace = {}
                result = resolve_sampling(check_plan=PLAN, accept_standard=STANDARD, item_name=ITEM, quantity=quantity, diagnostics=trace)
                self.assertEqual(result.accept_standard, expected)
                self.assertEqual(trace["reason"], reason)
                self.assertFalse(trace["item_has_qa1"])
                self.assertEqual(trace["quantity"], quantity)
                self.assertEqual(trace["accept_standard"], expected)
                self.assertEqual(trace["check_number_reason"], reason)
                self.assertEqual(trace["acceptance_reason"], "other")
                self.assertEqual(trace["parsed_accept_standard"], 1)
                self.assertEqual(trace["schema"], "iqc-acceptance/88225703-v2")
                if quantity == 1:
                    self.assertIsNone(trace["selected_aql"])
                else:
                    self.assertEqual(trace["selected_aql"], "4.0")

    def test_check_number_branches_do_not_clear_acceptance(self):
        cases = [
            ("加严", STANDARD, 100, "special_acceptance_table", 1),
            (PLAN, STANDARD, 0, "quantity_below_two", 1),
            (PLAN, "零缺陷", 100, "no_aql", -1),
            (PLAN, "AQL=0", 100, "non_positive_aql", -1),
            (PLAN, "□QA1：AQL=0 □其他：AQL=0", 100, "non_positive_aql", 1),
            ("正常检查", STANDARD, 100, "normal_plan_not_applicable", 1),
            ("GB2828 正常 IL=Ⅲ", STANDARD, 500001, "static_sample_unmapped", 1),
            (PLAN, "AQL=4.0", 100, "normal_sampling", -1),
        ]
        for plan, standard, quantity, reason, status in cases:
            with self.subTest(reason=reason):
                trace = {}
                result = resolve_sampling(check_plan=plan, accept_standard=standard, item_name=ITEM, quantity=quantity, diagnostics=trace)
                self.assertEqual(result.accept_standard, status)
                self.assertEqual(trace["reason"], reason)
                if reason == "normal_plan_not_applicable":
                    self.assertEqual(trace["parsed_accept_standard"], 1)
                    self.assertEqual(trace["accept_standard"], 1)

    def test_parse_failure_has_no_invented_minus_one_result(self):
        trace = {"accept_standard": 1}
        with self.assertRaises(SamplingRuleError):
            resolve_sampling(check_plan=PLAN, accept_standard="AQL=1.25", item_name=ITEM, quantity=100, diagnostics=trace)
        self.assertEqual(trace["reason"], "rule_error")
        self.assertEqual(trace["status"], "ERROR")
        self.assertNotIn("accept_standard", trace)

    def test_qa1_token_and_numeric_snapshot_preserve_existing_semantics(self):
        for name, status in (("物料|QA1", 0), ("物料|QA10", 1), ("物料|XQA1", 1)):
            trace = {}
            result = resolve_sampling(check_plan=PLAN, accept_standard=STANDARD, item_name=name, quantity=100, diagnostics=trace)
            self.assertEqual(result.accept_standard, status)
        trace = {}
        result = resolve_sampling(check_plan=-1, accept_standard=0, item_name=ITEM, quantity=100, diagnostics=trace)
        self.assertEqual(result, SamplingDecision(None, 0))
        self.assertEqual(trace["reason"], "numeric_snapshot_status")

    def test_runtime_event_and_diagnostic_agree_with_sql_and_api_payload(self):
        for quantity, item_name, status in ((100, ITEM, 1), (1, ITEM, 1), (1, ITEM[:-1] + "1", 0)):
            tasks, snapshot, names = fixture(quantity, plan="GB2828 正常检查一次抽样方案：IL=S-1", item_name=item_name)

            class Query:
                def load_snapshot(self):
                    return snapshot

            class Names:
                def query_names(self, codes):
                    return names

            with self.subTest(quantity=quantity), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                batch = GenerationService(
                    ROOT / "template" / "iqc_draft_template.xlsx", root,
                    RuntimeGenerationProvider(Query(), Names()),
                ).generate(tasks, ENGINEER)
                self.assertEqual(batch.errors, {})
                event = next(event for event in batch.events if event.name == "sampling_decision")
                outcome = batch.outcomes[0]
                record = outcome.resolved_order.sql_order.records[0]
                diagnostic = json.loads(outcome.artifacts.diagnostic_path.read_text(encoding="utf-8"))["records"][0]
                self.assertEqual(event.fields["accept_standard"], status)
                self.assertEqual(diagnostic["sampling_diagnostic"]["accept_standard"], status)
                self.assertEqual(diagnostic["accept_standard"], status)
                self.assertEqual(diagnostic["sampling_diagnostic"]["source_rows"], "4")
                self.assertEqual(diagnostic["sampling_diagnostic"]["item_name"], item_name)
                if quantity == 1:
                    self.assertEqual(record.check_number, 1)
                    self.assertEqual(event.fields["check_number_reason"], "quantity_one")
                    self.assertEqual(event.fields["acceptance_reason"], "qa1" if status == 0 else "other")
                    self.assertNotIn("允收水准未选中", event.detail)
                # 诊断字段不会改变 SQL，也不会混入 API 表单。
                sql_order = outcome.resolved_order.sql_order
                without_trace = replace(sql_order, records=(replace(record, sampling_diagnostic={}),))
                self.assertEqual(render_order_sql(sql_order), render_order_sql(without_trace))
                payload = build_entry_draft_payload(record, order_number="TRACE-TEST")
                self.assertEqual(payload["procedureDraftAcceptStandard"], status)
                self.assertIsNone(payload["procedureDraftCheckNumber"])
                self.assertNotIn("sampling_diagnostic", payload)
                log = AuditLogger(root / "test.jsonl")
                log.event(event.name, order_number=event.order_number, status=event.status, detail=event.detail, **event.fields)
                line = json.loads(log.tail(1)[0])
                self.assertEqual(line["fields"]["accept_standard"], str(status))
                self.assertEqual(line["fields"]["reason"], diagnostic["sampling_diagnostic"]["reason"])

    def test_diagnostic_observer_failure_cannot_change_generation(self):
        tasks, snapshot, names = fixture()

        def broken(payload):
            raise OSError("log unavailable")

        result = SnapshotResolvedOrderProvider(snapshot, names, event_callback=broken).resolve(tasks, ENGINEER)
        self.assertEqual(result["TRACE-TEST"].sql_order.records[0].accept_standard, 1)

    def test_generation_error_is_logged_and_text_is_redacted(self):
        tasks, snapshot, names = fixture(standard="AQL=1.25 password=test-secret")
        events = []
        with self.assertRaises(GenerationError):
            SnapshotResolvedOrderProvider(snapshot, names, event_callback=events.append).resolve(tasks, ENGINEER)
        event = next(event for event in events if event["name"] == "sampling_decision")
        self.assertEqual(event["status"], "ERROR")
        self.assertNotIn("accept_standard", event)
        self.assertNotIn("test-secret", json.dumps(event))

    def test_current_export_helper_keeps_boxes_only_for_minus_one(self):
        # 仅提取纯函数 AST，不导入 Django，也不启动导出或数据库。
        path = ROOT / "auto-factory/backend/dvadmin/qcauto/views/procedurerecord.py"
        if not path.is_file():
            self.skipTest("便携包不含后端源码；该导出纯函数检查在完整工作区执行")
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "get_option_by_index"]
        self.assertEqual(len(functions), 1)
        module = ast.Module(body=functions, type_ignores=[])
        namespace = {}
        exec(compile(module, str(path), "exec"), namespace)
        export = namespace["get_option_by_index"]
        self.assertEqual(export(None, STANDARD, -1), STANDARD)
        self.assertEqual(export(None, STANDARD, 0), "QA1：AQL=2.5")
        self.assertEqual(export(None, STANDARD, 1), "其他：AQL=4.0")


if __name__ == "__main__":
    unittest.main()
