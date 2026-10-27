from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.import_orchestrator import ImportStatus, OrderImportResult
from src.ui_state import UiBatchState, UiOrderStatus, UiStateError


def _result(order: str, status: ImportStatus, *, attempt: int = 1) -> OrderImportResult:
    return OrderImportResult(
        order_number=order,
        status=status,
        attempt=attempt,
        confirmed=True,
        retry_requested=False,
        exit_code=0 if status is ImportStatus.COMMITTED else 2,
        duration_seconds=0.1,
        bat_started=True,
        diagnostic=status.value,
    )


class UiBatchStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_confirm_build_import_and_commit(self) -> None:
        state = UiBatchState()
        result_path = self.root / "iqc_draft_A.xlsx"
        state.stage("A", self.root / "iqc_draft_A.sql", result_path=result_path, row_count=2)
        self.assertEqual(state.get("A").status, UiOrderStatus.READ_SUCCESS)
        self.assertEqual(state.get("A").result_path, result_path)
        state.mark_generated("A")
        state.confirm("A")
        requests = state.build_import_requests(("A",))
        self.assertEqual(requests[0].order_number, "A")
        self.assertEqual(state.get("A").status, UiOrderStatus.IMPORTING)
        state.apply_import_results((_result("A", ImportStatus.COMMITTED),))
        self.assertEqual(state.get("A").status, UiOrderStatus.COMMITTED)
        with self.assertRaises(UiStateError):
            state.confirm("A")
        with self.assertRaises(UiStateError):
            state.build_import_requests(("A",))

    def test_committed_order_requires_duplicate_confirmation_then_can_import(self) -> None:
        state = UiBatchState()
        state.stage("DUP", self.root / "iqc_draft_DUP.sql", status=UiOrderStatus.COMMITTED)
        with self.assertRaises(UiStateError):
            state.confirm("DUP")
        self.assertEqual(state.get("DUP").status, UiOrderStatus.COMMITTED)
        confirmed = state.confirm("DUP", duplicate_confirmed=True)
        self.assertTrue(confirmed.confirmed)
        self.assertTrue(confirmed.duplicate_confirmed)
        requests = state.build_import_requests(("DUP",))
        self.assertTrue(requests[0].duplicate_confirmed)
        self.assertEqual(state.get("DUP").status, UiOrderStatus.IMPORTING)
        state.apply_import_results((_result("DUP", ImportStatus.COMMITTED, attempt=2),))
        self.assertEqual(state.get("DUP").status, UiOrderStatus.COMMITTED)
        self.assertFalse(state.get("DUP").duplicate_confirmed)

    def test_regenerated_order_keeps_explicit_duplicate_confirmation_for_request(self) -> None:
        """Persisted history can mark a freshly generated row as duplicate."""

        state = UiBatchState()
        state.stage("REGENERATE", self.root / "iqc_draft_REGENERATE.sql", status=UiOrderStatus.GENERATED_CONFIRM)
        confirmed = state.confirm("REGENERATE", duplicate_confirmed=True)
        self.assertTrue(confirmed.confirmed)
        self.assertTrue(confirmed.duplicate_confirmed)
        request = state.build_import_requests(("REGENERATE",))[0]
        self.assertTrue(request.duplicate_confirmed)

    def test_duplicate_blocked_result_keeps_committed_order_repromptable(self) -> None:
        state = UiBatchState()
        state.stage("DUP", self.root / "iqc_draft_DUP.sql", status=UiOrderStatus.COMMITTED)
        state.confirm("DUP", duplicate_confirmed=True)
        state.build_import_requests(("DUP",))
        state.apply_import_results((_result("DUP", ImportStatus.DUPLICATE_BLOCKED),))
        current = state.get("DUP")
        self.assertEqual(current.status, UiOrderStatus.COMMITTED)
        self.assertFalse(current.confirmed)
        self.assertFalse(current.duplicate_confirmed)

    def test_rollback_requires_retry_request_and_second_confirmation(self) -> None:
        state = UiBatchState()
        state.stage("B", self.root / "iqc_draft_B.sql")
        state.mark_generated("B")
        state.confirm("B")
        state.build_import_requests(("B",))
        state.apply_import_results((_result("B", ImportStatus.ROLLED_BACK_RETRYABLE),))
        self.assertEqual(state.get("B").status, UiOrderStatus.ROLLED_BACK_RETRYABLE)
        state.request_retry("B")
        self.assertFalse(state.get("B").confirmed)
        state.confirm("B")
        retry = state.build_import_requests(("B",))
        self.assertTrue(retry[0].retry_failed)

    def test_not_confirmed_cancelled_and_blocked_transitions(self) -> None:
        state = UiBatchState()
        state.stage("A", self.root / "iqc_draft_A.sql")
        with self.assertRaises(UiStateError):
            state.build_import_requests(("A",))
        state.cancel("A")
        self.assertEqual(state.get("A").status, UiOrderStatus.CANCELLED)
        state.confirm("A")
        state.build_import_requests(("A",))
        state.apply_import_results((_result("A", ImportStatus.COMMIT_UNVERIFIED_BLOCKED),))
        self.assertEqual(state.get("A").status, UiOrderStatus.BLOCKED)
        with self.assertRaises(UiStateError):
            state.confirm("A")

    def test_start_failure_is_retryable_error_not_rollback_claim(self) -> None:
        state = UiBatchState()
        state.stage("START", self.root / "iqc_draft_START.sql")
        state.confirm("START")
        state.build_import_requests(("START",))
        state.apply_import_results((_result("START", ImportStatus.START_FAILED_RETRYABLE),))
        self.assertEqual(state.get("START").status, UiOrderStatus.ERROR_RETRYABLE)
        state.request_retry("START")
        state.confirm("START")
        self.assertTrue(state.build_import_requests(("START",))[0].retry_failed)

    def test_running_cancel_is_a_request_until_executor_result(self) -> None:
        state = UiBatchState()
        state.stage("RUN", self.root / "iqc_draft_RUN.sql")
        state.confirm("RUN")
        state.build_import_requests(("RUN",))
        state.cancel("RUN")
        current = state.get("RUN")
        self.assertEqual(current.status, UiOrderStatus.IMPORTING)
        self.assertTrue(current.cancel_requested)
        state.apply_import_results((_result("RUN", ImportStatus.CANCELLED_ROLLBACK_UNVERIFIED_BLOCKED),))
        self.assertEqual(state.get("RUN").status, UiOrderStatus.BLOCKED)

    def test_duplicate_and_clear_during_import_are_rejected(self) -> None:
        state = UiBatchState()
        state.stage("A", self.root / "iqc_draft_A.sql")
        with self.assertRaises(UiStateError):
            state.stage("A", self.root / "other.sql")
        state.confirm("A")
        state.build_import_requests(("A",))
        with self.assertRaises(UiStateError):
            state.clear()

    def test_entry_submission_reuses_confirmation_gate_and_marks_retryable(self) -> None:
        """Entry 保存与 BAT 共用确认门；失败状态可重提且不伪造成功。"""

        state = UiBatchState()
        state.stage("ENTRY", self.root / "iqc_draft_ENTRY.sql", status=UiOrderStatus.GENERATED_CONFIRM)
        state.confirm("ENTRY")
        state.begin_entry_submission(("ENTRY",))
        self.assertEqual(state.get("ENTRY").status, UiOrderStatus.IMPORTING)
        state.apply_entry_submission_results({"ENTRY": (False, "FAILED_FIELD_REQUIRED")})
        failed = state.get("ENTRY")
        self.assertEqual(failed.status, UiOrderStatus.ERROR_RETRYABLE)
        self.assertTrue(failed.retry_failed)

    def test_rt_paper_13_15_paper_only_cannot_confirm_import_or_entry_submit(self) -> None:
        state = UiBatchState()
        state.stage(
            "PAPER",
            self.root / "paper_only_placeholder.sql",
            status=UiOrderStatus.PAPER_ONLY,
            detail="纸质记录/不导入",
        )

        with self.assertRaises(UiStateError):
            state.confirm("PAPER")
        current = state.get("PAPER")
        self.assertFalse(current.confirmed)
        self.assertEqual(current.status, UiOrderStatus.PAPER_ONLY)

        with self.assertRaises(UiStateError):
            state.build_import_requests(("PAPER",))
        with self.assertRaises(UiStateError):
            state.begin_entry_submission(("PAPER",))


if __name__ == "__main__":
    unittest.main()
