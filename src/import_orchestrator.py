"""WO-08 per-order confirmation and BAT import orchestration.

The module deliberately separates orchestration from execution.  Every order
must be explicitly confirmed, every SQL file is statically revalidated, and
an executor is injected.  ``WindowsBatExecutor`` remains disabled unless its
constructor receives ``authorized=True``; unit tests use a simulated executor
and therefore cannot start BAT, SQL, EXE, or a database connection.
"""

from __future__ import annotations

import json
import locale
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Event
from typing import Protocol, Sequence

from .sql_generator import SqlGenerationError, render_order_bat, validate_generated_sql


STATE_SCHEMA = "iqc-python-wo08/state/v1"
BATCH_SCHEMA = "iqc-python-wo08/batch/v1"
_COMMIT_MARKER = re.compile(
    r"(?mi)^SELECT\s+@wo07_order_number\s+AS\s+log_order_number\s*,\s*"
    r"'COMMITTED'\s+AS\s+log_status\s*;\s*$"
)
_SAFE_BATCH_ID = re.compile(r"[A-Za-z0-9._-]+")
_CMD_META = set("\r\n%&|<>^!")
_MAX_DIAGNOSTIC_CHARS = 4000


class ImportOrchestrationError(ValueError):
    """The WO-08 request, artifact, or persisted state is unsafe."""


class ImportStatus(str, Enum):
    """Auditable state for one order in one batch."""

    NOT_CONFIRMED = "NOT_CONFIRMED"
    CANCELLED_BEFORE_START = "CANCELLED_BEFORE_START"
    COMMITTED = "COMMITTED"
    ROLLED_BACK_RETRYABLE = "ROLLED_BACK_RETRYABLE"
    TIMED_OUT_ROLLED_BACK_RETRYABLE = "TIMED_OUT_ROLLED_BACK_RETRYABLE"
    CANCELLED_ROLLED_BACK_RETRYABLE = "CANCELLED_ROLLED_BACK_RETRYABLE"
    START_FAILED_RETRYABLE = "START_FAILED_RETRYABLE"
    RETRY_CONFIRMATION_REQUIRED = "RETRY_CONFIRMATION_REQUIRED"
    DUPLICATE_BLOCKED = "DUPLICATE_BLOCKED"
    COMMIT_UNVERIFIED_BLOCKED = "COMMIT_UNVERIFIED_BLOCKED"
    ROLLBACK_UNVERIFIED_BLOCKED = "ROLLBACK_UNVERIFIED_BLOCKED"
    TIMED_OUT_ROLLBACK_UNVERIFIED_BLOCKED = "TIMED_OUT_ROLLBACK_UNVERIFIED_BLOCKED"
    CANCELLED_ROLLBACK_UNVERIFIED_BLOCKED = "CANCELLED_ROLLBACK_UNVERIFIED_BLOCKED"
    PREVIOUS_RESULT_UNCERTAIN_BLOCKED = "PREVIOUS_RESULT_UNCERTAIN_BLOCKED"


_RETRYABLE = {
    ImportStatus.ROLLED_BACK_RETRYABLE,
    ImportStatus.TIMED_OUT_ROLLED_BACK_RETRYABLE,
    ImportStatus.CANCELLED_ROLLED_BACK_RETRYABLE,
    ImportStatus.START_FAILED_RETRYABLE,
}
_PERSISTED_TERMINAL = _RETRYABLE | {
    ImportStatus.COMMITTED,
    ImportStatus.COMMIT_UNVERIFIED_BLOCKED,
    ImportStatus.ROLLBACK_UNVERIFIED_BLOCKED,
    ImportStatus.TIMED_OUT_ROLLBACK_UNVERIFIED_BLOCKED,
    ImportStatus.CANCELLED_ROLLBACK_UNVERIFIED_BLOCKED,
}


@dataclass(frozen=True)
class ImportRequest:
    """One manually reviewed order offered to the WO-08 orchestrator."""

    order_number: str
    sql_path: Path
    confirmed: bool
    retry_failed: bool = False
    cancel_before_start: bool = False
    # A previously committed order remains protected by default.  The UI sets
    # this only after the operator has explicitly confirmed the duplicate
    # warning for this import attempt.
    duplicate_confirmed: bool = False


@dataclass(frozen=True)
class BatExecutionResult:
    """Executor evidence; simulation and real adapters share this contract."""

    started: bool
    exit_code: int | None = None
    duration_seconds: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    commit_confirmed: bool = False
    rollback_confirmed: bool = False
    stdout: str = ""
    stderr: str = ""
    detail: str = ""


