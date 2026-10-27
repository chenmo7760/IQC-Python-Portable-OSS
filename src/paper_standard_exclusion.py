from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import zipfile
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS, "pr": PKG_REL_NS}

PARSE_VALID = "VALID"
PARSE_AMBIGUOUS = "AMBIGUOUS"
PARSE_INVALID = "INVALID"

MATCH_NOT_FOUND = "PAPER_STANDARD_NOT_FOUND"
MATCH_EXCLUDED = "PAPER_STANDARD_EXCLUDED"
MATCH_REVIEW_REQUIRED = "PAPER_STANDARD_REVIEW_REQUIRED"
MATCH_UNDETERMINED = "PAPER_EXCLUSION_UNDETERMINED"

# 合成示例：仅两个明确名称共用同一示例文档身份。
# 不能把物料号、DemoControl 关键词或全局标题归一化当作纸质身份依据。
DEMO_CONTROL_IDENTITY_RULE = "DEMO_GROUP_DEMO_CONTROL_X100_BOARD"
DEMO_CONTROL_EXCLUDE_KEY = "DEMO-DEMO17-DEMOSYS-T-DEMOFORM-DOC102-001"
DEMO_CONTROL_EXACT_NAMES = frozenset(("DemoControl定型电路板进货检验规范", "DemoControl定型电路板卡进货检验规范"))
DEMO_CONTROL_CATALOG_TITLE = "DemoControl定型电路板卡进货检验规范"
QA1_PAPER_SCOPE_BLOCKED = "QA1_PAPER_SCOPE_BLOCKED"

_SUPPORTED_EXTENSIONS = {".xlsx", ".xlsm"}
_DOCUMENT_EXTENSION_RE = re.compile(r"(?P<ext>\.(?:pdf|docx?|xlsx?|xlsm))$", re.IGNORECASE)
_REVISION_RE = re.compile(r"^(?:[0-9]+|R[0-9]+)$", re.IGNORECASE)
_CODE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9#井])"
    r"[A-Za-z0-9][A-Za-z0-9#井]*(?:-[A-Za-z0-9#井]+)+"
    r"(?![A-Za-z0-9#井])"
)
_Z_BOUNDARY_RE = re.compile(r"-(?P<suffix>Z0#?|Z)-", re.IGNORECASE)
_SIGNED_RE = re.compile(r"(?:SIGNED|签字版)", re.IGNORECASE)


class PaperStandardExclusionError(ValueError):
    """线下纸质规范清单无法安全作为排除目录时抛出。"""


@dataclass(frozen=True)
class PaperStandardEntry:
    source_path: Path = Path()
    sheet_name: str = ""
    source_row: int = 0
    raw_value: str = ""
    document_code: str = ""
    exclude_key: str = ""
    z_suffix: str = ""
    revision: str = ""
    title: str = ""
    extension: str = ""
    parse_status: str = PARSE_INVALID
    reason: str = ""


@dataclass(frozen=True)
class PaperStandardCatalog:
    source_path: Path = Path()
    entries: tuple[PaperStandardEntry, ...] = ()
    by_key: Mapping[str, PaperStandardEntry] = field(default_factory=dict, repr=False, compare=False)
    by_hash_well_key: Mapping[str, tuple[PaperStandardEntry, ...]] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.by_key and self.entries:
            object.__setattr__(self, "by_key", {entry.exclude_key: entry for entry in self.entries})
        if not self.by_hash_well_key and self.entries:
            grouped: dict[str, list[PaperStandardEntry]] = {}
            for entry in self.entries:
                grouped.setdefault(_hash_well_key(entry.exclude_key), []).append(entry)
            object.__setattr__(self, "by_hash_well_key", {key: tuple(value) for key, value in grouped.items()})

    def lookup(self, key: str) -> PaperStandardEntry | None:
        return self.by_key.get(key.strip())

    def hash_well_candidates(self, key: str) -> tuple[PaperStandardEntry, ...]:
        return tuple(self.by_hash_well_key.get(_hash_well_key(key), ()))


@dataclass(frozen=True)
class PaperStandardMatch:
    status: str
    windchill_raw: str = ""
    document_code: str = ""
    exclude_key: str = ""
    entry: PaperStandardEntry | None = None
    item_code: str = ""
    reason: str = ""
    identity_rule_id: str = ""
    identity_source: str = ""


def _demo_control_literal_name(value: str) -> str:
    """仅去首尾空白及一层书名号，不复用会删除内部/外围字符的标题清洗。"""
    text = value.strip()
    if text.startswith("《") and text.endswith("》"):
        text = text[1:-1].strip()
    return text


