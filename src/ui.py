"""WO-09 desktop UI with the frozen card layout and guarded actions."""

from __future__ import annotations

import json
import sys
import os
import queue
import re
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Mapping

from .audit_logger import AuditLogError, AuditLogger
from .config import AppPaths, load_paper_standard_catalog_path
from .engineer_service import EngineerConfigError, load_engineers
from .generation_service import GenerationBatch, GenerationError, GenerationEvent, GenerationService
from .entry_api import (
    EntryApiError,
    EntryDraftApiClient,
    EntryDraftResubmissionService,
    EntryDraftSaveResult,
    Qa1EntryDraftOrchestrator,
)
from .manual_confirmation import (
    FuzzyTemplateCandidate,
    ManualConfirmationDecision,
    ManualConfirmationRow,
    validate_confirmation_choices,
    write_confirmation_draft,
)
from .paper_standard_exclusion import (
    MATCH_EXCLUDED,
    MATCH_REVIEW_REQUIRED,
    MATCH_UNDETERMINED,
    PaperStandardCatalog,
    PaperStandardMatch,
    read_paper_standard_workbook,
)
from .import_orchestrator import BatchImportResult, ImportOrchestrator
from .inspection_adjustment import (
    INSPECTION_ADJUSTMENT_LABEL,
    INSPECTION_ADJUSTMENT_MODULE,
    InspectionAdjustmentPage,
)
from .source_excel import parse_order_numbers, read_order_tasks
from .models import Engineer, OrderTask
from .pqc_query import PqcReadonlyQueryService
from .pqc_ui import PqcPage
from .processend_api import ProcessEndDraftApiClient
from .ui_preferences import UiPreferenceStore
from .ui_state import UiBatchState, UiOrder, UiOrderStatus, UiStateError


BG = "#F4F8FD"
CARD = "#FFFFFF"
WHITE = "#FFFFFF"
BORDER = "#DCE6F2"
TEXT = "#1F2937"
MUTED = "#64748B"
PRIMARY = "#2563EB"
PRIMARY_DARK = "#1D4ED8"
SUCCESS = "#16A34A"
SUCCESS_BG = "#EAF8EF"
WARNING = "#D97706"
WARNING_BG = "#FFF8E7"
DANGER = "#DC2626"
DANGER_BG = "#FFF0F0"
INFO_BG = "#EFF6FF"


_STATUS_META: dict[UiOrderStatus, tuple[str, str, str, str]] = {
    UiOrderStatus.WAITING: ("待处理", MUTED, "#F1F5F9", "muted"),
    UiOrderStatus.READ_SUCCESS: ("读取成功", PRIMARY, INFO_BG, "info"),
    UiOrderStatus.QUERY_SUCCESS: ("查询成功", PRIMARY, INFO_BG, "info"),
    UiOrderStatus.WINDCHILL_SUCCESS: ("Windchill 成功", SUCCESS, SUCCESS_BG, "success"),
    UiOrderStatus.GENERATED_CONFIRM: ("待人工确认", WARNING, WARNING_BG, "warning"),
    UiOrderStatus.IMPORTING: ("导入中", PRIMARY, INFO_BG, "info"),
    UiOrderStatus.COMMITTED: ("已提交", SUCCESS, SUCCESS_BG, "success"),
    UiOrderStatus.ROLLED_BACK_RETRYABLE: ("已回滚，可重导", WARNING, WARNING_BG, "warning"),
    UiOrderStatus.ERROR_RETRYABLE: ("异常，可重试", DANGER, DANGER_BG, "error"),
    UiOrderStatus.CANCELLED: ("已取消", MUTED, "#F1F5F9", "muted"),
    UiOrderStatus.ERROR_SKIPPED: ("无记录，已跳过", DANGER, DANGER_BG, "error"),
    UiOrderStatus.BLOCKED: ("已阻断", DANGER, DANGER_BG, "error"),
    UiOrderStatus.PAPER_ONLY: ("纸质记录/不导入", MUTED, "#F1F5F9", "muted"),
}

_STATUS_ICONS = {
    UiOrderStatus.WAITING: "○",
    UiOrderStatus.READ_SUCCESS: "●",
    UiOrderStatus.QUERY_SUCCESS: "●",
    UiOrderStatus.WINDCHILL_SUCCESS: "✓",
    UiOrderStatus.GENERATED_CONFIRM: "◷",
    UiOrderStatus.IMPORTING: "↻",
    UiOrderStatus.COMMITTED: "✓",
    UiOrderStatus.ROLLED_BACK_RETRYABLE: "↶",
    UiOrderStatus.ERROR_RETRYABLE: "✕",
    UiOrderStatus.CANCELLED: "○",
    UiOrderStatus.ERROR_SKIPPED: "✕",
    UiOrderStatus.BLOCKED: "✕",
    UiOrderStatus.PAPER_ONLY: "○",
}

_EVENT_LABELS = {
    "engineer_config_load_start": "工程师配置读取开始",
    "engineer_config_load_result": "工程师配置读取结果",
    "source_read_start": "读取开始",
    "source_read_result": "读取结果",
    "generation_start": "生成开始",
    "provider_preflight": "生成依赖预检",
    "provider_prepare_start": "批次依赖解析开始",
    "provider_prepare_success": "批次依赖解析成功",
    "provider_prepare_error": "批次依赖解析失败",
    "readonly_query_start": "只读查询开始",
    "readonly_query_success": "只读查询成功",
    "readonly_query_error": "只读查询失败",
    "windchill_query_start": "Windchill 查询开始",
    "windchill_query_success": "Windchill 查询成功",
    "windchill_query_error": "Windchill 查询失败",
    "paper_catalog_load": "线下清单读取",
    "paper_standard_exclusion": "纸质规范排除",
    "paper_standard_batch_only": "纸质批次排除",
    "paper_only_order": "纸质单号",
    "model_match": "检查单模型匹配",
    "sampling_decision": "允收水准判定",
    "generation_resolve_start": "单号解析开始",
    "generation_resolve_success": "单号解析成功",
    "generation_resolve_error": "单号解析失败",
    "artifact_write_start": "产物写出开始",
    "artifact_write_success": "产物写出成功",
    "artifact_write_error": "产物写出失败",
    "source_read": "读取文件",
    "artifact_ready": "生成完成",
    "manual_confirm": "人工确认",
    "retry_requested": "申请重导",
    "import_start": "开始导入",
    "import_result": "导入结果",
    "duplicate_import_confirmation": "重复导入确认",
    "import_blocked": "导入阻断",
    "import_gate_error": "导入门阻断",
    "import_worker_error": "导入异常",
    "rollback_blocked": "回滚阻断",
    "cancel_requested": "取消请求",
    "open_output_dir": "打开输出目录",
    "open_log_error": "打开日志失败",
    "view_log": "查看日志",
    "read_error": "读取失败",
    "generation_blocked": "生成阻断",
    "manual_confirmation_required": "待人工确认模板",
    "manual_confirmation_result": "人工模板确认结果",
    "qa1_confirmation_required": "QA1 候选确认",
    "qa1_confirmation_result": "QA1 候选确认结果",
    "qa1_popup_gate": "QA1 弹窗门禁",
    "qa1_popup_diagnostic": "QA1 弹窗诊断",
    "qa1_submission_blocked": "QA1 草稿提交阻断",
    "entry_submit_start": "Entry 草稿保存开始",
    "entry_submit_result": "Entry 草稿保存结果",
    "state_error": "状态错误",
    "ui_reset": "界面重置",
    "ui_reset_blocked": "界面重置阻断",
    "pqc_project_query_start": "PQC 项目查询开始",
    "pqc_project_query_result": "PQC 项目查询结果",
    "pqc_excel_row_excluded": "PQC Excel 行排除",
    "pqc_candidate_load_start": "PQC 候选加载开始",
    "pqc_candidate_load_result": "PQC 候选加载结果",
    "pqc_selection_blocked": "PQC 选择阻断",
    "pqc_tools_saved": "PQC 工器具保存",
    "pqc_tools_copied": "PQC 工器具复制",
    "pqc_create_start": "PQC 草稿创建开始",
    "pqc_create_item_result": "PQC 单份创建结果",
    "pqc_create_result": "PQC 草稿创建结果",
    "pqc_ui_reset": "PQC 界面重置",
    "inspection_adjustment_read_start": "送检信息调整读取开始",
    "inspection_adjustment_read_result": "送检信息调整预览结果",
    "inspection_adjustment_sync_start": "送检信息调整同步开始",
    "inspection_adjustment_sync_result": "送检信息调整同步结果",
    "inspection_adjustment_error": "送检信息调整失败",
    "inspection_adjustment_preference_warning": "送检信息调整偏好提示",
    "ui_module_switch": "页面切换",
    "ui_preference_warning": "页面偏好提示",
}

_MODULE_LABELS = {
    "IQC": "IQC 进货检验",
    "PQC": "PQC 过程检验",
    INSPECTION_ADJUSTMENT_MODULE: INSPECTION_ADJUSTMENT_LABEL,
}

# 审计 JSONL 保留完整的技术字段，界面日志只显示业务可读名称；这样现场
# 不会在普通提示中看到 procedure_code、matched_by 等内部匹配实现字段。
_UI_LOG_FIELD_LABELS = {
    "item_code": "物项编码",
    "legacy_item_code": "旧编码",
    "model_id": "模板编号",
    "candidate_model_ids": "候选模板编号",
    "unique_candidate_model_id": "唯一候选模板编号",
    "candidate_names": "候选模板",
    "candidate_count": "候选数量",
    "candidate_row_count": "候选行数",
    "item_code_count": "物项数量",
    "unresolved_count": "待确认行数",
    "qa1_candidate_count": "关键零部件候选行数",
    "available_count": "可用人数",
    "draft_filename": "确认草稿",
    "runtime_entry": "运行入口",
    "item_name": "物料描述",
    "quantity": "进货数量",
    "check_number": "计算抽检数量",
    "accept_standard": "允收状态",
    "document_code": "规范编码",
    "exclude_key": "排除键",
    "catalog_sheet": "清单工作表",
    "catalog_row": "清单行号",
    "paper_match_status": "纸质判定",
}
_UI_LOG_HIDDEN_FIELDS = frozenset({
    "procedure_code", "matched_by", "basis", "preflight_basis",
    "schema", "reason", "item_has_qa1", "quantity_input", "quantity_input_type",
    "check_number_reason", "acceptance_reason",
    "model_check_plan", "model_accept_standard", "parsed_accept_standard",
    "selected_aql", "sampling_code", "source_sheet", "source_rows", "trigger_basis",
    "module", "type_code",
})


@dataclass(frozen=True)
class ReadGenerateResult:
    """后台读取与生成的组合结果，避免 UI 主线程重复读取源文件。"""

    tasks: Mapping[str, OrderTask]
    generation: GenerationBatch | None
    events: tuple[GenerationEvent, ...] = ()
    engineer: Engineer | None = None


@dataclass(frozen=True)
class EntryOrderResult:
    """统一 Entry 保存处理器的逐单结果，不改变 BAT 导入结果对象。"""

    order_number: str
    accepted: bool
    detail: str
    draft_ids: tuple[tuple[str, str], ...] = ()


def status_meta(status: UiOrderStatus) -> tuple[str, str, str, str]:
    """返回状态徽章文本、前景色、背景色和行标签。"""

    return _STATUS_META.get(status, (status.value, MUTED, "#F1F5F9", "muted"))


def status_badge_text(status: UiOrderStatus) -> str:
    """把状态文本渲染为带图标的徽章文案，保留状态机原值不变。"""

    badge, _fg, _bg, _tag = status_meta(status)
    return f"{_STATUS_ICONS.get(status, '●')}  {badge}"


def log_level(event: str, status: str | None = None, detail: str = "") -> str:
    """把审计事件映射到界面日志颜色，不改变 JSONL 原始记录。"""

    text = f"{event} {status or ''} {detail}".upper()
    if any(token in text for token in ("ERROR", "BLOCK", "失败", "异常", "阻断")):
        return "error"
    if any(token in text for token in ("SUCCESS", "COMMITTED", "成功", "已提交", "通过")):
        return "success"
    if any(token in text for token in ("WARNING", "SKIP", "ROLLBACK", "CANCEL", "跳过", "回滚", "取消")):
        return "warning"
    return "info"


_SENSITIVE_ERROR_RE = re.compile(
    r"(?i)(password|passwd|token|authorization|cookie|secret)(\s*[=:]\s*)[^\s;,]+"
)


def _safe_entry_error_text(error: BaseException) -> str:
    """保留 Entry 原始异常摘要，同时避免把凭据写入审计日志。"""

    text = " ".join(str(error).split()) or type(error).__name__
    text = _SENSITIVE_ERROR_RE.sub(r"\1\2<redacted>", text)
    return text[:500] + ("…" if len(text) > 500 else "")


def _maximize_window(window: tk.Misc) -> None:
    """请求 Tk 窗口以最大化状态打开，并兼容不支持该状态的 Tk 平台。"""

    try:
        window.state("zoomed")
    except (tk.TclError, AttributeError):
        # Windows Tk 支持 ``state('zoomed')``；其他平台/测试替身可能不支持，
        # 此时尝试窗口属性，仍不支持则保留 geometry 的安全回退尺寸。
        try:
            window.attributes("-zoomed", True)
        except (tk.TclError, AttributeError):
            pass


def _size_window_90_percent(window: tk.Misc) -> None:
    """把人工确认窗口放大到屏幕约 90%，并居中显示。

    ``state('zoomed')`` 在 Windows 上会覆盖窗口尺寸并占满整个工作区，
    不符合人工确认界面需要保留边界和任务栏可见的 90% 布局。测试替身或
    非 Tk 平台无法取得屏幕尺寸时，回退到原有最大化 helper。
    """

    try:
        screen_width = int(window.winfo_screenwidth())
        screen_height = int(window.winfo_screenheight())
        if screen_width <= 0 or screen_height <= 0:
            raise ValueError("invalid screen dimensions")
        width = min(screen_width, max(920, int(screen_width * 0.9)))
        height = min(screen_height, max(480, int(screen_height * 0.9)))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")
    except (tk.TclError, AttributeError, TypeError, ValueError):
        _maximize_window(window)


