from __future__ import annotations

import copy
import zipfile
from pathlib import Path
from typing import Iterable, Mapping
from xml.etree import ElementTree as ET

from .combination_aggregator import CombinationAggregationError, aggregate_task
from .models import (
    DraftRow,
    Engineer,
    InspectionCombination,
    OrderDraft,
    OrderTask,
    WindchillName,
    effective_supplier_for_order,
    normalize_procedure_name,
)
from .windchill_adapter import assert_success


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS, "pr": PKG_REL_NS}


class TemplateWriterError(ValueError):
    """模板缺失、业务对象不完整或输出路径不安全。"""


def assemble_order_draft(
    task: OrderTask,
    windchill_results: Iterable[WindchillName],
    engineer: Engineer,
    combinations: Iterable[InspectionCombination] | None = None,
) -> OrderDraft:
    """将组合对象、Windchill 名称和工程师组装为单号级结果对象。

    ``combinations`` 由生成 provider 显式传入；省略时仍兼容旧版不完整的
    三列测试夹具，完整月度源行则统一走 VBA 聚合规则。
    """

    if task.errors:
        raise TemplateWriterError(f"送检单号 {task.order_number} 存在未解决异常：{'；'.join(task.errors)}")
    if not task.rows:
        raise TemplateWriterError(f"送检单号 {task.order_number} 没有源数据行")
    if not engineer.display_name.strip():
        raise TemplateWriterError("质检工程师显示名称为空")

    results = list(windchill_results)
    if combinations is None:
        if all(len(row.values) < 10 for row in task.rows):
            # 仅保留旧版最小模板夹具的兼容路径；真实月度行一旦包含数量列，
            # 聚合失败必须显式报错，不能回退到逐行写出。
            combinations = tuple(
                InspectionCombination(
                    order_number=task.order_number,
                    item_code=row.item_code.strip(),
                    # 兼容旧版最小夹具也必须沿用 TK 供应商默认规则；
                    # 完整月度表则已在 aggregate_task 中完成同一归一化。
                    source=_effective_supplier_source(row, task.order_number),
                    quantity=0,
                    relative_line_numbers=(index,),
                    trigger_basis=f"{task.order_number}-{index}",
                    sequence_code=None,
                    source_rows=(row,),
                )
                for index, row in enumerate(task.rows, start=1)
            )
        else:
            try:
                combinations = aggregate_task(task)
            except CombinationAggregationError as exc:
                raise TemplateWriterError(str(exc)) from exc
    # provider 注入的组合也可能来自旧版调用方，不能因绕过
    # aggregate_task 而让 Excel 与 SQL 的供应商语义漂移。归一化只创建
    # 不修改源行的副本，raw_supplier/supplier_defaulted 继续可审计。
    combinations = tuple(
        _with_effective_supplier(combination)
        for combination in combinations
    )
    item_codes = [combination.item_code.strip() for combination in combinations]
    if any(not code for code in item_codes):
        raise TemplateWriterError(f"送检单号 {task.order_number} 存在空物料编码")
    # 校验去重后的 Windchill 返回，允许一个物料在源表出现多行。
    expected_codes = list(dict.fromkeys(item_codes))
    assert_success(results, expected_codes)
    names = {result.item_code: normalize_procedure_name(result.name) for result in results}
    draft_rows = tuple(
        DraftRow(
            source=combination.source,
            procedure_name=names[combination.item_code.strip()],
            qc_engineer=engineer.display_name.strip(),
        )
        for combination in combinations
    )
    return OrderDraft(order_number=task.order_number, rows=draft_rows)


