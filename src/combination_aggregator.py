"""按旧 VBA 规则把源行聚合为送检单-物料组合。

这一层是生成链路的唯一业务聚合入口：数量、送检单内相对行号和 M 码都在
这里完成，模板、抽样和 SQL 生成器不再各自按源行重新推导。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import re
from typing import Callable, Iterable

from .models import (
    InspectionCombination,
    OrderTask,
    SourceRow,
    canonicalize_m_code_separators,
    effective_supplier_for_order,
)


class CombinationAggregationError(ValueError):
    """源行无法按 VBA 组合规则安全聚合。"""


_M_CODE_RE = re.compile(r"M[0-9]{11}")
_M_RANGE_RE = re.compile(r"(?P<start>M[0-9]{11})\s*(?:~|～|至|-)\s*(?P<end>M[0-9]{11})")
_SEQUENCE_SPLIT_RE = re.compile(r"[;,；，/、]+")
_M_EXPANSION_SUFFIX_RE = re.compile(
    r"^(?P<head>.*?)\((?P<body>M\d{11}(?:\s*[/;,；，、]\s*M\d{11})*)\)$"
)
_M_ALLOWED_GAP_RE = re.compile(r"^[~～至\-/、,，;；()（）]*$")
MCodeWarningCallback = Callable[[str], None]


def _extract_m_codes(value: object) -> tuple[str, ...]:
    """Return strict M+11 members after removing all whitespace.

    Multiple members may be adjacent or separated by legacy punctuation.  The
    caller uses the count to avoid feeding a non-contiguous list to the strict
    single-code/range validator.  Malformed M-prefixed text is left for the
    existing ``_strict_m_code`` error path.
    """

    raw = "" if value is None else str(value).strip()
    if not raw:
        return ()
    compact = re.sub(r"\s+", "", raw)
    matches = tuple(_M_CODE_RE.finditer(compact))
    if not matches:
        return ()
    previous_end = 0
    for match in matches:
        if not _M_ALLOWED_GAP_RE.fullmatch(compact[previous_end : match.start()]):
            return ()
        previous_end = match.end()
    if not _M_ALLOWED_GAP_RE.fullmatch(compact[previous_end:]):
        return ()
    return tuple(match.group(0) for match in matches)


def parse_positive_integer(value: object, field_name: str = "quantity") -> int:
    """按 VBA 的数量约束解析正整数。

    Excel 中的 ``2.0`` 是合法的整数值；带小数部分、零、负数、布尔值和
    非有限数字一律拒绝，避免在汇总后才发现错误。
    """

    if isinstance(value, bool):
        raise CombinationAggregationError(f"{field_name} 必须是正整数")
    text = "" if value is None else str(value).strip().replace(",", "")
    if not text:
        raise CombinationAggregationError(f"{field_name} 不能为空")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise CombinationAggregationError(f"{field_name} 必须是正整数") from exc
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise CombinationAggregationError(f"{field_name} 必须是正整数")
    return int(number)


def _strict_m_code(value: object, field_name: str = "M码") -> str:
    # O/P 单码可能在 M 与数字之间带空格；校验前去掉所有空白，保持
    # ``M+11 位数字`` 的严格长度约束。
    text = "" if value is None else re.sub(r"\s+", "", str(value).strip())
    if _M_CODE_RE.fullmatch(text) is None:
        raise CombinationAggregationError(f"{field_name} 必须为 M+11 位数字")
    return text


def expand_m_range(
    start: object,
    end: object,
    *,
    warning_callback: MCodeWarningCallback | None = None,
) -> tuple[str, ...]:
    """展开一组 M 码起止值（包含端点）。

    起止码均先经过严格 ``M+11 位数字`` 校验。固定段（M+前 7 位）不一致时无法安全
    用单一前缀补造中间码，因此保留原始起止端点并通过回调发出可审计
    warning；这类前缀差异不再阻断整单生成。起止序号倒序仍然阻断。
    """

    start_text = "" if start is None else str(start).strip()
    end_text = "" if end is None else str(end).strip()
    if not start_text and not end_text:
        return ()
    # VBA 允许只填一端，将其视作单个 M 码。
    if not start_text:
        start_text = end_text
    if not end_text:
        end_text = start_text
    start_code = _strict_m_code(start_text, "M码起始")
    end_code = _strict_m_code(end_text, "M码结束")
    # M+11 位码的固定段为 M+前 7 位，余下 4 位作为序号；先校验完整
    # 数值顺序，确保跨前缀范围即使不展开也不会把倒序输入放行。
    start_suffix = int(start_code[8:])
    end_suffix = int(end_code[8:])
    if int(start_code[1:]) > int(end_code[1:]):
        raise CombinationAggregationError("M码起始值不能大于结束值")
    if start_code[:8] != end_code[:8]:
        detail = (
            f"M码起止值固定段（M+前7位）不一致：{start_code} ~ {end_code}；"
            "无法安全展开中间码，已保留起止端点继续生成"
        )
        if warning_callback is not None:
            # 诊断观察者不得改变聚合结果；即使日志回调自身异常，
            # 也继续按端点集合生成。
            try:
                warning_callback(detail)
            except Exception:
                pass
        return (start_code, end_code)
    prefix = start_code[:8]
    return tuple(f"{prefix}{suffix:04d}" for suffix in range(start_suffix, end_suffix + 1))


def _sequence_parts(row: SourceRow) -> tuple[tuple[str | None, str | None, bool], ...]:
    """提取一行的 M 单码/范围。

    新输入可以通过显式 ``sequence_start``/``sequence_end`` 提供 O/P 列起止值；
    未映射为显式字段的旧月度 16 列表仍从 O 列“序列码”读取，并兼容
    ``/``、``~``、``至``。
    非 M 的历史序列值保留为旧字段，不强行套用 M 码校验。
    """

    if row.sequence_start is not None or row.sequence_end is not None:
        start_text = "" if row.sequence_start is None else str(row.sequence_start).strip()
        end_text = "" if row.sequence_end is None else str(row.sequence_end).strip()
        start_range = _M_RANGE_RE.fullmatch(re.sub(r"\s+", "", start_text))
        end_range = _M_RANGE_RE.fullmatch(re.sub(r"\s+", "", end_text))
        if start_range is not None and not end_text:
            return ((start_range.group("start"), start_range.group("end"), False),)
        if end_range is not None and not start_text:
            return ((end_range.group("start"), end_range.group("end"), False),)
        start_codes = _extract_m_codes(start_text)
        end_codes = _extract_m_codes(end_text)
        if start_text[:1] in {"M", "m"} and not start_codes:
            _strict_m_code(re.sub(r"\s+", "", start_text), "M码起始")
        if end_text[:1] in {"M", "m"} and not end_codes:
            _strict_m_code(re.sub(r"\s+", "", end_text), "M码结束")
        if len(start_codes) > 1 or len(end_codes) > 1:
            return tuple((code, None, True) for code in (*start_codes, *end_codes))
        if len(start_codes) == 1:
            start_text = start_codes[0]
        if len(end_codes) == 1:
            end_text = end_codes[0]
        return ((start_text or None, end_text or None, not (start_text and end_text) or start_text == end_text),)
    values = list(row.values) + [""] * 16
    raw = "" if values[14] is None else str(values[14]).strip()
    raw_end = "" if values[15] is None else str(values[15]).strip()
    if not raw and raw_end:
        raw = raw_end
        raw_end = ""
    if not raw:
        return ()
    # Strip legacy parenthesized expansion before extracting M+11 members;
    # only the compressed head participates in the new output contract.
    legacy_suffix = _M_EXPANSION_SUFFIX_RE.fullmatch(raw)
    if legacy_suffix is not None:
        raw = legacy_suffix.group("head").strip()
    start_range = _M_RANGE_RE.fullmatch(re.sub(r"\s+", "", raw))
    end_range = _M_RANGE_RE.fullmatch(re.sub(r"\s+", "", raw_end))
    if start_range is not None and not raw_end:
        return ((start_range.group("start"), start_range.group("end"), False),)
    if end_range is not None and not raw:
        return ((end_range.group("start"), end_range.group("end"), False),)
    start_codes = _extract_m_codes(raw)
    end_codes = _extract_m_codes(raw_end)
    if raw[:1] in {"M", "m"} and not start_codes:
        _strict_m_code(re.sub(r"\s+", "", raw), "M码起始")
    if raw_end[:1] in {"M", "m"} and not end_codes:
        _strict_m_code(re.sub(r"\s+", "", raw_end), "M码结束")
    if len(start_codes) > 1 or len(end_codes) > 1:
        return tuple((code, None, True) for code in (*start_codes, *end_codes))
    if len(start_codes) == 1 and len(end_codes) == 1:
        return ((start_codes[0], end_codes[0], start_codes[0] == end_codes[0]),)
    if not start_codes and len(end_codes) == 1:
        return ((None, end_codes[0], True),)
    if len(start_codes) == 1 and not raw_end:
        raw = start_codes[0]
    # 读取旧结果/人工复制值时，先丢弃历史括号内完整展开列表；否则
    # ``/`` 分割会把 ``M...(M...)`` 拆成非法 token。该兼容清理只匹配
    # 严格 M 码列表，不会触碰普通业务括号文本。
    legacy_suffix = _M_EXPANSION_SUFFIX_RE.fullmatch(raw)
    if legacy_suffix is not None:
        raw = legacy_suffix.group("head").strip()
    tokens = [token.strip() for token in _SEQUENCE_SPLIT_RE.split(raw) if token.strip()]
    if not tokens:
        return ()
    parts: list[tuple[str | None, str | None, bool]] = []
    contains_m = any(token[:1] in {"M", "m"} for token in tokens)
    if not contains_m:
        return ()
    for token in tokens:
        match = _M_RANGE_RE.fullmatch(token)
        if match is not None:
            start_code = match.group("start")
            end_code = match.group("end")
            parts.append((start_code, end_code, start_code == end_code))
            continue
        # 含 M 但不是严格单码时，交由 expand_m_range 统一报出明确错误。
        parts.append((token, None, True))
    return tuple(parts)


def build_m_code_display(
    parts: Iterable[tuple[str | None, str | None, bool]],
    *,
    warning_callback: MCodeWarningCallback | None = None,
) -> str | None:
    """按 VBA 顺序展开、压缩并生成 M 码展示文本。

    VBA 的去重是有意不对称的：单端点单码去重，范围展开不做全局去重，
    也不与单码互相去重。压缩只针对展示序列中相邻且连续的同前缀编码。
    """

    full_codes: list[str] = []
    seen_single: set[str] = set()
    for start, end, is_single in parts:
        expanded = expand_m_range(start, end, warning_callback=warning_callback)
        if not expanded:
            continue
        if is_single:
            code = expanded[0]
            if code in seen_single:
                continue
            seen_single.add(code)
            full_codes.append(code)
        else:
            full_codes.extend(expanded)
    if not full_codes:
        return None

    compressed: list[str] = []
    index = 0
    while index < len(full_codes):
        start_code = full_codes[index]
        end_index = index
        while end_index + 1 < len(full_codes):
            current = full_codes[end_index]
            following = full_codes[end_index + 1]
            if current[:8] != following[:8] or int(following[8:]) != int(current[8:]) + 1:
                break
            end_index += 1
        if end_index - index >= 1:
            compressed.append(f"{start_code}~{full_codes[end_index]}")
        else:
            compressed.append(start_code)
        index = end_index + 1
    # ``/`` 是 v1.2 唯一的 M 码间分隔符；``~`` 仍保留为同前缀连续
    # 范围的压缩表示。旧版曾在压缩值后追加 ``(完整展开列表)``，会让
    # Excel、SQL、API 主字段和附表 JSON 对同一范围产生两种语义；新生成链
    # 只发送括号前的压缩结果。``full_codes`` 仍在内存中用于去重/压缩，
    # 不再拼入最终字段；斜杠只存在于字段文本，不参与路径拼接。
    return canonicalize_m_code_separators("/".join(compressed))


def _value(row: SourceRow, index: int) -> str:
    return str(row.values[index]).strip() if len(row.values) > index and row.values[index] is not None else ""


def _supplier_source(row: SourceRow, order_number: str) -> SourceRow:
    """为组合首行写入 TK 供应商 effective 值并保留 raw provenance。"""

    raw_supplier = row.raw_supplier
    if raw_supplier is None or (
        not raw_supplier
        and not row.supplier_defaulted
        and len(row.values) > 5
        and row.values[5] is not None
        and str(row.values[5]).strip()
    ):
        raw_supplier = "" if len(row.values) <= 5 or row.values[5] is None else str(row.values[5])
    effective, raw, defaulted = effective_supplier_for_order(order_number, raw_supplier)
    values = list(row.values)
    while len(values) <= 5:
        values.append("")
    values[5] = effective
    # 即使 effective 与当前值相同，也要补齐 raw/defaulted 字段，保证
    # 直接构造 SourceRow 的调用方不会丢失 TK 默认值证据。
    return replace(
        row,
        values=tuple(values),
        raw_supplier=raw,
        supplier_defaulted=defaulted,
    )


def _consistency_value(row: SourceRow, field_name: str) -> str:
    """读取组合一致性字段，并兼容直接构造的扩展 SourceRow。"""

    if field_name == "measurement_unit":
        return row.measurement_unit.strip() or _value(row, 18)
    if field_name == "procedure_name":
        return row.procedure_name.strip() or _value(row, 16)
    if field_name == "item_name":
        return _value(row, 4)
    if field_name == "supplier":
        raw_supplier = row.raw_supplier
        if raw_supplier is None or (
            not raw_supplier
            and not row.supplier_defaulted
            and len(row.values) > 5
            and row.values[5] is not None
            and str(row.values[5]).strip()
        ):
            raw_supplier = "" if len(row.values) <= 5 or row.values[5] is None else str(row.values[5])
        effective, _, _ = effective_supplier_for_order(row.order_number, raw_supplier)
        return effective
    raise CombinationAggregationError(f"未知组合一致性字段：{field_name}")


def _sequence_display_for_group(
    rows: list[SourceRow],
    *,
    warning_callback: MCodeWarningCallback | None = None,
) -> str | None:
    all_parts: list[tuple[str | None, str | None, bool]] = []
    has_m = False
    legacy_values: list[str] = []
    for row in rows:
        values = list(row.values) + [""] * 16
        raw = "" if values[14] is None else str(values[14]).strip()
        raw_end = "" if values[15] is None else str(values[15]).strip()
        if raw:
            legacy_values.append(raw)
        if raw_end:
            legacy_values.append(raw_end)
        row_parts = _sequence_parts(row)
        if row_parts:
            has_m = True
            all_parts.extend(row_parts)
    if has_m:
        if any(value and value[:1] not in {"M", "m"} for value in legacy_values):
            raise CombinationAggregationError("同一组合的序列码不能混用 M 码和其他格式")
        return build_m_code_display(all_parts, warning_callback=warning_callback)
    # 历史非 M 序列值没有 VBA M 码语义；保留首个值，避免旧输入被破坏。
    return legacy_values[0] if legacy_values else None


def aggregate_task(
    task: OrderTask,
    *,
    warning_callback: MCodeWarningCallback | None = None,
) -> tuple[InspectionCombination, ...]:
    """将一个送检单任务聚合为组合对象。"""

    if task.errors:
        raise CombinationAggregationError(f"送检单号 {task.order_number} 存在未解决异常：{'；'.join(task.errors)}")
    ordered_rows = sorted(enumerate(task.rows), key=lambda item: (item[1].source_row, item[0]))
    counters: dict[str, int] = {}
    relative_by_index: dict[int, int] = {}
    for index, row in ordered_rows:
        order = row.order_number.strip()
        if not order:
            continue
        counter_key = order.casefold()
        counters[counter_key] = counters.get(counter_key, 0) + 1
        relative_by_index[index] = counters[counter_key]

    groups: "OrderedDict[tuple[str, str], dict[str, object]]" = OrderedDict()
    for index, row in ordered_rows:
        order = row.order_number.strip()
        item_code = row.item_code.strip()
        if not order:
            raise CombinationAggregationError(f"源行 {row.source_row} 的送检单号不能为空")
        if not item_code:
            raise CombinationAggregationError(f"送检单 {order} 源行 {row.source_row} 的物料编号不能为空")
        quantity = parse_positive_integer(_value(row, 9))
        key = (order, item_code)
        if key not in groups:
            groups[key] = {
                "rows": [],
                "quantities": [],
                "line_numbers": [],
                "consistency": {
                    "item_name": _consistency_value(row, "item_name"),
                    "supplier": _consistency_value(row, "supplier"),
                    "measurement_unit": _consistency_value(row, "measurement_unit"),
                    "procedure_name": _consistency_value(row, "procedure_name"),
                },
            }
        else:
            existing = groups[key]["consistency"]
            assert isinstance(existing, dict)
            actual_fields = {
                "item_name": _consistency_value(row, "item_name"),
                "supplier": _consistency_value(row, "supplier"),
                "measurement_unit": _consistency_value(row, "measurement_unit"),
                "procedure_name": _consistency_value(row, "procedure_name"),
            }
            for field_name, expected_value in existing.items():
                if actual_fields[field_name] != expected_value:
                    raise CombinationAggregationError(
                        f"送检单 {order}、物料 {item_code} 的 {field_name} 在组合内不一致"
                    )
        group = groups[key]
        rows = group["rows"]
        assert isinstance(rows, list)
        rows.append(row)
        quantities = group["quantities"]
        assert isinstance(quantities, list)
        quantities.append(quantity)
        line_numbers = group["line_numbers"]
        assert isinstance(line_numbers, list)
        line_numbers.append(relative_by_index[index])

    result: list[InspectionCombination] = []
    for (order, item_code), payload in groups.items():
        rows = payload["rows"]
        quantities = payload["quantities"]
        line_numbers = payload["line_numbers"]
        assert isinstance(rows, list) and isinstance(quantities, list) and isinstance(line_numbers, list)
        # 只把组合输出副本中的供应商字段归一化；``source_rows`` 仍保留
        # 原始行，便于审计比较空值、空白和值为 NA 的不同来源。
        lead = _supplier_source(rows[0], order)
        sequence_display = _sequence_display_for_group(
            rows,
            warning_callback=(
                None
                if warning_callback is None
                else lambda detail, order=order, item_code=item_code: warning_callback(
                    f"送检单 {order}、物料 {item_code}：{detail}"
                )
            ),
        )
        values = list(lead.values) + [""] * 16
        values[0] = order
        values[3] = item_code
        values[9] = sum(quantities)
        values[14] = sequence_display or ""
        # 组合输出将 O 列作为最终序列码展示；P 列的原始结束值不能继续
        # 残留，否则结果行会同时呈现未聚合的起止字段。
        values[15] = ""
        aggregate_source = replace(
            lead,
            order_number=order,
            item_code=item_code,
            values=tuple(values[:16]),
            sequence_start=None,
            sequence_end=None,
        )
        sorted_lines = tuple(sorted(int(number) for number in line_numbers))
        trigger_basis = f"{order}-{'/'.join(str(number) for number in sorted_lines)}"
        result.append(
            InspectionCombination(
                order_number=order,
                item_code=item_code,
                source=aggregate_source,
                quantity=sum(quantities),
                relative_line_numbers=sorted_lines,
                trigger_basis=trigger_basis,
                sequence_code=sequence_display,
                source_rows=tuple(rows),
            )
        )
    return tuple(result)
