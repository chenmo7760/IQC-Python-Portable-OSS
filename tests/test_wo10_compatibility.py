from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.audit_logger import AuditLogger
from src.import_orchestrator import BatExecutionResult, ImportOrchestrator, ImportRequest
from src.security_scan import assert_no_credentials, scan_paths
from src.sql_generator import SqlContextRow, SqlOrder, SqlRecord, generate_order_artifacts


def _order(order_number: str) -> SqlOrder:
    return SqlOrder(
        order_number,
        (
            SqlRecord(
                item_code=f"M-{order_number}",
                item_name="WO-10 兼容性物料",
                quantity=1,
                supplier="兼容性供应商",
                procedure_name="兼容性检查单",
                procedure_model_id=42,
                engineer_id=7,
                engineer_name="合成工程师02",
                engineer_dept_id="10",
                trigger_basis=order_number,
                check_number=None,
                contexts=(SqlContextRow("1", "外观", "无损伤"),),
            ),
        ),
    )


class SuccessExecutor:
    def execute(self, runner_bat, sql_path, timeout_seconds, cancel_event=None):
        return BatExecutionResult(
            started=True,
            exit_code=0,
            commit_confirmed=True,
            duration_seconds=0.01,
            detail="中文路径模拟提交",
        )


class Wo10CompatibilityTests(unittest.TestCase):
    def test_chinese_paths_work_for_artifacts_state_and_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "质检兼容性" / "WO-10 中文目录"
            artifacts = generate_order_artifacts(_order("中文-A"), root / "输出")
            self.assertTrue(artifacts.sql_path.is_file())
            log = AuditLogger(root / "日志" / "运行.jsonl")
            log.event("artifact_ready", order_number="中文-A", detail="中文路径")
            log.close()
            state_path = root / "状态" / "state.json"
            runtime = root / "运行时"
            runtime.mkdir(parents=True, exist_ok=True)
            # The fixed runner is a placeholder for simulation only; no BAT is started.
            (runtime / "run_generated_sql.bat").write_text("@echo off\n", encoding="utf-8")
            importer = ImportOrchestrator(
                runtime / "run_generated_sql.bat",
                SuccessExecutor(),
                state_path=state_path,
                report_dir=root / "报告",
                timeout_seconds=5,
            )
            result = importer.run_batch(
                (ImportRequest("中文-A", artifacts.sql_path, confirmed=True),),
                batch_id="CN_BATCH",
            )
            self.assertEqual(result.results[0].status.value, "COMMITTED")
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["orders"]["中文-A"]["status"], "COMMITTED")
            self.assertEqual(len(log.tail()), 1)

    def test_writer_and_logger_release_handles_for_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "中文文件占用"
            artifacts = generate_order_artifacts(_order("HANDLE"), root)
            moved = root / "iqc_draft_HANDLE.moved.sql"
            os.replace(artifacts.sql_path, moved)
            os.replace(moved, artifacts.sql_path)
            logger = AuditLogger(root / "events.jsonl")
            logger.event("close_probe", detail="handle release")
            logger.close()
            moved_log = root / "events.moved.jsonl"
            os.replace(logger.path, moved_log)
            os.replace(moved_log, logger.path)

    def test_credential_scan_reports_findings_without_returning_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            clean = root / "clean.txt"
            clean.write_text("COMMITTED\nno connection config\n", encoding="utf-8")
            self.assertEqual(scan_paths((clean,)), ())
            dirty = root / "dirty.bat"
            dirty.write_text('set "MYSQL_PASSWORD=secret-value"\n', encoding="utf-8")
            findings = scan_paths((dirty,))
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].kind, "MYSQL_PASSWORD_ASSIGNMENT")
            self.assertNotIn("secret-value", repr(findings))
            with self.assertRaises(ValueError):
                assert_no_credentials((dirty,))
            wrapper = root / "wrapper.bat"
            wrapper.write_text('mysql.exe --password="%MYSQL_PASSWORD%"\n', encoding="utf-8")
            self.assertEqual(scan_paths((wrapper,)), ())

    def test_scan_generated_wo07_wo08_artifacts_excluding_user_config(self) -> None:
        root = Path(__file__).resolve().parents[1]
        excluded = (root / "运行时" / "db" / "mysql_connection.bat",)
        findings = scan_paths(
            (root / "src", root / "build" / "wo07", root / "build" / "wo08", root / "README_运行说明.md"),
            excluded=excluded,
        )
        self.assertEqual(findings, ())

    def test_unauthorized_real_executor_remains_process_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = root / "run_generated_sql.bat"
            sql = root / "iqc_draft_A.sql"
            runner.write_text("@echo off\n", encoding="utf-8")
            sql.write_text("SELECT 1;\n", encoding="utf-8")
            result = __import__("src.import_orchestrator", fromlist=["WindowsBatExecutor"]).WindowsBatExecutor(
                authorized=False
            ).execute(runner, sql, 1)
            self.assertFalse(result.started)
            self.assertIsNone(result.exit_code)


if __name__ == "__main__":
    unittest.main()
