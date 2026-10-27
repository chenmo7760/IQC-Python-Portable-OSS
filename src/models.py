from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


SOURCE_HEADERS = (
    "送检单号",
    "采购凭证号",
    "物项编码",
    "物料编号",
    "物料描述",
    "供应商名称",
    "采购合同号",
    "项目立项号",
    "项目名称",
    "进货数",
    "不合格数",
    "检验日期",
    "备注",
    "备注1",
    "序列码",
    "空列",
)


# Windchill 有时返回“文档编号_检查单中文名称”，有时直接返回中文名称。
# 编号前缀以 ASCII 字符开头，分隔符支持半角/全角下划线；前缀内部允许
# 现场系统可能出现的其他字符。
# 这样不会把正常的中文下划线名称误当成带前缀结果；优先选择分隔符后
# 以书名号或中文字符开头的位置，避免把名称内部的 ASCII 下划线（如
# ``检查单_示例``）误判成文档编号分隔符。对于名称本身以拉丁字符开头的
# 历史规范文档，只有在前缀呈现至少两个连字符分段、名称仍包含中文时才
# 使用兼容性回退规则。归一化仅用于匹配/展示，不改写外部返回对象。
_PROCEDURE_NAME_PREFIX_RE = re.compile(
    r"^[A-Za-z0-9][^\r\n]*?[_＿](?P<name>\s*[《\u3400-\u9fff][^\r\n]*)$"
)
_PROCEDURE_NAME_PREFIX_FALLBACK_RE = re.compile(
    r"^[A-Za-z0-9]+(?:-[A-Za-z0-9#*]+){2,}[_＿]"
    r"(?P<name>(?=[^\r\n]*[\u3400-\u9fff]).+)$"
)
_PROCEDURE_NAME_RESULT_SUFFIX_RE = re.compile(
    r"^(?P<name>.*?(?:检查单|检验单))\s*满足\s*[:：].*$"
)
_PROCEDURE_NAME_WRAPPED_RESULT_SUFFIX_RE = re.compile(
    r"^(?P<name>《[^\r\n]*?(?:检查单|检验单)》)\s*满足\s*[:：].*$"
)
# Windchill 的“进/退货检验要求”有时把规范名称包在书名号中，
# 再以全角冒号连接文档编号：``《模板名称》：DEMO-...``。该内容仍是
# 同一检查单名称，不应因冒号后的编号导致模板匹配失败；编号必须以
# ASCII 字符开头，避免吞掉合法的中文名称冒号文本。
_PROCEDURE_NAME_WRAPPED_DOCUMENT_RE = re.compile(
    r"^《(?P<name>[^\r\n》]+)》\s*[:：]\s*[A-Za-z0-9][^\r\n]*$"
)
_PROCEDURE_NAME_EXTENSION_RE = re.compile(r"\.(?:docx?|xlsx?|pdf)$", re.IGNORECASE)
# 只移除名称末尾明确允许的标点；不把任意尾部文本（尤其是业务编码）
# 当作噪声剥除。扩展名单独处理，避免把未知扩展名误改写。
_PROCEDURE_NAME_TRAILING_PUNCTUATION_RE = re.compile(
    r"[\s\u3001\u3002\uff0c\uff1b\uff1a\uff01\uff1f,.;:!?]+$"
)
_PROCEDURE_NAME_TRAILING_CLOSING_MARK_RE = re.compile(r"[）】」』”’]+$")
_PROCEDURE_NAME_TRAILING_BOOK_MARK_RE = re.compile(r"》+$")

# M 码是业务字段，不是文件路径。旧版结果曾用半角/全角分号、逗号或顿号
# 连接多个 M 码；这些分隔符在不同 Excel/WPS 版本中容易被再次解释。v1.2
# 统一由这一处把“同一字段内的多个 M 码”转换成半角 ``/``。只有文本中
# 确实出现严格形态的 M+11 位数字时才执行替换，因此不会把普通备注、路径
# 或不含 M 码的历史序列值误改写。
_M_CODE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])M[0-9]{11}(?![0-9])")
_M_CODE_LEGACY_SEPARATOR_RE = re.compile(r"[;,；，、]+")


def canonicalize_m_code_separators(value: object) -> str:
    """将包含 M 码的序列文本规范化为 ``/`` 分隔。

    该函数只负责分隔符归一，不负责 M 码校验、范围展开或路径处理。这样
    模板读取层可以继续保留源文本，聚合/Excel/SQL 输出层则共享同一规则；
    不含严格 M 码的文本（例如 ``S001-S003``）保持原值，避免改变历史语义。
    """

    text = "" if value is None else str(value).strip()
    if not text or _M_CODE_TOKEN_RE.search(text) is None:
        return text
    return _M_CODE_LEGACY_SEPARATOR_RE.sub("/", text)


