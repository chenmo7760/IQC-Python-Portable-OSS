from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime

from src.models import Engineer
from src.pqc_models import (
    PQC_TYPE_BY_CODE,
    PqcCreationStatus,
    PqcDraftRecord,
    PqcDraftSelection,
    PqcFormalRecord,
    PqcModelContext,
    PqcModelContextRow,
    PqcQuerySnapshot,
    PqcTool,
)
from src.processend_api import (
    PROCESSEND_IDENTITY_PATH,
    PROCESSEND_MODEL_CONTEXT_PATH,
    PROCESSEND_SAVE_DRAFT_PATH,
    PqcDraftCreationService,
    ProcessEndApiError,
    ProcessEndDraftApiClient,
    ProcessEndIdentityError,
    ProcessEndPostRejected,
    ProcessEndPostUncertain,
    build_processend_draft_payload,
    parse_processend_model_context,
)
from tests.pqc_test_helpers import TODAY, active_templates, snapshot, source_row


ENGINEER = Engineer("u1", "Engineer", "D1", "Engineer")
BLANK_ID_ENGINEER = Engineer("", "Engineer", "", "Engineer")


def model_context(type_code="05"):
    template_key = {"01": "CABINET_ASSEMBLY", "02": "CABINET_WIRING", "03": "CABINET_PRE_POWER", "04": "CABINET_POST_POWER", "05": "TORQUE"}[type_code]
    template = next(item for item in active_templates() if item.template_key == template_key)
    return PqcModelContext(
        template.model_id,
        template.procedure_code,
        template.procedure_name,
        "1",
        (
            PqcModelContextRow(" 1 ", " Check A ", " Standard A "),
            PqcModelContextRow("2", "Check B", ""),
        ),
        template_key=template.template_key,
        form_suffix=template.form_suffix,
    )


class FakeTransport:
    def __init__(self, *, response=None, post_error=None, identity=None, identity_error=None):
        self.response = response or {"data": ["Draft context saved successfully."]}
        self.post_error = post_error
        self.identity = identity or {"data": {"id": "u1", "name": "Engineer", "dept": "D1"}}
        self.identity_error = identity_error
        self.get_calls = []
        self.post_calls = []

    def get_json(self, path, params):
        self.get_calls.append((path, dict(params)))
        if path == PROCESSEND_IDENTITY_PATH:
            if self.identity_error:
                raise self.identity_error
            return self.identity
        return {
            "data": [
                [{"procedure_code": PQC_TYPE_BY_CODE["05"].template_code, "procedure_name": "扭矩质量检查记录单", "procedure_version": 1}],
                [{"context_number": "1", "inspection_items": "A", "inspection_standards": "B"}],
            ]
        }

    def post_form(self, path, fields):
        self.post_calls.append((path, dict(fields)))
        if self.post_error:
            raise self.post_error
        return self.response


class MemoryRepository:
    def __init__(self, current_snapshot=None):
        base = current_snapshot or snapshot()
        self.templates = list(base.templates)
        self.formal = list(base.formal_records)
        self.drafts = list(base.drafts)
        self.readback_windows = []

    def load_project_snapshot(self, project_number, *, include_tools=True):
        return PqcQuerySnapshot(
            tuple(self.templates),
            tuple(item for item in self.formal if item.project_number == project_number),
            tuple(item for item in self.drafts if item.project_number == project_number),
            (),
        )

    def matching_drafts(
        self,
        project_number,
        item_code,
        template_code,
        trigger_basis,
        qc_engineer,
        *,
        template_key="",
        created_not_before=None,
    ):
        self.readback_windows.append(created_not_before)
        return tuple(
            item
            for item in self.drafts
            if item.project_number == project_number
            and item.item_code == item_code
            and item.template_code == template_code
            and (not template_key or item.template_key == template_key)
            and item.trigger_basis == trigger_basis
            and item.qc_engineer == qc_engineer
        )