class BatExecutor(Protocol):
    def execute(
        self,
        runner_bat: Path,
        sql_path: Path,
        timeout_seconds: float,
        cancel_event: Event | None = None,
    ) -> BatExecutionResult:
        """Run exactly one SQL file in one BAT client session."""


@dataclass(frozen=True)
class OrderImportResult:
    order_number: str
    status: ImportStatus
    attempt: int
    confirmed: bool
    retry_requested: bool
    exit_code: int | None
    duration_seconds: float
    bat_started: bool
    diagnostic: str

    @property
    def retryable(self) -> bool:
        return self.status in _RETRYABLE

    def to_dict(self) -> dict[str, object]:
        return {
            "order_number": self.order_number,
            "status": self.status.value,
            "attempt": self.attempt,
            "confirmed": self.confirmed,
            "retry_requested": self.retry_requested,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 6),
            "bat_started": self.bat_started,
            "retryable": self.retryable,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True)
class BatchImportResult:
    batch_id: str
    started_at: str
    finished_at: str
    results: tuple[OrderImportResult, ...]
    report_path: Path | None
    # Input-order duplicate requests are skipped before validation/execution
    # and retained here as an audit-only list.  Cross-batch committed history
    # continues through the separate ``duplicate_confirmed`` gate below.
    deduplicated_orders: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": BATCH_SCHEMA,
            "batch_id": self.batch_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "bat_executor_invoked": any(result.bat_started for result in self.results),
            "deduplicated_orders": list(self.deduplicated_orders),
            "results": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True)
class _HistoryEntry:
    status: ImportStatus
    attempts: int
    batch_id: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "attempts": self.attempts,
            "batch_id": self.batch_id,
            "updated_at": self.updated_at,
        }


def _now() -> str:
    # Import reports are operator-facing; keep local wall-clock time and its
    # offset instead of emitting UTC that appears eight hours early in China.
    return datetime.now().astimezone().isoformat()


def _sanitize(text: object) -> str:
    """Redact connection-looking values and keep diagnostic files bounded."""

    value = "" if text is None else str(text)
    patterns = (
        re.compile(r"(?i)--password(?:=|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s]+)"),
        re.compile(r"(?i)MYSQL_PASSWORD\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\r\n]+)"),
        re.compile(r"(?i)password\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;\r\n]+)"),
    )
    for pattern in patterns:
        value = pattern.sub("[credential redacted]", value)
    return value[:_MAX_DIAGNOSTIC_CHARS]


