"""Strict, dependency-free reader for the frozen PQC A:J workbook."""

from __future__ import annotations

import unicodedata
import zipfile
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from .pqc_models import PqcProject, PqcSourceRow, PqcWorkbook
from .source_excel import NS, SourceExcelError, _column_index, _rows_from_sheet, _workbook_sheets


PQC_HEADERS = (
    "序号",
    "派工单",
    "项目名称",
    "项目编号",
    "盘柜名称/编号",
    "序列码/流转码",
    "质检工程师",
    "特殊情况",
    "NCR",
    "类别",
)
_REQUIRED_COLUMNS = ((1, "派工单"), (2, "项目名称"), (3, "项目编号"), (4, "盘柜名称/编号"), (5, "序列码/流转码"), (6, "质检工程师"))


class PqcExcelError(ValueError):
    """The workbook cannot be interpreted without guessing its contract."""


def _text(value: object) -> str:
    if value is None:
        return ""
    # Business text is source data, not a matching key.  Only trim its edges;
    # NFKC would silently change full-width identifiers and therefore violates
    # the frozen "trim but otherwise preserve" Excel contract.
    return str(value).strip()


def _header(value: object) -> str:
    # Header matching is metadata normalization, unlike B:I business text.
    return "".join(unicodedata.normalize("NFKC", _text(value)).split())


def _matching_header_row(rows: Iterable[tuple[int, list[object]]]) -> tuple[int, bool] | None:
    for row_number, values in rows:
        if row_number > 30:
            break
        normalized = tuple(_header(value) for value in values[:10])
        if normalized[:9] != tuple(_header(value) for value in PQC_HEADERS[:9]):
            continue
        # J 是新分类契约，缺失时必须阻断，不回退到旧五类规则。
        if len(normalized) <= 9 or normalized[9] != _header("类别"):
            continue
        if len(normalized) > 8 and normalized[8] not in {"", "NCR"}:
            continue
        return row_number, normalized[8] == "NCR"
    return None


def _uncached_formula_cells(
    archive: zipfile.ZipFile,
    target: str,
    *,
    after_row: int,
) -> tuple[str, ...]:
    """Find A:J formulas which have no saved value in the OOXML file.

    This reader intentionally does not calculate formulas.  A missing cache is
    indistinguishable from an empty result, so the only safe action is to ask
    the operator to calculate and save the workbook in Excel/WPS first.
    """

    member = target.lstrip("/")
    if not member.startswith("xl/"):
        member = "xl/" + member
    root = ET.fromstring(archive.read(member))
    result: list[str] = []
    for cell in root.findall(".//m:sheetData/m:row/m:c", NS):
        reference = cell.attrib.get("r", "")
        digits = "".join(char for char in reference if char.isdigit())
        row_number = int(digits or "0")
        if row_number <= after_row:
            continue
        try:
            column = _column_index(reference)
        except SourceExcelError:
            continue
        if not 1 <= column <= 10 or cell.find("m:f", NS) is None:
            continue
        value = cell.find("m:v", NS)
        if value is None or value.text is None:
            result.append(reference or f"row-{row_number}")
    return tuple(result)


def _row_from_values(row_number: int, values: list[object]) -> tuple[PqcSourceRow | None, tuple[str, ...]]:
    cells = [_text(values[index] if index < len(values) else "") for index in range(10)]
    if not any(cells[1:]):
        return None, ()
    missing = tuple(label for index, label in _REQUIRED_COLUMNS if not cells[index])
    if missing:
        return None, (f"Excel 第 {row_number} 行缺少必填字段：{', '.join(missing)}",)
    # -JD- 规则优先于 J；非 -JD- 空 J 保留设备行并阻断候选，不能静默丢失审计对象。
    category = cells[9]
    row_errors: tuple[str, ...] = ()
    if "-JD-" not in cells[4] and not category:
        row_errors = (f"Excel 第 {row_number} 行缺少必填字段：类别",)
    return (
        PqcSourceRow(
            source_rows=(row_number,),
            workorder_number=cells[1],
            project_name=cells[2],
            project_number=cells[3],
            item_code=cells[4],
            sequence_code=cells[5],
            qc_engineer=cells[6],
            special_case=cells[7],
            ncr_code=cells[8],
            category=category,
            errors=row_errors,
        ),
        (),
    )


def _business_values(row: PqcSourceRow) -> tuple[str, ...]:
    return (
        row.workorder_number,
        row.project_name,
        row.project_number,
        row.item_code,
        row.sequence_code,
        row.qc_engineer,
        row.special_case,
        row.ncr_code,
        row.category,
    )