def _demo_control_reference_title(text: str, document_code: str) -> str:
    """取编号后的字面标题；版本字段只在编号文件名约定的位置移除。"""
    if not document_code or document_code not in text:
        return _demo_control_literal_name(text)
    before, after = text.split(document_code, 1)
    title = before.strip(" _＿:：") or after.strip(" _＿:：")
    parts = re.split(r"[_＿]", title)
    if len(parts) > 1 and _REVISION_RE.fullmatch(parts[0]):
        title = "_".join(parts[1:])
    return _demo_control_literal_name(title)


def _match_controlled_demo_control(
    text: str, parsed: PaperStandardEntry, catalog: PaperStandardCatalog | None, item_code: str,
) -> PaperStandardMatch | None:
    codes = tuple(match.group(0) for match in _CODE_TOKEN_RE.finditer(text))
    keys = tuple(_exclude_key_from_code(code)[0] for code in codes)
    # 原始纯名称，或编号文件名的完整标题字段；不按子串认领未知文本。
    names = (_demo_control_literal_name(text),)
    if codes:
        names += tuple(_demo_control_reference_title(text, code) for code in codes)
        names += (_demo_control_literal_name(re.split(r"[_＿]", text)[-1]),)
    name_controlled = any(name in DEMO_CONTROL_EXACT_NAMES for name in names)
    code_controlled = DEMO_CONTROL_EXCLUDE_KEY in keys
    if not name_controlled and not code_controlled:
        return None
    source = "document_code" if codes else "exact_name"

    def result(status: str, reason: str, entry: PaperStandardEntry | None = None) -> PaperStandardMatch:
        return PaperStandardMatch(
            status, windchill_raw=text, document_code=parsed.document_code,
            exclude_key=parsed.exclude_key if codes else DEMO_CONTROL_EXCLUDE_KEY,
            entry=entry, item_code=item_code, reason=reason,
            identity_rule_id=DEMO_CONTROL_IDENTITY_RULE, identity_source=source,
        )

    # 单独扫描全部编号，防止旧解析器的“首段编号优先”吞掉第二个编号。
    if len(codes) > 1:
        return result(MATCH_UNDETERMINED, "受控 DemoControl 文本存在多个规范编号，待核实；普通电子记录已阻断")
    if codes and (parsed.parse_status != PARSE_VALID or keys[0] != DEMO_CONTROL_EXCLUDE_KEY):
        return result(MATCH_UNDETERMINED, "受控 DemoControl 完整名称与显式编号冲突，待核实；普通电子记录已阻断")
    if catalog is None:
        return result(MATCH_UNDETERMINED, "受控 DemoControl 缺少本次纸质清单，待核实；普通电子记录已阻断")
    # 同键重复或 #/井 候选均不能从字典中取第一项掩盖歧义。
    candidates = tuple(entry for entry in catalog.entries if entry.exclude_key == DEMO_CONTROL_EXCLUDE_KEY)
    if len(candidates) != 1 or len(catalog.hash_well_candidates(DEMO_CONTROL_EXCLUDE_KEY)) != 1:
        return result(MATCH_UNDETERMINED, "受控 DemoControl 纸质清单目标缺失或不唯一，待核实；普通电子记录已阻断")
    entry = candidates[0]
    if (entry.parse_status != PARSE_VALID
            or _demo_control_reference_title(entry.raw_value, entry.document_code) != DEMO_CONTROL_CATALOG_TITLE):
        return result(MATCH_UNDETERMINED, "受控 DemoControl 清单身份与核实依据不一致，待核实；普通电子记录已阻断", entry)
    return result(MATCH_EXCLUDED, "受控 DemoControl 身份唯一命中线下纸质清单，普通电子记录已阻断", entry)


