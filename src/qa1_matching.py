"""QA1 关键零部件模板的纯匹配规则。

本模块只做“候选集合计算”，不负责弹窗、不自动选择候选，也不向数据库或
API 发请求。这样可以把第二级 QA1 规则和普通检查单名称匹配隔离，既便于
离线单元测试，也避免把一个多候选结果误写成服务端草稿。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable, Sequence

from .mysql_query import ProcedureModel


_RANGE_SEPARATOR_RE = re.compile(r"[\s,，;；、/]+")
_INLINE_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<start>[A-Za-z0-9]+)\s*(?:~|～|至)\s*"
    r"(?P<end>[A-Za-z0-9]+)(?![A-Za-z0-9])"
)
_WILDCARD_SEGMENT_RE = re.compile(r"^[A-Z0-9#*]+$")


@dataclass(frozen=True)
class Qa1MatchResult:
    """一个 QA1 物项的候选结果。

    ``candidates`` 始终按只读快照原顺序返回；调用方只有在长度恰为 1 时
    才能把 ``selected_model`` 视为可提交模板。多候选和未命中都不能静默
    选择，也不会改变普通进检草稿的生成结果。
    """

    legacy_item_code: str
    candidates: tuple[ProcedureModel, ...] = ()
    matched_by: str = "no_match"
    detail: str = ""
    # 该字段仅在唯一命中时填充，避免在结果对象上隐式取首项。
    selected_model: ProcedureModel | None = field(default=None, compare=False)

    @property
    def is_unique(self) -> bool:
        return len(self.candidates) == 1

    @property
    def requires_manual_selection(self) -> bool:
        return len(self.candidates) > 1


@dataclass(frozen=True)
class Qa1DraftSpec:
    """现有生成编排交给后续 API 层的 QA1 草稿候选说明。

    ``combination`` 只能是已聚合的 InspectionCombination（类型不在此处
    反向导入，避免和 generation_service 循环依赖）。唯一命中时
    ``selected_model`` 有值，可由调用方构造附加草稿；多候选时只保留候选
    供 UI 明确选择，``selected_model`` 必须为空，禁止直接提交。
    """

    combination: Any
    legacy_item_code: str
    candidates: tuple[ProcedureModel, ...]
    matched_by: str
    selected_model: ProcedureModel | None = None
    # Windchill 名称经 normalize_procedure_name 处理后的展示值。QA1
    # 匹配本身按物料/旧编码执行，但确认表仍需展示这条业务名称上下文。
    normalized_name: str = ""

    @property
    def can_submit(self) -> bool:
        return self.selected_model is not None and len(self.candidates) == 1


def _is_active(model: ProcedureModel) -> bool:
    """沿用普通模板选择的有效性条件，不为 QA1 猜选冻结/旧版本模板。"""

    return (
        str(model.is_lasted_version).strip() == "1"
        and str(model.is_frozen).strip() == "0"
        and str(model.processstate).strip() == "2"
    )


def _normal_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _code_in_apply_range(code: str, apply_range: object) -> bool:
    """判断一个物项编码是否落在 apply_range 的明确表达式内。

    适用范围历史上既有单值，也有 ``A~B`` 区间；分隔符只在范围文本中
    作为字段分隔使用。区间展开仅对“相同非数字前缀 + 等宽数字后缀”进行，
    不把任意带连字符的业务编码猜作区间，避免把 `ABC-01` 拆坏。
    """

    target = _normal_text(code)
    if not target:
        return False
    text = _normal_text(apply_range)
    if not text:
        return False
    # 适用范围经常是“适用于……物项 DEMO-MAT-031/DEMO-MAT-009”的中文说明，
    # 不能只按整 token 比较。用 ASCII 字母/数字边界提取目标编码，既能
    # 命中斜杠列表，也不会把目标当作任意无界 substring（例如 9468 不会
    # 命中 99999999680）。连字符属于编码内部合法字符，因此不参与边界。
    bounded_target = re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(target) + r"(?![A-Za-z0-9])"
    )
    if bounded_target.search(text) is not None:
        return True
    # 中文说明也可能把编码区间写成 ``DEMO-MAT-009 至 DEMO-MAT-031``；仅
    # 处理带明确区间符号、两端同前缀且等宽数字后缀的范围，拒绝对普通
    # 文本做无界数值推断。
    for range_match in _INLINE_RANGE_RE.finditer(text):
        start = range_match.group("start")
        end = range_match.group("end")
        match_start = re.fullmatch(r"(?P<prefix>.*?)(?P<number>\d+)$", start)
        match_end = re.fullmatch(r"(?P<prefix>.*?)(?P<number>\d+)$", end)
        match_target = re.fullmatch(r"(?P<prefix>.*?)(?P<number>\d+)$", target)
        if not (match_start and match_end and match_target):
            continue
        if (
            match_start.group("prefix") != match_end.group("prefix")
            or match_start.group("prefix") != match_target.group("prefix")
            or len(match_start.group("number")) != len(match_end.group("number"))
            or len(match_start.group("number")) != len(match_target.group("number"))
        ):
            continue
        if int(match_start.group("number")) <= int(match_target.group("number")) <= int(match_end.group("number")):
            return True
    for token in filter(None, _RANGE_SEPARATOR_RE.split(text)):
        if token == target:
            return True
        if "~" not in token:
            continue
        start, end = (part.strip() for part in token.split("~", 1))
        if not start or not end:
            continue
        match_start = re.fullmatch(r"(?P<prefix>.*?)(?P<number>\d+)$", start)
        match_end = re.fullmatch(r"(?P<prefix>.*?)(?P<number>\d+)$", end)
        match_target = re.fullmatch(r"(?P<prefix>.*?)(?P<number>\d+)$", target)
        if not (match_start and match_end and match_target):
            continue
        if (
            match_start.group("prefix") != match_end.group("prefix")
            or match_start.group("prefix") != match_target.group("prefix")
            or len(match_start.group("number")) != len(match_end.group("number"))
            or len(match_start.group("number")) != len(match_target.group("number"))
        ):
            continue
        low = int(match_start.group("number"), 10)
        high = int(match_end.group("number"), 10)
        if low <= int(match_target.group("number"), 10) <= high:
            return True
    return False


def _wildcard_segment(procedure_code: object) -> str | None:
    """取得 procedure_code 中含单字符占位符的严格大写中间段。"""

    text = _normal_text(procedure_code)
    if not text:
        return None
    for segment in text.split("-"):
        segment = segment.strip()
        if not any(char in segment for char in "#*") or not _WILDCARD_SEGMENT_RE.fullmatch(segment):
            continue
        return segment
    return None


def _procedure_code_matches(code: str, procedure_code: object) -> bool:
    """逐位判断模板中间段与 Windchill 旧编码是否存在共同实例。

    模板侧 ``#``/``*`` 和旧编码侧 ``#`` 都表示恰好一个大写字母或数字；
    旧编码侧 ``*`` 仍是普通固定字符，不扩展为通配。等长约束避免任一占位符
    跨位或跨 procedure_code 段匹配。
    """

    template = _wildcard_segment(procedure_code)
    legacy = _normal_text(code)
    if template is None or len(template) != len(legacy):
        return False
    wildcard_chars = frozenset({"#", "*"})
    alphanumeric = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    for template_char, legacy_char in zip(template, legacy):
        if template_char in wildcard_chars:
            if legacy_char != "#" and legacy_char not in alphanumeric:
                return False
        elif legacy_char == "#":
            if template_char not in alphanumeric:
                return False
        elif template_char != legacy_char:
            return False
    return True


def match_qa1_template(
    legacy_item_code: object,
    models: Iterable[ProcedureModel],
    *,
    item_code: object | None = None,
) -> Qa1MatchResult:
    """按 ``item_code→apply_range``、再 ``legacy_item_code→procedure_code`` 匹配 QA1。

    新调用必须同时提供业务物料编码（Excel D 列 ``item_code``）和
    Windchill v1.1 C 列旧编码 ``legacy_item_code``：第一级只用业务编码匹配
    ``apply_range``；仅当第一级完全未命中时，才用旧编码匹配
    ``procedure_code``。``N/A``/``NA``/``NULL`` 等占位值永远不会作为第二级
    猜测键。为保持旧调用兼容，省略 ``item_code`` 时首个参数同时作为两级
    查询键（历史行为）。唯一候选写入 ``selected_model``，不按顺序猜选。
    """

    legacy_code = _normal_text(legacy_item_code)
    # Old callers passed one key only; preserve that behavior while making the
    # new two-key path explicit at the call site.
    business_code = _normal_text(item_code) if item_code is not None else legacy_code
    active = tuple(model for model in models if _is_active(model))
    by_range = tuple(model for model in active if _code_in_apply_range(business_code, model.apply_range))
    if by_range:
        return Qa1MatchResult(
            legacy_item_code=legacy_code,
            candidates=by_range,
            matched_by="apply_range",
            detail="apply_range 命中 %d 个有效模板" % len(by_range),
            selected_model=by_range[0] if len(by_range) == 1 else None,
        )
    normalized_legacy = legacy_code.upper().replace(" ", "")
    legacy_is_placeholder = normalized_legacy in {"", "N/A", "NA", "NULL"}
    by_code = () if legacy_is_placeholder else tuple(
        model for model in active if _procedure_code_matches(legacy_code, model.procedure_code)
    )
    return Qa1MatchResult(
        legacy_item_code=legacy_code,
        candidates=by_code,
        matched_by="procedure_code" if by_code else "no_match",
        detail=(
            "procedure_code 中间段命中 %d 个有效模板" % len(by_code)
            if by_code
            else "apply_range 和 procedure_code 均未命中有效模板"
        ),
        selected_model=by_code[0] if len(by_code) == 1 else None,
    )


def choose_qa1_model(result: Qa1MatchResult, selected_model_id: object | None) -> ProcedureModel:
    """校验人工明确选择的候选；未选择/取消时拒绝提交。"""

    selected = _normal_text(selected_model_id)
    if not selected:
        raise ValueError("QA1 多候选未明确选择模板，不能提交")
    matches = tuple(model for model in result.candidates if _normal_text(model.id) == selected)
    if len(matches) != 1:
        raise ValueError("QA1 人工选择的模板不在当前候选集合中")
    return matches[0]


__all__ = [
    "Qa1DraftSpec",
    "Qa1MatchResult",
    "choose_qa1_model",
    "match_qa1_template",
]