def _merge_devices(rows: Iterable[PqcSourceRow]) -> tuple[PqcSourceRow, ...]:
    groups: OrderedDict[tuple[str, str], list[PqcSourceRow]] = OrderedDict()
    for row in rows:
        groups.setdefault(row.key, []).append(row)

    merged: list[PqcSourceRow] = []
    for key, group in groups.items():
        first = group[0]
        source_rows = tuple(sorted(row.first_source_row for row in group))
        # 机柜由 -JD- 强制分类，J 的差异只保留审计原值，不构成冲突。
        variants = {_business_values(row) for row in group}
        if "-JD-" in first.item_code:
            variants = {
                tuple(value for index, value in enumerate(values) if index != 8)
                for values in variants
            }
        if len(variants) == 1:
            merged.append(replace(first, source_rows=source_rows))
            continue
        fields = (
            ("派工单", lambda item: item.workorder_number),
            ("项目名称", lambda item: item.project_name),
            ("序列码/流转码", lambda item: item.sequence_code),
            ("质检工程师", lambda item: item.qc_engineer),
            ("特殊情况", lambda item: item.special_case),
            ("NCR", lambda item: item.ncr_code),
        )
        if "-JD-" not in first.item_code:
            fields = (*fields, ("类别", lambda item: item.category))
        conflicts = [label for label, getter in fields if len({getter(item) for item in group}) > 1]
        detail = f"同一项目/设备重复行字段冲突：{', '.join(conflicts)}（Excel 行 {', '.join(map(str, source_rows))}）"
        merged.append(replace(first, source_rows=source_rows, errors=(detail,)))

    names_by_project: dict[str, set[str]] = {}
    for row in merged:
        names_by_project.setdefault(row.project_number, set()).add(row.project_name)
    result: list[PqcSourceRow] = []
    for row in merged:
        names = names_by_project[row.project_number]
        if len(names) > 1:
            detail = f"项目编号 {row.project_number} 对应多个项目名称，不能静默选择"
            result.append(replace(row, errors=tuple((*row.errors, detail))))
        else:
            result.append(row)
    return tuple(result)


def read_pqc_workbook(path: Path) -> PqcWorkbook:
    """Read all PQC devices without filtering an engineer or project."""

    source = Path(path)
    if source.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise PqcExcelError(f"PQC 仅支持 .xlsx/.xlsm：{source}")
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PqcExcelError(f"无法打开 PQC Excel：{source}") from exc

    parsed: list[PqcSourceRow] = []
    errors: list[str] = []
    matched_sheets = 0
    try:
        with archive:
            for sheet_name, target in _workbook_sheets(archive):
                try:
                    sheet_rows = list(_rows_from_sheet(archive, target))
                except SourceExcelError as exc:
                    raise PqcExcelError(str(exc)) from exc
                header_match = _matching_header_row(sheet_rows)
                if header_match is None:
                    continue
                header_row, has_ncr_header = header_match
                matched_sheets += 1
                uncached = _uncached_formula_cells(archive, target, after_row=header_row)
                if uncached:
                    preview = ", ".join(uncached[:10])
                    raise PqcExcelError(
                        f"{sheet_name}：公式单元格没有已保存的计算值（{preview}），"
                        "请先在 Excel/WPS 中计算并保存"
                    )
                for row_number, values in sheet_rows:
                    if row_number <= header_row:
                        continue
                    if not has_ncr_header and len(values) > 8 and _text(values[8]):
                        raise PqcExcelError(
                            f"{sheet_name}：Excel 第 {row_number} 行 I 列有值，但 I 列表头不是 NCR"
                        )
                    row, row_errors = _row_from_values(row_number, values)
                    if row is not None:
                        parsed.append(row)
                    errors.extend(f"{sheet_name}：{detail}" for detail in row_errors)
    except PqcExcelError:
        raise
    except (KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise PqcExcelError(f"PQC Excel 结构损坏或公式没有已保存值：{source}") from exc

    if matched_sheets == 0:
        raise PqcExcelError("未找到符合 PQC A:J 冻结表头（含类别列）的工作表")
    if not parsed:
        raise PqcExcelError("PQC Excel 没有可读取的 B:G 完整数据行")
    return PqcWorkbook(str(source), _merge_devices(parsed), tuple(errors))


def projects_for_engineer(workbook: PqcWorkbook, engineer_name: str) -> tuple[PqcProject, ...]:
    """Return exact-name projects in stable source-row order."""

    selected_name = _text(engineer_name)
    grouped: OrderedDict[str, list[PqcSourceRow]] = OrderedDict()
    for row in sorted(workbook.rows, key=lambda item: item.first_source_row):
        if row.qc_engineer != selected_name:
            continue
        grouped.setdefault(row.project_number, []).append(row)
    projects: list[PqcProject] = []
    for project_number, rows in grouped.items():
        names = {row.project_name for row in rows}
        project_errors = tuple(
            sorted({error for row in rows for error in row.errors if "项目编号" in error and "多个项目名称" in error})
        )
        name = rows[0].project_name if len(names) == 1 else " / ".join(sorted(names))
        projects.append(PqcProject(project_number, name, tuple(rows), project_errors))
    return tuple(projects)


__all__ = [
    "PQC_HEADERS",
    "PqcExcelError",
    "projects_for_engineer",
    "read_pqc_workbook",
]
