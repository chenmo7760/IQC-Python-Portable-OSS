from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.audit_logger import AuditLogError, AuditLogger, redact_log_text


class AuditLoggerTests(unittest.TestCase):
    def test_jsonl_is_append_only_bounded_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "iqc_ui.jsonl"
            logger = AuditLogger(path)
            logger.event(
                "import_result",
                order_number="A",
                status="已提交",
                detail='--password="secret" MYSQL_PASSWORD=abc password:\'xyz\'',
                exit_code=0,
            )
            logger.event("state_change", order_number="B", detail="x" * 5000)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            logged_at = datetime.fromisoformat(first["timestamp"])
            self.assertEqual(logged_at.utcoffset(), datetime.now().astimezone().utcoffset())
            self.assertNotIn("secret", lines[0])
            self.assertNotIn("abc", lines[0])
            self.assertNotIn("xyz", lines[0])
            self.assertIn("credential redacted", first["detail"])
            self.assertLessEqual(len(first["detail"]), 4000)
            self.assertEqual(len(logger.tail(1)), 1)
            logger.close()
            with self.assertRaises(AuditLogError):
                logger.event("after_close")

    def test_path_and_event_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(AuditLogError):
                AuditLogger(Path(temp) / "ui.log")
            logger = AuditLogger(Path(temp) / "ui.jsonl")
            with self.assertRaises(AuditLogError):
                logger.event("   ")
            logger.close()

    def test_clear_removes_persisted_history_for_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "iqc_ui.jsonl"
            logger = AuditLogger(path)
            logger.event("old_event", detail="old")
            self.assertEqual(len(logger.tail()), 1)
            logger.clear()
            self.assertEqual(path.read_text(encoding="utf-8"), "")
            self.assertEqual(logger.tail(), [])
            logger.event("new_event", detail="new")
            self.assertIn("new_event", logger.tail()[0])
            self.assertNotIn("old_event", path.read_text(encoding="utf-8"))
            logger.close()

    def test_redact_helper_handles_quoted_values(self) -> None:
        result = redact_log_text('--password="quoted" password:\'quoted2\'')
        self.assertNotIn("quoted", result)
        self.assertIn("credential redacted", result)

    def test_http_authorization_token_cookie_and_secret_are_redacted(self) -> None:
        raw = (
            "Authorization: JWT jwt-value "
            "authorization=Bearer bearer-value "
            "token=token-value cookie='cookie-value' secret:\"secret-value\""
        )
        result = redact_log_text(raw)
        for sensitive in (
            "jwt-value",
            "bearer-value",
            "token-value",
            "cookie-value",
            "secret-value",
        ):
            self.assertNotIn(sensitive, result)
        self.assertGreaterEqual(result.count("credential redacted"), 5)

    def test_http_credentials_never_reach_jsonl_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ui.jsonl"
            logger = AuditLogger(path)
            logger.event(
                "identity_error",
                detail="Authorization: Bearer highly-secret",
                response="token=response-token; Cookie=session-cookie",
            )
            persisted = path.read_text(encoding="utf-8")
            self.assertNotIn("highly-secret", persisted)
            self.assertNotIn("response-token", persisted)
            self.assertNotIn("session-cookie", persisted)
            self.assertIn("credential redacted", persisted)


if __name__ == "__main__":
    unittest.main()
