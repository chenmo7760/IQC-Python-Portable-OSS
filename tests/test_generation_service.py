from __future__ import annotations

import tempfile
import unittest
import unittest.mock
import json
from dataclasses import replace
from pathlib import Path

from src.config import AppPaths
from src.generation_service import (
    GenerationError,
    GenerationService,
    ResolvedOrder,
    RuntimeGenerationProvider,
    SnapshotResolvedOrderProvider,
    UnconfiguredGenerationProvider,
)
from src.main import build_generation_service
from src.mysql_query import ActiveUser, AttachedModel, MySqlQueryError, ProcedureContext, ProcedureModel, ReadonlySnapshot
from src.models import Engineer, OrderTask, SourceRow, WindchillName
from src.paper_standard_exclusion import (
    MATCH_EXCLUDED,
    PaperStandardCatalog,
    PaperStandardEntry,
)
from src.procedure_alias import ProcedureAlias, ProcedureAliasResolver
from src.sql_generator import SqlContextRow, SqlOrder, SqlRecord, render_order_sql
from src.source_excel import read_order_tasks
from src.template_writer import assemble_order_draft
from src.windchill_adapter import MockWindchillAdapter


class _ResolvedProvider:
    def resolve(self, tasks, engineer):
        result = {}
        names = MockWindchillAdapter({"MAT-001": "检查单_示例", "MAT-002": "检查单_示例"}).query_names(["MAT-001", "MAT-002"])
        for order_number, task in tasks.items():
            draft = assemble_order_draft(task, names, engineer)
            records = tuple(
                SqlRecord(
                    item_code=row.item_code,
                    item_name=str(row.values[4]),
                    quantity=row.values[9],
                    supplier=str(row.values[5]),
                    procedure_name="检查单_示例",
                    procedure_model_id=42,
                    engineer_id=7,
                    engineer_name=engineer.display_name,
                    engineer_dept_id="1001",
                    trigger_basis=order_number,
                    check_number=None,
                    contexts=(SqlContextRow("1", "外观", "无裂纹"),),
                )
                for row in task.rows
            )
            result[order_number] = ResolvedOrder(draft, SqlOrder(order_number, records))
        return result


class _FailOneProvider(_ResolvedProvider):
    def resolve(self, tasks, engineer):
        if "A-FAIL" in tasks:
            raise GenerationError("A-FAIL 模型未解析")
        return super().resolve(tasks, engineer)


class _MismatchedProvider(_ResolvedProvider):
    def resolve(self, tasks, engineer):
        resolved = super().resolve(tasks, engineer)
        return {
            order_number: ResolvedOrder(
                replace(item.order_draft, order_number="WRONG"),
                replace(item.sql_order, order_number="WRONG"),
            )
            for order_number, item in resolved.items()
        }


class _LeakyProvider:
    def resolve(self, tasks, engineer):
        raise RuntimeError("mysql://demo_user:super-secret@192.0.2.8:3306/example_database connection failed")


class _SnapshotLoader:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def load_snapshot(self):
        self.calls += 1
        return self.snapshot


class _NameAdapter:
    def __init__(self):
        self.calls = 0

    def query_names(self, item_codes):
        self.calls += 1
        return MockWindchillAdapter({"MAT-001": "检查单_示例", "MAT-002": "检查单_示例"}).query_names(list(item_codes))


class _FailingSnapshotLoader:
    def load_snapshot(self):
        raise MySqlQueryError(
            "MySQL 只读查询失败",
            query_stage="procedure_models",
            exit_code=1,
            safe_detail="表不存在；credential redacted",
        )


class _SpyQueryService:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0
        self.callback = None

    def set_event_callback(self, callback) -> None:
        self.callback = callback

    def load_snapshot(self):
        self.calls += 1
        return self.snapshot


class _SpyWindchillAdapter:
    def __init__(self, names):
        self.names = dict(names)
        self.calls = 0
        self.requested_codes: list[tuple[str, ...]] = []

    def query_names(self, item_codes):
        codes = tuple(item_codes)
        self.calls += 1
        self.requested_codes.append(codes)
        return tuple(WindchillName(code, self.names[code], "SUCCESS") for code in codes)


class _SpyAliasResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def resolve(self, *, item_code, source_name, normalized_name):
        self.calls.append((str(item_code), str(normalized_name)))
        return None


def _source_row(order_number: str, item_code: str, *, item_name: str | None = None) -> SourceRow:
    values = [""] * 16
    values[0] = order_number
    values[3] = item_code
    values[4] = item_name or f"{item_code}物料"
    values[5] = "测试供应商"
    values[9] = "1"
    return SourceRow(order_number, item_code, "Sheet1", 2, tuple(values))


def _task(order_number: str, *item_codes: str) -> OrderTask:
    return OrderTask(order_number, [_source_row(order_number, code) for code in item_codes])


def _paper_catalog(*keys: str) -> PaperStandardCatalog:
    return PaperStandardCatalog(
        entries=tuple(
            PaperStandardEntry(
                sheet_name="Sheet1",
                source_row=index + 2,
                raw_value=f"{key}_0_纸质规范.pdf",
                document_code=key,
                exclude_key=key,
                parse_status="VALID",
            )
            for index, key in enumerate(keys)
        )
    )


def _snapshot(*names: str) -> ReadonlySnapshot:
    models = tuple(
        ProcedureModel(str(index + 42), name, "-1", "0", "1", "0", "2")
        for index, name in enumerate(names)
    )
    contexts = tuple(ProcedureContext(model.id, "1", "外观", "无裂纹") for model in models)
    return ReadonlySnapshot(
        procedure_models=models,
        procedure_contexts=contexts,
        attached_models=(),
        active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
    )


