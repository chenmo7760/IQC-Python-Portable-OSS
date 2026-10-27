"""Small append-only, credential-redacted JSONL logger for WO-09."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from threading import RLock


_SECRET_PATTERNS = (
    # HTTP credentials are accepted only through runtime injection.  Redact
    # both conventional Authorization headers and assignment/JSON forms used
    # by error objects before anything reaches UI text or JSONL.
    re.compile(
        r"(?i)\bauthorization\s*[:=]\s*(?:(?:JWT|Bearer)\s+)?"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;\r\n]+)"
    ),
    re.compile(
        r"(?i)\b(?:token|access_token|refresh_token|cookie|set-cookie|secret)\b"
        r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;\r\n]+)"
    ),
    re.compile(r"(?i)--password(?:=|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s]+)"),
    re.compile(r"(?i)MYSQL_PASSWORD\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\r\n]+)"),
    re.compile(r"(?i)password\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;\r\n]+)"),
    # Provider exceptions must not expose a DSN, host, user or port in the
    # UI/JSONL stage diagnostics. Keep the key/phase while replacing values.
    re.compile(r"(?i)\b(?:mysql|mariadb|postgres(?:ql)?|oracle|sqlserver)://[^\s]+"),
    re.compile(r"(?i)(?:--)?(?:host|hostname|server|user(?:name)?|port|database|db)\s*(?:=|:)\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"),
    re.compile(r"(?i)--(?:host|user|port|database)\s+(?:\"[^\"]*\"|'[^']*'|[^\s]+)"),
)
_MAX_DETAIL = 4000


class AuditLogError(ValueError):
    pass


def redact_log_text(value: object) -> str:
    text = "" if value is None else str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[credential redacted]", text)
    return text[:_MAX_DETAIL]


class AuditLogger:
    """Thread-safe append-only logger; it never logs connection config."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if self.path.suffix.lower() != ".jsonl":
            raise AuditLogError("WO-09 日志必须使用 .jsonl 扩展名")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._closed = False

    def event(
        self,
        name: str,
        *,
        order_number: str | None = None,
        status: str | None = None,
        detail: str = "",
        **fields: object,
    ) -> None:
        if not name.strip():
            raise AuditLogError("日志事件名称不能为空")
        payload: dict[str, object] = {
            # Use the host's local timezone for the operator-facing log.  The
            # offset is retained in JSON so records remain unambiguous while
            # the Tk view shows the same wall-clock time as the workstation.
            "timestamp": datetime.now().astimezone().isoformat(),
            "event": redact_log_text(name),
            "order_number": redact_log_text(order_number) if order_number is not None else None,
            "status": redact_log_text(status) if status is not None else None,
            "detail": redact_log_text(detail),
            "fields": {str(key): redact_log_text(value) for key, value in fields.items()},
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if re.search(
            r"(?i)(MYSQL_PASSWORD|--password\s*=|\bauthorization\s*[:=]|"
            r"\b(?:token|access_token|refresh_token|cookie|set-cookie|secret)\b\s*[:=])",
            line,
        ):
            raise AuditLogError("日志脱敏失败，拒绝写入")
        with self._lock:
            if self._closed:
                raise AuditLogError("日志已关闭")
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
                stream.flush()

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def clear(self) -> None:
        """Clear persisted log lines so the next run starts cleanly."""

        with self._lock:
            if self._closed:
                raise AuditLogError("日志已关闭")
            try:
                self.path.write_text("", encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise AuditLogError(f"日志清空失败：{exc}") from exc

    def tail(self, limit: int = 100) -> list[str]:
        if limit <= 0:
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return []
        return lines[-limit:]
