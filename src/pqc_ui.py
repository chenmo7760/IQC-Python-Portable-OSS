"""Tk page for the PQC two-step, one-device-per-row workflow."""

from __future__ import annotations

import tkinter as tk
import textwrap
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterable, Mapping

from .models import Engineer
from .pqc_excel import PqcExcelError, projects_for_engineer, read_pqc_workbook
from .pqc_models import (
    PQC_UI_SLOT_CODES,
    PqcCandidateState,
    PqcCreationBatch,
    PqcCreationStatus,
    PqcDeviceCandidate,
    PqcProject,
    PqcTool,
    PqcToolValidity,
)
from .pqc_query import PqcReadonlyQueryService
from .pqc_rules import build_device_candidates, sort_tools_for_type
from .pqc_ui_state import PqcUiState, PqcUiStateError
from .processend_api import ProcessEndDraftApiClient, PqcDraftCreationService
from .ui_preferences import (
    PQC_COLUMN_WIDTH_DEFAULTS,
    PQC_COLUMN_WIDTH_KEYS,
    PQC_COLUMN_WIDTH_MAX,
    PQC_COLUMN_WIDTH_MIN,
    PQC_LAYOUT_WIDTH_DEFAULTS,
    PQC_LAYOUT_WIDTH_KEYS,
    PQC_LAYOUT_WIDTH_MAX,
    PQC_LAYOUT_WIDTH_MIN,
    PQC_SCOPE_WIDTH_MAX,
    PQC_STATUS_COLUMN_WIDTH,
    PQC_TOOL_COLUMN_WIDTH_DEFAULTS,
    PQC_TOOL_COLUMN_WIDTH_KEYS,
    UiPreferenceStore,
)


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
PQC_SLOT_LABELS = {
    "01": "装配",
    "02": "接线",
    "03": "上电前",
    "04": "上电后",
    "05": "扭矩",
    "CATEGORY": "类别模板",
}
PQC_COLUMN_WIDTH_LIMITS = {
    "select": (44, 58),
    "excel": (72, 86),
    "item": (160, 260),
    "sequence": (150, 240),
    "01": (78, 112),
    "02": (78, 112),
    "03": (92, 128),
    "04": (92, 128),
    "05": (78, 112),
    "special": (100, 600),
    "ncr": (100, 600),
    "category": (100, 600),
    "status": (PQC_STATUS_COLUMN_WIDTH, PQC_STATUS_COLUMN_WIDTH),
}