class GenerationServiceTests(unittest.TestCase):
    def test_snapshot_provider_applies_explicit_alias_but_preserves_source_output(self) -> None:
        values = [""] * 16
        values[0] = "3608590"
        values[3] = "MAT-ALIAS"
        values[4] = "示例物料"
        values[5] = "示例供应商"
        values[9] = "1"
        task = OrderTask(
            "3608590",
            rows=[SourceRow("3608590", "MAT-ALIAS", "3月", 4, tuple(values))],
        )
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("77", "目标检查单", "-1", "AQL=1.5", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("77", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        aliases = ProcedureAliasResolver(
            [
                ProcedureAlias(
                    True,
                    "MAT-ALIAS",
                    "",
                    "",
                    "目标检查单",
                    "测试映射",
                    "v1",
                    "2026-08-31",
                    "test-alias",
                )
            ]
        )
        events: list[dict[str, object]] = []
        resolved = SnapshotResolvedOrderProvider(
            snapshot,
            (WindchillName("MAT-ALIAS", "Windchill未规范名称", "SUCCESS"),),
            event_callback=events.append,
            procedure_aliases=aliases,
        ).resolve({"3608590": task}, Engineer("7", "合成工程师02", "1001", "合成工程师02"))
        record = resolved["3608590"].sql_order.records[0]
        self.assertEqual(record.procedure_model_id, 77)
        self.assertEqual(record.procedure_name, "Windchill未规范名称")
        self.assertEqual(resolved["3608590"].order_draft.rows[0].procedure_name, "Windchill未规范名称")
        model_event = next(item for item in events if item.get("name") == "model_match")
        self.assertTrue(model_event["alias_applied"])
        self.assertEqual(model_event["target_procedure_name"], "目标检查单")
        self.assertEqual(model_event["raw_name"], "Windchill未规范名称")

    def test_snapshot_provider_migrates_legacy_textual_sampling_fields(self) -> None:
        """模型表的文本方案按旧 BAS 规则转成草稿整数/抽样数量。"""

        def row(order: str, code: str, name: str, quantity: str) -> SourceRow:
            values = [""] * 16
            values[0] = order
            values[3] = code
            values[4] = name
            values[5] = "示例供应商"
            values[9] = quantity
            return SourceRow(order, code, "3月", 4, tuple(values))

        snapshot = ReadonlySnapshot(
            procedure_models=(
                ProcedureModel(
                    "19",
                    "合成类别06进货检查单",
                    "GB2828 正常检查一次抽样方案：IL=Ⅰ",
                    "□QA1 ：AQL=1.0          □其他：AQL=1.5",
                    "1",
                    "0",
                    "2",
                ),
            ),
            procedure_contexts=(ProcedureContext("19", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        tasks = {
            "8478993": OrderTask(
                "8478993",
                rows=[
                    row("8478993", "DEMO-MAT-034", "导线2.5mm²棕色 低烟无卤阻燃QA1", "1900.00"),
                    row("8478993", "DEMO-MAT-016", "双绞线2×1×1.0mm²低烟无卤阻燃QA4", "100.00"),
                ],
            ),
            "3555913": OrderTask(
                "3555913",
                rows=[row("3555913", "DEMO-MAT-023", "导线0.75mm² 白 WDZ", "8000.00")],
            ),
        }
        names = (
            WindchillName("DEMO-MAT-034", "DEMO-C-DEMOFORM-DEMOUNIT-PMBG********-01-##_合成类别06进货检查单", "SUCCESS"),
            WindchillName("DEMO-MAT-016", "DEMO-C-DEMOFORM-DEMOUNIT-PMBG********-01-##_合成类别06进货检查单", "SUCCESS"),
            WindchillName("DEMO-MAT-023", "DEMO-C-DEMOFORM-DEMOUNIT-PMBG********-01-##_合成类别06进货检查单", "SUCCESS"),
        )

        resolved = SnapshotResolvedOrderProvider(snapshot, {
            "8478993": names[:2],
            "3555913": names[2:],
        }).resolve(tasks, Engineer("7", "合成工程师02", "1001", "合成工程师02"))

        records = resolved["8478993"].sql_order.records
        self.assertEqual([(record.check_number, record.accept_standard, record.check_plan) for record in records], [(50, 0, -1), (8, 1, -1)])
        record_3555913 = resolved["3555913"].sql_order.records[0]
        self.assertEqual((record_3555913.check_number, record_3555913.accept_standard), (80, 1))

    def test_snapshot_provider_qa1_matches_business_item_code_when_legacy_code_is_placeholder(self) -> None:
        """QA1 候选以 Excel D item_code 命中适用范围，C=N/A 仅保留审计。"""

        values = [""] * 16
        values[0] = "QA1-ORDER"
        values[3] = "DEMO-MAT-033"
        values[4] = "关键零部件示例"
        values[5] = "供应商"
        values[9] = "10"
        task = OrderTask(
            "QA1-ORDER",
            rows=[SourceRow("QA1-ORDER", "DEMO-MAT-033", "批次", 4, tuple(values))],
        )
        model = ProcedureModel(
            "901",
            "检查单_示例",
            "-1",
            "AQL=1.0",
            "1",
            "0",
            "2",
            apply_range="适用于关键零部件 DEMO-MAT-033/DEMO-MAT-007",
        )
        snapshot = ReadonlySnapshot(
            procedure_models=(model,),
            procedure_contexts=(ProcedureContext("901", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        events: list[dict[str, object]] = []
        names = (
            WindchillName(
                "DEMO-MAT-033",
                "检查单_示例",
                "SUCCESS",
                legacy_item_code="N/A",
                quality_grade="QA1",
            ),
        )

        resolved = SnapshotResolvedOrderProvider(snapshot, names, event_callback=events.append).resolve(
            {"QA1-ORDER": task}, Engineer("7", "合成工程师02", "1001", "合成工程师02")
        )

        qa1 = resolved["QA1-ORDER"].qa1_drafts
        self.assertEqual(len(qa1), 1)
        self.assertEqual(qa1[0].legacy_item_code, "N/A")
        self.assertEqual(qa1[0].matched_by, "apply_range")
        self.assertEqual(qa1[0].selected_model.id, "901")
        self.assertFalse(any(event.get("name") == "key_component_no_match" for event in events))

    def test_generate_writes_template_and_three_artifacts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            batch = GenerationService(root / "template" / "iqc_draft_template.xlsx", output, _ResolvedProvider()).generate(tasks, engineer)
            self.assertEqual(batch.errors, {})
            self.assertEqual(len(batch.outcomes), 1)
            outcome = batch.outcomes[0]
            self.assertTrue(outcome.result_path.is_file())
            self.assertTrue(outcome.artifacts.sql_path.is_file())
            self.assertTrue(outcome.artifacts.bat_path.is_file())
            self.assertTrue(outcome.artifacts.diagnostic_path.is_file())
            self.assertEqual(outcome.result_path.name, "iqc_draft_DEMO-ORDER-001.xlsx")
            self.assertEqual(outcome.artifacts.sql_path.name, "iqc_draft_DEMO-ORDER-001.sql")
            self.assertEqual(outcome.artifacts.bat_path.name, "iqc_import_DEMO-ORDER-001.bat")
            self.assertEqual(outcome.artifacts.diagnostic_path.name, "iqc_draft_DEMO-ORDER-001.diagnostic.json")
            self.assertNotIn("measurement_unit", outcome.artifacts.sql_path.read_text(encoding="utf-8").lower())
            preflight = next(event for event in batch.events if event.name == "provider_preflight")
            self.assertEqual(preflight.status, "UNKNOWN")
            self.assertEqual(preflight.fields["preflight_basis"], "provider_fields_not_exposed")
            self.assertFalse(preflight.fields["snapshot_field_exposed"])
            self.assertFalse(preflight.fields["windchill_results_field_exposed"])

    def test_unconfigured_provider_is_an_explicit_generation_block(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        batch = GenerationService(
            root / "template" / "iqc_draft_template.xlsx",
            root / "输出",
            UnconfiguredGenerationProvider(),
        ).generate(tasks, engineer)
        self.assertEqual(batch.outcomes, ())
        self.assertRegex(batch.errors["DEMO-ORDER-001"], "尚未配置")
        event_names = [event.name for event in batch.events]
        self.assertEqual(
            event_names,
            [
                "generation_start",
                "provider_preflight",
                "generation_resolve_start",
                "generation_resolve_error",
            ],
        )
        preflight = batch.events[1]
        self.assertEqual(preflight.fields["provider_type"], "UnconfiguredGenerationProvider")
        self.assertFalse(preflight.fields["readonly_snapshot_injected"])
        self.assertFalse(preflight.fields["windchill_adapter_injected"])
        self.assertFalse(preflight.fields["windchill_name_results_injected"])
        self.assertNotIn("password", repr(batch.events).lower())

    def test_provider_failure_diagnostics_redact_connection_values(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        batch = GenerationService(
            root / "template" / "iqc_draft_template.xlsx",
            root / "输出",
            _LeakyProvider(),
        ).generate(tasks, engineer)
        self.assertIn("RuntimeError", batch.errors["DEMO-ORDER-001"])
        serialized = repr(batch.events) + repr(batch.errors)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("192.0.2.8", serialized)
        self.assertNotIn("demo_user", serialized)

    def test_generation_isolates_one_order_failure_from_other_orders(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source_tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        rows = source_tasks["DEMO-ORDER-001"].rows
        tasks = {
            "A-FAIL": OrderTask("A-FAIL", rows=list(rows)),
            "B-OK": OrderTask("B-OK", rows=list(rows)),
        }
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        with tempfile.TemporaryDirectory() as temp:
            batch = GenerationService(
                root / "template" / "iqc_draft_template.xlsx",
                Path(temp),
                _FailOneProvider(),
            ).generate(tasks, engineer)
            self.assertIn("A-FAIL", batch.errors)
            self.assertIn("模型未解析", batch.errors["A-FAIL"])
            self.assertEqual(batch.errors.get("B-OK"), None)
            self.assertEqual([item.order_number for item in batch.outcomes], ["B-OK"])
            self.assertTrue(batch.outcomes[0].artifacts.sql_path.is_file())

    def test_generation_rejects_resolved_order_number_mismatch_before_writing(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            batch = GenerationService(
                root / "template" / "iqc_draft_template.xlsx",
                output,
                _MismatchedProvider(),
            ).generate(tasks, engineer)
            self.assertEqual(batch.outcomes, ())
            self.assertIn("单号与请求不一致", batch.errors["DEMO-ORDER-001"])
            self.assertEqual(list(output.iterdir()), [])

    def test_snapshot_generation_isolates_model_failure_by_order(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source_tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        rows = source_tasks["DEMO-ORDER-001"].rows
        tasks = {
            "A-FAIL": OrderTask("A-FAIL", rows=list(rows)),
            "B-OK": OrderTask("B-OK", rows=list(rows)),
        }
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("42", "检查单_示例", "-1", "0", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        per_order_names = {
            "A-FAIL": tuple(
                WindchillName(code, "不存在的检查单", "SUCCESS")
                for code in ("MAT-001", "MAT-002")
            ),
            "B-OK": tuple(MockWindchillAdapter({"MAT-001": "检查单_示例", "MAT-002": "检查单_示例"}).query_names(["MAT-001", "MAT-002"])),
        }

        with tempfile.TemporaryDirectory() as temp:
            batch = GenerationService(
                root / "template" / "iqc_draft_template.xlsx",
                Path(temp),
                SnapshotResolvedOrderProvider(snapshot, per_order_names),
            ).generate(tasks, engineer)
            self.assertIn("A-FAIL", batch.errors)
            self.assertIn("模型未解析", batch.errors["A-FAIL"])
            self.assertEqual(batch.errors.get("B-OK"), None)
            self.assertEqual([item.order_number for item in batch.outcomes], ["B-OK"])
            self.assertTrue(batch.outcomes[0].artifacts.sql_path.is_file())

    def test_snapshot_provider_resolves_explicit_readonly_sources(self) -> None:
        """只读快照和 Windchill 结果显式注入后，链路可在内存中完成解析。"""

        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("42", "检查单_示例", "-1", "0", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(AttachedModel("99", "42", ("长度",) * 9, ("10-20",) * 9),),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        windchill = MockWindchillAdapter({"MAT-001": "检查单_示例", "MAT-002": "检查单_示例"}).query_names(
            ["MAT-001", "MAT-002"]
        )

        resolved = SnapshotResolvedOrderProvider(snapshot, windchill).resolve(tasks, engineer)

        self.assertEqual(set(resolved), {"DEMO-ORDER-001"})
        records = resolved["DEMO-ORDER-001"].sql_order.records
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].procedure_model_id, 42)
        self.assertEqual(records[0].engineer_id, 7)
        self.assertEqual(records[0].contexts[0].inspection_items, "外观")
        self.assertEqual(records[0].attached_tables[0].inspection_items[0], "长度")
        self.assertIsNone(records[0].check_number)

    def test_snapshot_provider_builds_attached_rows_with_inherited_identity_and_na(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("42", "检查单_示例", "-1", "0", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(
                AttachedModel(
                    "99",
                    "42",
                    ("输入电压", "", "", "频率", "", "", "", "", ""),
                    ("24V", "", "", "50Hz", "", "", "", "", ""),
                    "电源类附表",
                    "附表备注",
                ),
            ),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        windchill = MockWindchillAdapter({"MAT-001": "检查单_示例", "MAT-002": "检查单_示例"}).query_names(
            ["MAT-001", "MAT-002"]
        )

        resolved = SnapshotResolvedOrderProvider(snapshot, windchill).resolve(tasks, engineer)
        record = resolved["DEMO-ORDER-001"].sql_order.records[0]
        self.assertEqual(len(record.attached_tables), 1)
        attached = record.attached_tables[0]
        self.assertEqual(record.trigger_basis, "DEMO-ORDER-001-1")
        self.assertEqual(attached.inspection_number, record.trigger_basis)
        self.assertEqual(attached.inspection_items[:4], ("输入电压", "NA", "NA", "频率"))
        self.assertEqual(attached.standard_ranges[:4], ("24V", "NA", "NA", "50Hz"))
        self.assertEqual(attached.notes, "附表备注")
        content = json.loads(attached.inspection_content)
        self.assertEqual(content[0]["inspectionContent1"], "")
        self.assertEqual(content[0]["inspectionContent2"], "NA")
        self.assertEqual(content[0]["inspectionContent4"], "")
        self.assertEqual(content[0]["inspectionContent5"], "NA")
        sql = resolved["DEMO-ORDER-001"].sql_order
        rendered = render_order_sql(sql)
        self.assertIn("'DEMO-ORDER-001'", rendered)
        self.assertIn("'MAT-001'", rendered)
        self.assertIn("'输入电压'", rendered)
        self.assertIn("'NA'", rendered)

        mixed_tasks = {
            "A-ERROR": OrderTask("A-ERROR", errors=["源表异常"]),
            "B-OK": OrderTask("B-OK", rows=list(tasks["DEMO-ORDER-001"].rows)),
        }
        mixed_resolved = SnapshotResolvedOrderProvider(snapshot, windchill).resolve(mixed_tasks, engineer)
        self.assertEqual(set(mixed_resolved), {"B-OK"})

        # 同一显式提供者可直接走“读取并生成”的文件编排层；这里只写临时
        # 工作簿/SQL/BAT/诊断文件，不启动任何生成物。
        with tempfile.TemporaryDirectory() as temp:
            batch = GenerationService(
                root / "template" / "iqc_draft_template.xlsx",
                Path(temp),
                SnapshotResolvedOrderProvider(snapshot, windchill),
            ).generate(tasks, engineer)
            self.assertEqual(batch.errors, {})
            self.assertEqual(len(batch.outcomes), 1)
            self.assertTrue(batch.outcomes[0].artifacts.sql_path.is_file())
            self.assertNotIn(
                "measurement_unit",
                batch.outcomes[0].artifacts.sql_path.read_text(encoding="utf-8").lower(),
            )

    def test_snapshot_provider_keeps_model_sql_and_template_name_identical_after_normalization(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("42", "合成类别06进货检查单", "-1", "0", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        raw_name = "《DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_合成类别06进货检查单》满足:DEMO-C-DEMOFORM-DEMOUNIT-TEST#G******-01；"
        names = (
            WindchillName("MAT-001", raw_name, "SUCCESS"),
            WindchillName("MAT-002", raw_name, "SUCCESS"),
        )

        resolved = SnapshotResolvedOrderProvider(snapshot, names).resolve(tasks, engineer)
        order = resolved["DEMO-ORDER-001"]
        self.assertEqual(
            {record.procedure_name for record in order.sql_order.records},
            {"合成类别06进货检查单"},
        )
        self.assertEqual(
            {row.procedure_name for row in order.order_draft.rows},
            {"合成类别06进货检查单"},
        )
        self.assertEqual(
            {record.procedure_name for record in order.sql_order.records},
            {row.procedure_name for row in order.order_draft.rows},
        )

    def test_snapshot_provider_generates_one_record_per_order_item_combination(self) -> None:
        values_a = [""] * 16
        values_a[0] = "DN-COMB"
        values_a[3] = "MAT-001"
        values_a[4] = "示例物料"
        values_a[5] = "供应商"
        values_a[9] = "2"
        values_b = list(values_a)
        values_b[9] = "3"
        combination_tasks = {
            "DN-COMB": OrderTask(
                "DN-COMB",
                rows=[
                    SourceRow("DN-COMB", "MAT-001", "测试", 4, tuple(values_a)),
                    SourceRow("DN-COMB", "MAT-001", "测试", 6, tuple(values_b)),
                ],
            )
        }
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("42", "检查单_示例", "-1", "0", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        names = (WindchillName("MAT-001", "检查单_示例", "SUCCESS"),)
        resolved = SnapshotResolvedOrderProvider(snapshot, names).resolve(
            combination_tasks, Engineer("7", "合成工程师02", "1001", "合成工程师02")
        )
        records = resolved["DN-COMB"].sql_order.records
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].quantity, 5)
        self.assertEqual(records[0].trigger_basis, "DN-COMB-1/2")

    def test_snapshot_provider_rejects_invalid_windchill_result_sets(self) -> None:
        """名称结果必须严格覆盖本批次编码，不能依赖 dict 的静默覆盖。"""

        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("42", "检查单_示例", "-1", "0", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        cases = {
            "duplicate": (
                WindchillName("MAT-001", "检查单_示例", "SUCCESS"),
                WindchillName("MAT-001", "检查单_重复", "SUCCESS"),
                WindchillName("MAT-002", "检查单_示例", "SUCCESS"),
            ),
            "missing": (WindchillName("MAT-001", "检查单_示例", "SUCCESS"),),
            "extra": (
                WindchillName("MAT-001", "检查单_示例", "SUCCESS"),
                WindchillName("MAT-002", "检查单_示例", "SUCCESS"),
                WindchillName("MAT-003", "检查单_示例", "SUCCESS"),
            ),
            "failed": (
                WindchillName("MAT-001", "", "ERROR"),
                WindchillName("MAT-002", "检查单_示例", "SUCCESS"),
            ),
        }
        for label, results in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(GenerationError, "Windchill 名称结果校验失败"):
                SnapshotResolvedOrderProvider(snapshot, results).resolve(tasks, engineer)

    def test_main_builder_keeps_default_block_but_accepts_explicit_provider(self) -> None:
        """桌面入口不自动探测外部依赖，但允许调用方注入已验证提供者。"""

        root = Path(__file__).resolve().parents[1]
        paths = AppPaths.from_project_root(root)
        default_service = build_generation_service(paths)
        self.assertIsInstance(default_service.provider, UnconfiguredGenerationProvider)

        explicit = _ResolvedProvider()
        injected_service = build_generation_service(paths, explicit)
        self.assertIs(injected_service.provider, explicit)

    def test_runtime_provider_prepares_snapshot_and_windchill_once_per_batch(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("42", "检查单_示例", "-1", "0", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        loader = _SnapshotLoader(snapshot)
        adapter = _NameAdapter()
        from src.generation_service import RuntimeGenerationProvider

        provider = RuntimeGenerationProvider(loader, adapter)
        with tempfile.TemporaryDirectory() as temp:
            batch = GenerationService(
                root / "template" / "iqc_draft_template.xlsx", Path(temp), provider
            ).generate(tasks, engineer)
        self.assertEqual(batch.errors, {})
        self.assertEqual(loader.calls, 1)
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(any(event.name == "provider_prepare_success" for event in batch.events))
        dependency_events = [event for event in batch.events if event.name.startswith("windchill_query_")]
        self.assertEqual([event.name for event in dependency_events], ["windchill_query_start", "windchill_query_success"])
        self.assertEqual(dependency_events[0].fields["item_code_count"], 2)
        self.assertEqual(dependency_events[1].fields["result_count"], 2)
        model_events = [event for event in batch.events if event.name == "model_match"]
        self.assertEqual(len(model_events), 2)
        self.assertEqual(model_events[0].order_number, "DEMO-ORDER-001")
        self.assertEqual(model_events[0].fields["raw_name"], "检查单_示例")
        self.assertEqual(model_events[0].fields["expected_name"], "检查单_示例")
        self.assertEqual(model_events[0].fields["active_name_match_count"], 1)

    def test_runtime_provider_marks_empty_windchill_result_with_explicit_chinese_detail(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        snapshot = ReadonlySnapshot(
            procedure_models=(),
            procedure_contexts=(),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )

        class EmptyWindchillAdapter:
            def query_names(self, item_codes):
                return []

        provider = RuntimeGenerationProvider(_SnapshotLoader(snapshot), EmptyWindchillAdapter())
        with tempfile.TemporaryDirectory() as temp:
            batch = GenerationService(
                root / "template" / "iqc_draft_template.xlsx", Path(temp), provider
            ).generate(tasks, engineer)

        empty_event = next(event for event in batch.events if event.name == "windchill_query_error")
        self.assertEqual(empty_event.detail, "Windchill查询为空")
        self.assertEqual(empty_event.fields["item_code_count"], 2)
        self.assertEqual(empty_event.fields["result_count"], 0)
        self.assertEqual(empty_event.fields["query_stage"], "windchill_names")
        self.assertEqual(empty_event.fields["safe_detail"], "empty_result")
        self.assertIn("Windchill查询为空", batch.errors["DEMO-ORDER-001"])

    def test_runtime_m_code_prefix_warning_continues_and_reaches_diagnostic(self) -> None:
        """跨固定前缀 M 码范围只提醒，不阻断生成，并写入审计诊断。"""
        root = Path(__file__).resolve().parents[1]
        values = [""] * 16
        values[0] = "M-WARN-1"
        values[3] = "MAT-001"
        values[4] = "示例物料"
        values[5] = "示例供应商"
        values[9] = "1"
        task = OrderTask(
            "M-WARN-1",
            rows=[
                SourceRow(
                    "M-WARN-1",
                    "MAT-001",
                    "测试",
                    4,
                    tuple(values),
                    sequence_start="M88000000999",
                    sequence_end="M88000011002",
                )
            ],
        )
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("42", "检查单_示例", "-1", "0", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )
        loader = _SnapshotLoader(snapshot)
        adapter = MockWindchillAdapter({"MAT-001": "检查单_示例"})
        from src.generation_service import RuntimeGenerationProvider

        provider = RuntimeGenerationProvider(loader, adapter)
        with tempfile.TemporaryDirectory() as temp:
            batch = GenerationService(
                root / "template" / "iqc_draft_template.xlsx", Path(temp), provider
            ).generate({"M-WARN-1": task}, Engineer("7", "合成工程师02", "1001", "合成工程师02"))
            self.assertEqual(batch.errors, {})
            self.assertEqual(len(batch.outcomes), 1)
            warning_events = [event for event in batch.events if event.name == "m_code_range_warning"]
            self.assertEqual(len(warning_events), 1)
            warning = warning_events[0]
            self.assertEqual(warning.status, "WARNING")
            self.assertEqual(warning.fields["item_code"], "MAT-001")
            self.assertFalse(warning.fields["blocking"])
            diagnostic = json.loads(batch.outcomes[0].artifacts.diagnostic_path.read_text(encoding="utf-8"))
            self.assertEqual(len(diagnostic["warnings"]), 1)
            self.assertIn("固定段（M+前7位）不一致", diagnostic["warnings"][0])

    def test_runtime_provider_merges_readonly_query_events_from_query_service(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        snapshot = ReadonlySnapshot(
            procedure_models=(ProcedureModel("42", "检查单_示例", "-1", "0", "1", "0", "2"),),
            procedure_contexts=(ProcedureContext("42", "1", "外观", "无裂纹"),),
            attached_models=(),
            active_users=(ActiveUser("7", "合成工程师02", "", "", "1001"),),
        )

        class QueryService:
            def __init__(self) -> None:
                self.callback = None

            def set_event_callback(self, callback) -> None:
                self.callback = callback

            def load_snapshot(self):
                for label in ("procedure_models", "procedure_contexts", "attached_models", "active_users"):
                    self.callback({"name": "readonly_query_start", "status": "STARTED", "query_label": label})
                    self.callback({"name": "readonly_query_success", "status": "SUCCESS", "query_label": label, "row_count": 1})
                return snapshot

        class Adapter:
            def query_names(self, item_codes):
                return MockWindchillAdapter({"MAT-001": "检查单_示例", "MAT-002": "检查单_示例"}).query_names(list(item_codes))

        from src.generation_service import RuntimeGenerationProvider

        provider = RuntimeGenerationProvider(QueryService(), Adapter())
        with tempfile.TemporaryDirectory() as temp:
            batch = GenerationService(root / "template" / "iqc_draft_template.xlsx", Path(temp), provider).generate(tasks, engineer)
        readonly_events = [event for event in batch.events if event.name.startswith("readonly_query_")]
        self.assertEqual(len(readonly_events), 8)
        self.assertEqual(readonly_events[1].fields["query_label"], "procedure_models")
        self.assertEqual(readonly_events[1].fields["row_count"], 1)

    def test_runtime_provider_preserves_query_stage_and_safe_detail(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = read_order_tasks(root / "fixtures" / "source_sample.xlsx", ["DEMO-ORDER-001"])
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        from src.generation_service import RuntimeGenerationProvider

        provider = RuntimeGenerationProvider(_FailingSnapshotLoader(), _NameAdapter())
        with tempfile.TemporaryDirectory() as temp:
            batch = GenerationService(
                root / "template" / "iqc_draft_template.xlsx", Path(temp), provider
            ).generate(tasks, engineer)
        self.assertIn("表不存在", batch.errors["DEMO-ORDER-001"])
        event = next(item for item in batch.events if item.name == "provider_prepare_error")
        self.assertEqual(event.fields["stage"], "provider_prepare")
        self.assertEqual(event.fields["query_stage"], "procedure_models")
        self.assertIn("表不存在", str(event.fields["safe_detail"]))
        self.assertIn("credential redacted", str(event.fields["safe_detail"]))

    def test_rt_paper_11_full_paper_batch_does_not_require_alias_file_before_filtering(self) -> None:
        provider = RuntimeGenerationProvider(
            _SpyQueryService(_snapshot("电子检查单")),
            _SpyWindchillAdapter({"PAPER-ITEM": "PAPER-DOC_0_纸质规范.pdf"}),
            procedure_aliases_path=Path("__missing_procedure_aliases__.json"),
            paper_standard_catalog=_paper_catalog("PAPER-DOC"),
        )

        try:
            resolved = provider.prepare(
                {"PAPER-ORDER": _task("PAPER-ORDER", "PAPER-ITEM")},
                Engineer("7", "合成工程师02", "1001", "合成工程师02"),
            )
        except GenerationError as exc:
            self.fail(f"全纸质批次不应在纸质过滤前读取别名配置：{exc}")

        self.assertEqual(resolved, {})
        self.assertEqual(provider.query_service.calls, 0)
        self.assertEqual(provider.paper_only_orders, ("PAPER-ORDER",))

    def test_rt_paper_11_12_full_paper_batch_does_not_call_snapshot_select_or_alias_resolver(self) -> None:
        query = _SpyQueryService(_snapshot("电子检查单"))
        adapter = _SpyWindchillAdapter({"PAPER-ITEM": "PAPER-DOC_0_纸质规范.pdf"})
        aliases = _SpyAliasResolver()
        provider = RuntimeGenerationProvider(
            query,
            adapter,
            procedure_aliases=aliases,
            paper_standard_catalog=_paper_catalog("PAPER-DOC"),
        )
        select_calls: list[str] = []

        import src.mysql_query as mysql_query

        original_select = mysql_query.select_unique_active_model

        def spy_select(models, procedure_name, **kwargs):
            select_calls.append(str(procedure_name))
            return original_select(models, procedure_name, **kwargs)

        with unittest.mock.patch("src.mysql_query.select_unique_active_model", side_effect=spy_select):
            resolved = provider.prepare(
                {"PAPER-ORDER": _task("PAPER-ORDER", "PAPER-ITEM")},
                Engineer("7", "合成工程师02", "1001", "合成工程师02"),
            )

        self.assertEqual(resolved, {})
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(query.calls, 0)
        self.assertEqual(select_calls, [])
        self.assertEqual(aliases.calls, [])
        self.assertEqual(provider.paper_only_orders, ("PAPER-ORDER",))
        self.assertEqual(provider.paper_exclusions["PAPER-ORDER"][0].status, MATCH_EXCLUDED)

    def test_rt_paper_12_14_mixed_order_filters_paper_before_alias_and_model_matching(self) -> None:
        query = _SpyQueryService(_snapshot("电子检查单"))
        adapter = _SpyWindchillAdapter(
            {
                "PAPER-ITEM": "PAPER-DOC_0_纸质规范.pdf",
                "ELEC-ITEM": "ELEC-DOC-001_电子检查单.pdf",
            }
        )
        aliases = _SpyAliasResolver()
        provider = RuntimeGenerationProvider(
            query,
            adapter,
            procedure_aliases=aliases,
            paper_standard_catalog=_paper_catalog("PAPER-DOC"),
        )
        select_calls: list[str] = []

        import src.mysql_query as mysql_query

        original_select = mysql_query.select_unique_active_model

        def spy_select(models, procedure_name, **kwargs):
            select_calls.append(str(procedure_name))
            return original_select(models, procedure_name, **kwargs)

        with unittest.mock.patch("src.mysql_query.select_unique_active_model", side_effect=spy_select):
            resolved = provider.prepare(
                {"MIXED": _task("MIXED", "PAPER-ITEM", "ELEC-ITEM")},
                Engineer("7", "合成工程师02", "1001", "合成工程师02"),
            )

        self.assertEqual(query.calls, 1)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(select_calls, ["电子检查单"])
        self.assertEqual(aliases.calls, [("ELEC-ITEM", "电子检查单")])
        self.assertEqual(tuple(record.item_code for record in resolved["MIXED"].sql_order.records), ("ELEC-ITEM",))
        self.assertIn("MIXED", provider.paper_exclusions)
        self.assertEqual(provider.paper_only_orders, ())

    def test_rt_paper_14_generation_service_writes_only_electronic_item_for_mixed_order(self) -> None:
        root = Path(__file__).resolve().parents[1]
        query = _SpyQueryService(_snapshot("电子检查单"))
        provider = RuntimeGenerationProvider(
            query,
            _SpyWindchillAdapter(
                {
                    "PAPER-ITEM": "PAPER-DOC_0_纸质规范.pdf",
                    "ELEC-ITEM": "ELEC-DOC-001_电子检查单.pdf",
                }
            ),
            paper_standard_catalog=_paper_catalog("PAPER-DOC"),
        )
        with tempfile.TemporaryDirectory() as temp:
            batch = GenerationService(root / "template" / "iqc_draft_template.xlsx", Path(temp), provider).generate(
                {"MIXED": _task("MIXED", "PAPER-ITEM", "ELEC-ITEM")},
                Engineer("7", "合成工程师02", "1001", "合成工程师02"),
            )

        self.assertEqual(batch.errors, {})
        self.assertEqual(len(batch.outcomes), 1)
        records = batch.outcomes[0].resolved_order.sql_order.records
        self.assertEqual(tuple(record.item_code for record in records), ("ELEC-ITEM",))
        self.assertEqual(query.calls, 1)
        self.assertIn("MIXED", batch.paper_exclusions)
        self.assertEqual(batch.paper_only_orders, ())

    def test_rt_paper_15_full_paper_order_has_no_artifacts_and_uses_special_status_event(self) -> None:
        root = Path(__file__).resolve().parents[1]
        query = _SpyQueryService(_snapshot("电子检查单"))
        provider = RuntimeGenerationProvider(
            query,
            _SpyWindchillAdapter({"PAPER-ITEM": "PAPER-DOC_0_纸质规范.pdf"}),
            paper_standard_catalog=_paper_catalog("PAPER-DOC"),
        )
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            batch = GenerationService(root / "template" / "iqc_draft_template.xlsx", output_dir, provider).generate(
                {"PAPER-ORDER": _task("PAPER-ORDER", "PAPER-ITEM")},
                Engineer("7", "合成工程师02", "1001", "合成工程师02"),
            )
            written = tuple(output_dir.iterdir())

        self.assertEqual(batch.errors, {})
        self.assertEqual(batch.outcomes, ())
        self.assertEqual(batch.paper_only_orders, ("PAPER-ORDER",))
        self.assertEqual(query.calls, 0)
        self.assertEqual(written, ())
        self.assertIn("paper_standard_batch_only", [event.name for event in batch.events])
        self.assertIn("paper_only_order", [event.name for event in batch.events])

    def test_rt_paper_18_manual_confirmation_reuses_cached_paper_filter_and_no_excluded_item_returns(self) -> None:
        query = _SpyQueryService(_snapshot("目标检查单"))
        adapter = _SpyWindchillAdapter(
            {
                "PAPER-ITEM": "PAPER-DOC_0_纸质规范.pdf",
                "ELEC-ITEM": "ELEC-DOC-001_未知检查单.pdf",
            }
        )
        provider = RuntimeGenerationProvider(
            query,
            adapter,
            paper_standard_catalog=_paper_catalog("PAPER-DOC"),
        )
        tasks = {"MIXED": _task("MIXED", "PAPER-ITEM", "ELEC-ITEM")}
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")

        with self.assertRaises(Exception) as context:
            provider.prepare(tasks, engineer)
        self.assertEqual(type(context.exception).__name__, "ManualConfirmationRequired")
        self.assertEqual(query.calls, 1)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(tuple(match.item_code for match in provider.paper_exclusions["MIXED"]), ("PAPER-ITEM",))

        provider.set_manual_confirmations({"MIXED::ELEC-ITEM": {"model_id": "42", "manual_name": "目标检查单"}})
        resolved = provider.prepare(tasks, engineer)

        self.assertEqual(query.calls, 1)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(tuple(record.item_code for record in resolved["MIXED"].sql_order.records), ("ELEC-ITEM",))
        self.assertEqual(tuple(match.item_code for match in provider.paper_exclusions["MIXED"]), ("PAPER-ITEM",))

    def test_rt_paper_19_non_paper_runtime_path_still_loads_aliases_snapshot_and_generates(self) -> None:
        query = _SpyQueryService(_snapshot("电子检查单"))
        adapter = _SpyWindchillAdapter({"ELEC-ITEM": "ELEC-DOC-001_电子检查单.pdf"})
        aliases = _SpyAliasResolver()
        provider = RuntimeGenerationProvider(query, adapter, procedure_aliases=aliases)

        resolved = provider.prepare(
            {"ELEC-ORDER": _task("ELEC-ORDER", "ELEC-ITEM")},
            Engineer("7", "合成工程师02", "1001", "合成工程师02"),
        )

        self.assertEqual(query.calls, 1)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(aliases.calls, [("ELEC-ITEM", "电子检查单")])
        self.assertEqual(tuple(resolved), ("ELEC-ORDER",))
        self.assertEqual(tuple(record.item_code for record in resolved["ELEC-ORDER"].sql_order.records), ("ELEC-ITEM",))
        self.assertEqual(provider.paper_exclusions, {})
        self.assertEqual(provider.paper_only_orders, ())


class SyntheticGroupGenerationTests(unittest.TestCase):
    CODES = ("DEMO-MAT-017", "DEMO-MAT-002", "DEMO-MAT-013", "DEMO-MAT-015", "DEMO-MAT-022", "DEMO-MAT-019", "DEMO-MAT-024", "DEMO-MAT-035")
    TARGET = "附录 A DemoLogic定型优化PCM进货检验记录表"
    SOURCE = "DemoLogic定型优化PCM进货检验规范"
    KEY = "DEMO-DEMO18-DEMOSYS-A-DEMOFORM-DOC101-002"

    def setUp(self):
        from src.procedure_alias import load_procedure_aliases
        self.root = Path(__file__).resolve().parents[1]
        from tests.synthetic_aliases import synthetic_alias_resolver
        self.aliases = synthetic_alias_resolver()
        self.engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")
        # Fail loudly if a future test accidentally reaches external processes/network.
        self.process_guard = unittest.mock.patch("subprocess.Popen", side_effect=AssertionError("external process forbidden"))
        self.socket_guard = unittest.mock.patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        self.process_guard.start()
        self.socket_guard.start()
        self.addCleanup(self.process_guard.stop)
        self.addCleanup(self.socket_guard.stop)

    def pcm_snapshot(self, model_id="258", *, target=True, qa1_code=""):
        models = [ProcedureModel("21", "合成类别02进货检查单", "-1", "0", "1", "0", "2")]
        if target:
            models.append(ProcedureModel(model_id, self.TARGET, "-1", "0", "1", "0", "2"))
        if qa1_code:
            models.append(ProcedureModel("901", "关键零部件专项检查单", "-1", "0", "1", "0", "2", apply_range=qa1_code))
        return ReadonlySnapshot(tuple(models), tuple(ProcedureContext(model.id, "1", "外观", "无裂纹") for model in models), (), (ActiveUser("7", "合成工程师02", "", "", "1001"),))

    def test_eight_pcm_items_code_and_approved_name_use_snapshot_id_no_attached(self):
        for model_id in ("258", "7258"):
            for raw in (self.SOURCE, self.KEY + "-Z-01_0_" + self.SOURCE):
                with self.subTest(model_id=model_id, raw=raw):
                    events = []
                    resolved = SnapshotResolvedOrderProvider(
                        self.pcm_snapshot(model_id), tuple(WindchillName(code, raw, "SUCCESS") for code in self.CODES),
                        procedure_aliases=self.aliases, event_callback=events.append,
                    ).resolve({"PCM": _task("PCM", *self.CODES)}, self.engineer)
                    records = resolved["PCM"].sql_order.records
                    self.assertEqual(len(records), 8)
                    self.assertEqual({record.procedure_model_id for record in records}, {int(model_id)})
                    self.assertTrue(all(record.attached_tables == () for record in records))
                    matches = [event for event in events if event.get("name") == "model_match"]
                    self.assertEqual(len(matches), 8)
                    self.assertTrue(all(event["strict_target_match"] and event["selected_model_id"] == model_id for event in matches))
                    self.assertTrue(all(event["attached_model_ids"] == "" for event in matches))
                    if raw == self.SOURCE:
                        self.assertTrue(all(event["pcm_document_code"] == "" for event in matches))

    def test_pcm_wrong_and_multiple_identities_never_manual_fallback(self):
        raws = (self.KEY.replace("DOC101-002", "DOC101-001") + "_" + self.SOURCE, "OTHER-DOC-002_" + self.SOURCE, self.KEY + " OTHER-DOC-001_" + self.SOURCE, self.SOURCE + "补充版")
        for code in self.CODES:
            for raw in raws:
                with self.subTest(code=code, raw=raw):
                    provider = SnapshotResolvedOrderProvider(self.pcm_snapshot(), (WindchillName(code, raw, "SUCCESS"),), procedure_aliases=self.aliases, manual_confirmations={"PCM::" + code: {"model_id": "21", "manual_name": "合成类别02进货检查单"}})
                    with self.assertRaisesRegex(GenerationError, "原始规范身份"):
                        provider.resolve({"PCM": _task("PCM", code)}, self.engineer)

    def test_pcm_missing_target_or_strict_alias_cannot_fall_back_to_id21(self):
        code = self.CODES[0]
        for aliases, snapshot in ((self.aliases, self.pcm_snapshot(target=False)), (None, self.pcm_snapshot()), (ProcedureAliasResolver([ProcedureAlias(True, code, self.SOURCE, self.SOURCE, "合成类别02进货检查单", "", "", "", "full_confirmed_50299821_" + code)]), self.pcm_snapshot())):
            for decision in (None, {"PCM::" + code: {"model_id": "21", "manual_name": "合成类别02进货检查单"}}):
                with self.subTest(aliases=aliases, decision=decision):
                    provider = SnapshotResolvedOrderProvider(snapshot, (WindchillName(code, self.SOURCE, "SUCCESS"),), procedure_aliases=aliases, manual_confirmations=decision)
                    with self.assertRaisesRegex(GenerationError, "DEMO-GROUP"):
                        provider.resolve({"PCM": _task("PCM", code)}, self.engineer)

    def test_pcm_qa1_keeps_ordinary_then_additional_chain(self):
        code = self.CODES[0]
        names = (WindchillName(code, self.SOURCE, "SUCCESS", quality_grade="QA1"),)
        adapter = unittest.mock.Mock(query_names=unittest.mock.Mock(return_value=names))
        provider = RuntimeGenerationProvider(_SpyQueryService(self.pcm_snapshot(qa1_code=code)), adapter, procedure_aliases=self.aliases)
        resolved = provider.prepare({"PCM": _task("PCM", code)}, self.engineer)["PCM"]
        self.assertEqual(resolved.sql_order.records[0].procedure_model_id, 258)
        self.assertEqual(resolved.sql_order.records[0].attached_tables, ())
        self.assertEqual(len(resolved.qa1_drafts), 1)
        self.assertEqual(resolved.qa1_drafts[0].selected_model.id, "901")
        self.assertEqual(provider.paper_review_required, {})

    def test_demo_control_mixed_order_excludes_controlled_only_and_manual_resume_stays_filtered(self):
        from tests.test_paper_standard_exclusion import SyntheticGroupNuconIdentityTests
        near = "DemoControl定型电路板进货检验规范补充版"
        names = {"CONTROL": "DemoControl定型电路板进货检验规范", "NEAR": near, "ORDINARY": "普通纯名称"}
        for catalog in (SyntheticGroupNuconIdentityTests.catalog(), PaperStandardCatalog()):
            with self.subTest(catalog=catalog):
                query = _SpyQueryService(_snapshot(near, "普通纯名称", "合成类别07进货检查单"))
                aliases = _SpyAliasResolver()
                provider = RuntimeGenerationProvider(query, _SpyWindchillAdapter(names), procedure_aliases=aliases, paper_standard_catalog=catalog)
                tasks = {"MIX": _task("MIX", *names)}
                resolved = provider.prepare(tasks, self.engineer)
                self.assertEqual({row.item_code for row in resolved["MIX"].sql_order.records}, {"NEAR", "ORDINARY"})
                self.assertNotIn("CONTROL", [call[0] for call in aliases.calls])
                provider.set_manual_confirmations({"MIX::CONTROL": {"model_id": "44", "manual_name": "合成类别07进货检查单"}})
                resumed = provider.prepare(tasks, self.engineer)
                self.assertEqual({row.item_code for row in resumed["MIX"].sql_order.records}, {"NEAR", "ORDINARY"})
                self.assertEqual(query.calls, 1)

    def test_demo_control_qa1_blocks_entire_order_even_with_other_electronic_order_and_resume(self):
        from tests.test_paper_standard_exclusion import SyntheticGroupNuconIdentityTests
        names = (WindchillName("CONTROL", "DemoControl定型电路板进货检验规范", "SUCCESS", quality_grade="QA1"), WindchillName("ELEC", "电子检查单", "SUCCESS"))
        adapter = unittest.mock.Mock(query_names=unittest.mock.Mock(return_value=names))
        query = _SpyQueryService(_snapshot("电子检查单", "合成类别07进货检查单"))
        provider = RuntimeGenerationProvider(query, adapter, procedure_aliases=self.aliases, paper_standard_catalog=SyntheticGroupNuconIdentityTests.catalog())
        tasks = {"BLOCK": _task("BLOCK", "CONTROL", "ELEC"), "OK": _task("OK", "ELEC")}
        with tempfile.TemporaryDirectory() as temp:
            service = GenerationService(self.root / "template/iqc_draft_template.xlsx", Path(temp), provider)
            batch = service.generate(tasks, self.engineer)
            self.assertIn("QA1_PAPER_SCOPE_BLOCKED", batch.errors["BLOCK"])
            self.assertEqual([outcome.order_number for outcome in batch.outcomes], ["OK"])
            provider.set_manual_confirmations({"BLOCK::CONTROL": {"model_id": "43", "manual_name": "合成类别07进货检查单"}})
            resumed = service.generate(tasks, self.engineer)
            self.assertIn("QA1_PAPER_SCOPE_BLOCKED", resumed.errors["BLOCK"])
            self.assertEqual([outcome.order_number for outcome in resumed.outcomes], ["OK"])
            self.assertFalse(any("BLOCK" in path.name for path in Path(temp).iterdir()))
        self.assertEqual(query.calls, 1)

    def test_demo_control_all_controlled_review_or_paper_never_loads_snapshot(self):
        from tests.test_paper_standard_exclusion import SyntheticGroupNuconIdentityTests
        for catalog, expected in ((PaperStandardCatalog(), "BLOCKED"), (SyntheticGroupNuconIdentityTests.catalog(), "SUCCESS")):
            with self.subTest(expected=expected):
                query = _SpyQueryService(_snapshot("合成类别07进货检查单"))
                provider = RuntimeGenerationProvider(query, _SpyWindchillAdapter({"CONTROL": "DemoControl定型电路板进货检验规范"}), procedure_aliases=self.aliases, paper_standard_catalog=catalog)
                with tempfile.TemporaryDirectory() as temp:
                    service = GenerationService(self.root / "template/iqc_draft_template.xlsx", Path(temp), provider)
                    batch = service.generate({"ALL": _task("ALL", "CONTROL")}, self.engineer)
                    self.assertEqual(batch.outcomes, ())
                    self.assertEqual(batch.paper_only_orders, ("ALL",))
                    self.assertEqual([event.status for event in batch.events if event.name == "paper_only_order"], [expected])
                    self.assertEqual(tuple(Path(temp).iterdir()), ())
                self.assertEqual(query.calls, 0)

    def test_alias_config_is_loaded_once_and_new_provider_observes_restart_boundary(self):
        path = self.root / "config/procedure_aliases.json"
        provider = RuntimeGenerationProvider(object(), object(), procedure_aliases_path=path)
        with unittest.mock.patch("src.generation_service.load_procedure_aliases", return_value=self.aliases) as loader:
            provider._ensure_aliases_loaded()
            provider._ensure_aliases_loaded()
            provider.reset_runtime_state()
            provider._ensure_aliases_loaded()
            self.assertEqual(loader.call_count, 1)
            restarted = RuntimeGenerationProvider(object(), object(), procedure_aliases_path=path)
            restarted._ensure_aliases_loaded()
            self.assertEqual(loader.call_count, 2)


if __name__ == "__main__":
    unittest.main()
