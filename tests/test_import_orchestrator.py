from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.import_orchestrator import (
    BatExecutionResult,
    ImportOrchestrationError,
    ImportOrchestrator,
    ImportRequest,
    ImportStatus,
    WindowsBatExecutor,
    _decode_process_output,
)
from src.sql_generator import SqlContextRow, SqlOrder, SqlRecord, render_order_sql
from src.ui_state import UiBatchState, UiOrderStatus


def _record(order_number: str) -> SqlRecord:
    return SqlRecord(
        item_code=f"M-{order_number}",
        item_name="模拟物料",
        quantity=1,
        supplier="模拟供应商",
        procedure_name="模拟检查单",
        procedure_model_id=42,
        engineer_id=7,
        engineer_name="合成工程师02",
        engineer_dept_id="10",
        trigger_basis=order_number,
        check_number=None,
        contexts=(SqlContextRow("1", "外观", "无损伤"),),
    )


def _write_sql(folder: Path, order_number: str, *, committed_marker: bool = True) -> Path:
    path = folder / f"iqc_draft_{order_number}.sql"
    text = render_order_sql(SqlOrder(order_number, (_record(order_number),)))
    if not committed_marker:
        text = text.replace("'COMMITTED' AS log_status", "'PREPARED_NOT_EXECUTED' AS log_status")
    path.write_text(text, encoding="utf-8")
    return path


class ScriptedExecutor:
    def __init__(self, script: dict[str, list[BatExecutionResult]]) -> None:
        self.script = script
        self.calls: list[str] = []

    def execute(self, runner_bat, sql_path, timeout_seconds, cancel_event=None):
        prefix = "iqc_draft_"
        order_number = sql_path.stem[len(prefix):] if sql_path.stem.startswith(prefix) else sql_path.stem
        self.calls.append(order_number)
        return self.script[order_number].pop(0)


def _success(detail: str = "COMMITTED") -> BatExecutionResult:
    return BatExecutionResult(
        started=True,
        exit_code=0,
        duration_seconds=0.1,
        commit_confirmed=True,
        detail=detail,
    )


def _rolled_back(exit_code: int = 1) -> BatExecutionResult:
    return BatExecutionResult(
        started=True,
        exit_code=exit_code,
        duration_seconds=0.2,
        rollback_confirmed=True,
        detail="ROLLBACK_CONFIRMED",
    )


class ImportOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runner = self.root / "run_generated_sql.bat"
        self.runner.write_text("@echo off\nexit /b 0\n", encoding="utf-8")
        self.state = self.root / "iqc_import_state.json"
        self.reports = self.root / "reports"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _orchestrator(self, executor: ScriptedExecutor) -> ImportOrchestrator:
        return ImportOrchestrator(
            self.runner,
            executor,
            state_path=self.state,
            report_dir=self.reports,
            timeout_seconds=5,
        )

    def test_unconfirmed_and_cancelled_before_start_never_call_bat(self) -> None:
        a = _write_sql(self.root, "A")
        b = _write_sql(self.root, "B")
        executor = ScriptedExecutor({})
        result = self._orchestrator(executor).run_batch(
            (
                ImportRequest("A", a, confirmed=False),
                ImportRequest("B", b, confirmed=True, cancel_before_start=True),
            ),
            batch_id="manual-gate",
        )
        self.assertEqual(executor.calls, [])
        self.assertEqual(
            [item.status for item in result.results],
            [ImportStatus.NOT_CONFIRMED, ImportStatus.CANCELLED_BEFORE_START],
        )
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertFalse(report["bat_executor_invoked"])

    def test_a_success_b_failure_c_continues_and_states_are_persisted(self) -> None:
        paths = {order: _write_sql(self.root, order) for order in "ABC"}
        executor = ScriptedExecutor({"A": [_success()], "B": [_rolled_back()], "C": [_success()]})
        result = self._orchestrator(executor).run_batch(
            tuple(ImportRequest(order, paths[order], confirmed=True) for order in "ABC"),
            batch_id="continue-after-failure",
        )
        self.assertEqual(executor.calls, ["A", "B", "C"])
        self.assertEqual(
            [item.status for item in result.results],
            [ImportStatus.COMMITTED, ImportStatus.ROLLED_BACK_RETRYABLE, ImportStatus.COMMITTED],
        )
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["orders"]["A"]["status"], "COMMITTED")
        self.assertEqual(state["orders"]["B"]["status"], "ROLLED_BACK_RETRYABLE")
        self.assertEqual(state["orders"]["C"]["attempts"], 1)

    def test_retry_requires_new_confirmation_and_committed_order_is_blocked(self) -> None:
        a = _write_sql(self.root, "A")
        b = _write_sql(self.root, "B")
        first_executor = ScriptedExecutor({"A": [_success()], "B": [_rolled_back()]})
        orchestrator = self._orchestrator(first_executor)
        orchestrator.run_batch(
            (ImportRequest("A", a, True), ImportRequest("B", b, True)),
            batch_id="first-attempt",
        )

        retry_executor = ScriptedExecutor({"B": [_success()]})
        resumed = self._orchestrator(retry_executor)
        blocked = resumed.run_batch(
            (ImportRequest("A", a, True), ImportRequest("B", b, True)),
            batch_id="blocked-retry",
        )
        self.assertEqual(
            [item.status for item in blocked.results],
            [ImportStatus.DUPLICATE_BLOCKED, ImportStatus.RETRY_CONFIRMATION_REQUIRED],
        )
        self.assertEqual(retry_executor.calls, [])

        retried = resumed.run_batch(
            (ImportRequest("B", b, True, retry_failed=True),),
            batch_id="confirmed-retry",
        )
        self.assertEqual(retry_executor.calls, ["B"])
        self.assertEqual(retried.results[0].status, ImportStatus.COMMITTED)
        self.assertEqual(retried.results[0].attempt, 2)

    def test_duplicate_import_requires_explicit_confirmation_then_runs(self) -> None:
        sql = _write_sql(self.root, "DUPCONF")
        first_executor = ScriptedExecutor({"DUPCONF": [_success()]})
        orchestrator = self._orchestrator(first_executor)
        first = orchestrator.run_batch(
            (ImportRequest("DUPCONF", sql, confirmed=True),),
            batch_id="duplicate-first",
        )
        self.assertEqual(first.results[0].status, ImportStatus.COMMITTED)

        retry_executor = ScriptedExecutor({"DUPCONF": [_success("second COMMITTED")]})
        resumed = self._orchestrator(retry_executor)
        result = resumed.run_batch(
            (ImportRequest("DUPCONF", sql, confirmed=True, duplicate_confirmed=True),),
            batch_id="duplicate-confirmed",
        )
        self.assertEqual(retry_executor.calls, ["DUPCONF"])
        self.assertEqual(result.results[0].status, ImportStatus.COMMITTED)
        self.assertEqual(result.results[0].attempt, 2)
        self.assertIn("用户已确认本次为重复导入", result.results[0].diagnostic)

    def test_duplicate_import_without_confirmation_never_calls_bat(self) -> None:
        sql = _write_sql(self.root, "DUPNO")
        first_executor = ScriptedExecutor({"DUPNO": [_success()]})
        self._orchestrator(first_executor).run_batch(
            (ImportRequest("DUPNO", sql, confirmed=True),),
            batch_id="duplicate-no-first",
        )
        retry_executor = ScriptedExecutor({"DUPNO": [_success()]})
        result = self._orchestrator(retry_executor).run_batch(
            (ImportRequest("DUPNO", sql, confirmed=True),),
            batch_id="duplicate-no-confirm",
        )
        self.assertEqual(result.results[0].status, ImportStatus.DUPLICATE_BLOCKED)
        self.assertEqual(retry_executor.calls, [])
        self.assertIn("未获得用户确认", result.results[0].diagnostic)

    def test_committed_history_lookup_survives_orchestrator_restart(self) -> None:
        """A regenerated UI row must still see the first committed attempt."""

        sql = _write_sql(self.root, "REGENERATE")
        first_executor = ScriptedExecutor({"REGENERATE": [_success()]})
        first = self._orchestrator(first_executor)
        first_result = first.run_batch(
            (ImportRequest("REGENERATE", sql, confirmed=True),),
            batch_id="regenerate-first",
        )
        self.assertEqual(first_result.results[0].status, ImportStatus.COMMITTED)
        self.assertTrue(first.is_committed("REGENERATE"))

        # Constructing a new orchestrator models an application restart.  The
        # regenerated UI row itself is not COMMITTED, but persisted history is.
        resumed = self._orchestrator(ScriptedExecutor({}))
        self.assertEqual(resumed.history_status("REGENERATE"), ImportStatus.COMMITTED)
        self.assertTrue(resumed.is_committed("REGENERATE"))

        regenerated_ui = UiBatchState()
        regenerated_ui.stage("REGENERATE", sql, status=UiOrderStatus.GENERATED_CONFIRM)
        regenerated_ui.confirm("REGENERATE", duplicate_confirmed=True)
        request = regenerated_ui.build_import_requests(("REGENERATE",))[0]
        self.assertTrue(request.duplicate_confirmed)

        second_executor = ScriptedExecutor({"REGENERATE": [_success("second COMMITTED")]})
        second = self._orchestrator(second_executor)
        second_result = second.run_batch((request,), batch_id="regenerate-second")
        self.assertEqual(second_result.results[0].status, ImportStatus.COMMITTED)
        self.assertEqual(second_result.results[0].attempt, 2)

    def test_timeout_and_running_cancel_require_rollback_evidence(self) -> None:
        timeout_sql = _write_sql(self.root, "TIMEOUT")
        cancel_sql = _write_sql(self.root, "CANCEL")
        executor = ScriptedExecutor(
            {
                "TIMEOUT": [
                    BatExecutionResult(started=True, timed_out=True, rollback_confirmed=True, duration_seconds=5)
                ],
                "CANCEL": [
                    BatExecutionResult(started=True, cancelled=True, rollback_confirmed=False, duration_seconds=1)
                ],
            }
        )
        result = self._orchestrator(executor).run_batch(
            (
                ImportRequest("TIMEOUT", timeout_sql, True),
                ImportRequest("CANCEL", cancel_sql, True),
            ),
            batch_id="timeout-cancel",
        )
        self.assertEqual(result.results[0].status, ImportStatus.TIMED_OUT_ROLLED_BACK_RETRYABLE)
        self.assertEqual(result.results[1].status, ImportStatus.CANCELLED_ROLLBACK_UNVERIFIED_BLOCKED)

    def test_zero_exit_without_commit_and_error_without_rollback_are_blocked(self) -> None:
        commit_sql = _write_sql(self.root, "COMMITUNKNOWN")
        rollback_sql = _write_sql(self.root, "ROLLBACKUNKNOWN")
        executor = ScriptedExecutor(
            {
                "COMMITUNKNOWN": [BatExecutionResult(started=True, exit_code=0)],
                "ROLLBACKUNKNOWN": [BatExecutionResult(started=True, exit_code=2)],
            }
        )
        result = self._orchestrator(executor).run_batch(
            (
                ImportRequest("COMMITUNKNOWN", commit_sql, True),
                ImportRequest("ROLLBACKUNKNOWN", rollback_sql, True),
            ),
            batch_id="uncertain-evidence",
        )
        self.assertEqual(result.results[0].status, ImportStatus.COMMIT_UNVERIFIED_BLOCKED)
        self.assertEqual(result.results[1].status, ImportStatus.ROLLBACK_UNVERIFIED_BLOCKED)

    def test_sql_must_have_committed_runtime_marker(self) -> None:
        sql = _write_sql(self.root, "OLD", committed_marker=False)
        executor = ScriptedExecutor({"OLD": [_success()]})
        with self.assertRaises(ImportOrchestrationError):
            self._orchestrator(executor).run_batch(
                (ImportRequest("OLD", sql, True),),
                batch_id="old-marker",
            )
        self.assertEqual(executor.calls, [])

    def test_all_sql_artifacts_are_preflighted_before_first_call(self) -> None:
        valid = _write_sql(self.root, "VALID")
        old = _write_sql(self.root, "OLD2", committed_marker=False)
        executor = ScriptedExecutor({"VALID": [_success()], "OLD2": [_success()]})
        with self.assertRaises(ImportOrchestrationError):
            self._orchestrator(executor).run_batch(
                (ImportRequest("VALID", valid, True), ImportRequest("OLD2", old, True)),
                batch_id="preflight-all",
            )
        self.assertEqual(executor.calls, [])

    def test_report_redacts_connection_looking_values(self) -> None:
        sql = _write_sql(self.root, "SECRET")
        executor = ScriptedExecutor(
            {
                "SECRET": [
                    _success(
                        "--password=secret --password=\"quoted\" "
                        "MYSQL_PASSWORD=abc password:'xyz'"
                    )
                ]
            }
        )
        result = self._orchestrator(executor).run_batch(
            (ImportRequest("SECRET", sql, True),),
            batch_id="redaction",
        )
        report_text = result.report_path.read_text(encoding="utf-8")
        self.assertNotIn("secret", report_text)
        self.assertNotIn("abc", report_text)
        self.assertNotIn("xyz", report_text)
        self.assertNotIn("quoted", report_text)
        self.assertIn("credential redacted", report_text)

    def test_confirmation_flags_must_be_real_booleans(self) -> None:
        sql = _write_sql(self.root, "BOOL")
        executor = ScriptedExecutor({"BOOL": [_success()]})
        with self.assertRaises(ImportOrchestrationError):
            self._orchestrator(executor).run_batch(
                (ImportRequest("BOOL", sql, confirmed="yes"),),  # type: ignore[arg-type]
                batch_id="invalid-bool",
            )
        self.assertEqual(executor.calls, [])

    def test_timeout_must_be_finite_and_positive(self) -> None:
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ImportOrchestrationError):
                    ImportOrchestrator(self.runner, ScriptedExecutor({}), timeout_seconds=value)

    def test_unauthorized_windows_executor_cannot_start_bat(self) -> None:
        sql = _write_sql(self.root, "LOCKED")
        result = WindowsBatExecutor(authorized=False).execute(self.runner, sql, 5)
        self.assertFalse(result.started)
        self.assertIsNone(result.exit_code)
        self.assertIn("未获得", result.detail)

    def test_cmd_output_decodes_chinese_windows_code_page(self) -> None:
        encoded = "运行时\\db\\run_generated_sql.bat".encode("gb18030")
        self.assertEqual(_decode_process_output(encoded), "运行时\\db\\run_generated_sql.bat")

    @unittest.skipUnless(os.name == "nt", "BAT executor is Windows-only")
    def test_authorized_executor_preserves_quoted_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wo08 命令 ") as folder:
            root = Path(folder)
            runner = root / "run_generated_sql.bat"
            sql = root / "iqc_draft_QUOTED.sql"
            runner.write_text("@echo off\necho runner-called\nexit /b 0\n", encoding="ascii")
            sql.write_text("-- test only\n", encoding="ascii")
            result = WindowsBatExecutor(authorized=True).execute(runner, sql, 5)
        self.assertTrue(result.started)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("runner-called", result.stdout)

    def test_duplicate_order_in_one_batch_runs_first_only_and_audits_dedup(self) -> None:
        duplicate_sql = _write_sql(self.root, "DUP")
        other_sql = _write_sql(self.root, "OTHER")
        executor = ScriptedExecutor({"DUP": [_success()], "OTHER": [_success()]})
        result = self._orchestrator(executor).run_batch(
            (
                ImportRequest("DUP", duplicate_sql, True),
                ImportRequest("OTHER", other_sql, True),
                ImportRequest("DUP", duplicate_sql, True),
            ),
            batch_id="duplicate",
        )
        # Stable first occurrence wins; the duplicate does not block OTHER.
        self.assertEqual(executor.calls, ["DUP", "OTHER"])
        self.assertEqual([item.order_number for item in result.results], ["DUP", "OTHER"])
        self.assertEqual(result.deduplicated_orders, ("DUP",))
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["deduplicated_orders"], ["DUP"])
        self.assertEqual(len(report["results"]), 2)

    def test_corrupt_state_file_blocks_resume(self) -> None:
        self.state.write_text('{"schema":"wrong"}', encoding="utf-8")
        with self.assertRaises(ImportOrchestrationError):
            self._orchestrator(ScriptedExecutor({}))


if __name__ == "__main__":
    unittest.main()
