"""PQC v4 acceptance contract tests; no live DB/API or POST is used."""

from __future__ import annotations

import unittest
from datetime import date
from dataclasses import replace

from src.pqc_models import PqcCandidateState, PqcModelContext, PqcModelContextRow, PqcTemplate, PqcTool
from src.pqc_rules import build_device_candidates, classify_source, parse_trigger_basis
from src.pqc_ui_state import PqcUiState
from src.processend_api import ProcessEndApiError, build_processend_draft_payload
from tests.pqc_test_helpers import project, snapshot, source_row


class PqcV4AcceptanceTests(unittest.TestCase):
    def test_precedence_and_exact_category_matrix(self):
        self.assertEqual(classify_source(source_row("AA-JD-001", category="风扇盘")), "CABINET")
        self.assertEqual(classify_source(source_row("FAN-001", category="风扇盘")), "风扇盘")
        self.assertEqual(classify_source(source_row("FAN-002", category="风扇盘 ")), "风扇盘")
        self.assertIsNone(classify_source(source_row("FAN-003", category="风扇盤")))

    def test_mixed_devices_project_is_projected_to_six_slots(self):
        cabinet = source_row("CAB-JD-001", category="组件类")
        panel = source_row("PANEL-001", category="盘台", row_number=3)
        fan = source_row("FAN-001", category="风扇盘", row_number=4)
        devices = build_device_candidates(project(cabinet, panel, fan), snapshot())
        self.assertTrue(all(len(item.types) == 6 for item in devices))
        self.assertEqual([item.code for item in devices[0].types], ["01", "02", "03", "04", "05", "CATEGORY"])
        self.assertEqual(devices[1].type_candidate("04").state, PqcCandidateState.EXCLUDED)
        self.assertEqual(devices[2].type_candidate("CATEGORY").inspection_type.procedure_name, "风扇盘集成质量检查记录单")

    def test_single_template_is_not_split_by_h_and_unknown_h_is_manual(self):
        known = build_device_candidates(project(source_row("FAN-004", category="风扇盘", special_case="无扭矩上电")), snapshot())[0]
        self.assertTrue(known.type_candidate("CATEGORY").selected)
        self.assertTrue(any("无对应角色" in warning for warning in known.warnings))
        unknown = build_device_candidates(project(source_row("FAN-005", category="风扇盘", special_case="人工判断")), snapshot())[0]
        self.assertEqual(unknown.type_candidate("CATEGORY").state, PqcCandidateState.ANOMALY)
        self.assertFalse(unknown.type_candidate("CATEGORY").selected)

    def test_template_identity_fail_closed(self):
        templates = list(snapshot().templates)
        no_code = replace(templates[0], procedure_code="")
        candidate = build_device_candidates(project(source_row("CAB-JD-002")), snapshot(templates=(no_code, *templates[1:])))[0].type_candidate("01")
        self.assertEqual(candidate.state, PqcCandidateState.BLOCKED)
        no_ss = replace(templates[0], form_suffix="")
        candidate = build_device_candidates(project(source_row("CAB-JD-003")), snapshot(templates=(no_ss, *templates[1:])))[0].type_candidate("01")
        self.assertEqual(candidate.state, PqcCandidateState.CANDIDATE)
        duplicate_code = replace(templates[-1], procedure_code=templates[-2].procedure_code)
        candidate = build_device_candidates(project(source_row("CAB-JD-004")), snapshot(templates=(*templates[:-1], duplicate_code)))[0].type_candidate("01")
        self.assertEqual(candidate.state, PqcCandidateState.BLOCKED)

    def test_composite_identity_flows_into_selection_and_tool_key(self):
        source = source_row("CAB-JD-005")
        state = PqcUiState()
        state.set_projects("PQC.xlsx", "Engineer", (project(source),))
        state.load_candidates(build_device_candidates(project(source), snapshot()))
        tool = PqcTool("tool", "扭矩扳手", "SN", "2027-01-01")
        state.set_tools(source.item_code, "05", (tool,), completion_day=date(2026, 9, 6))
        self.assertIn(("P", source.item_code, "TORQUE", "DEMO-ICS-DEMOFORM-005"), state.tool_selections)
        selection = next(item for item in state.selected_drafts() if item.type_code == "05")
        self.assertEqual((selection.template_key, selection.procedure_code), ("TORQUE", "DEMO-ICS-DEMOFORM-005"))

    def test_category_template_payload_requires_real_procedure_code(self):
        source = source_row("FAN-006", category="风扇盘")
        context = PqcModelContext("15", "PQC-FAN_TRAY", "风扇盘集成质量检查记录单", "1", (PqcModelContextRow("1", "检查项目", "检查标准"),), template_key="FAN_TRAY", form_suffix="01")
        payload = build_processend_draft_payload(source, type_code="CATEGORY", template_key="FAN_TRAY", procedure_code="PQC-FAN_TRAY", procedure_name="风扇盘集成质量检查记录单", form_suffix="01", model_id="15", trigger_basis="P-001-01", qc_engineer="Engineer", model_context=context, completion_day=date(2026, 9, 6))
        self.assertEqual(payload["modelId"], "15")
        self.assertEqual(payload["procedureTriggerBasis"], "P-001-01")
        with self.assertRaises(ProcessEndApiError):
            build_processend_draft_payload(source, type_code="CATEGORY", template_key="FAN_TRAY", procedure_code="", procedure_name="风扇盘集成质量检查记录单", form_suffix="15", model_id="15", trigger_basis="P-001-15", qc_engineer="Engineer", model_context=context, completion_day=date(2026, 9, 6))
        with self.assertRaises(ProcessEndApiError):
            build_processend_draft_payload(source, type_code="CATEGORY", template_key="FAN_TRAY", procedure_code="PQC-FAN_TRAY", procedure_name="风扇盘集成质量检查记录单", form_suffix="15", model_id="15", trigger_basis="P-001-15", qc_engineer="Engineer", model_context=replace(context, template_key=""), completion_day=date(2026, 9, 6))

    def test_trigger_basis_uses_real_two_digit_suffix_not_category_column(self):
        self.assertEqual(parse_trigger_basis("P", "P-001-15"), (1, "15"))
        self.assertIsNone(parse_trigger_basis("P", "P-001-CATEGORY"))


if __name__ == "__main__":
    unittest.main()
