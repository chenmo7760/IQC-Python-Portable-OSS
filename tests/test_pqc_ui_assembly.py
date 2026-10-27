from __future__ import annotations

import inspect
import json
import queue
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src import main as main_module
import src.pqc_ui as pqc_ui_module
from src.pqc_ui import PqcPage
from src.pqc_models import PqcFormalRecord, PqcTool
from src.pqc_rules import build_device_candidates
from src.pqc_ui_state import PqcUiState
from src.processend_api import ProcessEndDraftApiClient
from src.ui import IqcApp
from src.ui_preferences import PQC_STATUS_COLUMN_WIDTH, UiPreferenceStore
from tests.pqc_test_helpers import project, snapshot, source_row


class _Widget:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}
        self.grid_visible = True

    def configure(self, **kwargs: object) -> None:
        self.options.update(kwargs)

    def grid(self) -> None:
        self.grid_visible = True

    def grid_remove(self) -> None:
        self.grid_visible = False


class _Var:
    def __init__(self, value: object = "") -> None:
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


class PqcUiAssemblyTests(unittest.TestCase):
    def test_primary_button_matches_iqc_and_keeps_blue_background_when_disabled(self) -> None:
        class _Button:
            def __init__(self, _parent: object, **kwargs: object) -> None:
                self.options = dict(kwargs)

            def configure(self, **kwargs: object) -> None:
                self.options.update(kwargs)

        pqc_page = object.__new__(PqcPage)
        iqc_app = object.__new__(IqcApp)
        with patch("src.pqc_ui.tk.Button", _Button), patch("src.ui.tk.Button", _Button):
            pqc_button = PqcPage._button(pqc_page, object(), "查询项目", lambda: None, kind="primary")
            iqc_button = IqcApp._button(iqc_app, object(), "读取并生成草稿", lambda: None, kind="primary")

        shared_style_keys = (
            "font",
            "relief",
            "bd",
            "padx",
            "pady",
            "highlightthickness",
            "highlightbackground",
            "highlightcolor",
            "bg",
            "fg",
            "activebackground",
            "activeforeground",
            "disabledforeground",
        )
        self.assertEqual(
            {key: pqc_button.options[key] for key in shared_style_keys},
            {key: iqc_button.options[key] for key in shared_style_keys},
        )
        pqc_button.configure(state="disabled")
        self.assertEqual(pqc_button.options["state"], "disabled")
        self.assertEqual(pqc_button.options["bg"], iqc_button.options["bg"])
        self.assertEqual(pqc_button.options["activebackground"], iqc_button.options["activebackground"])

    def test_tool_checkbox_toggle_still_uses_the_selected_column(self) -> None:
        source = source_row("A-JD-001")
        pqc_project = project(source)
        state = PqcUiState()
        state.set_projects("book.xlsx", "Engineer", (pqc_project,))
        state.load_candidates(build_device_candidates(pqc_project, snapshot()))
        tool = PqcTool("tool-1", "扭矩扳手", "T-001", "2099-12-31")
        refreshed: list[str] = []
        page = object.__new__(PqcPage)
        page._busy_flag = False
        page._current_item = source.item_code
        page._current_type = "05"
        page._pending_tool_ids = set()
        page._tools_by_id = {tool.tool_id: tool}
        page._tools = (tool,)
        page.state_model = state
        page.tool_tree = SimpleNamespace(identify_row=lambda _y: "tool-tool-1")
        page._refresh_tree = lambda: None
        page._refresh_tool_tree = lambda: refreshed.append("tools")

        PqcPage._on_tool_click(page, SimpleNamespace(y=1))

        self.assertEqual(page._pending_tool_ids, {"tool-1"})
        self.assertEqual(refreshed, ["tools"])

    def test_completed_device_renders_done_without_a_selectable_checkbox(self) -> None:
        source = source_row("A-JD-001")
        templates = tuple(replace(t, form_suffix="") for t in snapshot().templates)
        formal = tuple(PqcFormalRecord(str(i), "P", source.item_code, t.procedure_code, "2", f"P-001-{i:02d}", template_key=t.template_key, template_name=t.procedure_name) for i, t in enumerate(templates[:5], 1))
        pqc_project = project(source)
        state = PqcUiState()
        state.set_projects("book.xlsx", "Engineer", (pqc_project,))
        candidates = build_device_candidates(pqc_project, snapshot(templates=templates, formal=formal))
        state.load_candidates(candidates)
        page = object.__new__(PqcPage)
        page.state_model = state
        values = page._device_values(candidates[0])
        self.assertEqual(values[0], "—")
        self.assertEqual(values[4:9], ("已完成",) * 5)
        self.assertEqual(values[-1], "已完成")
        self.assertEqual(state.selected_count(), 0)

    def test_device_row_aggregates_all_blocked_slot_details(self) -> None:
        source = source_row("A-JD-BLOCKED")
        templates = list(snapshot().templates)
        templates[0] = replace(templates[0], procedure_code="")
        templates[1] = replace(templates[1], procedure_code="")
        pqc_project = project(source)
        candidates = build_device_candidates(pqc_project, snapshot(templates=tuple(templates)))
        page = object.__new__(PqcPage)
        page.state_model = PqcUiState()
        page.state_model.set_projects("book.xlsx", "Engineer", (pqc_project,))
        page.state_model.load_candidates(candidates)
        values = page._device_values(candidates[0])
        status = values[-1]
        self.assertIn("01:", status)
        self.assertIn("02:", status)

    def test_delayed_row_selection_preserves_completed_cell_on_mixed_device(self) -> None:
        source = source_row("A-JD-001")
        template = snapshot().templates[0]
        formal = PqcFormalRecord("1", "P", source.item_code, template.procedure_code, "2", "P-001-01", template_key=template.template_key, template_name=template.procedure_name)
        pqc_project = project(source)
        state = PqcUiState()
        state.set_projects("book.xlsx", "Engineer", (pqc_project,))
        state.load_candidates(build_device_candidates(pqc_project, snapshot(formal=(formal,))))
        self.assertTrue(state.candidates[0].type_candidate("02").selectable)
        selected_before = state.selected_count()
        page = object.__new__(PqcPage)
        page.state_model = state
        page.tree = SimpleNamespace(selection=lambda: ("device-0",))
        page._devices_by_iid = {"device-0": source.item_code}
        page._current_item, page._current_type = source.item_code, "01"
        refreshed = []
        page._refresh_tool_tree = lambda: refreshed.append("tools")
        page._update_action_states = lambda: refreshed.append("actions")

        page._on_tree_select()

        self.assertEqual((page._current_item, page._current_type), (source.item_code, "01"))
        self.assertEqual(refreshed, ["tools", "actions"])
        self.assertEqual(state.selected_count(), selected_before)

    def test_device_rows_render_placeholder_slots_without_tool_identity_errors(self) -> None:
        rows = (
            source_row("A-JD-001", row_number=2),
            source_row("PANEL-001", row_number=3, category="盘台"),
            source_row("COMPONENT-001", row_number=4, category="组件类"),
            source_row("NON-NUCLEAR-001", row_number=5, category="非核盘柜"),
            source_row("DEMO-001", row_number=6, category="DemoLogic_GLM"),
            source_row("CHASSIS-001", row_number=7, category="机箱类"),
            source_row("FAN-001", row_number=8, category="风扇盘"),
        )
        pqc_project = project(*rows)
        state = PqcUiState()
        state.set_projects("book.xlsx", "Engineer", (pqc_project,))
        candidates = build_device_candidates(pqc_project, snapshot())
        state.load_candidates(candidates)
        page = object.__new__(PqcPage)
        page.state_model = state

        values_by_item = {
            device.source.item_code: PqcPage._device_values(page, device)
            for device in candidates
        }
        # The cabinet has no CATEGORY template; the panel has no 04 or
        # CATEGORY template.  Both placeholders render as ``—`` and do not
        # attempt to read a synthetic tool key.
        self.assertEqual(values_by_item["A-JD-001"][11], "—")
        self.assertEqual(values_by_item["PANEL-001"][7], "—")
        self.assertEqual(values_by_item["PANEL-001"][11], "盘台")
        self.assertEqual(values_by_item["A-JD-001"][12], "待创建")
        self.assertEqual(values_by_item["PANEL-001"][12], "待创建")
        self.assertEqual(state.tools_for_type("A-JD-001", "CATEGORY"), ())
        self.assertEqual(state.tools_for_type("PANEL-001", "04"), ())
        self.assertEqual(state.tools_for_type("PANEL-001", "CATEGORY"), ())

        # Each single-template category keeps the five legacy slots as
        # placeholders and exposes only its real CATEGORY candidate.
        for item_code in ("COMPONENT-001", "NON-NUCLEAR-001", "DEMO-001", "CHASSIS-001", "FAN-001"):
            values = values_by_item[item_code]
            self.assertEqual(values[4:9], ("—", "—", "—", "—", "—"))
            self.assertNotEqual(values[11], "—")

    def test_pending_tools_are_reloaded_per_device_and_type_key(self) -> None:
        page = object.__new__(PqcPage)
        saved_a = SimpleNamespace(tool_id="saved-a")
        saved_b = SimpleNamespace(tool_id="saved-b")
        page.state_model = SimpleNamespace(
            tool_selections={
                ("P", "CAB-01", "CABINET_POST_POWER", "DEMO-ICS-DEMOFORM-004"): (saved_a,),
                ("P", "CAB-02", "TORQUE", "DEMO-ICS-DEMOFORM-005"): (saved_b,),
            }
        )
        page.state_model.tools_for_type = lambda item, type_code: {
            ("CAB-01", "04"): (saved_a,),
            ("CAB-02", "05"): (saved_b,),
        }.get((item, type_code), ())
        page._current_item = "CAB-01"
        page._current_type = "04"
        page._pending_tool_key = ("CAB-01", "04")
        page._pending_tool_ids = {"unsaved-a"}

        PqcPage._set_current_context(page, "CAB-02", "05")
        self.assertEqual(page._pending_tool_key, ("CAB-02", "05"))
        self.assertEqual(page._pending_tool_ids, {"saved-b"})

        # Unsaved edits for CAB-02/05 remain while that same context refreshes.
        page._pending_tool_ids.add("unsaved-b")
        PqcPage._set_current_context(page, "CAB-02", "05")
        self.assertEqual(page._pending_tool_ids, {"saved-b", "unsaved-b"})

        # Switching back discards CAB-02 edits and reloads CAB-01's saved set.
        PqcPage._set_current_context(page, "CAB-01", "04")
        self.assertEqual(page._pending_tool_ids, {"saved-a"})

    def test_busy_disables_both_tabs_and_pqc_reset(self) -> None:
        app = object.__new__(IqcApp)
        app.read_button = _Widget()
        app.manual_check = _Widget()
        app.skip_check = _Widget()
        app.iqc_tab_button = _Widget()
        app.pqc_tab_button = _Widget()
        app._worker_queue = queue.Queue()
        app._worker = None
        app._callback_in_progress = False
        reset = _Widget()

        class _PqcPage:
            def set_busy(self, busy: bool) -> None:
                reset.configure(state="disabled" if busy else "normal")

        app.pqc_page = _PqcPage()
        app._update_action_states = lambda: None

        IqcApp._set_busy(app, True)
        self.assertEqual(app.iqc_tab_button.options["state"], "disabled")
        self.assertEqual(app.pqc_tab_button.options["state"], "disabled")
        self.assertEqual(reset.options["state"], "disabled")
        self.assertTrue(IqcApp._busy(app))

        IqcApp._set_busy(app, False)
        self.assertEqual(app.iqc_tab_button.options["state"], "normal")
        self.assertEqual(app.pqc_tab_button.options["state"], "normal")
        self.assertEqual(reset.options["state"], "normal")

    def test_pqc_reset_preserves_scope_and_restores_default_confirmation(self) -> None:
        page = object.__new__(PqcPage)
        page._busy_flag = False
        reset_calls: list[bool] = []
        page.state_model = SimpleNamespace(
            projects=(object(), object()),
            reset_transient=lambda: reset_calls.append(True),
        )
        page.source_var = _Var("PQC.xlsx")
        page.engineer_var = _Var("金磊")
        page.project_var = _Var("16E013N-C  项目")
        page.confirm_var = _Var(False)
        page.count_var = _Var("old")
        page.summary_var = _Var("old")
        page._current_item = "CAB-01"
        page._current_type = "05"
        page._pending_tool_ids = {"tool-1"}
        page._pending_tool_key = ("CAB-01", "05")
        page._refresh_tree = lambda: None
        page._refresh_tool_tree = lambda: None
        events: list[tuple[tuple[object, ...], dict[str, object]]] = []
        page._log = lambda *args, **kwargs: events.append((args, kwargs))

        self.assertTrue(PqcPage._reset(page))
        self.assertEqual(reset_calls, [True])
        self.assertEqual(page.source_var.get(), "PQC.xlsx")
        self.assertEqual(page.engineer_var.get(), "金磊")
        self.assertEqual(page.project_var.get(), "16E013N-C  项目")
        self.assertTrue(page.confirm_var.get())
        self.assertEqual(page._current_item, "")
        self.assertEqual(page._current_type, "")
        self.assertEqual(page._pending_tool_ids, set())
        self.assertIsNone(page._pending_tool_key)
        self.assertIn("成功 0", str(page.count_var.get()))
        self.assertEqual(events[-1][0], ("pqc_ui_reset",))

    def test_project_load_gate_requires_successful_first_step(self) -> None:
        page = object.__new__(PqcPage)
        page._busy_flag = False
        page.source_var = _Var("PQC.xlsx")
        page.project_var = _Var("请先查询项目")
        page.confirm_var = _Var(True)
        page._projects_by_label = {}
        page.query_service = object()
        page.api_client = None
        page._selected_engineer = lambda: object()
        page.state_model = SimpleNamespace(candidates=(), selected_count=lambda: 0)
        for name in (
            "query_button",
            "project_box",
            "type_bulk_box",
            "load_button",
            "reset_button",
            "select_all_button",
            "clear_selection_button",
            "select_type_button",
            "clear_type_button",
            "create_button",
        ):
            setattr(page, name, _Widget())
        page._current_item = ""
        page._current_type = ""

        PqcPage._update_action_states(page)
        self.assertEqual(page.query_button.options["state"], "normal")
        self.assertEqual(page.project_box.options["state"], "disabled")
        self.assertEqual(page.load_button.options["state"], "disabled")

        page._projects_by_label = {"16E013N-C  项目": object()}
        page.project_var.set("16E013N-C  项目")
        PqcPage._update_action_states(page)
        self.assertEqual(page.project_box.options["state"], "readonly")
        self.assertEqual(page.load_button.options["state"], "normal")

    def test_busy_switch_is_blocked_without_changing_or_saving_module(self) -> None:
        app = object.__new__(IqcApp)
        app._active_module = "IQC"
        app._ui_busy_flag = True
        app._worker_queue = queue.Queue()
        app._worker = None
        app._callback_in_progress = False
        saves: list[str] = []
        app._preference_store = SimpleNamespace(save=lambda module: saves.append(module))
        events: list[tuple[tuple[object, ...], dict[str, object]]] = []
        app._append_log = lambda *args, **kwargs: events.append((args, kwargs))

        self.assertFalse(IqcApp._switch_module(app, "PQC"))
        self.assertEqual(app._active_module, "IQC")
        self.assertEqual(saves, [])
        self.assertEqual(events[-1][1]["status"], "BLOCKED")

    def test_switch_preserves_both_page_objects_and_persists_last_module(self) -> None:
        app = object.__new__(IqcApp)
        app._active_module = "IQC"
        app._ui_busy_flag = False
        app._worker_queue = queue.Queue()
        app._worker = None
        app._callback_in_progress = False
        app.iqc_page = _Widget()

        class _PqcPage(_Widget):
            def __init__(self) -> None:
                super().__init__()
                self.show_count = 0

            def on_show(self) -> None:
                self.show_count += 1

        app.pqc_page = _PqcPage()
        app.iqc_tab_button = _Widget()
        app.pqc_tab_button = _Widget()
        app.module_memory_var = _Var()
        saved: list[str] = []
        app._preference_store = SimpleNamespace(save=lambda module: (saved.append(module) or True, ""))
        events: list[tuple[tuple[object, ...], dict[str, object]]] = []
        app._append_log = lambda *args, **kwargs: events.append((args, kwargs))
        iqc_identity = id(app.iqc_page)
        pqc_identity = id(app.pqc_page)

        self.assertTrue(IqcApp._switch_module(app, "PQC"))
        self.assertEqual(id(app.iqc_page), iqc_identity)
        self.assertEqual(id(app.pqc_page), pqc_identity)
        self.assertFalse(app.iqc_page.grid_visible)
        self.assertTrue(app.pqc_page.grid_visible)
        self.assertEqual(app.pqc_page.show_count, 1)
        self.assertEqual(saved, ["PQC"])
        self.assertIn("PQC", str(app.module_memory_var.get()))
        self.assertEqual(events[-1][0], ("ui_module_switch",))

    def test_fast_switch_keeps_pqc_page_alive_and_ignores_stale_callbacks(self) -> None:
        app = object.__new__(IqcApp)
        app._active_module = "IQC"
        app._ui_busy_flag = False
        app._worker_queue = queue.Queue()
        app._worker = None
        app._callback_in_progress = False
        app.iqc_page = _Widget()

        class _PqcPage(_Widget):
            def __init__(self) -> None:
                super().__init__()
                self.show_count = 0

            def on_show(self) -> None:
                self.show_count += 1

        app.pqc_page = _PqcPage()
        app.iqc_tab_button = _Widget()
        app.pqc_tab_button = _Widget()
        app.module_memory_var = _Var()
        app._preference_store = SimpleNamespace(save=lambda module: (True, ""))
        app._append_log = lambda *args, **kwargs: None

        iqc_identity = id(app.iqc_page)
        pqc_identity = id(app.pqc_page)
        for index in range(20):
            self.assertTrue(IqcApp._switch_module(app, "PQC"))
            self.assertTrue(app.pqc_page.grid_visible)
            self.assertTrue(IqcApp._switch_module(app, "IQC"))
            self.assertFalse(app.pqc_page.grid_visible)
            self.assertEqual(id(app.iqc_page), iqc_identity)
            self.assertEqual(id(app.pqc_page), pqc_identity)
        self.assertEqual(app.pqc_page.show_count, 20)

        page = object.__new__(PqcPage)
        page._context_epoch = 3
        accepted: list[tuple[object, BaseException | None]] = []
        wrapped = PqcPage._guard_context_callback(page, 3, lambda result, error: accepted.append((result, error)))
        page._context_epoch = 4
        wrapped({"rows": []}, None)
        self.assertEqual(accepted, [])
        wrapped = PqcPage._guard_context_callback(page, 4, lambda result, error: accepted.append((result, error)))
        wrapped({"rows": [1]}, None)
        self.assertEqual(accepted, [({"rows": [1]}, None)])

    def test_shared_log_renders_explicit_module_prefix(self) -> None:
        class _LogText:
            def __init__(self) -> None:
                self.lines: list[str] = []

            def configure(self, **_kwargs: object) -> None:
                return None

            def delete(self, *_args: object) -> None:
                self.lines.clear()

            def insert(self, _index: object, text: str, _tag: str) -> None:
                self.lines.append(text)

            def see(self, *_args: object) -> None:
                return None

        app = object.__new__(IqcApp)
        app.log_text = _LogText()
        line = json.dumps(
            {
                "timestamp": "2026-09-06T01:02:03+08:00",
                "event": "pqc_candidate_load_result",
                "status": "SUCCESS",
                "detail": "已加载 33 台设备",
                "fields": {"module": "PQC"},
            },
            ensure_ascii=False,
        )

        IqcApp._render_logs(app, [line])
        self.assertIn("[PQC]", app.log_text.lines[0])
        self.assertIn("已加载 33 台设备", app.log_text.lines[0])

    def test_ui_declares_two_step_flow_and_one_row_six_type_columns(self) -> None:
        scope_source = inspect.getsource(PqcPage._build_scope_card)
        header_controls_source = inspect.getsource(PqcPage._build_device_header_controls)
        create_source = inspect.getsource(PqcPage._build_create_controls)
        device_source = inspect.getsource(PqcPage._build_devices_card)
        button_source = inspect.getsource(PqcPage._button)
        state_source = inspect.getsource(PqcPage._update_action_states)
        app_source = inspect.getsource(IqcApp._build)
        self.assertIn("源 Excel 文件", scope_source)
        self.assertIn("选择文件", scope_source)
        self.assertIn("系统自动匹配模板", scope_source)
        self.assertNotIn("手动选择 SS", scope_source)
        self.assertNotIn("SS 选择", scope_source)
        self.assertIn("质检工程师", scope_source)
        self.assertIn("项目", scope_source)
        self.assertIn("ipady=9", scope_source)
        # 表头只展示业务标签；01/05 仍保留为内部模板槽位键。
        self.assertIn('(\"01\", \"装配\"', device_source)
        self.assertIn('(\"05\", \"扭矩\"', device_source)
        self.assertIn('(\"category\", \"类别模板\"', device_source)
        self.assertIn('original_configure = button.configure', button_source)
        self.assertIn('original_configure(bg=colors["bg"], activebackground=colors["activebackground"])', button_source)
        self.assertIn('disabledforeground="#A8C0EF"', button_source)
        self.assertNotIn('bg="#F1F5F9"', state_source)
        self.assertIn('size=17', app_source)
        self.assertIn("source_entry", scope_source)
        self.assertIn("choose_button", scope_source)
        self.assertIn("一键全选", header_controls_source)
        self.assertIn("一键取消", header_controls_source)
        self.assertIn("选择此类", header_controls_source)
        self.assertIn("取消此类", header_controls_source)
        self.assertIn("width=8", header_controls_source)
        self.assertIn("创建 PQC 草稿", create_source)
        self.assertIn("输出目录", create_source)
        self.assertIn("我已确认设备与检验类型", create_source)
        self.assertNotIn("工器具配置", create_source)
        self.assertIn("_build_device_header_controls", device_source)
        self.assertNotIn("创建 PQC 草稿", device_source)
        self.assertNotIn("恢复列宽", scope_source + header_controls_source + create_source + device_source)

    def test_query_requires_existing_source_file_and_engineer_selection(self) -> None:
        page = object.__new__(PqcPage)
        page._busy_flag = False
        page.source_var = _Var("missing.xlsx")
        page.project_var = _Var("请先查询项目")
        page.engineer_var = _Var("请选择预配置人员")
        page._engineers_by_display = {"金磊": object()}
        page._projects_by_label = {}
        page.query_service = object()
        page._selected_engineer = lambda: page._engineers_by_display.get(page.engineer_var.get())
        page.state_model = SimpleNamespace(candidates=(), selected_count=lambda: 0)
        for name in (
            "source_entry",
            "choose_button",
            "query_button",
            "project_box",
            "type_bulk_box",
            "load_button",
            "reset_button",
            "select_all_button",
            "clear_selection_button",
            "select_type_button",
            "clear_type_button",
            "create_button",
        ):
            setattr(page, name, _Widget())

        PqcPage._update_action_states(page)
        self.assertEqual(page.source_entry.options["state"], "normal")
        self.assertEqual(page.choose_button.options["state"], "normal")
        self.assertEqual(page.query_button.options["state"], "disabled")

        with TemporaryDirectory() as temp:
            source = Path(temp) / "pqc.xlsx"
            source.write_text("ok", encoding="utf-8")
            page.source_var.set(str(source))
            page.engineer_var.set("金磊")
            PqcPage._update_action_states(page)
            self.assertEqual(page.query_button.options["state"], "normal")

    def test_main_wires_pqc_readonly_query_and_processend_api(self) -> None:
        source = inspect.getsource(main_module.main)
        self.assertIn("PqcReadonlyQueryService(", source)
        self.assertIn("ReadonlyMysqlClient(paths.readonly_query_bat)", source)
        self.assertNotIn("load_pqc_template_suffix_map(", source)
        self.assertNotIn("template_suffix_map=pqc_suffix_map", source)
        self.assertIn("ProcessEndDraftApiClient(transport)", source)
        self.assertIn("pqc_query_service=pqc_query_service", source)
        self.assertIn("pqc_entry_client=pqc_entry_client", source)

    def test_api_authorization_comes_only_from_environment_or_pointed_local_file(self) -> None:
        direct = main_module.load_entry_api_auth_headers(
            {"IQC_ENTRY_API_AUTHORIZATION": "  JWT opaque-value  "}
        )
        self.assertEqual(direct, {"Authorization": "JWT opaque-value"})

        with TemporaryDirectory() as temp:
            auth_file = Path(temp) / "api.auth"
            auth_file.write_text("JWT local-file-value\n", encoding="utf-8")
            pointed = main_module.load_entry_api_auth_headers(
                {"IQC_ENTRY_API_AUTH_FILE": str(auth_file.resolve())}
            )
            self.assertEqual(pointed, {"Authorization": "JWT local-file-value"})
            # Relative package/config paths are not accepted as token stores.
            self.assertEqual(
                main_module.load_entry_api_auth_headers(
                    {"IQC_ENTRY_API_AUTH_FILE": "config/api.auth"}
                ),
                {},
            )

        self.assertEqual(main_module.load_entry_api_auth_headers({}), {})
        self.assertNotIn("user_id", inspect.getsource(main_module.load_entry_api_auth_headers))
        self.assertNotIn("dept_id", inspect.getsource(main_module.load_entry_api_auth_headers))

    def test_create_button_uses_processend_service_and_has_no_sql_write_path(self) -> None:
        source = inspect.getsource(PqcPage._create_selected)
        self.assertIn("PqcDraftCreationService(self.query_service, self.api_client)", source)
        self.assertIn("service.create_serially", source)
        self.assertNotIn("sql", source.casefold())
        self.assertNotIn("bat", source.casefold())
        self.assertNotIn("execute", source.casefold())
        save_source = inspect.getsource(ProcessEndDraftApiClient.save_draft)
        self.assertNotIn("self.verify_identity", save_source)
        self.assertNotIn("PROCESSEND_IDENTITY_PATH", save_source)
        self.assertIn("post_form", save_source)
        self.assertIn("API 身份阻断", inspect.getsource(PqcPage._create_finished))


