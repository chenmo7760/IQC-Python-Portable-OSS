from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import date

from src.entry_api import (
    EntryApiError,
    EntryDraftApiClient,
    EntryDraftResubmissionService,
    Qa1EntryDraftOrchestrator,
    EntryModelContext,
    GET_DRAFT_CONTEXT_PATH,
    MODEL_CONTEXT_PATH,
    SAVE_DRAFT_CONTEXT_PATH,
    build_entry_draft_payload,
    build_qa1_entry_draft_payload,
    parse_model_context_response,
)
from src.mysql_query import ProcedureModel
from src.qa1_matching import Qa1DraftSpec
from src.sql_generator import SqlAttachedRow, SqlContextRow, SqlRecord


def _record(*, attached: tuple[SqlAttachedRow, ...] = ()) -> SqlRecord:
    return SqlRecord(
        item_code="MAT-001",
        item_name="物料",
        quantity=2,
        supplier="NA",
        raw_supplier="",
        supplier_defaulted=True,
        procedure_name="普通检查单",
        procedure_model_id=10,
        engineer_id=7,
        engineer_name="工程师",
        engineer_dept_id="D1",
        trigger_basis="TK001-1",
        check_number=1,
        contexts=(SqlContextRow("1", "外观", "无裂纹"),),
        sequence_code="M99999999001/M99999999003(M99999999001/M99999999003)",
        completion_time="2026-09-02",
        attached_tables=attached,
    )


class _FakeTransport:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.get_calls: list[tuple[str, dict[str, object]]] = []
        self.post_calls: list[tuple[str, dict[str, object]]] = []

    def get_json(self, path: str, params: dict[str, object]) -> dict[str, object]:
        self.get_calls.append((path, dict(params)))
        return self.response

    def post_form(self, path: str, fields: dict[str, object]) -> dict[str, object]:
        self.post_calls.append((path, dict(fields)))
        return {"data": ["Draft context saved successfully."]}


class _FailingTransport(_FakeTransport):
    """只模拟 update 网络失败，验证 staging 不会自动重试或清旧状态。"""

    def post_form(self, path: str, fields: dict[str, object]) -> dict[str, object]:
        self.post_calls.append((path, dict(fields)))
        raise OSError("simulated update failure")


