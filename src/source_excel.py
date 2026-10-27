from __future__ import annotations

import re
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from .models import OrderTask, SOURCE_HEADERS, SourceRow, canonicalize_m_code_separators


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS, "pr": PKG_REL_NS}


class SourceExcelError(ValueError):
    """源 Excel 不符合严格布局时抛出的错误。"""


def parse_order_numbers(raw: str) -> list[str]:
    """按英文分号拆分并按输入顺序去重。"""

    result: OrderedDict[str, None] = OrderedDict()
    for token in raw.split(";"):
        value = token.strip()
        if value:
            result.setdefault(value, None)
    return list(result)


def _column_index(cell_ref: str) -> int:
    letters = re.match(r"([A-Za-z]+)", cell_ref)
    if not letters:
        raise SourceExcelError(f"无法解析单元格地址：{cell_ref}")
    index = 0
    for char in letters.group(1).upper():
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    result: list[str] = []
    for item in root.findall("m:si", NS):
        result.append("".join(node.text or "" for node in item.findall(".//m:t", NS)))
    return result


def _cell_value(cell: ET.Element, shared: list[str]) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//m:t", NS))
    value = cell.find("m:v", NS)
    if value is None:
        return ""
    text = value.text or ""
    if cell_type == "s":
        try:
            return shared[int(text)]
        except (ValueError, IndexError) as exc:
            raise SourceExcelError(f"共享字符串索引无效：{text}") from exc
    if cell_type == "b":
        return text == "1"
    return text


def _rows_from_sheet(zf: zipfile.ZipFile, target: str) -> Iterable[tuple[int, list[Any]]]:
    target = target.lstrip("/")
    if not target.startswith("xl/"):
        target = "xl/" + target
    root = ET.fromstring(zf.read(target))
    shared = _shared_strings(zf)
    for row in root.findall(".//m:sheetData/m:row", NS):
        row_number = int(row.attrib.get("r", "0"))
        values: list[Any] = []
        for cell in row.findall("m:c", NS):
            index = _column_index(cell.attrib.get("r", ""))
            while len(values) <= index:
                values.append("")
            values[index] = _cell_value(cell, shared)
        yield row_number, values


def _workbook_sheets(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    targets = {item.attrib["Id"]: item.attrib["Target"] for item in rels.findall("pr:Relationship", NS)}
    result: list[tuple[str, str]] = []
    for sheet in workbook.findall("m:sheets/m:sheet", NS):
        name = sheet.attrib["name"]
        rel_id = sheet.attrib[f"{{{REL_NS}}}id"]
        result.append((name, targets[rel_id]))
    return result


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _is_monthly_sheet(rows: dict[int, list[Any]]) -> bool:
    row2 = rows.get(2, [])
    row3 = rows.get(3, [])
    if any(_text(value) for value in row2[:17]):
        return False
    first_fifteen = [_text(value) for value in row3[:15]]
    if first_fifteen != list(SOURCE_HEADERS[:15]):
        return False
    # 实际月度表的 P3 可能是空白；历史静态交接曾将其记作“空列”。
    # 新版月度表也可能明确写成“M码结束”，三者都表示同一个 P 列位置，
    # 但新版名称能让使用者知道该列会作为 M 码范围结束值读取。
    last_header = _text(row3[15] if len(row3) > 15 else "")
    if last_header not in ("", "空列", "M码结束"):
        return False
    # 2026 年历史汇总表的 Q3 曾使用“执行者”；两者都表示同一张月度
    # 数据表的状态列，不改变前 15 列和 P3 空列/“空列”约束。
    return _text(row3[16] if len(row3) > 16 else "") in ("未完成", "执行者")


def read_order_tasks(path: Path, order_numbers: list[str]) -> dict[str, OrderTask]:
    """读取符合严格月度布局的工作表，返回逐单任务。"""

    wanted = set(order_numbers)
    tasks = {order: OrderTask(order_number=order) for order in order_numbers}
    if not wanted:
        return tasks
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise SourceExcelError(f"仅支持源 .xlsx/.xlsm：{path}")
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise SourceExcelError(f"无法打开源 Excel：{path}") from exc
    with zf:
        matched_sheet_count = 0
        for sheet_name, target in _workbook_sheets(zf):
            rows = dict(_rows_from_sheet(zf, target))
            is_monthly = _is_monthly_sheet(rows)
            if not is_monthly:
                continue
            matched_sheet_count += 1
            for row_number, values in sorted(rows.items()):
                if row_number < 4:
                    continue
                order = _text(values[0] if values else "")
                if order not in wanted:
                    continue
                # Windchill/SQL 使用 D 列“物料编号”作为业务 item_code。
                # C 列“物项编码”仍随 values[:16] 保留在 SourceRow 中，供模板及审计追溯；
                # 不得将 C 列的源系统编码误送给 Windchill。
                item_code = _text(values[3] if len(values) > 3 else "")
                # 月度输入的 O/P 列分别是 M 码开始/结束。旧表的 O 列也可能
                # 保存非 M 序列值，或把完整范围写在 O 列；这两类值继续由
                # combination_aggregator 的兼容路径处理。只有明确填写 P，
                # 或 O 是单个 M 码（含大小写错误的 M 前缀）时，才转为显式
                # 起止字段，让聚合层执行严格 M+11 位校验。
                sequence_start = _text(values[14] if len(values) > 14 else "")
                sequence_end = _text(values[15] if len(values) > 15 else "")
                # 保存供应商单元格的原始文本；TK 空值的 ``NA`` 只在聚合/
                # 输出副本中生效，读取阶段不覆盖源值，便于 provenance 回读。
                raw_supplier = "" if len(values) <= 5 or values[5] is None else str(values[5])
                explicit_sequence = bool(sequence_end) or (
                    bool(sequence_start)
                    and sequence_start[:1] in {"M", "m"}
                    and not any(separator in sequence_start for separator in ("~", "～", "至", "-", "/", "、", ",", "，", ";", "；"))
                )
                source_values = list(values[:16])
                if len(source_values) > 14:
                    # 读取层即把含 M 码的多值文本规范成 ``/``；不含
                    # 严格 M 码的历史序列值保持原样。该字段只在单元格
                    # 文本中归一，不会被当作文件系统路径。
                    source_values[14] = canonicalize_m_code_separators(source_values[14])
                tasks[order].rows.append(
                    SourceRow(
                        order_number=order,
                        item_code=item_code,
                        sheet_name=sheet_name,
                        source_row=row_number,
                        values=tuple(source_values),
                        sequence_start=sequence_start if explicit_sequence and sequence_start else None,
                        sequence_end=sequence_end if explicit_sequence and sequence_end else None,
                        raw_supplier=raw_supplier,
                    )
                )
        if matched_sheet_count == 0:
            raise SourceExcelError("未找到符合严格月度布局的工作表")
    for task in tasks.values():
        if not task.rows:
            task.errors.append(f"未找到送检单号：{task.order_number}")
        sheets = {row.sheet_name for row in task.rows}
        if len(sheets) > 1:
            task.errors.append("同一送检单号命中多个工作表")
    return tasks
