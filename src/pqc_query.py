"""SELECT-only MySQL repository for PQC process-end candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping, Sequence

from .audit_logger import redact_log_text
from .mysql_query import MySqlQueryError, ReadonlyMysqlClient
from .pqc_models import (
    PQC_TEMPLATE_SPEC_BY_KEY,
    PQC_TEMPLATE_SPECS_BY_NAME,
    PqcDraftRecord,
    PqcFormalRecord,
    PqcQuerySnapshot,
    PqcTemplate,
    PqcTool,
)


# 仅保留旧审计/外部导入兼容；运行时模板加载严格按精确 procedure_name，绝不按这些 code 回退。
PQC_TEMPLATE_CODES = (
    "DEMO-ICS-DEMOFORM-001",
    "DEMO-ICS-DEMOFORM-002",
    "DEMO-ICS-DEMOFORM-003",
    "DEMO-ICS-DEMOFORM-004",
    "DEMO-ICS-DEMOFORM-005",
)
PQC_TEMPLATE_NAMES = tuple(dict.fromkeys(item.procedure_name for item in PQC_TEMPLATE_SPECS_BY_NAME.values()))
# MySQL 8.0 may assign a different default collation to ``CONVERT(... USING
# utf8mb4)`` than the legacy database columns.  Force both sides of every
# user-facing string comparison to the same broadly supported collation.
PQC_TEXT_COLLATION = "utf8mb4_unicode_ci"


class PqcQueryError(MySqlQueryError):
    pass


PqcQueryEventCallback = Callable[[Mapping[str, object]], None]


def _text(row: Sequence[object], index: int) -> str:
    if index >= len(row) or row[index] is None:
        return ""
    return str(row[index]).strip()


def _utf8_literal(value: object, field_name: str) -> str:
    """Encode a business value as a MySQL UTF-8 expression without quoting."""

    text = "" if value is None else str(value).strip()
    if not text:
        raise PqcQueryError(f"{field_name}不能为空")
    if any(ord(char) < 32 for char in text):
        raise PqcQueryError(f"{field_name}包含控制字符")
    if len(text) > 512:
        raise PqcQueryError(f"{field_name}过长")
    return (
        "(CONVERT(0x"
        + text.encode("utf-8").hex()
        + " USING utf8mb4) COLLATE "
        + PQC_TEXT_COLLATION
        + ")"
    )


def _collated_text(expression: str) -> str:
    """Normalize a database text expression before comparing it to literals."""

    return f"(CONVERT({expression} USING utf8mb4) COLLATE {PQC_TEXT_COLLATION})"


def _map_template(row: Sequence[object]) -> PqcTemplate:
    if len(row) < 7:
        raise PqcQueryError("PQC 模板只读结果列数不足")
    values = tuple(_text(row, index) for index in range(7))
    # 名称到业务键是只读配置映射；数据库 ID/版本仍由查询结果提供。
    spec = PQC_TEMPLATE_SPECS_BY_NAME.get(values[2])
    return PqcTemplate(*values, template_key=spec.template_key if spec else "")


def _template_key_for_name(name: str) -> str:
    spec = PQC_TEMPLATE_SPECS_BY_NAME.get(name)
    return spec.template_key if spec else ""


def _map_formal(row: Sequence[object]) -> PqcFormalRecord:
    if len(row) < 7:
        raise PqcQueryError("PQC 正式记录只读结果列数不足")
    return PqcFormalRecord(
        _text(row, 0), _text(row, 1), _text(row, 2), _text(row, 3),
        _text(row, 5), _text(row, 6), _template_key_for_name(_text(row, 4)), _text(row, 4),
    )


def _map_draft(row: Sequence[object]) -> PqcDraftRecord:
    if len(row) < 8:
        raise PqcQueryError("PQC 草稿只读结果列数不足")
    return PqcDraftRecord(
        _text(row, 0), _text(row, 1), _text(row, 2), _text(row, 3), _text(row, 5),
        _text(row, 6), _text(row, 7), _template_key_for_name(_text(row, 4)), _text(row, 4),
    )


def _map_tool(row: Sequence[object]) -> PqcTool:
    if len(row) < 4:
        raise PqcQueryError("PQC 工器具只读结果列数不足")
    return PqcTool(*(_text(row, index) for index in range(4)))


@dataclass
class PqcReadonlyQueryService:
    client: ReadonlyMysqlClient
    event_callback: PqcQueryEventCallback | None = None
    # Retained for constructor compatibility with older integrations. SS is
    # now generated from the inspection type and is never read from this map.
    template_suffix_map: Mapping[str, str] = field(default_factory=dict)

    def set_event_callback(self, callback: PqcQueryEventCallback | None) -> None:
        self.event_callback = callback

    def _emit(self, name: str, status: str, detail: str, **fields: object) -> None:
        if self.event_callback is None:
            return
        try:
            self.event_callback({"name": name, "status": status, "detail": detail, **fields})
        except Exception:
            return

    def _execute(self, sql: str, label: str) -> list[list[str]]:
        self._emit("pqc_readonly_query_start", "STARTED", "开始执行 PQC 只读 SELECT", query_label=label)
        try:
            rows = self.client.execute(sql, query_label=label)
        except Exception as exc:
            detail = redact_log_text(str(exc))
            self._emit(
                "pqc_readonly_query_error",
                "ERROR",
                f"PQC 只读 SELECT 失败：{type(exc).__name__}",
                query_label=label,
                safe_detail=detail,
            )
            if isinstance(exc, PqcQueryError):
                raise
            raise PqcQueryError(
                f"PQC 只读查询失败：{detail}",
                query_stage=label,
                exit_code=getattr(exc, "exit_code", None),
                safe_detail=detail,
            ) from exc
        self._emit(
            "pqc_readonly_query_success",
            "SUCCESS",
            "PQC 只读 SELECT 执行成功",
            query_label=label,
            row_count=len(rows),
        )
        return rows

    def load_templates(self) -> tuple[PqcTemplate, ...]:
        names = ", ".join(_utf8_literal(name, "模板名称") for name in PQC_TEMPLATE_NAMES)
        procedure_name = _collated_text("TRIM(COALESCE(procedure_name, ''))")
        sql = (
            "SELECT id, TRIM(COALESCE(procedure_code, '')), TRIM(COALESCE(procedure_name, '')), "
            "COALESCE(procedure_version, ''), COALESCE(is_lasted_version, ''), "
            "COALESCE(processstate, ''), COALESCE(is_frozen, '') "
            "FROM qcauto_procedure_model_processEnd "
            f"WHERE {procedure_name} IN ({names}) AND is_lasted_version = '1' "
            "AND processstate = 2 AND is_frozen = 0 ORDER BY procedure_name, procedure_version DESC, id DESC;"
        )
        result: list[PqcTemplate] = []
        for row in self._execute(sql, "pqc_templates"):
            template = _map_template(row)
            result.append(
                PqcTemplate(
                    template.model_id, template.procedure_code, template.procedure_name,
                    template.procedure_version, template.is_lasted_version,
                    template.processstate, template.is_frozen, template.template_key,
                    "",
                )
            )
        return tuple(result)

    def load_formal_records(self, project_number: str) -> tuple[PqcFormalRecord, ...]:
        project = _utf8_literal(project_number, "项目编号")
        project_column = _collated_text("TRIM(COALESCE(r.project_number, ''))")
        sql = (
            "SELECT r.id, TRIM(COALESCE(r.project_number, '')), TRIM(COALESCE(r.item_code, '')), "
            "TRIM(COALESCE(m.procedure_code, '')), TRIM(COALESCE(m.procedure_name, '')), "
            "COALESCE(r.processstate, ''), TRIM(COALESCE(r.trigger_basis, '')) "
            "FROM qcauto_procedure_record_processEnd r "
            "LEFT JOIN qcauto_procedure_model_processEnd m ON m.id = r.procedure_model_id "
            f"WHERE {project_column} = {project} "
            "ORDER BY r.item_code, m.procedure_code, r.id;"
        )
        return tuple(_map_formal(row) for row in self._execute(sql, "pqc_formal_records"))

    def load_drafts(self, project_number: str) -> tuple[PqcDraftRecord, ...]:
        project = _utf8_literal(project_number, "项目编号")
        project_column = _collated_text("TRIM(COALESCE(d.project_number, ''))")
        sql = (
            "SELECT d.id, TRIM(COALESCE(d.project_number, '')), TRIM(COALESCE(d.item_code, '')), "
            "TRIM(COALESCE(m.procedure_code, '')), TRIM(COALESCE(m.procedure_name, '')), "
            "TRIM(COALESCE(d.trigger_basis, '')), TRIM(COALESCE(d.qc_engineer, '')), "
            "DATE_FORMAT(d.create_datetime, '%Y-%m-%d %H:%i:%s') "
            "FROM qcauto_procedure_draft_processEnd d "
            "LEFT JOIN qcauto_procedure_model_processEnd m ON m.id = d.procedure_model_id "
            f"WHERE {project_column} = {project} "
            "ORDER BY d.item_code, m.procedure_code, d.id;"
        )
        return tuple(_map_draft(row) for row in self._execute(sql, "pqc_drafts"))

    def load_tools(self) -> tuple[PqcTool, ...]:
        sql = (
            "SELECT t.id, TRIM(COALESCE(t.tool_itemname, '')), "
            "TRIM(COALESCE(NULLIF(t.tool_itemid, ''), NULLIF(t.tool_id, ''), '')), "
            "COALESCE(DATE_FORMAT(c.tool_measvaliddate, '%Y-%m-%d'), '') "
            "FROM base_tool_info t LEFT JOIN base_tool_cabl c ON c.id = ("
            "SELECT c2.id FROM base_tool_cabl c2 WHERE c2.tool_id_id = t.id "
            "ORDER BY COALESCE(c2.tool_measdate, '1000-01-01') DESC, c2.id DESC LIMIT 1"
            ") WHERE TRIM(COALESCE(t.tool_itemname, '')) <> '' "
            "ORDER BY t.tool_itemname, t.tool_itemid, t.id;"
        )
        return tuple(_map_tool(row) for row in self._execute(sql, "pqc_tools"))

    def load_project_snapshot(self, project_number: str, *, include_tools: bool = True) -> PqcQuerySnapshot:
        return PqcQuerySnapshot(
            templates=self.load_templates(),
            formal_records=self.load_formal_records(project_number),
            drafts=self.load_drafts(project_number),
            tools=self.load_tools() if include_tools else (),
        )

    def matching_drafts(
        self,
        project_number: str,
        item_code: str,
        template_code: str,
        trigger_basis: str,
        qc_engineer: str,
        *,
        template_key: str = "",
        created_not_before: datetime | None = None,
    ) -> tuple[PqcDraftRecord, ...]:
        """Exact immediate readback used around every attempted POST."""

        project = _utf8_literal(project_number, "项目编号")
        device = _utf8_literal(item_code, "设备编号")
        template = _utf8_literal(template_code, "模板编码")
        if not template_key:
            raise PqcQueryError("草稿回查必须提供复合模板键")
        trigger = _utf8_literal(trigger_basis, "表单编号")
        engineer = _utf8_literal(qc_engineer, "质检工程师")
        project_column = _collated_text("TRIM(COALESCE(d.project_number, ''))")
        item_column = _collated_text("TRIM(COALESCE(d.item_code, ''))")
        template_column = _collated_text("TRIM(COALESCE(m.procedure_code, ''))")
        trigger_column = _collated_text("TRIM(COALESCE(d.trigger_basis, ''))")
        engineer_column = _collated_text("TRIM(COALESCE(d.qc_engineer, ''))")
        time_clause = ""
        key_clause = ""
        if template_key:
            spec = PQC_TEMPLATE_SPEC_BY_KEY.get(template_key)
            if spec is None:
                raise PqcQueryError("未知复合模板键")
            procedure_name_column = _collated_text("TRIM(COALESCE(m.procedure_name, ''))")
            key_clause = f"AND {procedure_name_column} = {_utf8_literal(spec.procedure_name, '模板名称')} "
        if created_not_before is not None:
            if not isinstance(created_not_before, datetime):
                raise PqcQueryError("草稿回查时间窗口必须是 datetime")
            lower = _utf8_literal(
                created_not_before.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
                "草稿回查起始时间",
            )
            time_clause = f"AND d.create_datetime >= {lower} "
        sql = (
            "SELECT d.id, TRIM(COALESCE(d.project_number, '')), TRIM(COALESCE(d.item_code, '')), "
            "TRIM(COALESCE(m.procedure_code, '')), TRIM(COALESCE(m.procedure_name, '')), "
            "TRIM(COALESCE(d.trigger_basis, '')), TRIM(COALESCE(d.qc_engineer, '')), "
            "DATE_FORMAT(d.create_datetime, '%Y-%m-%d %H:%i:%s') "
            "FROM qcauto_procedure_draft_processEnd d "
            "LEFT JOIN qcauto_procedure_model_processEnd m ON m.id = d.procedure_model_id "
            f"WHERE {project_column} = {project} "
            f"AND {item_column} = {device} "
            f"AND {template_column} = {template} "
            f"{key_clause}"
            f"AND {trigger_column} = {trigger} "
            f"AND {engineer_column} = {engineer} "
            f"{time_clause}ORDER BY d.create_datetime, d.id;"
        )
        return tuple(_map_draft(row) for row in self._execute(sql, "pqc_matching_drafts"))


__all__ = [
    "PQC_TEMPLATE_CODES",
    "PQC_TEMPLATE_NAMES",
    "PqcQueryError",
    "PqcReadonlyQueryService",
]
