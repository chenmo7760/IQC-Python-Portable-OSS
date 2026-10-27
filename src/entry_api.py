"""auto-factory 普通 Entry 草稿 API 的受控、可测试适配器。

接口调用被刻意放在一个很小的依赖注入层：``build_entry_draft_payload`` 是
纯函数，HTTP 传输由 ``EntryApiTransport`` 注入。这样本地生成/SQL 流程不必
依赖网络，也不会因 API 不返回 draft_id 而猜测“最新草稿”或自动重试。
本模块不触碰数据库、BAT 或 auto-factory 源码。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
import json
import re
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
import uuid

from .models import canonicalize_m_code_separators
from .qa1_matching import Qa1DraftSpec, choose_qa1_model
from .sql_generator import SqlAttachedRow, SqlContextRow, SqlRecord


MODEL_CONTEXT_PATH = "/api/api/qcauto/procedureModel/entry/getModelContextById/"
SAVE_DRAFT_CONTEXT_PATH = "/api/api/qcauto/procedureDraft/entry/saveDraftContextById/"
UPDATE_DRAFT_CONTEXT_PATH = "/api/api/qcauto/procedureDraft/entry/updateDraftContextById/"
GET_DRAFT_CONTEXT_PATH = "/api/api/qcauto/procedureDraft/entry/getDraftContextByDraftId/"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_M_EXPANSION_SUFFIX_RE = re.compile(
    r"^(?P<head>.*?)\((?P<body>M\d{11}(?:\s*[/;,；，、]\s*M\d{11})*)\)$"
)


class EntryApiError(RuntimeError):
    """模板读取、payload 校验或一次 API 请求失败。"""


class EntryApiTransport(Protocol):
    """HTTP 依赖注入协议；测试可用内存 fake，生产才注入 urllib 实现。"""

    def get_json(self, path: str, params: Mapping[str, object]) -> Mapping[str, object]:
        ...

    def post_form(self, path: str, fields: Mapping[str, object]) -> Mapping[str, object]:
        ...


@dataclass(frozen=True)
class EntryModelContext:
    """``getModelContextById`` 的最小本地快照。

    ``header``、``contexts``、``attached_templates`` 均保留服务端原字段，
    以便 diagnostic/回读阶段比对；payload 只消费已知字段，不把模板的
    ``notes``/``attached_table_name`` 猜写成 Draft/Record 新字段。
    """

    model_id: str
    header: Mapping[str, object] = field(default_factory=dict)
    contexts: tuple[Mapping[str, object], ...] = ()
    attached_templates: tuple[Mapping[str, object], ...] = ()
    is_exist_attached_table: str | None = None


@dataclass(frozen=True)
class EntryDraftSaveResult:
    """一次保存响应；服务端未返回 ID 时 ``draft_id`` 保持 ``None``。"""

    accepted: bool
    draft_id: str | None
    response: Mapping[str, object]


@dataclass(frozen=True)
class EntryDraftUpdateStage:
    """已有草稿重提的本地 staging 快照。

    ``old_fields`` 只作为审计/回显基线保存；客户端不会删除旧附表、先改
    主状态或在失败时伪造回滚。只有 ``EntryDraftResubmissionService`` 的
    显式 submit 阶段才会用已知 draft_id 发出一次 update POST。
    """

    draft_id: str
    new_fields: Mapping[str, object]
    old_fields: Mapping[str, object] = field(default_factory=dict)
    # ``old_snapshot_known`` 用来区分“服务端回读得到空集合”和“调用方没有
    # 提供旧快照”。flag=0 的重提若无法证明旧附表不存在，不能把空 child
    # 当成确认删除；只有显式 ``attached_tables=()`` 加审计理由才可继续。
    old_snapshot_known: bool = False
    child_drop_confirmed: bool = False
    child_drop_reason: str = ""


@dataclass(frozen=True)
class EntryDraftResubmissionResult:
    """重提结果及旧状态基线；失败时 ``accepted=False`` 且旧字段不变。"""

    accepted: bool
    draft_id: str
    status: str
    old_fields: Mapping[str, object]
    new_fields: Mapping[str, object]
    detail: str = ""
    response: Mapping[str, object] = field(default_factory=dict)


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _without_expansion_suffix(value: object) -> str:
    """移除旧版 ``压缩值(完整展开值)``，但不误删业务括号文本。"""

    text = _text(value)
    match = _M_EXPANSION_SUFFIX_RE.fullmatch(text)
    if match is not None:
        text = match.group("head").strip()
    # canonicalize 只转换字段中已经存在的 M 码分隔符；不把 ``/`` 当作
    # 路径，保证 M 码通过 API/JSON 时仍是一个字段内的业务文本。
    return canonicalize_m_code_separators(text)


def _qa1_trigger_basis(value: object) -> str:
    """为 QA1 独立草稿生成顶部检查单号，不修改原送检单号语义。"""

    text = _text(value)
    if not text:
        raise EntryApiError("QA1 普通检查单号为空，不能生成 S- 前缀检查单号")
    # 重提或外部调用可能已经传入 S-；大小写不敏感地识别既有前缀，
    # 统一规范为大写 S- 并避免生成 S-S-...。裸前缀不能成为检查单号。
    base = text[2:].strip() if text.casefold().startswith("s-") else text
    if not base:
        raise EntryApiError("QA1 检查单号不能只有 S- 前缀")
    # 普通聚合 trigger_basis（包括 ``送检单-首行/后续行``）必须完整
    # 保留；QA1 唯一变化是在最前面增加 S-。
    return f"S-{base}"


def _normal_date(value: object) -> str:
    text = _text(value)
    # SqlRecord 的既有 SQL/BAT 输出在缺少完成日期时使用运行日默认值；
    # Entry 草稿必须与该路径保持相同语义，不能把同一个空值再次编码成
    # 空字符串发送给 API。
    if not text:
        return date.today().isoformat()
    if not _DATE_RE.fullmatch(text):
        raise EntryApiError("API 草稿完成日期必须是 YYYY-MM-DD，拒绝发送非法日期")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise EntryApiError("API 草稿完成日期必须是 YYYY-MM-DD，拒绝发送非法日期") from exc
    return text


def _context_fields(context: object, index: int) -> dict[str, object]:
    """从 SqlContextRow/服务端模板 Mapping 取已确认的五个 context 字段。"""

    if isinstance(context, SqlContextRow):
        values: Mapping[str, object] = {
            "context_number": context.context_number,
            "inspection_items": context.inspection_items,
            "inspection_standards": context.inspection_standards,
            "inspect_records": context.inspect_records,
            "inspection_conclusion": context.inspection_conclusion,
        }
    elif isinstance(context, Mapping):
        values = context
    else:
        raise EntryApiError(f"检查 context[{index}] 不是受支持的 Mapping/SqlContextRow")

    def pick(*names: str) -> object:
        for name in names:
            if name in values:
                return values[name]
        return None

    return {
        f"procedureDraftContext[{index}][context_number]": pick("context_number", "contextNumber"),
        f"procedureDraftContext[{index}][inspection_items]": pick("inspection_items", "inspectionItems"),
        f"procedureDraftContext[{index}][inspection_standards]": pick(
            "inspection_standards", "inspectionStandards"
        ),
        # None 保持为 Python None；transport 会编码为空 multipart 值，绝不
        # 伪造字面量 ``undefined`` 或 ``NULL``。
        f"procedureDraftContext[{index}][inspect_records]": pick("inspect_records", "inspectRecords"),
        f"procedureDraftContext[{index}][inspection_conclusion]": pick(
            "inspection_conclusion", "inspectionConclusion"
        ),
    }


def _resolve_payload_contexts(
    record: SqlRecord,
    model_context: EntryModelContext | None,
    explicit_contexts: Sequence[object] | None,
) -> tuple[object, ...]:
    """按来源优先级选择 Draft context，拒绝把本地旧值静默带入 API。

    ``getModelContextById`` 返回的目标 context 是普通/QA1 API 草稿的
    权威来源。只有调用方明确传入 ``explicit_contexts``（例如人工在当前
    候选表中替换模板）才允许覆盖；这个参数即是可审计的 override 边界。
    当存在目标 model 但服务端未返回 context 时直接阻断，绝不回退到
    ``record.contexts``，避免把另一套普通模板写进新草稿。没有目标
    model_context 的纯本地调用仍保留原有 record 语义，供离线 SQL/单测使用。
    """

    if model_context is not None:
        if explicit_contexts is None:
            contexts = tuple(model_context.contexts)
            if not contexts:
                raise EntryApiError("GET 目标 model_context 未返回 context，拒绝沿用本地 context")
            return contexts
        # 显式 override 必须仍有至少一行 context；空集合不能被误解为
        # “确认删除 context”，因为当前 API 没有这样的安全契约。
        contexts = tuple(explicit_contexts)
        if not contexts:
            raise EntryApiError("显式 context override 为空，拒绝发送无 context 草稿")
        return contexts

    if explicit_contexts is not None:
        contexts = tuple(explicit_contexts)
        if not contexts:
            raise EntryApiError("显式 context override 为空，拒绝发送无 context 草稿")
        return contexts
    return tuple(record.contexts)


def _child_presence_from_snapshot(snapshot: Mapping[str, object] | None) -> bool | None:
    """从已知草稿回读中提取附表存在性；无法证明时返回 ``None``。

    回读响应在不同部署可能是 ``data=[header, contexts, attached]``，也
    可能直接使用 ``attachedTable``/``procedureDraftAttachedTableRecord``
    字段。这里只识别有边界的字段名，不做无界字符串搜索；``None`` 表示
    未取证，重提 flag=0 时必须停下而不是猜测旧 child 已不存在。
    """

    if snapshot is None or not isinstance(snapshot, Mapping):
        return None

    def value_present(value: object) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            text = value.strip()
            if not text or text.lower() in {"null", "none", "[]", "{}"}:
                return False
            try:
                decoded = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                return True
            return value_present(decoded)
        if isinstance(value, Mapping):
            return bool(value)
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            return bool(value)
        return bool(value)

    # 常见回读契约：data[2] 是附表集合；空列表是“已证明无 child”。
    if "data" in snapshot:
        data = snapshot.get("data")
        if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
            if len(data) >= 3:
                return value_present(data[2])

    known = False
    for key, value in snapshot.items():
        normalized = str(key).replace("_", "").lower()
        if normalized in {
            "isexistattachedtable",
            "isexistattached",
            "attachedflag",
        }:
            known = True
            if _text(value) in {"1", "true", "yes"}:
                return True
            if _text(value) in {"0", "false", "no", ""}:
                continue
        if "attached" in normalized or normalized in {"children", "childids"}:
            known = True
            if value_present(value):
                return True
    return False if known else None


def _guard_flag_zero_resubmission(stage: EntryDraftUpdateStage) -> None:
    """阻断未经取证的 flag=0 重提，避免服务端旧 child 被静默覆盖。

    ``build_entry_draft_payload`` 只负责新 payload 的一致性，无法单凭
    model context 知道已有 draft 是否挂着附表。因此重提 staging 必须携带
    已知 draft 回读快照。若快照明确为空可继续；若发现 child 或根本没有
    快照，只有调用方同时声明 ``attached_tables=()`` 和非空审计理由才
    允许一次 update。函数不发送请求、不删除旧附表。
    """

    if _text(stage.new_fields.get("isExistAttachedTable")) != "0":
        return
    if stage.child_drop_confirmed and _text(stage.child_drop_reason):
        return
    presence = (
        _child_presence_from_snapshot(stage.old_fields)
        if stage.old_snapshot_known
        else None
    )
    if presence is not False:
        raise EntryApiError(
            "目标 flag=0 但旧草稿附表未被证明为空；请先回读并显式确认 attached_tables=()，"
            "同时填写丢弃 child 的审计理由"
        )


def _content_rows(attached: SqlAttachedRow, sequence_code: str) -> list[dict[str, object]]:
    try:
        decoded = json.loads(attached.inspection_content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EntryApiError("附表 inspection_content 不是合法 JSON") from exc
    if not isinstance(decoded, list) or not decoded or any(not isinstance(row, dict) for row in decoded):
        raise EntryApiError("附表 inspection_content 必须是非空 JSON 对象数组")
    rows: list[dict[str, object]] = []
    for row in decoded:
        normalized = dict(row)
        normalized["sequenceCode"] = sequence_code
        rows.append(normalized)
    return rows


def _attached_payload(
    record: SqlRecord,
    attached: SqlAttachedRow,
    sequence_code: str,
) -> dict[str, object]:
    """把单条已选附表映射为接口要求的一个 JSON 对象。"""

    inspection_number = _text(record.trigger_basis)
    if not inspection_number:
        raise EntryApiError("附表 inspection_number 缺少主草稿 trigger_basis，不能猜测送检单号或物料编码代替")
    result: dict[str, object] = {
        "inspection_number": inspection_number,
        "item_code": record.item_code,
        # 抽样结果仍保留在 SqlRecord；正式保存附表时按业务要求显式写空。
        "check_number": None,
        "testing_tools": record.testing_tools,
        "inspection_conclusion": record.inspection_conclusion,
        "qc_engineer": record.engineer_name,
        "completion_time": _normal_date(record.completion_time),
        "inspection_content": _content_rows(attached, sequence_code),
    }
    # 附表目标 schema 固定有 inspection_item1..9/standard_range1..9。
    for index, value in enumerate(attached.inspection_items, start=1):
        result[f"inspection_item{index}"] = value
    for index, value in enumerate(attached.standard_ranges, start=1):
        result[f"standard_range{index}"] = value
    return result


def _attached_rows_from_model_context(model_context: EntryModelContext | None) -> tuple[SqlAttachedRow, ...]:
    """将 GET 返回的附表模板映射为本地 SqlAttachedRow。

    只要已取得目标 ``model_context`` 且调用方没有显式 override，
    就以这份 GET 结果为权威来源，不受普通 ``SqlRecord`` 中附表影响。
    项目/范围严格按 9 个槽位读取；名称和 notes 只作为来源快照
    保留在 ``EntryModelContext.header``，不进 Draft/Record JSON。
    """

    if model_context is None:
        return ()
    rows: list[SqlAttachedRow] = []
    for index, template in enumerate(model_context.attached_templates):
        items_value = template.get("inspection_items")
        ranges_value = template.get("standard_ranges")
        if not isinstance(items_value, (list, tuple)):
            items_value = tuple(template.get(f"inspection_item{slot}", "") for slot in range(1, 10))
        if not isinstance(ranges_value, (list, tuple)):
            ranges_value = tuple(template.get(f"standard_range{slot}", "") for slot in range(1, 10))
        if len(items_value) != 9 or len(ranges_value) != 9:
            raise EntryApiError(f"GET 附表模板[{index}] 不是 9×9 项目/范围")
        try:
            rows.append(
                SqlAttachedRow.from_template(
                    items_value,
                    ranges_value,
                    notes=template.get("notes", ""),
                )
            )
        except Exception as exc:
            raise EntryApiError(f"GET 附表模板[{index}] 无法组装") from exc
    return tuple(rows)


def build_entry_draft_payload(
    record: SqlRecord,
    *,
    order_number: object | None = None,
    model_context: EntryModelContext | None = None,
    contexts: Sequence[object] | None = None,
    attached_tables: Sequence[SqlAttachedRow] | None = None,
    selected_attached_index: int | None = None,
) -> dict[str, object]:
    """纯函数构造 ``saveDraftContextById`` 的 FormData 字段。

    调用方必须传入已聚合的 ``SqlRecord``；本函数不重新按源 Excel 行计算
    数量、检查数量或 M 码。附表多候选时必须显式给出索引，未选直接阻断。
    ``raw_supplier``/``supplier_defaulted`` 只进入 diagnostic provenance，
    不凭空增加 API 未确认的供应商字段。目标 ``model_context`` 的 context
    来源也由内部解析边界固定；如需人工替换，调用方必须显式传 ``contexts``
    并在外层审计记录 override 来源。
    """

    if model_context is not None:
        record_model_id = _text(record.procedure_model_id)
        context_model_id = _text(model_context.model_id)
        if not context_model_id:
            raise EntryApiError("目标 model_context 缺少 model_id，拒绝发送草稿")
        if context_model_id != record_model_id:
            raise EntryApiError(
                "目标 model_context.model_id 与草稿 modelId 不一致，拒绝混用模板上下文"
            )

    sequence_code = _without_expansion_suffix(record.sequence_code)
    # 普通草稿与附表遵循同一 API-权威规则：有 GET 目标模板时默认只用
    # model_context.contexts；调用方只有在明确人工 override 时才传 contexts。
    # 这里不再以 ``record.contexts`` 非空作为优先级，避免旧本地模板串入
    # 新 modelId。没有 model_context 的离线纯函数调用仍使用 record.contexts。
    resolved_contexts = _resolve_payload_contexts(record, model_context, contexts)

    if attached_tables is not None:
        # 只有调用方明确传入 override 才允许替换 GET 结果；QA1 UI 的
        # 人工 child 选择即走此参数，来源会在调用方审计，不静默沿用普通 child。
        candidate_attached_tables = tuple(attached_tables)
    elif model_context is not None:
        # API GET 返回的附表模板是权威来源。即使普通 SqlRecord 已带本地
        # child，也不能覆盖刚取得的目标 model context；flag=1 无 child 时
        # 由下方统一阻断，flag=0 则保持无附表。
        candidate_attached_tables = _attached_rows_from_model_context(model_context)
    else:
        candidate_attached_tables = tuple(record.attached_tables)
    if len(candidate_attached_tables) > 1:
        if selected_attached_index is None:
            raise EntryApiError("附表存在多个候选，未明确选择 child，不能提交")
        if selected_attached_index < 0 or selected_attached_index >= len(candidate_attached_tables):
            raise EntryApiError("人工选择的附表 child 不在当前候选集合中")
        attached = candidate_attached_tables[selected_attached_index]
    else:
        if selected_attached_index not in (None, 0):
            raise EntryApiError("单条附表不能使用非零 child 索引")
        attached = candidate_attached_tables[0] if candidate_attached_tables else None

    flag = None if model_context is None else _text(model_context.is_exist_attached_table)
    if flag not in (None, "0", "1"):
        raise EntryApiError("模板 is_exist_attached_table 不是 0/1，阻断 API 草稿")
    actual_flag = "1" if attached is not None else "0"
    if flag == "0" and actual_flag == "1":
        raise EntryApiError("模板 flag=0 但本地存在附表，拒绝制造不一致草稿")
    if flag == "1" and actual_flag == "0":
        # getModelContext 是附表是否存在的权威事实；不拿空对象冒充模板
        # child，也不把 flag=1 静默改成 0。
        raise EntryApiError("模板 flag=1 但未取得可提交附表 child")
    actual_flag = flag or actual_flag

    fields: dict[str, object] = {
        "modelId": _text(record.procedure_model_id),
        "isExistAttachedTable": actual_flag,
        "procedureDraftTriggerBasis": record.trigger_basis,
        "procedureDraftItemCode": record.item_code,
        "procedureDraftItemName": record.item_name,
        "procedureDraftTotolNumber": record.quantity,
        # 抽样结果仍保留在 SqlRecord；None 由 transport 编为空值，后端转为 NULL。
        "procedureDraftCheckNumber": None,
        "procedureDraftSupplier": record.supplier,
        "procedureDraftSequenceCode": sequence_code,
        "procedureDraftTestingTools": record.testing_tools,
        "procedureDraftInspectionConclusion": record.inspection_conclusion,
        "procedureDraftQcEngineer": record.engineer_name,
        "procedureDraftCompletionTime": _normal_date(record.completion_time),
        "procedureDraftNcrCode": record.ncr_code,
        "procedureDraftNotes": record.notes,
        # SQL 老契约未包含 measurement_unit 列，但 API 表单字段存在；按
        # ``SqlRecord`` 的可选扩展值传递，未提供时保持空而不猜单位。
        "procedureDraftMeasurementUnit": getattr(record, "measurement_unit", ""),
        "procedureDraftCheckPlan": record.check_plan,
        "procedureDraftAcceptStandard": record.accept_standard,
    }
    for index, context in enumerate(resolved_contexts):
        fields.update(_context_fields(context, index))
    if attached is not None:
        # 接口读取单个 request.POST 值，必须传一个 JSON 对象而非数组；模板
        # 名称/notes 只作为本地来源，不写入 Draft/Record 未确认列。
        attached_json = _attached_payload(
            record,
            attached,
            sequence_code,
        )
        fields["procedureDraftAttachedTableRecord"] = json.dumps(
            attached_json, ensure_ascii=False, separators=(",", ":")
        )
    return fields


def build_qa1_entry_draft_payload(
    record: SqlRecord,
    spec: Qa1DraftSpec,
    *,
    order_number: object,
    selected_model_id: object | None = None,
    contexts: Sequence[object] | None = None,
    attached_tables: Sequence[SqlAttachedRow] | None = None,
    model_context: EntryModelContext | None = None,
    selected_attached_index: int | None = None,
) -> dict[str, object]:
    """在 QA1 候选已明确选择后构造独立草稿 payload。

    该函数只负责本地纯组装；它不会自行 POST。多候选必须传
    ``selected_model_id``，未命中/取消直接阻断。生产调用方通过
    ``getModelContextById`` 注入已选模板的 ``model_context``；其 contexts、
    flag 和 attached_templates 是 QA1 首次新建的权威来源。显式传入的
    ``contexts``/``attached_tables`` 仅作为可审计 override。
    """

    if not spec.candidates:
        raise EntryApiError("QA1 未命中模板，不生成关键零部件草稿")
    if len(spec.candidates) == 1:
        selected = spec.candidates[0]
        if selected_model_id is not None and _text(selected_model_id) != _text(selected.id):
            raise EntryApiError("QA1 人工选择与唯一候选不一致")
    else:
        try:
            selected = choose_qa1_model(spec, selected_model_id)
        except ValueError as exc:
            raise EntryApiError(str(exc)) from exc
    if selected is None:  # 防御性分支，避免静态类型之外的伪候选提交
        raise EntryApiError("QA1 未得到明确模板，不能提交")
    if model_context is None and contexts is None:
        # 没有目标 model 的 GET 上下文时，不能把普通草稿的 contexts 当成
        # QA1 模板上下文；调用方必须显式提供目标上下文，避免串模板。
        raise EntryApiError("QA1 缺少目标 model_context/contexts，拒绝沿用普通模板")
    # QA1 默认只取目标模板的上下文；即使普通 record 已有 contexts，也
    # 不能在 contexts 未传时沿用。空目标上下文同样阻断，而不是静默使用
    # 普通检查单的项目。
    qa_contexts = tuple(contexts) if contexts is not None else tuple(model_context.contexts)
    if not qa_contexts:
        raise EntryApiError("QA1 目标 model_context 未返回 context，拒绝串用普通模板")
    # attached_tables=None 的语义是“等待 build_entry_draft_payload 从
    # 目标 model_context 的 attached_templates 组装”，绝不回退到普通
    # record.attached_tables。QA1 是独立的首次新建草稿；目标 flag=0
    # 意味着本草稿不生成 child，与普通草稿是否带附表无关。
    # Preserve ``None`` so build_entry_draft_payload can resolve the target
    # model_context.attached_templates as the authoritative child set.  An
    # empty tuple here is an explicit override and would make a flag=1 target
    # appear to have no child, blocking every QA1 submission that relies on
    # the GET response.
    qa_attached_tables = tuple(attached_tables) if attached_tables is not None else None
    selected_record = replace(
        record,
        procedure_model_id=selected.id,
        procedure_name=selected.procedure_name,
        # QA1 独立草稿的最终 trigger_basis 使用 S- 前缀；下层附表
        # inspection_number 从这个已替换 record 继承同一个最终值。
        trigger_basis=_qa1_trigger_basis(record.trigger_basis),
        contexts=qa_contexts,
        # 选中后的 QA1 记录不携带普通草稿 child。下层仍单独接收
        # 原始 ``None``，以便 flag=1 时从 GET 结果组装目标附表。
        attached_tables=tuple(attached_tables) if attached_tables is not None else (),
    )
    return build_entry_draft_payload(
        selected_record,
        order_number=order_number,
        model_context=model_context,
        # 仅在调用方明确提供 contexts 时作为 override 传入；否则让下层
        # 再次从 GET model_context 取权威 context，而不是依赖 replace 后
        # 的普通 record.contexts。
        contexts=contexts,
        attached_tables=qa_attached_tables,
        selected_attached_index=selected_attached_index,
    )


def parse_model_context_response(
    response: Mapping[str, object],
    *,
    model_id: object,
) -> EntryModelContext:
    """解析已知的 ``data=[header, contexts, attached]`` 响应形态。

    服务端版本若返回完全不同的结构，本函数明确失败；不通过“取第一条
    最新模板”之类的猜测来掩盖接口漂移。
    """

    if not isinstance(response, Mapping):
        raise EntryApiError("getModelContextById 响应不是 JSON 对象")
    data = response.get("data", response)
    header: Mapping[str, object] = {}
    contexts: Sequence[object] = ()
    attached: Sequence[object] = ()
    def normalize_attached_templates(raw: object) -> tuple[Mapping[str, object], ...]:
        """把服务端“无附表”占位值归一为空集合。

        ``getModelContextById`` 的后端实现对 ``flag=0`` 返回 ``{}``，而
        某些历史数据/异常路径会返回 ``{"error": ...}``。这两种映射都不
        是可组装的附表模板；若把它们当成模板，会被补成 9×9 个 ``NA``，
        最终让 flag=0 的普通草稿被错误阻断。flag=1 的一致性校验仍由
        ``build_entry_draft_payload`` 负责，避免在这里猜测附表内容。
        """

        def is_placeholder(item: Mapping[str, object]) -> bool:
            return not item or set(item) == {"error"}

        if isinstance(raw, Mapping):
            return () if is_placeholder(raw) else (raw,)
        if isinstance(raw, list):
            return tuple(item for item in raw if isinstance(item, Mapping) and not is_placeholder(item))
        return ()

    if isinstance(data, list):
        if data and isinstance(data[0], Mapping):
            header = data[0]
        elif data and isinstance(data[0], list):
            # 当前后端返回的 headContext 本身是 ``[{...}]``；兼容这个
            # 包装层，否则会丢失 is_exist_attached_table=0，导致后续
            # 无法区分“明确无附表”和“响应缺少 flag”。
            header = next((item for item in data[0] if isinstance(item, Mapping)), {})
        if len(data) > 1 and isinstance(data[1], list):
            contexts = data[1]
        if len(data) > 2 and isinstance(data[2], (list, Mapping)):
            attached = normalize_attached_templates(data[2])
    elif isinstance(data, Mapping):
        header = data
        raw_contexts = data.get("contexts", data.get("procedureDraftContext", data.get("context", ())))
        raw_attached = data.get(
            "attached_tables",
            data.get("attachedTable", data.get("procedureDraftAttachedTableRecord", ())),
        )
        contexts = raw_contexts if isinstance(raw_contexts, list) else ()
        attached = normalize_attached_templates(raw_attached)
    else:
        raise EntryApiError("getModelContextById data 不是可解析的对象/数组")
    if not header and not contexts and not attached:
        raise EntryApiError("getModelContextById 未返回可用模板上下文")
    model_text = _text(model_id)
    if not model_text:
        raise EntryApiError("modelId 为空，不能读取模板上下文")
    flag = None
    for key in ("is_exist_attached_table", "isExistAttachedTable"):
        if key in header:
            flag = _text(header[key])
            break
    return EntryModelContext(
        model_id=model_text,
        header=dict(header),
        contexts=tuple(item for item in contexts if isinstance(item, Mapping)),
        attached_templates=tuple(item for item in attached if isinstance(item, Mapping)),
        is_exist_attached_table=flag,
    )


@dataclass
class UrllibEntryApiTransport:
    """无第三方依赖的 multipart/JSON 传输实现；默认不重试。"""

    base_url: str
    timeout_seconds: float = 30.0
    headers: Mapping[str, str] = field(default_factory=dict)

    def _url(self, path: str, params: Mapping[str, object] | None = None) -> str:
        url = urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/"))
        if params:
            query = urlencode({key: _text(value) for key, value in params.items()})
            url += ("&" if "?" in url else "?") + query
        return url

    def get_json(self, path: str, params: Mapping[str, object]) -> Mapping[str, object]:
        request = Request(self._url(path, params), headers={"Accept": "application/json", **dict(self.headers)})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except (HTTPError, URLError, OSError) as exc:
            raise EntryApiError(f"GET 模板上下文失败：{type(exc).__name__}") from exc
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise EntryApiError("GET 模板上下文响应不是合法 JSON") from exc
        if not isinstance(decoded, Mapping):
            raise EntryApiError("GET 模板上下文响应根节点不是对象")
        return decoded

    @staticmethod
    def _multipart(fields: Mapping[str, object]) -> tuple[bytes, str]:
        boundary = "----IQCEntryBoundary" + uuid.uuid4().hex
        chunks: list[bytes] = []
        for key, value in fields.items():
            text = "" if value is None else str(value)
            chunks.extend(
                [
                    (f"--{boundary}\r\n").encode("ascii"),
                    (f'Content-Disposition: form-data; name="{key}"\r\n\r\n').encode("utf-8"),
                    text.encode("utf-8"),
                    b"\r\n",
                ]
            )
        chunks.append((f"--{boundary}--\r\n").encode("ascii"))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def post_form(self, path: str, fields: Mapping[str, object]) -> Mapping[str, object]:
        body, content_type = self._multipart(fields)
        request = Request(
            self._url(path),
            data=body,
            method="POST",
            headers={"Accept": "application/json", "Content-Type": content_type, **dict(self.headers)},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except (HTTPError, URLError, OSError) as exc:
            # 这里不重试：服务端可能已经创建草稿但客户端未收到响应。
            raise EntryApiError(f"POST 草稿失败（未自动重试）：{type(exc).__name__}") from exc
        try:
            decoded = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise EntryApiError("POST 草稿响应不是合法 JSON") from exc
        if not isinstance(decoded, Mapping):
            raise EntryApiError("POST 草稿响应根节点不是对象")
        return decoded


@dataclass
class EntryDraftApiClient:
    """实现严格的 GET→本地组装→一次 POST 普通草稿链。"""

    transport: EntryApiTransport

    def get_model_context(self, model_id: object) -> EntryModelContext:
        model_text = _text(model_id)
        if not model_text:
            raise EntryApiError("modelId 为空，不能调用 getModelContextById")
        response = self.transport.get_json(MODEL_CONTEXT_PATH, {"id": model_text})
        return parse_model_context_response(response, model_id=model_text)

    def save_draft(self, fields: Mapping[str, object]) -> EntryDraftSaveResult:
        # saveDraftContextById 是首次创建唯一入口；调用一次后无论响应是否
        # 含 ID 都停止，不查询“最新草稿”也不重复 POST。
        response = self.transport.post_form(SAVE_DRAFT_CONTEXT_PATH, fields)
        data = response.get("data") if isinstance(response, Mapping) else None
        accepted = bool(
            isinstance(data, list)
            and any(_text(item).lower() == "draft context saved successfully." for item in data)
        )
        raw_id = response.get("draft_id", response.get("draftId")) if isinstance(response, Mapping) else None
        draft_id = _text(raw_id) or None
        if not accepted:
            raise EntryApiError("POST 草稿响应未确认保存成功，未自动重试")
        return EntryDraftSaveResult(accepted=True, draft_id=draft_id, response=dict(response))

    def get_draft_context(self, draft_id: object) -> Mapping[str, object]:
        """仅按调用方提供的可靠 draft_id 回读草稿，不猜最新记录。"""

        draft_text = _text(draft_id)
        if not draft_text:
            raise EntryApiError("draft_id 为空，拒绝猜测最新草稿并回读")
        # 回读接口是只读 GET；无服务端返回 ID 的首次保存结果不会自动
        # 进入这里，调用方必须显式提供已知 ID。
        return self.transport.get_json(GET_DRAFT_CONTEXT_PATH, {"id": draft_text})

    def update_draft(self, draft_id: object, fields: Mapping[str, object]) -> EntryDraftSaveResult:
        """更新已有草稿；只接受显式 draft_id，且按 update schema 改名触发字段。"""

        draft_text = _text(draft_id)
        if not draft_text:
            raise EntryApiError("更新草稿必须提供可靠 draft_id，不能调用最新草稿猜测")
        update_fields = dict(fields)
        update_fields["draftId"] = draft_text
        if "procedureDraftTriggerBasis" in update_fields:
            # 创建/更新接口字段名不同；内部仍沿用同一 canonical 业务值。
            update_fields["procedureTriggerBasis"] = update_fields.pop("procedureDraftTriggerBasis")
        response = self.transport.post_form(UPDATE_DRAFT_CONTEXT_PATH, update_fields)
        data = response.get("data") if isinstance(response, Mapping) else None
        accepted = bool(
            isinstance(data, list)
            and any(_text(item).lower() == "draft context saved successfully." for item in data)
        )
        raw_id = response.get("draft_id", response.get("draftId")) if isinstance(response, Mapping) else None
        response_id = _text(raw_id) or draft_text
        if not accepted:
            raise EntryApiError("更新草稿响应未确认保存成功，未自动重试")
        return EntryDraftSaveResult(accepted=True, draft_id=response_id, response=dict(response))

    def create_draft_from_record(
        self,
        record: SqlRecord,
        *,
        order_number: object | None = None,
        selected_attached_index: int | None = None,
    ) -> EntryDraftSaveResult:
        model_context = self.get_model_context(record.procedure_model_id)
        fields = build_entry_draft_payload(
            record,
            order_number=order_number,
            model_context=model_context,
            selected_attached_index=selected_attached_index,
        )
        return self.save_draft(fields)

    def update_draft_from_record(
        self,
        draft_id: object,
        record: SqlRecord,
        *,
        order_number: object | None = None,
        model_context: EntryModelContext | None = None,
        contexts: Sequence[object] | None = None,
        attached_tables: Sequence[SqlAttachedRow] | None = None,
        selected_attached_index: int | None = None,
        old_fields: Mapping[str, object] | None = None,
        child_drop_audit_reason: str | None = None,
    ) -> EntryDraftSaveResult:
        """先构造/校验新 payload，再对已知 ID 发一次 update POST。

        未传 model_context 时只读取当前记录的明确 modelId；不会猜最新草稿。
        ``update_draft`` 内部改用服务端 update schema 的触发字段名，并且
        transport 失败不重试，因此旧附表/主状态仍由服务端原子事务负责，
        客户端不执行删除或伪回滚。
        """

        draft_text = _text(draft_id)
        if not draft_text:
            raise EntryApiError("更新草稿必须提供可靠 draft_id，不能调用最新草稿猜测")
        context = model_context or self.get_model_context(record.procedure_model_id)
        # 已知 ID 的 update 先回读旧草稿；没有这个快照时，flag=0 不能
        # 猜测远端没有 child。staging 通过后才允许唯一一次 update POST。
        snapshot = old_fields if old_fields is not None else self.get_draft_context(draft_text)
        stage = EntryDraftResubmissionService(self).stage_update(
            draft_text,
            record,
            order_number=order_number,
            model_context=context,
            contexts=contexts,
            attached_tables=attached_tables,
            selected_attached_index=selected_attached_index,
            old_fields=snapshot,
            child_drop_audit_reason=child_drop_audit_reason,
        )
        outcome = EntryDraftResubmissionService(self).submit(stage)
        if not outcome.accepted:
            raise EntryApiError(outcome.detail or "更新草稿未确认保存")
        return EntryDraftSaveResult(True, outcome.draft_id, outcome.response)


@dataclass
class EntryDraftResubmissionService:
    """可审查的“先 staging、后一次 update”重提编排。

    该服务不维护服务端旧附表副本，也不在失败时清理/覆盖旧状态；它将
    ``old_fields`` 作为审计基线透传给调用方。失败结果保留 READY 之前的
    旧映射，现场若无法证明服务端事务一致性应继续标记 FIELD_REQUIRED。
    """

    client: EntryDraftApiClient

    def stage_update(
        self,
        draft_id: object,
        record: SqlRecord,
        *,
        order_number: object | None = None,
        model_context: EntryModelContext | None = None,
        contexts: Sequence[object] | None = None,
        attached_tables: Sequence[SqlAttachedRow] | None = None,
        selected_attached_index: int | None = None,
        old_fields: Mapping[str, object] | None = None,
        child_drop_audit_reason: str | None = None,
    ) -> EntryDraftUpdateStage:
        draft_text = _text(draft_id)
        if not draft_text:
            raise EntryApiError("重提 staging 必须提供可靠 draft_id")
        # 先构造/校验 payload；构造失败不会触碰服务端，旧状态也不变。
        context = model_context or self.client.get_model_context(record.procedure_model_id)
        new_fields = build_entry_draft_payload(
            record,
            order_number=order_number,
            model_context=context,
            contexts=contexts,
            attached_tables=attached_tables,
            selected_attached_index=selected_attached_index,
        )
        explicit_drop = (
            attached_tables is not None
            and not tuple(attached_tables)
            and bool(_text(child_drop_audit_reason))
        )
        stage = EntryDraftUpdateStage(
            draft_text,
            dict(new_fields),
            dict(old_fields or {}),
            old_snapshot_known=old_fields is not None,
            child_drop_confirmed=explicit_drop,
            child_drop_reason=_text(child_drop_audit_reason),
        )
        # staging 阶段先完成所有本地校验；不通过时绝不进入 submit/POST。
        _guard_flag_zero_resubmission(stage)
        return stage

    def submit(self, stage: EntryDraftUpdateStage) -> EntryDraftResubmissionResult:
        draft_text = _text(stage.draft_id)
        if not draft_text:
            raise EntryApiError("重提 staging 缺少可靠 draft_id")
        # 防御性重复校验：调用方可能手工构造 EntryDraftUpdateStage 绕过
        # stage_update；在唯一 update POST 前再次阻断 flag=0 风险。
        _guard_flag_zero_resubmission(stage)
        try:
            result = self.client.update_draft(draft_text, stage.new_fields)
        except Exception as exc:
            # 失败不自动重试，不改变 old_fields；调用方可记录 FIELD_REQUIRED
            # 并等待现场核查服务端事务，而不是删除附表或伪造回滚。
            return EntryDraftResubmissionResult(
                accepted=False,
                draft_id=draft_text,
                status="FAILED_FIELD_REQUIRED",
                old_fields=dict(stage.old_fields),
                new_fields=dict(stage.new_fields),
                detail=f"重提失败：{type(exc).__name__}；旧状态保留，未自动重试",
            )
        return EntryDraftResubmissionResult(
            accepted=result.accepted,
            draft_id=result.draft_id or draft_text,
            status="UPDATED" if result.accepted else "FAILED_FIELD_REQUIRED",
            old_fields=dict(stage.old_fields),
            new_fields=dict(stage.new_fields),
            detail="重提已确认保存" if result.accepted else "服务端未确认更新，旧状态保留",
            response=dict(result.response),
        )


@dataclass
class Qa1EntryDraftOrchestrator:
    """显式提交 QA1 附加草稿的最小编排入口。

    ``submit`` 只有在候选唯一或操作员提供 ``selected_model_id`` 后才会
    执行一次 save/update；未命中、取消或多候选未选时在本地直接
    抛出，transport 不会收到 POST。首次新建必须现场 GET 已选模板，
    调用方的模板快照/子表 override 只对已知 ID 重提有效。普通
    GenerationService 不会自动调用此类，因此普通草稿数量、聚合和
    抽检规则保持不变。
    """

    client: EntryDraftApiClient

    def submit(
        self,
        record: SqlRecord,
        spec: Qa1DraftSpec,
        *,
        order_number: object,
        selected_model_id: object | None = None,
        contexts: Sequence[object] | None = None,
        attached_tables: Sequence[SqlAttachedRow] | None = None,
        model_context: EntryModelContext | None = None,
        selected_attached_index: int | None = None,
        known_draft_id: object | None = None,
        child_drop_audit_reason: str | None = None,
    ) -> EntryDraftSaveResult:
        if not spec.candidates:
            raise EntryApiError("QA1 未命中模板，不提交附加草稿")
        if len(spec.candidates) == 1:
            selected = spec.candidates[0]
            if selected_model_id is not None and _text(selected_model_id) != _text(selected.id):
                raise EntryApiError("QA1 人工选择与唯一候选不一致")
        else:
            try:
                selected = choose_qa1_model(spec, selected_model_id)
            except ValueError as exc:
                raise EntryApiError(str(exc)) from exc
        known_id = _text(known_draft_id)
        if known_id:
            # 已存在草稿的更新路径保留显式 override 契约；未提供
            # model_context 时才从已选 modelId 读取。这些 override 后续
            # 仍必须通过旧草稿回读与 child 丢弃审计校验。
            update_model_context = model_context or self.client.get_model_context(selected.id)
            fields = build_qa1_entry_draft_payload(
                record,
                spec,
                order_number=order_number,
                selected_model_id=selected.id,
                contexts=contexts,
                attached_tables=attached_tables,
                model_context=update_model_context,
                selected_attached_index=selected_attached_index,
            )
            # 重提先回读已知 ID 作为旧状态快照，再通过统一 staging 服务
            # 构造/校验后一次 update；失败不删除旧附表，也不自动重试。
            try:
                old_fields = self.client.get_draft_context(known_id)
            except Exception as exc:
                raise EntryApiError(f"QA1 重提无法取得旧状态快照：{type(exc).__name__}") from exc
            explicit_drop = (
                attached_tables is not None
                and not tuple(attached_tables)
                and bool(_text(child_drop_audit_reason))
            )
            staged = EntryDraftUpdateStage(
                known_id,
                dict(fields),
                dict(old_fields),
                old_snapshot_known=True,
                child_drop_confirmed=explicit_drop,
                child_drop_reason=_text(child_drop_audit_reason),
            )
            outcome = EntryDraftResubmissionService(self.client).submit(staged)
            if not outcome.accepted:
                raise EntryApiError(outcome.detail or "QA1 重提未确认保存")
            return EntryDraftSaveResult(True, outcome.draft_id, outcome.response)

        # 首次新建无条件读取已选 modelId；即使调用方传入了
        # model_context/contexts/attached_tables，也不得跳过 GET 或覆盖后端
        # 返回的 contexts、flag 和 attached_templates。附表多候选索引只用于
        # 在这份后端候选内选择，不是本地附表 override。
        create_model_context = self.client.get_model_context(selected.id)
        fields = build_qa1_entry_draft_payload(
            record,
            spec,
            order_number=order_number,
            selected_model_id=selected.id,
            model_context=create_model_context,
            selected_attached_index=selected_attached_index,
        )
        return self.client.save_draft(fields)


__all__ = [
    "EntryApiError",
    "EntryApiTransport",
    "EntryDraftApiClient",
    "EntryDraftSaveResult",
    "EntryDraftResubmissionService",
    "EntryDraftResubmissionResult",
    "EntryDraftUpdateStage",
    "EntryModelContext",
    "GET_DRAFT_CONTEXT_PATH",
    "MODEL_CONTEXT_PATH",
    "SAVE_DRAFT_CONTEXT_PATH",
    "UPDATE_DRAFT_CONTEXT_PATH",
    "UrllibEntryApiTransport",
    "Qa1EntryDraftOrchestrator",
    "build_entry_draft_payload",
    "build_qa1_entry_draft_payload",
    "parse_model_context_response",
]