def _atomic_json_write(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if re.search(r"(?i)(MYSQL_PASSWORD|--password\s*=)", text):
        raise ImportOrchestrationError("状态文件包含未脱敏连接字段")
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        # ``Path.write_text(..., newline=...)`` is unavailable on Python 3.8.
        # Keep the atomic replace behavior while writing deterministic LF JSON.
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ImportOrchestrationError(f"无法写入 WO-08 状态文件：{path}") from exc
    return path


def _read_lossy(path: Path) -> str:
    if not path.is_file():
        return ""
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _decode_process_output(data: bytes) -> str:
    """Decode ``cmd.exe`` output from UTF-8 or a Chinese Windows code page.

    ``cmd.exe`` normally writes using the active OEM code page (CP936 on the
    target Chinese Win7 workstations), while generated artifacts and Python
    logs are UTF-8.  Decoding process output as UTF-8 unconditionally turns a
    useful error such as ``运行时`` into replacement characters, hiding the
    actual BAT failure.  Try UTF-8 first, then the local preferred encoding and
    GB18030, which is compatible with CP936 and covers the target environment.
    """

    if not data:
        return ""
    encodings: list[str] = ["utf-8-sig", "utf-8"]
    preferred = locale.getpreferredencoding(False)
    if preferred:
        encodings.append(preferred)
    encodings.append("gb18030")
    seen: set[str] = set()
    for encoding in encodings:
        normalized = encoding.lower().replace("_", "-")
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode(preferred or "utf-8", errors="replace")


def _paths_are_cmd_safe(*paths: Path) -> bool:
    return all(not any(character in _CMD_META for character in str(path)) for path in paths)


class WindowsBatExecutor:
    """Explicitly authorized Windows BAT adapter.

    Merely constructing the orchestrator does not grant execution authority.
    A caller must opt in with ``authorized=True`` for each application run.
    Rollback is never guessed from a non-zero exit; real rollback evidence must
    be supplied by a future database-aware verifier.
    """

    def __init__(self, *, authorized: bool = False) -> None:
        self.authorized = authorized

    def execute(
        self,
        runner_bat: Path,
        sql_path: Path,
        timeout_seconds: float,
        cancel_event: Event | None = None,
    ) -> BatExecutionResult:
        if not self.authorized:
            return BatExecutionResult(started=False, detail="真实 BAT 执行未获得本次显式授权")
        if os.name != "nt":
            return BatExecutionResult(started=False, detail="真实 BAT 仅允许在 Windows 上执行")
        runner = Path(runner_bat).resolve()
        sql_file = Path(sql_path).resolve()
        if not runner.is_file() or runner.suffix.lower() != ".bat":
            return BatExecutionResult(started=False, detail="导入 BAT 不存在或扩展名错误")
        if not sql_file.is_file() or sql_file.suffix.lower() != ".sql":
            return BatExecutionResult(started=False, detail="单号 SQL 不存在或扩展名错误")
        if not _paths_are_cmd_safe(runner, sql_file):
            return BatExecutionResult(started=False, detail="BAT/SQL 路径包含 CMD 元字符")

        command = f'call "{runner}" "{sql_file}"'
        # Pass one exact command line to cmd.exe.  Supplying ``/c`` and the
        # command as a list item makes subprocess.list2cmdline escape the
        # embedded quotes as ``\"``; cmd.exe treats those backslashes as part
        # of the path, producing the observed ``\"D:\\...\\run_generated_sql.bat\"``
        # error.  Keeping the command text intact preserves normal BAT quoting
        # for paths containing spaces or Chinese characters.
        comspec = os.environ.get("ComSpec", "cmd.exe")
        command_line = f"{subprocess.list2cmdline([comspec])} /d /s /c {command}"
        started_at = time.monotonic()
        try:
            process = subprocess.Popen(
                command_line,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            return BatExecutionResult(started=False, detail=_sanitize(exc))

        timed_out = False
        cancelled = False
        while process.poll() is None:
            elapsed = time.monotonic() - started_at
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                self._terminate_tree(process)
                break
            if elapsed >= timeout_seconds:
                timed_out = True
                self._terminate_tree(process)
                break
            time.sleep(0.05)
        stdout_bytes, stderr_bytes = process.communicate()
        duration = time.monotonic() - started_at
        stdout = _decode_process_output(stdout_bytes)
        stderr = _decode_process_output(stderr_bytes)
        log_text = _read_lossy(sql_file.with_suffix(sql_file.suffix + ".log"))
        commit_confirmed = bool(
            process.returncode == 0
            and not timed_out
            and not cancelled
            and re.search(r"(?m)^.*\tCOMMITTED\s*$", log_text)
        )
        diagnostic = _sanitize("\n".join(part for part in (stdout, stderr, log_text) if part))
        return BatExecutionResult(
            started=True,
            exit_code=process.returncode,
            duration_seconds=duration,
            timed_out=timed_out,
            cancelled=cancelled,
            commit_confirmed=commit_confirmed,
            rollback_confirmed=False,
            stdout=stdout,
            stderr=stderr,
            detail=diagnostic,
        )

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass


class ImportOrchestrator:
    """Run confirmed orders serially and persist their latest safe state."""

    def __init__(
        self,
        runner_bat: Path,
        executor: BatExecutor,
        *,
        state_path: Path | None = None,
        report_dir: Path | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ImportOrchestrationError("timeout_seconds 必须是有限正数") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ImportOrchestrationError("timeout_seconds 必须大于 0")
        self.runner_bat = Path(runner_bat)
        if not self.runner_bat.is_file() or self.runner_bat.suffix.lower() != ".bat":
            raise ImportOrchestrationError(f"固定导入 BAT 不存在或扩展名错误：{self.runner_bat}")
        self.executor = executor
        self.state_path = Path(state_path) if state_path is not None else None
        self.report_dir = Path(report_dir) if report_dir is not None else None
        self.timeout_seconds = timeout
        self._history = self._load_history()

    def history_status(self, order_number: str) -> ImportStatus | None:
        """Return the persisted status for ``order_number`` without changing it.

        The desktop UI can regenerate an order after a restart, so its current
        row status is not sufficient to decide whether the next import is a
        duplicate.  Exposing this read-only lookup keeps that decision tied to
        the same history that the orchestrator enforces before starting BAT.
        """

        entry = self._history.get(str(order_number).strip())
        return entry.status if entry is not None else None

    def is_committed(self, order_number: str) -> bool:
        """Whether history proves that this order was already committed."""

        return self.history_status(order_number) is ImportStatus.COMMITTED

    def run_batch(
        self,
        requests: Sequence[ImportRequest],
        *,
        batch_id: str | None = None,
        cancel_event: Event | None = None,
    ) -> BatchImportResult:
        batch = batch_id or datetime.now().strftime("%Y%m%dT%H%M%S%f")
        if not _SAFE_BATCH_ID.fullmatch(batch):
            raise ImportOrchestrationError("batch_id 只能包含字母、数字、点、下划线和连字符")
        # De-duplicate only within this call, preserving the first request's
        # stable input position.  This is intentionally separate from the
        # persisted-history duplicate confirmation gate in ``_run_one``:
        # skipped requests are audit metadata, not blocked results, and the
        # remaining orders continue through the normal serial workflow.
        unique_requests: list[ImportRequest] = []
        deduplicated_orders: list[str] = []
        seen_orders: set[str] = set()
        for request in requests:
            if request.order_number in seen_orders:
                deduplicated_orders.append(request.order_number)
                continue
            seen_orders.add(request.order_number)
            unique_requests.append(request)
        for request in unique_requests:
            self._validate_request_shape(request)
            self._validate_sql_artifact(request)

        started_at = _now()
        results: list[OrderImportResult] = []
        for request in unique_requests:
            result = self._run_one(request, batch, cancel_event)
            results.append(result)
            if result.status in _PERSISTED_TERMINAL:
                self._history[request.order_number] = _HistoryEntry(
                    status=result.status,
                    attempts=result.attempt,
                    batch_id=batch,
                    updated_at=_now(),
                )
                self._write_history()

        finished_at = _now()
        report_path = self.report_dir / f"iqc_import_batch_{batch}.json" if self.report_dir else None
        batch_result = BatchImportResult(
            batch,
            started_at,
            finished_at,
            tuple(results),
            report_path,
            tuple(deduplicated_orders),
        )
        if report_path is not None:
            _atomic_json_write(report_path, batch_result.to_dict())
        return batch_result

    def _run_one(
        self,
        request: ImportRequest,
        batch_id: str,
        cancel_event: Event | None,
    ) -> OrderImportResult:
        previous = self._history.get(request.order_number)
        prior_attempts = previous.attempts if previous else 0
        if not request.confirmed:
            return self._result(request, ImportStatus.NOT_CONFIRMED, prior_attempts, "未人工确认，未调用 BAT")
        if request.cancel_before_start or (cancel_event is not None and cancel_event.is_set()):
            return self._result(
                request,
                ImportStatus.CANCELLED_BEFORE_START,
                prior_attempts,
                "用户在启动前取消，未调用 BAT",
            )
        duplicate_confirmed = False
        if previous is not None:
            if previous.status is ImportStatus.COMMITTED:
                if not request.duplicate_confirmed:
                    return self._result(
                        request,
                        ImportStatus.DUPLICATE_BLOCKED,
                        prior_attempts,
                        "检测到本次为重复导入；未获得用户确认，未调用 BAT",
                    )
                # Keep the duplicate decision explicit and auditable while
                # allowing the normal executor/commit evidence gates below to
                # remain unchanged.
                duplicate_confirmed = True
            elif previous.status in _RETRYABLE and not request.retry_failed:
                return self._result(
                    request,
                    ImportStatus.RETRY_CONFIRMATION_REQUIRED,
                    prior_attempts,
                    "上次已安全回滚/未启动；必须显式确认重导",
                )
            elif previous.status not in _RETRYABLE:
                return self._result(
                    request,
                    ImportStatus.PREVIOUS_RESULT_UNCERTAIN_BLOCKED,
                    prior_attempts,
                    "上次提交或回滚状态不确定，禁止自动重导",
                )

        sql_path = Path(request.sql_path)
        execution = self.executor.execute(
            self.runner_bat,
            sql_path,
            self.timeout_seconds,
            cancel_event,
        )
        attempt = prior_attempts + (1 if execution.started else 0)
        # The real executor stores the combined, sanitized process/log output
        # in ``detail`` and also exposes the decoded streams separately for
        # callers that need them.  Prefer the combined detail here so the UI
        # and persisted report do not print the same BAT error three times.
        diagnostic = _sanitize(
            execution.detail
            or "\n".join(part for part in (execution.stdout, execution.stderr) if part)
        )
        if duplicate_confirmed:
            diagnostic = _sanitize(f"用户已确认本次为重复导入；{diagnostic or '已进入执行'}")
        if not execution.started:
            return self._result(
                request,
                ImportStatus.START_FAILED_RETRYABLE,
                prior_attempts,
                diagnostic or "BAT 未启动，数据库未接触",
                execution,
            )
        if execution.cancelled:
            status = (
                ImportStatus.CANCELLED_ROLLED_BACK_RETRYABLE
                if execution.rollback_confirmed
                else ImportStatus.CANCELLED_ROLLBACK_UNVERIFIED_BLOCKED
            )
        elif execution.timed_out:
            status = (
                ImportStatus.TIMED_OUT_ROLLED_BACK_RETRYABLE
                if execution.rollback_confirmed
                else ImportStatus.TIMED_OUT_ROLLBACK_UNVERIFIED_BLOCKED
            )
        elif execution.exit_code == 0:
            status = ImportStatus.COMMITTED if execution.commit_confirmed else ImportStatus.COMMIT_UNVERIFIED_BLOCKED
        else:
            status = (
                ImportStatus.ROLLED_BACK_RETRYABLE
                if execution.rollback_confirmed
                else ImportStatus.ROLLBACK_UNVERIFIED_BLOCKED
            )
        return self._result(request, status, attempt, diagnostic or status.value, execution)

    @staticmethod
    def _result(
        request: ImportRequest,
        status: ImportStatus,
        attempt: int,
        diagnostic: str,
        execution: BatExecutionResult | None = None,
    ) -> OrderImportResult:
        return OrderImportResult(
            order_number=request.order_number,
            status=status,
            attempt=attempt,
            confirmed=request.confirmed,
            retry_requested=request.retry_failed,
            exit_code=execution.exit_code if execution else None,
            duration_seconds=execution.duration_seconds if execution else 0.0,
            bat_started=bool(execution and execution.started),
            diagnostic=_sanitize(diagnostic),
        )

    @staticmethod
    def _validate_request_shape(request: ImportRequest) -> None:
        if any(
            type(value) is not bool
            for value in (
                request.confirmed,
                request.retry_failed,
                request.cancel_before_start,
                request.duplicate_confirmed,
            )
        ):
            raise ImportOrchestrationError("确认、重导、重复导入确认和取消标志必须是布尔值")
        try:
            render_order_bat(request.order_number)
        except SqlGenerationError as exc:
            raise ImportOrchestrationError(f"送检单号不安全：{request.order_number!r}") from exc
        sql_path = Path(request.sql_path)
        expected = f"iqc_draft_{request.order_number}.sql"
        if sql_path.name != expected:
            raise ImportOrchestrationError(f"SQL 文件名与送检单号不一致：应为 {expected}")
        if not sql_path.is_file():
            raise ImportOrchestrationError(f"SQL 文件不存在：{sql_path}")

    @staticmethod
    def _validate_sql_artifact(request: ImportRequest) -> None:
        sql_path = Path(request.sql_path)
        try:
            sql_text = sql_path.read_text(encoding="utf-8")
            validate_generated_sql(sql_text)
        except (OSError, UnicodeError, SqlGenerationError) as exc:
            raise ImportOrchestrationError(f"单号 {request.order_number} 的 SQL 静态复核失败：{exc}") from exc
        if not _COMMIT_MARKER.search(sql_text):
            raise ImportOrchestrationError(
                f"单号 {request.order_number} 的 SQL 缺少 WO-08 COMMITTED 运行标记"
            )

    def _load_history(self) -> dict[str, _HistoryEntry]:
        if self.state_path is None or not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if payload.get("schema") != STATE_SCHEMA or not isinstance(payload.get("orders"), dict):
                raise ValueError("schema/orders 不匹配")
            history: dict[str, _HistoryEntry] = {}
            for order_number, item in payload["orders"].items():
                status = ImportStatus(item["status"])
                if status not in _PERSISTED_TERMINAL:
                    raise ValueError(f"不可持久化状态：{status.value}")
                history[str(order_number)] = _HistoryEntry(
                    status=status,
                    attempts=int(item["attempts"]),
                    batch_id=str(item["batch_id"]),
                    updated_at=str(item["updated_at"]),
                )
            return history
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ImportOrchestrationError(f"WO-08 状态文件损坏，拒绝继续导入：{self.state_path}") from exc

    def _write_history(self) -> None:
        if self.state_path is None:
            return
        _atomic_json_write(
            self.state_path,
            {
                "schema": STATE_SCHEMA,
                "updated_at": _now(),
                "orders": {key: value.to_dict() for key, value in sorted(self._history.items())},
            },
        )
