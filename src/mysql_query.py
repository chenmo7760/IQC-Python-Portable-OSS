from __future__ import annotations

import hashlib
import locale
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .audit_logger import AuditLogger, redact_log_text
from .models import Engineer, normalize_procedure_name


class MySqlQueryError(RuntimeError):
    """只读查询或人员解析失败。"""

    def __init__(
        self,
        message: str,
        *,
        query_stage: str | None = None,
        exit_code: int | None = None,
        safe_detail: str = "",
    ) -> None:
        super().__init__(message)
        self.query_stage = query_stage
        self.exit_code = exit_code
        self.safe_detail = redact_log_text(safe_detail)


class ModelMappingError(MySqlQueryError):
    """只读结果行字段数或业务候选不符合预期。"""


ATTACHED_SQL_NULL_SENTINEL = "__IQC_SQL_NULL__"


READONLY_SQL = {
    "procedure_models": (
        "SELECT id, procedure_name, procedure_code, apply_range, check_plan, accept_standard, "
        "is_lasted_version, is_frozen, processstate "
        "FROM qcauto_procedure_model;"
    ),
    "procedure_contexts": (
        "SELECT procedure_model_id, context_number, inspection_items, inspection_standards "
        "FROM qcauto_procedure_model_check_context "
        "ORDER BY CAST(context_number AS UNSIGNED), context_number;"
    ),
    "attached_models": (
        "SELECT id, procedure_model_id, "
        f"IF(inspection_item1 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', inspection_item1) AS inspection_item1, "
        f"IF(inspection_item2 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', inspection_item2) AS inspection_item2, "
        f"IF(inspection_item3 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', inspection_item3) AS inspection_item3, "
        f"IF(inspection_item4 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', inspection_item4) AS inspection_item4, "
        f"IF(inspection_item5 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', inspection_item5) AS inspection_item5, "
        f"IF(inspection_item6 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', inspection_item6) AS inspection_item6, "
        f"IF(inspection_item7 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', inspection_item7) AS inspection_item7, "
        f"IF(inspection_item8 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', inspection_item8) AS inspection_item8, "
        f"IF(inspection_item9 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', inspection_item9) AS inspection_item9, "
        f"IF(standard_range1 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', standard_range1) AS standard_range1, "
        f"IF(standard_range2 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', standard_range2) AS standard_range2, "
        f"IF(standard_range3 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', standard_range3) AS standard_range3, "
        f"IF(standard_range4 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', standard_range4) AS standard_range4, "
        f"IF(standard_range5 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', standard_range5) AS standard_range5, "
        f"IF(standard_range6 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', standard_range6) AS standard_range6, "
        f"IF(standard_range7 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', standard_range7) AS standard_range7, "
        f"IF(standard_range8 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', standard_range8) AS standard_range8, "
        f"IF(standard_range9 IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', standard_range9) AS standard_range9, "
        f"IF(attached_table_name IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', attached_table_name) AS attached_table_name, "
        f"IF(notes IS NULL, '{ATTACHED_SQL_NULL_SENTINEL}', notes) AS notes "
        "FROM qcauto_entry_attached_table_model ORDER BY id;"
    ),
    "active_users": (
        "SELECT id, name, username, work_number, dept_id "
        "FROM dvadmin_system_users WHERE is_active = 1 "
        "AND TRIM(COALESCE(name, '')) <> '' AND dept_id IS NOT NULL "
        "ORDER BY name, id;"
    ),
}


def validate_readonly_sql(sql: str) -> str:
    """仅允许单条 SELECT/WITH 查询，防止 WO-04 误变成 DML。"""

    normalized = sql.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    upper = normalized.upper()
    if not (upper.startswith("SELECT ") or upper.startswith("WITH ")):
        raise MySqlQueryError("只读查询只允许 SELECT 或 WITH")
    forbidden = ("INSERT", "UPDATE", "DELETE", "REPLACE", "ALTER", "DROP", "TRUNCATE", "CREATE", "GRANT")
    # Match complete SQL keyword tokens.  A substring check incorrectly rejects
    # ordinary read-only columns such as ``create_datetime`` and
    # ``last_update_time``.  Word boundaries still reject the actual DML/DDL
    # keywords in any case, including writable CTE bodies.
    forbidden_pattern = r"\b(?:" + "|".join(forbidden) + r")\b"
    if re.search(forbidden_pattern, upper):
        raise MySqlQueryError("只读查询包含禁止的 DML/DDL 关键字")
    if ";" in normalized:
        raise MySqlQueryError("只读查询不允许多条 SQL")
    return normalized + ";"


@dataclass(frozen=True)
class ActiveUser:
    user_id: str
    name: str
    username: str
    work_number: str
    dept_id: str


@dataclass(frozen=True)
class ProcedureModel:
    id: str
    procedure_name: str
    check_plan: str
    accept_standard: str
    is_lasted_version: str
    is_frozen: str
    processstate: str
    # QA1 关键零部件匹配所需字段。追加默认值保持旧版七列快照和位置
    # 构造调用兼容；运行时新查询返回九列并填充这两个字段。
    procedure_code: str = ""
    apply_range: str = ""


@dataclass(frozen=True)
class ProcedureContext:
    procedure_model_id: str
    context_number: str
    inspection_items: str
    inspection_standards: str