class PqcPage(tk.Frame):
    """Independent PQC controls embedded in the shared application window."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        owner: object,
        output_dir: Path,
        query_service: PqcReadonlyQueryService | None,
        api_client: ProcessEndDraftApiClient | None,
        preference_store: UiPreferenceStore | None = None,
    ) -> None:
        super().__init__(parent, bg=BG)
        self.owner = owner
        self.output_dir = Path(output_dir)
        self.query_service = query_service
        self.api_client = api_client
        # 与主窗口共用按用户隔离的偏好文件；读取失败时只使用源码默认列宽。
        self._preference_store = preference_store or UiPreferenceStore.for_current_user()
        self._pqc_column_widths = self._load_pqc_column_widths()
        self._pqc_tool_column_widths = self._load_pqc_tool_column_widths()
        self._pqc_layout_widths = self._load_pqc_layout_widths()
        self._pending_layout_restore = False
        self.state_model = PqcUiState()
        self._engineers_by_display: dict[str, Engineer] = {}
        self._projects_by_label: dict[str, PqcProject] = {}
        self._devices_by_iid: dict[str, str] = {}
        self._tools: tuple[PqcTool, ...] = ()
        self._tools_by_id: dict[str, PqcTool] = {}
        self._pending_tool_ids: set[str] = set()
        self._pending_tool_key: tuple[str, str] | None = None
        self._current_item = ""
        self._current_type = ""
        self._busy_flag = False
        self._context_epoch = 0

        shared_source_var = getattr(owner, "pqc_source_var", None)
        if not isinstance(shared_source_var, tk.Variable):
            shared_source_var = getattr(owner, "source_var", None)
        self.source_var = shared_source_var if isinstance(shared_source_var, tk.Variable) else tk.StringVar()
        self.engineer_var = tk.StringVar(value="请选择预配置人员")
        self.project_var = tk.StringVar(value="请先查询项目")
        self.type_bulk_var = tk.StringVar(value="装配")
        self.summary_var = tk.StringVar(value="尚未查询项目")
        self.current_device_var = tk.StringVar(value="请选择设备")
        self.current_type_var = tk.StringVar(value="请选择检验类型")
        self.confirm_var = tk.BooleanVar(value=True)
        self.count_var = tk.StringVar(value="成功 0  ·  失败 0  ·  跳过 0  ·  待确认 0")
        self._build()
        self.source_var.trace_add("write", lambda *_args: self._update_action_states())
        self.confirm_var.trace_add("write", lambda *_args: self._sync_confirmation())

    @staticmethod
    def _normalize_pqc_column_widths(raw: object) -> dict[str, int]:
        """将持久化列宽限制在安全范围，异常键值回退默认值。"""

        widths = dict(PQC_COLUMN_WIDTH_DEFAULTS)
        if not isinstance(raw, Mapping):
            return widths
        for key, value in raw.items():
            if key not in PQC_COLUMN_WIDTH_KEYS:
                continue
            # 状态是最右业务列，保持固定宽度；旧版曾允许 600px 偏好
            # 和 stretch 把它挤成可变宽度，这里统一迁移回稳定值。
            if key == "status":
                widths[key] = PQC_STATUS_COLUMN_WIDTH
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if PQC_COLUMN_WIDTH_MIN <= value <= PQC_COLUMN_WIDTH_MAX:
                minimum, maximum = PQC_COLUMN_WIDTH_LIMITS.get(
                    str(key), (PQC_COLUMN_WIDTH_MIN, PQC_COLUMN_WIDTH_MAX)
                )
                widths[key] = max(minimum, min(maximum, int(value)))
        return widths

    def _load_pqc_column_widths(self) -> dict[str, int]:
        try:
            preference = self._preference_store.load()
            return self._normalize_pqc_column_widths(getattr(preference, "pqc_column_widths", {}))
        except Exception:
            # UI 偏好不是业务数据，任何读取异常均不影响 PQC 候选和模板逻辑。
            return dict(PQC_COLUMN_WIDTH_DEFAULTS)

    @staticmethod
    def _normalize_pqc_tool_column_widths(raw: object) -> dict[str, int]:
        """校验右侧工器具表列宽，异常项回退独立默认值。"""

        widths = dict(PQC_TOOL_COLUMN_WIDTH_DEFAULTS)
        if not isinstance(raw, Mapping):
            return widths
        for key, value in raw.items():
            if key not in PQC_TOOL_COLUMN_WIDTH_KEYS:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if PQC_COLUMN_WIDTH_MIN <= value <= PQC_COLUMN_WIDTH_MAX:
                widths[key] = int(value)
        return widths

    def _load_pqc_tool_column_widths(self) -> dict[str, int]:
        try:
            preference = self._preference_store.load()
            return self._normalize_pqc_tool_column_widths(getattr(preference, "pqc_tool_column_widths", {}))
        except Exception:
            return dict(PQC_TOOL_COLUMN_WIDTH_DEFAULTS)

    @staticmethod
    def _normalize_pqc_layout_widths(raw: object) -> dict[str, int]:
        """将两栏宽度约束到安全范围，并补齐缺省比例。"""

        widths = dict(PQC_LAYOUT_WIDTH_DEFAULTS)
        if not isinstance(raw, Mapping):
            return widths
        for key, value in raw.items():
            if key not in PQC_LAYOUT_WIDTH_KEYS:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if PQC_LAYOUT_WIDTH_MIN <= value <= PQC_LAYOUT_WIDTH_MAX:
                widths[key] = min(PQC_SCOPE_WIDTH_MAX, int(value)) if key == "scope" else int(value)
        return widths

    def _load_pqc_layout_widths(self) -> dict[str, int]:
        try:
            preference = self._preference_store.load()
            return self._normalize_pqc_layout_widths(getattr(preference, "pqc_layout_widths", {}))
        except Exception:
            return dict(PQC_LAYOUT_WIDTH_DEFAULTS)

    @property
    def _tools_ui_enabled(self) -> bool:
        tree = getattr(self, "tool_tree", None)
        if tree is None:
            return False
        check_exists = getattr(tree, "winfo_exists", None)
        return bool(check_exists() if callable(check_exists) else True)

    def _font(self, size: int, weight: str = "normal") -> tuple[str, int, str]:
        return ("Microsoft YaHei UI", size, weight)

    def _label(self, parent: tk.Misc, text: str = "", *, textvariable: tk.Variable | None = None, size: int = 10, weight: str = "normal", color: str = TEXT, **grid: object) -> tk.Label:
        label = tk.Label(parent, text=text, textvariable=textvariable, font=self._font(size, weight), fg=color, bg=parent.cget("bg"), anchor="w")
        if grid:
            label.grid(**grid)
        return label

    def _button(self, parent: tk.Misc, text: str, command: Callable[[], None], *, kind: str = "outline") -> tk.Button:
        if kind == "primary":
            colors = dict(bg=PRIMARY, fg=WHITE, activebackground=PRIMARY_DARK, activeforeground=WHITE, disabledforeground="#A8C0EF")
        elif kind == "success":
            colors = dict(bg=SUCCESS, fg=WHITE, activebackground="#15803D", activeforeground=WHITE, disabledforeground="#A9D9B7")
        else:
            colors = dict(bg=WHITE, fg=PRIMARY, activebackground=INFO_BG, activeforeground=PRIMARY_DARK, disabledforeground="#A8B9D1")
        button = tk.Button(
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
            highlightbackground=SUCCESS if kind == "success" else PRIMARY,
            highlightcolor=SUCCESS if kind == "success" else PRIMARY,
            **colors,
        )
        original_configure = button.configure

        def _configure(**kwargs: object) -> object:
            result = original_configure(**kwargs)
            if "state" in kwargs:
                original_configure(bg=colors["bg"], activebackground=colors["activebackground"])
            return result

        button.configure = _configure  # type: ignore[assignment]
        button.config = _configure  # type: ignore[assignment]
        button._pqc_kind = kind  # type: ignore[attr-defined]
        return button

    def _card(self, parent: tk.Misc) -> tk.Frame:
        return tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1, bd=0)

    @staticmethod
    def _wrap_to_panel(label: tk.Widget, panel: tk.Misc, padding: int = 32) -> None:
        """Long notes wrap within their panel instead of taking table space."""
        panel.bind("<Configure>", lambda event: label.configure(wraplength=max(120, event.width - padding)), add="+")
        label.configure(justify=tk.LEFT)

    def _build(self) -> None:
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.layout_pane = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        self.layout_pane.grid(row=0, column=0, sticky="nsew")
        self.scope_pane = tk.Frame(self.layout_pane, bg=BG)
        self.devices_pane = tk.Frame(self.layout_pane, bg=BG)
        self.layout_pane.add(self.scope_pane, weight=1)
        self.layout_pane.add(self.devices_pane, weight=1)
        for pane in (self.scope_pane, self.devices_pane):
            pane.columnconfigure(0, weight=1)
            pane.rowconfigure(0, weight=1)
        self._build_scope_card()
        self._build_devices_card()
        self.layout_pane.bind("<ButtonRelease-1>", self._on_layout_pane_resize, add="+")
        self.bind("<Configure>", self._on_layout_configure, add="+")
        self.after_idle(self._restore_layout_widths)

    def _build_scope_card(self) -> None:
        card = self._card(self.scope_pane)
        self.scope_card = card
        card.grid(row=0, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(0, weight=1)
        self.scope_canvas = tk.Canvas(card, bg=CARD, highlightthickness=0, bd=0)
        self.scope_canvas.grid(row=0, column=0, sticky="nsew")
        scope_scroll = ttk.Scrollbar(card, orient=tk.VERTICAL, command=self.scope_canvas.yview)
        scope_scroll.grid(row=0, column=1, sticky="ns")
        self.scope_canvas.configure(yscrollcommand=scope_scroll.set)
        inner = tk.Frame(self.scope_canvas, bg=CARD)
        self.scope_scroll_window = self.scope_canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.columnconfigure(0, weight=1)

        def _sync_scrollregion(_event: tk.Event[tk.Misc]) -> None:
            self.scope_canvas.configure(scrollregion=self.scope_canvas.bbox("all"))

        def _fit_inner(event: tk.Event[tk.Misc]) -> None:
            self.scope_canvas.itemconfigure(self.scope_scroll_window, width=event.width)

        inner.bind("<Configure>", _sync_scrollregion, add="+")
        self.scope_canvas.bind("<Configure>", _fit_inner, add="+")

        self._label(inner, "源 Excel 文件", size=11, weight="bold", row=0, column=0, sticky="w", padx=14, pady=(0, 7))
        source_row = tk.Frame(inner, bg=CARD)
        source_row.grid(row=1, column=0, sticky="ew", padx=14)
        source_row.columnconfigure(0, weight=1)
        self.source_entry = tk.Entry(source_row, textvariable=self.source_var, font=self._font(10), relief=tk.FLAT, bd=0, highlightthickness=1, highlightbackground="#C9D7E8", highlightcolor=PRIMARY, bg="#FBFDFF", fg=TEXT, insertbackground=TEXT)
        self.source_entry.grid(row=0, column=0, sticky="ew", ipady=9)
        self.choose_button = self._button(source_row, "选择文件", self._choose_source)
        self.choose_button.grid(row=0, column=1, padx=(8, 0))
        self.choose_button.configure(font=self._font(9), pady=4)
        source_note = self._label(inner, "系统自动匹配模板。", size=10, color=MUTED, row=2, column=0, sticky="ew", pady=(10, 14), padx=14)
        self._wrap_to_panel(source_note, inner, 8)

        self._label(inner, "质检工程师", size=11, weight="bold", row=3, column=0, sticky="w", pady=(0, 7), padx=14)
        style = ttk.Style(self)
        style.configure("Pqc.TCombobox", fieldbackground=WHITE, background=WHITE, foreground=TEXT, bordercolor="#C9D7E8", lightcolor="#C9D7E8", darkcolor="#C9D7E8", padding=6)
        self.engineer_box = ttk.Combobox(inner, width=1, textvariable=self.engineer_var, state="readonly", style="Pqc.TCombobox", font=self._font(10))
        self.engineer_box.grid(row=4, column=0, sticky="ew", padx=14)
        self.engineer_box.bind("<<ComboboxSelected>>", lambda _event: self._update_action_states())
        self.query_button = self._button(inner, "查询项目", self._query_projects, kind="primary")
        self.query_button.grid(row=5, column=0, sticky="ew", padx=14, pady=(9, 0))

        self._label(inner, "项目", size=11, weight="bold", row=6, column=0, sticky="w", pady=(18, 7), padx=14)
        self.project_box = ttk.Combobox(inner, width=1, textvariable=self.project_var, state="disabled", style="Pqc.TCombobox", font=self._font(10))
        self.project_box.grid(row=7, column=0, sticky="ew", padx=14)
        self.project_box.bind("<<ComboboxSelected>>", self._on_project_selected)
        self.load_button = self._button(inner, "加载设备候选", self._load_devices, kind="primary")
        self.load_button.grid(row=8, column=0, sticky="ew", padx=14, pady=(9, 0))
        summary = self._label(inner, textvariable=self.summary_var, size=9, color=MUTED, row=9, column=0, sticky="ew", padx=14, pady=(12, 0))
        self._wrap_to_panel(summary, inner, 4)

        tk.Frame(inner, bg=BORDER, height=1).grid(row=10, column=0, sticky="ew", padx=14, pady=(14, 10))
        self._build_create_controls(inner, row=11)

    def _build_create_controls(self, parent: tk.Misc, *, row: int) -> None:
        create_card = self._card(parent)
        create_card.grid(row=row, column=0, sticky="ew", padx=14, pady=(0, 14))
        create_card.columnconfigure(0, weight=1)
        self._label(create_card, "创建 PQC 草稿", size=12, weight="bold", color=PRIMARY, row=0, column=0, sticky="w", padx=14, pady=(10, 8))
        notice = tk.Label(create_card, text="保存前自动复核记录，避免重复创建。", font=self._font(9), fg=TEXT, bg=WARNING_BG, anchor="w", padx=10, pady=7, highlightthickness=1, highlightbackground="#F2C46F")
        notice.grid(row=1, column=0, sticky="ew", padx=14)
        self._wrap_to_panel(notice, create_card, 52)
        self.confirm_check = tk.Checkbutton(create_card, text="我已确认设备与检验类型", variable=self.confirm_var, font=self._font(9), fg=TEXT, bg=CARD, activebackground=CARD, selectcolor=WHITE, anchor="w")
        self.confirm_check.grid(row=2, column=0, sticky="w", padx=14, pady=(8, 6))
        self._wrap_to_panel(self.confirm_check, create_card, 60)
        self.create_button = self._button(create_card, "创建已勾选草稿（0）", self._create_selected, kind="success")
        self.create_button.grid(row=3, column=0, sticky="ew", padx=14)
        self._label(create_card, textvariable=self.count_var, size=9, color=MUTED, row=4, column=0, sticky="w", padx=14, pady=(8, 8))
        utility = tk.Frame(create_card, bg=CARD)
        utility.grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 12))
        for column in range(3):
            utility.columnconfigure(column, weight=1)
        output_button = self._button(utility, "输出目录", self._open_output_dir)
        self.reset_button = self._button(utility, "重置界面", self._reset)
        log_button = self._button(utility, "查看日志", self._view_log)
        for column, button in enumerate((output_button, self.reset_button, log_button)):
            # 底部三个常用操作需与上方按钮保持可读字号，避免高 DPI/缩放
            # 下文字过小；缩小内边距以确保 410px 左栏内不发生溢出。
            button.configure(font=self._font(11), padx=2, pady=5)
            button.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 4, 0))

    def _build_devices_card(self) -> None:
        card = self._card(self.devices_pane)
        self.devices_card = card
        card.grid(row=0, column=0, sticky="nsew")
        card.rowconfigure(1, weight=1)
        card.columnconfigure(0, weight=1)
        header = tk.Frame(card, bg=CARD)
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 8))
        header.columnconfigure(0, weight=0)
        header.columnconfigure(1, weight=1)
        self._label(header, "设备与检验类型", size=12, weight="bold", row=0, column=0, sticky="w")
        self._build_device_header_controls(header)

        # 保留内部 01--05 槽位键，并在扭矩后增加类别模板业务列；
        # 表头只展示业务标签，避免把 UI 槽位编号误认为真实 SS。
        columns = ("select", "excel", "item", "sequence", "01", "02", "03", "04", "05", "special", "ncr", "category", "status")
        self.tree = ttk.Treeview(card, columns=columns, show="headings", selectmode="browse", style="Pqc.Treeview")
        style = ttk.Style(self)
        self._pqc_tree_style = "Pqc.Treeview"
        style.configure(self._pqc_tree_style, background=WHITE, fieldbackground=WHITE, foreground=TEXT, rowheight=30, borderwidth=1, relief=tk.SOLID, font=self._font(11))
        style.configure("Pqc.Treeview.Heading", background="#F6F9FD", foreground=TEXT, relief=tk.FLAT, font=self._font(11, "bold"), padding=(8, 12))
        style.map("Pqc.Treeview", background=[("selected", "#E8F1FF")], foreground=[("selected", TEXT)])
        self.tree.tag_configure("anomaly", foreground=DANGER)
        self.tree.tag_configure("blocked", foreground=DANGER)
        self.tree.tag_configure("completed", foreground=SUCCESS, background=SUCCESS_BG)
        self.tree.tag_configure("odd", background="#F8FBFF")
        self.tree.tag_configure("even", background=WHITE)
        headings = (
            ("select", "选择", PQC_COLUMN_WIDTH_DEFAULTS["select"]),
            ("excel", "Excel行", PQC_COLUMN_WIDTH_DEFAULTS["excel"]),
            ("item", "盘柜名称/编号", PQC_COLUMN_WIDTH_DEFAULTS["item"]),
            ("sequence", "序列码/流转码", PQC_COLUMN_WIDTH_DEFAULTS["sequence"]),
            # 这些宽度按中文整词预留，防止标签换行或被相邻列挤压。
            ("01", "装配", PQC_COLUMN_WIDTH_DEFAULTS["01"]),
            ("02", "接线", PQC_COLUMN_WIDTH_DEFAULTS["02"]),
            ("03", "上电前", PQC_COLUMN_WIDTH_DEFAULTS["03"]),
            ("04", "上电后", PQC_COLUMN_WIDTH_DEFAULTS["04"]),
            ("05", "扭矩", PQC_COLUMN_WIDTH_DEFAULTS["05"]),
            ("special", "特殊情况", PQC_COLUMN_WIDTH_DEFAULTS["special"]),
            ("ncr", "NCR", PQC_COLUMN_WIDTH_DEFAULTS["ncr"]),
            ("category", "类别模板", PQC_COLUMN_WIDTH_DEFAULTS["category"]),
            ("status", "状态", PQC_COLUMN_WIDTH_DEFAULTS["status"]),
        )
        for column, label, width in headings:
            self.tree.heading(column, text=label)
            self.tree.column(
                column,
                width=self._pqc_column_widths.get(column, width),
                minwidth=PQC_COLUMN_WIDTH_LIMITS[column][0],
                anchor=tk.CENTER if column in {"select", "excel", "01", "02", "03", "04", "05", "category"} else tk.W,
                # 类别模板是设备类型对应的检验模板匹配结果列；无匹配
                # 显示 ``—``。它吸收宽屏剩余空间，状态列始终固定在最右。
                stretch=(column == "category"),
            )
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(0, 0))
        scroll = ttk.Scrollbar(card, orient=tk.VERTICAL, command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.device_xscroll = ttk.Scrollbar(card, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.device_xscroll.grid(row=2, column=0, sticky="ew")
        self.tree.configure(xscrollcommand=self.device_xscroll.set)
        self.tree.bind("<Button-1>", self._on_tree_click)
        # Treeview 原生支持拖动表头分隔线；释放鼠标后读取实际宽度并原子保存。
        self.tree.bind("<ButtonRelease-1>", self._on_tree_column_resize, add="+")
        # 状态列是表格最右侧的固定业务列。拦截它左边的分隔线，避免
        # ttk 原生拖拽把状态列或其左侧的类别模板列一起重新分配宽度。
        self.tree.bind("<ButtonPress-1>", self._on_tree_column_resize_start, add="+")
        # ttk 的 DoubleClick 类绑定会直接调用内部 Press，不经过
        # ButtonPress 绑定；双击 separator 也必须走同一状态列拦截。
        self.tree.bind("<Double-Button-1>", self._on_tree_column_resize_start, add="+")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        legend = self._label(card, "已完成：已有正式记录，不再创建；—：不适用；草稿：可人工选择；⚠：需人工判断", size=9, color=MUTED, row=3, column=0, sticky="ew", padx=14, pady=8)
        self._wrap_to_panel(legend, card)

    def _build_right_column(self) -> None:
        return None

    def _build_device_header_controls(self, parent: tk.Misc) -> None:
        # 标题后只保留一个连续的左对齐控件组，避免控件漂到标题行中间
        # 或把批量选择按钮贴到表格右上角。
        controls = tk.Frame(parent, bg=CARD)
        controls.grid(row=0, column=1, sticky="w", padx=(20, 0))
        self.type_bulk_box = ttk.Combobox(
            controls,
            width=8,
            textvariable=self.type_bulk_var,
            values=tuple(PQC_SLOT_LABELS[code] for code in PQC_UI_SLOT_CODES),
            state="readonly",
            style="Pqc.TCombobox",
            font=self._font(10),
        )
        self.type_bulk_box.grid(row=0, column=0, sticky="w")
        self.select_type_button = self._button(controls, "选择此类", self._select_current_type)
        self.select_type_button.grid(row=0, column=1, padx=(6, 0))
        self.clear_type_button = self._button(controls, "取消此类", self._clear_current_type)
        self.clear_type_button.grid(row=0, column=2, padx=(6, 0))
        self.select_all_button = self._button(controls, "一键全选", self._select_all)
        self.select_all_button.grid(row=0, column=3, padx=(12, 0))
        self.clear_selection_button = self._button(controls, "一键取消", self._clear_selection)
        self.clear_selection_button.grid(row=0, column=4, padx=(6, 0))
        # 将设备表列宽恢复到源码定义的默认值，并同步覆盖已保存的用户偏好。
        self.restore_column_widths_button = self._button(
            controls, "一键恢复默认列宽", self._restore_default_column_widths
        )
        self.restore_column_widths_button.grid(row=0, column=5, padx=(12, 0))
        for button in (
            self.select_all_button,
            self.clear_selection_button,
            self.select_type_button,
            self.clear_type_button,
            self.restore_column_widths_button,
        ):
            button.configure(font=self._font(10), padx=8, pady=4)

    def set_engineers(self, engineers: Iterable[Engineer]) -> None:
        items = tuple(engineers)
        self._engineers_by_display = {item.display_name: item for item in items}
        self.engineer_box["values"] = ("请选择预配置人员", *(item.display_name for item in items))
        if self.engineer_var.get() not in self._engineers_by_display:
            self.engineer_var.set("请选择预配置人员")

    def on_show(self) -> None:
        self._context_epoch += 1
        self.state_model.on_show()
        self.confirm_var.set(True)
        self._refresh_tree()
        self._refresh_tool_tree()
        self._update_action_states()
        self.after_idle(self._restore_layout_widths)

    @staticmethod
    def _clamp_layout_widths(widths: Mapping[str, int], total_width: int) -> dict[str, int]:
        minimums = {"scope": PQC_LAYOUT_WIDTH_MIN, "devices": 760}
        if 1 < total_width < sum(minimums.values()):
            scope_width = max(1, min(PQC_SCOPE_WIDTH_MAX, int(round(total_width * 0.34))))
            return {"scope": scope_width, "devices": max(1, total_width - scope_width)}
        target = {
            "scope": min(
                PQC_SCOPE_WIDTH_MAX,
                max(minimums["scope"], int(widths.get("scope", PQC_LAYOUT_WIDTH_DEFAULTS["scope"]))),
            ),
            "devices": max(minimums["devices"], int(widths.get("devices", PQC_LAYOUT_WIDTH_DEFAULTS.get("devices", 1030)))),
        }
        if total_width > 0:
            # 保持左栏紧凑宽度，剩余空间全部给右侧设备表，避免按总宽度同比放大左栏。
            target["devices"] = max(minimums["devices"], total_width - target["scope"])
        return target

    def _current_layout_widths(self) -> dict[str, int]:
        pane_widgets = (self.scope_pane, self.devices_pane)
        return {
            key: max(1, int(widget.winfo_width()))
            for key, widget in zip(PQC_LAYOUT_WIDTH_KEYS, pane_widgets)
            if widget.winfo_exists()
        }

    def _restore_layout_widths(self) -> None:
        if not getattr(self, "layout_pane", None) or not self.winfo_exists():
            return
        total_width = int(self.layout_pane.winfo_width())
        if total_width <= 1:
            self._pending_layout_restore = True
            return
        widths = self._clamp_layout_widths(self._pqc_layout_widths, total_width)
        if sum(widths.values()) <= 0:
            return
        left = widths["scope"]
        try:
            self.layout_pane.sashpos(0, left)
        except tk.TclError:
            self._pending_layout_restore = True
        else:
            self._pending_layout_restore = False

    def _on_layout_configure(self, _event: tk.Event[tk.Misc]) -> None:
        if self._pending_layout_restore:
            self.after_idle(self._restore_layout_widths)

    def _save_layout_widths(self) -> None:
        widths = self._current_layout_widths()
        if len(widths) < 2:
            return
        normalized = self._normalize_pqc_layout_widths(widths)
        self._pqc_layout_widths = normalized
        try:
            saved, warning = self._preference_store.save_pqc_layout_widths(normalized)
        except Exception as exc:
            saved, warning = False, f"PQC 两栏宽度无法保存：{type(exc).__name__}"
        if not saved:
            self._log("pqc_layout_width_save", status="WARNING", detail=warning or "PQC 两栏宽度未保存")

    def _on_layout_pane_resize(self, event: tk.Event[tk.Misc]) -> None:
        if self.layout_pane.identify(event.x, event.y) not in {"", "sash"}:
            return
        self._save_layout_widths()

    def _guard_context_callback(
        self,
        context_epoch: int,
        callback: Callable[[object, BaseException | None], None],
    ) -> Callable[[object, BaseException | None], None]:
        """Drop stale async results that belong to a prior visible PQC session."""

        def wrapped(result: object, error: BaseException | None) -> None:
            if context_epoch != self._context_epoch:
                return
            callback(result, error)

        return wrapped

    def set_busy(self, busy: bool) -> None:
        self._busy_flag = bool(busy)
        state = tk.DISABLED if busy else tk.NORMAL
        for widget in (
            self.confirm_check,
            self.select_all_button,
            self.clear_selection_button,
            self.select_type_button,
            self.clear_type_button,
        ):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        self.engineer_box.configure(state="disabled" if busy else "readonly")
        self._update_action_states()

    def _log(self, name: str, *, status: str = "", detail: str = "", **fields: object) -> None:
        callback = getattr(self.owner, "_append_log", None)
        if callable(callback):
            callback(name, status=status or None, detail=detail, module="PQC", **fields)

    def _run_async(self, operation: str, function: Callable[[], object], callback: Callable[[object, BaseException | None], None]) -> None:
        getattr(self.owner, "_set_busy")(True)
        getattr(self.owner, "_run_async")(operation, function, callback)

    def _choose_source(self) -> None:
        selected = filedialog.askopenfilename(title="选择 PQC Excel", filetypes=(("Excel 工作簿", "*.xlsx *.xlsm"), ("所有文件", "*.*")))
        if selected:
            self.source_var.set(selected)
            self._preference_store.save_pqc_source_path(selected)

    def _selected_engineer(self) -> Engineer | None:
        return self._engineers_by_display.get(self.engineer_var.get())

    def _query_projects(self) -> None:
        if self._busy_flag:
            return
        path = Path(self.source_var.get().strip())
        engineer = self._selected_engineer()
        if not path.is_file():
            messagebox.showerror("输入错误", "请选择存在的 PQC Excel 文件。")
            return
        if engineer is None:
            messagebox.showerror("输入错误", "请选择预配置质检工程师。")
            return
        self._log("pqc_project_query_start", status="STARTED", detail="按工程师读取 PQC 项目")

        def work() -> object:
            workbook = read_pqc_workbook(path)
            return workbook, projects_for_engineer(workbook, engineer.name), engineer

        context_epoch = self._context_epoch
        self._run_async("pqc_projects", work, self._guard_context_callback(context_epoch, self._projects_finished))

    def _projects_finished(self, result: object, error: BaseException | None) -> None:
        if error is not None:
            detail = str(error) if isinstance(error, PqcExcelError) else f"读取 PQC Excel 失败：{type(error).__name__}"
            self._log("pqc_project_query_result", status="ERROR", detail=detail)
            messagebox.showerror("PQC Excel 读取失败", detail)
            return
        workbook, projects, engineer = result  # type: ignore[misc]
        self.state_model.set_projects(workbook.path, engineer.name, projects)
        self._projects_by_label = {project.label: project for project in projects}
        self.project_box["values"] = tuple(self._projects_by_label)
        self.project_box.configure(state="readonly" if projects else "disabled")
        if len(projects) == 1:
            self.project_var.set(projects[0].label)
        else:
            self.project_var.set("请选择项目" if projects else "未查询到项目")
        if not projects:
            messagebox.showinfo("查询结果", "该 Excel 中没有所选工程师的项目。")
        self.summary_var.set(f"0 台设备  ·  {len(projects)} 个项目  ·  0 份待创建草稿")
        for detail in workbook.errors:
            self._log("pqc_excel_row_excluded", status="WARNING", detail=detail)
        self._log("pqc_project_query_result", status="SUCCESS", detail=f"已查询到 {len(projects)} 个项目")
        self._refresh_tree()
        self._update_action_states()

    def _on_project_selected(self, _event: object = None) -> None:
        project = self._projects_by_label.get(self.project_var.get())
        if project is None:
            return
        self.state_model.select_project(project.project_number)
        self._refresh_tree()
        self.summary_var.set(f"0 台设备  ·  {len(self.state_model.projects)} 个项目  ·  0 份待创建草稿")
        self._update_action_states()

    def _load_devices(self) -> None:
        if self._busy_flag:
            return
        project = self._projects_by_label.get(self.project_var.get())
        if project is None:
            messagebox.showerror("输入错误", "请先选择查询到的 PQC 项目。")
            return
        if self.query_service is None:
            messagebox.showerror("运行时未配置", "PQC 只读数据库查询尚未配置。")
            return
        self.state_model.select_project(project.project_number)
        self._log("pqc_candidate_load_start", status="STARTED", detail="开始实时加载设备、模板、草稿和工器具")

        def work() -> object:
            snapshot = self.query_service.load_project_snapshot(project.project_number)
            return build_device_candidates(project, snapshot), snapshot.tools

        context_epoch = self._context_epoch
        self._run_async("pqc_devices", work, self._guard_context_callback(context_epoch, self._devices_finished))

    def _devices_finished(self, result: object, error: BaseException | None) -> None:
        if error is not None:
            detail = f"加载 PQC 设备候选失败：{type(error).__name__}；{error}"
            self._log("pqc_candidate_load_result", status="ERROR", detail=detail)
            messagebox.showerror("加载失败", detail)
            return
        candidates, tools = result  # type: ignore[misc]
        self.state_model.load_candidates(candidates)
        self._tools = tuple(tools)
        self._tools_by_id = {item.tool_id: item for item in self._tools}
        self._current_item = ""
        self._current_type = ""
        self._pending_tool_ids.clear()
        self._pending_tool_key = None
        self._refresh_tree()
        self._refresh_tool_tree()
        count = self.state_model.selected_count()
        self.summary_var.set(f"{len(candidates)} 台设备  ·  {len(self.state_model.projects)} 个项目  ·  {count} 份待创建草稿")
        self._log("pqc_candidate_load_result", status="SUCCESS", detail=f"已加载 {len(candidates)} 台设备，生成 {count} 份候选草稿")

    @staticmethod
    def _cell_text(candidate: object) -> str:
        state = getattr(candidate, "state")
        if state is PqcCandidateState.CANDIDATE:
            return "☑" if getattr(candidate, "selected") else "☐"
        if state is PqcCandidateState.FORMAL:
            return "已完成"
        if state is PqcCandidateState.DRAFT:
            return "☑" if getattr(candidate, "selected") else "草稿"
        if state is PqcCandidateState.EXCLUDED:
            return "—"
        if state is PqcCandidateState.ANOMALY:
            return "☑" if getattr(candidate, "selected") else "异常"
        return "阻断"

    def _wrap_tree_values(self, values: tuple[object, ...]) -> tuple[str, ...]:
        """Pre-wrap visible cells using current pixel widths.

        ttk.Treeview has one style-level row height rather than per-item
        heights, so the refresh path wraps text and recalculates a safe global
        row height. This keeps long special/NCR/category values readable while
        preserving the existing Treeview business bindings.
        """
        keys = tuple(PQC_COLUMN_WIDTH_KEYS)
        wrapped: list[str] = []
        for key, value in zip(keys, values):
            text = "" if value is None else str(value)
            try:
                width = int(self.tree.column(key, "width"))
            except (AttributeError, tk.TclError, TypeError, ValueError):
                width = int(PQC_COLUMN_WIDTH_DEFAULTS.get(key, 100))
            chars = max(4, width // 10)
            wrapped.append("\n".join(textwrap.wrap(text, width=chars, break_long_words=True, break_on_hyphens=False)) or "")
        return tuple(wrapped)

    def _refresh_tree_row_height(self, rows: Iterable[tuple[str, ...]]) -> None:
        max_lines = max((text.count("\n") + 1 for row in rows for text in row), default=1)
        ttk.Style(self).configure(self._pqc_tree_style, rowheight=max(30, min(120, max_lines * 18)))

    def _device_values(self, device: PqcDeviceCandidate) -> tuple[object, ...]:
        selectable = [item for item in device.types if item.selectable]
        selected = [item for item in selectable if item.selected]
        row_mark = "—" if not selectable else ("☑" if len(selected) == len(selectable) else "☐")
        blocked_items = [item for item in device.types if item.state is PqcCandidateState.BLOCKED]
        blocked = blocked_items[0] if blocked_items else None
        anomaly = next((item for item in device.types if item.state is PqcCandidateState.ANOMALY), None)
        completed = sum(item.state is PqcCandidateState.FORMAL for item in device.types)
        if blocked is not None:
            # A device may have several independently blocked slots. Keep all
            # reasons visible in the row instead of silently showing the first.
            blocked_details = "；".join(
                f"{item.code}: {item.detail}" for item in blocked_items
            )
            status = f"阻断：{blocked_details}"
        elif anomaly is not None:
            status = f"异常：{device.source.special_case or anomaly.detail}"
        elif selected:
            status = "待创建"
        elif completed:
            status = "已完成" if all(item.state in {PqcCandidateState.FORMAL, PqcCandidateState.EXCLUDED} for item in device.types) else f"已完成 {completed} 项"
        else:
            status = "无选择"
        if completed and (blocked is not None or anomaly is not None or selected):
            status = f"已完成 {completed} 项；{status}"
        return (
            row_mark,
            ",".join(map(str, device.source.source_rows)),
            device.source.item_code,
            device.source.sequence_code,
            *(self._cell_text(device.type_candidate(code)) for code in PQC_UI_SLOT_CODES if code != "CATEGORY"),
            device.source.special_case,
            device.source.ncr_code,
            device.source.category or "—",
            status,
        )

    def _refresh_tree(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._devices_by_iid.clear()
        wrapped_rows: list[tuple[str, ...]] = []
        for index, device in enumerate(self.state_model.candidates):
            iid = f"pqc-device-{index}"
            self._devices_by_iid[iid] = device.source.item_code
            tags = ["odd" if index % 2 else "even"]
            if any(item.state is PqcCandidateState.BLOCKED for item in device.types):
                tags.append("blocked")
            elif any(item.state is PqcCandidateState.ANOMALY for item in device.types):
                tags.append("anomaly")
            elif any(item.state is PqcCandidateState.FORMAL for item in device.types) and all(item.state in {PqcCandidateState.FORMAL, PqcCandidateState.EXCLUDED} for item in device.types):
                tags.append("completed")
            else:
                pass
            values = self._wrap_tree_values(self._device_values(device))
            wrapped_rows.append(values)
            self.tree.insert("", tk.END, iid=iid, values=values, tags=tuple(tags))
        self._refresh_tree_row_height(wrapped_rows)
        if self.state_model.candidates:
            self.summary_var.set(
                f"{len(self.state_model.candidates)} 台设备  ·  {len(self.state_model.projects)} 个项目  ·  "
                f"{self.state_model.selected_count()} 份待创建草稿"
            )
        self._update_action_states()

    def _set_current_context(self, item_code: str, type_code: str) -> None:
        """Select one device/type and reload only that key's saved tools.

        ``_pending_tool_ids`` deliberately represents unsaved edits for a
        single key.  Switching either the device or type must discard those
        edits and hydrate from ``PqcUiState.tool_selections``; otherwise tools
        chosen for one draft can visually leak into another draft.
        """

        key = (str(item_code).strip(), str(type_code).strip())
        self._current_item, self._current_type = key
        if key != self._pending_tool_key:
            self._pending_tool_ids = {
                item.tool_id for item in self.state_model.tools_for_type(*key)
            }
            self._pending_tool_key = key

    def _on_tree_click(self, event: tk.Event[tk.Misc]) -> None:
        iid = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        item_code = self._devices_by_iid.get(iid)
        if not item_code or self._busy_flag:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        if column == "#1":
            device = next(item for item in self.state_model.candidates if item.source.item_code == item_code)
            selectable = [item for item in device.types if item.selectable]
            select = not selectable or not all(item.selected for item in selectable)
            self.state_model.set_device_selected(item_code, select)
            self._refresh_tree()
            return
        # Treeview 的第 12 个业务列映射到 CATEGORY 槽位，不能把它当作 06；
        # 特殊情况/NCR 仅展示源 Excel 字段，不触发业务选择。
        type_columns = {"#5": "01", "#6": "02", "#7": "03", "#8": "04", "#9": "05", "#12": "CATEGORY"}
        type_code = type_columns.get(column)
        if type_code:
            candidate = next(item for item in self.state_model.candidates if item.source.item_code == item_code).type_candidate(type_code)
            if candidate.state is PqcCandidateState.FORMAL:
                self._current_item, self._current_type = item_code, type_code
                self._pending_tool_ids.clear()
                self._pending_tool_key = None
                self._refresh_tool_tree()
                self._update_action_states()
                return
            self._set_current_context(item_code, type_code)
            try:
                self.state_model.toggle_type(item_code, type_code)
            except PqcUiStateError as exc:
                # Keep the blocked event actionable: the generic exception text
                # often omits the device/slot that was clicked, while the row
                # may contain several independent BLOCKED candidates.
                detail = f"设备 {item_code} / 槽位 {type_code}：{exc}"
                self._log("pqc_selection_blocked", status="WARNING", detail=detail)
            self._refresh_tree()
            self._refresh_tool_tree()

    def _on_tree_column_resize_start(self, event: tk.Event[tk.Misc]) -> str | None:
        """禁止拖动状态列左边界；其它列仍可按需调整。"""

        tree = getattr(self, "tree", None)
        if tree is None:
            return None
        try:
            if tree.identify_region(event.x, event.y) != "separator":
                return None
            # Treeview.identify_column 在分隔线处返回分隔线左侧的列。
            # 因此状态列左边界会返回类别模板列（#10），右边界会返回
            # 状态列（#11）。两侧都必须拦截；此前只拦截 #11，实际拖动
            # 状态列左边界时事件落在 #10，Treeview 默认拖动仍会生效。
            column = str(tree.identify_column(event.x))
            if column in {"#12", "#13", "#-1"}:
                return "break"
        except (AttributeError, KeyError, TypeError, tk.TclError, ValueError):
            return None
        return None

    def _on_tree_column_resize(self, event: tk.Event[tk.Misc]) -> None:
        """持久化用户拖拽后的 PQC 列宽；非法/损坏值安全回退。"""

        try:
            if self.tree.identify_region(event.x, event.y) not in {"heading", "separator"}:
                return
            widths = {
                key: int(self.tree.column(key, "width"))
                for key in PQC_COLUMN_WIDTH_KEYS
                if key != "status"
            }
        except (AttributeError, KeyError, TypeError, ValueError):
            return
        normalized = self._normalize_pqc_column_widths(widths)
        # 无论 release 事件带回什么值，状态列都恢复固定宽度且不参与偏好保存。
        normalized["status"] = PQC_STATUS_COLUMN_WIDTH
        self._pqc_column_widths = normalized
        for key, width in normalized.items():
            current = int(self.tree.column(key, "width"))
            if current != width:
                self.tree.column(key, width=width)
        persisted = {key: width for key, width in normalized.items() if key != "status"}
        try:
            saved, warning = self._preference_store.save_pqc_column_widths(persisted)
        except Exception as exc:
            saved, warning = False, f"PQC 列宽无法保存：{type(exc).__name__}"
        if not saved:
            self._log("pqc_column_width_save", status="WARNING", detail=warning or "PQC 列宽未保存")

    def _restore_default_column_widths(self) -> None:
        """只恢复 PQC 设备表列宽，不触碰路径、工程师或选择状态。"""

        normalized = self._normalize_pqc_column_widths(PQC_COLUMN_WIDTH_DEFAULTS)
        self._pqc_column_widths = normalized
        tree = getattr(self, "tree", None)
        if tree is not None:
            for key, width in normalized.items():
                try:
                    tree.column(key, width=width)
                except tk.TclError:
                    pass
        # 状态列是固定业务列，只在运行时恢复其宽度，不把它写入用户偏好。
        persisted = {key: width for key, width in normalized.items() if key != "status"}
        try:
            saved, warning = self._preference_store.save_pqc_column_widths(persisted)
        except Exception as exc:
            saved, warning = False, f"PQC 列宽无法恢复：{type(exc).__name__}"
        if saved:
            self._log("pqc_column_width_restore", status="SUCCESS", detail="已恢复 PQC 表格默认列宽")
        else:
            self._log("pqc_column_width_restore", status="WARNING", detail=warning or "PQC 默认列宽未保存")

    def _on_tool_column_resize(self, event: tk.Event[tk.Misc]) -> None:
        """保持接口兼容；当右侧工具列不存在时不进行持久化。"""

        if not self._tools_ui_enabled:
            return

        try:
            if self.tool_tree.identify_region(event.x, event.y) not in {"heading", "separator"}:
                return
            widths = {
                key: int(self.tool_tree.column(key, "width"))
                for key in PQC_TOOL_COLUMN_WIDTH_KEYS
            }
        except (AttributeError, KeyError, TypeError, ValueError):
            return
        normalized = self._normalize_pqc_tool_column_widths(widths)
        self._pqc_tool_column_widths = normalized
        for key, width in normalized.items():
            if widths.get(key) != width:
                self.tool_tree.column(key, width=width)
        try:
            saved, warning = self._preference_store.save_pqc_tool_column_widths(normalized)
        except Exception as exc:
            saved, warning = False, f"PQC 工器具列宽无法保存：{type(exc).__name__}"
        if not saved:
            self._log("pqc_tool_column_width_save", status="WARNING", detail=warning or "PQC 工器具列宽未保存")

    def _on_tree_select(self, _event: object = None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        item_code = self._devices_by_iid.get(selected[0], "")
        if not item_code:
            return
        device = next(item for item in self.state_model.candidates if item.source.item_code == item_code)
        type_code = self._current_type
        if item_code == self._current_item and type_code and device.type_candidate(type_code).state is PqcCandidateState.FORMAL:
            # selection_set queues this event after the cell click. Preserve
            # the completed cell instead of switching to a creatable type.
            self._refresh_tool_tree()
            self._update_action_states()
            return
        if not type_code or not device.type_candidate(type_code).selectable:
            type_code = next((item.code for item in device.types if item.selectable), "")
        if type_code:
            self._set_current_context(item_code, type_code)
        else:
            self._current_item = item_code
            self._current_type = ""
            self._pending_tool_ids.clear()
            self._pending_tool_key = None
        self._refresh_tool_tree()

    def _refresh_tool_tree(self) -> None:
        if not self._tools_ui_enabled:
            self.current_device_var.set("请选择设备")
            self.current_type_var.set("请选择检验类型")
            self._pending_tool_ids.clear()
            self._pending_tool_key = None
            return
        for iid in self.tool_tree.get_children():
            self.tool_tree.delete(iid)
        if not self._current_item or not self._current_type:
            self.current_device_var.set("请选择设备")
            self.current_type_var.set("请选择检验类型")
            self._pending_tool_ids.clear()
            self._pending_tool_key = None
            return
        key = (self._current_item, self._current_type)
        self.current_device_var.set(self._current_item)
        # 右侧必须展示精确模板名称，尤其是类别模板列不可显示虚构子项。
        current_candidate = next(
            item for item in self.state_model.candidates
            if item.source.item_code == self._current_item
        ).type_candidate(self._current_type)
        if current_candidate.state is PqcCandidateState.FORMAL:
            self.current_type_var.set(f"{current_candidate.inspection_type.label} · 已完成，已有正式记录")
            self._pending_tool_ids.clear()
            self._pending_tool_key = None
            return
        if key != self._pending_tool_key:
            self._set_current_context(*key)
        # 右侧当前类型同样只显示业务标签；槽位键仅用于内部模板身份匹配。
        slot_label = {"01": "装配", "02": "接线", "03": "上电前", "04": "上电后", "05": "扭矩", "CATEGORY": "类别模板"}[self._current_type]
        exact_name = current_candidate.inspection_type.procedure_name
        self.current_type_var.set(f"{slot_label}  ·  {exact_name}" if exact_name else slot_label)
        for tool in sort_tools_for_type(self._tools, self._current_type, date.today()):
            validity = tool.validity_on(date.today())
            mark = "已选" if tool.tool_id in self._pending_tool_ids else "未选"
            self.tool_tree.insert("", tk.END, iid=f"tool-{tool.tool_id}", values=(mark, tool.display_text(), validity.value))

    def _on_tool_click(self, event: tk.Event[tk.Misc]) -> None:
        if not self._tools_ui_enabled:
            return
        iid = self.tool_tree.identify_row(event.y)
        if not iid.startswith("tool-") or self._busy_flag:
            return
        tool = self._tools_by_id.get(iid[5:])
        if tool is None:
            return
        try:
            candidate = next(
                item for item in self.state_model.candidates
                if item.source.item_code == self._current_item
            ).type_candidate(self._current_type)
        except (StopIteration, KeyError):
            return
        if not candidate.selectable:
            return
        if tool.validity_on(date.today()) is PqcToolValidity.EXPIRED:
            messagebox.showwarning("工器具已过期", "过期工器具不可选择。")
            return
        if tool.tool_id in self._pending_tool_ids:
            self._pending_tool_ids.remove(tool.tool_id)
        else:
            self._pending_tool_ids.add(tool.tool_id)
        if not self._persist_current_tools(log=False):
            self._pending_tool_ids = {item.tool_id for item in self.state_model.tools_for_type(self._current_item, self._current_type)}
        self._refresh_tool_tree()

    def _current_sorted_tools(self) -> tuple[PqcTool, ...]:
        if not self._current_type:
            return ()
        return sort_tools_for_type(self._tools, self._current_type, date.today())

    def _persist_current_tools(self, *, log: bool = True) -> bool:
        if not self._current_item or not self._current_type:
            return False
        tools = tuple(
            tool
            for tool in self._current_sorted_tools()
            if tool.tool_id in self._pending_tool_ids
        )
        try:
            self.state_model.set_tools(self._current_item, self._current_type, tools, completion_day=date.today())
        except PqcUiStateError as exc:
            messagebox.showwarning("无法保存", str(exc))
            return False
        if log:
            self._log("pqc_tools_saved", status="SUCCESS", detail=f"已保存当前设备与类型的 {len(tools)} 件工器具")
        self._refresh_tree()
        return True

    def _save_tools(self) -> bool:
        if not self._current_item or not self._current_type:
            messagebox.showinfo("工器具", "请先点击设备的检验类型。")
            return False
        # Persist in the same deterministic order shown to the operator; a set
        # is used only for checkbox membership, never for payload ordering.
        return self._persist_current_tools()

    def _select_all_tools(self) -> None:
        if not self._tools_ui_enabled:
            return
        if not self._current_item or not self._current_type:
            messagebox.showinfo("工器具", "请先点击设备的检验类型。")
            return
        valid_ids = {
            tool.tool_id
            for tool in self._current_sorted_tools()
            if tool.validity_on(date.today()) is not PqcToolValidity.EXPIRED
        }
        self._pending_tool_ids = valid_ids
        self._persist_current_tools(log=False)
        self._refresh_tool_tree()

    def _clear_tools(self) -> None:
        if not self._tools_ui_enabled:
            return
        if not self._current_item or not self._current_type:
            messagebox.showinfo("工器具", "请先点击设备的检验类型。")
            return
        self._pending_tool_ids.clear()
        self._persist_current_tools(log=False)
        self._refresh_tool_tree()

    def _copy_tools(self) -> None:
        if not self._tools_ui_enabled:
            return
        if not self._save_tools() or not self._current_item or not self._current_type:
            return
        try:
            copied = self.state_model.copy_tools_to_selected_type(self._current_item, self._current_type, completion_day=date.today())
        except PqcUiStateError as exc:
            messagebox.showwarning("复制失败", str(exc))
            return
        self._log("pqc_tools_copied", status="SUCCESS", detail=f"工器具配置已复制到 {copied} 台同类型已选设备")
        self._refresh_tree()

    def _select_all(self) -> None:
        for device in tuple(self.state_model.candidates):
            self.state_model.set_device_selected(device.source.item_code, True)
        self._refresh_tree()

    def _clear_selection(self) -> None:
        for device in tuple(self.state_model.candidates):
            self.state_model.set_device_selected(device.source.item_code, False)
        self._refresh_tree()

    def _bulk_type_code(self) -> str:
        by_label = {label: code for code, label in PQC_SLOT_LABELS.items()}
        return by_label.get(self.type_bulk_var.get(), "01")

    def _set_current_type_selected(self, selected: bool) -> None:
        type_code = self._bulk_type_code()
        for device in tuple(self.state_model.candidates):
            candidate = device.type_candidate(type_code)
            if candidate.selectable:
                self.state_model.toggle_type(device.source.item_code, type_code, selected)
        self._refresh_tree()
        self._refresh_tool_tree()

    def _select_current_type(self) -> None:
        self._set_current_type_selected(True)

    def _clear_current_type(self) -> None:
        self._set_current_type_selected(False)

    def _sync_confirmation(self) -> None:
        self.state_model.confirmed = bool(self.confirm_var.get())
        self._update_action_states()

    def _create_selected(self) -> None:
        if self._current_item and self._current_type:
            try:
                current_selectable = next(
                    item for item in self.state_model.candidates
                    if item.source.item_code == self._current_item
                ).type_candidate(self._current_type).selectable
            except (StopIteration, KeyError):
                current_selectable = False
            if current_selectable:
                self._persist_current_tools(log=False)
        selections = self.state_model.selected_drafts()
        engineer = self._selected_engineer()
        if not self.confirm_var.get() or not selections:
            messagebox.showerror("创建门禁", "请确认配置并至少勾选一份 PQC 草稿。")
            return
        if engineer is None:
            messagebox.showerror("创建门禁", "质检工程师配置无效。")
            return
        if self.query_service is None or self.api_client is None:
            messagebox.showerror("创建门禁", "processEnd API 或只读查询未配置；未发送 POST。")
            return
        service = PqcDraftCreationService(self.query_service, self.api_client)
        self._log("pqc_create_start", status="STARTED", detail=f"开始本机串行创建 {len(selections)} 份 PQC 草稿")
        context_epoch = self._context_epoch
        self._run_async(
            "pqc_create",
            # 传递所选 Engineer，由创建服务只取姓名写入
            # procedureDraftQcEngineer；creator/dept 由后端登录用户决定。
            lambda: service.create_serially(selections, qc_engineer=engineer),
            self._guard_context_callback(context_epoch, self._create_finished),
        )

    def _create_finished(self, result: object, error: BaseException | None) -> None:
        if error is not None:
            detail = f"PQC 创建编排异常：{type(error).__name__}"
            self._log("pqc_create_result", status="ERROR", detail=detail)
            messagebox.showerror("创建异常", detail)
            return
        creation = result if isinstance(result, PqcCreationBatch) else PqcCreationBatch()
        self.state_model.apply_creation(creation)
        success = creation.count(PqcCreationStatus.SUCCESS)
        failed = creation.count(PqcCreationStatus.FAILED)
        skipped = creation.count(PqcCreationStatus.SKIPPED)
        unknown = creation.count(PqcCreationStatus.UNKNOWN)
        self.count_var.set(f"成功 {success}  ·  失败 {failed}  ·  跳过 {skipped}  ·  待确认 {unknown}")
        identity_failures = [
            item.detail
            for item in creation.results
            if item.status is PqcCreationStatus.FAILED
            and "API " in item.detail
            and any(marker in item.detail for marker in ("身份", "登录用户", "当前用户"))
        ]
        for item in creation.results:
            self._log(
                "pqc_create_item_result",
                status=item.status.value,
                detail=item.detail,
                item_code=item.item_code,
                type_code=item.type_code,
                template_key=item.template_key,
                procedure_code=item.procedure_code,
            )
        self._log("pqc_create_result", status="SUCCESS" if not failed and not unknown else "WARNING", detail=self.count_var.get())
        if identity_failures:
            # Identity errors contain no credential/header values.  Surface the
            # fail-closed reason instead of leaving it only in the audit log.
            messagebox.showerror("API 身份阻断", identity_failures[0])

    def _reset(self) -> bool:
        if self._busy_flag:
            return False
        self.state_model.reset_transient()
        self.confirm_var.set(True)
        self._current_item = ""
        self._current_type = ""
        self._pending_tool_ids.clear()
        self._pending_tool_key = None
        self.count_var.set("成功 0  ·  失败 0  ·  跳过 0  ·  待确认 0")
        self.summary_var.set(f"0 台设备  ·  {len(self.state_model.projects)} 个项目  ·  0 份待创建草稿")
        self._refresh_tree()
        self._refresh_tool_tree()
        self._log("pqc_ui_reset", status="SUCCESS", detail="已保留 Excel、工程师、项目、当前页签和日志；已清空 PQC 运行态")
        return True

    def _open_output_dir(self) -> None:
        callback = getattr(self.owner, "_open_output_dir", None)
        if callable(callback):
            callback()

    def _view_log(self) -> None:
        callback = getattr(self.owner, "_view_log", None)
        if callable(callback):
            callback()

    def _update_action_states(self) -> None:
        busy = self._busy_flag
        engineer_ready = self._selected_engineer() is not None
        project_ready = self.project_var.get() in self._projects_by_label
        source_ready = bool(self.source_var.get().strip())
        source_entry = getattr(self, "source_entry", None)
        if source_entry is not None:
            source_entry.configure(state=tk.NORMAL if not busy else tk.DISABLED)
        choose_button = getattr(self, "choose_button", None)
        if choose_button is not None:
            choose_button.configure(state=tk.NORMAL if not busy else tk.DISABLED)
        self.query_button.configure(state=tk.NORMAL if not busy and source_ready and engineer_ready else tk.DISABLED)
        self.project_box.configure(state="disabled" if busy or not self._projects_by_label else "readonly")
        self.load_button.configure(state=tk.NORMAL if not busy and project_ready and self.query_service is not None else tk.DISABLED)
        self.reset_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        has_candidates = bool(self.state_model.candidates)
        self.select_all_button.configure(state=tk.NORMAL if not busy and has_candidates else tk.DISABLED)
        self.clear_selection_button.configure(state=tk.NORMAL if not busy and has_candidates else tk.DISABLED)
        self.type_bulk_box.configure(state="readonly" if not busy and has_candidates else "disabled")
        self.select_type_button.configure(state=tk.NORMAL if not busy and has_candidates else tk.DISABLED)
        self.clear_type_button.configure(state=tk.NORMAL if not busy and has_candidates else tk.DISABLED)
        count = self.state_model.selected_count()
        self.create_button.configure(text=f"创建已勾选草稿（{count}）", state=tk.NORMAL if not busy and count and self.confirm_var.get() and self.api_client is not None else tk.DISABLED)


__all__ = ["PqcPage"]