def read_paper_standard_workbook(path: Path) -> PaperStandardCatalog:
    """只读解析线下规范清单 B 列，任何异常行都 fail-closed。"""

    source = Path(path)
    if source.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        raise PaperStandardExclusionError(f"线下规范清单仅支持 .xlsx/.xlsm：{source}")
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PaperStandardExclusionError(f"无法打开线下规范清单：{source}") from exc
    entries: list[PaperStandardEntry] = []
    invalid: list[PaperStandardEntry] = []
    header_seen = False
    with archive:
        for sheet_name, target in _workbook_sheets(archive):
            rows = dict(_rows_from_sheet(archive, target))
            header = _text(_cell_at(rows.get(1, []), 1))
            if header != "文件编号/文件名":
                continue
            header_seen = True
            for row_number, values in sorted(rows.items()):
                if row_number < 2:
                    continue
                raw = _text(_cell_at(values, 1))
                if not raw:
                    continue
                entry = parse_paper_standard_entry(
                    raw,
                    source_path=source,
                    sheet_name=sheet_name,
                    source_row=row_number,
                )
                if entry.parse_status != PARSE_VALID:
                    invalid.append(entry)
                else:
                    entries.append(entry)
    if not header_seen:
        raise PaperStandardExclusionError("线下规范清单未找到 B1=文件编号/文件名 的工作表")
    if invalid:
        details = "；".join(
            f"{entry.sheet_name}!B{entry.source_row}:{entry.reason or entry.parse_status}"
            for entry in invalid[:5]
        )
        raise PaperStandardExclusionError(f"线下规范清单存在无法解析的 B 列条目：{details}")
    if not entries:
        raise PaperStandardExclusionError("线下规范清单 B 列没有有效条目")
    by_key: dict[str, PaperStandardEntry] = {}
    duplicates: list[str] = []
    for entry in entries:
        if entry.exclude_key in by_key:
            duplicates.append(entry.exclude_key)
        else:
            by_key[entry.exclude_key] = entry
    if duplicates:
        raise PaperStandardExclusionError("线下规范清单排除键重复：" + "、".join(sorted(set(duplicates))[:10]))
    by_hash_well: dict[str, list[PaperStandardEntry]] = {}
    for entry in entries:
        by_hash_well.setdefault(_hash_well_key(entry.exclude_key), []).append(entry)
    return PaperStandardCatalog(
        source,
        tuple(entries),
        by_key,
        {key: tuple(value) for key, value in by_hash_well.items()},
    )


def parse_paper_standard_entry(
    raw: object,
    *,
    source_path: Path = Path(),
    sheet_name: str = "",
    source_row: int = 0,
) -> PaperStandardEntry:
    return _parse_document_reference(
        raw,
        source_path=source_path,
        sheet_name=sheet_name,
        source_row=source_row,
        catalog_entry=True,
    )


def parse_windchill_document_reference(raw: object) -> PaperStandardEntry:
    return _parse_document_reference(raw, catalog_entry=False)


def match_paper_standard(
    raw: object,
    catalog: PaperStandardCatalog | None,
    *,
    item_code: str = "",
) -> PaperStandardMatch:
    text = _text(raw)
    parsed = parse_windchill_document_reference(text)
    controlled = _match_controlled_demo_control(text, parsed, catalog, item_code)
    if controlled is not None:
        return controlled
    if catalog is None:
        return PaperStandardMatch(MATCH_NOT_FOUND, windchill_raw=text, item_code=item_code)
    if parsed.parse_status != PARSE_VALID:
        return PaperStandardMatch(
            MATCH_UNDETERMINED,
            windchill_raw=text,
            document_code=parsed.document_code,
            exclude_key=parsed.exclude_key,
            item_code=item_code,
            reason=parsed.reason or "Windchill 文本无法唯一解析出规范编码",
        )
    entry = catalog.lookup(parsed.exclude_key)
    if entry is not None:
        return PaperStandardMatch(
            MATCH_EXCLUDED,
            windchill_raw=text,
            document_code=parsed.document_code,
            exclude_key=parsed.exclude_key,
            entry=entry,
            item_code=item_code,
            reason="Windchill 规范编码命中线下纸质清单",
        )
    candidates = catalog.hash_well_candidates(parsed.exclude_key)
    if candidates and any(candidate.exclude_key != parsed.exclude_key for candidate in candidates):
        return PaperStandardMatch(
            MATCH_REVIEW_REQUIRED,
            windchill_raw=text,
            document_code=parsed.document_code,
            exclude_key=parsed.exclude_key,
            entry=candidates[0] if len(candidates) == 1 else None,
            item_code=item_code,
            reason="#/井 字符存在差异，需人工检查编码字符",
        )
    return PaperStandardMatch(
        MATCH_NOT_FOUND,
        windchill_raw=text,
        document_code=parsed.document_code,
        exclude_key=parsed.exclude_key,
        item_code=item_code,
    )