class MemoryApi:
    def __init__(self, repository, modes=None):
        self.repository = repository
        self.modes = dict(modes or {})
        self.posts = []
        self.context_gets = []

    def get_model_context(self, model_id, expected_template_code, **kwargs):
        self.context_gets.append((str(model_id), expected_template_code))
        return PqcModelContext(
            str(model_id),
            expected_template_code,
            kwargs.get("expected_template_name", ""),
            "1",
            (PqcModelContextRow("1", "Item", "Standard"),),
            template_key=kwargs.get("expected_template_key", ""),
            form_suffix=kwargs.get("expected_form_suffix", ""),
        )

    def save_draft(self, fields, *, selected_engineer=None):
        self.selected_engineer = selected_engineer
        payload = dict(fields)
        self.posts.append(payload)
        type_code = payload["procedureTriggerBasis"][-2:]
        mode = self.modes.get(type_code, "success")
        if mode in {"success", "uncertain_saved"}:
            model_id = str(payload["modelId"])
            template = next(item for item in self.repository.templates if item.model_id == model_id)
            self.repository.drafts.append(
                PqcDraftRecord(
                    f"new-{len(self.repository.drafts) + 1}",
                    payload["procedureDraftProjectNumber"],
                    payload["procedureDraftItemCode"],
                    template.procedure_code,
                    payload["procedureTriggerBasis"],
                    payload["procedureDraftQcEngineer"],
                    "2026-09-06 12:00:00",
                    template.template_key,
                    template.procedure_name,
                )
            )
        if mode == "rejected":
            raise ProcessEndPostRejected("rejected")
        if mode in {"uncertain", "uncertain_saved"}:
            raise ProcessEndPostUncertain("timeout")
        return {"data": ["Draft context saved successfully."]}