def _effective_supplier_source(source, order_number: str):
    """返回带 TK effective 供应商的 SourceRow 副本。

    保持该小 helper 在模板层而不修改原始 ``SourceRow``，以便调用方仍可
    使用原始 Excel 值做回读和审计；类型注解故意省略以兼容 Python 3.8
    便携运行时及旧版测试夹具。
    """

    from dataclasses import replace

    raw = source.raw_supplier
    if raw is None or (
        not raw
        and not source.supplier_defaulted
        and len(source.values) > 5
        and source.values[5] is not None
        and str(source.values[5]).strip()
    ):
        raw = "" if len(source.values) <= 5 or source.values[5] is None else str(source.values[5])
    effective, raw_text, defaulted = effective_supplier_for_order(order_number, raw)
    values = list(source.values)
    while len(values) <= 5:
        values.append("")
    values[5] = effective
    return replace(source, values=tuple(values), raw_supplier=raw_text, supplier_defaulted=defaulted)


def _with_effective_supplier(combination: InspectionCombination) -> InspectionCombination:
    """为外部注入组合统一 Excel 侧供应商值。"""

    from dataclasses import replace

    source = _effective_supplier_source(combination.source, combination.order_number)
    if source == combination.source:
        return combination
    return replace(combination, source=source)


def output_path_for_order(output_dir: Path, order_number: str) -> Path:
    """生成单号输出路径，并拒绝 Windows 文件名/路径穿越字符。"""

    value = order_number.strip()
    forbidden = '<>:"/\\|?*'
    if not value or value in {".", ".."} or any(char in forbidden or ord(char) < 32 for char in value):
        raise TemplateWriterError(f"送检单号不能作为安全文件名：{order_number!r}")
    if value.endswith(".") or value.endswith(" "):
        raise TemplateWriterError(f"送检单号不能以点号或空格结尾：{order_number!r}")
    return output_dir / f"iqc_draft_{value}.xlsx"


def write_order_draft(template_path: Path, output_path: Path, draft: OrderDraft) -> Path:
    """复制模板并只写 A:P、R:S，保留 Q 列及模板样式/页面设置。"""

    template = template_path.resolve()
    output = output_path.resolve()
    if not template.exists():
        raise TemplateWriterError(f"模板不存在：{template}")
    if template == output:
        raise TemplateWriterError("输出路径不能覆盖模板原件")
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(template, "r") as source_zip:
            entries = {info.filename: source_zip.read(info.filename) for info in source_zip.infolist()}
    except (OSError, zipfile.BadZipFile) as exc:
        raise TemplateWriterError(f"无法读取模板：{template}") from exc

    sheet_name, sheet_path = _find_target_sheet(entries)
    if sheet_name != "进货检验草稿基本信息":
        raise TemplateWriterError(f"模板结果工作表名称不符：{sheet_name}")
    root = ET.fromstring(entries[sheet_path])
    sheet_data = root.find("m:sheetData", NS)
    if sheet_data is None:
        raise TemplateWriterError("模板缺少 sheetData")

    rows = {int(row.attrib["r"]): row for row in sheet_data.findall("m:row", NS) if row.attrib.get("r", "").isdigit()}
    if not rows:
        raise TemplateWriterError("模板没有可写入的行")
    template_row = rows.get(29)
    if template_row is None:
        template_row = rows[max(rows)]
    last_data_row = 3 + len(draft.rows)
    for row_number in range(4, max(max(rows), last_data_row) + 1):
        row = rows.get(row_number)
        if row is None:
            row = copy.deepcopy(template_row)
            _renumber_row(row, row_number)
            sheet_data.append(row)
            rows[row_number] = row
        _clear_business_cells(row)

    for offset, draft_row in enumerate(draft.rows, start=4):
        row = rows[offset]
        values = list(draft_row.source.values[:16])
        values.extend([""] * (16 - len(values)))
        for column, value in zip(_columns(1, 16), values):
            _set_cell_value(row, column, value)
        _set_cell_value(row, "R", draft_row.procedure_name)
        _set_cell_value(row, "S", draft_row.qc_engineer)

    entries[sheet_path] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as target_zip:
            for name, content in entries.items():
                target_zip.writestr(name, content)
        # 允许覆盖已有输出，但以替换方式避免留下半写文件。
        temporary.replace(output)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise TemplateWriterError(f"无法写入结果文件：{output}") from exc
    return output


