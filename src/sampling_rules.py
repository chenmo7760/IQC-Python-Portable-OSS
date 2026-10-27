"""独立判定允收选项，并按既有 GB2828/特殊方案规则计算检查数量。

``qcauto_procedure_model`` 中的 ``check_plan`` 和 ``accept_standard`` 是
面向工程师的文本配置；草稿表中的同名字段才是写入用的整数/抽样结果。
允收选项只依赖模型文本和物料描述，不受数量、抽样方案适用性影响。
检查数量保留既有计算及校验规则，无法解析时不猜测数量。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Final


class SamplingRuleError(ValueError):
    """验收标准、检验方案或进货数量无法安全解析。"""


@dataclass(frozen=True)
class SamplingDecision:
    """一条源物料对应的草稿字段。"""

    check_number: int | None
    accept_standard: int


# GB/T 2828.1 一次正常抽样方案的批量范围和检验水平代码，保持与旧 BAS
# IncomingGbStaticBatchDefinition 的 15 行定义一致。
_BATCH_CODE_ROWS: Final[tuple[tuple[Decimal, Decimal | None, tuple[str, ...]], ...]] = (
    (Decimal("2"), Decimal("8"), ("A", "A", "A", "A", "A", "A", "B")),
    (Decimal("9"), Decimal("15"), ("A", "A", "A", "A", "A", "B", "C")),
    (Decimal("16"), Decimal("25"), ("A", "A", "B", "B", "B", "C", "D")),
    (Decimal("26"), Decimal("50"), ("A", "B", "B", "C", "C", "D", "E")),
    (Decimal("51"), Decimal("90"), ("B", "B", "C", "C", "C", "E", "F")),
    (Decimal("91"), Decimal("150"), ("B", "B", "C", "D", "D", "F", "G")),
    (Decimal("151"), Decimal("280"), ("B", "C", "D", "E", "E", "G", "H")),
    (Decimal("281"), Decimal("500"), ("B", "C", "D", "E", "F", "H", "J")),
    (Decimal("501"), Decimal("1200"), ("C", "C", "E", "F", "G", "J", "K")),
    (Decimal("1201"), Decimal("3200"), ("C", "D", "E", "G", "H", "K", "L")),
    (Decimal("3201"), Decimal("10000"), ("C", "D", "F", "G", "J", "L", "M")),
    (Decimal("10001"), Decimal("35000"), ("C", "D", "F", "H", "K", "M", "N")),
    (Decimal("35001"), Decimal("150000"), ("D", "E", "G", "J", "L", "N", "P")),
    (Decimal("150001"), Decimal("500000"), ("D", "E", "G", "J", "M", "P", "Q")),
    (Decimal("500001"), None, ("D", "E", "H", "K", "N", "Q", "R")),
)

# 允收水准文本中的“加严/放宽”沿用现场附图的批量区间，不依赖 GB2828
# 正常抽样方案的检验水平或 AQL 文本；区间包含端点，数量 0/1 均按附图取 1。
_TIGHT_ACCEPTANCE_ROWS: Final[tuple[tuple[Decimal, Decimal | None, int], ...]] = (
    (Decimal("0"), Decimal("1"), 1),
    (Decimal("2"), Decimal("8"), 2),
    (Decimal("9"), Decimal("15"), 3),
    (Decimal("16"), Decimal("25"), 5),
    (Decimal("26"), Decimal("50"), 8),
    (Decimal("51"), Decimal("90"), 13),
    (Decimal("91"), Decimal("150"), 20),
    (Decimal("151"), Decimal("280"), 32),
    (Decimal("281"), Decimal("500"), 50),
    (Decimal("501"), Decimal("1200"), 80),
    (Decimal("1201"), Decimal("3200"), 125),
    (Decimal("3201"), Decimal("10000"), 200),
    (Decimal("10001"), Decimal("35000"), 315),
    (Decimal("35001"), Decimal("150000"), 500),
    (Decimal("150001"), Decimal("500000"), 800),
    (Decimal("500001"), None, 1250),
)

_RELAXED_ACCEPTANCE_ROWS: Final[tuple[tuple[Decimal, Decimal | None, int], ...]] = (
    (Decimal("0"), Decimal("1"), 1),
    (Decimal("2"), Decimal("15"), 2),
    (Decimal("16"), Decimal("50"), 3),
    (Decimal("51"), Decimal("150"), 5),
    (Decimal("151"), Decimal("500"), 8),
    (Decimal("501"), Decimal("3200"), 13),
    (Decimal("3201"), Decimal("35000"), 20),
    (Decimal("35001"), Decimal("500000"), 32),
    (Decimal("500001"), None, 50),
)

_SAMPLE_SIZE_BY_CODE: Final[dict[str, int]] = {
    "A": 2,
    "B": 3,
    "C": 5,
    "D": 8,
    "E": 13,
    "F": 20,
    "G": 32,
    "H": 50,
    "J": 80,
    "K": 125,
    "L": 200,
    "M": 315,
    "N": 500,
    "P": 800,
    "Q": 1250,
    "R": 2000,
}

_SUPPORTED_AQL: Final[frozenset[Decimal]] = frozenset(
    Decimal(value)
    for value in (
        "0",
        "0.01",
        "0.015",
        "0.025",
        "0.04",
        "0.065",
        "0.1",
        "0.15",
        "0.25",
        "0.4",
        "0.65",
        "1",
        "1.5",
        "2.5",
        "4",
        "6.5",
        "10",
        "15",
        "25",
        "40",
        "65",
        "100",
    )
)

_AQL_RE = re.compile(r"AQL\s*[:=]\s*(?P<value>\d+(?:\.\d+)?)", re.IGNORECASE)
_QA1_AQL_RE = re.compile(
    r"(?<![A-Za-z0-9])QA1[^0-9]{0,40}AQL\s*[:=]\s*(?P<value>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_OTHER_AQL_RE = re.compile(
    r"其他[^0-9]{0,40}AQL\s*[:=]\s*(?P<value>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_ITEM_QA1_RE = re.compile(r"(?<![A-Za-z0-9])QA1(?![A-Za-z0-9])", re.IGNORECASE)
# VBA 用 LOCATE 在整段方案文本中查找检验水平，不要求必须带 ``IL=`` 或
# “检验水平”前缀；保留 ASCII/全角罗马数字兼容现场已有文本。
#
# ``IL=S-1`` 需要特别注意：如果先从整段文本直接搜索 ``I``，会把
# ``IL`` 中的 I 误认为普通检验水平 I（索引 4），而不是 S-1（索引 0）。
# S-1 允许连接号两侧有空格；ASCII 罗马数字要求不紧邻字母/数字，避免
# 再次从 ``IL`` 前缀中截取 I。
_LEVEL_RE = re.compile(
    r"(?P<level>(?<![A-Za-z0-9])S\s*[-－]\s*[1-4](?![A-Za-z0-9])|(?<![A-Za-z0-9])III(?![A-Za-z0-9])|(?<![A-Za-z0-9])II(?![A-Za-z0-9])|(?<![A-Za-z0-9])I(?![A-Za-z0-9])|Ⅰ|Ⅱ|Ⅲ)",
    re.IGNORECASE,
)

# VBA IncomingGbStaticPlanDefinition 的 15 行。第三项是 22 个 AQL 单元格，
# ``*n`` 表示重复 n 次；“上/下”需要沿同一 AQL 列跳到相邻方案。
_STATIC_PLAN_DEFINITIONS: Final[tuple[tuple[str, int, tuple[str, ...]], ...]] = (
    ("A", 2, ("下*15", "0", "下*2", "1", "2", "3", "5")),
    ("B", 3, ("下*14", "0", "上", "下", "1", "2", "3", "5", "7")),
    ("C", 5, ("下*13", "0", "上", "下", "1", "2", "3", "5", "7", "10")),
    ("D", 8, ("下*12", "0", "上", "下", "1", "3", "3", "5", "7", "10", "14")),
    ("E", 13, ("下*11", "0", "上", "下", "1", "2", "3", "5", "7", "10", "14", "21")),
    ("F", 20, ("下*10", "0", "上", "下", "1", "2", "3", "5", "7", "10", "14", "21", "上")),
    ("G", 32, ("下*9", "0", "上", "下", "1", "2", "3", "5", "7", "10", "14", "21", "上*2")),
    ("H", 50, ("下*8", "0", "上", "下", "1", "2", "3", "5", "7", "10", "14", "21", "上*3")),
    ("J", 80, ("下*7", "0", "上", "下", "1", "2", "3", "5", "7", "10", "14", "21", "上*4")),
    ("K", 125, ("下*6", "0", "上", "下", "1", "2", "3", "5", "7", "10", "14", "21", "上*5")),
    ("L", 200, ("下*5", "0", "上", "下", "1", "2", "3", "5", "7", "10", "14", "21", "上*6")),
    ("M", 315, ("下*4", "0", "上", "下", "1", "2", "3", "5", "7", "10", "14", "21", "上*7")),
    ("N", 500, ("下*3", "0", "上", "下", "1", "2", "3", "5", "7", "10", "14", "21", "上*8")),
    ("P", 800, ("下*2", "0", "上", "下", "1", "2", "3", "5", "7", "10", "14", "21", "上*9")),
    ("Q", 1250, ("止", "0", "上", "下", "1", "2", "3", "5", "7", "10", "14", "21", "上*10")),
)


def _expand_plan_cells(encoded: tuple[str, ...]) -> tuple[str, ...]:
    cells: list[str] = []
    for item in encoded:
        if "*" in item:
            value, count_text = item.rsplit("*", 1)
            try:
                count = int(count_text)
            except ValueError as exc:
                raise SamplingRuleError("GB2828 静态抽样表重复次数无效") from exc
            if count <= 0:
                raise SamplingRuleError("GB2828 静态抽样表重复次数无效")
            cells.extend([value] * count)
        else:
            cells.append(item)
    if len(cells) != 22:
        raise SamplingRuleError("GB2828 静态抽样表必须包含 22 个 AQL 单元格")
    return tuple(cells)


_STATIC_PLANS: Final[tuple[tuple[str, int, tuple[str, ...]], ...]] = tuple(
    (code, sample, _expand_plan_cells(cells))
    for code, sample, cells in _STATIC_PLAN_DEFINITIONS
)
_STATIC_PLAN_INDEX: Final[dict[str, int]] = {code: index for index, (code, _, _) in enumerate(_STATIC_PLANS)}
_AQL_VALUES: Final[tuple[Decimal, ...]] = tuple(
    Decimal(value)
    for value in (
        "0", "0.01", "0.015", "0.025", "0.04", "0.065", "0.1", "0.15", "0.25", "0.4",
        "0.65", "1", "1.5", "2.5", "4", "6.5", "10", "15", "25", "40", "65", "100",
    )
)


@dataclass(frozen=True)
class _Acceptance:
    status: int
    aql: Decimal | None
    textual: bool


def _parse_decimal(value: object, field_name: str) -> Decimal:
    text = "" if value is None else str(value).strip().replace(",", "")
    if not text:
        raise SamplingRuleError(f"{field_name} 为空")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise SamplingRuleError(f"{field_name} 不是有效数字") from exc
    if not number.is_finite() or number < 0:
        raise SamplingRuleError(f"{field_name} 必须是非负有限数字")
    return number


def _parse_quantity(value: object) -> Decimal:
    quantity = _parse_decimal(value, "quantity")
    if quantity != quantity.to_integral_value():
        raise SamplingRuleError("quantity 必须是整数")
    return quantity


def _aql_from_match(match: re.Match[str], field_name: str) -> Decimal:
    value = _parse_decimal(match.group("value"), field_name)
    if value not in _SUPPORTED_AQL:
        raise SamplingRuleError(f"{field_name} 不在旧 BAS 支持的 AQL 表内")
    return value


def resolve_acceptance_standard(*, accept_standard: object, item_name: object) -> int:
    """判定 QA1/其他选项；不读取数量、检验方案或验证抽样表 AQL。

    已有整数快照保留原值；没有可识别的双 AQL 选项时仍返回 -1。
    单件或特殊抽样无需计算 AQL，不能因为跳过抽样而清空允收选项。
    """
    text = "" if accept_standard is None else str(accept_standard).strip()
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text, 10)
    if _QA1_AQL_RE.search(text) is None or _OTHER_AQL_RE.search(text) is None:
        return -1
    return 0 if _ITEM_QA1_RE.search("" if item_name is None else str(item_name)) else 1


def _parse_acceptance(value: object, item_name: object) -> _Acceptance:
    text = "" if value is None else str(value).strip()
    # 保留测试夹具和已迁移快照中已经是草稿状态码的兼容性；生产模型
    # 的文本值则必须走下面的 QA1/AQL 规则。
    if re.fullmatch(r"[+-]?\d+", text):
        return _Acceptance(int(text, 10), None, False)
    if not text:
        raise SamplingRuleError("accept_standard 为空")

    qa1_match = _QA1_AQL_RE.search(text)
    other_match = _OTHER_AQL_RE.search(text)
    if qa1_match is None:
        # “零缺陷”等文本没有 AQL；旧 BAS 对这类模型不生成抽样状态。
        generic_match = _AQL_RE.search(text)
        if generic_match is None:
            return _Acceptance(-1, None, True)
        # 旧 BAS 在没有 QA1/其他双分支时把整段文本作为通用 AQL，
        # 但不生成 0/1 接收状态；保留该明确的兼容语义。
        return _Acceptance(-1, _aql_from_match(generic_match, "accept_standard.AQL"), True)
    if other_match is None:
        generic_match = _AQL_RE.search(text)
        if generic_match is None:
            raise SamplingRuleError("accept_standard 缺少可解析的 AQL")
        return _Acceptance(-1, _aql_from_match(generic_match, "accept_standard.AQL"), True)

    qa1_aql = _aql_from_match(qa1_match, "accept_standard.QA1.AQL")
    other_aql = _aql_from_match(other_match, "accept_standard.其他.AQL")
    status = resolve_acceptance_standard(accept_standard=value, item_name=item_name)
    return _Acceptance(status, qa1_aql if status == 0 else other_aql, True)


def _level_index(check_plan: str) -> int:
    normalized = check_plan.replace("－", "-")
    match = _LEVEL_RE.search(normalized)
    if match is None:
        raise SamplingRuleError("check_plan 未解析出检验水平 IL")
    level = re.sub(r"\s+", "", match.group("level").upper()).replace("－", "-")
    return {
        "S-1": 0,
        "S-2": 1,
        "S-3": 2,
        "S-4": 3,
        "Ⅰ": 4,
        "I": 4,
        "Ⅱ": 5,
        "II": 5,
        "Ⅲ": 6,
        "III": 6,
    }[level]


def _normal_sampling_code(check_plan: object, quantity: Decimal) -> str | None:
    text = "" if check_plan is None else str(check_plan).strip()
    compact = text.upper().replace(" ", "").replace("/", "").replace("T", "")
    if "GB2828" not in compact or "正常" not in text:
        return None
    if any(marker in text for marker in ("加严", "放宽", "首批", "后续")):
        return None
    level_index = _level_index(text)
    for minimum, maximum, codes in _BATCH_CODE_ROWS:
        if quantity >= minimum and (maximum is None or quantity <= maximum):
            return codes[level_index]
    return None


def _special_acceptance_sample(
    check_plan: object,
    accept_standard: object,
    quantity: Decimal,
) -> int | None:
    """Return the strict/relaxed-table sample size when either field selects it."""

    texts = tuple(
        "" if value is None else str(value).strip()
        for value in (check_plan, accept_standard)
    )
    markers = tuple(
        marker
        for marker in ("加严", "放宽")
        if any(marker in text for text in texts)
    )
    if not markers:
        return None
    if len(markers) > 1:
        raise SamplingRuleError("accept_standard 同时包含加严和放宽，无法确定抽样表")
    rows = _TIGHT_ACCEPTANCE_ROWS if markers[0] == "加严" else _RELAXED_ACCEPTANCE_ROWS
    for minimum, maximum, sample_size in rows:
        if quantity >= minimum and (maximum is None or quantity <= maximum):
            return sample_size
    raise SamplingRuleError("accept_standard 加严/放宽批量区间无效")


def _static_plan_cell(plan_index: int, aql_index: int) -> str:
    if plan_index < 0 or plan_index >= len(_STATIC_PLANS):
        raise SamplingRuleError("GB2828 静态抽样方案索引无效")
    if aql_index < 1 or aql_index > 22:
        raise SamplingRuleError("GB2828 静态 AQL 列无效")
    return _STATIC_PLANS[plan_index][2][aql_index - 1]


def _resolved_static_sample(code: str, aql: Decimal) -> int:
    """解析 VBA 静态抽样表中的“上/下”指针。"""

    plan_index = _STATIC_PLAN_INDEX.get(code.upper())
    if plan_index is None:
        # VBA 的 R 方案没有对应静态定义，保持不可映射。
        return -1
    try:
        aql_index = _AQL_VALUES.index(aql) + 1
    except ValueError:
        return -1
    visited: set[int] = set()
    while True:
        if plan_index in visited:
            return -1
        visited.add(plan_index)
        direction = _static_plan_cell(plan_index, aql_index)
        # VBA 将“止”视为当前抽样方案的终点，仍返回当前行的样本量；
        # 只有越界、循环或不存在的方案才是不可映射。
        if direction == "止":
            return _STATIC_PLANS[plan_index][1]
        if direction == "上" or direction == "下":
            plan_index += -1 if direction == "上" else 1
            if plan_index < 0 or plan_index >= len(_STATIC_PLANS):
                return -1
            continue
        return _STATIC_PLANS[plan_index][1]


SAMPLING_TRACE_SCHEMA = "iqc-acceptance/88225703-v2"
SAMPLING_REASON_LABELS = {
    "quantity_one": "进货数量为 1，检查数量为 1",
    "special_acceptance_table": "检查数量采用加严或放宽抽样表",
    "numeric_snapshot_status": "整数快照未提供 AQL，检查数量留空",
    "quantity_below_two": "进货数量不足 2，检查数量留空",
    "no_aql": "未解析到 AQL，检查数量留空",
    "non_positive_aql": "AQL 为 0，检查数量留空",
    "normal_plan_not_applicable": "未匹配正常 GB2828 方案，检查数量留空",
    "static_sample_unmapped": "静态抽样表无法映射，检查数量留空",
    "normal_sampling": "检查数量按正常抽样表计算",
    "rule_error": "抽样输入解析失败，本条未生成草稿",
}
ACCEPTANCE_REASON_LABELS = {
    "qa1": "允收水准选择 QA1",
    "other": "允收水准选择其他",
    "numeric_snapshot_status": "允收水准沿用快照整数状态",
    "no_dual_option": "未识别 QA1/其他双选项，允收状态为 -1",
}


def resolve_sampling(
    *,
    check_plan: object,
    accept_standard: object,
    item_name: object,
    quantity: object,
    diagnostics: dict[str, object] | None = None,
) -> SamplingDecision:
    """独立完成允收选择和检查数量计算，再合成草稿字段。

    单件和特殊方案仍跳过 AQL 数值解析，但不跳过允收选项判定。
    数量计算的解析错误仍抛出 SamplingRuleError，不生成虚构结果。
    """

    trace: dict[str, object] = {
        "schema": SAMPLING_TRACE_SCHEMA,
        "item_name": "" if item_name is None else str(item_name),
        "item_has_qa1": _ITEM_QA1_RE.search("" if item_name is None else str(item_name)) is not None,
        "quantity_input": "" if quantity is None else str(quantity),
        "quantity_input_type": type(quantity).__name__,
        "model_check_plan": "" if check_plan is None else str(check_plan),
        "model_accept_standard": "" if accept_standard is None else str(accept_standard),
        "parsed_accept_standard": None,
        "selected_aql": None,
        "sampling_code": None,
        "status": "STARTED",
    }
    try:
        selected = resolve_acceptance_standard(accept_standard=accept_standard, item_name=item_name)
        trace["parsed_accept_standard"] = selected
        trace["acceptance_reason"] = (
            "numeric_snapshot_status"
            if re.fullmatch(r"[+-]?\d+", trace["model_accept_standard"].strip())
            else {0: "qa1", 1: "other"}.get(selected, "no_dual_option")
        )
        check_number = _resolve_check_number(
            check_plan=check_plan, accept_standard=accept_standard,
            item_name=item_name, quantity=quantity, trace=trace,
        )
        result = SamplingDecision(check_number, selected)
    except SamplingRuleError as exc:
        trace.update(status="ERROR", reason="rule_error", check_number_reason="rule_error", error=str(exc))
        raise
    else:
        trace.update(status="RESOLVED", check_number=result.check_number, accept_standard=result.accept_standard)
        return result
    finally:
        if diagnostics is not None:
            diagnostics.clear()
            diagnostics.update(trace)


def _resolve_check_number(
    *, check_plan: object, accept_standard: object, item_name: object,
    quantity: object, trace: dict[str, object],
) -> int | None:
    def finish(check_number: int | None, reason: str) -> int | None:
        # reason 保留为检查数量原因的兼容别名；允收原因单独记录。
        trace["reason"] = reason
        trace["check_number_reason"] = reason
        return check_number

    amount = _parse_quantity(quantity)
    trace["quantity"] = int(amount)
    # 单件检查数量仍为 1，不需要验证 AQL 数值；允收选择由调用方独立处理。
    if amount == 1:
        return finish(1, "quantity_one")
    special_sample = _special_acceptance_sample(check_plan, accept_standard, amount)
    if special_sample is not None:
        return finish(special_sample, "special_acceptance_table")
    parsed = _parse_acceptance(accept_standard, item_name)
    trace["selected_aql"] = str(parsed.aql) if parsed.aql is not None else None
    if not parsed.textual:
        return finish(None, "numeric_snapshot_status")
    if amount < 2:
        return finish(None, "quantity_below_two")
    if parsed.aql is None:
        return finish(None, "no_aql")
    if parsed.aql <= 0:
        return finish(None, "non_positive_aql")
    code = _normal_sampling_code(check_plan, amount)
    trace["sampling_code"] = code
    if code is None:
        return finish(None, "normal_plan_not_applicable")
    sample_size = _resolved_static_sample(code, parsed.aql)
    if sample_size < 0:
        return finish(None, "static_sample_unmapped")
    return finish(min(int(amount), sample_size), "normal_sampling")
