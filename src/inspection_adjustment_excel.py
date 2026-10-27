"""送检信息调整的 openpyxl 读取、计划、追加和安全保存实现。"""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
from copy import copy
from numbers import Number
from pathlib import Path
from typing import Iterable
from unicodedata import east_asian_width

from openpyxl import Workbook, load_workbook
from openpyxl.cell.rich_text import CellRichText, InlineFont, TextBlock
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.exceptions import InvalidFileException

from .inspection_adjustment import (
    InspectionAdjustmentError,
    InspectionAdjustmentPreview,
    InspectionAdjustmentResult,
    InspectionRecord,
    SOURCE_SHEET_NAME,
    SOURCE_TO_TARGET_COLUMNS,
    SYSTEM_YELLOW,
    TARGET_DEFAULT_SHEET_NAME,
    TARGET_HEADERS,
    is_valid_identifier,
    normalized_identifier,
)


SOURCE_REQUIRED_MAX_COLUMN = max(column_index_from_string(value) for value in SOURCE_TO_TARGET_COLUMNS.values())
TARGET_COLUMN_COUNT = len(TARGET_HEADERS)
# 当前送检信息调整映射把源表 C 列“物项编码”写入 B 的目标第 1 列（A）。
# 只在这一目标列做编码尾段强调，避免影响 T 列“单号”及其它编码/业务列。
ITEM_CODE_TARGET_COLUMN = 1
ITEM_CODE_SUFFIX_LENGTH = 10
ITEM_CODE_SUFFIX_BLUE = "0070C0"
ITEM_CODE_FONT_NAME = "微软雅黑"
ITEM_CODE_FONT_SIZE = 11
MAX_COLUMN_WIDTH = 60
MIN_COLUMN_WIDTH = 8
SOURCE_DISPLAY_COLUMNS = frozenset({"C", "D", "G", "H", "Q"})


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_value(cell: object) -> object:
    """读取映射字符串的 Excel 显示语义，不影响未映射列。"""

    value = getattr(cell, "value", None)
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, Number):
        if isinstance(value, int):
            numeric_value: int | float = value
        else:
            numeric_value = float(value)
            if not math.isfinite(numeric_value):
                return str(value)
        number_format = str(getattr(cell, "number_format", "") or "General")
        number_format = number_format.split(";", 1)[0]
        number_format = re.sub(r'"[^"]*"|\\.', "", number_format)
        integer_format, separator, fraction_format = number_format.partition(".")
        decimal_places = len(re.findall(r"[0#]", fraction_format)) if separator else 0
        zero_run = re.search(r"0+", integer_format)
        if isinstance(numeric_value, int) or numeric_value.is_integer():
            integer_value = int(numeric_value)
            if zero_run is not None and len(zero_run.group(0)) > 1:
                return f"{integer_value:0{len(zero_run.group(0))}d}"
            if decimal_places == 0:
                return str(integer_value)
        if decimal_places:
            format_spec = f",.{decimal_places}f" if "," in integer_format else f".{decimal_places}f"
            return format(numeric_value, format_spec)
        return str(value)
    return str(value)


def _same_value(left: object, right: object) -> bool:
    if left is None and right in (None, ""):
        return True
    if right is None and left in (None, ""):
        return True
    return left == right


def _normalized_headers(values: Iterable[object]) -> tuple[str, ...]:
    return tuple("" if value is None else str(value).strip() for value in values)


def _is_system_yellow(fill: object) -> bool:
    fill_type = getattr(fill, "fill_type", None)
    if fill_type not in {"solid", "lightUp", "lightTrellis", "darkTrellis", "darkGrid", "lightGrid"}:
        return False
    color = getattr(fill, "fgColor", None)
    rgb = getattr(color, "rgb", None)
    if not isinstance(rgb, str):
        return False
    return rgb.upper()[-6:] == SYSTEM_YELLOW


def _visual_length(value: object) -> int:
    text = "" if value is None else str(value)
    longest = max((len(line) for line in text.splitlines()), default=0)
    width = 0
    for char in text:
        width += 2 if east_asian_width(char) in {"W", "F", "A"} else 1
    return max(longest, width)