class ProcessEndPayloadTests(unittest.TestCase):
    def test_parses_real_endpoint_wrapper_shape(self):
        response = {
            "data": [
                [{"procedure_code": "DEMO-ICS-DEMOFORM-005", "procedure_name": " 扭矩 ", "procedure_version": 2}],
                [
                    {"context_number": " 1 ", "inspection_items": " A ", "inspection_standards": " B "},
                    {"context_number": "2", "inspection_items": "C", "inspection_standards": ""},
                ],
            ]
        }
        parsed = parse_processend_model_context(
            response, model_id="5", expected_template_code="DEMO-ICS-DEMOFORM-005"
        )
        self.assertEqual(parsed.model_id, "5")
        self.assertEqual(parsed.procedure_name, "扭矩")
        self.assertEqual(len(parsed.rows), 2)
        self.assertEqual(parsed.rows[0].inspection_items, "A")

    def test_payload_omits_empty_ncr_tools_and_notes_and_leaves_conclusions_empty(self):
        source = replace(
            source_row(special_case=" 无上电 "),
            workorder_number=" WO ",
            project_name=" Project  Name ",
            sequence_code=" 000123 ",
            ncr_code="   ",
        )
        payload = build_processend_draft_payload(
            source,
            type_code="05",
            model_id="5",
            trigger_basis=" P-001-05 ",
            template_key="TORQUE",
            procedure_code="DEMO-ICS-DEMOFORM-005",
            procedure_name="扭矩质量检查记录单",
            form_suffix="05",
            qc_engineer=" Engineer ",
            model_context=model_context(),
            completion_day=TODAY,
        )
        self.assertEqual(payload["procedureTriggerBasis"], "P-001-05")
        self.assertEqual(payload["procedureDraftContractNumber"], "NA")
        self.assertEqual(payload["procedureDraftSequenceCode"], "000123")
        self.assertEqual(payload["procedureDraftInspectionConclusion"], "")
        self.assertEqual(payload["procedureDraftContext[0][inspect_records]"], "")
        self.assertEqual(payload["procedureDraftContext[0][inspection_conclusion]"], "")
        self.assertNotIn("procedureDraftNcrCode", payload)
        self.assertNotIn("procedureDraftTestingTools", payload)
        self.assertNotIn("procedureDraftNotes", payload)
        self.assertFalse(any(isinstance(value, str) and value != value.strip() for value in payload.values()))

    def test_payload_includes_trimmed_ncr_and_selected_tools(self):
        source = source_row(ncr_code=" NCR-7 ")
        tool = PqcTool("1", "扭矩扳手", "SN-1", "2027-01-01")
        payload = build_processend_draft_payload(
            source,
            type_code="05",
            model_id="5",
            trigger_basis="P-001-05",
            procedure_code=PQC_TYPE_BY_CODE["05"].template_code,
            template_key="TORQUE",
            procedure_name="扭矩质量检查记录单",
            form_suffix="05",
            qc_engineer="Engineer",
            model_context=model_context(),
            completion_day=TODAY,
            tools=(tool,),
        )
        self.assertEqual(payload["procedureDraftNcrCode"], "NCR-7")
        self.assertEqual(payload["procedureDraftTestingTools"], "扭矩扳手(SN-1)(2027-01-01)")

    def test_payload_rejects_wrong_engineer_trigger_expired_tool_and_conflicted_source(self):
        base = dict(
            source=source_row(),
            type_code="05",
            model_id="5",
            trigger_basis="P-001-05",
            template_key="TORQUE",
            procedure_code=PQC_TYPE_BY_CODE["05"].template_code,
            procedure_name="扭矩质量检查记录单",
            form_suffix="05",
            qc_engineer="Engineer",
            model_context=model_context(),
            completion_day=TODAY,
        )
        for override in (
            {"qc_engineer": "Other"},
            # SS 不是 UI 列号；用真正不一致的模板身份覆盖错误分支。
            {"procedure_code": PQC_TYPE_BY_CODE["04"].template_code},
            {"tools": (PqcTool("x", "扭矩扳手", "SN", "2026-09-05"),)},
            {"source": replace(source_row(), errors=("conflict",))},
        ):
            with self.subTest(override=override):
                with self.assertRaises(ProcessEndApiError):
                    build_processend_draft_payload(**{**base, **override})

    def test_api_adapter_uses_only_processend_get_and_one_draft_post(self):
        transport = FakeTransport()
        client = ProcessEndDraftApiClient(transport)
        context = client.get_model_context(
            "5", PQC_TYPE_BY_CODE["05"].template_code,
            expected_template_name="扭矩质量检查记录单",
            expected_template_key="TORQUE",
            expected_form_suffix="05",
        )
        response = client.save_draft(
            {"modelId": "5", "procedureDraftQcEngineer": "Engineer"},
            selected_engineer=ENGINEER,
        )
        self.assertEqual(context.model_id, "5")
        self.assertEqual(response["data"], ["Draft context saved successfully."])
        self.assertEqual(transport.get_calls, [
            (PROCESSEND_MODEL_CONTEXT_PATH, {"id": "5"}),
        ])
        self.assertEqual(transport.post_calls[0][0], PROCESSEND_SAVE_DRAFT_PATH)
        self.assertNotIn("procedureRecord", transport.post_calls[0][0])

    def test_api_adapter_distinguishes_rejected_response_and_uncertain_transport(self):
        with self.assertRaises(ProcessEndPostRejected):
            ProcessEndDraftApiClient(FakeTransport(response={"data": ["no"]})).save_draft(
                {"procedureDraftQcEngineer": "Engineer"}, selected_engineer=ENGINEER
            )
        transport = FakeTransport(post_error=TimeoutError())
        with self.assertRaises(ProcessEndPostUncertain):
            ProcessEndDraftApiClient(transport).save_draft(
                {"procedureDraftQcEngineer": "Engineer"}, selected_engineer=ENGINEER
            )
        self.assertEqual(len(transport.post_calls), 1)

    def test_explicit_identity_verification_still_parses_current_user(self):
        transport = FakeTransport()
        client = ProcessEndDraftApiClient(transport)
        identity = client.verify_identity(ENGINEER)
        self.assertEqual((identity.name, identity.dept_id, identity.current_dept_id), ("Engineer", "D1", "D1"))
        self.assertEqual(transport.get_calls[-1], (PROCESSEND_IDENTITY_PATH, {}))

    def test_blank_user_and_dept_do_not_block_post_or_call_identity_get(self):
        transport = FakeTransport()
        response = ProcessEndDraftApiClient(transport).save_draft(
            {"procedureDraftQcEngineer": "Engineer"}, selected_engineer=BLANK_ID_ENGINEER
        )
        self.assertEqual(response["data"], ["Draft context saved successfully."])
        self.assertEqual(transport.get_calls, [])
        self.assertEqual(len(transport.post_calls), 1)

    def test_explicit_identity_verification_rejects_mismatch(self):
        for identity in (
            {"data": {"id": "u2", "name": "Engineer", "dept": "D1"}},
            {"data": {"id": "u1", "name": "Other", "dept": "D1"}},
            {"data": {"id": "u1", "name": "Engineer", "dept": "D2"}},
            {"data": {"id": "u1", "name": "Engineer", "dept": "D1", "current_dept_id": "D2"}},
        ):
            transport = FakeTransport(identity=identity)
            with self.subTest(identity=identity):
                with self.assertRaises(ProcessEndIdentityError):
                    ProcessEndDraftApiClient(transport).verify_identity(ENGINEER)
                self.assertEqual(transport.post_calls, [])

    def test_explicit_current_department_is_required_and_must_match_account_department(self):
        for identity in (
            {"data": {"id": "u1", "name": "Engineer", "current_dept_id": ""}},
            {"data": {"id": "u1", "name": "Engineer", "current_dept_id": None}},
        ):
            transport = FakeTransport(identity=identity)
            with self.subTest(identity=identity):
                with self.assertRaises(ProcessEndIdentityError):
                    ProcessEndDraftApiClient(transport).verify_identity(ENGINEER)
                self.assertEqual(transport.post_calls, [])

    def test_payload_engineer_mismatch_blocks_post_without_identity_get(self):
        transport = FakeTransport()
        with self.assertRaises(ProcessEndApiError):
            ProcessEndDraftApiClient(transport).save_draft(
                {"procedureDraftQcEngineer": "Other"}, selected_engineer=ENGINEER
            )
        self.assertEqual(transport.get_calls, [])
        self.assertEqual(transport.post_calls, [])

    def test_explicit_identity_get_failure_redacts_transport_detail(self):
        transports = (
            FakeTransport(identity={"data": {"id": "u2", "name": "Other", "dept": "D1"}}),
            FakeTransport(identity={"data": {"id": "", "name": "AnonymousUser", "dept": ""}}),
            FakeTransport(identity_error=RuntimeError("Authorization: JWT secret-token")),
        )
        for transport in transports:
            with self.subTest(identity=transport.identity):
                with self.assertRaises(ProcessEndIdentityError) as caught:
                    ProcessEndDraftApiClient(transport).verify_identity(ENGINEER)
                self.assertEqual(transport.post_calls, [])
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertNotIn("Authorization", str(caught.exception))

    def test_identity_does_not_copy_user_or_department_into_payload(self):
        transport = FakeTransport()
        fields = {"procedureDraftQcEngineer": "Engineer", "modelId": "5"}
        ProcessEndDraftApiClient(transport).save_draft(fields, selected_engineer=BLANK_ID_ENGINEER)
        sent = transport.post_calls[0][1]
        self.assertNotIn("creator", sent)
        self.assertNotIn("creator_id", sent)
        self.assertNotIn("modifier", sent)
        self.assertNotIn("dept_belong_id", sent)
        self.assertEqual(transport.get_calls, [])