@dataclass(frozen=True)
class AttachedModel:
    id: str
    procedure_model_id: str
    # The eighteen slot values intentionally remain nullable at the snapshot
    # boundary.  ``None`` means the source carried a SQL NULL marker; ``""``
    # remains an actual empty string.  The generator owns the later display
    # conversion to ``NA`` and does not touch notes/check_number/conclusions.
    inspection_items: tuple[str | None, ...]
    standard_ranges: tuple[str | None, ...]
    # auto-factory 的附表模板还保存名称和备注。旧版快照只有 20 列，
    # 因此这两个字段保留默认值以兼容历史测试夹具和离线快照。
    attached_table_name: str | None = ""
    notes: str | None = ""
    # Raw source/provenance is retained for audit and regression fixtures.  It
    # is deliberately additive so old 20-column snapshots and positional
    # constructors remain valid.
    raw_inspection_items: tuple[str | None, ...] = field(default=(), compare=False, repr=False)
    raw_standard_ranges: tuple[str | None, ...] = field(default=(), compare=False, repr=False)
    null_provenance: tuple[bool, ...] = field(default=(), compare=False, repr=False)
    null_source_columns: tuple[int, ...] = field(default=(), compare=False)
    metadata_null_provenance: tuple[bool, bool] = field(default=(False, False), compare=False, repr=False)
    raw_attached_table_name: str | None = field(default="", compare=False, repr=False)
    raw_notes: str | None = field(default="", compare=False, repr=False)
    raw_tsv: bytes = field(default=b"", compare=False, repr=False)
    raw_tsv_row: bytes = field(default=b"", compare=False, repr=False)
    source_row_number: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ReadonlySnapshot:
    procedure_models: tuple[ProcedureModel, ...]
    procedure_contexts: tuple[ProcedureContext, ...]
    attached_models: tuple[AttachedModel, ...]
    active_users: tuple[ActiveUser, ...]


# 只读查询阶段的可选诊断回调。回调接收安全的结构化字段；实现方不得把
# 连接配置、SQL 内容或人员 ID 写入事件。使用 Mapping 而不是 GenerationEvent
# 可保持 WO-04 查询层独立，GenerationService 再将其转换为统一事件。
QueryEventCallback = Callable[[Mapping[str, object]], None]


def _diagnostic_bytes(value: object) -> bytes:
    """将进程输出转换成可哈希的字节，不把内容写入诊断日志。"""

    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return str(value).encode("utf-8", errors="replace")


def _decode_process_output(value: object) -> tuple[str, str]:
    """解码 Windows BAT/客户端输出，并返回实际采用的编码标签。

    旧实现把所有输出固定按 UTF-8 解码，Windows 中文代码页下会产生
    ``���``。这里保留原始字节并按无损优先级尝试，失败时才使用替换字符。
    """

    if value is None:
        return "", "none"
    if isinstance(value, str):
        # 测试 runner 可能已经完成解码；真实 subprocess 使用 bytes。
        return value, "runner_text"
    raw = _diagnostic_bytes(value)
    encodings: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", locale.getpreferredencoding(False), "gb18030"):
        if encoding and encoding.lower() not in {item.lower() for item in encodings}:
            encodings.append(encoding)
    for encoding in encodings:
        try:
            return raw.decode(encoding), encoding.lower()
        except UnicodeDecodeError:
            continue
    fallback = encodings[-1] if encodings else "utf-8"
    return raw.decode(fallback, errors="replace"), f"{fallback.lower()}-replace"