def effective_supplier_for_order(order_number: object, raw_supplier: object) -> tuple[str, str, bool]:
    """返回 ``(effective, raw, defaulted)`` 供应商三元组。

    v1.2 只对大写 ``TK`` 开头的送检单、且源供应商为空的情形填充 ``NA``。
    大小写不做推测，非 TK 单号及已有供应商文本原样保留。``raw`` 不裁剪
    内部内容，仅把 ``None`` 表示成空串，确保 diagnostic 能证明默认值的
    来源，而不是把业务文本覆盖成 ``NA`` 后丢失 provenance。
    """

    order = "" if order_number is None else str(order_number).strip()
    raw = "" if raw_supplier is None else str(raw_supplier)
    if order.startswith("TK") and not raw.strip():
        return "NA", raw, True
    return raw.strip(), raw, False


def normalize_procedure_name(value: object) -> str:
    """规范化 Windchill 检查单名称，并保证结果幂等。

    例如 ``DEMO-...-01-04_合成类别06进货检查单`` 归一化为
    ``合成类别06进货检查单``；外层 ``《》`` 书名号、明确的结果说明后缀、
    常见文件扩展名和尾部标点也会按白名单移除。处理顺序是先剥外层书名号、
    再剥 ASCII 文档编号前缀，然后重复上述清理直到稳定；不会泛化剥除尾部
    业务编码或任意文本。
    """

    text = "" if value is None else str(value).strip()
    while True:
        previous = text

        # 书名号外面可能紧跟一个导出标点（例如 ``《名称》；``）；先
        # 去掉这个白名单标点，恢复成完整的成对书名号后再按规定剥离。
        text = _PROCEDURE_NAME_TRAILING_PUNCTUATION_RE.sub("", text).strip()

        # Windchill 可能把文档编号和中文名称整体包在书名号中；先剥
        # 书名号，下一步才能识别出里面的 ASCII 编号前缀。
        wrapped_document = _PROCEDURE_NAME_WRAPPED_DOCUMENT_RE.match(text)
        if wrapped_document is not None:
            text = f"《{wrapped_document.group('name').strip()}》"
        if len(text) >= 2 and text.startswith("《") and text.endswith("》"):
            text = text[1:-1].strip()

        # 编号前缀以 ASCII 字符开头，分隔符支持半角/全角下划线；优先
        # 选择后面以中文或书名号开头的分隔符，保留中文名称内部下划线。
        # 拉丁字符开头的历史名称由兼容性回退规则处理。
        match = _PROCEDURE_NAME_PREFIX_RE.match(text)
        if match is None:
            match = _PROCEDURE_NAME_PREFIX_FALLBACK_RE.match(text)
        if match is not None:
            text = match.group("name").strip()

        # 成对书名号外偶尔还会附带一个闭合标点（如 ``）``/``】``），
        # 或源数据只剩一个多余的 ``》``；只按白名单去除这些尾部闭合符号。
        # 若当前已恢复完整的 ``《名称》``，先保留闭合符号，下一轮再剥外层，
        # 避免前缀剥离后把合法书名号破坏成单边标记。
        if not (len(text) >= 2 and text.startswith("《") and text.endswith("》")):
            text = _PROCEDURE_NAME_TRAILING_CLOSING_MARK_RE.sub("", text).strip()
            # 单独残留的 ``》`` 只在当前文本不是一个成对书名号时移除；
            # 避免连同合法的外层 ``《名称》`` 一起剥坏。
            if not (len(text) >= 2 and text.startswith("《") and text.endswith("》")):
                text = _PROCEDURE_NAME_TRAILING_BOOK_MARK_RE.sub("", text).strip()

        # 部分 Windchill 返回文本会在检查单名称后追加“满足：文档编号”说明；
        # 空格可有可无，但必须出现中文/半角冒号，避免误删正常名称中的“满足”。
        # 书名号有时只包住名称、把“满足”放在书名号外，先保留成对书名号，
        # 下一轮再剥外层，避免留下单边书名号。
        result_suffix = _PROCEDURE_NAME_WRAPPED_RESULT_SUFFIX_RE.match(text)
        if result_suffix is None:
            result_suffix = _PROCEDURE_NAME_RESULT_SUFFIX_RE.match(text)
        if result_suffix is not None:
            text = result_suffix.group("name").strip()

        # 文件扩展名只按显式白名单处理；未知扩展名保留，防止误删业务文本。
        text = _PROCEDURE_NAME_EXTENSION_RE.sub("", text).strip()

        # 末尾标点只按显式白名单处理，标点剥离后可能再次暴露书名号、
        # 编号前缀或扩展名，因此整个流程循环到稳定。
        text = _PROCEDURE_NAME_TRAILING_PUNCTUATION_RE.sub("", text).strip()

        if text == previous:
            return text


