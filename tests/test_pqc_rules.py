from __future__ import annotations

import unittest
from dataclasses import replace

from src.pqc_models import PqcCandidateState, PqcDraftRecord, PqcFormalRecord, PqcSourceRow, PqcTemplate
from src.pqc_rules import (
    PqcRuleError,
    build_device_candidates,
    classify_source,
    parse_trigger_basis,
    special_case_exclusions,
    with_selection,
)
from tests.pqc_test_helpers import project, snapshot, source_row


class PqcRulesV4Tests(unittest.TestCase):
    def test_existing_formal_records_complete_even_when_new_suffix_mapping_is_missing(self):
        source = source_row("A-JD-001")
        templates = tuple(replace(t, form_suffix="") for t in snapshot().templates)
        formal = tuple(PqcFormalRecord(str(i), "P", source.item_code, t.procedure_code, "2", f"P-001-{i:02d}", template_key=t.template_key, template_name=t.procedure_name) for i, t in enumerate(templates[:5], 1))
        device = build_device_candidates(project(source), snapshot(templates=templates, formal=formal))[0]
        self.assertTrue(all(c.state is PqcCandidateState.FORMAL for c in device.types[:5]))
        self.assertTrue(all(not c.selected and not c.selectable for c in device.types))
        with self.assertRaises(PqcRuleError):
            with_selection(device.types[0], True)

    def test_completion_does_not_hide_missing_mapping_for_unfinished_types(self):
        source = source_row("A-JD-001")
        templates = tuple(replace(t, form_suffix="") for t in snapshot().templates)
        t = templates[0]
        formal = PqcFormalRecord("r", "P", source.item_code, t.procedure_code, "1", "P-001-01", template_key=t.template_key, template_name=t.procedure_name)
        device = build_device_candidates(project(source), snapshot(templates=templates, formal=(formal,)))[0]
        self.assertEqual(device.types[0].state, PqcCandidateState.FORMAL)
        self.assertTrue(all(c.state is PqcCandidateState.CANDIDATE for c in device.types[1:5]))

    def test_completion_is_scoped_to_exact_project_device_and_template(self):
        source = source_row("A-JD-001")
        templates = tuple(replace(t, form_suffix="") for t in snapshot().templates)
        t = templates[0]
        record = PqcFormalRecord("r", "P", source.item_code, t.procedure_code, "2", "P-001-01", template_key=t.template_key, template_name=t.procedure_name)
        for other in (replace(record, project_number="OTHER"), replace(record, item_code="OTHER"), replace(record, template_code="OTHER"), replace(record, template_key="OTHER"), replace(record, template_name="OTHER")):
            with self.subTest(record=other):
                device = build_device_candidates(project(source), snapshot(templates=templates, formal=(other,)))[0]
                identity_matches = other.template_key == t.template_key and other.template_name == t.procedure_name
                expected = PqcCandidateState.CANDIDATE if identity_matches else PqcCandidateState.BLOCKED
                self.assertEqual(device.types[0].state, expected)
                self.assertEqual(device.types[0].selectable, expected is PqcCandidateState.CANDIDATE)

    def test_completion_requires_approved_record_status_and_resolved_template(self):
        source = source_row("A-JD-001")
        templates = snapshot().templates
        t = templates[0]
        record = PqcFormalRecord("r", "P", source.item_code, t.procedure_code, "2", "P-001-01", template_key=t.template_key, template_name=t.procedure_name)
        for state in ("", "0", "3", "审核中"):
            with self.subTest(state=state):
                candidate = build_device_candidates(project(source), snapshot(formal=(replace(record, processstate=state),)))[0].types[0]
                self.assertEqual(candidate.state, PqcCandidateState.ANOMALY)
        for invalid in (replace(t, model_id=""), replace(t, is_lasted_version="0"), replace(t, is_frozen="1"), replace(t, processstate="1")):
            with self.subTest(template=invalid):
                candidate = build_device_candidates(project(source), snapshot(templates=(invalid, *templates[1:]), formal=(record,)))[0].types[0]
                self.assertEqual(candidate.state, PqcCandidateState.BLOCKED)
                self.assertFalse(candidate.selectable)

    def test_completed_type_stays_nonselectable_when_new_number_allocation_conflicts(self):
        source = source_row("A-JD-001")
        t = snapshot().templates[0]
        record = PqcFormalRecord("r", "P", source.item_code, t.procedure_code, "2", "P-001-01", template_key=t.template_key, template_name=t.procedure_name)
        other = replace(record, record_id="other", item_code="OTHER")
        device = build_device_candidates(project(source), snapshot(formal=(record, other)))[0]
        self.assertIsNone(device.serial_number)
        self.assertEqual(device.types[0].state, PqcCandidateState.FORMAL)
        self.assertFalse(device.types[0].selectable)
        self.assertTrue(all(c.state is PqcCandidateState.BLOCKED for c in device.types[1:5]))

    def test_exact_jd_precedence_and_case_sensitive_categories(self):
        self.assertEqual(classify_source(source_row("A-JD-001", category="组件类")), "CABINET")
        self.assertIsNone(classify_source(source_row("A-jd-001", category="nupac_glm")))
        self.assertEqual(classify_source(source_row("A-001", category="DemoLogic_GLM")), "DemoLogic_GLM")
        self.assertIsNone(classify_source(source_row("A-001", category="nupac_glm")))

    def test_cabinet_has_only_five_real_slots_and_ignores_j(self):
        device = build_device_candidates(
            project(source_row("AA-JD-001", category="组件类")), snapshot()
        )[0]
        self.assertEqual([item.code for item in device.types], ["01", "02", "03", "04", "05", "CATEGORY"])
        self.assertEqual([item.inspection_type.procedure_name for item in device.types[:5]], [
            "机柜装配质量检查记录单", "机柜接线质量检查记录单", "机柜上电前质量检查记录单", "机柜上电后质量检查记录单", "扭矩质量检查记录单",
        ])
        self.assertEqual(device.type_candidate("CATEGORY").state, PqcCandidateState.EXCLUDED)

    def test_panel_has_no_post_power_and_single_templates_do_not_expand(self):
        panel = build_device_candidates(project(source_row("PANEL-001", category="盘台")), snapshot())[0]
        self.assertTrue(all(panel.type_candidate(code).selectable for code in ("01", "02", "03", "05")))
        self.assertEqual(panel.type_candidate("04").state, PqcCandidateState.EXCLUDED)
        fan = build_device_candidates(project(source_row("FAN-001", category="风扇盘")), snapshot())[0]
        self.assertEqual(fan.type_candidate("CATEGORY").inspection_type.procedure_name, "风扇盘集成质量检查记录单")
        self.assertTrue(fan.type_candidate("CATEGORY").selected)
        self.assertTrue(all(item.state is PqcCandidateState.EXCLUDED for item in fan.types[:5]))

    def test_unknown_j_blocks_every_slot(self):
        device = build_device_candidates(project(source_row("UNKNOWN-001", category="未知")), snapshot())[0]
        self.assertTrue(all(item.state is PqcCandidateState.BLOCKED for item in device.types))
        self.assertTrue(all("类别无法识别" in item.detail for item in device.types))

    def test_unrecognized_h_single_template_is_anomaly_and_manual(self):
        device = build_device_candidates(project(source_row("FAN-002", category="风扇盘", special_case="人工判断")), snapshot())[0]
        candidate = device.type_candidate("CATEGORY")
        self.assertEqual(candidate.state, PqcCandidateState.ANOMALY)
        self.assertTrue(candidate.selectable)
        self.assertFalse(candidate.selected)

    def test_h_exclusion_only_applies_to_existing_roles(self):
        panel = build_device_candidates(project(source_row("PANEL-002", category="盘台", special_case="无上电")), snapshot())[0]
        self.assertEqual(panel.type_candidate("03").state, PqcCandidateState.EXCLUDED)
        self.assertEqual(panel.type_candidate("04").state, PqcCandidateState.EXCLUDED)
        fan = build_device_candidates(project(source_row("FAN-003", category="风扇盘", special_case="无上电")), snapshot())[0]
        self.assertEqual(fan.type_candidate("CATEGORY").state, PqcCandidateState.CANDIDATE)

    def test_single_template_known_h_without_role_only_warns(self):
        device = build_device_candidates(project(source_row("FAN-003B", category="风扇盘", special_case="无扭矩上电")), snapshot())[0]
        self.assertTrue(device.type_candidate("CATEGORY").selected)
        self.assertTrue(any("无对应角色" in warning for warning in device.warnings))

    def test_form_suffix_is_derived_from_slot_and_ignores_template_value(self):
        templates = list(snapshot().templates)
        for invalid_suffix in ("1", "０１", "01 ", "17", ""):
            invalid = replace(templates[0], form_suffix=invalid_suffix)
            device = build_device_candidates(
                project(source_row("A-JD-001")),
                snapshot(templates=(invalid, *templates[1:])),
            )[0]
            candidate = device.type_candidate("01")
            self.assertEqual(candidate.state, PqcCandidateState.CANDIDATE)
            self.assertEqual(candidate.trigger_basis, "P-001-01")
        self.assertIsNone(parse_trigger_basis("P", "P-001-０１"))

    def test_ss_follows_slot_mapping_and_history_keeps_distinct_trigger_basis(self):
        templates = list(snapshot().templates)
        duplicate = replace(templates[1], form_suffix=templates[0].form_suffix)
        device = build_device_candidates(
            project(source_row("A-JD-002")),
            snapshot(templates=(templates[0], duplicate, *templates[2:])),
        )[0]
        self.assertEqual(device.type_candidate("01").state, PqcCandidateState.CANDIDATE)
        self.assertEqual(device.type_candidate("02").state, PqcCandidateState.CANDIDATE)
        self.assertEqual(device.type_candidate("01").trigger_basis, "P-001-01")
        self.assertEqual(device.type_candidate("02").trigger_basis, "P-001-02")

    def test_missing_procedure_code_or_ss_fails_closed(self):
        templates = list(snapshot().templates)
        missing_code = replace(templates[0], procedure_code="")
        device = build_device_candidates(project(source_row("A-JD-002")), snapshot(templates=(missing_code, *templates[1:])))[0]
        self.assertEqual(device.type_candidate("01").state, PqcCandidateState.BLOCKED)
        missing_ss = replace(templates[1], form_suffix="")
        device = build_device_candidates(project(source_row("A-JD-003")), snapshot(templates=(templates[0], missing_ss, *templates[2:])))[0]
        self.assertEqual(device.type_candidate("02").state, PqcCandidateState.CANDIDATE)

    def test_serial_allocation_is_isolated_by_project_number(self):
        a_existing = PqcFormalRecord("a", "A", "A-JD-001", "DEMO-ICS-DEMOFORM-001", "2", "A-921-01", template_key="CABINET_ASSEMBLY", template_name=snapshot().templates[0].procedure_name)
        b_existing = PqcFormalRecord("b", "B", "B-JD-001", "DEMO-ICS-DEMOFORM-001", "2", "B-002-01", template_key="CABINET_ASSEMBLY", template_name=snapshot().templates[0].procedure_name)
        a_device = build_device_candidates(project(source_row("A-JD-002", project_number="A"), project_number="A"), snapshot(formal=(a_existing, b_existing)))[0]
        b_device = build_device_candidates(project(source_row("B-JD-002", project_number="B"), project_number="B"), snapshot(formal=(a_existing, b_existing)))[0]
        self.assertEqual(a_device.serial_number, 922)
        self.assertEqual(b_device.serial_number, 3)
        self.assertEqual(a_device.type_candidate("01").trigger_basis, "A-922-01")
        self.assertEqual(b_device.type_candidate("01").trigger_basis, "B-003-01")

    def test_same_procedure_code_for_different_names_is_blocked(self):
        templates = list(snapshot().templates)
        duplicate = replace(templates[-1], procedure_code=templates[-2].procedure_code)
        device = build_device_candidates(project(source_row("A-001", category="组件类")), snapshot(templates=(*templates[:-1], duplicate)))[0]
        self.assertEqual(device.type_candidate("CATEGORY").state, PqcCandidateState.BLOCKED)

    def test_empty_template_key_history_does_not_fallback_by_procedure_code(self):
        source = source_row("A-JD-009")
        history = PqcFormalRecord("r", "P", source.item_code, "DEMO-ICS-DEMOFORM-001", "2", "P-001-01")
        device = build_device_candidates(project(source), snapshot(formal=(history,)))[0]
        self.assertEqual(device.type_candidate("01").state, PqcCandidateState.BLOCKED)
        self.assertIn("复合身份", device.type_candidate("01").detail)

    def test_special_case_normalization_and_strict_trigger_structure(self):
        excluded, recognized = special_case_exclusions(" （无扭矩）。 ")
        self.assertTrue(recognized)
        self.assertEqual(excluded, frozenset({"05"}))
        self.assertEqual(parse_trigger_basis("P", "P-001-05"), (1, "05"))
        self.assertEqual(parse_trigger_basis("P", "P-001-99"), (1, "99"))
        self.assertIsNone(parse_trigger_basis("P", "P-1-05"))

    def test_nonselectable_cell_cannot_be_forced(self):
        candidate = build_device_candidates(project(source_row("FAN-004", category="风扇盘")), snapshot())[0].type_candidate("01")
        with self.assertRaises(PqcRuleError):
            with_selection(candidate, True)


if __name__ == "__main__":
    unittest.main()