class ProcessEndCreationTests(unittest.TestCase):
    def service(self, repository, api):
        return PqcDraftCreationService(
            repository,
            api,
            today=lambda: TODAY,
            now=lambda: datetime(2026, 9, 6, 12, 0, 0),
        )

    def selection(self, type_code, *, item="D-JD-001", preview_serial=1, allow=False, row_number=2):
        template_key = {"01": "CABINET_ASSEMBLY", "02": "CABINET_WIRING", "03": "CABINET_PRE_POWER", "04": "CABINET_POST_POWER", "05": "TORQUE"}.get(type_code, "")
        template = next(item for item in active_templates() if item.template_key == template_key)
        return PqcDraftSelection(
            source_row(item, row_number=row_number),
            type_code,
            allow_existing_draft=allow,
            preview_trigger_basis=f"P-{preview_serial:03d}-{type_code}",
            template_key=template.template_key,
            procedure_code=template.procedure_code,
            model_id=template.model_id,
            procedure_name=template.procedure_name,
            form_suffix=template.form_suffix,
            classification="CABINET",
        )

    def test_serial_success_uses_one_post_each_and_same_new_device_nnn(self):
        repository = MemoryRepository()
        api = MemoryApi(repository)
        result = self.service(repository, api).create_serially(
            (self.selection("02"), self.selection("01")), qc_engineer=ENGINEER
        )
        self.assertEqual([item.type_code for item in result.results], ["01", "02"])
        self.assertTrue(all(item.status is PqcCreationStatus.SUCCESS for item in result.results))
        self.assertEqual([item.trigger_basis for item in result.results], ["P-001-01", "P-001-02"])
        self.assertEqual(len(api.posts), 2)
        self.assertIsNone(repository.readback_windows[0])
        self.assertIsNotNone(repository.readback_windows[1])

    def test_blank_engineer_ids_flow_to_fake_save_with_name_only_payload(self):
        repository = MemoryRepository()
        api = MemoryApi(repository)
        result = self.service(repository, api).create_serially(
            (self.selection("05"),), qc_engineer=BLANK_ID_ENGINEER
        )
        self.assertEqual(result.results[0].status, PqcCreationStatus.SUCCESS)
        self.assertEqual(api.selected_engineer, BLANK_ID_ENGINEER)
        self.assertEqual(api.posts[0]["procedureDraftQcEngineer"], "Engineer")
        self.assertNotIn("creator_id", api.posts[0])
        self.assertNotIn("dept_belong_id", api.posts[0])

    def test_existing_draft_forced_duplicate_reuses_original_number(self):
        existing = PqcDraftRecord("old", "P", "D-JD-001", PQC_TYPE_BY_CODE["05"].template_code, "P-023-05", "Engineer", "", "TORQUE", "扭矩质量检查记录单")
        repository = MemoryRepository(snapshot(drafts=(existing,)))
        api = MemoryApi(repository)
        result = self.service(repository, api).create_serially(
            (self.selection("05", preview_serial=23, allow=True),), qc_engineer=ENGINEER
        )
        self.assertEqual(result.results[0].status, PqcCreationStatus.SUCCESS)
        self.assertEqual(result.results[0].trigger_basis, "P-023-05")
        self.assertEqual(len(api.posts), 1)

    def test_post_uncertain_is_not_retried_even_when_readback_finds_one(self):
        repository = MemoryRepository()
        api = MemoryApi(repository, {"05": "uncertain_saved"})
        result = self.service(repository, api).create_serially(
            (self.selection("05"),), qc_engineer=ENGINEER
        )
        self.assertEqual(result.results[0].status, PqcCreationStatus.UNKNOWN)
        self.assertEqual(len(api.posts), 1)
        self.assertIn("未自动重试", result.results[0].detail)

    def test_explicit_failure_does_not_rollback_success_and_batch_continues(self):
        repository = MemoryRepository()
        api = MemoryApi(repository, {"01": "rejected", "02": "success"})
        result = self.service(repository, api).create_serially(
            (self.selection("01"), self.selection("02")), qc_engineer=ENGINEER
        )
        self.assertEqual([item.status for item in result.results], [PqcCreationStatus.FAILED, PqcCreationStatus.SUCCESS])
        self.assertEqual(len(api.posts), 2)
        self.assertEqual(len(repository.drafts), 1)

    def test_fresh_formal_record_skips_without_post(self):
        formal = PqcFormalRecord("r", "P", "D-JD-001", PQC_TYPE_BY_CODE["01"].template_code, "2", "P-001-01", "CABINET_ASSEMBLY", "机柜装配质量检查记录单")
        repository = MemoryRepository(snapshot(formal=(formal,)))
        api = MemoryApi(repository)
        result = self.service(repository, api).create_serially(
            (self.selection("01"),), qc_engineer=ENGINEER
        )
        self.assertEqual(result.results[0].status, PqcCreationStatus.SKIPPED)
        self.assertEqual(api.posts, [])

    def test_preview_number_occupied_by_other_computer_fails_without_renumber_or_post(self):
        occupied = PqcDraftRecord(
            "other", "P", "OTHER", PQC_TYPE_BY_CODE["02"].template_code, "P-024-02", "Other", "", "CABINET_WIRING", "机柜接线质量检查记录单"
        )
        repository = MemoryRepository(snapshot(drafts=(occupied,)))
        api = MemoryApi(repository)
        result = self.service(repository, api).create_serially(
            (self.selection("05", preview_serial=24),), qc_engineer=ENGINEER
        )
        self.assertEqual(result.results[0].status, PqcCreationStatus.FAILED)
        self.assertEqual(result.results[0].trigger_basis, "P-024-05")
        self.assertIn("预览不一致", result.results[0].detail)
        self.assertEqual(api.posts, [])

    def test_missing_or_invalid_preview_number_never_allocates_silently(self):
        repository = MemoryRepository()
        api = MemoryApi(repository)
        selection = replace(self.selection("05"), preview_trigger_basis="")
        result = self.service(repository, api).create_serially((selection,), qc_engineer=ENGINEER)
        self.assertEqual(result.results[0].status, PqcCreationStatus.FAILED)
        self.assertEqual(api.posts, [])

    def test_fresh_recheck_rejects_stale_composite_template_identity(self):
        repository = MemoryRepository()
        api = MemoryApi(repository)
        stale = replace(self.selection("05"), model_id="stale-model")
        result = self.service(repository, api).create_serially((stale,), qc_engineer=ENGINEER)
        self.assertEqual(result.results[0].status, PqcCreationStatus.FAILED)
        self.assertIn("模板身份", result.results[0].detail)
        self.assertEqual(api.posts, [])


if __name__ == "__main__":
    unittest.main()