@dataclass(frozen=True)
class SourceRow:
    order_number: str
    item_code: str
    sheet_name: str
    source_row: int
    values: tuple[Any, ...]
    # 月度表把序列信息放在 O/P 列；保留显式起止字段，兼容 VBA 的
    # sequence_start/sequence_end 语义，同时不破坏旧版五参数构造。
    sequence_start: str | None = None
    sequence_end: str | None = None
    measurement_unit: str = ""
    procedure_name: str = ""
    # 供应商默认值只在 TK 单号且源值为空时生效。保留精确源文本和是否
    # 发生默认，供 Excel/SQL/diagnostic 三条输出链路共同追溯；新增字段
    # 置于末尾以保持旧版位置参数构造兼容。
    raw_supplier: str | None = None
    supplier_defaulted: bool = False


@dataclass
class OrderTask:
    order_number: str
    rows: list[SourceRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InspectionCombination:
    """同一送检单、同一物料聚合后的业务对象。

    ``source`` 是组合首行的副本，数量和序列码已替换为聚合结果；下游
    模板、抽样和 SQL 只消费此对象，避免各层重新按源行计算。
    """

    order_number: str
    item_code: str
    source: SourceRow
    quantity: int
    relative_line_numbers: tuple[int, ...]
    trigger_basis: str
    sequence_code: str | None = None
    source_rows: tuple[SourceRow, ...] = field(default_factory=tuple)

    @property
    def item_name(self) -> str:
        values = self.source.values
        return str(values[4]).strip() if len(values) > 4 else ""

    @property
    def supplier(self) -> str:
        values = self.source.values
        raw_supplier = self.source.raw_supplier
        if raw_supplier is None or (
            not raw_supplier
            and not self.source.supplier_defaulted
            and len(values) > 5
            and values[5] is not None
            and str(values[5]).strip()
        ):
            raw_supplier = "" if len(values) <= 5 or values[5] is None else str(values[5])
        return effective_supplier_for_order(self.order_number, raw_supplier)[0]

    @property
    def raw_supplier(self) -> str:
        """组合首行供应商原文，供 SQL/diagnostic provenance 使用。"""

        values = self.source.values
        if self.source.raw_supplier is not None and (
            self.source.raw_supplier
            or self.source.supplier_defaulted
            or len(values) <= 5
            or values[5] is None
            or not str(values[5]).strip()
        ):
            return self.source.raw_supplier
        return "" if len(values) <= 5 or values[5] is None else str(values[5])

    @property
    def supplier_defaulted(self) -> bool:
        """该组合是否因 TK 空供应商触发了 NA 默认值。"""

        return effective_supplier_for_order(self.order_number, self.raw_supplier)[2]


@dataclass(frozen=True)
class Engineer:
    user_id: str
    name: str
    dept_id: str
    display_name: str
    enabled: bool = True


@dataclass(frozen=True)
class WindchillName:
    item_code: str
    name: str
    status: str
    # Windchill v1.1 A:E 返回扩展字段。旧版三参数构造继续有效；新字段
    # 仅保存 B/C/D/E 的原始/有效语义，不改变既有 item_code/name/status
    # 的普通检查单匹配契约。
    legacy_item_code: str = ""
    quality_grade: str = ""
    quality_grade_effective: str = ""
    quality_grade_basis: str = ""
    status_raw: str = ""

    @property
    def is_qa1(self) -> bool:
        """按 v1.1 D 列规则判断 QA1，不依赖名称或源 Excel 描述。"""

        # 真实 XLS 解析始终保留 D 列 raw ``quality_grade``；离线夹具也可
        # 只提供已规范化的 effective 值，因此仅在 raw 为空时回退 effective，
        # 不覆盖 raw provenance。
        source = self.quality_grade if self.quality_grade != "" else self.quality_grade_effective
        return source.strip().upper() == "QA1"

    @property
    def old_item_code(self) -> str:
        """兼容业务文档中的“旧物项识别码”命名。"""

        return self.legacy_item_code


@dataclass(frozen=True)
class DraftRow:
    """一个送检单-物料组合的结果对象；Q 列/measurement_unit 不进入该对象。"""

    source: SourceRow
    procedure_name: str
    qc_engineer: str


@dataclass(frozen=True)
class OrderDraft:
    """一个送检单号对应的完整结果草稿。"""

    order_number: str
    rows: tuple[DraftRow, ...]