def _file_diagnostic_metadata(path: Path) -> dict[str, object]:
    """只记录文件可追溯元数据，不读取或保存 MySQL 配置正文。"""

    metadata: dict[str, object] = {"path": str(path), "exists": False}
    try:
        if not path.is_file():
            return metadata
        raw = path.read_bytes()
    except OSError as exc:
        metadata["exists"] = True
        metadata["read_error"] = type(exc).__name__
        return metadata
    metadata.update(
        {
            "exists": True,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    )
    return metadata


def _stream_diagnostic_metadata(value: object) -> tuple[str, dict[str, object]]:
    """返回脱敏前仅用于异常文本的解码结果及其长度/哈希元数据。"""

    text, encoding = _decode_process_output(value)
    raw = _diagnostic_bytes(value)
    return text, {
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "encoding": encoding,
    }


def _readonly_config_diagnostic_metadata(
    readonly_bat: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """说明固定 r44 BAT 使用相邻配置，外部覆盖变量不会生效。

    只记录来源、路径和文件哈希/长度，绝不读取配置文本或输出连接字段。
    """

    source = os.environ if environ is None else environ
    raw_value = source.get("IQC_MYSQL_CONFIG_FILE", "")
    override_present = isinstance(raw_value, str) and bool(raw_value.strip())
    default = readonly_bat.parent / "mysql_connection.bat"
    metadata = _file_diagnostic_metadata(default)
    return {
        "config_source": "package_default",
        "config_override_present": override_present,
        "config_override_valid": False,
        "config_override_supported": False,
        "config_override_ignored": override_present,
        "config_path": metadata.pop("path"),
        "config_exists": metadata.pop("exists"),
        "config_bytes": metadata.pop("bytes", 0),
        "config_sha256": metadata.pop("sha256", ""),
        "config_read_error": metadata.pop("read_error", ""),
    }


def _mysql_client_diagnostic_metadata(
    readonly_bat: Path,
    child_environment: Mapping[str, str],
) -> dict[str, object]:
    """仅记录 bundled 候选；r44 的实际客户端由配置中 MYSQL_EXE 决定。"""

    bundled = readonly_bat.parent / "mysql-client" / "mysql.exe"
    metadata = _file_diagnostic_metadata(bundled)
    return {
        "mysql_client_source": "config_mysql_exe",
        "mysql_client_path": "",
        "mysql_client_exists": "not_evaluated",
        "mysql_client_bytes": 0,
        "mysql_client_sha256": "",
        "mysql_client_version_probe": "not_run_config_not_executed",
        "mysql_client_path_fallback_supported": False,
        "bundled_mysql_candidate_path": metadata.get("path", ""),
        "bundled_mysql_candidate_exists": metadata.get("exists", False),
        "bundled_mysql_candidate_bytes": metadata.get("bytes", 0),
        "bundled_mysql_candidate_sha256": metadata.get("sha256", ""),
    }


def _write_query_diagnostic(
    path: Path | None,
    event: str,
    *,
    status: str,
    detail: str = "",
    fields: Mapping[str, object] | None = None,
) -> None:
    """追加只读查询诊断；日志失败不能改变查询结果。"""

    if path is None:
        return
    try:
        AuditLogger(Path(path)).event(
            event,
            status=status,
            detail=detail,
            **dict(fields or {}),
        )
    except Exception:
        # 诊断文件不可写时仍保持原有查询错误，不让日志链路反向污染业务。
        return


def _readonly_subprocess_environment(
    environ: Mapping[str, str] | None = None,
    *,
    mysql_client_dir: Path | None = None,
) -> dict[str, str]:
    """Match r44: drop the unsupported override and preserve the normal PATH.

    ``mysql_client_dir`` remains accepted for call compatibility. r44 invokes
    the config's ``MYSQL_EXE`` directly, with no bundled/PATH fallback.
    """

    child = dict(os.environ if environ is None else environ)
    child.pop("IQC_MYSQL_CONFIG_FILE", None)
    return child


def _hidden_cmd_window_kwargs() -> dict[str, object]:
    """Hide Windows cmd.exe wrappers while preserving normal stream capture."""

    if os.name != "nt":
        return {}
    kwargs: dict[str, object] = {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags
    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _row_value(row: Sequence[object], index: int) -> str:
    value = row[index] if index < len(row) else ""
    return "" if value is None else str(value).strip()


def map_procedure_model_row(row: Sequence[object]) -> ProcedureModel:
    if len(row) == 7:
        return ProcedureModel(*(_row_value(row, index) for index in range(7)))
    if len(row) == 9:
        # 新查询顺序把 QA1 匹配字段放在名称之后；dataclass 末尾追加
        # procedure_code/apply_range，不能直接按九列位置展开。
        return ProcedureModel(
            id=_row_value(row, 0),
            procedure_name=_row_value(row, 1),
            check_plan=_row_value(row, 4),
            accept_standard=_row_value(row, 5),
            is_lasted_version=_row_value(row, 6),
            is_frozen=_row_value(row, 7),
            processstate=_row_value(row, 8),
            procedure_code=_row_value(row, 2),
            apply_range=_row_value(row, 3),
        )
    raise ModelMappingError(f"qcauto_procedure_model 返回字段数不是 7（旧版）或 9（含 QA1 匹配字段）：{len(row)}")


def map_procedure_context_row(row: Sequence[object]) -> ProcedureContext:
    if len(row) != 4:
        raise ModelMappingError(f"qcauto_procedure_model_check_context 返回字段数不是 4：{len(row)}")
    return ProcedureContext(*(_row_value(row, index) for index in range(4)))


def map_attached_model_row(
    row: Sequence[object],
    *,
    null_provenance: Sequence[bool] | None = None,
    raw_tsv: bytes | str = b"",
    source_row_number: int | None = None,
) -> AttachedModel:
    """Map one attached-template row without erasing slot provenance.

    The attached SELECT serializes SQL ``NULL`` as a dedicated sentinel.  Keep
    that raw spelling and expose it as ``None`` only for the eighteen
    inspection-item/range slots when an explicit raw null mask identifies the
    marker.  Without that provenance a string ``"NULL"`` is a legal literal
    and remains unchanged; ordinary empty strings stay ``""`` and the literal
    ``NA`` stays ``NA``.  Model-source metadata (name/notes) follows the same
    explicit sentinel mask when present and otherwise remains a literal value;
    additive raw/provenance fields do not alter the Draft/Record schema.
    """

    if len(row) not in {20, 22}:
        raise ModelMappingError(
            f"qcauto_entry_attached_table_model 返回字段数不是 20（旧版）或 22（含名称/备注）：{len(row)}"
        )

    def raw_slot(value: object) -> str | None:
        return None if value is None else str(value).strip()

    raw_items = tuple(raw_slot(row[index]) for index in range(2, 11))
    raw_ranges = tuple(raw_slot(row[index]) for index in range(11, 20))
    supplied_mask = tuple(bool(value) for value in null_provenance) if null_provenance is not None else ()
    metadata_mask: tuple[bool, bool] = (False, False)
    if len(supplied_mask) in {20, 22}:
        # ``MysqlTsvRows`` records one mask bit per source column.  Accepting
        # that full-row form here avoids callers having to know the slot slice
        # while still enforcing the canonical eighteen-slot model boundary.
        metadata_mask = (
            tuple(supplied_mask[20:22])  # type: ignore[assignment]
            if len(supplied_mask) == 22
            else (False, False)
        )
        supplied_mask = supplied_mask[2:20]
    if supplied_mask and len(supplied_mask) != 18:
        raise ModelMappingError(f"附表 NULL provenance 必须恰有 18 个槽位：{len(supplied_mask)}")
    # A Python ``None`` already carries null semantics for an injected row; a
    # textual ``NULL`` is converted only when the parser supplied an explicit
    # provenance bit.  This prevents an unannotated, legitimate literal
    # ``"NULL"`` from being silently rewritten.
    slot_mask = supplied_mask or tuple(value is None for value in (*raw_items, *raw_ranges))

    def mapped_slot(value: str | None, is_null: bool) -> str | None:
        return None if is_null else value

    def raw_metadata(value: object) -> str | None:
        # Metadata is model-source evidence; preserve internal/outer whitespace
        # and line breaks instead of applying the slot-oriented ``strip``.
        return None if value is None else str(value)

    raw_name = raw_metadata(row[20]) if len(row) == 22 else ""
    raw_notes = raw_metadata(row[21]) if len(row) == 22 else ""
    mapped_name = None if metadata_mask[0] else raw_name
    mapped_notes = None if metadata_mask[1] else raw_notes

    mapped_items = tuple(mapped_slot(value, slot_mask[index]) for index, value in enumerate(raw_items))
    mapped_ranges = tuple(mapped_slot(value, slot_mask[9 + index]) for index, value in enumerate(raw_ranges))
    raw_source = raw_tsv.encode("utf-8") if isinstance(raw_tsv, str) else bytes(raw_tsv)
    raw_lines = raw_source.splitlines(keepends=True)
    raw_row = (
        raw_lines[source_row_number - 1]
        if source_row_number is not None and source_row_number > 0 and source_row_number <= len(raw_lines)
        else b""
    )
    return AttachedModel(
        id=_row_value(row, 0),
        procedure_model_id=_row_value(row, 1),
        inspection_items=mapped_items,
        standard_ranges=mapped_ranges,
        attached_table_name=mapped_name,
        notes=mapped_notes,
        raw_inspection_items=raw_items,
        raw_standard_ranges=raw_ranges,
        null_provenance=slot_mask,
        null_source_columns=tuple(index + 3 for index, is_null in enumerate(slot_mask) if is_null),
        metadata_null_provenance=metadata_mask,
        raw_attached_table_name=raw_name,
        raw_notes=raw_notes,
        raw_tsv=raw_source,
        raw_tsv_row=raw_row,
        source_row_number=source_row_number,
    )


def map_active_user_row(row: Sequence[object]) -> ActiveUser:
    if len(row) != 5:
        raise ModelMappingError(f"dvadmin_system_users 返回字段数不是 5：{len(row)}")
    return ActiveUser(
        user_id=_row_value(row, 0),
        name=_row_value(row, 1),
        username=_row_value(row, 2),
        work_number=_row_value(row, 3),
        dept_id=_row_value(row, 4),
    )


def select_unique_active_model(
    models: Iterable[ProcedureModel],
    procedure_name: str,
    *,
    diagnostics_callback: Callable[[Mapping[str, object]], None] | None = None,
    prefer_exact: bool = False,
    strict_exact: bool = False,
) -> ProcedureModel:
    """按 VBA 的 ``LIKE '%检查单名称%'`` 语义选择唯一有效模型。

    Windchill 返回可能带有 ``文档编号_`` 前缀，也可能直接返回中文名称；
    两种形式都只映射到数据库中的唯一有效模型，重复或歧义仍然阻断。
    ``prefer_exact`` 仅供已批准的显式别名目标使用：先选完整规范名（兼容
    检查单/检验单），否则维持历史 VBA 子串匹配语义。
    ``strict_exact`` 只用于本次受控 PCM：仅去首尾空白后逐字相等，禁止兼容/包含回退。
    """

    expected_name = str(procedure_name).strip()
    if strict_exact:
        # 独立出口先于所有名称归一化，保留用户确认名称中的内部空格。
        model_rows = list(models)
        name_matches = [model for model in model_rows if expected_name and model.procedure_name.strip() == expected_name]
        active_matches = [
            model for model in name_matches
            if model.is_lasted_version == "1" and model.is_frozen == "0" and model.processstate == "2"
        ]
        _emit_model_match_diagnostics(
            diagnostics_callback,
            {
                "name": "model_match",
                "status": "SUCCESS" if len(active_matches) == 1 else "BLOCKED",
                "detail": "严格精确模板匹配完成" if len(active_matches) == 1 else "严格精确模板未唯一生效，禁止回退",
                "strict_exact": True,
                "expected_name": expected_name,
                "name_match_count": len(name_matches),
                "active_name_match_count": len(active_matches),
                "selected_model_id": str(active_matches[0].id) if len(active_matches) == 1 else "",
                "selected_model_name": active_matches[0].procedure_name if len(active_matches) == 1 else "",
                "matched_model_ids": " | ".join(str(model.id) for model in active_matches),
                "matched_model_names": " | ".join(model.procedure_name for model in active_matches),
            },
        )
        if len(active_matches) != 1:
            raise ModelMappingError(f"严格精确模板未解析为唯一有效候选：{expected_name}，活动匹配数={len(active_matches)}；禁止包含/人工回退")
        return active_matches[0]
    normalized_name = normalize_procedure_name(expected_name)
    lookup_names = {expected_name, normalized_name}
    # VBA 在组装模型匹配条件时会将“检查单”替换为“检验单”，并对
    # 数据库中的 procedure_name 做同样替换。现场 Windchill 与模型表
    # 经常分别使用这两个称呼；匹配键兼容该别名，但输出仍保留源名称。
    lookup_match_names = {_vba_procedure_name_match(name) for name in lookup_names if name}
    model_rows = list(models)
    matches = []
    name_matches_all: list[ProcedureModel] = []
    exact_name_matches_all: list[ProcedureModel] = []
    active_models = []
    for model in model_rows:
        model_name = model.procedure_name.strip()
        normalized_model_name = normalize_procedure_name(model_name)
        model_match_forms = {
            model_name,
            normalized_model_name,
            _vba_procedure_name_match(model_name),
            _vba_procedure_name_match(normalized_model_name),
        }
        # VBA 把源名称作为 LIKE 的右侧模式值拼入两侧百分号。这里用
        # 明确的字面子串判断复现“模型名包含检查单名称”，同时保留
        # Windchill 文档编号前缀/书名号的归一化兼容。
        name_matches = any(
            candidate and candidate in target
            for candidate in lookup_names | lookup_match_names
            for target in model_match_forms
        )
        # A configured target is the current canonical template name.  Prefer
        # an exact normalized/VBA-compatible model name before retaining the
        # legacy VBA substring fallback; this prevents ``光缆类`` from also
        # selecting ``预制光缆类`` while preserving historical suffix matches.
        exact_name_matches = bool(
            lookup_match_names
            & {
                _vba_procedure_name_match(model_name),
                _vba_procedure_name_match(normalized_model_name),
            }
        ) or bool(
            lookup_names
            & {model_name, normalized_model_name}
        )
        if name_matches:
            name_matches_all.append(model)
        if exact_name_matches:
            exact_name_matches_all.append(model)
        is_active = (
            model.is_lasted_version == "1"
            and model.is_frozen == "0"
            and model.processstate == "2"
        )
        if is_active:
            active_models.append(model)
        if name_matches and is_active:
            matches.append(model)
    if prefer_exact and exact_name_matches_all:
        # Exact names, including 检查单/检验单 compatibility, are authoritative
        # for an alias target.  An inactive exact row must not fall through to
        # a different fuzzy candidate.
        matches = [
            model
            for model in exact_name_matches_all
            if model.is_lasted_version == "1"
            and model.is_frozen == "0"
            and model.processstate == "2"
        ]
        name_matches_all = exact_name_matches_all
    _emit_model_match_diagnostics(
        diagnostics_callback,
        {
            "name": "model_match",
            "status": "SUCCESS" if len(matches) == 1 else "ERROR",
            "detail": "检查单模型匹配完成" if len(matches) == 1 else "检查单模型匹配失败",
            "expected_name": expected_name,
            "normalized_name": normalized_name,
            # ``match_key`` 保留归一化后的实际名称，便于现场直接对照数据库；
            # VBA 替换后的兼容键单独记录，避免把比较别名误认为源名称。
            "match_key": normalized_name,
            "vba_alias_match_key": _vba_procedure_name_match(normalized_name),
            "model_count": len(model_rows),
            "active_model_count": len(active_models),
            "name_match_count": len(name_matches_all),
            "active_name_match_count": len(matches),
            "matched_model_names": " | ".join(model.procedure_name for model in name_matches_all[:10]),
            "matched_model_details": " | ".join(
                "%s [is_lasted_version=%s,is_frozen=%s,processstate=%s]"
                % (model.procedure_name, model.is_lasted_version, model.is_frozen, model.processstate)
                for model in name_matches_all[:10]
            ),
        },
    )
    if len(matches) != 1:
        suffix = f"；归一化名称={normalized_name}" if normalized_name != expected_name else ""
        raise ModelMappingError(
            f"检查单模型未解析为唯一有效候选：{expected_name}{suffix}，匹配数={len(matches)}"
        )
    return matches[0]


def _emit_model_match_diagnostics(
    callback: Callable[[Mapping[str, object]], None] | None,
    payload: Mapping[str, object],
) -> None:
    """诊断回调不能改变模型解析结果，也不能阻断生成流程。"""

    if callback is None:
        return
    try:
        callback(payload)
    except Exception:
        return


def _vba_procedure_name_match(value: object) -> str:
    """返回 VBA 模型匹配使用的检查单/检验单兼容键。"""

    return normalize_procedure_name(value).replace("检查单", "检验单")


_MYSQL_BATCH_ESCAPES = {
    # mysql --batch (without --raw) uses C-style escapes for control bytes.
    # Keep this table explicit; unknown escapes are preserved verbatim below.
    "0": "\x00",
    "b": "\b",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "Z": "\x1a",
    "\\": "\\",
    "'": "'",
    '"': '"',
}


def parse_mysql_batch_tsv(text: str) -> list[list[str]]:
    """解析 ``mysql --batch`` 的转义 TSV，保留字段内换行/制表符。"""

    if text.startswith("\ufeff"):
        text = text[1:]

    rows: list[list[str]] = []
    row: list[str] = []
    field: list[str] = []
    record_has_separator = False

    def flush_row() -> None:
        nonlocal row, field, record_has_separator
        row.append("".join(field))
        # Ignore only a truly blank physical line.  A row containing tabs but
        # empty values is still a legitimate result and must be retained.
        if record_has_separator or any(value != "" for value in row):
            rows.append(row)
        row = []
        field = []
        record_has_separator = False

    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            if index + 1 >= len(text):
                # Preserve a dangling literal backslash in malformed fixtures.
                field.append("\\")
                break
            escaped = text[index + 1]
            if escaped == "\r" or escaped == "\n" or escaped == "\t":
                # A physical row/column delimiter is never a valid escaped
                # payload in mysql --batch output.  Keep the slash and let the
                # next loop iteration process the delimiter normally.
                field.append("\\")
            else:
                index += 1
                field.append(_MYSQL_BATCH_ESCAPES.get(escaped, "\\" + escaped))
        elif char == "\t":
            row.append("".join(field))
            field = []
            record_has_separator = True
        elif char == "\r" or char == "\n":
            flush_row()
            # Redirection on Windows normally yields CRLF; count it once.
            if char == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                index += 1
        else:
            field.append(char)
        index += 1

    if field or row:
        flush_row()
    return rows


class MysqlTsvRows(list):
    """List-compatible parsed TSV carrying raw bytes and NULL provenance.

    Existing callers continue to receive and compare a normal list of rows;
    attached-template consumers can opt into the audit metadata without
    changing the read-only BAT result contract.
    """

    def __init__(
        self,
        rows: Sequence[Sequence[str]],
        *,
        raw_tsv: bytes,
        raw_rows: Sequence[str],
        null_provenance: Sequence[Sequence[bool]],
    ) -> None:
        super().__init__([list(row) for row in rows])
        self.raw_tsv = bytes(raw_tsv)
        self.raw_row_bytes = tuple(self.raw_tsv.splitlines(keepends=True))
        self.raw_rows = tuple(str(row) for row in raw_rows)
        self.null_provenance = tuple(tuple(bool(value) for value in row) for row in null_provenance)

    @property
    def rows(self) -> tuple[tuple[str, ...], ...]:
        """Tuple view for callers that prefer an explicit parsed-result field."""

        return tuple(tuple(row) for row in self)


def parse_mysql_batch_tsv_with_provenance(text: str | bytes) -> MysqlTsvRows:
    """Parse mysql batch TSV and retain raw bytes/row and NULL markers.

    The attached-template SELECT uses ``ATTACHED_SQL_NULL_SENTINEL`` for SQL
    NULL values.  Only that sentinel is marked as null provenance; an ordinary
    business literal ``NULL`` remains a literal.  The original bytes and
    physical rows remain available for an audit report.
    """

    raw_bytes = bytes(text) if isinstance(text, bytes) else str(text).encode("utf-8")
    decoded = (
        text.decode("utf-8-sig", errors="replace")
        if isinstance(text, bytes)
        else str(text).lstrip("\ufeff")
    )
    rows = parse_mysql_batch_tsv(decoded)
    provenance = tuple(
        tuple(value == ATTACHED_SQL_NULL_SENTINEL for value in row)
        for row in rows
    )
    return MysqlTsvRows(
        rows,
        raw_tsv=raw_bytes,
        raw_rows=decoded.splitlines(),
        null_provenance=provenance,
    )


def _parse_tsv_file(path: Path) -> MysqlTsvRows:
    """读取 UTF-8 TSV，并解码 mysql batch 转义，保留原始 provenance。"""

    return parse_mysql_batch_tsv_with_provenance(path.read_bytes())


def parse_active_users_tsv(text: str) -> list[ActiveUser]:
    """解析 export_active_users.bat 产生的 TSV，不记录或回显连接信息。"""

    result: list[ActiveUser] = []
    for line_number, row in enumerate(parse_mysql_batch_tsv(text), start=1):
        if len(row) != 5:
            raise MySqlQueryError(f"active_users.tsv 第 {line_number} 行字段数不是 5")
        result.append(
            ActiveUser(
                user_id=row[0].strip(),
                name=row[1].strip(),
                username=row[2].strip(),
                work_number=row[3].strip(),
                dept_id=row[4].strip(),
            )
        )
    return result


def resolve_preconfigured_engineer(engineer: Engineer, active_users: Iterable[ActiveUser]) -> ActiveUser:
    """将配置中的工程师安全解析为唯一有效用户；禁止同名任取。"""

    users = list(active_users)
    if engineer.user_id and engineer.dept_id:
        matches = [
            user
            for user in users
            if user.user_id == engineer.user_id
            and user.dept_id == engineer.dept_id
            and user.name == engineer.name
        ]
    else:
        matches = [user for user in users if user.name == engineer.name]
    if len(matches) != 1:
        raise MySqlQueryError(f"预配置工程师未解析为唯一有效用户：{engineer.name}，匹配数={len(matches)}")
    return matches[0]


@dataclass
class BatActiveUserProvider:
    """通过现有 export_active_users.bat 获取只读人员列表。

    该类只在后续授权的只读联调阶段调用 BAT；本阶段测试使用 parse_active_users_tsv。
    """

    bat_path: Path
    tsv_path: Path
    error_log_path: Path
    timeout_seconds: int = 30
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None

    def query(self) -> list[ActiveUser]:
        if not self.bat_path.exists():
            raise MySqlQueryError(f"只读人员查询 BAT 不存在：{self.bat_path}")
        command = ["cmd.exe", "/d", "/c", "call", str(self.bat_path)]
        run = self.runner or subprocess.run
        try:
            completed = run(
                command,
                cwd=str(self.bat_path.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                **_hidden_cmd_window_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            raise MySqlQueryError("只读人员查询超时") from exc
        except OSError as exc:
            raise MySqlQueryError(f"无法启动只读人员查询 BAT：{exc}") from exc
        if completed.returncode != 0:
            raise MySqlQueryError(f"只读人员查询失败，BAT exit_code={completed.returncode}")
        if not self.tsv_path.exists():
            raise MySqlQueryError(f"只读人员查询未生成 TSV：{self.tsv_path}")
        try:
            text = self.tsv_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise MySqlQueryError(f"无法读取只读人员 TSV：{self.tsv_path}") from exc
        return parse_active_users_tsv(text)


@dataclass
class ReadonlyMysqlClient:
    """通过项目内只读 BAT 调用 MySQL CLI；本阶段不自动执行。"""

    readonly_bat: Path
    temp_dir: Path | None = None
    timeout_seconds: int = 60
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None
    # 默认不写入，便于离线单元测试；桌面入口显式绑定到 ``日志`` 目录。
    diagnostic_log_path: Path | None = None

    def execute(self, sql: str, query_label: str | None = None) -> list[list[str]]:
        """执行一条受控只读查询并返回 TSV 行。

        ``query_label`` 只用于诊断阶段标识，不会拼接进 SQL 或命令行；失败时
        同时收集临时 ``.err.log`` 与 ``CompletedProcess.stderr``，再统一脱敏。
        """

        query_stage = query_label or "readonly_query"
        safe_sql = validate_readonly_sql(sql)
        sql_sha256 = hashlib.sha256(safe_sql.encode("utf-8")).hexdigest()
        child_environment = _readonly_subprocess_environment(
            mysql_client_dir=self.readonly_bat.parent / "mysql-client"
        )
        bat_metadata = _file_diagnostic_metadata(self.readonly_bat)
        start_fields: dict[str, object] = {
            "query_stage": query_stage,
            "sql_sha256": sql_sha256,
            "bat_path": bat_metadata.pop("path", str(self.readonly_bat)),
            "bat_exists": bat_metadata.pop("exists", False),
            "bat_bytes": bat_metadata.pop("bytes", 0),
            "bat_sha256": bat_metadata.pop("sha256", ""),
            "bat_read_error": bat_metadata.pop("read_error", ""),
            **_readonly_config_diagnostic_metadata(self.readonly_bat),
            **_mysql_client_diagnostic_metadata(self.readonly_bat, child_environment),
        }
        started_at = time.monotonic()
        _write_query_diagnostic(
            self.diagnostic_log_path,
            "mysql_readonly_query_start",
            status="STARTED",
            detail="开始执行只读查询；未记录 SQL 或连接配置正文",
            fields=start_fields,
        )
        if not self.readonly_bat.exists():
            _write_query_diagnostic(
                self.diagnostic_log_path,
                "mysql_readonly_query_result",
                status="ERROR",
                detail="只读查询 BAT 不存在",
                fields={
                    **start_fields,
                    "exit_code": "",
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "error_kind": "bat_missing",
                },
            )
            raise MySqlQueryError(
                f"只读查询 BAT 不存在：{self.readonly_bat}",
                query_stage=query_stage,
                safe_detail="readonly_query_bat_missing",
            )
        run = self.runner or subprocess.run
        with tempfile.TemporaryDirectory(dir=str(self.temp_dir) if self.temp_dir else None) as temp:
            temp_path = Path(temp)
            sql_file = temp_path / "readonly_query.sql"
            result_file = temp_path / "readonly_result.tsv"
            sql_file.write_text(safe_sql, encoding="utf-8")
            command = ["cmd.exe", "/d", "/c", "call", str(self.readonly_bat), str(sql_file), str(result_file)]
            try:
                completed = run(
                    command,
                    cwd=str(self.readonly_bat.parent),
                    env=child_environment,
                    capture_output=True,
                    # 保留 Windows 原始字节，不能在 subprocess 层固定 UTF-8。
                    text=False,
                    timeout=self.timeout_seconds,
                    check=False,
                    **_hidden_cmd_window_kwargs(),
                )
            except subprocess.TimeoutExpired as exc:
                timeout_stderr, timeout_stderr_meta = _stream_diagnostic_metadata(
                    getattr(exc, "stderr", None)
                )
                _write_query_diagnostic(
                    self.diagnostic_log_path,
                    "mysql_readonly_query_result",
                    status="ERROR",
                    detail="MySQL 只读查询超时",
                    fields={
                        **start_fields,
                        **{f"stderr_{key}": value for key, value in timeout_stderr_meta.items()},
                        "exit_code": "",
                        "duration_ms": int((time.monotonic() - started_at) * 1000),
                        "error_kind": "timeout",
                        "safe_detail": redact_log_text(timeout_stderr.strip() or "timeout"),
                    },
                )
                raise MySqlQueryError(
                    "MySQL 只读查询超时", query_stage=query_stage, safe_detail="timeout"
                ) from exc
            except OSError as exc:
                detail = redact_log_text(str(exc))
                _write_query_diagnostic(
                    self.diagnostic_log_path,
                    "mysql_readonly_query_result",
                    status="ERROR",
                    detail="无法启动 MySQL 只读查询 BAT",
                    fields={
                        **start_fields,
                        "exit_code": "",
                        "duration_ms": int((time.monotonic() - started_at) * 1000),
                        "error_kind": "process_start",
                        "safe_detail": detail,
                    },
                )
                raise MySqlQueryError(
                    f"无法启动 MySQL 只读查询 BAT：{detail}",
                    query_stage=query_stage,
                    safe_detail=detail,
                ) from exc
            _stdout_text, stdout_meta = _stream_diagnostic_metadata(getattr(completed, "stdout", None))
            stderr_text, stderr_meta = _stream_diagnostic_metadata(getattr(completed, "stderr", None))
            error_file = Path(f"{result_file}.err.log")
            err_log_text = ""
            err_log_meta: dict[str, object] = {
                "bytes": 0,
                "sha256": "",
                "encoding": "none",
                "exists": error_file.is_file(),
            }
            if error_file.is_file():
                try:
                    err_log_text, err_log_stream_meta = _stream_diagnostic_metadata(error_file.read_bytes())
                    err_log_meta.update(err_log_stream_meta)
                except OSError:
                    err_log_meta["read_error"] = "OSError"
            result_meta = _file_diagnostic_metadata(result_file)
            result_fields: dict[str, object] = {
                **start_fields,
                "exit_code": completed.returncode,
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "stdout_bytes": stdout_meta["bytes"],
                "stdout_sha256": stdout_meta["sha256"],
                "stderr_bytes": stderr_meta["bytes"],
                "stderr_sha256": stderr_meta["sha256"],
                "stderr_encoding": stderr_meta["encoding"],
                "err_log_exists": err_log_meta.get("exists", False),
                "err_log_bytes": err_log_meta.get("bytes", 0),
                "err_log_sha256": err_log_meta.get("sha256", ""),
                "err_log_encoding": err_log_meta.get("encoding", "none"),
                "result_exists": result_meta.get("exists", False),
                "result_bytes": result_meta.get("bytes", 0),
                "result_sha256": result_meta.get("sha256", ""),
            }
            if completed.returncode != 0:
                diagnostics: list[str] = []
                if stderr_text:
                    diagnostics.append(stderr_text.strip())
                if err_log_text:
                    diagnostics.append(err_log_text)
                elif err_log_meta.get("read_error"):
                    diagnostics.append("err_log_unreadable")
                safe_detail = redact_log_text(" ".join(item for item in diagnostics if item).strip())
                if not safe_detail:
                    safe_detail = "mysql_query_failed"
                _write_query_diagnostic(
                    self.diagnostic_log_path,
                    "mysql_readonly_query_result",
                    status="ERROR",
                    detail="MySQL 只读查询失败",
                    fields={
                        **result_fields,
                        "error_kind": "bat_exit_nonzero",
                        "safe_detail": safe_detail,
                    },
                )
                raise MySqlQueryError(
                    f"MySQL 只读查询失败，BAT exit_code={completed.returncode}；{safe_detail}",
                    query_stage=query_stage,
                    exit_code=completed.returncode,
                    safe_detail=safe_detail,
                )
            if not result_file.exists():
                _write_query_diagnostic(
                    self.diagnostic_log_path,
                    "mysql_readonly_query_result",
                    status="ERROR",
                    detail="MySQL 只读查询未生成结果 TSV",
                    fields={
                        **result_fields,
                        "error_kind": "result_tsv_missing",
                        "safe_detail": "result_tsv_missing",
                    },
                )
                raise MySqlQueryError(
                    "MySQL 只读查询未生成结果 TSV",
                    query_stage=query_stage,
                    exit_code=completed.returncode,
                    safe_detail="result_tsv_missing",
                )
            try:
                rows = _parse_tsv_file(result_file)
            except Exception as exc:
                detail = redact_log_text(str(exc))
                _write_query_diagnostic(
                    self.diagnostic_log_path,
                    "mysql_readonly_query_result",
                    status="ERROR",
                    detail="MySQL 只读查询结果解析失败",
                    fields={
                        **result_fields,
                        "error_kind": "result_parse",
                        "safe_detail": detail,
                    },
                )
                raise
            _write_query_diagnostic(
                self.diagnostic_log_path,
                "mysql_readonly_query_result",
                status="SUCCESS",
                detail="MySQL 只读查询成功",
                fields={
                    **result_fields,
                    "row_count": len(rows),
                },
            )
            return rows


@dataclass
class ReadonlyQueryService:
    """按固定顺序加载 WO-04 的只读模型快照；不包含任何 DML。"""

    client: ReadonlyMysqlClient
    event_callback: QueryEventCallback | None = None

    def set_event_callback(self, callback: QueryEventCallback | None) -> None:
        """设置/清除结构化诊断回调；旧版只传 ``client`` 的调用保持兼容。"""

        self.event_callback = callback

    def _emit(self, payload: Mapping[str, object]) -> None:
        callback = self.event_callback
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:
            # 日志回调不能改变只读查询本身的成功/失败结果；调用方仍会收到
            # 原始查询异常。这里不回显回调异常，避免诊断链路反过来污染业务。
            return

    def _execute(self, query_label: str) -> list[list[str]]:
        self._emit(
            {
                "name": "readonly_query_start",
                "status": "STARTED",
                "detail": "开始执行只读 SELECT",
                "query_label": query_label,
            }
        )
        try:
            rows = self.client.execute(READONLY_SQL[query_label], query_label=query_label)
        except Exception as exc:
            query_stage = getattr(exc, "query_stage", None) or query_label
            safe_detail = getattr(exc, "safe_detail", "") or str(exc)
            fields: dict[str, object] = {
                "query_label": query_label,
                "query_stage": redact_log_text(str(query_stage)),
                "safe_detail": redact_log_text(str(safe_detail)),
            }
            exit_code = getattr(exc, "exit_code", None)
            if exit_code is not None:
                fields["exit_code"] = str(exit_code)
            self._emit(
                {
                    "name": "readonly_query_error",
                    "status": "ERROR",
                    "detail": redact_log_text(f"只读 SELECT 失败：{type(exc).__name__}"),
                    **fields,
                }
            )
            raise
        self._emit(
            {
                "name": "readonly_query_success",
                "status": "SUCCESS",
                "detail": "只读 SELECT 执行成功",
                "query_label": query_label,
                "row_count": len(rows),
            }
        )
        return rows

    def load_snapshot(self) -> ReadonlySnapshot:
        models = tuple(
            map_procedure_model_row(row)
            for row in self._execute("procedure_models")
        )
        contexts = tuple(
            map_procedure_context_row(row)
            for row in self._execute("procedure_contexts")
        )
        attached_rows = self._execute("attached_models")
        attached_provenance = getattr(attached_rows, "null_provenance", ())
        attached_raw_tsv = getattr(attached_rows, "raw_tsv", b"")
        attached = tuple(
            map_attached_model_row(
                row,
                null_provenance=(
                    tuple(attached_provenance[index])
                    if index < len(attached_provenance)
                    else None
                ),
                raw_tsv=attached_raw_tsv,
                source_row_number=index + 1,
            )
            for index, row in enumerate(attached_rows)
        )
        users = tuple(
            map_active_user_row(row)
            for row in self._execute("active_users")
        )
        return ReadonlySnapshot(
            procedure_models=models,
            procedure_contexts=contexts,
            attached_models=attached,
            active_users=users,
        )
