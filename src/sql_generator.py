"""Generate one non-executing SQL import bundle per delivery note.

WO-07 deliberately stops at artifact generation. The generator accepts a
fully resolved, explicit data object produced by later read-only stages; it
does not query MySQL, invoke a BAT/EXE, or read connection configuration.

The three INSERT targets and their column lists are kept here as constants so
the target-table boundary can be statically audited. The source VBA was used
to recover the column order, but no VBA code is executed by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Sequence

from .models import canonicalize_m_code_separators, effective_supplier_for_order


class SqlGenerationError(ValueError):
    """Input data or an output path is not safe for artifact generation."""


TARGET_TABLES = (
    "qcauto_procedure_draft",
    "qcauto_procedure_draft_check_context",
    "qcauto_entry_attached_table_draft",
)

_FORBIDDEN_SQL = (
    "ANALYZE",
    "ALTER",
    "CALL",
    "CREATE",
    "DEALLOCATE",
    "DELETE",
    "DROP",
    "EXECUTE",
    "GRANT",
    "HANDLER",
    "LOAD",
    "LOCK",
    "OPTIMIZE",
    "PREPARE",
    "RENAME",
    "REPLACE",
    "REVOKE",
    "SLEEP",
    "TRUNCATE",
    "UNLOCK",
    "UPDATE",
    "USE",
)

# 兼容旧快照中 ``压缩值(完整展开值)`` 的附表 sequenceCode；新生成 SQL
# 只保留括号前合并结果，与主表 sequence_code 保持完全一致。
_M_EXPANSION_SUFFIX_RE = re.compile(
    r"^(?P<head>.*?)\((?P<body>M\d{11}(?:\s*[/;,；，、]\s*M\d{11})*)\)$"
)


def _output_sequence_code(value: object | None) -> str | None:
    """统一 SQL 主字段/诊断中的 M 码值，移除旧括号展开后缀。"""

    if value is None:
        return None
    text = str(value).strip()
    suffix = _M_EXPANSION_SUFFIX_RE.fullmatch(text)
    if suffix is not None:
        text = suffix.group("head").strip()
    return canonicalize_m_code_separators(text)


@dataclass(frozen=True)
class SqlContextRow:
    """One row copied to ``qcauto_procedure_draft_check_context``."""

    context_number: str
    inspection_items: str
    inspection_standards: str
    description: str | None = None
    inspect_records: str | None = None
    inspection_conclusion: str | None = None


_ATTACHED_SLOT_COUNT = 9


def _attached_text(value: object) -> str:
    """将附表模板单元格转成与前端一致的可显示文本。"""

    if value is None:
        return ""
    return str(value).strip()


def _attached_slot_text(value: object) -> str:
    """Normalize only one of the eighteen item/range template slots.

    SQL ``NULL`` values are converted to ``None`` by the TSV provenance
    parser before they reach this layer.  A regular text value (including the
    legitimate literal ``"NULL"``) must remain unchanged; only ``None`` or
    an empty source cell is treated as an empty slot and rendered as ``NA``
    by the display layer.  Notes, conclusions, and check_number never call
    this helper and therefore retain their source values.
    """

    return _attached_text(value)


def _attached_slot_is_empty(value: object) -> bool:
    """Whether an item slot has no applicable inspection content."""

    text = _attached_slot_text(value)
    return not text or text.upper() == "NA"


def _attached_header_value(value: object) -> str:
    """附表检查项目/标准范围为空时显示 ``NA``。"""

    return _attached_slot_text(value) or "NA"


def _blank_inspection_content() -> str:
    """返回 auto-factory 使用的数组形态空行，而不是单个 JSON 对象。"""

    row = {"sequenceCode": ""}
    row.update({f"inspectionContent{index}": "" for index in range(1, _ATTACHED_SLOT_COUNT + 1)})
    return json.dumps([row], ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class SqlAttachedRow:
    """One row copied to ``qcauto_entry_attached_table_draft``."""

    inspection_items: tuple[str, ...] = field(default_factory=lambda: ("",) * 9)
    standard_ranges: tuple[str, ...] = field(default_factory=lambda: ("",) * 9)
    inspection_number: str | None = None
    testing_tools: str = ""
    # 结论由模板/上游业务提供；生成器不再擅自写入“合格”。
    # None 通过 _sql_string 渲染为 SQL NULL，与 VBA 的空值语义一致。
    inspection_conclusion: str | None = None
    notes: str = ""
    inspection_content: str = field(default_factory=_blank_inspection_content)

    def __post_init__(self) -> None:
        if len(self.inspection_items) != _ATTACHED_SLOT_COUNT or len(self.standard_ranges) != _ATTACHED_SLOT_COUNT:
            raise SqlGenerationError("附表行必须恰有 inspection_item1..9 和 standard_range1..9")
        try:
            content = json.loads(self.inspection_content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SqlGenerationError("附表 inspection_content 必须是合法 JSON 数组") from exc
        if not isinstance(content, list) or not content:
            raise SqlGenerationError("附表 inspection_content 必须是非空 JSON 数组")
        for index, row in enumerate(content, start=1):
            if not isinstance(row, dict):
                raise SqlGenerationError(f"附表 inspection_content 第{index}行必须是 JSON 对象")
            if "sequenceCode" not in row:
                raise SqlGenerationError(f"附表 inspection_content 第{index}行缺少 sequenceCode")
            missing = [
                f"inspectionContent{slot}"
                for slot in range(1, _ATTACHED_SLOT_COUNT + 1)
                if f"inspectionContent{slot}" not in row
            ]
            if missing:
                raise SqlGenerationError(
                    f"附表 inspection_content 第{index}行缺少字段：{','.join(missing)}"
                )

    @classmethod
    def from_template(
        cls,
        inspection_items: Sequence[object],
        standard_ranges: Sequence[object],
        *,
        inspection_number: str | None = None,
        notes: object = "",
    ) -> "SqlAttachedRow":
        """按 auto-factory 的附表规则生成一条可落库附表行。

        模板中已有的检查项目/标准范围原样保留；空槽的表头写 ``NA``，
        对应的检查内容也写 ``NA``。有检查项目的内容留空，等待后续人工
        检验记录填写。调用方可在组装期传入当时已知的检查单号；最终
        SQL/API 持久化边界会以主草稿的最终 ``trigger_basis`` 为准。
        """

        if len(inspection_items) != _ATTACHED_SLOT_COUNT or len(standard_ranges) != _ATTACHED_SLOT_COUNT:
            raise SqlGenerationError("附表模板必须恰有 9 个检查项目和 9 个标准范围")
        raw_items = tuple(_attached_slot_text(value) for value in inspection_items)
        raw_ranges = tuple(_attached_slot_text(value) for value in standard_ranges)
        items = tuple(_attached_header_value(value) for value in raw_items)
        ranges = tuple(_attached_header_value(value) for value in raw_ranges)
        content_row = {"sequenceCode": ""}
        content_row.update(
            {
                f"inspectionContent{slot}": "NA" if _attached_slot_is_empty(raw_items[slot - 1]) else ""
                for slot in range(1, _ATTACHED_SLOT_COUNT + 1)
            }
        )
        return cls(
            inspection_items=items,
            standard_ranges=ranges,
            inspection_number=inspection_number,
            notes=_attached_text(notes),
            inspection_content=json.dumps([content_row], ensure_ascii=False, separators=(",", ":")),
        )


def build_attached_row_from_template(
    inspection_items: Sequence[object],
    standard_ranges: Sequence[object],
    *,
    inspection_number: str | None = None,
    notes: object = "",
) -> SqlAttachedRow:
    """业务层附表生成入口，供解析 provider 和其他上传适配器复用。"""

    return SqlAttachedRow.from_template(
        inspection_items,
        standard_ranges,
        inspection_number=inspection_number,
        notes=notes,
    )


@dataclass(frozen=True)
class SqlRecord:
    """Resolved values required to generate one draft and its child rows.

    All values are explicit on purpose. In particular, the generator does
    not guess model, user, or sampling values from an Excel row. A missing
    ``completion_time`` intentionally follows the old BAS run-day default
    (``date.today()``); any supplied date is validated as a real ISO date.
    The read-only query stage must resolve the remaining values before WO-07.
    """

    item_code: str
    item_name: str
    quantity: int | float | str | Decimal
    supplier: str
    procedure_name: str
    procedure_model_id: int | str
    engineer_id: int | str
    engineer_name: str
    engineer_dept_id: str
    trigger_basis: str
    check_number: int | str | None
    contexts: tuple[SqlContextRow, ...]
    sequence_code: str | None = None
    accept_standard: int | str = -1
    check_plan: int | str = -1
    testing_tools: str = ""
    # 不设置业务默认值。调用方明确提供时原样写入，否则为 SQL NULL。
    inspection_conclusion: str | None = None
    completion_time: str | None = None
    ncr_code: str = ""
    notes: str = ""
    attachment: str = "[]"
    attached_tables: tuple[SqlAttachedRow, ...] = field(default_factory=tuple)
    # 别名仅用于审计诊断，不参与 SQL 字段；原始/规范化源名与目标名均保留
    # 便于人工复核，默认值保持既有注入调用兼容。
    alias_applied: bool = False
    source_procedure_name: str = ""
    target_procedure_name: str = ""
    alias_matched_by: str = ""
    alias_rule_id: str = ""
    alias_version: str = ""
    alias_effective_date: str = ""
    # 人工确认只影响本次草稿，不改变全局别名配置；保留输入名称及
    # 解析依据，便于随后从 diagnostic.json 追溯操作员决策。
    manual_confirmation_applied: bool = False
    manual_input_name: str = ""
    manual_input_basis: str = ""
    # Excel/SQL 均写 effective supplier；raw_supplier 仅用于 diagnostic
    # provenance。追加到全部既有字段之后并提供默认值，完整兼容旧版位置
    # 构造，同时不会改变既有 SQL 列顺序。
    raw_supplier: str = ""
    supplier_defaulted: bool = False
    # API 主表有 measurementUnit 字段；该值在 SQL 既有契约中尚未落列，
    # 追加为可选字段供 API payload 使用，旧版位置构造保持兼容。
    measurement_unit: str = ""
    # 只用于日志/诊断文件，不进入 SQL 列或 Entry 表单。
    sampling_diagnostic: Mapping[str, object] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class SqlOrder:
    """All rows for one delivery note and therefore one transaction."""

    order_number: str
    records: tuple[SqlRecord, ...]
    # 聚合阶段产生的非阻断提醒（例如跨 M 码前缀范围）；保留在诊断 JSON
    # 中，便于人工确认实际写入的起止端点。
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not _safe_order_number(self.order_number):
            raise SqlGenerationError(f"送检单号不是安全文件名：{self.order_number!r}")
        if not self.records:
            raise SqlGenerationError("每单至少需要一条已解析的 SQL 记录")


@dataclass(frozen=True)
class GeneratedOrderArtifacts:
    order_number: str
    sql_path: Path
    bat_path: Path
    diagnostic_path: Path


def _safe_order_number(value: str) -> bool:
    if not isinstance(value, str):
        return False
    text = str(value).strip()
    # The value is inserted into a generated CMD file name. Besides Windows
    # path characters, reject CMD expansion/separator metacharacters so a
    # later manual BAT launch cannot reinterpret the order number.
    forbidden = '<>:"/\\|?*%&^!();'
    reserved_device_names = {"CON", "PRN", "AUX", "NUL", "CLOCK$"} | {
        f"COM{index}" for index in range(1, 10)
    } | {f"LPT{index}" for index in range(1, 10)}
    return bool(
        text
        and text not in {".", ".."}
        and text.upper() not in reserved_device_names
        and not text.endswith((".", " "))
        and not any(c in forbidden or ord(c) < 32 for c in text)
    )


def _safe_stem(value: str) -> str:
    if not isinstance(value, str) or not _safe_order_number(value):
        raise SqlGenerationError(f"送检单号不能作为安全文件名：{value!r}")
    return value.strip()


def _sql_string(value: object | None) -> str:
    """Return a MySQL UTF-8 string literal with quote/backslash escaping."""

    if value is None:
        return "NULL"
    text = str(value)
    if any((ord(char) < 32 and char not in "\r\n\t") or ord(char) == 127 for char in text):
        raise SqlGenerationError("文本值包含不允许的控制字符")
    escaped = text.replace("\\", "\\\\").replace("'", "''")
    return "'" + escaped + "'"


def _sql_json_outer_string(value: str) -> str:
    """将内层 JSON 文本包装成 auto-factory 兼容的外层 JSON 字符串。

    ``EntryAttachedTableDraft`` 的 Django ``JSONField`` 由旧业务接口以
    ``json.dumps(inspection_content)`` 保存，读取时再 ``json.loads`` 一次；
    旧 BAS 因此使用 ``JSON_QUOTE(CAST(... AS CHAR))``。直接把数组文本作为
    JSON 列值会落成 JSON 数组，破坏该“外层字符串/内层数组”契约。
    """

    return f"JSON_QUOTE(CAST({_sql_string(value)} AS CHAR))"


def _canonicalize_inspection_content(value: str, sequence_code: object | None = None) -> str:
    """只规范附表 JSON ``sequenceCode`` 中的 M 码分隔符。

    附表 JSON 不是普通路径文本；仅当字段包含严格 M 码时调用共享归一
    函数，其他 inspectionContent/未知扩展字段及其顺序保持原样。解析失败
    不在这里吞掉，``SqlAttachedRow`` 构造阶段会给出原有明确错误。
    """

    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value
    if not isinstance(payload, list):
        return value
    changed = False
    for row in payload:
        if not isinstance(row, dict) or "sequenceCode" not in row:
            continue
        current = row.get("sequenceCode")
        normalized = canonicalize_m_code_separators(current)
        # SqlAttachedRow 的历史 JSON 默认 sequenceCode 为空；当主记录有
        # M 码时以同一聚合字段覆盖，防止主表/附表各自保存不同分隔或旧
        # 括号展开值。没有主值则只规范附表原值。
        shared_sequence = _output_sequence_code(sequence_code)
        if shared_sequence is not None and shared_sequence.strip():
            normalized = shared_sequence
        suffix = _M_EXPANSION_SUFFIX_RE.fullmatch(normalized)
        if suffix is not None:
            normalized = canonicalize_m_code_separators(suffix.group("head").strip())
        if normalized != current:
            row["sequenceCode"] = normalized
            changed = True
    if not changed:
        return value
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _sql_integer(value: object | None, field_name: str, *, nullable: bool = False) -> str:
    if value is None or str(value).strip() == "":
        if nullable:
            return "NULL"
        raise SqlGenerationError(f"{field_name} 不能为空")
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?\d+", text):
        raise SqlGenerationError(f"{field_name} 必须是整数")
    return str(int(text, 10))


def _sql_decimal(value: object, field_name: str) -> str:
    try:
        decimal = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise SqlGenerationError(f"{field_name} 必须是数字") from exc
    if not decimal.is_finite() or decimal < 0:
        raise SqlGenerationError(f"{field_name} 必须是非负有限数字")
    text = format(decimal, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".") or "0"
    return text


def _required_text(value: object, field_name: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise SqlGenerationError(f"{field_name} 不能为空")
    return text


def _effective_record_supplier(record: SqlRecord, order_number: str) -> tuple[str, str, bool]:
    """统一 SqlRecord 的供应商 effective/raw/defaulted 三元组。

    生成服务会显式传入 ``raw_supplier``；旧版/外部构造若未提供该字段，
    仅在 raw 为空且 supplier 也为空时按 TK 规则补 ``NA``，已有供应商文本
    则视为其自身 raw，避免兼容调用被误清空。该 helper 只参与输出与校验，
    不修改冻结的 SqlRecord 实例。
    """

    raw = record.raw_supplier
    if not raw and str(record.supplier).strip() and not record.supplier_defaulted:
        raw = str(record.supplier)
    effective, raw_text, defaulted = effective_supplier_for_order(order_number, raw)
    return effective, raw_text, bool(record.supplier_defaulted or defaulted)


def _normalise_record(record: SqlRecord, order_number: str) -> None:
    _required_text(record.item_code, "item_code")
    _required_text(record.item_name, "item_name")
    effective_supplier, _, _ = _effective_record_supplier(record, order_number)
    _required_text(effective_supplier, "supplier")
    _required_text(record.procedure_name, "procedure_name")
    _required_text(record.engineer_name, "engineer_name")
    _required_text(record.engineer_dept_id, "engineer_dept_id")
    _required_text(record.trigger_basis, "trigger_basis")
    _sql_decimal(record.quantity, "quantity")
    _sql_integer(record.procedure_model_id, "procedure_model_id")
    _sql_integer(record.engineer_id, "engineer_id")
    # ``check_number`` is required as an explicit constructor argument, but
    # the legacy sampling rule legitimately supplies NULL for non-GB2828
    # paths; retain that value instead of inventing a sample size.
    _sql_integer(record.check_number, "check_number", nullable=True)
    _sql_integer(record.accept_standard, "accept_standard")
    _sql_integer(record.check_plan, "check_plan")
    if record.completion_time is not None:
        try:
            date.fromisoformat(str(record.completion_time))
        except ValueError as exc:
            raise SqlGenerationError(f"completion_time 必须为有效的 YYYY-MM-DD 日期：{record.completion_time!r}") from exc
    if not _safe_order_number(order_number):
        raise SqlGenerationError(f"送检单号不是安全文件名：{order_number!r}")
    if not record.contexts:
        raise SqlGenerationError("每条 SQL 记录至少需要一条检查上下文")
    for context in record.contexts:
        _required_text(context.context_number, "context_number")
        _required_text(context.inspection_items, "inspection_items")
        _required_text(context.inspection_standards, "inspection_standards")


def _draft_insert(record: SqlRecord, order_number: str) -> str:
    columns = (
        "description, modifier, dept_belong_id, update_datetime, create_datetime, "
        "trigger_basis, item_code, item_name, totol_number, supplier, sequence_code, "
        "check_number, check_plan, accept_standard, testing_tools, inspection_conclusion, "
        "qc_engineer, completion_time, ncr_code, notes, attachment, creator_id, procedure_model_id"
    )
    trigger = record.trigger_basis
    effective_supplier, _, _ = _effective_record_supplier(record, order_number)
    # 保留 None 的 SQL NULL 语义；仅对实际文本中的 M 码分隔符归一化。
    sequence_code = _output_sequence_code(record.sequence_code)
    completion = record.completion_time or date.today().isoformat()
    values = (
        "NULL",
        _sql_integer(record.engineer_id, "engineer_id"),
        _sql_string(record.engineer_dept_id),
        "NOW(6)",
        "NOW(6)",
        _sql_string(trigger),
        _sql_string(record.item_code),
        _sql_string(record.item_name),
        _sql_decimal(record.quantity, "quantity"),
        _sql_string(effective_supplier),
        _sql_string(sequence_code),
        # 抽样计算值保留在 record；最终 SQL 持久化边界固定写 NULL。
        "NULL",
        _sql_integer(record.check_plan, "check_plan"),
        _sql_integer(record.accept_standard, "accept_standard"),
        _sql_string(record.testing_tools),
        _sql_string(record.inspection_conclusion),
        _sql_string(record.engineer_name),
        _sql_string(completion),
        _sql_string(record.ncr_code),
        _sql_string(record.notes),
        _sql_string(record.attachment),
        _sql_integer(record.engineer_id, "engineer_id"),
        _sql_integer(record.procedure_model_id, "procedure_model_id"),
    )
    return f"INSERT INTO qcauto_procedure_draft ({columns}) VALUES ({', '.join(values)});"


def _context_insert(row: SqlContextRow, draft_variable: str, record: SqlRecord) -> str:
    columns = (
        "description, modifier, dept_belong_id, update_datetime, create_datetime, "
        "context_number, inspection_items, inspection_standards, inspect_records, "
        "inspection_conclusion, creator_id, procedure_draft_id"
    )
    values = (
        _sql_string(row.description),
        _sql_integer(record.engineer_id, "engineer_id"),
        _sql_string(record.engineer_dept_id),
        "NOW(6)",
        "NOW(6)",
        _sql_string(row.context_number),
        _sql_string(row.inspection_items),
        _sql_string(row.inspection_standards),
        _sql_string(row.inspect_records),
        _sql_string(row.inspection_conclusion),
        _sql_integer(record.engineer_id, "engineer_id"),
        draft_variable,
    )
    return f"INSERT INTO qcauto_procedure_draft_check_context ({columns}) VALUES ({', '.join(values)});"


def _attached_insert(row: SqlAttachedRow, draft_variable: str, record: SqlRecord) -> str:
    columns = (
        "description, modifier, dept_belong_id, update_datetime, create_datetime, "
        "inspection_number, item_code, check_number, testing_tools, inspection_conclusion, "
        "qc_engineer, completion_time, creator_id, procedure_draft_id, "
        "inspection_item1, inspection_item2, inspection_item3, inspection_item4, inspection_item5, "
        "inspection_item6, inspection_item7, inspection_item8, inspection_item9, "
        "standard_range1, standard_range2, standard_range3, standard_range4, standard_range5, "
        "standard_range6, standard_range7, standard_range8, standard_range9, notes, inspection_content"
    )
    # 附表是当前主草稿的子记录；inspection_number 必须与主草稿
    # 最终 trigger_basis 一致，不采用 SqlAttachedRow 中可能过时的中间值。
    inspection_number = record.trigger_basis
    completion = record.completion_time or date.today().isoformat()
    values: list[str] = [
        "NULL",
        _sql_integer(record.engineer_id, "engineer_id"),
        _sql_string(record.engineer_dept_id),
        "NOW(6)",
        "NOW(6)",
        _sql_string(inspection_number),
        _sql_string(record.item_code),
        # 抽样计算值保留在 record；最终附表 SQL 持久化边界固定写 NULL。
        "NULL",
        _sql_string(row.testing_tools),
        _sql_string(row.inspection_conclusion),
        _sql_string(record.engineer_name),
        _sql_string(completion),
        _sql_integer(record.engineer_id, "engineer_id"),
        draft_variable,
    ]
    values.extend(_sql_string(value) for value in row.inspection_items)
    values.extend(_sql_string(value) for value in row.standard_ranges)
    values.extend(
        (
            _sql_string(row.notes),
            _sql_json_outer_string(
                _canonicalize_inspection_content(row.inspection_content, record.sequence_code)
            ),
        )
    )
    return f"INSERT INTO qcauto_entry_attached_table_draft ({columns}) VALUES ({', '.join(values)});"


def render_order_sql(order: SqlOrder) -> str:
    """Render one order as UTF-8 SQL without executing it."""

    for record in order.records:
        _normalise_record(record, order.order_number)

    lines = [
        "-- IQC Python WO-07 PREPARED artifact; generator never executes this file.",
        "-- One delivery note is one transaction. A failing client session must not be reused for another order.",
        "-- Failure rollback evidence is supplied by the WO-08 per-order executor.",
        "SET NAMES utf8mb4;",
        "START TRANSACTION;",
        f"SET @wo07_order_number = {_sql_string(order.order_number)};",
    ]
    for index, record in enumerate(order.records, start=1):
        draft_variable = f"@wo07_draft_id_{index}"
        lines.extend(
            [
                f"-- record {index}: values were resolved by the read-only stage.",
                _draft_insert(record, order.order_number),
                f"SET {draft_variable} = LAST_INSERT_ID();",
            ]
        )
        lines.extend(_context_insert(context, draft_variable, record) for context in record.contexts)
        lines.extend(_attached_insert(attached, draft_variable, record) for attached in record.attached_tables)
    lines.extend(
        [
            "COMMIT;",
            "SELECT @wo07_order_number AS log_order_number, 'COMMITTED' AS log_status;",
        ]
    )
    sql = "\n".join(lines) + "\n"
    validate_generated_sql(sql)
    return sql


def _sql_code_only(sql: str) -> str:
    """Mask quoted strings/comments before lexical static checks.

    Business text can legitimately contain words such as ``DROP`` or
    ``ROLLBACK``.  Static gates must inspect SQL syntax, not those values.
    """

    chars = list(sql)
    index = 0
    state = "code"
    while index < len(chars):
        current = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if current == "'":
                chars[index] = " "
                state = "single"
            elif current == '"':
                chars[index] = " "
                state = "double"
            elif current == "#" or (current == "-" and following == "-"):
                chars[index] = " "
                if current == "-":
                    chars[index + 1] = " "
                    index += 1
                state = "line_comment"
            elif current == "/" and following == "*":
                chars[index] = chars[index + 1] = " "
                index += 1
                state = "block_comment"
        elif state in {"single", "double"}:
            chars[index] = " "
            if current == "\\":
                if index + 1 < len(chars):
                    chars[index + 1] = " "
                    index += 1
            elif (state == "single" and current == "'") or (state == "double" and current == '"'):
                state = "code"
        elif state == "line_comment":
            chars[index] = " "
            if current in "\r\n":
                state = "code"
        elif state == "block_comment":
            chars[index] = " "
            if current == "*" and following == "/":
                chars[index + 1] = " "
                index += 1
                state = "code"
        index += 1
    return "".join(chars)


def validate_generated_sql(sql: str) -> None:
    """Static gate for the generator's SQL grammar and target scope.

    This is intentionally an output allow-list, not a general MySQL parser.
    Only statements emitted by :func:`render_order_sql` are accepted; any
    future statement shape requires an explicit review and test update.
    """

    if not isinstance(sql, str) or not sql.strip():
        raise SqlGenerationError("SQL 内容为空")
    code = _sql_code_only(sql)
    # Unlike generic SQL keywords, this retired field must have zero textual
    # references in an artifact, including comments or literal values.
    if "measurement_unit" in sql.lower():
        raise SqlGenerationError("生成 SQL 含已取消的单位字段")
    upper = code.upper()
    if any(re.search(rf"\b{token}\b", upper) for token in _FORBIDDEN_SQL):
        raise SqlGenerationError("生成 SQL 含禁止的 DDL/DML 关键字")
    if re.search(r"\bSET\s+(GLOBAL|PERSIST|SESSION)\b", upper) or re.search(r"\bINTO\s+(OUTFILE|DUMPFILE)\b", upper):
        raise SqlGenerationError("生成 SQL 含禁止的会话/文件写入语句")

    statements = [statement.strip() for statement in code.split(";") if statement.strip()]
    if not statements:
        raise SqlGenerationError("生成 SQL 没有可执行语句")
    insert_targets: list[str] = []
    for statement in statements:
        if re.fullmatch(r"SET\s+NAMES\s+utf8mb4", statement, flags=re.IGNORECASE):
            continue
        if re.fullmatch(r"START\s+TRANSACTION", statement, flags=re.IGNORECASE):
            continue
        if re.fullmatch(r"COMMIT", statement, flags=re.IGNORECASE):
            continue
        if re.fullmatch(r"SET\s+@wo07_order_number\s*=\s*", statement, flags=re.IGNORECASE):
            continue
        if re.fullmatch(r"SET\s+@wo07_draft_id_\d+\s*=\s*LAST_INSERT_ID\(\)", statement, flags=re.IGNORECASE):
            continue
        insert_match = re.fullmatch(
            r"INSERT\s+INTO\s+([A-Za-z0-9_]+)\s*\([A-Za-z0-9_,\s]+\)\s+VALUES\s*\(.*\)",
            statement,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if insert_match:
            insert_targets.append(insert_match.group(1))
            continue
        # The diagnostic SELECT has a masked literal between the comma and
        # its alias after _sql_code_only; no other SELECT is in the grammar.
        if re.fullmatch(
            r"SELECT\s+@wo07_order_number\s+AS\s+log_order_number\s*,\s+AS\s+log_status",
            statement,
            flags=re.IGNORECASE,
        ):
            continue
        raise SqlGenerationError(f"生成 SQL 含未允许的语句：{statement[:80]!r}")

    if not insert_targets:
        raise SqlGenerationError("生成 SQL 没有目标表 INSERT")
    invalid = sorted(set(insert_targets) - set(TARGET_TABLES))
    if invalid:
        raise SqlGenerationError(f"生成 SQL 超出 WO-07 目标表范围：{invalid}")
    if sum(bool(re.fullmatch(r"START\s+TRANSACTION", item, flags=re.IGNORECASE)) for item in statements) != 1:
        raise SqlGenerationError("每单 SQL 必须恰有一个 START TRANSACTION")
    if sum(bool(re.fullmatch(r"COMMIT", item, flags=re.IGNORECASE)) for item in statements) != 1:
        raise SqlGenerationError("每单 SQL 必须恰有一个 COMMIT")


def _atomic_write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        # ``Path.write_text(..., newline=...)`` was added after Python 3.8.
        # Use ``open`` so the Win7 Python 3.8 build keeps deterministic LF
        # output without changing the atomic replace contract.
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SqlGenerationError(f"无法写入生成文件：{path}") from exc
    return path


def render_order_bat(order_number: str) -> str:
    """Render a manual BAT wrapper; this function itself never starts it."""

    stem = _safe_stem(order_number)
    return (
        "@echo off\n"
        "setlocal EnableExtensions DisableDelayedExpansion\n"
        f"set \"SQL_FILE=%~dp0iqc_draft_{stem}.sql\"\n"
        "set \"RUNNER_BAT=%~dp0..\\运行时\\db\\run_generated_sql.bat\"\n"
        f"set \"DIAGNOSTIC_FILE=%~dp0iqc_draft_{stem}.diagnostic.json\"\n"
        "if not exist \"%SQL_FILE%\" (echo SQL file missing&exit /b 2)\n"
        "if not exist \"%RUNNER_BAT%\" (echo import runner missing&exit /b 3)\n"
        "echo [PREPARED] This wrapper requires explicit user authorization before manual launch.\n"
        "call \"%RUNNER_BAT%\" \"%SQL_FILE%\"\n"
        "set \"EXIT_CODE=%ERRORLEVEL%\"\n"
        "echo [RESULT] exit_code=%EXIT_CODE%\n"
        "exit /b %EXIT_CODE%\n"
    )


def validate_generated_bat(text: str) -> None:
    """Static gate for the exact generated wrapper BAT grammar."""

    upper = text.upper()
    if "MYSQL_PASSWORD" in upper or "MYSQL_PWD" in upper or "--PASSWORD" in upper:
        raise SqlGenerationError("生成 BAT 不得包含连接凭据")
    if "RUN_GENERATED_SQL.BAT" not in upper or "%SQL_FILE%" not in upper:
        raise SqlGenerationError("生成 BAT 未调用固定导入入口或未传入 SQL 文件")
    lines = text.splitlines()
    if lines:
        lines[0] = lines[0].lstrip("\ufeff")
    allowed_patterns = (
        r"@echo off",
        r"setlocal EnableExtensions DisableDelayedExpansion",
        r'set "SQL_FILE=%~dp0iqc_draft_[^"]+\.sql"',
        r'set "RUNNER_BAT=%~dp0\.\.\\运行时\\db\\run_generated_sql\.bat"',
        r'set "DIAGNOSTIC_FILE=%~dp0iqc_draft_[^"]+\.diagnostic\.json"',
        r'if not exist "%SQL_FILE%" \(echo SQL file missing&exit /b 2\)',
        r'if not exist "%RUNNER_BAT%" \(echo import runner missing&exit /b 3\)',
        r"echo \[PREPARED\] This wrapper requires explicit user authorization before manual launch\.",
        r'call "%RUNNER_BAT%" "%SQL_FILE%"',
        r'set "EXIT_CODE=%ERRORLEVEL%"',
        r"echo \[RESULT\] exit_code=%EXIT_CODE%",
        r"exit /b %EXIT_CODE%",
    )
    if len(lines) != len(allowed_patterns) or any(
        not re.fullmatch(pattern, line, flags=re.IGNORECASE) for pattern, line in zip(allowed_patterns, lines)
    ):
        raise SqlGenerationError("生成 BAT 含非 WO-07 固定包装命令")


def render_order_diagnostic(order: SqlOrder, sql_path: Path, bat_path: Path, sql_text: str, bat_text: str) -> str:
    """Render an audit-only diagnostic JSON; execution status is explicit."""

    diagnostic = {
        "schema": "iqc-python-wo07/v1",
        "order_number": order.order_number,
        "status": "PREPARED_NOT_EXECUTED",
        "execution_prohibited_in_this_stage": True,
        "target_tables": list(TARGET_TABLES),
        "record_count": len(order.records),
        "context_row_count": sum(len(record.contexts) for record in order.records),
        "attached_row_count": sum(len(record.attached_tables) for record in order.records),
        "warnings": list(order.warnings),
        "sql_file": sql_path.name,
        "bat_file": bat_path.name,
        "bat_encoding": "UTF-8 with BOM",
        "sql_sha256": hashlib.sha256(sql_text.encode("utf-8")).hexdigest().upper(),
        "bat_sha256": hashlib.sha256(bat_text.encode("utf-8")).hexdigest().upper(),
        "static_gates": {
            "target_table_scope": "PASS",
            "transaction_boundary_count": "PASS",
            "transaction_rollback_on_failure": "NOT_VERIFIED",
            "special_character_escaping": "PASS",
            "unit_field_zero_reference": "PASS",
            "connection_values_in_artifacts": "NOT_INCLUDED",
        },
        "records": [
            {
                "item_code": record.item_code,
                "item_name": record.item_name,
                "quantity": str(record.quantity),
                "procedure_model_id": str(record.procedure_model_id),
                "trigger_basis": record.trigger_basis,
                "check_number": record.check_number,
                "check_plan": record.check_plan,
                "accept_standard": record.accept_standard,
                "accept_standard_type": type(record.accept_standard).__name__,
                "sampling_diagnostic": dict(record.sampling_diagnostic),
                # 诊断明确同时写 effective 和 raw，证明 TK 空供应商被
                # 归一为 NA 的依据；SQL/Excel 使用同一个 effective 值。
                "supplier": _effective_record_supplier(record, order.order_number)[0],
                "raw_supplier": _effective_record_supplier(record, order.order_number)[1],
                "supplier_defaulted": _effective_record_supplier(record, order.order_number)[2],
                "sequence_code": _output_sequence_code(record.sequence_code),
                "procedure_name": record.procedure_name,
                "alias_applied": bool(record.alias_applied),
                "source_procedure_name": record.source_procedure_name or record.procedure_name,
                "target_procedure_name": record.target_procedure_name or record.procedure_name,
                "alias_matched_by": record.alias_matched_by,
                "alias_rule_id": record.alias_rule_id,
                "alias_version": record.alias_version,
                "alias_effective_date": record.alias_effective_date,
                "manual_confirmation_applied": bool(record.manual_confirmation_applied),
                "manual_input_name": record.manual_input_name,
                "manual_input_basis": record.manual_input_basis,
                "source_contexts": len(record.contexts),
                "source_attached_tables": len(record.attached_tables),
            }
            for record in order.records
        ],
    }
    return json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n"


def generate_order_artifacts(order: SqlOrder, output_dir: Path) -> GeneratedOrderArtifacts:
    """Write SQL, BAT and diagnostic files for one order, without execution."""

    stem = _safe_stem(order.order_number)
    destination = Path(output_dir)
    sql_path = destination / f"iqc_draft_{stem}.sql"
    bat_path = destination / f"iqc_import_{stem}.bat"
    diagnostic_path = destination / f"iqc_draft_{stem}.diagnostic.json"
    sql_text = render_order_sql(order)
    # A UTF-8 BOM keeps the Chinese relative runtime path readable when a
    # legacy Windows cmd.exe opens the generated wrapper.  The BOM is part of
    # the hashed artifact and is validated before writing.
    bat_text = "\ufeff" + render_order_bat(order.order_number)
    validate_generated_bat(bat_text)
    _atomic_write(sql_path, sql_text)
    _atomic_write(bat_path, bat_text)
    diagnostic_text = render_order_diagnostic(order, sql_path, bat_path, sql_text, bat_text)
    _atomic_write(diagnostic_path, diagnostic_text)
    return GeneratedOrderArtifacts(order.order_number, sql_path, bat_path, diagnostic_path)


def generate_placeholder_sql(order_number: str, output_path: Path) -> None:
    """Backward-compatible marker; resolved records are required for DML."""

    _safe_stem(order_number)
    text = (
        "-- WO-07 PREPARED compatibility marker; resolved records are required.\n"
        "-- No target DML is present in this marker.\n"
        f"-- order_number={_sql_string(order_number)}\n"
    )
    _atomic_write(Path(output_path), text)