def _find_target_sheet(entries: Mapping[str, bytes]) -> tuple[str, str]:
    try:
        workbook = ET.fromstring(entries["xl/workbook.xml"])
        rels = ET.fromstring(entries["xl/_rels/workbook.xml.rels"])
    except (KeyError, ET.ParseError) as exc:
        raise TemplateWriterError("模板缺少有效 workbook 元数据") from exc
    targets = {item.attrib["Id"]: item.attrib["Target"] for item in rels.findall("pr:Relationship", NS)}
    sheets = workbook.findall("m:sheets/m:sheet", NS)
    if len(sheets) != 1:
        raise TemplateWriterError(f"Python 模板必须只有一个工作表，实际为 {len(sheets)} 个")
    sheet = sheets[0]
    rel_id = sheet.attrib.get(f"{{{REL_NS}}}id", "")
    target = targets.get(rel_id, "").lstrip("/")
    if not target.startswith("xl/"):
        target = "xl/" + target
    if target not in entries:
        raise TemplateWriterError(f"模板工作表文件不存在：{target}")
    return sheet.attrib.get("name", ""), target


def _renumber_row(row: ET.Element, row_number: int) -> None:
    row.set("r", str(row_number))
    for cell in row.findall("m:c", NS):
        reference = cell.attrib.get("r", "")
        letters = "".join(char for char in reference if char.isalpha())
        if letters:
            cell.set("r", f"{letters}{row_number}")


def _clear_business_cells(row: ET.Element) -> None:
    for cell in row.findall("m:c", NS):
        reference = cell.attrib.get("r", "")
        column = "".join(char for char in reference if char.isalpha()).upper()
        if column == "Q" or column == "" or (column not in set(_columns(1, 16)) and column not in {"R", "S"}):
            continue
        for child in list(cell):
            if child.tag in {f"{{{MAIN_NS}}}v", f"{{{MAIN_NS}}}is", f"{{{MAIN_NS}}}f"}:
                cell.remove(child)
        cell.attrib.pop("t", None)


def _set_cell_value(row: ET.Element, column: str, value: object) -> None:
    cell = _find_or_add_cell(row, column)
    for child in list(cell):
        if child.tag in {f"{{{MAIN_NS}}}v", f"{{{MAIN_NS}}}is", f"{{{MAIN_NS}}}f"}:
            cell.remove(child)
    if value is None or value == "":
        cell.attrib.pop("t", None)
        return
    if isinstance(value, bool):
        cell.set("t", "b")
        text = "1" if value else "0"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        cell.attrib.pop("t", None)
        text = str(value)
    else:
        cell.set("t", "str")
        text = str(value)
    value_node = ET.SubElement(cell, f"{{{MAIN_NS}}}v")
    value_node.text = text


def _find_or_add_cell(row: ET.Element, column: str) -> ET.Element:
    target_ref = f"{column}{row.attrib['r']}"
    for cell in row.findall("m:c", NS):
        if cell.attrib.get("r") == target_ref:
            return cell
    cell = ET.Element(f"{{{MAIN_NS}}}c", {"r": target_ref})
    existing = row.findall("m:c", NS)
    inserted = False
    target_index = _column_index(column)
    for index, current in enumerate(existing):
        current_column = "".join(char for char in current.attrib.get("r", "") if char.isalpha())
        if _column_index(current_column) > target_index:
            row.insert(index, cell)
            inserted = True
            break
    if not inserted:
        row.append(cell)
    return cell


def _column_index(column: str) -> int:
    index = 0
    for char in column.upper():
        if not char.isalpha():
            raise TemplateWriterError(f"非法列名：{column}")
        index = index * 26 + ord(char) - 64
    return index


def _columns(start: int, end: int) -> list[str]:
    return [_column_name(index) for index in range(start, end + 1)]


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result
