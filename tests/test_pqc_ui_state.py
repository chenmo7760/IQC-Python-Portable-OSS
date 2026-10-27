from __future__ import annotations

import unittest
from datetime import date

from src.pqc_models import PqcCreationBatch, PqcCreationResult, PqcCreationStatus, PqcTool
from src.pqc_rules import build_device_candidates
from src.pqc_ui_state import PqcUiState, PqcUiStateError
from tests.pqc_test_helpers import project, snapshot, source_row


class PqcUiStateV4Tests(unittest.TestCase):
    def setUp(self):
        self.row_a = source_row("A-JD-001", row_number=2)
        self.row_b = source_row("B-001", row_number=3, category="风扇盘")
        self.project = project(self.row_a, self.row_b)
        self.candidates = build_device_candidates(self.project, snapshot())
        self.state = PqcUiState()
        self.state.set_projects(" book.xlsx ", " Engineer ", (self.project,))
        self.state.load_candidates(self.candidates)

    def test_single_project_and_six_slots(self):
        self.assertEqual(self.state.project_number, "P")
        self.assertEqual(len(self.state.candidates[0].types), 6)
        self.assertEqual(self.state.selected_count(), 6)  # 机柜5 + 风扇盘1

    def test_single_template_category_only_is_selectable(self):
        fan = self.state.candidates[1]
        self.assertEqual(fan.type_candidate("CATEGORY").code, "CATEGORY")
        self.assertTrue(fan.type_candidate("CATEGORY").selected)
        self.assertFalse(fan.type_candidate("01").selectable)
        self.state.toggle_type("B-001", "CATEGORY", False)
        self.assertFalse(self.state.candidates[1].type_candidate("CATEGORY").selected)

    def test_tools_are_isolated_by_composite_identity_and_copy_only_selected(self):
        tool = PqcTool("1", "数字万用表", "SN-1", "")
        self.state.set_tools("A-JD-001", "04", (tool, tool), completion_day=date(2026, 9, 6))
        key = ("P", "A-JD-001", "CABINET_POST_POWER", "DEMO-ICS-DEMOFORM-004")
        self.assertEqual(self.state.tool_selections[key], (tool,))
        self.assertNotIn(("P", "A-JD-001", "CABINET_PRE_POWER", "DEMO-ICS-DEMOFORM-003"), self.state.tool_selections)
        self.state.toggle_type("B-001", "CATEGORY", False)
        copied = self.state.copy_tools_to_selected_type("A-JD-001", "04", completion_day=date(2026, 9, 6))
        self.assertEqual(copied, 1)
        self.assertNotIn(("P", "B-001", "FAN_TRAY", "PQC-FAN_TRAY"), self.state.tool_selections)

    def test_selected_drafts_carry_template_identity(self):
        selected = self.state.selected_drafts()
        cabinet = next(item for item in selected if item.source.item_code == "A-JD-001" and item.type_code == "01")
        self.assertEqual(cabinet.template_key, "CABINET_ASSEMBLY")
        self.assertEqual(cabinet.procedure_code, "DEMO-ICS-DEMOFORM-001")

    def test_reset_preserves_scope_and_clears_runtime_state(self):
        self.state.confirmed = False
        self.state.apply_creation(PqcCreationBatch((PqcCreationResult((2,), "P", "A-JD-001", "01", "P-001-01", PqcCreationStatus.SUCCESS, "ok"),)))
        self.state.reset_transient()
        self.assertEqual(self.state.source_path, "book.xlsx")
        self.assertEqual(self.state.project_number, "P")
        self.assertEqual(self.state.candidates, ())
        self.assertEqual(self.state.tool_selections, {})
        self.assertTrue(self.state.confirmed)

    def test_candidates_require_current_project_and_exact_six_slots(self):
        with self.assertRaises(PqcUiStateError):
            self.state.load_candidates((self.candidates[0], self.candidates[0]))
        foreign = build_device_candidates(project(source_row("X-001", project_number="OTHER", category="风扇盘"), project_number="OTHER"), snapshot())[0]
        with self.assertRaises(PqcUiStateError):
            self.state.load_candidates((foreign,))


if __name__ == "__main__":
    unittest.main()
