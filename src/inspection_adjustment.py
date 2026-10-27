"""送检信息调整的领域模型和 Tk 页面。"""

from __future__ import annotations

import os
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Mapping, Sequence


INSPECTION_ADJUSTMENT_MODULE = "INSPECTION_ADJUSTMENT"
INSPECTION_ADJUSTMENT_LABEL = "送检信息调整"
DEFAULT_A_PATH = ""
DEFAULT_B_PATH = ""
SOURCE_SHEET_NAME = "质检信息导出-1"
TARGET_DEFAULT_SHEET_NAME = "IQC统计"
SYSTEM_YELLOW = "FFF2CC"
TARGET_HEADERS: tuple[str, ...] = (
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
    "",
    "未完成",
    "结果",
    "机柜/盘台",
    "单号",
)

# 目标列使用 1-based Excel 列号，便于和需求表逐项核对。
SOURCE_TO_TARGET_COLUMNS: Mapping[int, str] = {
    1: "C",
    2: "A",
    4: "G",
    5: "H",
    6: "Q",
    10: "L",
    20: "D",
}
UNMAPPED_TARGET_COLUMNS: tuple[int, ...] = tuple(
    column for column in range(1, 21) if column not in SOURCE_TO_TARGET_COLUMNS
)


class InspectionAdjustmentError(RuntimeError):
    """送检信息调整的可见业务错误。"""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        requires_reread: bool = False,
        preserve_temp: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.requires_reread = requires_reread
        self.preserve_temp = preserve_temp


@dataclass(frozen=True)
class InspectionRecord:
    """一条已经完成源列到目标列映射的新增记录。"""

    source_row: int
    identifier: str
    target_values: tuple[object, ...]


@dataclass(frozen=True)
class InspectionAdjustmentPreview:
    """读取和去重后的只读计划，不代表已经写入 B。"""

    source_path: Path
    target_path: Path
    target_sheet: str
    target_fingerprint: str
    scanned_count: int
    valid_count: int
    new_records: tuple[InspectionRecord, ...]
    duplicate_count: int
    anomaly_count: int
    conflict_identifiers: tuple[str, ...]
    target_last_data_row: int
    existing_rows: tuple[tuple[object, ...], ...]

    @property
    def new_count(self) -> int:
        return len(self.new_records)


@dataclass(frozen=True)
class InspectionAdjustmentResult:
    """正式同步及回读核对结果。"""

    source_path: Path
    target_path: Path
    target_sheet: str
    scanned_count: int
    new_count: int
    duplicate_count: int
    anomaly_count: int
    written: bool
    backup_path: Path | None
    new_rows: tuple[tuple[str, int], ...]


def normalized_identifier(value: object) -> str:
    """只去首尾空格，不做大小写、全角半角或模糊转换。"""

    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).strip()
    if isinstance(value, float) and value.is_integer():
        return str(int(value)).strip()
    return str(value).strip()


def is_valid_identifier(value: object) -> bool:
    """拒绝空值、Excel 错误值和不可写入的控制字符。"""

    identifier = normalized_identifier(value)
    if not identifier or identifier.startswith("#"):
        return False
    return not any(ord(char) < 32 for char in identifier if char not in "\t")


def default_path_for_a(preference: object) -> str:
    value = str(getattr(preference, "last_inspection_adjustment_a_path", "") or "").strip()
    return value or DEFAULT_A_PATH


def default_path_for_b(preference: object) -> str:
    value = str(getattr(preference, "last_inspection_adjustment_b_path", "") or "").strip()
    return value or DEFAULT_B_PATH