class EntryApiTests(unittest.TestCase):
    def test_missing_completion_time_uses_same_run_day_default_as_sql(self) -> None:
        for missing in (None, "", "   "):
            record = replace(_record(), completion_time=missing)
            payload = build_entry_draft_payload(record, order_number="TK001")
            self.assertEqual(payload["procedureDraftCompletionTime"], date.today().isoformat())

        attached = SqlAttachedRow.from_template(("项目",) + ("",) * 8, ("范围",) + ("",) * 8)
        attached_payload = build_entry_draft_payload(
            replace(_record(), completion_time=None, attached_tables=(attached,)), order_number="TK001"
        )
        self.assertEqual(
            json.loads(attached_payload["procedureDraftAttachedTableRecord"])["completion_time"],
            date.today().isoformat(),
        )

    def test_invalid_completion_time_remains_blocked(self) -> None:
        record = replace(_record(), completion_time="2026-02-30")
        with self.assertRaisesRegex(EntryApiError, "非法日期"):
            build_entry_draft_payload(record, order_number="TK001")

    def test_model_context_empty_attached_mapping_means_no_child(self) -> None:
        context = parse_model_context_response(
            {
                "data": [
                    {"is_exist_attached_table": "0"},
                    [{"context_number": "目标", "inspection_items": "项目", "inspection_standards": "范围"}],
                    {},
                ]
            },
            model_id="10",
        )
        self.assertEqual(context.attached_templates, ())
        payload = build_entry_draft_payload(_record(), order_number="TK001", model_context=context)
        self.assertEqual(payload["isExistAttachedTable"], "0")
        self.assertNotIn("procedureDraftAttachedTableRecord", payload)

    def test_model_context_backend_nested_header_and_empty_attached_mapping(self) -> None:
        context = parse_model_context_response(
            {
                "data": [
                    [{"is_exist_attached_table": 0, "procedure_name": "普通模板"}],
                    [{"context_number": "目标", "inspection_items": "项目", "inspection_standards": "范围"}],
                    {},
                ]
            },
            model_id="10",
        )
        self.assertEqual(context.header["procedure_name"], "普通模板")
        self.assertEqual(context.is_exist_attached_table, "0")
        self.assertEqual(context.attached_templates, ())

    def test_model_context_error_attached_mapping_is_not_a_child(self) -> None:
        context = parse_model_context_response(
            {
                "data": [
                    {"is_exist_attached_table": "1"},
                    [{"context_number": "目标", "inspection_items": "项目", "inspection_standards": "范围"}],
                    {"error": "EntryAttachedTableModel matching query does not exist."},
                ]
            },
            model_id="10",
        )
        self.assertEqual(context.attached_templates, ())
        with self.assertRaisesRegex(EntryApiError, "flag=1"):
            build_entry_draft_payload(_record(), order_number="TK001", model_context=context)

    def test_payload_uses_unparenthesized_sequence_for_main_and_attached_json(self) -> None:
        attached = SqlAttachedRow.from_template(("项目",) + ("",) * 8, ("范围",) + ("",) * 8)
        record = _record(attached=(attached,))
        payload = build_entry_draft_payload(
            record,
            order_number="TK001",
            # API-06：有 GET 模板时必须以 GET 附表为权威来源；本夹具
            # 明确提供目标 child，避免依赖普通 record.attached_tables。
            model_context=EntryModelContext(
                "10",
                contexts=(
                    {"context_number": "目标", "inspection_items": "目标外观", "inspection_standards": "目标无裂纹"},
                ),
                attached_templates=(
                    {"inspection_items": ("项目",) + ("",) * 8, "standard_ranges": ("范围",) + ("",) * 8},
                ),
                is_exist_attached_table="1",
            ),
        )
        self.assertEqual(payload["procedureDraftSequenceCode"], "M99999999001/M99999999003")
        attached_json = json.loads(payload["procedureDraftAttachedTableRecord"])
        self.assertEqual(attached_json["inspection_number"], record.trigger_basis)
        self.assertEqual(attached_json["inspection_content"][0]["sequenceCode"], "M99999999001/M99999999003")
        self.assertNotIn("notes", attached_json)
        self.assertNotIn("attached_table_name", attached_json)

    def test_client_performs_one_get_and_one_post_without_retry_or_latest_guess(self) -> None:
        attached = SqlAttachedRow.from_template(("项目",) + ("",) * 8, ("范围",) + ("",) * 8)
        transport = _FakeTransport(
            {
                "data": [
                    {"is_exist_attached_table": "1"},
                    [{"context_number": "目标", "inspection_items": "目标外观", "inspection_standards": "目标无裂纹"}],
                    [{"inspection_items": ("项目",) + ("",) * 8, "standard_ranges": ("范围",) + ("",) * 8}],
                ]
            }
        )
        result = EntryDraftApiClient(transport).create_draft_from_record(
            _record(attached=(attached,)), order_number="TK001"
        )
        self.assertTrue(result.accepted)
        self.assertIsNone(result.draft_id)
        self.assertEqual(transport.get_calls, [(MODEL_CONTEXT_PATH, {"id": "10"})])
        self.assertEqual(len(transport.post_calls), 1)
        self.assertEqual(transport.post_calls[0][0], SAVE_DRAFT_CONTEXT_PATH)

    def test_model_flag_one_without_local_child_is_blocked(self) -> None:
        with self.assertRaises(EntryApiError):
            build_entry_draft_payload(
                _record(),
                order_number="TK001",
                model_context=EntryModelContext("10", is_exist_attached_table="1"),
            )

    def test_get_attached_template_takes_precedence_over_local_record_child(self) -> None:
        local = SqlAttachedRow.from_template(("本地项目",) + ("",) * 8, ("本地范围",) + ("",) * 8)
        target = EntryModelContext(
            "10",
            contexts=(
                {
                    "context_number": "GET-1",
                    "inspection_items": "目标上下文项目",
                    "inspection_standards": "目标上下文范围",
                },
            ),
            attached_templates=(
                {"inspection_items": ("目标项目",) + ("",) * 8, "standard_ranges": ("目标范围",) + ("",) * 8},
            ),
            is_exist_attached_table="1",
        )
        payload = build_entry_draft_payload(_record(attached=(local,)), order_number="TK001", model_context=target)
        attached_json = json.loads(payload["procedureDraftAttachedTableRecord"])
        self.assertEqual(attached_json["inspection_item1"], "目标项目")

    def test_get_context_takes_precedence_and_missing_context_is_blocked(self) -> None:
        target = EntryModelContext(
            "10",
            contexts=(
                {"context_number": "目标", "inspection_items": "目标项目", "inspection_standards": "目标范围"},
            ),
            is_exist_attached_table="0",
        )
        payload = build_entry_draft_payload(_record(), order_number="TK001", model_context=target)
        self.assertEqual(payload["procedureDraftContext[0][context_number]"], "目标")
        override = build_entry_draft_payload(
            _record(),
            order_number="TK001",
            model_context=target,
            contexts=({"context_number": "人工", "inspection_items": "人工项目", "inspection_standards": "人工范围"},),
        )
        self.assertEqual(override["procedureDraftContext[0][context_number]"], "人工")
        with self.assertRaises(EntryApiError):
            build_entry_draft_payload(
                _record(),
                order_number="TK001",
                model_context=EntryModelContext("10", is_exist_attached_table="0"),
            )

    def test_payload_rejects_model_context_for_different_model(self) -> None:
        with self.assertRaises(EntryApiError):
            build_entry_draft_payload(
                _record(),
                order_number="TK001",
                model_context=EntryModelContext(
                    "999",
                    contexts=(
                        {"context_number": "错误", "inspection_items": "错误项目", "inspection_standards": "错误范围"},
                    ),
                    is_exist_attached_table="0",
                ),
            )

    def test_resubmission_failure_keeps_old_snapshot_and_posts_once(self) -> None:
        transport = _FailingTransport({"data": [{"is_exist_attached_table": "0"}, [], []]})
        service = EntryDraftResubmissionService(EntryDraftApiClient(transport))
        stage = service.stage_update(
            "659",
            _record(),
            order_number="TK001",
            model_context=EntryModelContext("10", contexts=({"context_number": "QA"},), is_exist_attached_table="0"),
            old_fields={"status": "OLD", "data": [{"is_exist_attached_table": "0"}, [], []]},
        )
        result = service.submit(stage)
        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "FAILED_FIELD_REQUIRED")
        self.assertEqual(result.old_fields["status"], "OLD")
        self.assertEqual(len(transport.post_calls), 1)

    def test_flag_zero_update_with_old_child_is_blocked_without_explicit_drop(self) -> None:
        transport = _FakeTransport({"data": [{"is_exist_attached_table": "0"}, [], []]})
        service = EntryDraftResubmissionService(EntryDraftApiClient(transport))
        with self.assertRaises(EntryApiError):
            service.stage_update(
                "659",
                _record(),
                order_number="TK001",
                model_context=EntryModelContext(
                    "10",
                    contexts=({"context_number": "QA"},),
                    is_exist_attached_table="0",
                ),
                old_fields={"data": [{"is_exist_attached_table": "1"}, [], [{"child": "old"}]]},
            )
        self.assertEqual(transport.post_calls, [])

    def test_flag_zero_update_requires_drop_reason_even_when_child_explicitly_empty(self) -> None:
        transport = _FakeTransport({"data": [{"is_exist_attached_table": "0"}, [], []]})
        service = EntryDraftResubmissionService(EntryDraftApiClient(transport))
        with self.assertRaises(EntryApiError):
            service.stage_update(
                "659",
                _record(),
                order_number="TK001",
                model_context=EntryModelContext(
                    "10",
                    contexts=({"context_number": "QA"},),
                    is_exist_attached_table="0",
                ),
                attached_tables=(),
                old_fields={"data": [{"is_exist_attached_table": "1"}, [], [{"child": "old"}]]},
            )
        stage = service.stage_update(
            "659",
            _record(),
            order_number="TK001",
            model_context=EntryModelContext(
                "10",
                contexts=({"context_number": "QA"},),
                is_exist_attached_table="0",
            ),
            attached_tables=(),
            child_drop_audit_reason="人工确认删除旧附表",
            old_fields={"data": [{"is_exist_attached_table": "1"}, [], [{"child": "old"}]]},
        )
        result = service.submit(stage)
        self.assertTrue(result.accepted)
        self.assertEqual(len(transport.post_calls), 1)

    def test_readback_and_update_require_known_draft_id_and_use_update_trigger_field(self) -> None:
        transport = _FakeTransport({"data": [{"is_exist_attached_table": "0"}, [], []]})
        client = EntryDraftApiClient(transport)
        with self.assertRaises(EntryApiError):
            client.get_draft_context("")
        with self.assertRaises(EntryApiError):
            client.update_draft("", {"procedureDraftTriggerBasis": "x"})
        readback = client.get_draft_context("659")
        self.assertEqual(readback["data"][0]["is_exist_attached_table"], "0")
        client.update_draft("659", {"procedureDraftTriggerBasis": "TK001-1"})
        update_fields = transport.post_calls[-1][1]
        self.assertEqual(update_fields["draftId"], "659")
        self.assertEqual(update_fields["procedureTriggerBasis"], "TK001-1")
        self.assertNotIn("procedureDraftTriggerBasis", update_fields)

    def test_qa1_payload_requires_explicit_multi_candidate_selection(self) -> None:
        candidates = (
            ProcedureModel("11", "关键A", "-1", "-1", "1", "0", "2"),
            ProcedureModel("12", "关键B", "-1", "-1", "1", "0", "2"),
        )
        spec = Qa1DraftSpec(_record().contexts[0], "OLD-1", candidates, "apply_range")
        with self.assertRaises(EntryApiError):
            build_qa1_entry_draft_payload(_record(), spec, order_number="TK001")
        payload = build_qa1_entry_draft_payload(
            _record(),
            spec,
            order_number="TK001",
            selected_model_id="12",
            model_context=EntryModelContext(
                "12",
                contexts=(
                    {"context_number": "QA", "inspection_items": "目标项目", "inspection_standards": "目标范围"},
                ),
                is_exist_attached_table="0",
            ),
        )
        self.assertEqual(payload["modelId"], "12")
        self.assertEqual(payload["procedureDraftItemName"], "物料")

    def test_qa1_trigger_basis_adds_s_prefix_without_changing_order_or_ordinary_payload(self) -> None:
        record = replace(_record(), trigger_basis="8194648-2")
        candidate = ProcedureModel("21", "关键模板", "-1", "-1", "1", "0", "2")
        spec = Qa1DraftSpec(record.contexts[0], "OLD-1", (candidate,), "apply_range")
        model_context = EntryModelContext(
            "21",
            contexts=({"context_number": "QA", "inspection_items": "项目", "inspection_standards": "范围"},),
            attached_templates=(
                {"inspection_items": ("附表项目",) + ("",) * 8, "standard_ranges": ("附表范围",) + ("",) * 8},
            ),
            is_exist_attached_table="1",
        )

        ordinary = build_entry_draft_payload(record, order_number="8194648")
        qa1 = build_qa1_entry_draft_payload(
            record,
            spec,
            order_number="8194648",
            model_context=model_context,
        )

        self.assertEqual(ordinary["procedureDraftTriggerBasis"], "8194648-2")
        self.assertEqual(record.check_number, 1)
        self.assertIsNone(ordinary["procedureDraftCheckNumber"])
        self.assertEqual(qa1["procedureDraftTriggerBasis"], "S-8194648-2")
        self.assertEqual(qa1["procedureDraftItemCode"], record.item_code)
        self.assertIsNone(qa1["procedureDraftCheckNumber"])
        qa1_attached = json.loads(qa1["procedureDraftAttachedTableRecord"])
        self.assertEqual(qa1_attached["inspection_number"], "S-8194648-2")
        self.assertEqual(qa1_attached["item_code"], record.item_code)
        self.assertIsNone(qa1_attached["check_number"])

        already_prefixed = build_qa1_entry_draft_payload(
            replace(record, trigger_basis="S-8194648-2"),
            spec,
            order_number="8194648",
            model_context=model_context,
        )
        self.assertEqual(already_prefixed["procedureDraftTriggerBasis"], "S-8194648-2")

        lowercase_prefixed = build_qa1_entry_draft_payload(
            replace(record, trigger_basis="s-8194648-2"),
            spec,
            order_number="8194648",
            model_context=model_context,
        )
        self.assertEqual(lowercase_prefixed["procedureDraftTriggerBasis"], "S-8194648-2")
        with self.assertRaisesRegex(EntryApiError, "只有 S- 前缀"):
            build_qa1_entry_draft_payload(
                replace(record, trigger_basis="s-"),
                spec,
                order_number="8194648",
                model_context=model_context,
            )

        merged_record = replace(record, trigger_basis="1998434-2/3")
        merged_ordinary = build_entry_draft_payload(merged_record, order_number="1998434")
        merged_qa1 = build_qa1_entry_draft_payload(
            merged_record,
            spec,
            order_number="1998434",
            model_context=model_context,
        )
        self.assertEqual(merged_ordinary["procedureDraftTriggerBasis"], "1998434-2/3")
        self.assertEqual(merged_qa1["procedureDraftTriggerBasis"], "S-1998434-2/3")
        self.assertEqual(
            json.loads(merged_qa1["procedureDraftAttachedTableRecord"])["inspection_number"],
            "S-1998434-2/3",
        )
        for prefixed in ("S-1998434-2/3", "s-1998434-2/3"):
            prefixed_qa1 = build_qa1_entry_draft_payload(
                replace(record, trigger_basis=prefixed),
                spec,
                order_number="1998434",
                model_context=model_context,
            )
            self.assertEqual(prefixed_qa1["procedureDraftTriggerBasis"], "S-1998434-2/3")

        nonmatching = build_qa1_entry_draft_payload(
            replace(record, trigger_basis="1998434-2/A"),
            spec,
            order_number="1998434",
            model_context=model_context,
        )
        self.assertEqual(nonmatching["procedureDraftTriggerBasis"], "S-1998434-2/A")

    def test_qa1_orchestrator_posts_only_after_selection_and_uses_known_update_id(self) -> None:
        candidates = (
            ProcedureModel("11", "关键A", "-1", "-1", "1", "0", "2"),
            ProcedureModel("12", "关键B", "-1", "-1", "1", "0", "2"),
        )
        spec = Qa1DraftSpec(_record().contexts[0], "OLD-1", candidates, "procedure_code")
        transport = _FakeTransport(
            {
                "data": [
                    {"is_exist_attached_table": "0"},
                    [{"context_number": "QA", "inspection_items": "目标项目", "inspection_standards": "目标范围"}],
                    [],
                ]
            }
        )
        orchestrator = Qa1EntryDraftOrchestrator(EntryDraftApiClient(transport))
        with self.assertRaises(EntryApiError):
            orchestrator.submit(_record(), spec, order_number="TK001")
        self.assertEqual(transport.post_calls, [])
        result = orchestrator.submit(
            _record(), spec, order_number="TK001", selected_model_id="12", known_draft_id="659"
        )
        self.assertTrue(result.accepted)
        self.assertEqual(len(transport.post_calls), 1)
        self.assertEqual(transport.post_calls[0][1]["draftId"], "659")
        self.assertEqual(transport.post_calls[0][1]["procedureTriggerBasis"], "S-TK001-1")

    def test_qa1_flag_zero_first_create_ignores_ordinary_attached_child(self) -> None:
        ordinary_attached = SqlAttachedRow.from_template(("普通项目",) + ("",) * 8, ("普通范围",) + ("",) * 8)
        candidate = ProcedureModel("21", "关键模板", "-1", "-1", "1", "0", "2")
        spec = Qa1DraftSpec(_record().contexts[0], "OLD-1", (candidate,), "procedure_code")
        target_context = EntryModelContext(
            "21",
            contexts=({"context_number": "QA", "inspection_items": "目标项目", "inspection_standards": "目标范围"},),
            is_exist_attached_table="0",
        )
        payload = build_qa1_entry_draft_payload(
            _record(attached=(ordinary_attached,)),
            spec,
            order_number="TK001",
            model_context=target_context,
        )
        self.assertEqual(payload["procedureDraftContext[0][context_number]"], "QA")
        self.assertEqual(payload["isExistAttachedTable"], "0")
        self.assertNotIn("procedureDraftAttachedTableRecord", payload)

    def test_qa1_orchestrator_first_create_forces_target_get_and_ignores_all_template_overrides(self) -> None:
        ordinary_attached = SqlAttachedRow.from_template(("普通项目",) + ("",) * 8, ("普通范围",) + ("",) * 8)
        override_attached = SqlAttachedRow.from_template(("override 项目",) + ("",) * 8, ("override 范围",) + ("",) * 8)
        candidate = ProcedureModel("21", "关键模板", "-1", "-1", "1", "0", "2")
        spec = Qa1DraftSpec(_record().contexts[0], "OLD-1", (candidate,), "procedure_code")
        transport = _FakeTransport(
            {
                "data": [
                    {"is_exist_attached_table": "0"},
                    [{"context_number": "QA", "inspection_items": "目标项目", "inspection_standards": "目标范围"}],
                    [],
                ]
            }
        )
        override_model_context = EntryModelContext(
            "21",
            contexts=({"context_number": "OVERRIDE", "inspection_items": "override", "inspection_standards": "override"},),
            attached_templates=(
                {"inspection_items": ("override 项目",) + ("",) * 8, "standard_ranges": ("override 范围",) + ("",) * 8},
            ),
            is_exist_attached_table="1",
        )

        result = Qa1EntryDraftOrchestrator(EntryDraftApiClient(transport)).submit(
            _record(attached=(ordinary_attached,)),
            spec,
            order_number="TK001",
            selected_model_id="21",
            model_context=override_model_context,
            contexts=({"context_number": "OVERRIDE-2", "inspection_items": "override", "inspection_standards": "override"},),
            attached_tables=(override_attached,),
        )

        self.assertTrue(result.accepted)
        self.assertEqual(transport.get_calls, [(MODEL_CONTEXT_PATH, {"id": "21"})])
        self.assertEqual(len(transport.post_calls), 1)
        path, fields = transport.post_calls[0]
        self.assertEqual(path, SAVE_DRAFT_CONTEXT_PATH)
        self.assertEqual(fields["modelId"], "21")
        self.assertEqual(fields["procedureDraftContext[0][context_number]"], "QA")
        self.assertEqual(fields["isExistAttachedTable"], "0")
        self.assertNotIn("procedureDraftAttachedTableRecord", fields)

    def test_qa1_known_draft_flag_zero_still_blocks_unconfirmed_old_child_drop(self) -> None:
        candidate = ProcedureModel("21", "关键模板", "-1", "-1", "1", "0", "2")
        spec = Qa1DraftSpec(_record().contexts[0], "OLD-1", (candidate,), "procedure_code")
        transport = _FakeTransport(
            {"data": [{"is_exist_attached_table": "1"}, [], [{"child": "old"}]]}
        )
        target_context = EntryModelContext(
            "21",
            contexts=({"context_number": "QA", "inspection_items": "目标项目", "inspection_standards": "目标范围"},),
            is_exist_attached_table="0",
        )

        with self.assertRaisesRegex(EntryApiError, "旧草稿附表未被证明为空"):
            Qa1EntryDraftOrchestrator(EntryDraftApiClient(transport)).submit(
                _record(),
                spec,
                order_number="TK001",
                selected_model_id="21",
                model_context=target_context,
                known_draft_id="659",
            )
        self.assertEqual(transport.get_calls, [(GET_DRAFT_CONTEXT_PATH, {"id": "659"})])
        self.assertEqual(transport.post_calls, [])

    def test_qa1_target_flag_one_requires_target_attached_template(self) -> None:
        candidate = ProcedureModel("22", "关键模板", "-1", "-1", "1", "0", "2")
        spec = Qa1DraftSpec(_record().contexts[0], "OLD-1", (candidate,), "procedure_code")
        target_context = EntryModelContext(
            "22",
            contexts=({"context_number": "QA", "inspection_items": "目标项目", "inspection_standards": "目标范围"},),
            is_exist_attached_table="1",
        )
        with self.assertRaises(EntryApiError):
            build_qa1_entry_draft_payload(_record(), spec, order_number="TK001", model_context=target_context)

    def test_qa1_none_attached_tables_uses_target_model_context_child(self) -> None:
        candidate = ProcedureModel("23", "关键模板", "-1", "-1", "1", "0", "2")
        spec = Qa1DraftSpec(_record().contexts[0], "OLD-1", (candidate,), "procedure_code")
        target_context = EntryModelContext(
            "23",
            contexts=({"context_number": "QA", "inspection_items": "目标项目", "inspection_standards": "目标范围"},),
            attached_templates=({"inspection_items": ("目标附表项目",) + ("",) * 8, "standard_ranges": ("目标附表范围",) + ("",) * 8},),
            is_exist_attached_table="1",
        )
        payload = build_qa1_entry_draft_payload(
            _record(), spec, order_number="TK001", model_context=target_context
        )
        self.assertEqual(payload["isExistAttachedTable"], "1")
        attached_json = json.loads(payload["procedureDraftAttachedTableRecord"])
        self.assertEqual(attached_json["inspection_item1"], "目标附表项目")


if __name__ == "__main__":
    unittest.main()