def _parse_document_reference(
    raw: object,
    *,
    source_path: Path = Path(),
    sheet_name: str = "",
    source_row: int = 0,
    catalog_entry: bool,
) -> PaperStandardEntry:
    raw_text = _text(raw)
    if not raw_text:
        return PaperStandardEntry(source_path, sheet_name, source_row, raw_text, parse_status=PARSE_INVALID, reason="空白条目")
    basename = _basename(raw_text)
    stem, extension = _split_extension(basename)
    parts = [part.strip() for part in re.split(r"[_＿]", stem)]
    document_code = ""
    if parts and _looks_like_code(parts[0]):
        document_code = parts[0]
    else:
        candidates = _code_candidates(stem)
        if len(candidates) == 1:
            document_code = candidates[0]
        elif len(candidates) > 1:
            return PaperStandardEntry(
                source_path,
                sheet_name,
                source_row,
                raw_text,
                parse_status=PARSE_AMBIGUOUS,
                reason="文本中存在多个可能规范编码",
                extension=extension,
                title=_clean_title(stem),
            )
        else:
            return PaperStandardEntry(
                source_path,
                sheet_name,
                source_row,
                raw_text,
                parse_status=PARSE_INVALID,
                reason="未识别到规范编码",
                extension=extension,
                title=_clean_title(stem),
            )
    exclude_key, z_suffix = _exclude_key_from_code(document_code)
    if not exclude_key:
        return PaperStandardEntry(
            source_path,
            sheet_name,
            source_row,
            raw_text,
            document_code=document_code,
            parse_status=PARSE_INVALID,
            reason="规范编码为空",
            extension=extension,
            title=_clean_title(stem),
        )
    revision = ""
    title_start = 1
    if parts and parts[0] == document_code and len(parts) > 1 and _REVISION_RE.match(parts[1]):
        revision = parts[1].upper()
        title_start = 2
    title = _clean_title("_".join(part for part in parts[title_start:] if part))
    if not catalog_entry and parts and parts[0] != document_code:
        before = stem.split(document_code, 1)[0].strip(" _＿:：-")
        after = stem.split(document_code, 1)[1].strip(" _＿:：-")
        title = _clean_title(before or after)
    return PaperStandardEntry(
        source_path=source_path,
        sheet_name=sheet_name,
        source_row=source_row,
        raw_value=raw_text,
        document_code=document_code,
        exclude_key=exclude_key,
        z_suffix=z_suffix,
        revision=revision,
        title=title,
        extension=extension,
        parse_status=PARSE_VALID,
    )


def _workbook_sheets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {item.attrib["Id"]: item.attrib["Target"] for item in rels.findall("pr:Relationship", NS)}
    result: list[tuple[str, str]] = []
    for sheet in workbook.findall("m:sheets/m:sheet", NS):
        name = sheet.attrib["name"]
        rel_id = sheet.attrib[f"{{{REL_NS}}}id"]
        result.append((name, targets[rel_id]))
    return result


def _rows_from_sheet(archive: zipfile.ZipFile, target: str) -> Iterable[tuple[int, list[Any]]]:
    target = target.lstrip("/")
    if not target.startswith("xl/"):
        target = "xl/" + target
    root = ET.fromstring(archive.read(target))
    shared = _shared_strings(archive)
    for row in root.findall(".//m:sheetData/m:row", NS):
        row_number = int(row.attrib.get("r", "0"))
        values: list[Any] = []
        for cell in row.findall("m:c", NS):
            index = _column_index(cell.attrib.get("r", ""))
            while len(values) <= index:
                values.append("")
            values[index] = _cell_value(cell, shared)
        yield row_number, values


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.findall(".//m:t", NS)) for item in root.findall("m:si", NS)]


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
            raise PaperStandardExclusionError(f"共享字符串索引无效：{text}") from exc
    if cell_type == "b":
        return text == "1"
    return text


def _column_index(cell_ref: str) -> int:
    match = re.match(r"([A-Za-z]+)", cell_ref)
    if match is None:
        raise PaperStandardExclusionError(f"无法解析单元格地址：{cell_ref}")
    index = 0
    for char in match.group(1).upper():
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _cell_at(values: list[Any], index: int) -> Any:
    return values[index] if len(values) > index else ""


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _basename(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    return normalized.rsplit("/", 1)[-1].strip()


def _split_extension(value: str) -> tuple[str, str]:
    match = _DOCUMENT_EXTENSION_RE.search(value.strip())
    if match is None:
        return value.strip(), ""
    return value[: match.start()].strip(), match.group("ext").lower()


def _looks_like_code(value: str) -> bool:
    return _CODE_TOKEN_RE.fullmatch(value.strip()) is not None


def _code_candidates(value: str) -> list[str]:
    candidates = []
    seen: set[str] = set()
    for match in _CODE_TOKEN_RE.finditer(value):
        candidate = match.group(0).strip(" _＿:：,，;；。()（）[]【】<>《》")
        if candidate and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def _exclude_key_from_code(document_code: str) -> tuple[str, str]:
    text = document_code.strip()
    match = _Z_BOUNDARY_RE.search(text)
    if match is None:
        return text, ""
    return text[: match.start()], text[match.start() + 1 :]


def _clean_title(value: str) -> str:
    text = _SIGNED_RE.sub("", value or "")
    return text.strip(" _＿-:：,，;；。()（）[]【】<>《》")


def _hash_well_key(value: str) -> str:
    return value.strip().replace("#", "井")