class IqcApp(tk.Tk):
    """截图冻结版单窗口 UI；耗时读取/导入继续通过后台线程执行。"""

    def __init__(
        self,
        paths: AppPaths,
        importer: ImportOrchestrator | None = None,
        logger: AuditLogger | None = None,
        generator: GenerationService | None = None,
        entry_client: EntryDraftApiClient | None = None,
        pqc_query_service: PqcReadonlyQueryService | None = None,
        pqc_entry_client: ProcessEndDraftApiClient | None = None,
        preference_store: UiPreferenceStore | None = None,
        inspection_adjustment_service: object | None = None,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.importer = importer
        self.generator = generator
        # API 客户端必须由调用方显式注入（例如现场配置了入口地址时由
        # main.py 构造）。未注入时保留原有 BAT 导入流程，不猜测 API 地址。
        self.entry_client = entry_client
        self.pqc_query_service = pqc_query_service
        self.pqc_entry_client = pqc_entry_client
        self.inspection_adjustment_service = inspection_adjustment_service
        self._preference_store = preference_store or UiPreferenceStore.for_current_user()
        preference = self._preference_store.load()
        self._preference = preference
        self._active_module = preference.module
        self._preference_warning = preference.warning
        self.state = UiBatchState()
        self._worker: threading.Thread | None = None
        self._worker_queue: queue.Queue[tuple[str, object, BaseException | None]] = queue.Queue()
        self._cancel_event = threading.Event()
        self._ui_busy_flag = False
        self._active_import_orders: tuple[str, ...] = ()
        self._tree_orders: dict[str, str] = {}
        self._tree_links: dict[str, dict[str, Path | None]] = {}
        self._close_requested = False
        self._pending_confirmation_rows: tuple[ManualConfirmationRow, ...] = ()
        self._pending_confirmation_tasks: Mapping[str, OrderTask] | None = None
        self._pending_confirmation_engineer: Engineer | None = None
        self._pending_confirmation_path: Path | None = None
        self._paper_catalog: PaperStandardCatalog | None = None
        # 本批次已生成的解析对象，供“提交”按钮在同一 UI 状态机中组装
        # 普通草稿及 QA1 附加草稿；不重新读取 Excel 或重做聚合。
        self._generated_outcomes: dict[str, object] = {}
        # QA1 候选由同一套人工确认表展示。键为 order::item，值为明确的
        # model_id；False 表示该行主动取消/放弃，缺失表示尚未确认。
        self._qa1_choices: dict[str, object] = {}
        self._entry_draft_ids: dict[str, str] = {}
        # 人工确认恢复任务的生命周期独立于候选 JSON 文件；重置界面只清掉
        # 当前内存引用，不删除该审计文件，且 busy 时绝不清理。
        self._manual_resume_started = False
        self._engineer_display_names: list[str] = []
        self._engineers_by_display: dict[str, Engineer] = {}
        # _drain_worker_queue 在回调期间置位，避免 worker 已结束但回调尚未
        # 处理时被误判为空闲，导致重置入口清掉即将回写的当前批次。
        self._callback_in_progress = False
        try:
            self.logger = logger or AuditLogger(paths.log_dir / f"iqc_ui_{date.today().isoformat()}.jsonl")
        except AuditLogError as exc:
            raise RuntimeError(f"无法初始化 WO-09 脱敏日志：{exc}") from exc

        self._app_version = self._read_app_version(paths.project_root)
        self.title(f"质量检验草稿工具 · 版次 {self._app_version}")
        self.geometry("1680x940")
        self.minsize(1180, 760)
        self.configure(bg=BG)
        self.source_var = tk.StringVar(value=preference.last_iqc_source_path)
        self.pqc_source_var = tk.StringVar(value=preference.last_pqc_source_path)
        self.paper_source_var = tk.StringVar(
            value=preference.last_paper_standard_catalog_path or self._load_default_paper_source(paths)
        )
        self.orders_var = tk.StringVar()
        self.engineer_var = tk.StringVar(value=preference.last_engineer_name or "请选择预配置人员")
        self.status_var = tk.StringVar(value="就绪：请选择源 Excel 和预配置工程师")
        self.detail_var = tk.StringVar(value="请选择单号查看详情")
        # 预先勾选只表示界面默认状态；真正导入前仍会经过选中单号、产物和
        # ImportOrchestrator 的确认/授权门。导入完成后在 _import_finished 中清除。
        self.manual_confirm_var = tk.BooleanVar(value=True)
        self.skip_errors_var = tk.BooleanVar(value=True)
        self._build()
        self._load_engineers()
        if self._preference_warning:
            self._append_log(
                "ui_preference_warning",
                status="WARNING",
                detail=self._preference_warning,
                module="IQC",
            )
        self.manual_confirm_var.trace_add("write", lambda *_args: self._update_action_states())
        self.skip_errors_var.trace_add("write", lambda *_args: self._update_action_states())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_worker_queue)

    def _font(self, size: int, weight: str = "normal") -> tuple[str, int, str]:
        return ("Microsoft YaHei UI", size, weight)

    @staticmethod
    def _read_app_version(project_root: Path) -> str:
        """读取包内版次；源码运行时没有 VERSION 则显示源码版。"""

        try:
            value = (Path(project_root) / "VERSION").read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError):
            value = ""
        return value or "源码版"

    @staticmethod
    def _load_default_paper_source(paths: AppPaths) -> str:
        """从 launcher.json 读取线下清单默认路径；读取失败时保持空输入。"""

        return load_paper_standard_catalog_path(Path(paths.project_root) / "config" / "launcher.json")

    def _card(self, parent: tk.Misc, *, padx: int = 14, pady: int = 14) -> tk.Frame:
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightcolor=BORDER, highlightthickness=1, bd=0)
        card.grid_columnconfigure(0, weight=1)
        card._content_padding = (padx, pady)  # type: ignore[attr-defined]
        return card

    def _button(self, parent: tk.Misc, text: str, command: Callable[[], None], *, kind: str = "outline", width: int | None = None) -> tk.Button:
        if kind == "primary":
            options = dict(bg=PRIMARY, fg=WHITE, activebackground=PRIMARY_DARK, activeforeground=WHITE, disabledforeground="#A8C0EF")
        elif kind == "success":
            options = dict(bg=SUCCESS, fg=WHITE, activebackground="#15803D", activeforeground=WHITE, disabledforeground="#A9D9B7")
        elif kind == "danger":
            options = dict(bg=WHITE, fg=DANGER, activebackground=DANGER_BG, activeforeground=DANGER, disabledforeground="#E7A5A5")
        else:
            options = dict(bg=WHITE, fg=PRIMARY, activebackground=INFO_BG, activeforeground=PRIMARY_DARK, disabledforeground="#A8B9D1")
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=self._font(11, "bold"),
            relief=tk.FLAT,
            bd=0,
            padx=12,
            pady=9,
            cursor="hand2",
            highlightthickness=1,
            highlightbackground=(DANGER if kind == "danger" else (SUCCESS if kind == "success" else PRIMARY)),
            highlightcolor=(DANGER if kind == "danger" else (SUCCESS if kind == "success" else PRIMARY)),
            **options,
        )

    def _label(self, parent: tk.Misc, text: str, *, size: int = 11, weight: str = "normal", color: str = TEXT, **grid: object) -> tk.Label:
        label = tk.Label(parent, text=text, font=self._font(size, weight), fg=color, bg=parent.cget("bg"))
        if grid:
            label.grid(**grid)
        return label

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = tk.Frame(self, bg=WHITE, height=68, highlightbackground="#D7E2F0", highlightthickness=1)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.columnconfigure(1, weight=1)
        self._label(header, f"质量检验草稿工具 · 版次 {self._app_version}", size=17, weight="bold", color="#111827", row=0, column=0, padx=28, pady=16, sticky="w")
        self.header_status = self._label(header, "", size=10, color=MUTED, row=0, column=1, padx=28, pady=16, sticky="e")

        self._build_module_tabs()

        module_host = tk.Frame(self, bg=BG)
        module_host.grid(row=2, column=0, sticky="nsew", padx=12, pady=(8, 8))
        module_host.columnconfigure(0, weight=1)
        module_host.rowconfigure(0, weight=1)

        self.iqc_page = tk.Frame(module_host, bg=BG)
        self.iqc_page.grid(row=0, column=0, sticky="nsew")
        self.iqc_page.columnconfigure(0, minsize=460, weight=0)
        self.iqc_page.columnconfigure(1, minsize=520, weight=1)
        self.iqc_page.columnconfigure(2, minsize=400, weight=0)
        self.iqc_page.rowconfigure(0, weight=1)

        self._build_input_card(self.iqc_page)
        self._build_results_card(self.iqc_page)
        self._build_actions_column(self.iqc_page)
        self.pqc_page = PqcPage(
            module_host,
            owner=self,
            output_dir=self.paths.output_dir,
            query_service=self.pqc_query_service,
            api_client=self.pqc_entry_client,
            preference_store=self._preference_store,
        )
        self.pqc_page.grid(row=0, column=0, sticky="nsew")
        self.inspection_adjustment_page = InspectionAdjustmentPage(
            module_host,
            owner=self,
            service=self.inspection_adjustment_service,
            preference_store=self._preference_store,
            preference=self._preference,
        )
        self.inspection_adjustment_page.grid(row=0, column=0, sticky="nsew")
        self._build_log_card()
        self._show_module(self._active_module, persist=False, announce=False)
        self._update_action_states()

    def _build_module_tabs(self) -> None:
        tabs = tk.Frame(self, bg=WHITE, height=46, highlightbackground="#D7E2F0", highlightthickness=1)
        tabs.grid(row=1, column=0, sticky="ew")
        tabs.grid_propagate(False)
        tabs.columnconfigure(3, weight=1)
        tab_options = dict(
            font=self._font(11, "bold"),
            relief=tk.FLAT,
            bd=0,
            padx=28,
            pady=11,
            cursor="hand2",
            activebackground="#EFF6FF",
            activeforeground=PRIMARY_DARK,
            disabledforeground="#A8B9D1",
        )
        self.iqc_tab_button = tk.Button(
            tabs,
            text="IQC 进货检验",
            command=lambda: self._switch_module("IQC"),
            **tab_options,
        )
        self.iqc_tab_button.grid(row=0, column=0, sticky="ns")
        self.pqc_tab_button = tk.Button(
            tabs,
            text="PQC 过程检验",
            command=lambda: self._switch_module("PQC"),
            **tab_options,
        )
        self.pqc_tab_button.grid(row=0, column=1, sticky="ns")
        self.inspection_adjustment_tab_button = tk.Button(
            tabs,
            text=INSPECTION_ADJUSTMENT_LABEL,
            command=lambda: self._switch_module(INSPECTION_ADJUSTMENT_MODULE),
            **tab_options,
        )
        self.inspection_adjustment_tab_button.grid(row=0, column=2, sticky="ns")
        self.module_memory_var = tk.StringVar()
        tk.Label(
            tabs,
            textvariable=self.module_memory_var,
            font=self._font(10),
            fg=MUTED,
            bg=WHITE,
            anchor="e",
        ).grid(row=0, column=3, sticky="e", padx=28)

    def _refresh_module_tabs(self) -> None:
        active = self.__dict__.get("_active_module", "IQC")
        for module, button in (
            ("IQC", self.__dict__.get("iqc_tab_button")),
            ("PQC", self.__dict__.get("pqc_tab_button")),
            (INSPECTION_ADJUSTMENT_MODULE, self.__dict__.get("inspection_adjustment_tab_button")),
        ):
            if button is None:
                continue
            selected = module == active
            button.configure(
                bg="#EFF6FF" if selected else WHITE,
                fg=PRIMARY if selected else MUTED,
                highlightthickness=2 if selected else 0,
                highlightbackground=PRIMARY,
                highlightcolor=PRIMARY,
            )
        self.module_memory_var.set(
            f"✓ 已记住当前页，下次启动打开 {_MODULE_LABELS.get(active, active)}"
        )

    def _show_module(self, module: object, *, persist: bool, announce: bool) -> bool:
        value = str(module).strip().upper()
        if value not in {"IQC", "PQC", INSPECTION_ADJUSTMENT_MODULE}:
            value = "IQC"
        if value == INSPECTION_ADJUSTMENT_MODULE and "inspection_adjustment_page" not in self.__dict__:
            value = "IQC"
        self._active_module = value
        for page_name in ("iqc_page", "pqc_page", "inspection_adjustment_page"):
            page = self.__dict__.get(page_name)
            if page is not None:
                page.grid_remove()
        if value == "PQC":
            self.pqc_page.grid()
            self.pqc_page.on_show()
        elif value == INSPECTION_ADJUSTMENT_MODULE:
            self.inspection_adjustment_page.grid()
            self.inspection_adjustment_page.on_show()
        else:
            self.iqc_page.grid()
        self._refresh_module_tabs()
        if persist:
            saved, warning = self._preference_store.save(value)
            if not saved:
                self.module_memory_var.set(
                    f"当前页：{_MODULE_LABELS.get(value, value)}；本机页面偏好未保存"
                )
                self._append_log(
                    "ui_preference_warning",
                    status="WARNING",
                    detail=warning or "页面偏好无法保存",
                    module=value,
                )
        if announce:
            self._append_log(
                "ui_module_switch",
                status="SUCCESS",
                detail=f"已切换到 {_MODULE_LABELS.get(value, value)} 页面",
                module=value,
            )
        return True

    def _switch_module(self, module: object) -> bool:
        value = str(module).strip().upper()
        if value not in {"IQC", "PQC", INSPECTION_ADJUSTMENT_MODULE}:
            return False
        if self._busy():
            self._append_log(
                "ui_module_switch",
                status="BLOCKED",
                detail="当前正在读取、查询或创建，页面切换已禁用",
                module=self.__dict__.get("_active_module", "IQC"),
            )
            return False
        if value == self.__dict__.get("_active_module", "IQC"):
            return True
        return self._show_module(value, persist=True, announce=True)

    def _build_input_card(self, parent: tk.Misc) -> None:
        card = self._card(parent, pady=10)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        padx, pady = card._content_padding  # type: ignore[attr-defined]
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill=tk.BOTH, expand=True, padx=padx, pady=pady)
        inner.columnconfigure(0, weight=1)
        self._label(inner, "源 Excel 文件", size=12, weight="bold", row=0, column=0, sticky="w", pady=(0, 4))
        path_row = tk.Frame(inner, bg=CARD)
        path_row.grid(row=1, column=0, sticky="ew")
        path_row.columnconfigure(0, weight=1)
        self.source_entry = tk.Entry(path_row, textvariable=self.source_var, font=self._font(11), fg=TEXT, bg="#FBFDFF", relief=tk.FLAT, highlightthickness=1, highlightbackground="#C9D7E8", highlightcolor=PRIMARY)
        self.source_entry.grid(row=0, column=0, sticky="ew", ipady=9)
        self._button(path_row, "选择文件", self._choose_source).grid(row=0, column=1, padx=(14, 0), sticky="ew")

        self._label(inner, "线下规范清单", size=12, weight="bold", row=2, column=0, sticky="w", pady=(12, 6))
        paper_path_row = tk.Frame(inner, bg=CARD)
        paper_path_row.grid(row=3, column=0, sticky="ew")
        paper_path_row.columnconfigure(0, weight=1)
        self.paper_source_entry = tk.Entry(paper_path_row, textvariable=self.paper_source_var, font=self._font(11), fg=TEXT, bg="#FBFDFF", relief=tk.FLAT, highlightthickness=1, highlightbackground="#C9D7E8", highlightcolor=PRIMARY)
        self.paper_source_entry.grid(row=0, column=0, sticky="ew", ipady=9)
        self._button(paper_path_row, "选择线下清单", self._choose_paper_source).grid(row=0, column=1, padx=(14, 0), sticky="ew")

        order_header = tk.Frame(inner, bg=CARD)
        order_header.grid(row=4, column=0, sticky="ew", pady=(14, 4))
        order_header.columnconfigure(0, weight=1)
        self._label(order_header, "送检单号（多个请用 ; 分隔）", size=12, weight="bold", row=0, column=0, sticky="w")
        self._button(order_header, "清空", self._clear_orders).grid(row=0, column=1, sticky="e")
        # 固定多行输入框的字符宽度，避免 Tk 默认 80 列把左卡片撑得过宽，
        # 使三栏比例稳定落在截图冻结规范的约 28% / 47% / 25%。
        self.orders_text = tk.Text(inner, width=40, height=4, font=self._font(11), fg=TEXT, bg="#FBFDFF", relief=tk.FLAT, highlightthickness=1, highlightbackground="#C9D7E8", highlightcolor=PRIMARY, padx=10, pady=8, wrap=tk.WORD)
        self.orders_text.grid(row=5, column=0, sticky="ew")
        self.orders_text.bind("<KeyRelease>", lambda _event: self.orders_var.set(self.orders_text.get("1.0", tk.END).strip()))

        self._label(inner, "质检工程师", size=12, weight="bold", row=6, column=0, sticky="w", pady=(14, 8))
        combo_style = ttk.Style(self)
        try:
            combo_style.theme_use("clam")
        except tk.TclError:
            pass
        combo_style.configure("Modern.TCombobox", fieldbackground=WHITE, background=WHITE, foreground=TEXT, bordercolor="#C9D7E8", lightcolor="#C9D7E8", darkcolor="#C9D7E8", padding=8)
        self.engineer_box = ttk.Combobox(inner, textvariable=self.engineer_var, state="readonly", style="Modern.TCombobox", font=self._font(11))
        self.engineer_box.grid(row=7, column=0, sticky="ew")
        self.engineer_box.bind("<<ComboboxSelected>>", self._on_engineer_selected)

        self.read_button = self._button(inner, "▤  读取并生成", self._read_source, kind="primary")
        self.read_button.grid(row=8, column=0, sticky="ew", pady=(14, 8))
        utility_row = tk.Frame(inner, bg=CARD)
        utility_row.grid(row=9, column=0, sticky="ew")
        utility_row.columnconfigure(0, weight=1)
        utility_row.columnconfigure(1, weight=1)
        utility_row.columnconfigure(2, weight=1)
        self._button(utility_row, "▱  打开输出目录", self._open_output_dir).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        # “重置界面”与“清空日志”严格分开：前者仅清理当前批次/输入控件，
        # 后者仍由底部日志卡片单独执行，避免用户为解除 UI 卡顿而误删审计。
        self.reset_button = self._button(utility_row, "↺  重置界面", self._reset_ui)
        self.reset_button.grid(row=0, column=1, sticky="ew", padx=6)
        self._button(utility_row, "▤  查看日志", self._view_log).grid(row=0, column=2, sticky="ew", padx=(6, 0))

    def _build_results_card(self, parent: tk.Misc) -> None:
        card = self._card(parent, padx=14, pady=14)
        card.grid(row=0, column=1, sticky="nsew", padx=(0, 8))
        card.rowconfigure(0, weight=1)
        card.columnconfigure(0, weight=1)
        columns = ("index", "order", "rows", "result", "sql", "status")
        self.tree = ttk.Treeview(card, columns=columns, show="headings", selectmode="extended", style="Modern.Treeview")
        style = ttk.Style(self)
        style.configure("Modern.Treeview", background=WHITE, fieldbackground=WHITE, foreground=TEXT, rowheight=46, borderwidth=0, font=self._font(10))
        style.configure("Modern.Treeview.Heading", background="#F6F9FD", foreground=TEXT, relief=tk.FLAT, font=self._font(10, "bold"), padding=(8, 12))
        style.map("Modern.Treeview", background=[("selected", "#E8F1FF")], foreground=[("selected", TEXT)])
        headings = (
            ("index", "", 42),
            ("order", "送检单号", 124),
            ("rows", "命中行数", 88),
            ("result", "结果文件", 190),
            ("sql", "SQL 文件", 170),
            ("status", "状态", 150),
        )
        for column, heading, width in headings:
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, anchor=tk.W, stretch=column in {"result", "sql", "status"})
        self.tree.column("index", anchor=tk.CENTER, stretch=False)
        scrollbar = ttk.Scrollbar(card, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._open_link)
        for tag, color, background in (("success", SUCCESS, SUCCESS_BG), ("warning", WARNING, WARNING_BG), ("error", DANGER, DANGER_BG), ("info", PRIMARY, WHITE), ("muted", MUTED, WHITE)):
            self.tree.tag_configure(tag, foreground=color, background=background)

    def _build_actions_column(self, parent: tk.Misc) -> None:
        column = tk.Frame(parent, bg=BG)
        column.grid(row=0, column=2, sticky="nsew")
        column.columnconfigure(0, weight=1)

        import_card = self._card(column, padx=18, pady=12)
        import_card.grid(row=0, column=0, sticky="new")
        inner = tk.Frame(import_card, bg=CARD)
        inner.pack(fill=tk.BOTH, expand=True)
        self._label(inner, "导入选中单号", size=16, weight="bold", color=PRIMARY, row=0, column=0, sticky="w")
        notice = tk.Frame(inner, bg=WARNING_BG, highlightbackground="#F2C46F", highlightthickness=1)
        notice.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        notice.columnconfigure(1, weight=1)
        self._label(notice, "ⓘ", size=14, weight="bold", color=WARNING, row=0, column=0, padx=12, pady=10, sticky="n")
        import_notice = (
            "请先人工确认结果；已配置 Entry API 时，普通与 QA1 草稿统一保存。"
            if self.__dict__.get("entry_client") is not None
            else "请先人工确认结果文件内容无误后再导入。"
        )
        self._label(notice, import_notice, size=10, color=TEXT, row=0, column=1, padx=(0, 12), pady=12, sticky="w")
        self.manual_check = tk.Checkbutton(inner, text="我已人工确认以上选中单号的结果文件内容无误", variable=self.manual_confirm_var, font=self._font(10), fg=TEXT, bg=CARD, activebackground=CARD, selectcolor=WHITE, anchor="w", justify=tk.LEFT, wraplength=330)
        self.manual_check.grid(row=2, column=0, sticky="w", pady=(0, 14))
        self.import_button = self._button(inner, "☁  导入选中单号", self._import_selected, kind="success")
        self.import_button.grid(row=3, column=0, sticky="ew")
        tk.Frame(inner, bg=BORDER, height=1).grid(row=4, column=0, sticky="ew", pady=(14, 10))
        self.skip_check = tk.Checkbutton(inner, text="异常单跳过，其他单继续", variable=self.skip_errors_var, font=self._font(10), fg=TEXT, bg=CARD, activebackground=CARD, selectcolor=PRIMARY, anchor="w")
        self.skip_check.grid(row=5, column=0, sticky="w")
        self._label(inner, "ⓘ", size=10, color=MUTED, row=5, column=0, padx=(240, 0), sticky="e")

        tx_card = self._card(column, padx=18, pady=12)
        tx_card.grid(row=1, column=0, sticky="new", pady=(14, 0))
        tx_inner = tk.Frame(tx_card, bg=CARD)
        tx_inner.pack(fill=tk.BOTH, expand=True)
        self._label(tx_inner, "逐单提交 / 回滚", size=16, weight="bold", color=PRIMARY, row=0, column=0, sticky="w")
        submit_notice = (
            "“提交”复用普通检查单保存处理器；已确认的 QA1 候选作为附加分支保存。"
            if self.__dict__.get("entry_client") is not None
            else "“导入”和“提交”当前共用同一条导入 BAT，区别仅为界面动作名称。"
        )
        self._label(tx_inner, submit_notice, size=9, color=MUTED, row=1, column=0, sticky="w", pady=(6, 0))
        tx_row = tk.Frame(tx_inner, bg=CARD)
        tx_row.grid(row=2, column=0, sticky="ew", pady=(18, 0))
        tx_row.columnconfigure(0, weight=1)
        tx_row.columnconfigure(1, weight=1)
        self.submit_button = self._button(tx_row, "●  提交选中单号", self._submit_selected, kind="success")
        self.submit_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.rollback_button = self._button(tx_row, "↶  回滚选中单号", self._rollback_selected, kind="danger")
        self.rollback_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _build_log_card(self) -> None:
        card = self._card(self, padx=14, pady=10)
        card.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 8))
        card.rowconfigure(1, weight=1)
        card.columnconfigure(0, weight=1)
        header = tk.Frame(card, bg=CARD)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self._label(header, "运行日志", size=13, weight="bold", row=0, column=0, sticky="w")
        self.clear_log_button = self._button(header, "▱  清空日志", self._clear_log_view)
        self.clear_log_button.grid(row=0, column=1, sticky="e")
        log_body = tk.Frame(card, bg="#FBFDFF", highlightbackground="#E4EBF4", highlightthickness=1)
        log_body.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        log_body.rowconfigure(0, weight=1)
        log_body.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_body, height=8, wrap=tk.NONE, state=tk.DISABLED, bg="#FBFDFF", fg=TEXT, relief=tk.FLAT, font=("Consolas", 10), padx=10, pady=8)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_body, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.tag_configure("info", foreground=PRIMARY)
        self.log_text.tag_configure("success", foreground=SUCCESS)
        self.log_text.tag_configure("warning", foreground=WARNING)
        self.log_text.tag_configure("error", foreground=DANGER)
        self._label(self, "就绪：请在当前页完成选择与检查；活动任务期间不能切换页面。", size=9, color=MUTED, row=4, column=0, padx=16, pady=(0, 6), sticky="w")

    def _load_engineers(self) -> None:
        config_path = Path(self.paths.engineer_config)
        config_exists = config_path.is_file()
        self._append_log(
            "engineer_config_load_start",
            status="STARTED",
            detail="开始读取预配置工程师",
            config_exists=config_exists,
        )
        try:
            engineers = load_engineers(config_path)
        except EngineerConfigError as exc:
            self._append_log(
                "engineer_config_load_result",
                status="ERROR",
                detail=str(exc),
                config_exists=config_exists,
                available_count=0,
                error_type=type(exc).__name__,
            )
            self.engineer_box["values"] = ("配置错误",)
            self.engineer_box.current(0)
            self.engineer_var.set("请选择预配置人员")
            pqc_page = self.__dict__.get("pqc_page")
            if pqc_page is not None:
                pqc_page.set_engineers(())
            self.status_var.set(str(exc))
            self.header_status.configure(text="工程师配置不可用")
            return
        self._engineer_display_names = [engineer.display_name for engineer in engineers]
        self._engineers_by_display = {engineer.display_name: engineer for engineer in engineers}
        self.engineer_box["values"] = ("请选择预配置人员", *self._engineer_display_names)
        preferred_engineer = self.engineer_var.get().strip()
        if preferred_engineer and preferred_engineer in self._engineers_by_display:
            self.engineer_box.current(self._engineer_display_names.index(preferred_engineer) + 1)
        else:
            self.engineer_box.current(0)
            self.engineer_var.set("请选择预配置人员")
        pqc_page = self.__dict__.get("pqc_page")
        if pqc_page is not None:
            pqc_page.set_engineers(engineers)
        self.header_status.configure(text=f"已加载 {len(engineers)} 名预配置工程师")
        self._append_log(
            "engineer_config_load_result",
            status="SUCCESS",
            detail="预配置工程师读取完成",
            config_exists=config_exists,
            available_count=len(engineers),
        )

    def _choose_source(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择源 Excel",
            filetypes=(
                ("Excel 工作簿 (*.xlsx)", "*.xlsx"),
                ("所有文件", "*.*"),
            ),
        )
        if selected:
            self.source_var.set(selected)
            self._save_source_path_preference()

    def _choose_paper_source(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择线下规范清单",
            filetypes=(
                ("Excel 工作簿 (*.xlsx;*.xlsm)", "*.xlsx;*.xlsm"),
                ("所有文件", "*.*"),
            ),
        )
        if selected:
            self.paper_source_var.set(selected)
            self._save_source_path_preference()

    def _on_engineer_selected(self, _event: object | None = None) -> None:
        self._save_source_path_preference()

    def _save_source_path_preference(self) -> None:
        self._preference_store.save_iqc_source_path(self.source_var.get().strip())
        self._preference_store.save_pqc_source_path(self.pqc_source_var.get().strip())
        self._preference_store.save_paper_standard_catalog_path(self.paper_source_var.get().strip())
        self._preference_store.save_engineer_name(self.engineer_var.get().strip())
        adjustment_page = self.__dict__.get("inspection_adjustment_page")
        save_adjustment_preferences = getattr(adjustment_page, "save_preferences", None)
        if callable(save_adjustment_preferences):
            save_adjustment_preferences()

    def _read_source(self) -> None:
        if self._busy():
            return
        self.orders_var.set(self.orders_text.get("1.0", tk.END).strip())
        path = Path(self.source_var.get().strip())
        paper_path = Path(self.paper_source_var.get().strip())
        orders = parse_order_numbers(self.orders_var.get())
        if not path.exists():
            messagebox.showerror("输入错误", "请选择存在的源 Excel 文件。")
            return
        if not paper_path.exists():
            messagebox.showerror("输入错误", "请选择存在的线下规范清单。")
            return
        if paper_path.suffix.lower() not in {".xlsx", ".xlsm"}:
            messagebox.showerror("输入错误", "线下规范清单仅支持 .xlsx/.xlsm。")
            return
        if not orders:
            messagebox.showerror("输入错误", "至少输入一个送检单号。")
            return
        if self.engineer_var.get() in {"", "请选择预配置人员", "配置错误"}:
            messagebox.showerror("输入错误", "请选择预配置质检工程师。")
            return
        self.status_var.set("正在读取源 Excel；窗口保持响应")
        self.header_status.configure(text="处理中…")
        self._set_busy(True)
        engineer = self._engineers_by_display.get(self.engineer_var.get())
        if engineer is None:
            messagebox.showerror("输入错误", "工程师配置未能解析，请重新选择。")
            self._set_busy(False)
            return
        self._run_async("read", lambda: self._read_and_generate(path, paper_path, orders, engineer), self._read_finished)

    def _read_and_generate(self, path: Path, paper_path: Path, orders: list[str], engineer: Engineer) -> ReadGenerateResult:
        """在后台完成源文件读取，并在配置生成器时继续写出四类产物。"""

        events: list[GenerationEvent] = [
            GenerationEvent(
                "source_read_start",
                status="STARTED",
                detail="开始读取源 Excel",
                fields={"requested_order_count": len(orders), "source_suffix": path.suffix.lower()},
            )
        ]
        catalog = read_paper_standard_workbook(paper_path)
        events.append(
            GenerationEvent(
                "paper_catalog_load",
                status="SUCCESS",
                detail="线下规范清单读取完成",
                fields={
                    "paper_catalog_filename": paper_path.name,
                    "paper_catalog_entry_count": len(catalog.entries),
                },
            )
        )
        tasks = read_order_tasks(path, orders)
        for order_number, task in tasks.items():
            sheet_names = sorted({row.sheet_name for row in task.rows})
            events.append(
                GenerationEvent(
                    "source_read_result",
                    order_number=order_number,
                    status="SUCCESS" if not task.errors else "ERROR",
                    detail="源 Excel 单号读取完成" if not task.errors else "源 Excel 单号读取存在异常",
                    fields={
                        "row_count": len(task.rows),
                        "sheet_count": len(sheet_names),
                        "sheet_names": ",".join(sheet_names),
                        "task_error_count": len(task.errors),
                    },
                )
            )
        if self.generator is None:
            # 保留可嵌入/预览场景的纯解析模式；主入口会注入生成服务，避免生产按钮静默跳过生成。
            return ReadGenerateResult(tasks, None, tuple(events), engineer)
        try:
            provider = getattr(self.generator, "provider", None)
            setter = getattr(provider, "set_paper_standard_catalog", None)
            if callable(setter):
                setter(catalog)
            self._paper_catalog = catalog
            generation = self.generator.generate(tasks, engineer)
            return ReadGenerateResult(tasks, generation, tuple(events) + generation.events, engineer)
        except GenerationError as exc:
            # 依赖解析失败也回传逐单结果，让界面显示明确阻断，而不是看起来“没有变化”。
            blocked = GenerationBatch(errors={order: str(exc) for order in tasks})
            return ReadGenerateResult(tasks, blocked, tuple(events), engineer)

    def _read_finished(
        self,
        tasks: object,
        error: BaseException | None,
        *,
        preserve_qa1_choices: bool = False,
    ) -> None:
        if error is not None:
            self._append_log("read_error", detail=str(error), stage="source_read", error_type=type(error).__name__)
            self.status_var.set("读取失败")
            self.header_status.configure(text="读取失败")
            messagebox.showerror("读取失败", str(error))
            self._set_busy(False)
            return
        try:
            manual_rows: tuple[ManualConfirmationRow, ...] = ()
            qa1_api_blocked = False
            qa1_api_blocked_detail = ""
            if isinstance(tasks, ReadGenerateResult):
                task_map = tasks.tasks
                generation = tasks.generation
                engineer = tasks.engineer
                if generation is not None:
                    manual_rows = generation.manual_confirmation_rows
                for event in tasks.events:
                    self._append_log(
                        event.name,
                        order_number=event.order_number,
                        status=event.status,
                        detail=event.detail,
                        **event.fields,
                    )
            else:
                task_map = tasks if isinstance(tasks, Mapping) else {}
                generation = None
            generated = {item.order_number: item for item in (generation.outcomes if generation else ())}
            # 保留本批生成对象供统一 Entry 保存处理器使用；对象中已经包含
            # 聚合后的 SqlRecord 与 QA1 候选，不重复合并行或重算抽检数量。
            self._generated_outcomes = dict(generated)
            generation_errors = dict(generation.errors) if generation else {}
            paper_exclusions = dict(generation.paper_exclusions) if generation else {}
            paper_review_required = dict(generation.paper_review_required) if generation else {}
            paper_only_orders = set(generation.paper_only_orders) if generation else set()
            self.state.clear()
            for order_number, task in task_map.items():
                errors = getattr(task, "errors", [])
                outcome = generated.get(order_number)
                if errors:
                    status = UiOrderStatus.ERROR_SKIPPED
                    detail = "；".join(str(error) for error in errors)
                elif outcome is not None:
                    status = UiOrderStatus.GENERATED_CONFIRM
                    detail = outcome.detail
                    self._append_log("artifact_ready", order_number=order_number, status=status.value, detail=detail)
                elif order_number in paper_only_orders:
                    controlled_review = any(
                        match.identity_rule_id for match in paper_review_required.get(order_number, ())
                    )
                    # DEMO-GROUP：受控身份待核实是阻断状态，不能显示为已确认纸质。
                    status = UiOrderStatus.BLOCKED if controlled_review else UiOrderStatus.PAPER_ONLY
                    detail = self._paper_only_detail(
                        paper_exclusions.get(order_number, ()),
                        paper_review_required.get(order_number, ()),
                    )
                    self._append_log(
                        "generation_blocked" if controlled_review else "paper_only_order",
                        order_number=order_number, status=status.value, detail=detail,
                    )
                elif order_number in generation_errors:
                    status = UiOrderStatus.BLOCKED
                    detail = generation_errors[order_number]
                    self._append_log(
                        "generation_blocked",
                        order_number=order_number,
                        status=status.value,
                        detail=detail,
                        stage="generation_resolve",
                        error_type="generation_blocked",
                    )
                else:
                    status = UiOrderStatus.READ_SUCCESS
                    detail = "读取成功，等待生成结果文件"
                sql_path = self.paths.output_dir / f"iqc_draft_{order_number}.sql"
                # 只有四类产物全部成功时才暴露结果文件链接；生成阻断/源数据异常时，
                # 即使目录中残留了上次同名单号的文件，也不得把它显示成当前批次结果。
                result_path = outcome.result_path if outcome is not None else None
                if outcome is not None:
                    sql_path = outcome.artifacts.sql_path
                self.state.stage(order_number, sql_path, result_path=result_path, row_count=len(getattr(task, "rows", [])), status=status, detail=detail)
                self._append_log("source_read", order_number=order_number, status=status.value, detail=detail)
            self._refresh_tree()
            self._select_all_orders()
            if generation is not None and (generation.paper_exclusions or generation.paper_review_required):
                self._show_paper_exclusion_dialog(generation)
            entry_client_configured = self.__dict__.get("entry_client") is not None
            # 新批次先清空上一批次的 QA1 选择，再为唯一候选填入自动选择
            # 结果；人工恢复生成则保留本批已确认值，避免已确认项再次弹窗。
            previous_qa1_choices = dict(getattr(self, "_qa1_choices", {})) if preserve_qa1_choices else {}
            self._qa1_choices = previous_qa1_choices
            qa1_rows = self._qa1_rows_from_outcomes(generated) if entry_client_configured else ()
            if preserve_qa1_choices:
                qa1_rows = tuple(row for row in qa1_rows if row.confirmation_key not in previous_qa1_choices)
            auto_choices: dict[str, str] = {}
            if entry_client_configured:
                auto_choices = self._qa1_unique_choices_from_outcomes(generated)
                self._qa1_choices.update(auto_choices)
                for confirmation_key, model_id in auto_choices.items():
                    order_number, item_code = confirmation_key.split("::", 1)
                    self._append_log(
                        "qa1_auto_confirmed",
                        order_number=order_number,
                        status="SUCCESS",
                        detail="QA1 唯一候选已自动确认，无需人工选择",
                        item_code=item_code,
                        model_id=model_id,
                        matched_by="unique_candidate",
                        reason="unique_candidate_auto_confirmed",
                    )
            self._log_qa1_popup_diagnostics(
                generated,
                entry_client_configured=entry_client_configured,
                manual_rows=manual_rows,
                qa1_rows=qa1_rows,
                auto_choices=auto_choices if entry_client_configured else {},
            )
            if not entry_client_configured:
                # QA1 metadata is intentionally kept out of ordinary SQL/BAT.
                # Surface the missing Entry API dependency instead of silently
                # discarding a matched candidate; the ordinary draft path stays
                # available for the operator.
                qa1_candidate_items = tuple(
                    (
                        str(order_number),
                        str(getattr(getattr(spec, "combination", None), "item_code", "")).strip(),
                    )
                    for order_number, outcome in generated.items()
                    for spec in getattr(getattr(outcome, "resolved_order", None), "qa1_drafts", ())
                    if getattr(spec, "candidates", ())
                    and str(getattr(getattr(spec, "combination", None), "item_code", "")).strip()
                )
                if qa1_candidate_items:
                    qa1_api_blocked = True
                    qa1_api_blocked_detail = (
                        "QA1关键零部件候选存在，但未配置 IQC_ENTRY_API_BASE_URL，"
                        "未新增关键零部件草稿；普通 BAT 流程仍可用。配置后请重启程序再提交。"
                    )
                    self._append_log(
                        "qa1_submission_blocked",
                        status="BLOCKED",
                        detail=qa1_api_blocked_detail,
                        candidate_row_count=len(qa1_candidate_items),
                        item_code_count=len({item_code for _order, item_code in qa1_candidate_items}),
                        api_env_required="IQC_ENTRY_API_BASE_URL",
                    )
            if qa1_rows:
                self._append_log(
                    "qa1_confirmation_required",
                    status="PENDING",
                    detail="QA1 候选已并入普通模板确认表；尚未提交 API 草稿",
                    candidate_row_count=len(qa1_rows),
                )
            if (manual_rows or qa1_rows) and (not manual_rows or (engineer is not None and self.generator is not None)):
                self._pending_confirmation_rows = manual_rows
                self._pending_confirmation_tasks = task_map
                self._pending_confirmation_engineer = engineer
                self._manual_resume_started = False
                # 普通 unresolved 行和 QA1 行一次性进入同一个候选对话；
                # 对话结束后普通行可能触发 provider 恢复，QA1 仅保存选择。
                self._begin_manual_confirmation(manual_rows, qa1_rows=qa1_rows)
            elif qa1_rows:
                # 无普通 unresolved 行时也可独立展示 QA1，但仍只调用一次
                # 同一确认函数，绝不弹出第二套 QA1 对话。
                self._begin_manual_confirmation((), qa1_rows=qa1_rows)
            if generation is not None and generation.outcomes:
                excluded_count = sum(len(matches) for matches in generation.paper_exclusions.values())
                suffix = f"；纸质记录 {excluded_count} 项不导入" if excluded_count else ""
                self.status_var.set(f"读取并生成完成：{len(generation.outcomes)} 个单号；请确认结果文件后导入{suffix}")
                self.header_status.configure(text=f"生成完成 · {len(generation.outcomes)} 个单号")
            elif generation is not None and generation.errors:
                self.status_var.set(f"解析完成，但生成失败：{len(generation.errors)} 个单号")
                self.header_status.configure(text="生成阻断")
            elif generation is not None and generation.paper_only_orders:
                if any(match.identity_rule_id for matches in generation.paper_review_required.values() for match in matches):
                    self.status_var.set(f"解析完成：{len(generation.paper_only_orders)} 个单号为纸质或待核实，普通电子记录已阻断")
                    self.header_status.configure(text="纸质/待核实 · 不导入")
                else:
                    self.status_var.set(f"解析完成：{len(generation.paper_only_orders)} 个单号均为纸质记录，不导入")
                    self.header_status.configure(text="纸质记录 · 不导入")
            else:
                self.status_var.set(f"解析完成：{len(task_map)} 个送检单号；请确认结果文件后导入")
                self.header_status.configure(text=f"解析完成 · {len(task_map)} 个单号")
            if qa1_api_blocked:
                self.detail_var.set(qa1_api_blocked_detail)
                try:
                    current_status = str(self.status_var.get())
                except (AttributeError, tk.TclError):
                    current_status = ""
                status_suffix = "QA1 附加草稿未新增（需配置 Entry API）"
                self.status_var.set((current_status + "；" if current_status else "") + status_suffix)
                try:
                    current_header = str(self.header_status.cget("text"))
                except (AttributeError, tk.TclError):
                    current_header = "解析完成"
                self.header_status.configure(text=current_header + " · QA1待配置API")
        except (UiStateError, TypeError) as exc:
            self.status_var.set("状态装载失败")
            self._append_log("state_error", detail=str(exc))
            messagebox.showerror("状态装载失败", str(exc))
        finally:
            # 人工确认后可能已启动后台恢复生成；此时保持按钮禁用，避免
            # 操作员在恢复查询期间再次点击“读取并生成”。
            if getattr(self, "_manual_resume_started", False):
                self._set_busy(True)
            else:
                self._set_busy(False)

    @staticmethod
    def _paper_only_detail(
        exclusions: tuple[PaperStandardMatch, ...],
        review_required: tuple[PaperStandardMatch, ...],
    ) -> str:
        parts: list[str] = []
        if exclusions:
            parts.append(f"{len(exclusions)} 项命中线下纸质清单")
        if review_required:
            parts.append(
                f"{len(review_required)} 项待核实/范围阻断，普通电子记录已阻断"
                if any(match.identity_rule_id for match in review_required)
                else f"{len(review_required)} 项编码需人工检查"
            )
        return "；".join(parts) + "；未生成电子草稿或导入产物"

    def _show_paper_exclusion_dialog(self, batch: GenerationBatch) -> None:
        excluded_count = sum(len(matches) for matches in batch.paper_exclusions.values())
        review_count = sum(len(matches) for matches in batch.paper_review_required.values())
        electronic_count = len(batch.outcomes)
        rows: list[tuple[str, PaperStandardMatch]] = []
        for order_number, matches in batch.paper_exclusions.items():
            rows.extend((order_number, match) for match in matches)
        for order_number, matches in batch.paper_review_required.items():
            rows.extend((order_number, match) for match in matches)
        if not rows:
            return
        dialog = tk.Toplevel(self)
        dialog.title("线下纸质规范识别结果")
        dialog.transient(self)
        dialog.configure(bg=BG)
        dialog.geometry("1180x520")
        dialog.minsize(980, 420)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        summary = (
            f"已识别 {excluded_count} 份纸质记录，已跳过数据库草稿匹配。"
            f"剩余 {electronic_count} 份继续进行电子草稿匹配。"
        )
        if review_count:
            summary += (
                f" 另有 {review_count} 项待核实/范围阻断，普通电子记录已阻断；具体原因见下表。"
                if any(match.identity_rule_id for _, match in rows)
                else f" 另有 {review_count} 项编码需人工检查，已按 fail-closed 处理。"
            )
        self._label(dialog, summary, size=11, weight="bold", color=TEXT, row=0, column=0, padx=14, pady=(14, 10), sticky="w")
        columns = ("order", "item", "windchill", "key", "catalog", "raw", "revision", "result", "reason")
        tree = ttk.Treeview(dialog, columns=columns, show="headings", style="Modern.Treeview")
        headings = (
            ("order", "送检单号", 120),
            ("item", "物料编号", 128),
            ("windchill", "Windchill 原文", 210),
            ("key", "排除键", 160),
            ("catalog", "清单工作表/行号", 120),
            ("raw", "B 列原文", 220),
            ("revision", "版次", 70),
            ("result", "结果", 140),
            ("reason", "阻断原因", 190),
        )
        for column, title, width in headings:
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor=tk.W, stretch=column in {"windchill", "raw", "reason"})
        scrollbar = ttk.Scrollbar(dialog, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=1, column=0, sticky="nsew", padx=(14, 0), pady=(0, 12))
        scrollbar.grid(row=1, column=1, sticky="ns", padx=(4, 14), pady=(0, 12))
        for index, (order_number, match) in enumerate(rows, start=1):
            entry = match.entry
            catalog_ref = f"{entry.sheet_name}!B{entry.source_row}" if entry is not None else ""
            raw_value = entry.raw_value if entry is not None else ""
            revision = entry.revision if entry is not None else ""
            result_label = {
                MATCH_EXCLUDED: "纸质记录/不导入",
                MATCH_REVIEW_REQUIRED: "待核实/范围阻断" if match.identity_rule_id else "编码字符待人工检查",
                MATCH_UNDETERMINED: "身份待核实/普通电子已阻断" if match.identity_rule_id else "Windchill 编码无法确认",
            }.get(match.status, match.status)
            tree.insert(
                "",
                tk.END,
                iid=f"paper-{index}",
                values=(
                    order_number,
                    match.item_code,
                    match.windchill_raw,
                    match.exclude_key,
                    catalog_ref,
                    raw_value,
                    revision,
                    result_label,
                    match.reason,
                ),
            )
        close_row = tk.Frame(dialog, bg=BG)
        close_row.grid(row=2, column=0, columnspan=2, sticky="e", padx=14, pady=(0, 14))
        self._button(close_row, "关闭", dialog.destroy).grid(row=0, column=0, sticky="e")

    def _begin_manual_confirmation(
        self,
        rows: tuple[ManualConfirmationRow, ...],
        *,
        qa1_rows: tuple[ManualConfirmationRow, ...] = (),
    ) -> None:
        """一次弹出普通与 QA1 候选表，并分流同一份确认结果。

        ``rows`` 由普通模板恢复生成使用，``qa1_rows`` 只用于同批 API
        modelId 选择。两者合并后只显示一个对话；普通行在确认后触发
        provider 恢复，QA1 行仅写入内存选择，最终由同一提交按钮保存。
        """

        combined_rows = tuple(rows) + tuple(qa1_rows)

        draft_path = self.paths.output_dir / (
            "manual_model_confirmation_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json"
        )
        self._pending_confirmation_path = draft_path
        try:
            write_confirmation_draft(draft_path, combined_rows, status="PENDING")
        except (OSError, UnicodeError) as exc:
            self._append_log("manual_confirmation_required", status="ERROR", detail=f"人工确认草稿写入失败：{exc}")
            messagebox.showerror("人工确认不可用", f"无法写入人工确认草稿：{exc}")
            return
        self._append_log(
            "manual_confirmation_required",
            status="BLOCKED",
            detail="候选模板请在同一表中逐行确认；未勾选项将按人工放弃处理",
            unresolved_count=len(rows),
            qa1_candidate_count=len(qa1_rows),
            item_code_count=len({row.confirmation_key for row in combined_rows}),
            draft_filename=draft_path.name,
        )
        choices = self._show_manual_confirmation_dialog(combined_rows)
        if choices is None:
            self._qa1_choices = {}
            self.detail_var.set("人工确认已取消；未写出导入草稿")
            self._append_log(
                "manual_confirmation_result",
                status="CANCELLED",
                detail="操作员关闭候选确认表；普通/QA1 均未继续保存",
            )
            return
        try:
            decisions = validate_confirmation_choices(combined_rows, choices)
            # QA1 的草稿名称不是必填输入；人工选中模板后若名称留空，
            # 自动回填所选 ACTIVE 模板的规范名称，避免后续记录出现空名称。
            decisions = self._fill_qa1_manual_names(qa1_rows, decisions)
            write_confirmation_draft(draft_path, combined_rows, decisions=decisions, status="CONFIRMED")
        except (ValueError, OSError, UnicodeError) as exc:
            self._append_log("manual_confirmation_result", status="ERROR", detail=f"人工确认结果无效：{exc}")
            messagebox.showerror("人工确认无效", str(exc))
            return
        for row in combined_rows:
            decision = decisions.get(row.confirmation_key)
            model_id = str(getattr(decision, "model_id", "")).strip() if decision else ""
            manual_name = str(getattr(decision, "manual_name", "")).strip() if decision else ""
            self._append_log(
                "qa1_confirmation_result" if row in qa1_rows else "manual_confirmation_result",
                order_number=row.order_number,
                status="CONFIRMED" if decision else "DECLINED",
                detail="已确认候选模板" if decision else "未勾选，按人工放弃处理",
                item_code=row.item_code,
                model_id=model_id,
                manual_name=manual_name,
                reason="manual_confirmed" if decision else "manual_declined",
            )
        # QA1 行只保存显式选择/放弃，不喂给普通 provider，避免污染普通
        # 模板恢复决策。复合 key 与 _entry_submit_orders 保持一致。
        # 保留本批次在进入对话前已经自动确认的唯一 QA1 候选。
        self._qa1_choices = dict(getattr(self, "_qa1_choices", {}))
        for row in qa1_rows:
            decision = decisions.get(row.confirmation_key)
            if decision is None:
                self._qa1_choices[row.confirmation_key] = False
            else:
                model_id = str(getattr(decision, "model_id", "")).strip()
                self._qa1_choices[row.confirmation_key] = model_id or False
        if not rows:
            # 只有 QA1 候选时无需启动 provider 恢复线程；统一表单确认
            # 已完成，提交按钮可直接进入 Entry 保存处理器。
            self.status_var.set("候选确认完成；请人工确认结果文件后提交")
            self.header_status.configure(text="候选确认完成")
            return
        normal_decisions = {row.confirmation_key: decisions.get(row.confirmation_key) for row in rows}
        provider = getattr(self.generator, "provider", None)
        setter = getattr(provider, "set_manual_confirmations", None)
        if not callable(setter) or self._pending_confirmation_tasks is None or self._pending_confirmation_engineer is None:
            self._append_log("manual_confirmation_result", status="ERROR", detail="当前生成提供者不支持人工确认恢复")
            messagebox.showerror("人工确认不可用", "当前生成提供者不支持人工确认恢复，未生成导入草稿。")
            return
        setter(normal_decisions)
        self._manual_resume_started = True
        self.status_var.set("人工确认已提交，正在复用 Windchill 结果继续生成")
        self.header_status.configure(text="人工确认恢复中…")
        self._set_busy(True)
        pending_tasks = self._pending_confirmation_tasks
        pending_engineer = self._pending_confirmation_engineer
        self._run_async(
            "manual_resume",
            lambda: self.generator.generate(pending_tasks, pending_engineer),
            self._manual_resume_finished,
        )

    def _manual_resume_finished(self, result: object, error: BaseException | None) -> None:
        """人工确认后的恢复生成仍走统一结果装载与产物安全门。"""

        task_map = self._pending_confirmation_tasks or {}
        engineer = self._pending_confirmation_engineer
        # 恢复结果仍属于同一批次；让 _read_finished 保留既有 QA1 选择，
        # 并在成功/异常两条路径都结束恢复 busy 生命周期。
        try:
            if error is not None:
                self._read_finished(
                    ReadGenerateResult(task_map, None, (), engineer),
                    error,
                    preserve_qa1_choices=True,
                )
                return
            generation = result if isinstance(result, GenerationBatch) else None
            self._read_finished(
                ReadGenerateResult(task_map, generation, generation.events if generation else (), engineer),
                None,
                preserve_qa1_choices=True,
            )
        finally:
            self._manual_resume_started = False
            self._set_busy(False)

    def _log_qa1_popup_diagnostics(
        self,
        generated: Mapping[str, object],
        *,
        entry_client_configured: bool,
        manual_rows: tuple[ManualConfirmationRow, ...],
        qa1_rows: tuple[ManualConfirmationRow, ...],
        auto_choices: Mapping[str, str],
    ) -> None:
        """记录 QA1 候选是否进入人工弹窗的完整决策链。

        ``key_component_match`` 只说明匹配层得到候选，不能说明 UI 是否会
        弹窗。这里把候选数量、候选 ID/名称、Entry API 配置、自动确认结果、
        QA1 行是否进入弹窗，以及普通检查单是否同时触发弹窗分开记录，便于
        现场区分“旧版本/候选不唯一”和“普通检查单行触发了共用弹窗”。
        """

        qa1_popup_keys = {row.confirmation_key for row in qa1_rows}
        for order_number, outcome in generated.items():
            resolved = getattr(outcome, "resolved_order", None)
            specs = getattr(resolved, "qa1_drafts", ()) if resolved is not None else ()
            for spec in specs:
                combination = getattr(spec, "combination", None)
                item_code = str(getattr(combination, "item_code", "")).strip()
                key = f"{order_number}::{item_code}"
                candidates = tuple(getattr(spec, "candidates", ()))
                candidate_ids = [str(getattr(candidate, "id", "")).strip() for candidate in candidates]
                candidate_names = [
                    str(getattr(candidate, "procedure_name", "")).strip()
                    for candidate in candidates
                ]
                unique_id = self._qa1_unique_candidate_id(spec)
                auto_confirmed = key in auto_choices
                in_manual_popup = key in qa1_popup_keys
                if not entry_client_configured:
                    reason = "entry_api_unconfigured"
                    status = "WARNING"
                elif auto_confirmed and unique_id:
                    reason = "unique_candidate_auto_confirmed"
                    status = "SUCCESS"
                elif len(candidates) == 0:
                    reason = "no_candidate"
                    status = "WARNING"
                elif len(candidates) != 1:
                    reason = "multiple_candidates_manual_required"
                    status = "PENDING"
                else:
                    reason = "single_candidate_missing_id_or_name"
                    status = "PENDING" if in_manual_popup else "WARNING"
                self._append_log(
                    "qa1_popup_diagnostic",
                    order_number=str(order_number),
                    status=status,
                    detail=reason,
                    item_code=item_code,
                    legacy_item_code=str(getattr(spec, "legacy_item_code", "")).strip(),
                    matched_by=str(getattr(spec, "matched_by", "")).strip(),
                    candidate_count=len(candidates),
                    candidate_model_ids="|".join(candidate_ids),
                    candidate_names="|".join(candidate_names),
                    unique_candidate_model_id=unique_id,
                    entry_api_configured=entry_client_configured,
                    auto_confirmed=auto_confirmed,
                    in_qa1_manual_popup=in_manual_popup,
                    confirmation_key=key,
                )

        popup_will_open = bool(manual_rows or qa1_rows)
        if manual_rows and qa1_rows:
            popup_reason = "ordinary_and_qa1_rows"
        elif manual_rows:
            popup_reason = "ordinary_manual_rows"
        elif qa1_rows:
            popup_reason = "qa1_manual_rows"
        else:
            popup_reason = "no_manual_rows"
        root = getattr(getattr(self, "paths", None), "project_root", None)
        runtime_version = ""
        current_pointer = ""
        if root is not None:
            root_path = Path(root)
            for filename, target in (("VERSION", "version"), ("current.txt", "pointer")):
                try:
                    value = (root_path / filename).read_text(encoding="utf-8-sig").strip()
                except (OSError, UnicodeError):
                    value = ""
                if target == "version":
                    runtime_version = value
                else:
                    current_pointer = value
        self._append_log(
            "qa1_popup_gate",
            status="PENDING" if popup_will_open else "SUCCESS",
            detail=("人工确认弹窗将打开" if popup_will_open else "QA1/普通检查单均未触发人工弹窗"),
            runtime_version=runtime_version,
            current_pointer=current_pointer,
            runtime_entry="frozen_exe" if bool(getattr(sys, "frozen", False)) else "source_python",
            entry_api_configured=entry_client_configured,
            generated_order_count=len(generated),
            ordinary_manual_row_count=len(manual_rows),
            ordinary_manual_item_codes="|".join(str(row.item_code).strip() for row in manual_rows),
            qa1_manual_row_count=len(qa1_rows),
            qa1_manual_item_codes="|".join(str(row.item_code).strip() for row in qa1_rows),
            popup_will_open=popup_will_open,
            popup_reason=popup_reason,
            auto_confirmed_qa1_count=len(auto_choices),
        )

    def _qa1_rows_from_outcomes(self, generated: Mapping[str, object]) -> tuple[ManualConfirmationRow, ...]:
        """仅把需要人工决策的 QA1 metadata 转成候选表行。"""

        rows: list[ManualConfirmationRow] = []
        for order_number, outcome in generated.items():
            resolved = getattr(outcome, "resolved_order", None)
            specs = getattr(resolved, "qa1_drafts", ()) if resolved is not None else ()
            for spec in specs:
                combination = getattr(spec, "combination", None)
                item_code = str(getattr(combination, "item_code", "")).strip()
                if not item_code:
                    continue
                # 同批同物项可能出现在多个送检单；ManualConfirmationRow
                # 的 confirmation_key 会生成复合键，显示仍保留原编码。
                candidates: list[FuzzyTemplateCandidate] = []
                for candidate in getattr(spec, "candidates", ()):
                    model_id = str(getattr(candidate, "id", "")).strip()
                    name = str(getattr(candidate, "procedure_name", "")).strip()
                    if not model_id or not name:
                        continue
                    candidates.append(
                        FuzzyTemplateCandidate(
                            model_id=model_id,
                            procedure_name=name,
                            score=1.0,
                            active=True,
                            basis=str(getattr(spec, "matched_by", "qa1_match")),
                        )
                    )
                if not candidates:
                    # 未命中只保留 provider 的审计事件，不向 UI 制造一个
                    # 无法确认的伪候选；普通草稿仍可照旧保存。
                    continue
                # 无论候选来自物料编码 apply_range，还是旧编码 procedure_code
                # 通配匹配，只要有效候选恰好一个，结果就是确定的；UI 不再
                # 额外依赖 selected_model 是否随中间对象传递成功。
                if self._qa1_unique_candidate_id(spec):
                    continue
                rows.append(
                    ManualConfirmationRow(
                        order_number=str(order_number),
                        item_code=item_code,
                        raw_name=str(getattr(spec, "legacy_item_code", "")),
                        normalized_name=(
                            str(getattr(spec, "normalized_name", "")).strip()
                            or str(getattr(combination, "item_name", "")).strip()
                        ),
                        candidates=tuple(candidates),
                        blocking_reason=(
                            "QA1 关键零部件候选，请明确选择模板；未勾选将放弃该附加草稿"
                        ),
                    )
                )
        return tuple(rows)

    def _qa1_unique_choices_from_outcomes(self, generated: Mapping[str, object]) -> dict[str, str]:
        """返回可安全自动采用的 QA1 唯一候选，供提交处理器使用。"""

        choices: dict[str, str] = {}
        for order_number, outcome in generated.items():
            resolved = getattr(outcome, "resolved_order", None)
            specs = getattr(resolved, "qa1_drafts", ()) if resolved is not None else ()
            for spec in specs:
                model_id = self._qa1_unique_candidate_id(spec)
                if not model_id:
                    continue
                combination = getattr(spec, "combination", None)
                item_code = str(getattr(combination, "item_code", "")).strip()
                if item_code and model_id:
                    choices[f"{order_number}::{item_code}"] = model_id
        return choices

    @staticmethod
    def _qa1_unique_candidate_id(spec: object) -> str:
        """唯一有效候选直接采用；多候选、缺 ID/名称时仍不得猜选。"""

        candidates = tuple(getattr(spec, "candidates", ()))
        if len(candidates) != 1:
            return ""
        candidate = candidates[0]
        model_id = str(getattr(candidate, "id", "")).strip()
        procedure_name = str(getattr(candidate, "procedure_name", "")).strip()
        return model_id if model_id and procedure_name else ""

    @staticmethod
    def _fill_qa1_manual_names(
        qa1_rows: tuple[ManualConfirmationRow, ...],
        decisions: Mapping[str, object],
    ) -> dict[str, object]:
        """用所选候选的规范名称补齐 QA1 人工决策中的空名称。"""

        normalized = dict(decisions)
        for row in qa1_rows:
            decision = normalized.get(row.confirmation_key)
            model_id = str(getattr(decision, "model_id", "")).strip() if decision else ""
            manual_name = str(getattr(decision, "manual_name", "")).strip() if decision else ""
            if not model_id or manual_name:
                continue
            candidate = next((item for item in row.candidates if item.model_id == model_id), None)
            if candidate is None:
                continue
            normalized[row.confirmation_key] = ManualConfirmationDecision(
                model_id=model_id,
                manual_name=candidate.procedure_name,
            )
        return normalized

    def _show_manual_confirmation_dialog(
        self, rows: tuple[ManualConfirmationRow, ...]
    ) -> Mapping[str, object] | None:
        """Tk 表格薄层：候选排序/校验留在无界面的纯函数中。"""

        dialog = tk.Toplevel(self)
        dialog.title("检查单模板人工确认")
        dialog.geometry("1280x680")
        dialog.minsize(920, 480)
        _size_window_90_percent(dialog)
        dialog.transient(self)
        dialog.grab_set()
        tk.Label(
            dialog,
            text="以下物项需要人工确认。请逐行勾选并选择当前有效模板；未勾选=仅放弃该物项，同单其他已确认物项仍可继续；全部放弃才不生成。草稿名称留空时将自动采用所选模板的规范名称。",
            anchor="w",
            justify=tk.LEFT,
            wraplength=1240,
            font=self._font(11, "bold"),
            fg=WARNING,
        ).pack(fill=tk.X, padx=16, pady=(14, 8))
        body = tk.Frame(dialog, bg=WHITE)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)
        canvas = tk.Canvas(body, bg=WHITE, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=canvas.yview)
        rows_frame = tk.Frame(canvas, bg=WHITE)
        canvas_window = canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        rows_frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(canvas_window, width=event.width))
        headers = ("确认", "物项编码", "旧编码", "规范化名称", "候选模板", "草稿名称（可选）")
        for column, heading in enumerate(headers):
            tk.Label(rows_frame, text=heading, bg="#F6F9FD", fg=TEXT, font=self._font(10, "bold"), anchor="w").grid(
                row=0, column=column, sticky="ew", padx=1, pady=1
            )
        # 物项编码固定为 10 位左右，不应吞掉候选和草稿输入空间；候选/
        # 草稿列按窗口剩余宽度伸缩，普通检查单和 QA1 共用同一套布局。
        column_specs = (
            (0, 0, 64),
            (0, 1, 112),
            (1, 2, 160),
            (2, 3, 220),
            (4, 4, 380),
            (2, 5, 220),
        )
        for weight, column, minsize in column_specs:
            rows_frame.grid_columnconfigure(column, weight=weight, minsize=minsize)
        check_vars: dict[str, tk.BooleanVar] = {}
        candidate_vars: dict[str, tk.StringVar] = {}
        candidate_ids: dict[str, dict[str, str]] = {}
        manual_name_vars: dict[str, tk.StringVar] = {}
        rows_by_order: dict[str, list[ManualConfirmationRow]] = {}
        for row in rows:
            rows_by_order.setdefault(row.order_number, []).append(row)
        display_row = 1
        for order_number, order_rows in rows_by_order.items():
            tk.Label(
                rows_frame,
                text=f"送检单号：{order_number}（本组 {len(order_rows)} 项）",
                bg="#E8F1FB",
                fg=TEXT,
                font=self._font(10, "bold"),
                anchor="w",
            ).grid(row=display_row, column=0, columnspan=len(headers), sticky="ew", padx=1, pady=(8, 2))
            display_row += 1
            for row in order_rows:
                # 人工确认表默认选中每一行：用户只需选择模板；主动取消
                # 某行即表示明确放弃该物项。候选下拉仍保持“请选择模板”，
                # 不因候选排序而静默代选第一项。
                check = tk.BooleanVar(value=True)
                row_key = row.confirmation_key
                check_vars[row_key] = check
                tk.Checkbutton(rows_frame, variable=check, bg=WHITE, activebackground=WHITE).grid(row=display_row, column=0, sticky="n", padx=8, pady=8)
                tk.Label(rows_frame, text=row.item_code, bg=WHITE, fg=TEXT, anchor="w").grid(row=display_row, column=1, sticky="new", padx=6, pady=8)
                tk.Label(rows_frame, text=row.raw_name or "—", bg=WHITE, fg=TEXT, anchor="w", justify=tk.LEFT, wraplength=220).grid(row=display_row, column=2, sticky="new", padx=6, pady=8)
                tk.Label(rows_frame, text=row.normalized_name, bg=WHITE, fg=TEXT, anchor="w", justify=tk.LEFT, wraplength=260).grid(row=display_row, column=3, sticky="new", padx=6, pady=8)
                values = ["请选择模板"]
                mapping: dict[str, str] = {}
                for candidate in row.candidates:
                    # basis（例如 procedure_code）只用于日志/审计，不在业务确认
                    # 界面展示技术匹配过程，避免候选项看起来像内部字段。
                    display = candidate.display_name
                    values.append(display)
                    mapping[display] = candidate.model_id
                selected = tk.StringVar(value=values[0])
                candidate_vars[row_key] = selected
                candidate_ids[row_key] = mapping
                combo = ttk.Combobox(rows_frame, textvariable=selected, values=values, state="readonly", width=38)
                combo.grid(row=display_row, column=4, sticky="ew", padx=6, pady=8)
                manual_name = tk.StringVar(value="")
                manual_name_vars[row_key] = manual_name
                tk.Entry(rows_frame, textvariable=manual_name, width=22).grid(row=display_row, column=5, sticky="ew", padx=6, pady=8)
                display_row += 1
        decision: dict[str, object] = {}
        closed = {"value": False, "accepted": False}

        def finish(accept: bool) -> None:
            if not accept:
                closed["value"] = True
                dialog.destroy()
                return
            for code, var in check_vars.items():
                decision[code] = (
                    {
                        "model_id": candidate_ids[code].get(candidate_vars[code].get(), ""),
                        "manual_name": manual_name_vars[code].get().strip(),
                    }
                    if var.get()
                    else False
                )
            closed["accepted"] = True
            closed["value"] = True
            dialog.destroy()

        buttons = tk.Frame(dialog, bg=WHITE)
        buttons.pack(fill=tk.X, padx=16, pady=(4, 14))

        def set_all_checks(selected: bool) -> None:
            """一键切换所有人工确认行；逐行仍可继续手工调整。"""

            for variable in check_vars.values():
                variable.set(selected)

        tk.Button(
            buttons,
            text="全选",
            command=lambda: set_all_checks(True),
            padx=12,
            pady=7,
        ).pack(side=tk.LEFT)
        tk.Button(
            buttons,
            text="全取消",
            command=lambda: set_all_checks(False),
            padx=12,
            pady=7,
        ).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(buttons, text="取消（稍后处理）", command=lambda: finish(False), padx=14, pady=7).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(buttons, text="确认并继续", command=lambda: finish(True), bg=PRIMARY, fg=WHITE, padx=16, pady=7).pack(side=tk.RIGHT)
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        self.wait_window(dialog)
        # ``None`` 表示取消/关闭；空字典表示明确点击“确认并继续”但
        # 未勾选任何行，后者会写入 DECLINED 决策并阻断所属订单。
        return decision if closed["value"] and closed["accepted"] else None

    def _import_selected(self) -> None:
        # 配置 Entry API 后，两个正向动作必须使用同一个普通+QA1 保存
        # 处理器；旧 BAT 只包含生成的普通 SQL，继续走该路径会静默漏掉
        # 已确认的关键零部件草稿。未配置 API 时仍保留历史 BAT 行为。
        if self.__dict__.get("entry_client") is not None:
            self._submit_entry_selected()
        else:
            self._run_import(action_label="导入")

    def _submit_selected(self) -> None:
        # 配置了 Entry API 时，“提交”仍复用同一批次确认/状态机，但
        # 保存处理器改为普通草稿 GET→组装→POST，并在同一循环中追加已
        # 明确选择的 QA1 候选；未配置时保持历史 BAT 提交行为。
        if self.__dict__.get("entry_client") is not None:
            self._submit_entry_selected()
        else:
            self._run_import(action_label="提交")

    def _submit_entry_selected(self) -> None:
        """统一保存普通草稿及 QA1 分支，实际网络由注入 client 执行。"""

        if self._busy():
            return
        selected = self._selected_orders()
        if not selected:
            messagebox.showinfo("未选择单号", "请先在结果表中选择送检单号。")
            return
        selected, skipped = self._filter_import_orders(selected)
        if skipped:
            self._append_log("entry_submit_result", status="WARNING", detail=f"异常单已跳过：{', '.join(skipped)}")
        if not selected:
            messagebox.showwarning("没有可提交单号", "所选单号均为异常/跳过或阻断状态。")
            return
        if not self.manual_confirm_var.get():
            messagebox.showwarning("需要人工确认", "请先勾选“我已人工确认结果文件内容无误”。")
            return
        if self.__dict__.get("entry_client") is None:  # 防御性分支，保持静态类型和测试夹具安全
            self._append_log("entry_submit_result", status="BLOCKED", detail="Entry API 客户端未配置")
            return
        try:
            duplicate_orders = self._duplicate_orders(selected)
            if duplicate_orders and not self._confirm_duplicate_imports(duplicate_orders):
                self._refresh_tree()
                return
            duplicate_set = set(duplicate_orders)
            for order_number in selected:
                self.state.confirm(order_number, duplicate_confirmed=order_number in duplicate_set)
            self.state.begin_entry_submission(selected)
        except UiStateError as exc:
            self._append_log("import_gate_error", detail=str(exc))
            messagebox.showerror("提交门阻断", str(exc))
            self._refresh_tree()
            return
        # 后台线程只消费本批次快照，UI 线程不会在其运行期间修改候选；
        # ordinary_records/QA1 specs 仍来自同一个 GenerationOutcome。
        outcomes = dict(self._generated_outcomes)
        choices = dict(self._qa1_choices)
        known_ids = dict(self._entry_draft_ids)
        self._active_import_orders = tuple(selected)
        self._append_log(
            "entry_submit_start",
            status="STARTED",
            detail=f"提交：{len(selected)} 个单号进入统一 Entry 保存处理器",
        )
        self.status_var.set(f"提交中；普通草稿与已确认 QA1 分支逐单保存")
        self.header_status.configure(text="Entry 提交中…")
        self._set_busy(True)
        self._run_async(
            "entry_submit",
            lambda: self._entry_submit_orders(selected, outcomes, choices, known_ids),
            self._entry_submit_finished,
        )

    def _entry_submit_orders(
        self,
        order_numbers: list[str],
        outcomes: Mapping[str, object],
        qa1_choices: Mapping[str, object],
        known_ids: Mapping[str, str],
    ) -> tuple[EntryOrderResult, ...]:
        """后台执行普通草稿和 QA1 附加草稿的一次性保存序列。"""

        client = self.__dict__.get("entry_client")
        if client is None:
            raise EntryApiError("Entry API 客户端未配置")
        qa1_orchestrator = Qa1EntryDraftOrchestrator(client)
        resubmission = EntryDraftResubmissionService(client)
        results: list[EntryOrderResult] = []
        for order_number in order_numbers:
            outcome = outcomes.get(order_number)
            resolved = getattr(outcome, "resolved_order", None) if outcome is not None else None
            if resolved is None:
                results.append(EntryOrderResult(order_number, False, "当前单号缺少生成解析对象，未调用 Entry POST"))
                continue
            saved_ids: list[tuple[str, str]] = []
            details: list[str] = []
            stage = "ordinary"
            stage_item_code = ""
            stage_model_id = ""
            try:
                # 普通检查单沿用原聚合 records；每条记录独立调用同一
                # create/update helper，不重新拆行、合并或计算抽检数量。
                for record in resolved.sql_order.records:
                    stage = "ordinary"
                    stage_item_code = str(record.item_code).strip()
                    stage_model_id = str(record.procedure_model_id).strip()
                    record_key = f"{order_number}::ordinary::{record.item_code}"
                    known_id = known_ids.get(record_key)
                    if known_id:
                        # 已知 ID 重提先回读旧状态快照，再由统一 staging
                        # 服务构造/校验 payload 并一次 update；失败不会删除
                        # 旧 child/附件，也不会自动重试。
                        old_fields = client.get_draft_context(known_id)
                        stage = resubmission.stage_update(
                            known_id,
                            record,
                            order_number=order_number,
                            old_fields=old_fields,
                        )
                        staged_result = resubmission.submit(stage)
                        if not staged_result.accepted:
                            raise EntryApiError(staged_result.detail or "普通草稿重提未确认保存")
                        save_result = EntryDraftSaveResult(
                            True,
                            staged_result.draft_id,
                            staged_result.response,
                        )
                    else:
                        save_result = client.create_draft_from_record(
                            record,
                            order_number=order_number,
                        )
                    if not save_result.accepted:
                        raise EntryApiError(f"普通草稿未确认保存：{record.item_code}")
                    if save_result.draft_id:
                        saved_ids.append((record_key, save_result.draft_id))
                    details.append(f"普通 {record.item_code} 已提交")

                # QA1 只消费明确的复合 key 决策；取消/未选择/未命中均
                # 不进入 orchestrator，因此不会触发 GET/POST。
                records_by_code = {str(record.item_code).strip(): record for record in resolved.sql_order.records}
                for spec in getattr(resolved, "qa1_drafts", ()):
                    combination = getattr(spec, "combination", None)
                    item_code = str(getattr(combination, "item_code", "")).strip()
                    stage = "qa1"
                    stage_item_code = item_code
                    key = f"{order_number}::{item_code}"
                    choice = qa1_choices.get(key)
                    if not choice or not item_code:
                        details.append(f"QA1 {item_code or '未知物项'} 未明确选择，未提交")
                        continue
                    record = records_by_code.get(item_code)
                    if record is None:
                        details.append(f"QA1 {item_code} 缺少普通聚合记录，未提交")
                        continue
                    stage_model_id = str(choice).strip()
                    qa_key = f"{order_number}::qa1::{item_code}"
                    save_result = qa1_orchestrator.submit(
                        record,
                        spec,
                        order_number=order_number,
                        selected_model_id=choice,
                        known_draft_id=known_ids.get(qa_key),
                    )
                    if not save_result.accepted:
                        raise EntryApiError(f"QA1 草稿未确认保存：{item_code}")
                    if save_result.draft_id:
                        saved_ids.append((qa_key, save_result.draft_id))
                    details.append(f"QA1 {item_code} 已提交")
                suffix = "；".join(details)
                results.append(EntryOrderResult(order_number, True, suffix or "普通草稿已提交", tuple(saved_ids)))
            except Exception as exc:
                # 已成功的前序 POST 不做客户端伪回滚/删除；该单标记失败
                # 并保留审计，由操作员决定后续重提。
                error_text = _safe_entry_error_text(exc)
                progress = "；".join(details)
                progress_suffix = f"；此前步骤：{progress}" if progress else ""
                stage_label = "关键零部件" if stage == "qa1" else "普通检查单"
                stage_detail = f"保存阶段={stage_label}；物项编码={stage_item_code or '未知'}"
                results.append(
                    EntryOrderResult(
                        order_number,
                        False,
                        f"Entry 保存失败：{type(exc).__name__}: {error_text}；{stage_detail}"
                        f"{progress_suffix}；已保留此前服务端状态，未自动重试",
                        tuple(saved_ids),
                    )
                )
        return tuple(results)

    def _entry_submit_finished(self, result: object, error: BaseException | None) -> None:
        """把 Entry 后台结果接回与 BAT 相同的逐单状态/审计界面。"""

        if error is not None:
            self.state.mark_error(self._active_import_orders, str(error))
            self._append_log("entry_submit_result", status="ERROR", detail=str(error))
            self.status_var.set("Entry 保存编排异常；未宣称提交或回滚")
            self.header_status.configure(text="Entry 提交异常")
        elif isinstance(result, tuple):
            entry_results = tuple(item for item in result if isinstance(item, EntryOrderResult))
            self.state.apply_entry_submission_results(
                {item.order_number: (item.accepted, item.detail) for item in entry_results}
            )
            for item in entry_results:
                self._entry_draft_ids.update(dict(item.draft_ids))
                self._append_log(
                    "entry_submit_result",
                    order_number=item.order_number,
                    status="SUCCESS" if item.accepted else "ERROR",
                    detail=item.detail,
                    draft_id_count=len(item.draft_ids),
                )
            self.status_var.set(f"Entry 提交批次完成：{len(entry_results)} 个单号；请查看逐单状态")
            self.header_status.configure(text="Entry 批次完成")
        self.manual_confirm_var.set(False)
        self._refresh_tree()
        self._set_busy(False)
        self._active_import_orders = ()
        if self._close_requested:
            self._finish_close()

    def _run_import(self, *, action_label: str) -> None:
        if self._busy():
            return
        selected = self._selected_orders()
        if not selected:
            messagebox.showinfo("未选择单号", "请先在结果表中选择送检单号。")
            return
        selected, skipped = self._filter_import_orders(selected)
        if skipped:
            self._append_log("import_blocked", detail=f"异常单已跳过：{', '.join(skipped)}")
        if not selected:
            messagebox.showwarning("没有可导入单号", "所选单号均为异常/跳过或阻断状态。")
            return
        if not self.manual_confirm_var.get():
            messagebox.showwarning("需要人工确认", "请先勾选“我已人工确认结果文件内容无误”。")
            return
        if self.importer is None:
            detail = "当前 UI 未注入真实导入编排器；导入按钮已被安全门阻断。"
            self._append_log("import_blocked", detail=detail)
            messagebox.showwarning("导入未配置", detail)
            return
        try:
            # A fresh generation clears the in-memory UI rows.  Merge that
            # view with the orchestrator's persisted history so a committed
            # order still triggers the duplicate confirmation dialog after
            # regeneration or application restart.
            duplicate_orders = self._duplicate_orders(selected)
            if duplicate_orders and not self._confirm_duplicate_imports(duplicate_orders):
                self._refresh_tree()
                return
            duplicate_set = set(duplicate_orders)
            for order_number in selected:
                self.state.confirm(order_number, duplicate_confirmed=order_number in duplicate_set)
            requests = self.state.build_import_requests(selected)
        except UiStateError as exc:
            self._append_log("import_gate_error", detail=str(exc))
            messagebox.showerror("导入门阻断", str(exc))
            self._refresh_tree()
            return
        self._cancel_event = threading.Event()
        self._active_import_orders = tuple(selected)
        self._append_log("import_start", detail=f"{action_label}：{len(requests)} 个单号进入逐单编排")
        self.status_var.set(f"{action_label}中；失败单不会阻断后续单号")
        self.header_status.configure(text=f"{action_label}中…")
        self._set_busy(True)
        self._run_async("import", lambda: self.importer.run_batch(requests, cancel_event=self._cancel_event), self._import_finished)

    def _duplicate_orders(self, selected: list[str]) -> list[str]:
        """Return selected orders proven committed by either state source."""

        current = {
            order_number
            for order_number in selected
            if self.state.get(order_number).status is UiOrderStatus.COMMITTED
        }
        importer = self.importer
        persisted_checker = getattr(importer, "is_committed", None)
        if callable(persisted_checker):
            for order_number in selected:
                # ImportOrchestrator.is_committed is a read-only lookup over
                # its validated state file.  A defensive bool conversion keeps
                # test/injection adapters compatible without changing the
                # duplicate gate semantics.
                if bool(persisted_checker(order_number)):
                    current.add(order_number)
        return [order_number for order_number in selected if order_number in current]

    def _filter_import_orders(self, selected: list[str]) -> tuple[list[str], list[str]]:
        skipped: list[str] = []
        valid: list[str] = []
        for order_number in selected:
            item = self.state.get(order_number)
            if item.status in {UiOrderStatus.ERROR_SKIPPED, UiOrderStatus.BLOCKED, UiOrderStatus.PAPER_ONLY}:
                if self.skip_errors_var.get():
                    skipped.append(order_number)
                    continue
                messagebox.showwarning("异常单号未跳过", f"单号 {order_number} 处于{item.status.value}，请勾选异常单跳过。")
                return [], []
            valid.append(order_number)
        return valid, skipped

    def _confirm_duplicate_imports(self, order_numbers: list[str]) -> bool:
        """Ask once before re-importing already committed orders.

        Cancelling this dialog leaves every selected order unchanged and, most
        importantly, never enters ``build_import_requests`` or the BAT worker.
        """

        joined = "、".join(order_numbers)
        accepted = messagebox.askyesno(
            "重复导入确认",
            "检测到本次为重复导入。\n"
            f"单号：{joined}\n"
            "确认后将继续调用导入 BAT，是否继续？",
            parent=self,
        )
        status = "CONFIRMED" if accepted else "CANCELLED"
        detail = (
            f"用户确认重复导入：{joined}"
            if accepted
            else f"用户取消重复导入：{joined}；未调用 BAT"
        )
        self._append_log("duplicate_import_confirmation", status=status, detail=detail)
        if not accepted:
            self.status_var.set("已取消重复导入；未调用 BAT")
        return bool(accepted)

    def _import_finished(self, result: object, error: BaseException | None) -> None:
        if error is not None:
            self.state.mark_error(self._active_import_orders, str(error))
            self._append_log("import_worker_error", detail=str(error))
            self.status_var.set("导入编排异常；未宣称提交或回滚")
            self.header_status.configure(text="导入异常")
        elif isinstance(result, BatchImportResult):
            self.state.apply_import_results(result.results)
            for item in result.results:
                self._append_log("import_result", order_number=item.order_number, status=item.status.value, detail=item.diagnostic)
            self.status_var.set(f"导入批次完成：{len(result.results)} 个单号；请查看逐单状态")
            self.header_status.configure(text="批次完成")
        self.manual_confirm_var.set(False)
        self._refresh_tree()
        self._set_busy(False)
        self._active_import_orders = ()
        if self._close_requested:
            self._finish_close()

    def _rollback_selected(self) -> None:
        selected = self._selected_orders()
        if not selected:
            messagebox.showinfo("未选择单号", "请先在结果表中选择送检单号。")
            return
        retryable: list[str] = []
        blocked: list[str] = []
        for order_number in selected:
            try:
                item = self.state.get(order_number)
                if item.status in {UiOrderStatus.ROLLED_BACK_RETRYABLE, UiOrderStatus.ERROR_RETRYABLE}:
                    self.state.request_retry(order_number)
                    retryable.append(order_number)
                    self._append_log("retry_requested", order_number=order_number, status=UiOrderStatus.GENERATED_CONFIRM.value, detail="回滚证据已存在，可再次确认重导")
                else:
                    blocked.append(order_number)
            except UiStateError as exc:
                blocked.append(order_number)
                self._append_log("rollback_blocked", order_number=order_number, detail=str(exc))
        if blocked:
            self._append_log("rollback_blocked", detail=f"不允许手工伪造回滚：{', '.join(blocked)}")
            self.detail_var.set("回滚必须由执行器提供证据，不能手工伪造")
        if retryable:
            self.detail_var.set(f"已将 {len(retryable)} 个有回滚证据的单号置为待确认重导")
        self._refresh_tree()

    def _cancel_selected(self) -> None:
        selected = self._selected_orders()
        for order_number in selected:
            try:
                item = self.state.cancel(order_number)
                self._append_log("cancel_requested", order_number=order_number, status=item.status.value, detail=item.detail)
            except UiStateError as exc:
                self._append_log("cancel_error", order_number=order_number, detail=str(exc))
        if self._busy():
            self._cancel_event.set()
            self.status_var.set("已请求取消；等待当前执行器退出")
        self._refresh_tree()

    def _selected_orders(self) -> list[str]:
        return [self._tree_orders[item_id] for item_id in self.tree.selection() if item_id in self._tree_orders]

    def _select_all_orders(self) -> None:
        """Select every freshly loaded result row for the next operator step."""

        item_ids = self.tree.get_children()
        if not item_ids:
            return
        self.tree.selection_set(item_ids)
        self.tree.focus(item_ids[0])
        self.tree.see(item_ids[0])

    def _clear_orders(self) -> None:
        self.orders_text.delete("1.0", tk.END)
        self.orders_var.set("")
        self.detail_var.set("送检单号输入已清空")

    def _reset_ui(self) -> bool:
        """重置当前界面批次，并将控件恢复为初始门禁状态。

        该入口只清理内存/UI，不删除审计日志，也不触碰 SQL/BAT/数据库。
        worker、待处理队列或 Tk 回调仍在运行时直接阻断，避免清空后异步
        回调又把旧结果写回界面；操作员应先点击“取消”并等待状态稳定。
        返回布尔值便于无界面单测验证门禁结果。
        """

        if self._busy():
            detail = "正在处理，请先取消并等待完成后再重置界面"
            self.status_var.set(detail)
            self.detail_var.set(detail)
            self._append_log("ui_reset_blocked", status="BLOCKED", detail=detail)
            messagebox.showwarning("暂不能重置", detail)
            return False

        # 导入 worker 可能已经退出但回调未能写入最终结果，UiBatchState
        # 仍会保留 IMPORTING。此时结果存在不确定性，不能清 provider 快照
        # 或伪造完成；必须先由操作员核查执行器/审计证据再决定后续动作。
        if any(item.status is UiOrderStatus.IMPORTING for item in self.state.snapshot()):
            detail = "导入结果不确定，不能重置；请先核查后再操作"
            self.status_var.set(detail)
            self.detail_var.set(detail)
            self._append_log("ui_reset_blocked", status="BLOCKED", detail=detail)
            messagebox.showwarning("暂不能重置", detail)
            return False

        # RuntimeGenerationProvider 会缓存本批只读快照、Windchill 名称和
        # 人工确认决定。先执行这一步，若注入 provider 明确报告清理失败，
        # 后续 UiBatchState/输入控件均不动，避免形成“半复位”状态；没有该
        # 可选接口的注入 provider 保持兼容。
        provider = getattr(getattr(self, "generator", None), "provider", None)
        reset_runtime_state = getattr(provider, "reset_runtime_state", None)
        if callable(reset_runtime_state):
            try:
                reset_runtime_state()
            except Exception as exc:
                detail = f"运行时批次状态清理失败：{type(exc).__name__}；未继续重置界面"
                self.status_var.set(detail)
                self.detail_var.set(detail)
                self._append_log("ui_reset_blocked", status="ERROR", detail=detail)
                messagebox.showwarning("重置未完成", detail)
                return False

        try:
            # UiBatchState 自身会拒绝 IMPORTING 状态清空；即使 worker 因
            # 回调异常已退出，也不在这里伪造“已完成”，而是给出安全提示。
            self.state.clear()
        except UiStateError as exc:
            detail = f"当前批次仍不可安全重置：{exc}；请先取消并等待"
            self.status_var.set(detail)
            self.detail_var.set(detail)
            self._append_log("ui_reset_blocked", status="BLOCKED", detail=detail)
            messagebox.showwarning("暂不能重置", detail)
            return False

        # 仅清理当前界面引用；人工确认 JSON 草稿及持久化审计日志保留，
        # 这样“重置界面”和“清空日志”不会被误认为同一个动作。
        self._pending_confirmation_rows = ()
        self._pending_confirmation_tasks = None
        self._pending_confirmation_engineer = None
        self._pending_confirmation_path = None
        self._manual_resume_started = False
        self._paper_catalog = None
        self._generated_outcomes = {}
        self._qa1_choices = {}
        self._entry_draft_ids = {}
        self._active_import_orders = ()
        self._cancel_event = threading.Event()
        self._close_requested = False

        # Preserve source file, order numbers and engineer selection across reset.
        # Only transient batch state is cleared here.
        self.manual_confirm_var.set(True)
        self.skip_errors_var.set(True)
        self._refresh_tree()
        self.status_var.set("就绪：可继续读取当前源文件和送检单号")
        self.detail_var.set("请选择单号查看详情")
        loaded_count = len(getattr(self, "_engineer_display_names", ()))
        self.header_status.configure(text=f"已加载 {loaded_count} 名预配置工程师")
        self._append_log("ui_reset", status="SUCCESS", detail="已重置界面和当前批次；审计日志保留")
        # _update_action_states 只负责依赖选择/busy 的按钮；读取、人工确认
        # 和跳过复选框的 enable 状态由 _set_busy 统一恢复，避免上一次异常
        # 将它们留在 DISABLED 后复位仍不可再次读取。
        self._set_busy(False)
        return True

    def _on_tree_select(self, _event: tk.Event[tk.Misc]) -> None:
        selected = self._selected_orders()
        if not selected:
            self.detail_var.set("请选择单号查看详情")
        else:
            item = self.state.get(selected[0])
            badge, _fg, _bg, _tag = status_meta(item.status)
            self.detail_var.set(f"{item.order_number}：{badge}；{item.detail or '无异常详情'}")
        self._update_action_states()

    def _refresh_tree(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        self._tree_orders.clear()
        self._tree_links.clear()
        for index, item in enumerate(self.state.snapshot(), start=1):
            item_id = f"order-{index}"
            self._tree_orders[item_id] = item.order_number
            self._tree_links[item_id] = {"result": item.result_path, "sql": item.sql_path}
            _badge, _fg, _bg, tag = status_meta(item.status)
            result_text = f"↗  {item.result_path.name}" if item.result_path and item.result_path.is_file() else "—"
            sql_text = f"↗  {item.sql_path.name}" if item.sql_path.is_file() else "—"
            self.tree.insert("", tk.END, iid=item_id, tags=(tag,), values=(index, item.order_number, item.row_count, result_text, sql_text, status_badge_text(item.status)))
        self._update_action_states()

    def _open_link(self, event: tk.Event[tk.Misc]) -> None:
        row_id = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not row_id or row_id not in self._tree_links or column not in {"#4", "#5"}:
            return
        key = "result" if column == "#4" else "sql"
        path = self._tree_links[row_id].get(key)
        if path is None or not path.is_file():
            self.detail_var.set(f"{key.upper()} 文件尚未生成：{path or '—'}")
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)  # type: ignore[attr-defined]
            self._append_log("open_output_dir", detail=str(path))
        except OSError as exc:
            self._append_log("open_output_error", detail=str(exc))
            messagebox.showerror("打开文件失败", str(exc))

    def _append_log(
        self,
        name: str,
        *,
        order_number: str | None = None,
        status: str | None = None,
        detail: str = "",
        **fields: object,
    ) -> None:
        module = str(fields.pop("module", self.__dict__.get("_active_module", "IQC"))).strip().upper()
        if module not in {"IQC", "PQC", INSPECTION_ADJUSTMENT_MODULE}:
            module = "IQC"
        try:
            self.logger.event(
                name,
                order_number=order_number,
                status=status,
                detail=detail,
                module=module,
                **fields,
            )
        except AuditLogError:
            pass
        self._render_logs(self.logger.tail(80))

    def _render_logs(self, lines: list[str]) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                self.log_text.insert(tk.END, line + "\n", "info")
                continue
            timestamp = str(payload.get("timestamp", ""))[:19].replace("T", " ")
            event = str(payload.get("event", ""))
            label = _EVENT_LABELS.get(event, event)
            status = str(payload.get("status") or "")
            # 只改界面显示文本，JSONL 审计记录仍保留原始诊断字段。
            detail = str(payload.get("detail") or "")
            detail = (
                detail.replace("procedure_code", "旧编码匹配")
                .replace("apply_range", "物料编码范围")
                .replace("source_python", "源码入口")
                .replace("frozen_exe", "打包程序")
            )
            order = str(payload.get("order_number") or "")
            raw_fields = payload.get("fields")
            field_text = ""
            module = "IQC"
            if isinstance(raw_fields, Mapping):
                candidate_module = str(raw_fields.get("module", "IQC")).strip().upper()
                if candidate_module in {"IQC", "PQC", INSPECTION_ADJUSTMENT_MODULE}:
                    module = candidate_module
                visible_fields = []
                for key, value in raw_fields.items():
                    key_text = str(key)
                    if key_text in _UI_LOG_HIDDEN_FIELDS:
                        continue
                    label_text = _UI_LOG_FIELD_LABELS.get(key_text, key_text)
                    value_text = (
                        str(value)
                        .replace("procedure_code", "旧编码匹配")
                        .replace("apply_range", "物料编码范围")
                        .replace("source_python", "源码入口")
                        .replace("frozen_exe", "打包程序")
                    )
                    visible_fields.append(f"{label_text}={value_text}")
                field_text = ";".join(visible_fields)
            suffix = "；".join(value for value in (order, status, detail, field_text) if value and value != "None")
            text = f"{timestamp}  [{module}]  [{label}]  {suffix}".rstrip()
            self.log_text.insert(tk.END, text + "\n", log_level(event, status, detail))
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log_view(self) -> None:
        """Clear both the visible log and persisted JSONL history."""

        try:
            self.logger.clear()
        except AuditLogError as exc:
            self._append_log("open_log_error", detail=str(exc))
            messagebox.showerror("清空日志失败", str(exc))
            return
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)
        self.detail_var.set("日志已清空；下一次执行只显示新日志")

    def _view_log(self) -> None:
        path = self.logger.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            opened_with_fallback = False
            try:
                if not hasattr(os, "startfile"):
                    raise OSError("当前系统没有 Windows 文件关联接口")
                # 优先交给 Windows 默认关联程序打开 JSONL；不启动 BAT/SQL/数据库流程。
                os.startfile(str(path))  # type: ignore[attr-defined]
            except OSError as association_error:
                # 某些 Windows 环境没有注册 .jsonl 关联，回退到系统 Notepad，确保日志仍可查看。
                try:
                    subprocess.Popen(["notepad.exe", str(path)], close_fds=True)
                    opened_with_fallback = True
                except OSError as fallback_error:
                    raise OSError(f"默认关联程序打开失败：{association_error}；Notepad 回退失败：{fallback_error}") from fallback_error
            detail = str(path) if not opened_with_fallback else f"已使用 Notepad 打开：{path}"
            self._append_log("view_log", detail=detail)
            self.detail_var.set(str(path))
        except OSError as exc:
            detail = f"无法打开日志文件：{path}；{exc}"
            self._append_log("open_log_error", detail=detail)
            self.detail_var.set(detail)
            messagebox.showerror("打开日志失败", detail)

    def _open_output_dir(self) -> None:
        try:
            self.paths.output_dir.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(self.paths.output_dir)  # type: ignore[attr-defined]
            else:
                self.detail_var.set(str(self.paths.output_dir))
            self._append_log("open_output_dir", detail=str(self.paths.output_dir))
        except OSError as exc:
            self._append_log("open_output_error", detail=str(exc))
            messagebox.showerror("打开失败", str(exc))

    def _update_action_states(self) -> None:
        selected = self._selected_orders() if hasattr(self, "tree") else []
        busy = self._busy()
        has_selection = bool(selected)
        can_import = has_selection and self.manual_confirm_var.get() and not busy
        for button in (getattr(self, "import_button", None), getattr(self, "submit_button", None)):
            if button is not None:
                button.configure(state=tk.NORMAL if can_import else tk.DISABLED)
        if hasattr(self, "rollback_button"):
            self.rollback_button.configure(state=tk.DISABLED if busy or not has_selection else tk.NORMAL)
        if hasattr(self, "clear_log_button"):
            self.clear_log_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        # 重置按钮始终保持可点击；busy 时由 _reset_ui 给出安全提示，
        # 避免 worker/callback 异常把用户锁在必须重启软件的状态。
        if hasattr(self, "reset_button"):
            self.reset_button.configure(state=tk.NORMAL)

    def _run_async(self, operation: str, function: Callable[[], object], callback: Callable[[object, BaseException | None], None]) -> None:
        def worker() -> None:
            try:
                value = function()
                self._worker_queue.put((operation, value, None))
            except BaseException as exc:  # 将异常安全交还 Tk 主线程，不吞掉状态
                self._worker_queue.put((operation, None, exc))

        self._worker = threading.Thread(target=worker, name=f"iqc-{operation}", daemon=True)
        self._worker.start()
        setattr(self, f"_callback_{operation}", callback)

    def _drain_worker_queue(self) -> None:
        try:
            operation, value, error = self._worker_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._drain_worker_queue)
            return
        callback = getattr(self, f"_callback_{operation}", None)
        completed_worker = self._worker
        self._callback_in_progress = True
        callback_error: BaseException | None = None
        try:
            if callback is None:
                raise RuntimeError(f"后台操作 {operation!r} 缺少结果回调")
            callback(value, error)
        except BaseException as exc:  # 保证 Tk 队列不会因回调异常永久停摆
            callback_error = exc
            # 回调异常并不等于导入已回滚或已完成；先提示核查证据，
            # 不向操作员承诺可以立即重置，避免误清 IMPORTING 批次。
            detail = f"后台结果处理异常：{type(exc).__name__}；请先核查当前批次结果，再决定是否重置界面"
            try:
                self._append_log("state_error", status="ERROR", detail=detail, operation=operation)
                self.status_var.set(detail)
                self.header_status.configure(text="后台回调异常")
            except BaseException:
                # 窗口关闭/测试夹具销毁期间，诊断控件本身可能不可用；
                # 无论如何都要释放 worker 状态并继续安排下一次队列轮询。
                pass
        finally:
            self._callback_in_progress = False
            # 回调可能启动了新的 manual_resume worker；只有当 self._worker
            # 仍指向刚完成的对象时才清空，避免把新任务误标为空闲。
            if self._worker is completed_worker:
                self._worker = None
                try:
                    self._set_busy(False)
                except BaseException:
                    # 控件销毁/测试夹具异常不能阻止 worker 指针释放；
                    # 下次用户仍可通过“重置界面”进入安全恢复路径。
                    pass
            elif callback_error is not None:
                try:
                    self._update_action_states()
                except BaseException:
                    pass
            try:
                self.after(100, self._drain_worker_queue)
            except (tk.TclError, RuntimeError):
                # 窗口正在关闭时 after 可能失败；这不应伪造业务完成状态。
                pass

    def _set_busy(self, busy: bool) -> None:
        self._ui_busy_flag = bool(busy)
        state = tk.DISABLED if busy else tk.NORMAL
        self.read_button.configure(state=state)
        self.manual_check.configure(state=state)
        self.skip_check.configure(state=state)
        for button_name in (
            "iqc_tab_button",
            "pqc_tab_button",
            "inspection_adjustment_tab_button",
        ):
            button = self.__dict__.get(button_name)
            if button is not None:
                button.configure(state=state)
        pqc_page = self.__dict__.get("pqc_page")
        if pqc_page is not None:
            pqc_page.set_busy(busy)
        adjustment_page = self.__dict__.get("inspection_adjustment_page")
        if adjustment_page is not None:
            adjustment_page.set_busy(busy)
        self._update_action_states()

    def _busy(self) -> bool:
        # 线程可能已结束但结果尚未由 Tk 主线程消费；队列非空同样属于
        # 活动批次，复位必须阻断，否则 drain 回调会把旧结果重新写回。
        worker_queue = self.__dict__.get("_worker_queue")
        queue_pending = worker_queue is not None and not worker_queue.empty()
        worker = self.__dict__.get("_worker")
        return bool(
            self.__dict__.get("_ui_busy_flag", False)
            or (worker is not None and worker.is_alive())
            or queue_pending
            or self.__dict__.get("_callback_in_progress", False)
        )

    def _on_close(self) -> None:
        if self._busy():
            self._close_requested = True
            self._cancel_event.set()
            self.status_var.set("正在取消活动工作，完成后关闭窗口")
            self.after(100, self._poll_close)
            return
        self._finish_close()

    def _poll_close(self) -> None:
        if self._busy():
            self.after(100, self._poll_close)
        else:
            self._finish_close()

    def _finish_close(self) -> None:
        try:
            self._save_source_path_preference()
            self.logger.close()
        finally:
            self.destroy()