class InspectionAdjustmentPage(tk.Frame):
    """第三个同级页面；耗时工作交给 IqcApp 的统一后台线程。"""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        owner: object,
        service: object | None,
        preference_store: object,
        preference: object,
    ) -> None:
        super().__init__(parent, bg="#F4F8FD")
        self.owner = owner
        self.service = service
        self.preference_store = preference_store
        self._preview: InspectionAdjustmentPreview | None = None
        self._path_change_guard = False
        self.a_path_var = tk.StringVar(value=default_path_for_a(preference))
        self.b_path_var = tk.StringVar(value=default_path_for_b(preference))
        self.a_status_var = tk.StringVar()
        self.b_status_var = tk.StringVar()
        self.summary_var = tk.StringVar(value="尚未读取")
        self.rows_var = tk.StringVar(value="—")
        self.detail_var = tk.StringVar(value="请先读取并预览 A/B 工作簿")
        self._controls: list[tk.Misc] = []
        self._build()
        self.a_path_var.trace_add("write", self._on_path_changed)
        self.b_path_var.trace_add("write", self._on_path_changed)
        self._refresh_path_status()

    def _font(self, size: int, weight: str = "normal") -> tuple[str, int, str]:
        font_factory = getattr(self.owner, "_font", None)
        if callable(font_factory):
            return font_factory(size, weight)
        return ("Microsoft YaHei UI", size, weight)

    def _card(self, parent: tk.Misc) -> tk.Frame:
        factory = getattr(self.owner, "_card", None)
        if callable(factory):
            return factory(parent, padx=16, pady=16)
        return tk.Frame(
            parent,
            bg="#FFFFFF",
            highlightbackground="#DCE6F2",
            highlightcolor="#DCE6F2",
            highlightthickness=1,
            bd=0,
        )

    def _button(self, parent: tk.Misc, text: str, command: Callable[[], None], *, kind: str = "outline") -> tk.Button:
        factory = getattr(self.owner, "_button", None)
        if callable(factory):
            return factory(parent, text, command, kind=kind)
        return tk.Button(parent, text=text, command=command)

    def _label(
        self,
        parent: tk.Misc,
        text: str,
        *,
        size: int = 11,
        weight: str = "normal",
        color: str = "#1F2937",
        **grid: object,
    ) -> tk.Label:
        factory = getattr(self.owner, "_label", None)
        if callable(factory):
            return factory(parent, text, size=size, weight=weight, color=color, **grid)
        label = tk.Label(parent, text=text, font=self._font(size, weight), fg=color, bg=parent.cget("bg"))
        if grid:
            label.grid(**grid)
        return label

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        title_card = self._card(self)
        title_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        padx, pady = getattr(title_card, "_content_padding", (16, 16))
        title_inner = tk.Frame(title_card, bg="#FFFFFF")
        title_inner.pack(fill=tk.BOTH, expand=True, padx=padx, pady=pady)
        self._label(title_inner, INSPECTION_ADJUSTMENT_LABEL, size=15, weight="bold", row=0, column=0, sticky="w")
        self._label(
            title_inner,
            "只处理脱敏/选定的 A、B .xlsx 文件；正式同步前会生成预览和 B 指纹校验。",
            size=10,
            color="#64748B",
            row=1,
            column=0,
            sticky="w",
            pady=(5, 0),
        )

        body_card = self._card(self)
        body_card.grid(row=1, column=0, sticky="nsew")
        body_card.grid_columnconfigure(0, weight=1)
        body_card.grid_rowconfigure(2, weight=1)
        padx, pady = getattr(body_card, "_content_padding", (16, 16))
        inner = tk.Frame(body_card, bg="#FFFFFF")
        inner.pack(fill=tk.BOTH, expand=True, padx=padx, pady=pady)
        inner.columnconfigure(1, weight=1)
        inner.rowconfigure(4, weight=1)

        self._label(inner, "A · 1455质检信息", size=12, weight="bold", row=0, column=0, sticky="w", pady=(0, 6))
        a_row = tk.Frame(inner, bg="#FFFFFF")
        a_row.grid(row=1, column=0, columnspan=3, sticky="ew")
        a_row.columnconfigure(0, weight=1)
        self.a_entry = tk.Entry(
            a_row,
            textvariable=self.a_path_var,
            font=self._font(10),
            fg="#1F2937",
            bg="#FBFDFF",
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#C9D7E8",
            highlightcolor="#2563EB",
        )
        self.a_entry.grid(row=0, column=0, sticky="ew", ipady=8)
        self.a_choose_button = self._button(a_row, "选择文件", self._choose_a)
        self.a_choose_button.grid(row=0, column=1, padx=(12, 0), sticky="ew")
        self.a_status_label = tk.Label(a_row, textvariable=self.a_status_var, font=self._font(9), fg="#64748B", bg="#FFFFFF", anchor="w")
        self.a_status_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self._label(inner, "B · IQC统计", size=12, weight="bold", row=2, column=0, sticky="w", pady=(18, 6))
        b_row = tk.Frame(inner, bg="#FFFFFF")
        b_row.grid(row=3, column=0, columnspan=3, sticky="ew")
        b_row.columnconfigure(0, weight=1)
        self.b_entry = tk.Entry(
            b_row,
            textvariable=self.b_path_var,
            font=self._font(10),
            fg="#1F2937",
            bg="#FBFDFF",
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#C9D7E8",
            highlightcolor="#2563EB",
        )
        self.b_entry.grid(row=0, column=0, sticky="ew", ipady=8)
        self.b_choose_button = self._button(b_row, "选择文件", self._choose_b)
        self.b_choose_button.grid(row=0, column=1, padx=(12, 0), sticky="ew")
        self.b_status_label = tk.Label(b_row, textvariable=self.b_status_var, font=self._font(9), fg="#64748B", bg="#FFFFFF", anchor="w")
        self.b_status_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        action_row = tk.Frame(inner, bg="#FFFFFF")
        action_row.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(22, 0))
        action_row.columnconfigure(0, weight=1)
        action_row.rowconfigure(2, weight=1)
        self.summary_label = self._label(action_row, "尚未读取", size=12, weight="bold", row=0, column=0, sticky="w")
        self.summary_var.trace_add("write", lambda *_args: self.summary_label.configure(text=self.summary_var.get()))
        buttons = tk.Frame(action_row, bg="#FFFFFF")
        buttons.grid(row=0, column=1, sticky="e")
        self.read_button = self._button(buttons, "读取/预览", self._read_preview, kind="primary")
        self.read_button.grid(row=0, column=0, padx=(0, 8))
        self.sync_button = self._button(buttons, "开始同步", self._start_sync, kind="success")
        self.sync_button.grid(row=0, column=1)

        self._label(action_row, "新增记录对应的 B 行号", size=10, color="#64748B", row=1, column=0, sticky="w", pady=(16, 2))
        self._label(action_row, "", size=10, color="#1F2937", row=1, column=1, sticky="e", pady=(16, 2))
        rows_label = tk.Label(action_row, textvariable=self.rows_var, font=self._font(10), fg="#1F2937", bg="#FFFFFF", anchor="e", justify="right")
        rows_label.grid(row=2, column=0, columnspan=2, sticky="ne")

        self.detail_text = tk.Text(
            action_row,
            height=7,
            font=self._font(10),
            fg="#1F2937",
            bg="#FBFDFF",
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#C9D7E8",
            highlightcolor="#2563EB",
            wrap=tk.WORD,
        )
        self.detail_text.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        self.detail_text.insert("1.0", self.detail_var.get())
        self.detail_text.configure(state=tk.DISABLED)
        self.detail_var.trace_add("write", lambda *_args: self._set_detail(self.detail_var.get()))

        tree_frame = tk.Frame(action_row, bg="#FFFFFF")
        tree_frame.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.preview_tree = ttk.Treeview(tree_frame, columns=("identifier", "row"), show="headings", height=8)
        self.preview_tree.heading("identifier", text="新增唯一识别号")
        self.preview_tree.heading("row", text="B 行号")
        self.preview_tree.column("identifier", width=300, anchor="w")
        self.preview_tree.column("row", width=100, anchor="center")
        self.preview_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.preview_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.preview_tree.configure(yscrollcommand=scrollbar.set)

        self._controls.extend(
            [
                self.a_entry,
                self.b_entry,
                self.a_choose_button,
                self.b_choose_button,
                self.read_button,
                self.sync_button,
            ]
        )

    def _set_detail(self, text: str) -> None:
        if not hasattr(self, "detail_text"):
            return
        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state=tk.DISABLED)

    @staticmethod
    def _path_status(path_text: str) -> tuple[bool, str]:
        text = path_text.strip()
        if not text:
            return False, "未选择文件"
        path = Path(text)
        if path.suffix.lower() != ".xlsx":
            return False, "不可用：首期仅支持 .xlsx"
        try:
            if not path.is_file():
                return False, "不可用：文件不存在或当前账号无读取权限"
        except OSError as exc:
            return False, f"不可用：{type(exc).__name__}"
        return True, "可用"

    def _refresh_path_status(self) -> None:
        for path_var, status_var, label in (
            (self.a_path_var, self.a_status_var, self.a_status_label),
            (self.b_path_var, self.b_status_var, self.b_status_label),
        ):
            valid, detail = self._path_status(path_var.get())
            status_var.set(detail)
            label.configure(fg="#16A34A" if valid else "#D97706")

    def _on_path_changed(self, *_args: object) -> None:
        self._refresh_path_status()
        if self._path_change_guard or self._preview is None:
            return
        self._preview = None
        self.rows_var.set("—")
        self.summary_var.set("路径已改变，请重新读取/预览")
        self._set_detail("A 或 B 路径已改变，旧预览已作废；请重新读取。")

    def _choose_file(self, variable: tk.StringVar, title: str, save_method: str) -> None:
        selected = filedialog.askopenfilename(
            title=title,
            filetypes=(("Excel 工作簿 (*.xlsx)", "*.xlsx"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        valid, detail = self._path_status(selected)
        if not valid:
            messagebox.showerror("文件不可用", detail)
            return
        self._path_change_guard = True
        try:
            variable.set(selected)
        finally:
            self._path_change_guard = False
        saved, warning = getattr(self.preference_store, save_method)(selected)
        if not saved:
            self._log("inspection_adjustment_preference_warning", status="WARNING", detail=warning or "路径偏好无法保存")
        self._refresh_path_status()

    def _choose_a(self) -> None:
        self._choose_file(self.a_path_var, "选择 A · 1455质检信息", "save_inspection_adjustment_a_path")

    def _choose_b(self) -> None:
        self._choose_file(self.b_path_var, "选择 B · IQC统计", "save_inspection_adjustment_b_path")

    def _validate_paths(self) -> tuple[Path, Path] | None:
        a_path = Path(self.a_path_var.get().strip())
        b_path = Path(self.b_path_var.get().strip())
        a_valid, a_detail = self._path_status(str(a_path))
        b_valid, b_detail = self._path_status(str(b_path))
        if not a_valid:
            messagebox.showerror("A 文件不可用", f"A · 1455质检信息：{a_detail}\n路径：{a_path}")
            return None
        if not b_valid:
            messagebox.showerror("B 文件不可用", f"B · IQC统计：{b_detail}\n路径：{b_path}")
            return None
        return a_path, b_path

    def _persist_valid_paths(self) -> None:
        for path_var, method_name in (
            (self.a_path_var, "save_inspection_adjustment_a_path"),
            (self.b_path_var, "save_inspection_adjustment_b_path"),
        ):
            valid, _detail = self._path_status(path_var.get())
            if not valid:
                continue
            saved, warning = getattr(self.preference_store, method_name)(path_var.get().strip())
            if not saved:
                self._log("inspection_adjustment_preference_warning", status="WARNING", detail=warning or "路径偏好无法保存")

    def save_preferences(self) -> None:
        """关闭窗口时只保存已经确认可用的 A/B 路径。"""

        self._persist_valid_paths()

    def _service_method(self, name: str) -> Callable[..., object] | None:
        method = getattr(self.service, name, None)
        return method if callable(method) else None

    def _read_preview(self) -> None:
        if bool(getattr(self.owner, "_busy")()):
            return
        paths = self._validate_paths()
        if paths is None:
            return
        method = self._service_method("build_preview")
        if method is None:
            messagebox.showerror("功能不可用", "openpyxl 依赖或送检信息调整服务尚未配置。")
            return
        self._persist_valid_paths()
        self._preview = None
        self.rows_var.set("—")
        a_path, b_path = paths
        self._log(
            "inspection_adjustment_read_start",
            status="STARTED",
            detail="开始读取 A/B 工作簿并生成只读预览",
            a_path=str(a_path),
            b_path=str(b_path),
        )
        self._set_shared_status("读取/预览中…")
        getattr(self.owner, "_set_busy")(True)
        getattr(self.owner, "_run_async")(
            "inspection_adjustment_read",
            lambda: method(a_path, b_path),
            self._preview_finished,
        )

    def _preview_finished(self, result: object, error: BaseException | None) -> None:
        if error is not None:
            detail = f"读取/预览失败：{type(error).__name__}；{str(error)}"
            self._log("inspection_adjustment_error", status="ERROR", detail=detail, error_type=type(error).__name__)
            self.summary_var.set("读取失败")
            self._set_detail(detail)
            self._set_shared_status("读取失败，请检查路径、Sheet 和表头")
            messagebox.showerror("读取/预览失败", f"{detail}\n\n请检查 A/B 文件后重新读取。")
            return
        if not isinstance(result, InspectionAdjustmentPreview):
            detail = "读取/预览返回了无法识别的结果，未继续同步"
            self._log("inspection_adjustment_error", status="ERROR", detail=detail, error_type="InvalidPreview")
            self.summary_var.set("读取失败")
            self._set_detail(detail)
            self._set_shared_status(detail)
            messagebox.showerror("读取/预览失败", detail)
            return
        self._preview = result
        self.summary_var.set(
            f"读取 {result.scanned_count} 条；可用 {result.valid_count} 条；"
            f"新增 {result.new_count} 条；重复 {result.duplicate_count} 条；异常 {result.anomaly_count} 条"
        )
        expected_rows = [
            (record.identifier, result.target_last_data_row + index)
            for index, record in enumerate(result.new_records, start=1)
        ]
        self._fill_preview_rows(expected_rows)
        row_text = "、".join(str(row) for _identifier, row in expected_rows) or "无新增行"
        self.rows_var.set(row_text)
        conflict_text = f"；冲突唯一识别号 {len(result.conflict_identifiers)} 个" if result.conflict_identifiers else ""
        self._set_detail(
            f"目标 Sheet：{result.target_sheet}；预览不写入 B。"
            f"本次新增将在历史数据末尾连续追加，新增批次按 B!T 升序排序{conflict_text}。"
        )
        self._set_shared_status("预览完成：确认结果后可开始同步")
        self._log(
            "inspection_adjustment_read_result",
            status="SUCCESS",
            detail="送检信息调整预览完成",
            row_count=result.scanned_count,
            valid_count=result.valid_count,
            new_count=result.new_count,
            duplicate_count=result.duplicate_count,
            anomaly_count=result.anomaly_count,
            target_sheet=result.target_sheet,
        )

    def _fill_preview_rows(self, rows: Sequence[tuple[str, int]]) -> None:
        for item_id in self.preview_tree.get_children():
            self.preview_tree.delete(item_id)
        for identifier, row_number in rows:
            self.preview_tree.insert("", tk.END, values=(identifier, row_number))

    def _start_sync(self) -> None:
        if bool(getattr(self.owner, "_busy")()):
            return
        preview = self._preview
        if preview is None:
            messagebox.showwarning("尚未预览", "请先读取/预览 A/B 工作簿。")
            return
        if os.path.normcase(str(Path(self.a_path_var.get().strip()))) != os.path.normcase(str(preview.source_path)):
            messagebox.showwarning("预览已失效", "A 路径已改变，请重新读取/预览。")
            return
        if os.path.normcase(str(Path(self.b_path_var.get().strip()))) != os.path.normcase(str(preview.target_path)):
            messagebox.showwarning("预览已失效", "B 路径已改变，请重新读取/预览。")
            return
        method = self._service_method("synchronize")
        if method is None:
            messagebox.showerror("功能不可用", "openpyxl 依赖或送检信息调整服务尚未配置。")
            return
        self._log(
            "inspection_adjustment_sync_start",
            status="STARTED",
            detail="开始执行临时保存、原文件替换和回读核对",
            target_sheet=preview.target_sheet,
            new_count=preview.new_count,
        )
        self._set_shared_status("同步中…")
        getattr(self.owner, "_set_busy")(True)
        getattr(self.owner, "_run_async")(
            "inspection_adjustment_sync",
            lambda: method(preview),
            self._sync_finished,
        )

    def _sync_finished(self, result: object, error: BaseException | None) -> None:
        if error is not None:
            detail = f"同步失败：{type(error).__name__}；{str(error)}"
            self._log("inspection_adjustment_error", status="ERROR", detail=detail, error_type=type(error).__name__)
            if bool(getattr(error, "requires_reread", False)):
                self._preview = None
                self.summary_var.set("预览已失效，请重新读取")
                self.rows_var.set("—")
            else:
                self.summary_var.set("同步失败，可关闭占用后重试")
            self._set_detail(detail + "；未显示同步成功，请关闭 WPS/释放占用后选择重试或重新读取。")
            self._set_shared_status("同步失败，未确认写入结果")
            messagebox.showerror(
                "同步失败",
                f"{detail}\n\n未显示同步成功。请关闭 WPS 或处理权限后选择重试/重新读取。",
            )
            return
        if not isinstance(result, InspectionAdjustmentResult):
            detail = "同步返回了无法识别的结果，未显示成功"
            self._log("inspection_adjustment_error", status="ERROR", detail=detail, error_type="InvalidResult")
            self._set_detail(detail)
            self._set_shared_status(detail)
            messagebox.showerror("同步失败", detail)
            return
        self.summary_var.set(
            f"完成：读取 {result.scanned_count} 条；新增 {result.new_count} 条；"
            f"重复 {result.duplicate_count} 条；异常 {result.anomaly_count} 条"
        )
        row_text = "、".join(str(row) for _identifier, row in result.new_rows) or "无新增行"
        self.rows_var.set(row_text)
        self._fill_preview_rows(result.new_rows)
        backup_text = str(result.backup_path) if result.backup_path else "无备份（直接更新原文件）"
        self._set_detail(
            f"目标 Sheet：{result.target_sheet}；已完成临时文件替换和回读核对。"
            f"备份：{backup_text}"
        )
        self._set_shared_status("同步完成：新增记录已回读核对")
        self._log(
            "inspection_adjustment_sync_result",
            status="SUCCESS",
            detail="送检信息调整同步完成并通过回读核对",
            target_sheet=result.target_sheet,
            new_count=result.new_count,
            duplicate_count=result.duplicate_count,
            anomaly_count=result.anomaly_count,
            backup_path=backup_text,
        )

    def _set_shared_status(self, text: str) -> None:
        status_var = getattr(self.owner, "status_var", None)
        if status_var is not None:
            status_var.set(text)
        header = getattr(self.owner, "header_status", None)
        if header is not None:
            header.configure(text=text)

    def _log(self, event: str, *, status: str, detail: str, **fields: object) -> None:
        append_log = getattr(self.owner, "_append_log", None)
        if callable(append_log):
            append_log(event, status=status, detail=detail, module=INSPECTION_ADJUSTMENT_MODULE, **fields)

    def set_busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        for control in self._controls:
            try:
                control.configure(state=state)
            except (tk.TclError, AttributeError):
                pass

    def on_show(self) -> None:
        self._refresh_path_status()


__all__ = [
    "DEFAULT_A_PATH",
    "DEFAULT_B_PATH",
    "INSPECTION_ADJUSTMENT_LABEL",
    "INSPECTION_ADJUSTMENT_MODULE",
    "InspectionAdjustmentError",
    "InspectionAdjustmentPage",
    "InspectionAdjustmentPreview",
    "InspectionAdjustmentResult",
    "InspectionRecord",
    "SOURCE_SHEET_NAME",
    "SOURCE_TO_TARGET_COLUMNS",
    "SYSTEM_YELLOW",
    "TARGET_DEFAULT_SHEET_NAME",
    "TARGET_HEADERS",
    "UNMAPPED_TARGET_COLUMNS",
    "default_path_for_a",
    "default_path_for_b",
    "is_valid_identifier",
    "normalized_identifier",
]