def _highlight_item_code_suffix(value: object) -> object:
    """将物项编码末尾十位数字设为蓝色粗体富文本。

    仅匹配带非空前缀且末尾连续十位 ASCII 数字的字符串。返回的富文本在
    openpyxl 默认 ``data_only=True`` 回读时仍折叠为完整字符串，因此不会
    改变现有去重、回读和业务值比较语义。
    """

    if not isinstance(value, str) or len(value) <= ITEM_CODE_SUFFIX_LENGTH:
        return value
    prefix = value[:-ITEM_CODE_SUFFIX_LENGTH]
    suffix = value[-ITEM_CODE_SUFFIX_LENGTH:]
    if not prefix or re.fullmatch(r"[0-9]{10}", suffix) is None:
        return value
    return CellRichText(
        TextBlock(
            InlineFont(rFont=ITEM_CODE_FONT_NAME, sz=ITEM_CODE_FONT_SIZE),
            prefix,
        ),
        TextBlock(
            InlineFont(
                rFont=ITEM_CODE_FONT_NAME,
                sz=ITEM_CODE_FONT_SIZE,
                color=ITEM_CODE_SUFFIX_BLUE,
                b=True,
            ),
            suffix,
        ),
    )


def create_empty_target_workbook(path: Path) -> None:
    """显式创建一个符合 B 契约的单 Sheet 工作簿。"""

    target = Path(path)
    if target.suffix.lower() != ".xlsx":
        raise InspectionAdjustmentError("B 文件仅支持 .xlsx。", retryable=False)
    try:
        if target.exists():
            raise InspectionAdjustmentError(
                f"B 目标文件已存在，拒绝覆盖既有文件：{target}",
                retryable=False,
            )
    except OSError as exc:
        raise InspectionAdjustmentError(
            f"B 目标路径无法核对是否已存在，已拒绝创建：{target}；{type(exc).__name__}",
            retryable=True,
        ) from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = TARGET_DEFAULT_SHEET_NAME
    for column, value in enumerate(TARGET_HEADERS, start=1):
        sheet.cell(row=1, column=column, value=value or None)
    workbook.save(target)
    workbook.close()