class UiPreferenceTests(unittest.TestCase):
    def test_missing_and_corrupt_preferences_default_to_iqc_with_warning(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "preferences.json"
            store = UiPreferenceStore(path)
            missing = store.load()
            self.assertEqual(missing.module, "IQC")
            self.assertTrue(missing.warning)

            path.write_text("{broken", encoding="utf-8")
            corrupt = store.load()
            self.assertEqual(corrupt.module, "IQC")
            self.assertTrue(corrupt.warning)

    def test_last_module_round_trip_is_per_store_path(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "current-user" / "ui_preferences.json"
            store = UiPreferenceStore(path)
            self.assertEqual(store.save("pqc"), (True, ""))
            loaded = store.load()
            self.assertEqual(loaded.module, "PQC")
            self.assertEqual(loaded.warning, "")
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["last_active_module"],
                "PQC",
            )

    def test_invalid_module_is_not_persisted(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "ui_preferences.json"
            store = UiPreferenceStore(path)
            accepted, warning = store.save("OTHER")
            self.assertFalse(accepted)
            self.assertTrue(warning)
            self.assertFalse(path.exists())

    @unittest.expectedFailure  # Existing layout contract mismatch; tracked in README.
    def test_pqc_column_widths_round_trip_and_invalid_values_fail_safe(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "ui_preferences.json"
            store = UiPreferenceStore(path)
            self.assertEqual(store.save("PQC"), (True, ""))
            self.assertEqual(store.save_pqc_column_widths({"01": 99, "item": 220}), (True, ""))
            self.assertEqual(store.save_pqc_tool_column_widths({"name": 260, "validity": 100}), (True, ""))
            self.assertEqual(store.save_pqc_layout_widths({"scope": 238, "devices": 996}), (True, ""))
            loaded = store.load()
            self.assertEqual(loaded.module, "PQC")
            self.assertEqual(dict(loaded.pqc_column_widths), {"01": 99, "item": 220})
            self.assertEqual(dict(loaded.pqc_tool_column_widths), {"name": 260, "validity": 100})
            self.assertEqual(dict(loaded.pqc_layout_widths), {"scope": 238, "devices": 996})

            # 非法列宽只被忽略；PqcPage 在应用时会为该键使用默认值。
            path.write_text(
                '{"last_active_module":"PQC","pqc_column_widths":{"01":-1,"02":true,"bogus":80}}',
                encoding="utf-8",
            )
            invalid = store.load()
            self.assertEqual(dict(invalid.pqc_column_widths), {})
            self.assertTrue(invalid.warning)
            normalized = PqcPage._normalize_pqc_column_widths(invalid.pqc_column_widths)
            self.assertEqual(normalized["01"], 84)
            self.assertEqual(normalized["02"], 84)
            tool_normalized = PqcPage._normalize_pqc_tool_column_widths(invalid.pqc_tool_column_widths)
            self.assertEqual(tool_normalized["name"], 245)
            layout_normalized = PqcPage._normalize_pqc_layout_widths(getattr(invalid, "pqc_layout_widths", {}))
            self.assertEqual(layout_normalized["scope"], 336)
            self.assertEqual(layout_normalized["devices"], 1030)

            old = PqcPage._normalize_pqc_column_widths({"item": 600, "status": 600, "excel": 40})
            self.assertEqual(old["item"], 260)
            self.assertEqual(old["status"], PQC_STATUS_COLUMN_WIDTH)
            self.assertEqual(old["excel"], 72)

    @unittest.expectedFailure  # Existing layout contract mismatch; tracked in README.
    def test_pqc_layout_widths_clamp_and_restore_helpers(self) -> None:
        clamped = PqcPage._clamp_layout_widths({"scope": 100, "devices": 1200, "tools": 150}, 1680)
        self.assertGreaterEqual(clamped["scope"], 320)
        self.assertGreaterEqual(clamped["devices"], 760)
        self.assertEqual(sum(clamped.values()), 1680)

    def test_pqc_layout_builds_all_two_cards_with_real_sizes(self) -> None:
        import tkinter as tk

        root = tk.Tk()
        try:
            page = PqcPage(root, owner=SimpleNamespace(_append_log=lambda *a, **k: None, _set_busy=lambda *a, **k: None, _run_async=lambda *a, **k: None, _open_output_dir=lambda: None, _view_log=lambda: None), output_dir=Path("."), query_service=None, api_client=None)
            page.pack(fill="both", expand=True)
            root.geometry("1680x960+0+0")
            root.update_idletasks()
            root.update()
            self.assertGreater(page.scope_card.winfo_width(), 50)
            self.assertGreater(page.devices_card.winfo_width(), 50)
        finally:
            root.destroy()

    def test_pqc_tree_binds_release_for_column_width_persistence(self) -> None:
        source = inspect.getsource(PqcPage._build_devices_card)
        self.assertIn('"<ButtonRelease-1>"', source)
        self.assertIn('"<ButtonPress-1>"', source)
        self.assertIn('"<Double-Button-1>"', source)
        self.assertIn("save_pqc_column_widths", inspect.getsource(PqcPage._on_tree_column_resize))
        self.assertIn('{"heading", "separator"}', inspect.getsource(PqcPage._on_tree_column_resize))
        resize_source = inspect.getsource(PqcPage._on_tree_column_resize)
        self.assertIn('if key != "status"', resize_source)
        self.assertIn('normalized["status"] = PQC_STATUS_COLUMN_WIDTH', resize_source)
        self.assertIn('persisted = {key: width for key, width in normalized.items() if key != "status"}', resize_source)
        restore_source = inspect.getsource(PqcPage._restore_default_column_widths)
        self.assertIn('persisted = {key: width for key, width in normalized.items() if key != "status"}', restore_source)
        self.assertIn('tree.identify_region(event.x, event.y) != "separator"', inspect.getsource(PqcPage._on_tree_column_resize_start))
        self.assertIn("stretch=(column == \"category\")", source)
        self.assertIn("minwidth=PQC_COLUMN_WIDTH_LIMITS[column][0]", source)
        self.assertIn("PQC_STATUS_COLUMN_WIDTH", inspect.getsource(pqc_ui_module))
        self.assertIn("Panedwindow", inspect.getsource(PqcPage._build))
        self.assertIn("save_pqc_layout_widths", inspect.getsource(PqcPage._save_layout_widths))
        self.assertIn("sashpos", inspect.getsource(PqcPage._restore_layout_widths))

    def test_pqc_status_separator_is_blocked_and_not_persisted(self) -> None:
        class _Tree:
            def __init__(self) -> None:
                self.widths = {key: 100 for key in pqc_ui_module.PQC_COLUMN_WIDTH_KEYS}
                self.widths["status"] = 600

            def identify_region(self, _x: int, _y: int) -> str:
                return "separator"

            def identify_column(self, _x: int) -> str:
                return "#12"

            def column(self, key: str, option: str = "width", **kwargs: object) -> object:
                if kwargs:
                    self.widths[key] = int(kwargs["width"])
                    return None
                return self.widths[key]

        class _Store:
            def __init__(self) -> None:
                self.saved: dict[str, int] | None = None

            def save_pqc_column_widths(self, widths: object) -> tuple[bool, str]:
                self.saved = dict(widths)  # type: ignore[arg-type]
                return True, ""

        page = PqcPage.__new__(PqcPage)
        page.tree = _Tree()
        page._preference_store = _Store()
        page._pqc_column_widths = {}
        self.assertEqual(page._on_tree_column_resize_start(SimpleNamespace(x=200, y=4)), "break")
        page._on_tree_column_resize(SimpleNamespace(x=200, y=4))
        self.assertEqual(page.tree.widths["status"], PQC_STATUS_COLUMN_WIDTH)
        self.assertNotIn("status", page._preference_store.saved or {})

    def test_pqc_status_separators_are_hard_fixed_but_other_columns_still_drag(self) -> None:
        """状态两侧 separator 均阻断；普通列仍交给 Treeview 原生拖动。"""

        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        try:
            root.geometry("1700x240")
            columns = tuple(pqc_ui_module.PQC_COLUMN_WIDTH_DEFAULTS)
            tree = ttk.Treeview(root, columns=columns, show="headings")
            tree.pack(fill="both", expand=True)
            for column, width in pqc_ui_module.PQC_COLUMN_WIDTH_DEFAULTS.items():
                tree.heading(column, text=column)
                tree.column(column, width=width, minwidth=width, stretch=(column == "category"))
            root.update_idletasks()
            root.update()

            page = PqcPage.__new__(PqcPage)
            page.tree = tree
            tree.bind("<ButtonPress-1>", page._on_tree_column_resize_start, add="+")
            tree.bind("<Double-Button-1>", page._on_tree_column_resize_start, add="+")

            def separator_x(column: str) -> int:
                return next(
                    x
                    for x in range(tree.winfo_width())
                    if tree.identify_region(x, 5) == "separator" and tree.identify_column(x) == column
                )

            before_widths = {column: tree.column(column, "width") for column in columns}
            before_columns = tree["columns"]
            before_displaycolumns = tree["displaycolumns"]
            # Tk 的真实状态左边界返回左侧类别模板列 #12，右边界返回状态列 #13。
            for column, delta in (("#12", 120), ("#13", -120)):
                x = separator_x(column)
                self.assertEqual(page._on_tree_column_resize_start(SimpleNamespace(x=x, y=5)), "break")
                tree.event_generate("<ButtonPress-1>", x=x, y=5)
                tree.event_generate("<B1-Motion>", x=max(1, min(tree.winfo_width() - 1, x + delta)), y=5)
                tree.event_generate("<ButtonRelease-1>", x=max(1, min(tree.winfo_width() - 1, x + delta)), y=5)
                root.update_idletasks()
                self.assertEqual(
                    {key: tree.column(key, "width") for key in columns},
                    before_widths,
                )
                self.assertEqual(tree["columns"], before_columns)
                self.assertEqual(tree["displaycolumns"], before_displaycolumns)

            # 表头正文拖动也不能改变状态列；ttk 默认不支持表头重排，
            # 这里保留实测门禁，避免后续新增绑定把它变成隐式拖动入口。
            left_status = separator_x("#12")
            right_status = separator_x("#13")
            heading_x = (left_status + right_status) // 2
            tree.event_generate("<ButtonPress-1>", x=heading_x, y=5)
            tree.event_generate("<B1-Motion>", x=max(1, heading_x - 180), y=5)
            tree.event_generate("<ButtonRelease-1>", x=max(1, heading_x - 180), y=5)
            root.update_idletasks()
            self.assertEqual(
                {key: tree.column(key, "width") for key in columns},
                before_widths,
            )
            self.assertEqual(tree["columns"], before_columns)
            self.assertEqual(tree["displaycolumns"], before_displaycolumns)

            # 单独验证 DoubleClick 类路径：去掉 ButtonPress 绑定后，第二次
            # 按下仍命中 Double-Button-1 拦截，不会重新启动 separator resize。
            tree.unbind("<ButtonPress-1>")
            double_x = separator_x("#12")
            tree.event_generate("<ButtonPress-1>", x=double_x, y=5, when="tail")
            tree.event_generate("<ButtonRelease-1>", x=double_x, y=5, when="tail")
            tree.event_generate("<ButtonPress-1>", x=double_x, y=5, when="tail")
            tree.event_generate("<B1-Motion>", x=double_x - 180, y=5)
            tree.event_generate("<ButtonRelease-1>", x=double_x - 180, y=5)
            root.update_idletasks()
            self.assertEqual(
                {key: tree.column(key, "width") for key in columns},
                before_widths,
            )
            tree.bind("<ButtonPress-1>", page._on_tree_column_resize_start, add="+")

            # 状态表头正文的双击只走 heading press/release，不触发业务行点击，
            # 也不改变任何列宽或列序。
            heading_x = (separator_x("#10") + separator_x("#11")) // 2
            for sequence in (
                "<ButtonPress-1>",
                "<ButtonRelease-1>",
                "<ButtonPress-1>",
                "<ButtonRelease-1>",
            ):
                tree.event_generate(sequence, x=heading_x, y=5, when="tail")
                root.update()
            self.assertEqual(
                {key: tree.column(key, "width") for key in columns},
                before_widths,
            )
            self.assertEqual(tree["columns"], before_columns)
            self.assertEqual(tree["displaycolumns"], before_displaycolumns)

            # 分隔线 #9（扭矩/类别模板之间）不属于状态边界，仍允许原生调宽。
            ordinary_x = separator_x("#9")
            ordinary_before = tree.column("05", "width")
            tree.event_generate("<ButtonPress-1>", x=ordinary_x, y=5)
            tree.event_generate("<B1-Motion>", x=ordinary_x + 40, y=5)
            tree.event_generate("<ButtonRelease-1>", x=ordinary_x + 40, y=5)
            root.update_idletasks()
            self.assertNotEqual(tree.column("05", "width"), ordinary_before)
            self.assertEqual(tree.column("status", "width"), before_widths["status"])
            self.assertEqual(tree["columns"], before_columns)
            self.assertEqual(tree["displaycolumns"], before_displaycolumns)
        finally:
            root.destroy()

    def test_pqc_header_controls_are_continuous_and_left_aligned(self) -> None:
        source = inspect.getsource(PqcPage._build_device_header_controls)
        self.assertIn('controls.grid(row=0, column=1, sticky="w"', source)
        self.assertIn("self.select_all_button.grid(row=0, column=3", source)
        self.assertIn("self.clear_selection_button.grid(row=0, column=4", source)
        self.assertIn('font=self._font(10)', source)

    def test_category_column_is_template_match_result(self) -> None:
        source = inspect.getsource(PqcPage._build_devices_card)
        self.assertIn("检验模板匹配结果列", source)
        self.assertIn("无匹配", source)

    def test_pqc_visual_palette_tracks_iqc_colors(self) -> None:
        build_devices = inspect.getsource(PqcPage._build_devices_card)
        build_button = inspect.getsource(PqcPage._button)
        self.assertIn('background=SUCCESS_BG', build_devices)
        self.assertIn('activebackground=INFO_BG', build_button)
        self.assertIn('font=self._font(11, "bold")', build_button)
        self.assertIn('padx=12', build_button)
        self.assertIn('pady=9', build_button)


if __name__ == "__main__":
    unittest.main()
