"""受控的“读取并生成”编排层。

该模块只负责把已经解析且明确解析的业务对象交给模板写出和 SQL 生成器；
不执行生成的 BAT、Windchill EXE、SQL 或数据库操作。真实的只读快照和
Windchill 名称由调用方注入，避免在 UI 层猜测模型、人员或抽样字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import re
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from .audit_logger import redact_log_text
from .combination_aggregator import CombinationAggregationError, aggregate_task
from .models import (
    Engineer,
    OrderDraft,
    OrderTask,
    WindchillName,
    effective_supplier_for_order,
    normalize_procedure_name,
)
from .procedure_alias import (
    SYNTHETIC_GROUP_EXCLUDE_KEY,
    SYNTHETIC_GROUP_ITEM_CODES,
    SYNTHETIC_GROUP_SOURCE_NAME,
    SYNTHETIC_GROUP_TARGET_NAME,
    ProcedureAliasError,
    ProcedureAliasResolver,
    load_procedure_aliases,
)
from .sql_generator import (
    GeneratedOrderArtifacts,
    SqlContextRow,
    SqlOrder,
    SqlRecord,
    build_attached_row_from_template,
    generate_order_artifacts,
)
from .sampling_rules import ACCEPTANCE_REASON_LABELS, SAMPLING_REASON_LABELS, SamplingRuleError, resolve_sampling
from .template_writer import assemble_order_draft, output_path_for_order, write_order_draft
from .manual_confirmation import (
    ManualConfirmationDecision,
    ManualConfirmationRequired,
    ManualConfirmationRow,
    build_confirmation_row,
)
from .paper_standard_exclusion import (
    MATCH_EXCLUDED,
    MATCH_NOT_FOUND,
    MATCH_REVIEW_REQUIRED,
    MATCH_UNDETERMINED,
    QA1_PAPER_SCOPE_BLOCKED,
    PARSE_VALID,
    PaperStandardCatalog,
    PaperStandardMatch,
    match_paper_standard,
    parse_windchill_document_reference,
)
from .qa1_matching import Qa1DraftSpec, match_qa1_template


class GenerationError(RuntimeError):
    """生成依赖缺失、解析失败或文件写出失败。"""

    def __init__(
        self,
        message: str,
        *,
        query_stage: str | None = None,
        safe_detail: str = "",
    ) -> None:
        # Provider 边界上的异常可能来自外部 BAT/CLI；错误对象本身也要只
        # 保存脱敏后的文本，避免后续 UI/JSONL 事件意外回显连接信息。
        safe_message = redact_log_text(message)
        super().__init__(safe_message)
        self.query_stage = redact_log_text(query_stage) if query_stage else None
        self.safe_detail = redact_log_text(safe_detail or safe_message)


@dataclass(frozen=True)
class ResolvedOrder:
    """一个单号所需的完整、明确的模板和 SQL 输入。"""

    order_draft: OrderDraft
    sql_order: SqlOrder
    # QA1 附加关键零部件链只在这里挂载候选说明，普通 Excel/SQL 产物仍
    # 完全由 ``sql_order`` 生成。多候选不自动选，也不改变普通草稿结果；
    # API/UI 阶段可消费该元数据后再提交明确选择。当前
    # GenerationService 没有注入 EntryApiClient，故这里明确是
    # ``PARTIAL/TODO``：绝不在普通本地生成阶段隐式 POST QA1 草稿。
    qa1_drafts: tuple[Qa1DraftSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GenerationBatch:
    """逐单生成结果；单号失败不会覆盖其他单号结果。"""

    outcomes: tuple["GenerationOutcome", ...] = ()
    errors: Mapping[str, str] = field(default_factory=dict)
    events: tuple["GenerationEvent", ...] = ()
    # 自动模型匹配失败时只返回候选，不写出任何导入产物；UI 在操作员
    # 逐行确认后把决策交回 provider，再重新进入同一生成流程。
    manual_confirmation_rows: tuple[ManualConfirmationRow, ...] = ()
    paper_exclusions: Mapping[str, tuple[PaperStandardMatch, ...]] = field(default_factory=dict)
    paper_review_required: Mapping[str, tuple[PaperStandardMatch, ...]] = field(default_factory=dict)
    paper_only_orders: tuple[str, ...] = ()


@dataclass(frozen=True)
class GenerationEvent:
    """生成阶段诊断事件；字段只允许安全的计数、状态和文件名。"""

    name: str
    order_number: str | None = None
    status: str | None = None
    detail: str = ""
    fields: Mapping[str, object] = field(default_factory=dict)


# Provider 依赖层使用轻量 Mapping 回调，避免 mysql_query/windchill_adapter
# 反向依赖本模块；GenerationService 会把它统一转换成 GenerationEvent。
ProviderEventCallback = Callable[[Mapping[str, object]], None]


@dataclass(frozen=True)
class GenerationOutcome:
    order_number: str
    row_count: int
    result_path: Path
    artifacts: GeneratedOrderArtifacts
    detail: str
    # 保留本次已聚合、已解析的输入对象，供 UI 的普通 Entry 保存处理器
    # 在人工确认后复用。同一对象同时携带 qa1_drafts 候选元数据，因此
    # API 分支不需要重新读取 Excel、重做行合并或重新计算抽检数量。
    # 该字段追加在既有字段末尾，旧的五参数构造仍保持兼容。
    resolved_order: ResolvedOrder | None = None


class ResolvedOrderProvider(Protocol):
    """提供已解析的单号对象；实现可接入只读 MySQL/Windchill 适配器。"""

    def resolve(self, tasks: Mapping[str, OrderTask], engineer: Engineer) -> Mapping[str, ResolvedOrder]:
        ...


class PreparedResolvedOrderProvider(ResolvedOrderProvider, Protocol):
    """可选的批次预解析接口。

    真实现场 provider 用它在一个“读取并生成”批次内只加载一次只读快照、
    只调用一次 Windchill 固定文件适配器，然后由生成服务逐单写出文件。
    旧的仅实现 ``resolve`` 的注入 provider 仍保持兼容。
    """

    def prepare(self, tasks: Mapping[str, OrderTask], engineer: Engineer) -> Mapping[str, ResolvedOrder]:
        ...


@dataclass(frozen=True)
class UnconfiguredGenerationProvider:
    """生产入口未配置只读快照/Windchill backend 时的显式阻断。"""

    message: str = (
        "生成链路尚未配置只读模型快照和 Windchill 名称适配器；"
        "为避免猜测 procedure_model_id、人员 ID 或检查字段，未生成可导入 SQL。"
    )

    def resolve(self, tasks: Mapping[str, OrderTask], engineer: Engineer) -> Mapping[str, ResolvedOrder]:
        raise GenerationError(self.message)


@dataclass
class GenerationService:
    """把已解析对象写成结果工作簿和三件 WO-07 生成物。

    QA1 附加 Entry 草稿的 POST 刻意不在此层自动执行；调用方须在 UI
    明确选择模板后，消费 ``ResolvedOrder.qa1_drafts`` 并调用纯 payload
    helper/注入 API client。这样普通 SQL/Excel 生成不会因网络副作用改变。
    """

    template_path: Path
    output_dir: Path
    provider: ResolvedOrderProvider

    def generate(self, tasks: Mapping[str, OrderTask], engineer: Engineer) -> GenerationBatch:
        outcomes: list[GenerationOutcome] = []
        errors: dict[str, str] = {}
        events: list[GenerationEvent] = []
        manual_rows: tuple[ManualConfirmationRow, ...] = ()
        paper_exclusions: Mapping[str, tuple[PaperStandardMatch, ...]] = {}
        paper_review_required: Mapping[str, tuple[PaperStandardMatch, ...]] = {}
        paper_only_orders: tuple[str, ...] = ()

        def refresh_paper_metadata() -> None:
            nonlocal paper_exclusions, paper_review_required, paper_only_orders
            exclusions = getattr(self.provider, "paper_exclusions", {})
            reviews = getattr(self.provider, "paper_review_required", {})
            only_orders = getattr(self.provider, "paper_only_orders", ())
            if isinstance(exclusions, Mapping):
                paper_exclusions = {
                    str(order): tuple(value)
                    for order, value in exclusions.items()
                }
            if isinstance(reviews, Mapping):
                paper_review_required = {
                    str(order): tuple(value)
                    for order, value in reviews.items()
                }
            try:
                paper_only_orders = tuple(str(order) for order in only_orders)
            except TypeError:
                paper_only_orders = ()

        def merge_manual_rows(rows: Sequence[ManualConfirmationRow]) -> None:
            """按物项编码合并各单号的人工确认行，保持首次出现顺序。

            解析 provider 可能按批次或逐单抛出 ``ManualConfirmationRequired``。
            逐单路径下若直接覆盖 ``manual_rows``，后一个单号会把前一个单号
            的候选表冲掉；批次路径下重复转发同一异常又会出现重复行。UI
            需要看到本批全部待确认物项，因此在这里做稳定去重合并。
            """

            nonlocal manual_rows
            # 同批不同送检单可能出现相同物项编码；人工确认必须按
            # order_number::item_code 复合键合并，避免后单覆盖前单。
            existing = {row.confirmation_key for row in manual_rows}
            merged = list(manual_rows)
            for row in rows:
                if row.confirmation_key not in existing:
                    merged.append(row)
                    existing.add(row.confirmation_key)
            manual_rows = tuple(merged)

        def emit(
            name: str,
            *,
            order_number: str | None = None,
            status: str | None = None,
            detail: str = "",
            **fields: object,
        ) -> None:
            safe_fields = {
                key: redact_log_text(value) if isinstance(value, str) else value
                for key, value in fields.items()
            }
            events.append(GenerationEvent(name, order_number, status, redact_log_text(detail), safe_fields))

        def emit_provider_event(payload: Mapping[str, object]) -> None:
            """把 RuntimeProvider 的查询事件安全合并到本批次事件序列。"""

            if not isinstance(payload, Mapping):
                return
            name = str(payload.get("name", payload.get("event", "provider_event")))
            order_number_value = payload.get("order_number")
            order_number = str(order_number_value) if order_number_value is not None else None
            status_value = payload.get("status")
            status = str(status_value) if status_value is not None else None
            detail = str(payload.get("detail", ""))
            nested = payload.get("fields")
            if isinstance(nested, Mapping):
                fields = dict(nested)
            else:
                fields = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"name", "event", "order_number", "status", "detail"}
                }
            emit(name, order_number=order_number, status=status, detail=detail, **fields)

        emit(
            "generation_start",
            status="STARTED",
            detail="开始生成结果工作簿及逐单 SQL/BAT/诊断文件",
            order_count=len(tasks),
        )
        snapshot_field_exposed = hasattr(self.provider, "snapshot")
        windchill_field_exposed = hasattr(self.provider, "windchill_results")
        snapshot = getattr(self.provider, "snapshot", None)
        try:
            from .mysql_query import ReadonlySnapshot

            readonly_snapshot_injected = isinstance(snapshot, ReadonlySnapshot)
        except ImportError:
            readonly_snapshot_injected = False
        windchill_results = getattr(self.provider, "windchill_results", None)
        windchill_adapter_injected = getattr(self.provider, "windchill_adapter", None) is not None
        if isinstance(windchill_results, Mapping):
            windchill_result_count = sum(len(value) for value in windchill_results.values())
        elif windchill_results is None:
            windchill_result_count = 0
        else:
            try:
                windchill_result_count = len(windchill_results)
            except TypeError:
                windchill_result_count = 0
        # 只有明确的未配置提供者或暴露了依赖字段但字段缺失时才标记
        # BLOCKED。通用注入提供者可能不公开其内部快照属性；此时预检状态
        # 为 UNKNOWN，仍会继续调用 resolve，避免把可用的测试/现场适配器误报为阻断。
        provider_preflight = getattr(self.provider, "preflight", None)
        preflight_fields: dict[str, object] = {}
        if callable(provider_preflight):
            try:
                reported = provider_preflight()
            except Exception as exc:
                reported = {
                    "status": "ERROR",
                    "detail": "生成依赖预检异常",
                    "basis": "provider_preflight_error",
                    "error_type": type(exc).__name__,
                }
            if isinstance(reported, Mapping):
                preflight_status = str(reported.get("status", "UNKNOWN"))
                preflight_detail = str(reported.get("detail", "生成依赖预检完成"))
                preflight_basis = str(reported.get("basis", "provider_preflight"))
                preflight_fields = {
                    key: value
                    for key, value in reported.items()
                    if key not in {"status", "detail", "basis"}
                }
            else:
                preflight_status = "UNKNOWN"
                preflight_detail = "生成依赖预检未返回结构化结果"
                preflight_basis = "provider_preflight_invalid"
        elif isinstance(self.provider, UnconfiguredGenerationProvider):
            preflight_status = "BLOCKED"
            preflight_detail = "生成依赖未配置；保持安全阻断"
            preflight_basis = "unconfigured_provider"
        elif snapshot_field_exposed or windchill_field_exposed:
            preflight_status = "SUCCESS" if readonly_snapshot_injected and windchill_result_count else "BLOCKED"
            preflight_detail = "生成依赖预检完成；未配置项保持阻断"
            preflight_basis = "injected_fields"
        else:
            preflight_status = "UNKNOWN"
            preflight_detail = "通用提供者未公开依赖预检字段，将继续调用并以逐单结果为准"
            preflight_basis = "provider_fields_not_exposed"
        emit(
            "provider_preflight",
            status=preflight_status,
            detail=preflight_detail,
            provider_type=type(self.provider).__name__,
            preflight_basis=preflight_basis,
            snapshot_field_exposed=snapshot_field_exposed,
            windchill_results_field_exposed=windchill_field_exposed,
            readonly_snapshot_injected=readonly_snapshot_injected,
            windchill_adapter_injected=windchill_adapter_injected,
            windchill_name_results_injected=bool(windchill_result_count),
            windchill_name_result_count=windchill_result_count,
            template_exists=self.template_path.is_file(),
            output_dir_exists=self.output_dir.is_dir(),
            **preflight_fields,
        )
        prepared: Mapping[str, ResolvedOrder] | None = None
        prepare_error: BaseException | None = None
        prepare_method = getattr(self.provider, "prepare", None)
        if callable(prepare_method):
            emit("provider_prepare_start", status="STARTED", detail="开始批次解析只读快照和 Windchill 名称")
            set_event_callback = getattr(self.provider, "set_event_callback", None)
            callback_bound = False
            previous_provider_callback = getattr(self.provider, "event_callback", None)
            if callable(set_event_callback):
                # 仅在 provider_prepare 窗口接收依赖层事件，避免回调泄漏到
                # 后续导入批次；旧 fake provider 没有该方法时完全不受影响。
                set_event_callback(emit_provider_event)
                callback_bound = True
            try:
                candidate = prepare_method(tasks, engineer)
                if not isinstance(candidate, Mapping):
                    raise GenerationError("批次解析 provider 未返回单号映射")
                prepared = candidate
                emit(
                    "provider_prepare_success",
                    status="SUCCESS",
                    detail="批次依赖解析完成；后续逐单仅写出生成文件",
                    resolved_order_count=len(prepared),
                )
            except Exception as exc:
                prepare_error = exc
                if isinstance(exc, ManualConfirmationRequired):
                    merge_manual_rows(exc.rows)
                    emit(
                        "manual_confirmation_required",
                        status="BLOCKED",
                        detail="自动模板匹配失败；已生成候选确认表，未写出导入产物",
                        unresolved_count=len(manual_rows),
                        item_code_count=len({row.item_code for row in manual_rows}),
                        candidate_count=sum(len(row.candidates) for row in manual_rows),
                    )
                emit(
                    "provider_prepare_error",
                    status="ERROR",
                    detail=_safe_provider_failure("批次依赖解析失败", exc),
                    error_type=type(exc).__name__,
                    **_provider_error_fields(exc, "provider_prepare"),
                )
            finally:
                if callback_bound:
                    # 恢复调用方原先注入的观察者；RuntimeProvider 会同步
                    # 清理 query_service 的回调，避免下一批次串入本批次事件。
                    set_event_callback(previous_provider_callback)
        refresh_paper_metadata()
        paper_only_order_set = set(paper_only_orders)
        for order_number, task in tasks.items():
            emit("generation_resolve_start", order_number=order_number, status="STARTED", detail="开始解析单号生成依赖")
            if task.errors:
                errors[order_number] = "；".join(str(error) for error in task.errors)
                emit(
                    "generation_resolve_error",
                    order_number=order_number,
                    status="ERROR",
                    detail=errors[order_number],
                    error_type="source_task_error",
                )
                continue
            qa1_scope_blocks = tuple(
                match for match in paper_review_required.get(order_number, ())
                if match.reason.startswith(QA1_PAPER_SCOPE_BLOCKED)
            )
            if qa1_scope_blocks:
                # DEMO-GROUP：沿用既有逐单 errors 出口，不构造空普通记录承载 QA1。
                errors[order_number] = "；".join(match.reason for match in qa1_scope_blocks)
                emit(
                    "generation_blocked", order_number=order_number, status="BLOCKED",
                    detail=errors[order_number], error_type=QA1_PAPER_SCOPE_BLOCKED,
                )
                continue
            if order_number in paper_only_order_set:
                controlled_review = any(
                    match.identity_rule_id for match in paper_review_required.get(order_number, ())
                )
                emit(
                    "paper_only_order",
                    order_number=order_number,
                    status="BLOCKED" if controlled_review else "SUCCESS",
                    detail=("该送检单包含待核实项，普通电子记录及导入产物已阻断"
                            if controlled_review
                            else "该送检单全部物项为纸质记录，已跳过数据库草稿匹配和导入产物生成"),
                    excluded_count=len(paper_exclusions.get(order_number, ())),
                    review_required_count=len(paper_review_required.get(order_number, ())),
                )
                continue
            # 每单单独调用解析提供者；某单的模型、Windchill 或上下文异常只
            # 记录到该单，不得让同批其他有效单号失去生成机会。
            try:
                if prepare_error is not None:
                    raise prepare_error
                if prepared is not None:
                    resolved = {order_number: prepared[order_number]} if order_number in prepared else {}
                else:
                    resolved = self.provider.resolve({order_number: task}, engineer)
            except Exception as exc:  # provider 边界统一转成可显示错误，不吞状态
                if isinstance(exc, ManualConfirmationRequired):
                    merge_manual_rows(exc.rows)
                    emit(
                        "manual_confirmation_required",
                        order_number=order_number,
                        status="BLOCKED",
                        detail="自动模板匹配失败；已生成候选确认表，未写出导入产物",
                        unresolved_count=len(manual_rows),
                        item_code_count=len({row.item_code for row in manual_rows}),
                        candidate_count=sum(len(row.candidates) for row in manual_rows),
                    )
                errors[order_number] = _safe_provider_failure("生成依赖解析失败", exc)
                emit(
                    "generation_resolve_error",
                    order_number=order_number,
                    status="ERROR",
                    detail=errors[order_number],
                    error_type=type(exc).__name__,
                    **_provider_error_fields(exc, "generation_resolve"),
                )
                continue
            item = resolved.get(order_number)
            if item is None:
                errors[order_number] = "生成依赖未返回该单号的完整解析对象"
                emit(
                    "generation_resolve_error",
                    order_number=order_number,
                    status="ERROR",
                    detail=errors[order_number],
                    error_type="missing_resolved_order",
                )
                continue
            draft_order_number = getattr(getattr(item, "order_draft", None), "order_number", None)
            sql_order_number = getattr(getattr(item, "sql_order", None), "order_number", None)
            if draft_order_number != order_number or sql_order_number != order_number:
                errors[order_number] = "生成依赖返回对象的单号与请求不一致，已阻断写出"
                emit(
                    "generation_resolve_error",
                    order_number=order_number,
                    status="ERROR",
                    detail=errors[order_number],
                    error_type="order_number_mismatch",
                )
                continue
            emit(
                "generation_resolve_success",
                order_number=order_number,
                status="SUCCESS",
                detail="单号依赖解析成功",
                record_count=len(item.sql_order.records),
            )
            emit(
                "artifact_write_start",
                order_number=order_number,
                status="STARTED",
                detail="开始写出结果工作簿、SQL、BAT 和诊断文件",
            )
            try:
                result_path = output_path_for_order(self.output_dir, order_number)
                write_order_draft(self.template_path, result_path, item.order_draft)
                artifacts = generate_order_artifacts(item.sql_order, self.output_dir)
                outcomes.append(
                    GenerationOutcome(
                        order_number=order_number,
                        row_count=len(task.rows),
                        result_path=result_path,
                        artifacts=artifacts,
                        detail="结果 Excel、SQL、BAT 和诊断文件已生成，等待人工确认",
                        resolved_order=item,
                    )
                )
                emit(
                    "artifact_write_success",
                    order_number=order_number,
                    status="SUCCESS",
                    detail="四类生成文件已写出，等待人工确认",
                    result_filename=result_path.name,
                    sql_filename=artifacts.sql_path.name,
                    bat_filename=artifacts.bat_path.name,
                    diagnostic_filename=artifacts.diagnostic_path.name,
                )
            except Exception as exc:
                errors[order_number] = _safe_provider_failure("生成文件失败", exc)
                emit(
                    "artifact_write_error",
                    order_number=order_number,
                    status="ERROR",
                    detail=errors[order_number],
                    error_type=type(exc).__name__,
                )
        return GenerationBatch(
            tuple(outcomes),
            errors,
            tuple(events),
            manual_rows,
            paper_exclusions,
            paper_review_required,
            paper_only_orders,
        )


@dataclass(frozen=True)
class SnapshotResolvedOrderProvider:
    """用只读快照和注入式 Windchill 名称构造生成对象。

    该实现不会启动任何外部进程。调用方必须先取得快照和名称，并明确提供
    模型中的数值字段；无法解析的字段直接失败，绝不写入猜测值。
    """

    snapshot: object
    # 生产调用按单注入名称结果时可传 ``{order_number: results}``；保留
    # Sequence 形态用于单任务/静态测试，但不得把整批结果靠猜测过滤。
    windchill_results: Sequence[WindchillName] | Mapping[str, Sequence[WindchillName]]
    event_callback: ProviderEventCallback | None = None
    procedure_aliases: ProcedureAliasResolver | None = None
    # code -> model id (confirmed) or None (explicitly declined).  This map is
    # intentionally per-run and never written into the global alias config.
    manual_confirmations: Mapping[str, object] | None = None
    # RuntimeGenerationProvider may aggregate once before Windchill 查询；把该
    # 次收集的提醒带入最终 SqlOrder，避免诊断 JSON 丢失或重复事件。
    precomputed_m_code_warnings: Mapping[str, Sequence[str]] | None = None

    def resolve(self, tasks: Mapping[str, OrderTask], engineer: Engineer) -> Mapping[str, ResolvedOrder]:
        # 延迟导入以保持 WO-07 生成模块本身不依赖 MySQL 查询实现。
        from .mysql_query import (
            ModelMappingError,
            ReadonlySnapshot,
            resolve_preconfigured_engineer,
            select_unique_active_model,
        )
        from .windchill_adapter import WindchillAdapterError, assert_success

        if not isinstance(self.snapshot, ReadonlySnapshot):
            raise GenerationError("生成需要 ReadonlySnapshot；不得使用未解析的模型数据")
        active_user = resolve_preconfigured_engineer(engineer, self.snapshot.active_users)
        valid_tasks = [task for task in tasks.values() if not task.errors]
        if isinstance(self.windchill_results, Mapping) and len(valid_tasks) > 1:
            # 显式逐单映射也支持直接批量解析；每单子集独立执行同一严格
            # 校验，跨单重复物料不会被误判为重复结果。
            combined: dict[str, ResolvedOrder] = {}
            manual_rows: list[ManualConfirmationRow] = []
            manual_row_keys: set[str] = set()
            for order_number, task in tasks.items():
                if task.errors:
                    continue
                order_key = task.order_number
                if order_key not in self.windchill_results:
                    raise GenerationError(f"单号 {order_key} 缺少按单提供的 Windchill 名称结果")
                child = SnapshotResolvedOrderProvider(
                    self.snapshot,
                    self.windchill_results[order_key],
                    event_callback=self.event_callback,
                    procedure_aliases=self.procedure_aliases,
                    manual_confirmations=self.manual_confirmations,
                    precomputed_m_code_warnings=(
                        {order_number: tuple(self.precomputed_m_code_warnings.get(order_number, ())) }
                        if self.precomputed_m_code_warnings is not None
                        else None
                    ),
                )
                try:
                    child_result = child.resolve({order_number: task}, engineer)
                except ManualConfirmationRequired as exc:
                    # 当前单号的候选不足以阻断同批其他单号；按稳定复合键
                    # 合并候选，待全部单号扫描结束后一次性通知 UI。
                    for row in exc.rows:
                        key = row.confirmation_key
                        if key in manual_row_keys:
                            continue
                        manual_row_keys.add(key)
                        manual_rows.append(row)
                    continue
                combined.update(child_result)
            if manual_rows:
                raise ManualConfirmationRequired(tuple(manual_rows))
            return combined
        windchill_results = self._results_for_tasks(tasks, valid_tasks)
        combinations_by_order = {}
        m_code_warnings_by_order: dict[str, list[str]] = {}
        for task in valid_tasks:
            warnings: list[str] = list(
                self.precomputed_m_code_warnings.get(task.order_number, ())
                if self.precomputed_m_code_warnings is not None
                else ()
            )
            warning_seen = set(warnings)

            def emit_m_code_warning(
                detail: str,
                *,
                _task: OrderTask = task,
                _warnings: list[str] = warnings,
            ) -> None:
                if detail in warning_seen:
                    return
                warning_seen.add(detail)
                _warnings.append(detail)
                callback = self.event_callback
                if callback is None:
                    return
                try:
                    item_code = ""
                    marker = f"送检单 {_task.order_number}、物料 "
                    if detail.startswith(marker):
                        item_code = detail[len(marker) :].split("：", 1)[0].strip()
                    callback(
                        {
                            "name": "m_code_range_warning",
                            "status": "WARNING",
                            "detail": detail,
                            "order_number": _task.order_number,
                            "item_code": item_code,
                            "blocking": False,
                            "warning_type": "m_code_prefix_mismatch",
                        }
                    )
                except Exception:
                    # 诊断观察者不得改变生成结果。
                    pass

            try:
                combinations_by_order[task.order_number] = aggregate_task(
                    task,
                    warning_callback=emit_m_code_warning,
                )
                m_code_warnings_by_order[task.order_number] = warnings
            except CombinationAggregationError as exc:
                raise GenerationError(str(exc)) from exc
        expected_codes = list(
            dict.fromkeys(
                combination.item_code.strip()
                for combinations in combinations_by_order.values()
                for combination in combinations
            )
        )
        try:
            # Windchill 结果必须与本批次物料编码一一对应；禁止缺失、重复、
            # 失败、空名称或额外编码在字典转换时被静默覆盖/忽略。
            assert_success(windchill_results, expected_codes)
        except WindchillAdapterError as exc:
            raise GenerationError(f"Windchill 名称结果校验失败：{exc}") from exc
        names = {item.item_code: item for item in windchill_results}
        output_names = dict(names)
        result: dict[str, ResolvedOrder] = {}
        unresolved_rows: dict[str, ManualConfirmationRow] = {}

        def manual_decision_for(order_number: str, item_code: str) -> tuple[bool, object | None]:
            """按复合键读取人工决策；唯一编码时兼容旧 raw key。"""

            if self.manual_confirmations is None:
                return False, None
            composite = f"{order_number}::{item_code}"
            if composite in self.manual_confirmations:
                return True, self.manual_confirmations[composite]
            if item_code in self.manual_confirmations:
                return True, self.manual_confirmations[item_code]
            return False, None
        for order_number, task in tasks.items():
            if task.errors:
                continue
            combinations = combinations_by_order[order_number]
            records: list[SqlRecord] = []
            included_combinations = []
            qa1_specs: list[Qa1DraftSpec] = []
            for combination in combinations:
                source = combination.source
                code = combination.item_code.strip()
                decision: object = None
                windchill = names.get(code)
                if windchill is None or windchill.status != "SUCCESS" or not windchill.name.strip():
                    raise GenerationError(f"物料 {code} 缺少有效 Windchill 检查单名称")
                controlled_pcm = code in SYNTHETIC_GROUP_ITEM_CODES
                if controlled_pcm:
                    # 在通用名称归一化及物料别名前检查原文，不能把 DOC101-001 截成 002。
                    raw_pcm_name = windchill.name.strip()
                    raw_pcm_codes = re.findall(
                        r"(?<![A-Za-z0-9#井])[A-Za-z0-9][A-Za-z0-9#井]*(?:-[A-Za-z0-9#井]+)+(?![A-Za-z0-9#井])",
                        raw_pcm_name,
                    )
                    pcm_reference = parse_windchill_document_reference(raw_pcm_name)
                    identity_valid = (
                        len(raw_pcm_codes) == 1
                        and pcm_reference.parse_status == PARSE_VALID
                        and pcm_reference.exclude_key == SYNTHETIC_GROUP_EXCLUDE_KEY
                    ) if raw_pcm_codes else raw_pcm_name == SYNTHETIC_GROUP_SOURCE_NAME
                    if not identity_valid:
                        raise GenerationError(
                            f"DEMO-GROUP 优化 PCM 物料 {code} 的原始规范身份不是唯一 DOC101-002 或已确认完整纯名称；"
                            "普通电子记录已阻断，禁止物料别名及人工回退"
                        )
                procedure_name = normalize_procedure_name(windchill.name)
                source_procedure_name = procedure_name
                alias_resolution = None
                try:
                    if self.procedure_aliases is not None:
                        alias_resolution = self.procedure_aliases.resolve(
                            item_code=code,
                            source_name=windchill.name,
                            normalized_name=procedure_name,
                        )
                except ProcedureAliasError as exc:
                    raise GenerationError(f"检查单别名解析失败：{exc}") from exc
                if controlled_pcm and (
                    alias_resolution is None
                    or alias_resolution.matched_by != "item_code"
                    or not alias_resolution.alias.strict_target_match
                    or alias_resolution.alias.rule_id != f"full_confirmed_50299821_{code}"
                    or alias_resolution.alias.source_name != SYNTHETIC_GROUP_SOURCE_NAME
                    or alias_resolution.target_procedure_name != SYNTHETIC_GROUP_TARGET_NAME
                ):
                    raise GenerationError(f"DEMO-GROUP 优化 PCM 物料 {code} 缺少已确认严格配置，禁止旧模板及人工回退；请完整重启")
                target_procedure_name = (
                    alias_resolution.target_procedure_name
                    if alias_resolution is not None
                    else procedure_name
                )
                alias_fields: dict[str, object] = {
                    "alias_applied": alias_resolution is not None,
                    "source_procedure_name": procedure_name,
                    "target_procedure_name": target_procedure_name,
                }
                if alias_resolution is not None:
                    alias_fields.update(
                        {
                            "alias_matched_by": alias_resolution.matched_by,
                            "alias_version": alias_resolution.alias.version,
                            "alias_effective_date": alias_resolution.alias.effective_date,
                            "alias_rule_id": alias_resolution.alias.rule_id,
                            "strict_target_match": alias_resolution.alias.strict_target_match,
                            "alias_config_sha256": alias_resolution.alias.config_sha256,
                        }
                    )

                def emit_model_match(payload: Mapping[str, object]) -> None:
                    fields = dict(payload)
                    fields.update(
                        {
                            "order_number": task.order_number,
                            "item_code": code,
                            "raw_name": windchill.name,
                            **alias_fields,
                        }
                    )
                    if controlled_pcm:
                        selected_id = str(payload.get("selected_model_id", ""))
                        attached = tuple(
                            row for row in self.snapshot.attached_models
                            if selected_id and str(row.procedure_model_id) == selected_id
                        )
                        fields.update({
                            "pcm_document_code": pcm_reference.document_code,
                            "pcm_exclude_key": SYNTHETIC_GROUP_EXCLUDE_KEY,
                            "pcm_identity_source": "document_code" if raw_pcm_codes else "confirmed_item_exact_name",
                            "attached_model_ids": " | ".join(str(row.id) for row in attached),
                            "attached_parent_ids": " | ".join(str(row.procedure_model_id) for row in attached),
                        })
                    if self.event_callback is not None:
                        try:
                            self.event_callback(fields)
                        except Exception:
                            pass

                try:
                    model = select_unique_active_model(
                        self.snapshot.procedure_models,
                        target_procedure_name,
                        diagnostics_callback=emit_model_match,
                        prefer_exact=alias_resolution is not None,
                        strict_exact=controlled_pcm,
                    )
                except ModelMappingError as exc:
                    if controlled_pcm:
                        # 本次目标失败不能进入通用人工候选，旧 ID21/任意人工 ID 均无解除路径。
                        raise GenerationError(f"DEMO-GROUP 优化 PCM 物料 {code} 严格目标匹配阻断：{exc}") from exc
                    # 第一次运行只收集候选，不猜测模型 ID，也不写出草稿。
                    # 重新运行时，UI 的明确决策才允许从当前有效快照中选定 ID。
                    decision_present, decision = manual_decision_for(task.order_number, code)
                    if decision_present and not decision:
                        if self.event_callback is not None:
                            self.event_callback(
                                {
                                    "name": "manual_confirmation_result",
                                    "status": "DECLINED",
                                    "detail": "操作员未勾选该物项，按人工放弃处理",
                                    "item_code": code,
                                    "order_number": task.order_number,
                                    "reason": "manual_declined",
                                }
                            )
                        continue
                    if decision_present and decision:
                        manual_name = ""
                        decision_id = decision
                        if isinstance(decision, ManualConfirmationDecision):
                            decision_id = decision.model_id
                            manual_name = decision.manual_name.strip()
                        elif isinstance(decision, Mapping):
                            decision_id = str(decision.get("model_id", decision.get("id", ""))).strip()
                            manual_name = str(decision.get("manual_name", decision.get("name", ""))).strip()
                        selected = None
                        if str(decision_id).strip():
                            selected = next(
                                (
                                    candidate
                                    for candidate in self.snapshot.procedure_models
                                    if str(candidate.id).strip() == str(decision_id).strip()
                                ),
                                None,
                            )
                        elif manual_name:
                            try:
                                selected = select_unique_active_model(
                                    self.snapshot.procedure_models,
                                    normalize_procedure_name(manual_name),
                                    prefer_exact=True,
                                )
                                decision_id = selected.id
                            except ModelMappingError as name_exc:
                                raise GenerationError(
                                    f"物项 {code} 的人工输入检查单名称未唯一匹配当前有效模板：{manual_name}"
                                ) from name_exc
                        if selected is None:
                            raise GenerationError(f"物项 {code} 的人工确认模型 ID 不存在：{decision_id}") from exc
                        if not (
                            selected.is_lasted_version == "1"
                            and selected.is_frozen == "0"
                            and selected.processstate == "2"
                        ):
                            raise GenerationError(f"物项 {code} 的人工确认模型 ID{decision_id} 不是当前有效模板") from exc
                        if manual_name:
                            try:
                                manual_model = select_unique_active_model(
                                    self.snapshot.procedure_models,
                                    normalize_procedure_name(manual_name),
                                    prefer_exact=True,
                                )
                            except ModelMappingError as name_exc:
                                raise GenerationError(
                                    f"物项 {code} 的人工输入检查单名称未唯一匹配当前有效模板：{manual_name}"
                                ) from name_exc
                            if str(manual_model.id).strip() != str(selected.id).strip():
                                raise GenerationError(
                                    f"物项 {code} 的人工输入名称与所选模板 ID 不一致：{manual_name} / ID{selected.id}"
                                ) from exc
                            target_procedure_name = normalize_procedure_name(manual_name)
                            procedure_name = target_procedure_name
                            # 人工确认只替换普通检查单名称；保留 Windchill
                            # v1.1 C/D/E 原始字段，否则恢复生成会丢失 QA1
                            # 触发依据和旧物项识别码 provenance。
                            output_names[code] = WindchillName(
                                code,
                                target_procedure_name,
                                "SUCCESS",
                                legacy_item_code=windchill.legacy_item_code,
                                quality_grade=windchill.quality_grade,
                                quality_grade_effective=windchill.quality_grade_effective,
                                quality_grade_basis=windchill.quality_grade_basis,
                                status_raw=windchill.status_raw,
                            )
                        model = selected
                        if self.event_callback is not None:
                            self.event_callback(
                                {
                                    "name": "manual_confirmation_result",
                                    "status": "CONFIRMED",
                                    "detail": "操作员已确认候选模板",
                                    "item_code": code,
                                    "order_number": task.order_number,
                                    "model_id": str(model.id),
                                    "manual_name": manual_name,
                                    "reason": "manual_confirmed",
                                }
                            )
                    else:
                        unresolved_rows.setdefault(
                            f"{task.order_number}::{code}",
                            build_confirmation_row(
                                order_number=task.order_number,
                                item_code=code,
                                raw_name=windchill.legacy_item_code,
                                normalized_name=procedure_name,
                                models=self.snapshot.procedure_models,
                                blocking_reason=str(exc),
                            ),
                        )
                        continue
                included_combinations.append(combination)
                # QA1 关键零部件是普通草稿之后的附加链。这里仅针对已经
                # 聚合的 ``combination`` 计算候选并发出安全诊断；未命中和
                # 多候选均不阻断普通草稿，更不能静默选第一条。
                if windchill.is_qa1:
                    qa1_result = match_qa1_template(
                        windchill.legacy_item_code,
                        self.snapshot.procedure_models,
                        item_code=code,
                    )
                    qa1_specs.append(
                        Qa1DraftSpec(
                            combination=combination,
                            legacy_item_code=windchill.legacy_item_code,
                            candidates=qa1_result.candidates,
                            matched_by=qa1_result.matched_by,
                            selected_model=qa1_result.selected_model,
                            normalized_name=procedure_name,
                        )
                    )
                    if self.event_callback is not None:
                        try:
                            if qa1_result.is_unique:
                                self.event_callback(
                                    {
                                        "name": "key_component_match",
                                        "status": "SUCCESS",
                                        "detail": "QA1 关键零部件模板唯一命中，已形成候选说明；API 附加草稿需调用方明确编排",
                                        "order_number": task.order_number,
                                        "item_code": code,
                                        "legacy_item_code": windchill.legacy_item_code,
                                        "matched_by": qa1_result.matched_by,
                                        "model_id": str(qa1_result.selected_model.id),
                                    }
                                )
                            elif qa1_result.requires_manual_selection:
                                self.event_callback(
                                    {
                                        "name": "key_component_manual_required",
                                        "status": "PENDING",
                                        "detail": "QA1 关键零部件存在多候选，未自动提交",
                                        "order_number": task.order_number,
                                        "item_code": code,
                                        "legacy_item_code": windchill.legacy_item_code,
                                        "matched_by": qa1_result.matched_by,
                                        "candidate_count": len(qa1_result.candidates),
                                        "candidate_model_ids": ",".join(
                                            str(candidate.id) for candidate in qa1_result.candidates
                                        ),
                                    }
                                )
                            else:
                                self.event_callback(
                                    {
                                        "name": "key_component_no_match",
                                        "status": "WARNING",
                                        "detail": "QA1 关键零部件未命中模板，仅记录审计，不制造草稿",
                                        "order_number": task.order_number,
                                        "item_code": code,
                                        "legacy_item_code": windchill.legacy_item_code,
                                        "matched_by": qa1_result.matched_by,
                                    }
                                )
                        except Exception:
                            # 诊断观察者不得改变普通草稿和候选结果。
                            pass
                contexts = tuple(
                    SqlContextRow(context.context_number, context.inspection_items, context.inspection_standards)
                    for context in self.snapshot.procedure_contexts
                    if context.procedure_model_id == model.id
                )
                values = list(source.values) + [""] * 16
                # aggregate_task/template_writer 已将 Excel 行的 supplier
                # 写成 effective 值；这里仍从 SourceRow 的 raw_supplier
                # 取证，确保 SqlRecord/SQL/diagnostic 与 Excel 同步而不丢
                # TK 空供应商的原始 provenance。
                raw_supplier = source.raw_supplier
                if raw_supplier is None or (
                    not raw_supplier
                    and not source.supplier_defaulted
                    and len(source.values) > 5
                    and source.values[5] is not None
                    and str(source.values[5]).strip()
                ):
                    raw_supplier = (
                        ""
                        if len(source.values) <= 5 or source.values[5] is None
                        else str(source.values[5])
                    )
                effective_supplier, raw_supplier, supplier_defaulted = effective_supplier_for_order(
                    task.order_number,
                    raw_supplier,
                )
                values[5] = effective_supplier
                sampling_diagnostic: dict[str, object] = {}

                def emit_sampling_diagnostic() -> None:
                    # 白名单诊断来自抽样函数，不读取连接配置；日志失败不能
                    # 改变抽样结果。该元数据还会进入同单号 diagnostic.json。
                    sampling_diagnostic.update(
                        order_number=task.order_number,
                        item_code=code,
                        model_id=str(model.id),
                        trigger_basis=combination.trigger_basis,
                        source_sheet=source.sheet_name,
                        source_rows=",".join(str(row.source_row) for row in combination.source_rows),
                    )
                    for key, value in sampling_diagnostic.items():
                        if isinstance(value, str):
                            sampling_diagnostic[key] = redact_log_text(value)
                    if self.event_callback is not None:
                        try:
                            self.event_callback({
                                **sampling_diagnostic,
                                "name": "sampling_decision",
                                "detail": "；".join(filter(None, (
                                    SAMPLING_REASON_LABELS.get(str(sampling_diagnostic.get("reason")), "抽样诊断"),
                                    ACCEPTANCE_REASON_LABELS.get(str(sampling_diagnostic.get("acceptance_reason")), ""),
                                ))),
                            })
                        except Exception:
                            pass

                try:
                    sampling = resolve_sampling(
                        check_plan=model.check_plan,
                        accept_standard=model.accept_standard,
                        item_name=combination.item_name,
                        quantity=combination.quantity,
                        diagnostics=sampling_diagnostic,
                    )
                except SamplingRuleError as exc:
                    emit_sampling_diagnostic()
                    raise GenerationError(str(exc)) from exc
                emit_sampling_diagnostic()
                # 附表是主表草稿的子记录：检查单号继承主草稿最终 trigger_basis，
                # 物项识别码和检查数量由 SqlRecord 统一写入；模板的检查项目、
                # 标准范围及空槽 NA/inspection_content 在这里一次性生成，
                # 避免外部上传路径漏掉前端的初始化规则。
                attached = tuple(
                    build_attached_row_from_template(
                        attached_model.inspection_items,
                        attached_model.standard_ranges,
                        inspection_number=combination.trigger_basis,
                        notes=attached_model.notes,
                    )
                    for attached_model in self.snapshot.attached_models
                    if attached_model.procedure_model_id == model.id
                )
                records.append(
                    SqlRecord(
                        item_code=code,
                        item_name=str(values[4]).strip(),
                        quantity=values[9],
                        supplier=effective_supplier,
                        raw_supplier=raw_supplier,
                        supplier_defaulted=supplier_defaulted,
                        procedure_name=procedure_name,
                        procedure_model_id=_integer(model.id, "procedure_model_id"),
                        engineer_id=_integer(active_user.user_id, "engineer_id"),
                        engineer_name=engineer.display_name,
                        engineer_dept_id=active_user.dept_id,
                        trigger_basis=combination.trigger_basis,
                        check_number=sampling.check_number,
                        accept_standard=sampling.accept_standard,
                        sampling_diagnostic=sampling_diagnostic,
                        # 旧 BAS 将模型中的文本检验方案用于抽样计算，
                        # 草稿表的 check_plan 固定写入 -1。
                        check_plan=-1,
                        contexts=contexts,
                        sequence_code=combination.sequence_code,
                        measurement_unit=(
                            source.measurement_unit.strip()
                            or (str(values[18]).strip() if len(values) > 18 and values[18] is not None else "")
                        ),
                        attached_tables=attached,
                        alias_applied=alias_resolution is not None,
                        source_procedure_name=source_procedure_name,
                        target_procedure_name=target_procedure_name,
                        alias_matched_by=(alias_resolution.matched_by if alias_resolution is not None else ""),
                        alias_rule_id=(alias_resolution.alias.rule_id if alias_resolution is not None else ""),
                        alias_version=(alias_resolution.alias.version if alias_resolution is not None else ""),
                        alias_effective_date=(alias_resolution.alias.effective_date if alias_resolution is not None else ""),
                        manual_confirmation_applied=isinstance(decision, ManualConfirmationDecision)
                        or (isinstance(decision, Mapping) and bool(decision.get("model_id", decision.get("id")))),
                        manual_input_name=(
                            decision.manual_name
                            if isinstance(decision, ManualConfirmationDecision)
                            else (str(decision.get("manual_name", decision.get("name", ""))) if isinstance(decision, Mapping) else "")
                        ),
                        manual_input_basis=("manual_name_unique_active" if (
                            isinstance(decision, ManualConfirmationDecision) and decision.manual_name.strip()
                        ) or (
                            isinstance(decision, Mapping) and str(decision.get("manual_name", decision.get("name", ""))).strip()
                        ) else ("manual_model_id" if (
                            isinstance(decision, ManualConfirmationDecision) or isinstance(decision, Mapping)
                        ) else "")),
                    )
                )
            if not records and not unresolved_rows:
                # 人工确认阶段允许操作员把一个单号的全部未解析物项都
                # 明确放弃。该单号不得写出任何 Excel/SQL/BAT/diagnostic，
                # 但也不能让同一批次的其他单号被这个“空单”连带阻断；
                # GenerationService 会把缺失的映射记录为该单号的阻断，
                # 并继续写出其他已有确认项的单号。
                if self.manual_confirmations is not None:
                    continue
                raise GenerationError(f"单号 {order_number} 没有可生成的已确认物项，未生成导入草稿")
            if not records:
                # 本轮仅收集候选；等所有订单扫描结束后统一抛出
                # ManualConfirmationRequired，确保没有空 SqlOrder 被构造。
                continue
            sql_order = SqlOrder(
                order_number,
                tuple(records),
                tuple(m_code_warnings_by_order.get(order_number, ())),
            )
            # 模板组装再次验证 Windchill 返回和 A:P/R:S 映射，Q 列不进入对象。
            order_draft = assemble_order_draft(
                task,
                tuple(output_names[code] for code in dict.fromkeys(combination.item_code.strip() for combination in included_combinations)),
                engineer,
                combinations=included_combinations,
            )
            result[order_number] = ResolvedOrder(order_draft, sql_order, tuple(qa1_specs))
        if unresolved_rows:
            raise ManualConfirmationRequired(tuple(unresolved_rows.values()))
        return result

    def _results_for_tasks(
        self,
        tasks: Mapping[str, OrderTask],
        valid_tasks: Sequence[OrderTask],
    ) -> list[WindchillName]:
        """解析当前调用的名称结果，不对共享 Sequence 做隐式筛选。"""

        if not isinstance(self.windchill_results, Mapping):
            return list(self.windchill_results)
        valid_orders = [task.order_number for task in valid_tasks]
        if len(valid_orders) > 1:
            raise GenerationError("Windchill 名称映射必须按单号逐次解析，不接受未分组的共享结果")
        if not valid_orders:
            return []
        order_number = valid_orders[0]
        if order_number not in self.windchill_results:
            raise GenerationError(f"单号 {order_number} 缺少按单提供的 Windchill 名称结果")
        return list(self.windchill_results[order_number])


@dataclass
class RuntimeGenerationProvider:
    """现场运行时 provider：批次只读快照 + 一次 Windchill 名称查询。

    ``query_service`` 和 ``windchill_adapter`` 均通过调用方注入，避免本层
    猜测连接、BIFF 或外部程序协议。真实按钮调用时才执行其 ``load_snapshot``
    与 ``query_names``；构造和预检阶段只检查路径/对象，不连接数据库。
    """

    query_service: object
    windchill_adapter: object
    mysql_config_path: Path | None = None
    procedure_aliases_path: Path | None = None
    procedure_aliases: ProcedureAliasResolver | None = None
    paper_standard_catalog: PaperStandardCatalog | None = None
    snapshot: object | None = None
    windchill_results: dict[str, tuple[WindchillName, ...]] = field(default_factory=dict)
    paper_exclusions: dict[str, tuple[PaperStandardMatch, ...]] = field(default_factory=dict)
    paper_review_required: dict[str, tuple[PaperStandardMatch, ...]] = field(default_factory=dict)
    paper_only_orders: tuple[str, ...] = ()
    event_callback: ProviderEventCallback | None = None
    # UI 逐行确认的临时决策；只存在本次运行内，不写入全局配置。
    manual_confirmations: Mapping[str, object] | None = None
    _prepared_tasks: dict[str, OrderTask] = field(default_factory=dict, init=False, repr=False)
    _precomputed_m_code_warnings: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._bind_query_callback()

    def _bind_query_callback(self) -> None:
        setter = getattr(self.query_service, "set_event_callback", None)
        if callable(setter):
            setter(self._emit if self.event_callback is not None else None)

    def set_event_callback(self, callback: ProviderEventCallback | None) -> None:
        """设置/清除依赖层诊断回调；不改变旧 provider 的调用协议。"""

        self.event_callback = callback
        self._bind_query_callback()

    def set_manual_confirmations(self, decisions: Mapping[str, object]) -> None:
        """设置一次人工确认结果，下一次生成复用已取得的只读数据。"""

        merged = dict(self.manual_confirmations or {})
        # 同一批次允许 UI 分页/分组回传确认结果；新批次结果覆盖旧
        # key，但 None/False 仍是明确的“放弃”决定，不能被丢弃。
        merged.update(decisions)
        self.manual_confirmations = merged

    def set_paper_standard_catalog(self, catalog: PaperStandardCatalog | None) -> None:
        """注入当前 UI 批次选择的线下规范目录，不读取或固定任何默认路径。"""

        self.paper_standard_catalog = catalog
        self.paper_exclusions = {}
        self.paper_review_required = {}
        self.paper_only_orders = ()
        self.manual_confirmations = None
        self._prepared_tasks = {}
        self._precomputed_m_code_warnings = {}

    def reset_runtime_state(self) -> None:
        """清理当前批次的运行时快照，供 UI“重置界面”显式调用。

        ``snapshot``、Windchill 名称结果和人工确认决定只应在同一批次的
        恢复生成中复用。若操作员在人工确认取消、读取异常或导入异常后
        开始下一批，继续保留这些字段会把上一批的只读模型/候选决定带入
        新批次。该方法只清理 provider 内存，不删除别名配置、审计日志或
        任何持久化文件；UI 会先确认没有活动 worker/待处理回调再调用。
        """

        self.snapshot = None
        # 重新赋空字典而非原地 clear，兼容测试/现场注入 provider 曾将
        # 该字段替换为只读 Mapping 或 None 的情况，确保复位入口不抛异常。
        self.windchill_results = {}
        self.paper_standard_catalog = None
        self.paper_exclusions = {}
        self.paper_review_required = {}
        self.paper_only_orders = ()
        self._prepared_tasks = {}
        self._precomputed_m_code_warnings = {}
        self.manual_confirmations = None

    def _emit(self, payload: Mapping[str, object]) -> None:
        callback = self.event_callback
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:
            # 诊断观察者不得改变真实查询/适配器的结果。
            return

    def preflight(self) -> Mapping[str, object]:
        client = getattr(self.query_service, "client", None)
        readonly_bat = getattr(client, "readonly_bat", None)
        exe_path = getattr(self.windchill_adapter, "exe_path", None)
        information_xls = getattr(self.windchill_adapter, "information_xls", None)
        backend = getattr(self.windchill_adapter, "backend", None)
        missing: list[str] = []
        if self.mysql_config_path is not None and not self.mysql_config_path.is_file():
            missing.append("mysql_connection.bat")
        missing.extend(
            label
            for label, path in (
                ("readonly_query_bat", readonly_bat),
                ("windchill_exe", exe_path),
                ("information_xls", information_xls),
            )
            if not isinstance(path, Path) or not path.is_file()
        )
        if self.procedure_aliases_path is not None and not self.procedure_aliases_path.is_file():
            missing.append("procedure_aliases.json")
        if backend is None:
            missing.append("information_xls_backend")
        return {
            "status": "SUCCESS" if not missing else "BLOCKED",
            "detail": (
                "现场生成依赖路径/backend 预检已就绪；仅完成路径/backend 预检，尚未访问 DB/Windchill"
                if not missing
                else "现场生成依赖路径/backend 预检缺失；仅完成路径/backend 预检，尚未访问 DB/Windchill；保持安全阻断"
            ),
            "basis": "runtime_provider_paths",
            "missing_dependency_count": len(missing),
            "missing_dependencies": ",".join(missing),
            "snapshot_loaded": self.snapshot is not None,
            "windchill_result_count": sum(len(items) for items in self.windchill_results.values()),
            "paper_catalog_loaded": self.paper_standard_catalog is not None,
            "paper_catalog_entry_count": (
                len(self.paper_standard_catalog.entries)
                if self.paper_standard_catalog is not None
                else 0
            ),
        }

    def _ensure_aliases_loaded(self) -> None:
        if self.procedure_aliases is not None or self.procedure_aliases_path is None:
            return
        if not self.procedure_aliases_path.is_file():
            raise GenerationError(f"检查单别名配置缺失：{self.procedure_aliases_path}")
        try:
            self.procedure_aliases = load_procedure_aliases(self.procedure_aliases_path)
        except ProcedureAliasError as exc:
            raise GenerationError(f"检查单别名配置无效：{exc}") from exc

    def prepare(self, tasks: Mapping[str, OrderTask], engineer: Engineer) -> Mapping[str, ResolvedOrder]:
        if not any(not task.errors and task.rows for task in tasks.values()):
            return {}
        self._bind_query_callback()
        # 人工确认后的恢复调用复用本次已完成的 Windchill/只读快照，避免
        # 弹窗确认期间再次请求外部系统；纸质过滤结果也不得回流。
        if self.manual_confirmations is not None and self.snapshot is not None and self.windchill_results:
            self._ensure_aliases_loaded()
            return SnapshotResolvedOrderProvider(
                self.snapshot,
                self.windchill_results,
                event_callback=self.event_callback,
                procedure_aliases=self.procedure_aliases,
                manual_confirmations=self.manual_confirmations,
                precomputed_m_code_warnings=self._precomputed_m_code_warnings,
            ).resolve(self._prepared_tasks or dict(tasks), engineer)

        self.snapshot = None
        self.windchill_results = {}
        self.paper_exclusions = {}
        self.paper_review_required = {}
        self.paper_only_orders = ()
        self._prepared_tasks = {}
        self._precomputed_m_code_warnings = {}

        combinations_by_order = {}
        m_code_warnings_by_order: dict[str, tuple[str, ...]] = {}
        for task in tasks.values():
            if task.errors:
                continue
            warnings: list[str] = []
            warning_seen: set[str] = set()

            def emit_m_code_warning(
                detail: str,
                *,
                _task: OrderTask = task,
                _warnings: list[str] = warnings,
                _warning_seen: set[str] = warning_seen,
            ) -> None:
                try:
                    if detail in _warning_seen:
                        return
                    _warning_seen.add(detail)
                    _warnings.append(detail)
                    item_code = ""
                    marker = f"送检单 {_task.order_number}、物料 "
                    if detail.startswith(marker):
                        item_code = detail[len(marker) :].split("：", 1)[0].strip()
                    self._emit(
                        {
                            "name": "m_code_range_warning",
                            "status": "WARNING",
                            "detail": detail,
                            "order_number": _task.order_number,
                            "item_code": item_code,
                            "blocking": False,
                            "warning_type": "m_code_prefix_mismatch",
                        }
                    )
                except Exception:
                    # 诊断观察者不得改变查询或生成结果。
                    pass
            try:
                combinations_by_order[task.order_number] = aggregate_task(
                    task,
                    warning_callback=emit_m_code_warning,
                )
                m_code_warnings_by_order[task.order_number] = tuple(warnings)
            except CombinationAggregationError as exc:
                raise GenerationError(str(exc)) from exc

        item_codes = list(
            dict.fromkeys(
                combination.item_code.strip()
                for combinations in combinations_by_order.values()
                for combination in combinations
                if combination.item_code.strip()
            )
        )
        self._emit(
            {
                "name": "windchill_query_start",
                "status": "STARTED",
                "detail": "开始查询 Windchill 物料名称",
                "item_code_count": len(item_codes),
            }
        )
        try:
            names = tuple(self.windchill_adapter.query_names(item_codes))
        except Exception as exc:
            from .windchill_adapter import WindchillEmptyResultError

            empty_result = isinstance(exc, WindchillEmptyResultError)
            query_stage = getattr(exc, "query_stage", None) or "windchill_names"
            safe_detail = getattr(exc, "safe_detail", "") or ("empty_result" if empty_result else str(exc))
            fields: dict[str, object] = {
                "item_code_count": len(item_codes),
                "query_stage": redact_log_text(str(query_stage)),
                "safe_detail": redact_log_text(str(safe_detail)),
            }
            if empty_result:
                fields["result_count"] = 0
            exit_code = getattr(exc, "exit_code", None)
            if exit_code is not None:
                fields["exit_code"] = str(exit_code)
            self._emit(
                {
                    "name": "windchill_query_error",
                    "status": "ERROR",
                    "detail": (
                        "Windchill查询为空"
                        if empty_result
                        else redact_log_text(f"Windchill 名称查询失败：{type(exc).__name__}")
                    ),
                    **fields,
                }
            )
            raise GenerationError(
                "Windchill查询为空" if empty_result else _safe_provider_failure("Windchill 名称查询失败", exc),
                query_stage=getattr(exc, "query_stage", None) or "windchill_names",
                safe_detail=("empty_result" if empty_result else (getattr(exc, "safe_detail", "") or str(exc))),
            ) from exc
        if not names:
            fields = {
                "item_code_count": len(item_codes),
                "result_count": 0,
                "query_stage": "windchill_names",
                "safe_detail": "empty_result",
            }
            self._emit(
                {
                    "name": "windchill_query_error",
                    "status": "ERROR",
                    "detail": "Windchill查询为空",
                    **fields,
                }
            )
            raise GenerationError("Windchill查询为空", query_stage="windchill_names", safe_detail="empty_result")
        self._emit(
            {
                "name": "windchill_query_success",
                "status": "SUCCESS",
                "detail": "Windchill 名称查询成功",
                "item_code_count": len(item_codes),
                "result_count": len(names),
            }
        )
        try:
            from .windchill_adapter import WindchillAdapterError, assert_success

            assert_success(names, item_codes)
        except WindchillAdapterError as exc:
            raise GenerationError(f"Windchill 名称结果校验失败：{exc}") from exc

        names_by_code = {item.item_code: item for item in names}
        filtered_tasks: dict[str, OrderTask] = {}
        filtered_codes_by_order: dict[str, list[str]] = {}
        excluded_by_order: dict[str, list[PaperStandardMatch]] = {}
        review_by_order: dict[str, list[PaperStandardMatch]] = {}
        paper_only_orders: list[str] = []
        catalog = self.paper_standard_catalog
        catalog_sha256 = ""
        if catalog is not None:
            try:
                catalog_sha256 = hashlib.sha256(catalog.source_path.read_bytes()).hexdigest()
            except OSError:
                # 已加载目录仍为本批判定依据；文件不可读只影响审计哈希，不回流电子项。
                pass
        for order, task in tasks.items():
            if task.errors or order not in combinations_by_order:
                continue
            electronic_codes: list[str] = []
            blocked_codes: set[str] = set()
            qa1_scope_blocked = False
            for combination in combinations_by_order[order]:
                code = combination.item_code.strip()
                windchill = names_by_code.get(code)
                if windchill is None:
                    continue
                match = match_paper_standard(windchill.name, catalog, item_code=code)
                # 仅受控 DemoControl 先完成身份判断。其他 QA1 保留既有附加链规则。
                if windchill.is_qa1 and not match.identity_rule_id:
                    electronic_codes.append(code)
                    continue
                if windchill.is_qa1 and match.identity_rule_id:
                    qa1_scope_blocked = True
                    match = replace(
                        match, status=MATCH_REVIEW_REQUIRED,
                        reason=(f"{QA1_PAPER_SCOPE_BLOCKED}：物料 {code}；纸质判定={match.status}；"
                                f"{match.reason}；受控 DemoControl 的 QA1 附加链尚未覆盖，整单普通及 QA1 生成/提交均阻断"),
                    )
                identity_audit = {
                    "windchill_raw": match.windchill_raw,
                    "identity_rule_id": match.identity_rule_id,
                    "identity_source": match.identity_source,
                    "resolved_exclude_key": match.exclude_key,
                    "catalog_path": str(catalog.source_path) if catalog is not None else "",
                    "catalog_file_sha256_at_prepare": catalog_sha256,
                    "quality_grade": windchill.quality_grade,
                    "quality_grade_effective": windchill.quality_grade_effective,
                    "quality_grade_basis": windchill.quality_grade_basis,
                    "legacy_item_code": windchill.legacy_item_code,
                    "is_qa1": windchill.is_qa1,
                } if match.identity_rule_id else {}
                # 普通检查单以 Windchill 名称进入后续模板候选匹配。无法从
                # 名称解析纸质清单编码时只是“无法确认纸质”，不能据此将
                # 物项标成纸质记录；因此 NOT_FOUND 与 UNDETERMINED 均保留
                # 为电子项。只有明确命中纸质清单或编码字符差异待复核时
                # 才在这里阻断电子生成。本次受控 DemoControl 的未知身份是局部例外。
                if match.status in (MATCH_NOT_FOUND, MATCH_UNDETERMINED) and not match.identity_rule_id:
                    electronic_codes.append(code)
                    self._emit(
                        {
                            "name": "paper_standard_exclusion",
                            "order_number": order,
                            "status": match.status,
                            "detail": match.reason or "未命中纸质清单，继续普通名称匹配",
                            "item_code": code,
                            "document_code": match.document_code,
                            "exclude_key": match.exclude_key,
                            "catalog_sheet": match.entry.sheet_name if match.entry is not None else "",
                            "catalog_row": match.entry.source_row if match.entry is not None else "",
                            "paper_match_status": match.status,
                            **identity_audit,
                        }
                    )
                    continue
                blocked_codes.add(code)
                target = excluded_by_order if match.status == MATCH_EXCLUDED else review_by_order
                target.setdefault(order, []).append(match)
                self._emit(
                    {
                        "name": "paper_standard_exclusion",
                        "order_number": order,
                        "status": match.status,
                        "detail": match.reason,
                        "item_code": code,
                        "document_code": match.document_code,
                        "exclude_key": match.exclude_key,
                        "catalog_sheet": match.entry.sheet_name if match.entry is not None else "",
                        "catalog_row": match.entry.source_row if match.entry is not None else "",
                        "paper_match_status": match.status,
                        **identity_audit,
                    }
                )
            if qa1_scope_blocked:
                # 同单普通项也不进入预备任务；恢复生成继续依赖 review_by_order 的专用原因。
                continue
            unique_electronic_codes = list(dict.fromkeys(electronic_codes))
            if unique_electronic_codes:
                electronic_code_set = set(unique_electronic_codes)
                filtered_tasks[order] = OrderTask(
                    order_number=task.order_number,
                    rows=[row for row in task.rows if row.item_code.strip() in electronic_code_set],
                    errors=list(task.errors),
                )
                filtered_codes_by_order[order] = unique_electronic_codes
                self._precomputed_m_code_warnings[order] = tuple(m_code_warnings_by_order.get(order, ()))
            elif blocked_codes:
                paper_only_orders.append(order)

        self.paper_exclusions = {order: tuple(matches) for order, matches in excluded_by_order.items()}
        self.paper_review_required = {order: tuple(matches) for order, matches in review_by_order.items()}
        self.paper_only_orders = tuple(paper_only_orders)
        self._prepared_tasks = filtered_tasks
        if not filtered_tasks:
            controlled_review = any(
                match.identity_rule_id for matches in self.paper_review_required.values() for match in matches
            )
            self._emit(
                {
                    "name": "paper_standard_batch_only",
                    "status": "BLOCKED" if controlled_review else "SUCCESS",
                    "detail": ("本批次含待核实或 QA1 范围阻断项，无可生成电子项，未加载只读数据库快照"
                               if controlled_review
                               else "本批次全部有效物项为纸质记录，未加载只读数据库快照"),
                    "paper_only_order_count": len(self.paper_only_orders),
                    "paper_excluded_count": sum(len(matches) for matches in self.paper_exclusions.values()),
                    "paper_review_required_count": sum(len(matches) for matches in self.paper_review_required.values()),
                }
            )
            return {}

        self._ensure_aliases_loaded()
        try:
            snapshot = self.query_service.load_snapshot()
        except Exception as exc:
            raise GenerationError(
                _safe_provider_failure("只读模型快照加载失败", exc),
                query_stage=getattr(exc, "query_stage", None) or "readonly_snapshot",
                safe_detail=getattr(exc, "safe_detail", "") or str(exc),
            ) from exc
        self.snapshot = snapshot
        self.windchill_results = {
            order: tuple(names_by_code[code] for code in filtered_codes_by_order.get(order, ()) if code in names_by_code)
            for order in filtered_tasks
        }
        return SnapshotResolvedOrderProvider(
            snapshot,
            self.windchill_results,
            event_callback=self.event_callback,
            procedure_aliases=self.procedure_aliases,
            manual_confirmations=self.manual_confirmations,
            precomputed_m_code_warnings=self._precomputed_m_code_warnings,
        ).resolve(filtered_tasks, engineer)

    def resolve(self, tasks: Mapping[str, OrderTask], engineer: Engineer) -> Mapping[str, ResolvedOrder]:
        # 兼容未使用批次 prepare 的嵌入调用；GenerationService 会优先调用 prepare。
        if self.snapshot is None:
            return self.prepare(tasks, engineer)
        self._ensure_aliases_loaded()
        if len(tasks) == 1:
            order_number = next(iter(tasks))
            names = self.windchill_results.get(order_number)
            if names is not None:
                return SnapshotResolvedOrderProvider(
                    self.snapshot,
                    names,
                    procedure_aliases=self.procedure_aliases,
                    manual_confirmations=self.manual_confirmations,
                ).resolve(tasks, engineer)
        return self.prepare(tasks, engineer)


def _integer(value: object, field_name: str) -> int:
    text = str(value).strip()
    if not text or not text.lstrip("+-").isdigit():
        raise GenerationError(f"{field_name} 未解析为整数，拒绝猜测")
    return int(text, 10)


def _safe_provider_failure(prefix: str, error: BaseException) -> str:
    """保留业务错误语义，同时去掉异常中可能携带的连接值。"""

    base_detail = str(error).strip()
    attached_detail = getattr(error, "safe_detail", "")
    if attached_detail and str(attached_detail).strip() not in base_detail:
        base_detail = f"{base_detail}；{attached_detail}" if base_detail else str(attached_detail)
    detail = redact_log_text(base_detail).strip()
    if not detail:
        detail = type(error).__name__
    return f"{prefix}（{type(error).__name__}）：{detail}"[:4000]


def _provider_error_fields(error: BaseException, default_stage: str) -> dict[str, str]:
    """为阶段事件补充安全的阶段和错误详情。"""

    query_stage = getattr(error, "query_stage", None)
    safe_detail = getattr(error, "safe_detail", "")
    if not safe_detail:
        safe_detail = str(error)
    fields = {
        "stage": redact_log_text(default_stage),
        "safe_detail": redact_log_text(safe_detail),
    }
    if query_stage:
        fields["query_stage"] = redact_log_text(query_stage)
    exit_code = getattr(error, "exit_code", None)
    if exit_code is not None:
        fields["exit_code"] = str(exit_code)
    return fields