class InspectionAdjustmentService:
    """实现 REQ-003～REQ-010 的文件级同步服务。"""

    def build_preview(self, source_path: Path, target_path: Path) -> InspectionAdjustmentPreview:
        source = self._require_xlsx_file(source_path, "A")
        target = self._require_xlsx_file(target_path, "B")
        self._ensure_distinct_files(source, target)

        source_workbook = self._load(source, data_only=True, label="A")
        target_workbook = self._load(target, data_only=True, label="B")
        try:
            source_sheet = self._source_sheet(source_workbook)
            target_sheet = self._target_sheet(target_workbook)
            self._validate_source_columns(source_sheet)
            self._validate_target_headers(target_sheet)

            records, scanned_count, valid_count, duplicate_count, anomaly_count, conflicts = self._read_source_records(source_sheet)
            last_data_row = self._last_data_row(target_sheet)
            existing_rows = tuple(
                tuple(target_sheet.cell(row=row, column=column).value for column in range(1, TARGET_COLUMN_COUNT + 1))
                for row in range(2, last_data_row + 1)
            )
            existing_identifiers = {
                normalized_identifier(_display_value(target_sheet.cell(row=row, column=20)))
                for row in range(2, last_data_row + 1)
                if normalized_identifier(_display_value(target_sheet.cell(row=row, column=20)))
            }

            new_records: list[InspectionRecord] = []
            for record in records:
                if record.identifier in existing_identifiers:
                    duplicate_count += 1
                    continue
                new_records.append(record)

            new_records.sort(key=lambda item: item.identifier)
            fingerprint = _file_sha256(target)
            return InspectionAdjustmentPreview(
                source_path=source,
                target_path=target,
                target_sheet=target_sheet.title,
                target_fingerprint=fingerprint,
                scanned_count=scanned_count,
                valid_count=valid_count,
                new_records=tuple(new_records),
                duplicate_count=duplicate_count,
                anomaly_count=anomaly_count,
                conflict_identifiers=tuple(sorted(conflicts)),
                target_last_data_row=last_data_row,
                existing_rows=existing_rows,
            )
        finally:
            source_workbook.close()
            target_workbook.close()

    def synchronize(self, preview: InspectionAdjustmentPreview) -> InspectionAdjustmentResult:
        self._ensure_distinct_files(preview.source_path, preview.target_path)
        if preview.conflict_identifiers:
            conflict_text = "、".join(preview.conflict_identifiers)
            raise InspectionAdjustmentError(
                "A 同批次存在字段冲突，必须人工核查后重新读取；"
                f"冲突唯一识别号：{conflict_text}；未写入 B、未清除底纹。",
                retryable=False,
            )
        if preview.new_count == 0:
            # 没有新增时不读取写入句柄、不清底纹、不改写原文件。
            return InspectionAdjustmentResult(
                source_path=preview.source_path,
                target_path=preview.target_path,
                target_sheet=preview.target_sheet,
                scanned_count=preview.scanned_count,
                new_count=0,
                duplicate_count=preview.duplicate_count,
                anomaly_count=preview.anomaly_count,
                written=False,
                backup_path=None,
                new_rows=(),
            )

        self._ensure_target_unchanged(
            preview.target_path,
            preview.target_fingerprint,
            phase="读取预览后",
        )

        temp_path: Path | None = None
        preserve_temp = False
        new_rows: tuple[tuple[str, int], ...] = ()
        try:
            # 以 rich_text=True 载入，保存下一批新增记录时保留历史目标编码列的
            # 蓝色粗体尾段；默认 rich_text=False 会把历史富文本扁平化为普通字符串。
            workbook = self._load(
                preview.target_path,
                data_only=False,
                rich_text=True,
                label="B",
            )
            try:
                target_sheet = self._target_sheet(workbook)
                self._validate_target_headers(target_sheet)
                if target_sheet.title != preview.target_sheet:
                    raise InspectionAdjustmentError(
                        "B 的目标 Sheet 在正式写入前发生变化，已取消保存；请重新读取。",
                        retryable=True,
                        requires_reread=True,
                    )
                current_last_data_row = self._last_data_row(target_sheet)
                if current_last_data_row != preview.target_last_data_row:
                    raise InspectionAdjustmentError(
                        "B 的有效数据范围在读取后发生变化，已取消保存；请重新读取。",
                        retryable=True,
                        requires_reread=True,
                    )
                self._ensure_append_area_is_not_merged(
                    target_sheet,
                    current_last_data_row + 1,
                    current_last_data_row + preview.new_count,
                )
                self._clear_system_yellow(target_sheet, current_last_data_row)
                start_row = current_last_data_row + 1
                for offset, record in enumerate(preview.new_records):
                    row_number = start_row + offset
                    self._copy_template_style(target_sheet, row_number)
                    for column, value in enumerate(record.target_values, start=1):
                        target_sheet.cell(row=row_number, column=column, value=value)
                    item_code_cell = target_sheet.cell(
                        row=row_number,
                        column=ITEM_CODE_TARGET_COLUMN,
                    )
                    item_code_cell.value = _highlight_item_code_suffix(item_code_cell.value)
                    item_code_cell.font = Font(
                        name=ITEM_CODE_FONT_NAME,
                        sz=ITEM_CODE_FONT_SIZE,
                    )
                    for column in range(1, TARGET_COLUMN_COUNT + 1):
                        target_sheet.cell(
                            row=row_number,
                            column=column,
                        ).fill = PatternFill(fill_type="solid", fgColor=SYSTEM_YELLOW)
                new_rows = tuple(
                    (record.identifier, start_row + offset)
                    for offset, record in enumerate(preview.new_records)
                )
                self._auto_width(target_sheet)

                temp_path = self._make_temp_path(preview.target_path)
                workbook.save(temp_path)
            finally:
                workbook.close()

            self._verify_written_workbook(
                temp_path,
                preview,
                new_rows,
            )
            self._ensure_target_unchanged(
                preview.target_path,
                preview.target_fingerprint,
                phase="临时保存期间",
                temp_path=temp_path,
                preserve_temp=True,
            )
            temp_path.replace(preview.target_path)
            self._verify_written_workbook(
                preview.target_path,
                preview,
                new_rows,
            )
        except InspectionAdjustmentError as exc:
            preserve_temp = exc.preserve_temp
            raise
        except (OSError, InvalidFileException, ValueError, TypeError) as exc:
            raise InspectionAdjustmentError(
                self._save_error_message(exc),
                retryable=True,
            ) from exc
        except Exception as exc:
            raise InspectionAdjustmentError(
                self._save_error_message(exc),
                retryable=True,
            ) from exc
        finally:
            if not preserve_temp and temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        return InspectionAdjustmentResult(
            source_path=preview.source_path,
            target_path=preview.target_path,
            target_sheet=preview.target_sheet,
            scanned_count=preview.scanned_count,
            new_count=preview.new_count,
            duplicate_count=preview.duplicate_count,
            anomaly_count=preview.anomaly_count,
            written=True,
            backup_path=None,
            new_rows=new_rows,
        )

    @staticmethod
    def _require_xlsx_file(path: Path, label: str) -> Path:
        value = Path(path)
        if value.suffix.lower() != ".xlsx":
            raise InspectionAdjustmentError(f"{label} 文件仅支持 .xlsx：{value}", retryable=False)
        try:
            if not value.is_file():
                raise InspectionAdjustmentError(f"{label} 文件不存在或当前账号无读取权限：{value}", retryable=False)
        except OSError as exc:
            raise InspectionAdjustmentError(f"{label} 文件不可访问：{value}；{type(exc).__name__}", retryable=True) from exc
        return value

    @staticmethod
    def _ensure_distinct_files(source: Path, target: Path) -> None:
        source_path = os.fspath(source)
        target_path = os.fspath(target)
        normalized_source = os.path.normcase(
            os.path.abspath(os.path.realpath(source_path))
        )
        normalized_target = os.path.normcase(
            os.path.abspath(os.path.realpath(target_path))
        )
        normalized_same = normalized_source == normalized_target
        try:
            if os.path.exists(source_path) and os.path.exists(target_path):
                if os.path.samefile(source_path, target_path):
                    raise InspectionAdjustmentError(
                        "不允许 A/B 指向同一文件，已阻断；未修改 A，也未写入 B。",
                        retryable=False,
                    )
        except InspectionAdjustmentError:
            raise
        except OSError as exc:
            if normalized_same:
                raise InspectionAdjustmentError(
                    "不允许 A/B 指向同一文件，已阻断；未修改 A，也未写入 B。",
                    retryable=False,
                ) from exc
            raise InspectionAdjustmentError(
                "无法核对 A/B 是否为同一实际文件，已阻断；未修改 A，也未写入 B。",
                retryable=True,
            ) from exc
        if normalized_same:
            raise InspectionAdjustmentError(
                "不允许 A/B 指向同一文件，已阻断；未修改 A，也未写入 B。",
                retryable=False,
            )

    @staticmethod
    def _ensure_target_unchanged(
        target: Path,
        expected_fingerprint: str,
        *,
        phase: str,
        temp_path: Path | None = None,
        preserve_temp: bool = False,
    ) -> None:
        try:
            actual_fingerprint = _file_sha256(target)
        except OSError as exc:
            detail = f"B 文件在{phase}无法完成最终状态核对：{type(exc).__name__}；请重新读取。"
            if temp_path is not None:
                detail += f"临时文件：{temp_path}。"
            raise InspectionAdjustmentError(
                detail,
                retryable=True,
                requires_reread=True,
                preserve_temp=preserve_temp,
            ) from exc
        if actual_fingerprint == expected_fingerprint:
            return
        detail = f"B 文件在{phase}发生变化，已阻断正式替换；请重新读取。"
        if temp_path is not None:
            detail += f"临时文件：{temp_path}。"
        raise InspectionAdjustmentError(
            detail,
            retryable=True,
            requires_reread=True,
            preserve_temp=preserve_temp,
        )

    @staticmethod
    def _load(path: Path, *, data_only: bool, label: str, rich_text: bool = False):
        try:
            return load_workbook(
                path,
                data_only=data_only,
                read_only=False,
                rich_text=rich_text,
            )
        except (OSError, InvalidFileException, ValueError) as exc:
            raise InspectionAdjustmentError(
                f"{label} 工作簿无法读取：{path}；{type(exc).__name__}",
                retryable=True,
            ) from exc

    @staticmethod
    def _source_sheet(workbook):
        if SOURCE_SHEET_NAME not in workbook.sheetnames:
            raise InspectionAdjustmentError(
                f"A 缺少固定 Sheet：{SOURCE_SHEET_NAME}；未修改 B。",
                retryable=False,
            )
        return workbook[SOURCE_SHEET_NAME]

    @staticmethod
    def _target_sheet(workbook):
        if len(workbook.sheetnames) != 1:
            raise InspectionAdjustmentError(
                f"B 必须只有一个 Sheet，当前为 {len(workbook.sheetnames)} 个；未修改 B。",
                retryable=False,
            )
        return workbook[workbook.sheetnames[0]]

    @staticmethod
    def _validate_source_columns(sheet) -> None:
        if sheet.max_column < SOURCE_REQUIRED_MAX_COLUMN:
            raise InspectionAdjustmentError(
                f"A 的有效列范围不足，至少需要到 Q 列，当前最大列为 {sheet.max_column}；未修改 B。",
                retryable=False,
            )

    @staticmethod
    def _validate_target_headers(sheet) -> None:
        actual = _normalized_headers(
            sheet.cell(row=1, column=column).value
            for column in range(1, TARGET_COLUMN_COUNT + 1)
        )
        expected = tuple(TARGET_HEADERS)
        if actual != expected:
            raise InspectionAdjustmentError(
                "B 第 1 行 A:T 表头不匹配，已阻断写入；未修改 B。",
                retryable=False,
            )

    @staticmethod
    def _read_source_records(sheet):
        scanned_count = max(0, sheet.max_row - 1)
        valid_count = 0
        duplicate_count = 0
        anomaly_count = 0
        records_by_identifier: dict[str, InspectionRecord] = {}
        conflicts: set[str] = set()

        for row in range(2, sheet.max_row + 1):
            identifier_cell = sheet.cell(row=row, column=column_index_from_string("D"))
            identifier_value = _display_value(identifier_cell)
            if not is_valid_identifier(identifier_value):
                anomaly_count += 1
                continue
            identifier = normalized_identifier(identifier_value)
            target_values: list[object] = [None] * TARGET_COLUMN_COUNT
            for target_column, source_letter in SOURCE_TO_TARGET_COLUMNS.items():
                source_cell = sheet.cell(
                    row=row,
                    column=column_index_from_string(source_letter),
                )
                source_value = (
                    _display_value(source_cell)
                    if source_letter in SOURCE_DISPLAY_COLUMNS
                    else source_cell.value
                )
                target_values[target_column - 1] = (
                    identifier if target_column == 20 else source_value
                )
            record = InspectionRecord(row, identifier, tuple(target_values))
            valid_count += 1
            previous = records_by_identifier.get(identifier)
            if previous is None and identifier not in conflicts:
                records_by_identifier[identifier] = record
            elif identifier in conflicts:
                continue
            elif previous is not None and previous.target_values == record.target_values:
                duplicate_count += 1
            else:
                conflicts.add(identifier)
                records_by_identifier.pop(identifier, None)
                anomaly_count += 1

        return (
            tuple(records_by_identifier.values()),
            scanned_count,
            valid_count,
            duplicate_count,
            anomaly_count,
            conflicts,
        )

    @staticmethod
    def _last_data_row(sheet) -> int:
        last_row = 1
        for row in range(2, sheet.max_row + 1):
            if any(
                sheet.cell(row=row, column=column).value not in (None, "")
                for column in range(1, TARGET_COLUMN_COUNT + 1)
            ):
                last_row = row
        return last_row

    @staticmethod
    def _clear_system_yellow(sheet, last_data_row: int) -> None:
        for row in range(2, last_data_row + 1):
            for column in range(1, TARGET_COLUMN_COUNT + 1):
                cell = sheet.cell(row=row, column=column)
                if _is_system_yellow(cell.fill):
                    cell.fill = PatternFill()

    @staticmethod
    def _copy_template_style(sheet, target_row: int) -> None:
        template_row = 2 if sheet.max_row >= 2 else 1
        if template_row == target_row:
            return
        for column in range(1, TARGET_COLUMN_COUNT + 1):
            source = sheet.cell(row=template_row, column=column)
            target = sheet.cell(row=target_row, column=column)
            if source.has_style:
                target._style = copy(source._style)
        source_height = sheet.row_dimensions[template_row].height
        if source_height is not None:
            sheet.row_dimensions[target_row].height = source_height

    @staticmethod
    def _ensure_append_area_is_not_merged(sheet, first_row: int, last_row: int) -> None:
        for merged_range in sheet.merged_cells.ranges:
            if merged_range.max_row < first_row or merged_range.min_row > last_row:
                continue
            if merged_range.max_col < 1 or merged_range.min_col > TARGET_COLUMN_COUNT:
                continue
            raise InspectionAdjustmentError(
                f"B 追加区域与合并单元格 {merged_range} 相交，已阻断保存；未修改 B。",
                retryable=False,
            )

    @staticmethod
    def _auto_width(sheet) -> None:
        last_row = InspectionAdjustmentService._last_data_row(sheet)
        for column in range(1, TARGET_COLUMN_COUNT + 1):
            width = max(
                _visual_length(sheet.cell(row=row, column=column).value)
                for row in range(1, last_row + 1)
            )
            sheet.column_dimensions[get_column_letter(column)].width = min(
                MAX_COLUMN_WIDTH,
                max(MIN_COLUMN_WIDTH, width + 2),
            )

    @staticmethod
    def _make_temp_path(target: Path) -> Path:
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{target.stem}.",
                suffix=".tmp.xlsx",
                dir=str(target.parent),
            )
            os.close(descriptor)
            return Path(name)
        except OSError as exc:
            raise InspectionAdjustmentError(
                f"B 临时文件创建失败，未修改原文件：{type(exc).__name__}",
                retryable=True,
            ) from exc

    @staticmethod
    def _verify_written_workbook(
        path: Path | None,
        preview: InspectionAdjustmentPreview,
        new_rows: tuple[tuple[str, int], ...],
    ) -> None:
        if path is None:
            raise InspectionAdjustmentError("临时文件不存在，未显示同步成功。", retryable=True)
        workbook = InspectionAdjustmentService._load(path, data_only=True, label="B 回读")
        try:
            sheet = InspectionAdjustmentService._target_sheet(workbook)
            InspectionAdjustmentService._validate_target_headers(sheet)
            if sheet.title != preview.target_sheet:
                raise InspectionAdjustmentError("B 回读时目标 Sheet 不一致，未显示同步成功。", retryable=True)

            for offset, previous_values in enumerate(preview.existing_rows, start=2):
                for column, expected in enumerate(previous_values, start=1):
                    actual = sheet.cell(row=offset, column=column).value
                    if not _same_value(actual, expected):
                        raise InspectionAdjustmentError(
                            "B 历史数据回读不一致，未显示同步成功；请重新读取后重试。",
                            retryable=True,
                        )

            record_by_identifier = {record.identifier: record for record in preview.new_records}
            for identifier, row_number in new_rows:
                record = record_by_identifier[identifier]
                for column, expected in enumerate(record.target_values, start=1):
                    actual = sheet.cell(row=row_number, column=column).value
                    if not _same_value(actual, expected):
                        raise InspectionAdjustmentError(
                            "B 新增映射值回读不一致，未显示同步成功；请重新读取后重试。",
                            retryable=True,
                        )
                    if column not in SOURCE_TO_TARGET_COLUMNS and actual not in (None, ""):
                        raise InspectionAdjustmentError(
                            "B 新增记录的未映射列非空，未显示同步成功。",
                            retryable=True,
                        )
                    if not _is_system_yellow(sheet.cell(row=row_number, column=column).fill):
                        raise InspectionAdjustmentError(
                            "B 新增记录浅黄色底纹回读不一致，未显示同步成功。",
                            retryable=True,
                        )
        finally:
            workbook.close()

    @staticmethod
    def _save_error_message(error: BaseException) -> str:
        detail = str(error).strip() or type(error).__name__
        if isinstance(error, PermissionError):
            return f"B 保存失败，可能被 WPS/Excel 占用或无写权限；{detail}"
        return f"B 保存或回读失败，未确认同步成功；{detail}"


__all__ = [
    "InspectionAdjustmentService",
    "MAX_COLUMN_WIDTH",
    "MIN_COLUMN_WIDTH",
    "SOURCE_REQUIRED_MAX_COLUMN",
    "create_empty_target_workbook",
]
