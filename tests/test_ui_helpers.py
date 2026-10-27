from __future__ import annotations

import json
import inspect
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src.generation_service import (
    GenerationBatch,
    GenerationService,
    RuntimeGenerationProvider,
    UnconfiguredGenerationProvider,
)
from src.paper_standard_exclusion import PaperStandardExclusionError
from src.import_orchestrator import BatExecutionResult, ImportOrchestrator, ImportRequest
from src.manual_confirmation import FuzzyTemplateCandidate, ManualConfirmationDecision, ManualConfirmationRow
from src.models import Engineer, OrderTask
from src import ui as ui_module
from src.sql_generator import SqlContextRow, SqlOrder, SqlRecord, render_order_sql
from src.ui import IqcApp, ReadGenerateResult, _maximize_window, _size_window_90_percent, log_level, status_meta
from src.ui_state import UiOrderStatus
from src.ui_state import UiBatchState
from tests.test_paper_standard_exclusion import _write_xlsx


class UiHelperTests(unittest.TestCase):
    class _Var:
        def __init__(self, value: object) -> None:
            self.value = value

        def get(self) -> object:
            return self.value

        def set(self, value: object) -> None:
            self.value = value

    class _Header:
        def configure(self, **_kwargs: object) -> None:
            return None

    def _import_stub_app(self, *, accepted: bool):
        class _Importer:
            def __init__(self) -> None:
                self.requests = None

            def is_committed(self, order_number: str) -> bool:
                return order_number == "REGENERATE"

            def run_batch(self, requests, **_kwargs):
                self.requests = requests
                return None

        app = object.__new__(ui_module.IqcApp)
        app.state = UiBatchState()
        app.state.stage(
            "REGENERATE",
            Path("iqc_draft_REGENERATE.sql"),
            status=UiOrderStatus.GENERATED_CONFIRM,
        )
        app.importer = _Importer()
        app.manual_confirm_var = self._Var(True)
        app.skip_errors_var = self._Var(True)
        app._busy = lambda: False
        app._selected_orders = lambda: ["REGENERATE"]
        app._filter_import_orders = lambda selected: (selected, [])
        app._confirm_duplicate_imports = lambda _orders: accepted
        app._refresh_tree = lambda: None
        app._append_log = lambda *args, **kwargs: None
        app._set_busy = lambda _busy: None
        app.status_var = self._Var("")
        app.header_status = self._Header()
        app._run_async = lambda _operation, function, _callback: function()
        return app

    def test_duplicate_lookup_merges_persisted_history_with_regenerated_ui_row(self) -> None:
        app = self._import_stub_app(accepted=False)
        self.assertEqual(app._duplicate_orders(["REGENERATE"]), ["REGENERATE"])

    def test_import_button_reuses_entry_handler_when_api_is_configured(self) -> None:
        app = object.__new__(IqcApp)
        app.entry_client = object()
        calls: list[str] = []
        app._submit_entry_selected = lambda: calls.append("entry")
        app._run_import = lambda **_kwargs: calls.append("bat")

        IqcApp._import_selected(app)

        self.assertEqual(calls, ["entry"])

    def test_import_button_keeps_bat_fallback_without_entry_api(self) -> None:
        app = object.__new__(IqcApp)
        app.entry_client = None
        calls: list[tuple[str, object]] = []
        app._run_import = lambda **kwargs: calls.append(("bat", kwargs.get("action_label")))

        IqcApp._import_selected(app)

        self.assertEqual(calls, [("bat", "导入")])

    def test_rt_paper_13_paper_only_selection_never_calls_entry_client_or_worker(self) -> None:
        class _EntryClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def create_draft_from_record(self, *args, **kwargs):
                self.calls.append("ordinary_create")
                raise AssertionError("paper-only order must not create ordinary draft")

            def save_draft(self, *args, **kwargs):
                self.calls.append("qa1_create")
                raise AssertionError("paper-only order must not create QA1 draft")

            def update_draft(self, *args, **kwargs):
                self.calls.append("update")
                raise AssertionError("paper-only order must not update draft")

        app = object.__new__(IqcApp)
        client = _EntryClient()
        app.entry_client = client
        app.state = UiBatchState()
        app.state.stage("PAPER", Path("paper_only_placeholder.sql"), status=UiOrderStatus.PAPER_ONLY)
        app.skip_errors_var = self._Var(True)
        app.manual_confirm_var = self._Var(True)
        app._busy = lambda: False
        app._selected_orders = lambda: ["PAPER"]
        app._append_log = lambda *args, **kwargs: events.append((args, kwargs))
        app._refresh_tree = lambda: None
        app._run_async = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("paper-only order must not start Entry worker")
        )
        events: list[tuple[tuple[object, ...], dict[str, object]]] = []

        with patch.object(ui_module.messagebox, "showwarning"):
            IqcApp._submit_entry_selected(app)

        self.assertEqual(client.calls, [])
        self.assertEqual(app.state.get("PAPER").status, UiOrderStatus.PAPER_ONLY)
        self.assertIn("entry_submit_result", [args[0] for args, _kwargs in events])

    def test_duplicate_dialog_cancel_does_not_build_request_or_start_worker(self) -> None:
        app = self._import_stub_app(accepted=False)
        app._run_import(action_label="导入")
        self.assertIsNone(app.importer.requests)
        current = app.state.get("REGENERATE")
        self.assertEqual(current.status, UiOrderStatus.GENERATED_CONFIRM)
        self.assertFalse(current.confirmed)

    def test_duplicate_dialog_accepts_only_then_forwards_duplicate_flag(self) -> None:
        app = self._import_stub_app(accepted=True)
        app._run_import(action_label="导入")
        self.assertIsNotNone(app.importer.requests)
        self.assertTrue(app.importer.requests[0].duplicate_confirmed)

    def test_read_finished_regeneration_uses_persisted_duplicate_gate(self) -> None:
        """The actual read-finished path must retain duplicate protection."""

        class _NoBatExecutor:
            def __init__(self) -> None:
                self.calls: list[Path] = []

            def execute(self, _runner, sql_path, _timeout, _cancel_event=None):
                self.calls.append(Path(sql_path))
                return BatExecutionResult(
                    started=True,
                    exit_code=0,
                    commit_confirmed=True,
                    detail="simulated executor; no BAT",
                )

        order = "REGENERATE"
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runner = root / "run_generated_sql.bat"
            runner.write_text("@echo off\n", encoding="ascii")
            output = root / "输出"
            output.mkdir()
            sql_path = output / f"iqc_draft_{order}.sql"
            sql_path.write_text(
                render_order_sql(
                    SqlOrder(
                        order,
                        (
                            SqlRecord(
                                item_code="M-001",
                                item_name="物项",
                                quantity=1,
                                supplier="供应商",
                                procedure_name="检查单",
                                procedure_model_id=1,
                                engineer_id=1,
                                engineer_name="工程师",
                                engineer_dept_id="D1",
                                trigger_basis=order,
                                check_number=1,
                                contexts=(SqlContextRow("1", "外观", "无缺陷"),),
                            ),
                        ),
                    )
                ),
                encoding="utf-8",
            )
            state_path = root / "状态" / "iqc_import_state.json"
            first_executor = _NoBatExecutor()
            ImportOrchestrator(runner, first_executor, state_path=state_path).run_batch(
                (ImportRequest(order, sql_path, confirmed=True),),
                batch_id="first",
            )
            self.assertEqual(first_executor.calls, [sql_path])

            resumed_executor = _NoBatExecutor()
            importer = ImportOrchestrator(runner, resumed_executor, state_path=state_path)
            app = object.__new__(IqcApp)
            app.state = UiBatchState()
            app.paths = SimpleNamespace(output_dir=output)
            app.importer = importer
            app._append_log = lambda *args, **kwargs: None
            app._refresh_tree = lambda: None
            app._select_all_orders = lambda: None
            app._set_busy = lambda _busy: None
            app._busy = lambda: False
            app._selected_orders = lambda: [order]
            app._filter_import_orders = lambda selected: (selected, [])
            app.manual_confirm_var = self._Var(True)
            app.skip_errors_var = self._Var(True)
            app.status_var = self._Var("")
            app.header_status = self._Header()
            app._manual_resume_started = False

            # This is the path used after reading/regenerating a source file:
            # it clears the old UI row and stages a fresh, non-COMMITTED row.
            IqcApp._read_finished(app, {order: OrderTask(order)}, None)
            self.assertEqual(app.state.get(order).status, UiOrderStatus.READ_SUCCESS)
            self.assertEqual(app._duplicate_orders([order]), [order])

            async_calls: list[object] = []
            app._run_async = lambda _operation, function, _callback: async_calls.append(function())
            app._confirm_duplicate_imports = lambda _orders: False
            app._run_import(action_label="导入")
            self.assertEqual(async_calls, [])
            self.assertEqual(resumed_executor.calls, [])
            self.assertEqual(app.state.get(order).status, UiOrderStatus.READ_SUCCESS)

            app._confirm_duplicate_imports = lambda _orders: True
            app._run_import(action_label="导入")
            self.assertEqual(len(async_calls), 1)
            self.assertEqual(len(resumed_executor.calls), 1)
            result = async_calls[0]
            self.assertEqual(result.results[0].status.value, "COMMITTED")
            self.assertIn("用户已确认本次为重复导入", result.results[0].diagnostic)
            # The executor saw one second attempt only after the accepted
            # duplicate dialog; the generated request carried its audit flag.
            self.assertEqual(result.results[0].attempt, 2)

    def test_engineer_config_stage_events_are_safe_and_explain_missing_config(self) -> None:
        class _ComboBox:
            def __setitem__(self, _key, _value) -> None:
                return None

            def current(self, _index: int) -> None:
                return None

        class _Var:
            def set(self, _value) -> None:
                return None

        class _Header:
            def configure(self, **_kwargs) -> None:
                return None

        with TemporaryDirectory() as temp:
            config_path = Path(temp) / "missing-engineers.json"
            app = object.__new__(IqcApp)
            app.paths = SimpleNamespace(engineer_config=config_path)
            app.engineer_box = _ComboBox()
            app.engineer_var = self._Var("请选择预配置人员")
            app.status_var = _Var()
            app.header_status = _Header()
            events: list[tuple[tuple[object, ...], dict[str, object]]] = []
            app._append_log = lambda *args, **kwargs: events.append((args, kwargs))

            IqcApp._load_engineers(app)

        self.assertEqual([entry[0][0] for entry in events], ["engineer_config_load_start", "engineer_config_load_result"])
        result = events[-1][1]
        self.assertEqual(result["status"], "ERROR")
        self.assertFalse(result["config_exists"])
        self.assertEqual(result["available_count"], 0)
        self.assertEqual(result["error_type"], "EngineerConfigError")
        serialized = json.dumps(events, ensure_ascii=False).lower()
        self.assertNotIn("user_id", serialized)
        self.assertNotIn("dept_id", serialized)
        self.assertNotIn("password", serialized)

    def test_engineer_config_success_event_reports_count_without_ids(self) -> None:
        class _ComboBox:
            def __setitem__(self, _key, _value) -> None:
                return None

            def current(self, _index: int) -> None:
                return None

        class _Var:
            def set(self, _value) -> None:
                return None

        class _Header:
            def configure(self, **_kwargs) -> None:
                return None

        with TemporaryDirectory() as temp:
            config_path = Path(temp) / "engineers.json"
            config_path.write_text(
                json.dumps({"engineers": [{"user_id": "u-1", "dept_id": "d-1", "name": "合成工程师02", "display_name": "合成工程师02"}]}),
                encoding="utf-8",
            )
            app = object.__new__(IqcApp)
            app.paths = SimpleNamespace(engineer_config=config_path)
            app.engineer_box = _ComboBox()
            app.engineer_var = self._Var("合成工程师02")
            app.status_var = _Var()
            app.header_status = _Header()
            events: list[tuple[tuple[object, ...], dict[str, object]]] = []
            app._append_log = lambda *args, **kwargs: events.append((args, kwargs))

            IqcApp._load_engineers(app)

        result = events[-1][1]
        self.assertEqual(result["status"], "SUCCESS")
        self.assertTrue(result["config_exists"])
        self.assertEqual(result["available_count"], 1)
        serialized = json.dumps(events, ensure_ascii=False).lower()
        self.assertNotIn("u-1", serialized)
        self.assertNotIn("d-1", serialized)

    def test_paper_source_default_loads_from_launcher_and_manual_override_still_wins(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_dir = root / "config"
            config_dir.mkdir()
            default_path = root / "默认线下清单.xlsx"
            manual_path = root / "手工覆盖线下清单.xlsx"
            (config_dir / "launcher.json").write_text(
                json.dumps({"paper_standard_catalog_path": f" {default_path} "}, ensure_ascii=False),
                encoding="utf-8",
            )

            self.assertEqual(IqcApp._load_default_paper_source(SimpleNamespace(project_root=root)), str(default_path))

            app = object.__new__(IqcApp)
            app.paper_source_var = self._Var(str(default_path))
            app.source_var = self._Var("")
            app.pqc_source_var = self._Var("pqc.xlsx")
            app.engineer_var = self._Var("请选择预配置人员")
            app._preference_store = SimpleNamespace(
                save_iqc_source_path=lambda _value: None,
                save_pqc_source_path=lambda _value: None,
                save_paper_standard_catalog_path=lambda _value: None,
                save_engineer_name=lambda _value: None,
            )
            with patch.object(ui_module.filedialog, "askopenfilename", return_value=str(manual_path)):
                IqcApp._choose_paper_source(app)

            self.assertEqual(app.paper_source_var.get(), str(manual_path))

    def test_missing_configured_paper_source_still_blocks_read_gate(self) -> None:
        root = Path(__file__).resolve().parents[1]

        class _Text:
            def get(self, *_args) -> str:
                return "DEMO-ORDER-001"

        app = object.__new__(IqcApp)
        app._busy = lambda: False
        app.orders_text = _Text()
        app.orders_var = self._Var("")
        app.source_var = self._Var(str(root / "fixtures" / "source_sample.xlsx"))
        app.paper_source_var = self._Var(str(root / "fixtures" / "missing_paper_catalog.xlsx"))
        app.engineer_var = self._Var("合成工程师02")
        app._engineers_by_display = {"合成工程师02": Engineer("", "合成工程师02", "", "合成工程师02")}
        app._set_busy = lambda _busy: self.fail("线下清单不存在时不应进入 busy 或后台读取")
        app._run_async = lambda *_args, **_kwargs: self.fail("线下清单不存在时不应启动后台任务")

        with patch.object(ui_module.messagebox, "showerror") as showerror:
            IqcApp._read_source(app)

        showerror.assert_called_once()
        self.assertIn("线下规范清单", showerror.call_args.args[1])

    def test_status_badges_have_visual_and_row_metadata(self) -> None:
        text, foreground, background, tag = status_meta(UiOrderStatus.COMMITTED)
        self.assertEqual(text, "已提交")
        self.assertTrue(foreground.startswith("#"))
        self.assertTrue(background.startswith("#"))
        self.assertEqual(tag, "success")

    def test_error_and_warning_log_levels_are_distinct(self) -> None:
        self.assertEqual(log_level("import_worker_error", detail="异常"), "error")
        self.assertEqual(log_level("import_result", status="已回滚/可重导"), "warning")
        self.assertEqual(log_level("source_read", status="读取成功"), "success")
        self.assertEqual(log_level("view_log"), "info")

    def test_manual_confirmation_dialog_uses_90_percent_window_helper(self) -> None:
        self.assertIn("_size_window_90_percent(dialog)", inspect.getsource(IqcApp._show_manual_confirmation_dialog))

    def test_90_percent_window_geometry_is_centered(self) -> None:
        class _Window:
            def __init__(self) -> None:
                self.geometry_value = ""

            def winfo_screenwidth(self) -> int:
                return 1920

            def winfo_screenheight(self) -> int:
                return 1080

            def geometry(self, value: str) -> None:
                self.geometry_value = value

        window = _Window()
        _size_window_90_percent(window)
        self.assertEqual(window.geometry_value, "1728x972+96+54")

    def test_maximize_window_requests_zoomed_state(self) -> None:
        class _Window:
            def __init__(self) -> None:
                self.states = []
                self.attributes_calls = []

            def state(self, value: str) -> None:
                self.states.append(value)

            def attributes(self, *args) -> None:
                self.attributes_calls.append(args)

        window = _Window()
        _maximize_window(window)
        self.assertEqual(window.states, ["zoomed"])
        self.assertEqual(window.attributes_calls, [])

    def test_maximize_window_falls_back_when_zoomed_state_is_unsupported(self) -> None:
        class _Window:
            def __init__(self) -> None:
                self.attributes_calls = []

            def state(self, _value: str) -> None:
                raise ui_module.tk.TclError("unsupported")

            def attributes(self, *args) -> None:
                self.attributes_calls.append(args)

        window = _Window()
        _maximize_window(window)
        self.assertEqual(window.attributes_calls, [("-zoomed", True)])

    def test_duplicate_import_confirmation_prompt_accept_and_cancel(self) -> None:
        class _Var:
            def __init__(self) -> None:
                self.values: list[str] = []

            def set(self, value: str) -> None:
                self.values.append(value)

        for answer in (True, False):
            with self.subTest(answer=answer):
                app = object.__new__(IqcApp)
                app.status_var = _Var()
                events: list[tuple[tuple[object, ...], dict[str, object]]] = []
                app._append_log = lambda *args, **kwargs: events.append((args, kwargs))
                with patch.object(ui_module.messagebox, "askyesno", return_value=answer) as ask:
                    result = IqcApp._confirm_duplicate_imports(app, ["DUP-1", "DUP-2"])
                self.assertEqual(result, answer)
                ask.assert_called_once()
                self.assertIn("检测到本次为重复导入", ask.call_args.args[1])
                self.assertEqual(events[0][0][0], "duplicate_import_confirmation")
                self.assertEqual(events[0][1]["status"], "CONFIRMED" if answer else "CANCELLED")
                if answer:
                    self.assertEqual(app.status_var.values, [])
                else:
                    self.assertEqual(app.status_var.values, ["已取消重复导入；未调用 BAT"])

    def test_read_and_generate_button_delegates_to_generation_service(self) -> None:
        root = Path(__file__).resolve().parents[1]

        class CapturingGenerator:
            def __init__(self) -> None:
                self.calls = []

            def generate(self, tasks, engineer):
                self.calls.append((tasks, engineer))
                return GenerationBatch()

        generator = CapturingGenerator()
        app = object.__new__(IqcApp)
        app.generator = generator
        engineer = Engineer("", "合成工程师02", "", "合成工程师02")
        with TemporaryDirectory() as temp:
            paper_path = Path(temp) / "paper.xlsx"
            _write_xlsx(paper_path, [(1, "文件编号/文件名"), (2, "PAPER-DOC_0_纸质规范.pdf")])
            result = IqcApp._read_and_generate(
                app,
                root / "fixtures" / "source_sample.xlsx",
                paper_path,
                ["DEMO-ORDER-001"],
                engineer,
            )
        self.assertIsInstance(result, ReadGenerateResult)
        self.assertEqual(len(generator.calls), 1)
        self.assertEqual(generator.calls[0][1], engineer)
        self.assertIn("DEMO-ORDER-001", result.tasks)
        self.assertIsNotNone(result.generation)

    def test_read_finished_surfaces_qa1_block_when_entry_api_is_unconfigured(self) -> None:
        """QA1 metadata must not disappear when the legacy BAT mode is active."""

        class _Var:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: object) -> None:
                self.value = value

            def get(self) -> object:
                return self.value

        class _Header:
            def __init__(self) -> None:
                self.value = ""

            def configure(self, **kwargs: object) -> None:
                self.value = str(kwargs.get("text", ""))

            def cget(self, _name: str) -> str:
                return self.value

        with TemporaryDirectory() as temp:
            output_dir = Path(temp)
            order_number = "QA1-ORDER"
            spec = SimpleNamespace(
                combination=SimpleNamespace(item_code="DEMO-MAT-021"),
                candidates=(SimpleNamespace(id="171"),),
            )
            resolved = SimpleNamespace(qa1_drafts=(spec,), sql_order=SimpleNamespace(records=()))
            outcome = SimpleNamespace(
                order_number=order_number,
                resolved_order=resolved,
                artifacts=SimpleNamespace(sql_path=output_dir / "iqc_draft_QA1-ORDER.sql"),
                result_path=None,
                detail="普通草稿已生成",
            )
            generation = GenerationBatch(outcomes=(outcome,))
            app = object.__new__(IqcApp)
            app.entry_client = None
            app.paths = SimpleNamespace(output_dir=output_dir)
            app.state = UiBatchState()
            app._generated_outcomes = {}
            app._engineer_display_names = []
            app._manual_resume_started = False
            app.status_var = _Var()
            app.detail_var = _Var()
            app.header_status = _Header()
            app._append_log = lambda *args, **kwargs: events.append((args, kwargs))
            app._refresh_tree = lambda: None
            app._select_all_orders = lambda: None
            app._set_busy = lambda _busy: None
            events: list[tuple[tuple[object, ...], dict[str, object]]] = []

            IqcApp._read_finished(
                app,
                ReadGenerateResult(
                    {order_number: OrderTask(order_number)},
                    generation,
                    (),
                    None,
                ),
                None,
            )

        blocked = [kwargs for args, kwargs in events if args and args[0] == "qa1_submission_blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["status"], "BLOCKED")
        self.assertIn("IQC_ENTRY_API_BASE_URL", blocked[0]["detail"])
        self.assertIn("未新增关键零部件草稿", blocked[0]["detail"])
        self.assertIn("QA1 附加草稿未新增", app.status_var.get())
        self.assertIn("QA1待配置API", app.header_status.value)

    def test_qa1_unique_apply_range_candidate_is_auto_selected(self) -> None:
        app = object.__new__(IqcApp)
        spec = SimpleNamespace(
            combination=SimpleNamespace(item_code="DEMO-MAT-021"),
            selected_model=SimpleNamespace(id="171"),
            candidates=(SimpleNamespace(id="171", procedure_name="端子类关键零部件专项检查单"),),
            matched_by="apply_range",
        )
        outcome = SimpleNamespace(resolved_order=SimpleNamespace(qa1_drafts=(spec,)))
        generated = {"5297951": outcome}

        self.assertEqual(IqcApp._qa1_rows_from_outcomes(app, generated), ())
        self.assertEqual(IqcApp._qa1_unique_choices_from_outcomes(app, generated), {"5297951::DEMO-MAT-021": "171"})

    def test_qa1_unique_legacy_wildcard_candidate_is_auto_selected_without_selected_model(self) -> None:
        app = object.__new__(IqcApp)
        spec = SimpleNamespace(
            combination=SimpleNamespace(item_code="DEMO-MAT-021"),
            selected_model=None,
            candidates=(SimpleNamespace(id="171", procedure_name="端子类关键零部件专项检查单"),),
            matched_by="procedure_code",
            legacy_item_code="DEMOHCAA#AKB",
        )
        outcome = SimpleNamespace(resolved_order=SimpleNamespace(qa1_drafts=(spec,)))
        generated = {"5297951": outcome}

        self.assertEqual(IqcApp._qa1_rows_from_outcomes(app, generated), ())
        self.assertEqual(IqcApp._qa1_unique_choices_from_outcomes(app, generated), {"5297951::DEMO-MAT-021": "171"})

    def test_qa1_multiple_legacy_wildcard_candidates_still_require_manual_selection(self) -> None:
        app = object.__new__(IqcApp)
        spec = SimpleNamespace(
            combination=SimpleNamespace(item_code="DEMO-MAT-021"),
            selected_model=None,
            candidates=(
                SimpleNamespace(id="171", procedure_name="端子类关键零部件专项检查单"),
                SimpleNamespace(id="210", procedure_name="插拔式桥接件关键零部件专项检查单1"),
            ),
            matched_by="procedure_code",
            legacy_item_code="DEMOHCAA#AKB",
        )
        outcome = SimpleNamespace(resolved_order=SimpleNamespace(qa1_drafts=(spec,)))
        generated = {"5297951": outcome}

        rows = IqcApp._qa1_rows_from_outcomes(app, generated)
        self.assertEqual(len(rows), 1)
        self.assertEqual(IqcApp._qa1_unique_choices_from_outcomes(app, generated), {})

    def test_read_finished_does_not_open_confirmation_for_unique_qa1_candidate(self) -> None:
        class _Var:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: object) -> None:
                self.value = value

            def get(self) -> object:
                return self.value

        class _Header:
            def configure(self, **kwargs: object) -> None:
                self.value = kwargs.get("text", "")

        with TemporaryDirectory() as temp:
            output_dir = Path(temp)
            order_number = "5297951"
            spec = SimpleNamespace(
                combination=SimpleNamespace(item_code="DEMO-MAT-021"),
                selected_model=SimpleNamespace(id="171"),
                candidates=(SimpleNamespace(id="171", procedure_name="端子类关键零部件专项检查单"),),
                matched_by="apply_range",
            )
            outcome = SimpleNamespace(
                order_number=order_number,
                resolved_order=SimpleNamespace(
                    qa1_drafts=(spec,),
                    sql_order=SimpleNamespace(records=()),
                ),
                artifacts=SimpleNamespace(sql_path=output_dir / "iqc_draft_5297951.sql"),
                result_path=None,
                detail="普通草稿已生成",
            )
            app = object.__new__(IqcApp)
            app.entry_client = object()
            app.paths = SimpleNamespace(output_dir=output_dir)
            app.state = UiBatchState()
            app.status_var = _Var()
            app.detail_var = _Var()
            app.header_status = _Header()
            app._append_log = lambda *args, **kwargs: events.append((args, kwargs))
            app._refresh_tree = lambda: None
            app._select_all_orders = lambda: None
            app._set_busy = lambda _busy: None
            app._manual_resume_started = False
            app._begin_manual_confirmation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("unique QA1 candidate must not open manual confirmation")
            )
            events: list[tuple[tuple[object, ...], dict[str, object]]] = []

            IqcApp._read_finished(
                app,
                ReadGenerateResult(
                    {order_number: OrderTask(order_number)},
                    GenerationBatch(outcomes=(outcome,)),
                    (),
                    None,
                ),
                None,
            )

        self.assertEqual(app._qa1_choices, {"5297951::DEMO-MAT-021": "171"})
        self.assertIn("qa1_auto_confirmed", [args[0] for args, _kwargs in events])
        self.assertNotIn("qa1_confirmation_required", [args[0] for args, _kwargs in events])

    def test_qa1_manual_name_uses_selected_candidate_name_when_input_is_blank(self) -> None:
        app = object.__new__(IqcApp)
        row = ManualConfirmationRow(
            order_number="5297951",
            item_code="DEMO-MAT-021",
            raw_name="",
            normalized_name="QA1 · DEMO-MAT-021",
            candidates=(FuzzyTemplateCandidate("171", "端子类关键零部件专项检查单", 1.0, True),),
        )
        decisions = IqcApp._fill_qa1_manual_names(
            (row,),
            {row.confirmation_key: ManualConfirmationDecision(model_id="171", manual_name="")},
        )
        self.assertEqual(
            decisions[row.confirmation_key],
            ManualConfirmationDecision(model_id="171", manual_name="端子类关键零部件专项检查单"),
        )

    def test_read_finished_groups_multiple_ordinary_orders_into_one_confirmation(self) -> None:
        """多个单号的普通人工候选必须只进入一次确认入口。"""

        rows = (
            ManualConfirmationRow("ORD-1", "ITEM-1", "原文1", "名称1"),
            ManualConfirmationRow("ORD-2", "ITEM-2", "原文2", "名称2"),
        )
        with TemporaryDirectory() as temp:
            app = object.__new__(IqcApp)
            app.entry_client = None
            app.paths = SimpleNamespace(output_dir=Path(temp))
            app.state = UiBatchState()
            app.generator = object()
            app.status_var = self._Var("")
            app.detail_var = self._Var("")
            app.header_status = self._Header()
            app._append_log = lambda *_args, **_kwargs: None
            app._refresh_tree = lambda: None
            app._select_all_orders = lambda: None
            app._set_busy = lambda _busy: None
            calls: list[tuple[tuple[ManualConfirmationRow, ...], dict[str, object]]] = []
            app._begin_manual_confirmation = lambda normal, **kwargs: calls.append((normal, kwargs))

            IqcApp._read_finished(
                app,
                ReadGenerateResult(
                    {"ORD-1": OrderTask("ORD-1"), "ORD-2": OrderTask("ORD-2")},
                    GenerationBatch(manual_confirmation_rows=rows),
                    (),
                    object(),
                ),
                None,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], rows)
        self.assertEqual(calls[0][1]["qa1_rows"], ())

    def test_qa1_rows_group_by_order_and_same_item_code_does_not_overwrite(self) -> None:
        """同一物项跨单号必须保留两个复合键，而不是互相覆盖。"""

        def outcome(order_number: str) -> object:
            spec = SimpleNamespace(
                combination=SimpleNamespace(item_code="ITEM-SAME", item_name="关键件"),
                candidates=(
                    SimpleNamespace(id="171", procedure_name="模板一"),
                    SimpleNamespace(id="210", procedure_name="模板二"),
                ),
            )
            return SimpleNamespace(
                order_number=order_number,
                resolved_order=SimpleNamespace(qa1_drafts=(spec,), sql_order=SimpleNamespace(records=())),
            )

        app = object.__new__(IqcApp)
        rows = IqcApp._qa1_rows_from_outcomes(
            app,
            {"ORD-1": outcome("ORD-1"), "ORD-2": outcome("ORD-2")},
        )
        self.assertEqual([row.confirmation_key for row in rows], ["ORD-1::ITEM-SAME", "ORD-2::ITEM-SAME"])

        with TemporaryDirectory() as temp:
            app.paths = SimpleNamespace(output_dir=Path(temp))
            app._append_log = lambda *_args, **_kwargs: None
            app.detail_var = self._Var("")
            app.status_var = self._Var("")
            app.header_status = self._Header()
            app._qa1_choices = {}
            app._show_manual_confirmation_dialog = lambda _rows: {
                rows[0].confirmation_key: "171",
                rows[1].confirmation_key: "210",
            }
            IqcApp._begin_manual_confirmation(app, (), qa1_rows=rows)

        self.assertEqual(
            app._qa1_choices,
            {"ORD-1::ITEM-SAME": "171", "ORD-2::ITEM-SAME": "210"},
        )

    def test_mixed_confirmation_resume_filters_already_decided_qa1_row(self) -> None:
        """普通恢复再次装载结果时，不应重复弹出已经决定的 QA1 行。"""

        qa1_key = "ORD-1::ITEM-QA1"
        spec = SimpleNamespace(
            combination=SimpleNamespace(item_code="ITEM-QA1", item_name="关键件"),
            candidates=(
                SimpleNamespace(id="171", procedure_name="模板一"),
                SimpleNamespace(id="210", procedure_name="模板二"),
            ),
        )
        outcome = SimpleNamespace(
            order_number="ORD-1",
            resolved_order=SimpleNamespace(qa1_drafts=(spec,), sql_order=SimpleNamespace(records=())),
            artifacts=SimpleNamespace(sql_path=Path("draft.sql")),
            result_path=None,
            detail="生成完成",
        )
        ordinary = ManualConfirmationRow("ORD-1", "ITEM-NORMAL", "原文", "普通模板")
        with TemporaryDirectory() as temp:
            app = object.__new__(IqcApp)
            app.entry_client = object()
            app.paths = SimpleNamespace(output_dir=Path(temp))
            app.state = UiBatchState()
            app._qa1_choices = {qa1_key: False}
            app.status_var = self._Var("")
            app.detail_var = self._Var("")
            app.header_status = self._Header()
            app._append_log = lambda *_args, **_kwargs: None
            app._refresh_tree = lambda: None
            app._select_all_orders = lambda: None
            app._set_busy = lambda _busy: None
            app.generator = object()
            calls: list[dict[str, object]] = []
            app._begin_manual_confirmation = lambda _rows, **kwargs: calls.append(kwargs)

            IqcApp._read_finished(
                app,
                ReadGenerateResult(
                    {"ORD-1": OrderTask("ORD-1")},
                    GenerationBatch(outcomes=(outcome,), manual_confirmation_rows=(ordinary,)),
                    (),
                    object(),
                ),
                None,
                preserve_qa1_choices=True,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["qa1_rows"], ())
        self.assertIs(app._qa1_choices[qa1_key], False)

    def test_qa1_false_and_missing_choice_are_recorded_as_decided_abandonment(self) -> None:
        row = ManualConfirmationRow(
            "ORD-1",
            "ITEM-QA1",
            "原文",
            "关键件",
            candidates=(FuzzyTemplateCandidate("171", "模板一", 1.0, True),),
        )
        with TemporaryDirectory() as temp:
            for choices in ({row.confirmation_key: False}, {}):
                app = object.__new__(IqcApp)
                app.paths = SimpleNamespace(output_dir=Path(temp))
                app._append_log = lambda *_args, **_kwargs: None
                app.detail_var = self._Var("")
                app.status_var = self._Var("")
                app.header_status = self._Header()
                app._qa1_choices = {}
                app._show_manual_confirmation_dialog = lambda _rows, choices=choices: choices
                IqcApp._begin_manual_confirmation(app, (), qa1_rows=(row,))
                self.assertEqual(app._qa1_choices, {row.confirmation_key: False})

    def test_new_batch_clears_old_qa1_choices(self) -> None:
        spec = SimpleNamespace(
            combination=SimpleNamespace(item_code="ITEM-QA1", item_name="关键件"),
            candidates=(
                SimpleNamespace(id="171", procedure_name="模板一"),
                SimpleNamespace(id="210", procedure_name="模板二"),
            ),
        )
        outcome = SimpleNamespace(
            order_number="NEW-ORDER",
            resolved_order=SimpleNamespace(qa1_drafts=(spec,), sql_order=SimpleNamespace(records=())),
            artifacts=SimpleNamespace(sql_path=Path("draft.sql")),
            result_path=None,
            detail="生成完成",
        )
        with TemporaryDirectory() as temp:
            app = object.__new__(IqcApp)
            app.entry_client = object()
            app.paths = SimpleNamespace(output_dir=Path(temp))
            app.state = UiBatchState()
            app._qa1_choices = {"OLD-ORDER::ITEM-QA1": "old-model"}
            app.status_var = self._Var("")
            app.detail_var = self._Var("")
            app.header_status = self._Header()
            app._append_log = lambda *_args, **_kwargs: None
            app._refresh_tree = lambda: None
            app._select_all_orders = lambda: None
            app._set_busy = lambda _busy: None
            calls: list[dict[str, object]] = []
            app._begin_manual_confirmation = lambda _rows, **kwargs: calls.append(kwargs)

            IqcApp._read_finished(
                app,
                ReadGenerateResult(
                    {"NEW-ORDER": OrderTask("NEW-ORDER")},
                    GenerationBatch(outcomes=(outcome,)),
                    (),
                    None,
                ),
                None,
            )

        self.assertEqual(app._qa1_choices, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]["qa1_rows"]), 1)

    def test_manual_resume_finished_clears_started_and_busy_on_success_and_error(self) -> None:
        for result, error in ((GenerationBatch(), None), (None, RuntimeError("resume failed"))):
            app = object.__new__(IqcApp)
            app._pending_confirmation_tasks = {"ORD-1": object()}
            app._pending_confirmation_engineer = object()
            app._manual_resume_started = True
            busy_states: list[bool] = []
            app._set_busy = busy_states.append
            read_calls: list[tuple[object, BaseException | None, bool]] = []

            def read_finished(value, read_error, *, preserve_qa1_choices=False):
                read_calls.append((value, read_error, preserve_qa1_choices))

            app._read_finished = read_finished
            IqcApp._manual_resume_finished(app, result, error)

            self.assertFalse(app._manual_resume_started)
            self.assertEqual(busy_states, [False])
            self.assertEqual(len(read_calls), 1)
            self.assertTrue(read_calls[0][2])
            if error is None:
                self.assertIsNone(read_calls[0][1])
            else:
                self.assertIs(read_calls[0][1], error)

    def test_rt_paper_16_read_and_generate_stops_before_generator_when_catalog_is_invalid(self) -> None:
        class _Generator:
            def __init__(self) -> None:
                self.called = False
                self.provider = SimpleNamespace(set_paper_standard_catalog=lambda _catalog: None)

            def generate(self, *_args, **_kwargs):
                self.called = True
                raise AssertionError("invalid paper catalog must block before generation")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            cases: list[tuple[str, Path]] = []
            cases.append(("missing", root / "missing.xlsx"))
            empty = root / "empty.xlsx"
            _write_xlsx(empty, [(1, "文件编号/文件名")])
            cases.append(("empty", empty))
            bad_header = root / "bad_header.xlsx"
            _write_xlsx(bad_header, [(1, "错误表头"), (2, "DEMO04-52-DEMOFORM-003_R1_专项检验规范.pdf")])
            cases.append(("bad_header", bad_header))
            illegal = root / "illegal.xlsx"
            _write_xlsx(illegal, [(1, "文件编号/文件名"), (2, "这不是编码")])
            cases.append(("illegal", illegal))

            for label, paper_path in cases:
                with self.subTest(label=label):
                    app = object.__new__(IqcApp)
                    generator = _Generator()
                    app.generator = generator
                    with self.assertRaises(PaperStandardExclusionError):
                        IqcApp._read_and_generate(
                            app,
                            root / "source.xlsx",
                            paper_path,
                            ["DEMO-ORDER-001"],
                            Engineer("7", "合成工程师02", "1001", "合成工程师02"),
                        )
                    self.assertFalse(generator.called)

    def test_unconfigured_read_and_generate_returns_full_stage_diagnostics(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app = object.__new__(IqcApp)
        app.generator = GenerationService(
            root / "template" / "iqc_draft_template.xlsx",
            root / "输出",
            UnconfiguredGenerationProvider(),
        )
        engineer = Engineer("7", "合成工程师02", "1001", "合成工程师02")

        with TemporaryDirectory() as temp:
            paper_path = Path(temp) / "paper.xlsx"
            _write_xlsx(paper_path, [(1, "文件编号/文件名"), (2, "PAPER-DOC_0_纸质规范.pdf")])
            result = IqcApp._read_and_generate(
                app,
                root / "fixtures" / "source_sample.xlsx",
                paper_path,
                ["DEMO-ORDER-001"],
                engineer,
            )

        names = [event.name for event in result.events]
        self.assertEqual(
            names,
            [
                "source_read_start",
                "paper_catalog_load",
                "source_read_result",
                "generation_start",
                "provider_preflight",
                "generation_resolve_start",
                "generation_resolve_error",
            ],
        )
        source_result = result.events[2]
        self.assertEqual(source_result.fields["row_count"], 2)
        self.assertEqual(source_result.fields["sheet_names"], "8月")
        self.assertEqual(result.generation.errors["DEMO-ORDER-001"], result.events[-1].detail)
        self.assertNotIn("password", json.dumps([event.__dict__ for event in result.events], ensure_ascii=False).lower())

    def test_render_logs_displays_structured_stage_fields(self) -> None:
        class _LogText:
            def __init__(self) -> None:
                self.inserted: list[str] = []

            def configure(self, **_kwargs) -> None:
                return None

            def delete(self, *_args) -> None:
                self.inserted.clear()

            def insert(self, _index, text: str, _tag: str) -> None:
                self.inserted.append(text)

            def see(self, *_args) -> None:
                return None

        app = object.__new__(IqcApp)
        app.log_text = _LogText()
        payload = json.dumps(
            {
                "timestamp": "2026-08-28T00:00:00+00:00",
                "event": "source_read_result",
                "order_number": "DEMO-ORDER-001",
                "status": "SUCCESS",
                "detail": "源 Excel 单号读取完成",
                "fields": {"row_count": "2", "sheet_names": "8月"},
            },
            ensure_ascii=False,
        )

        IqcApp._render_logs(app, [payload])

        self.assertIn("row_count=2", app.log_text.inserted[0])
        self.assertIn("sheet_names=8月", app.log_text.inserted[0])

    def test_read_source_resets_busy_when_engineer_mapping_fails(self) -> None:
        root = Path(__file__).resolve().parents[1]

        class _Var:
            def __init__(self, value: str = "") -> None:
                self.value = value

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        class _Text:
            def get(self, *_args) -> str:
                return "DEMO-ORDER-001"

        class _Header:
            def configure(self, **_kwargs) -> None:
                return None

        app = object.__new__(IqcApp)
        app._busy = lambda: False
        app.orders_text = _Text()
        app.orders_var = _Var()
        app.source_var = _Var(str(root / "fixtures" / "source_sample.xlsx"))
        app.paper_source_var = _Var(str(root / "fixtures" / "source_sample.xlsx"))
        app.engineer_var = _Var("合成工程师02")
        app.status_var = _Var()
        app.header_status = _Header()
        app._engineers_by_display = {}
        busy_states: list[bool] = []
        app._set_busy = busy_states.append
        app._run_async = lambda *_args, **_kwargs: self.fail("工程师解析失败时不应启动后台任务")

        with patch.object(ui_module.messagebox, "showerror") as showerror:
            IqcApp._read_source(app)

        showerror.assert_called_once()
        self.assertEqual(busy_states, [True, False])

    def test_view_log_opens_jsonl_with_default_windows_association(self) -> None:
        class _Var:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value) -> None:
                self.value = value

        class _Logger:
            def __init__(self, path: Path) -> None:
                self.path = path

        with TemporaryDirectory() as temp:
            path = Path(temp) / "iqc_ui.jsonl"
            app = object.__new__(IqcApp)
            app.logger = _Logger(path)
            app.detail_var = _Var()
            events = []
            app._append_log = lambda *args, **kwargs: events.append((args, kwargs))
            with patch.object(ui_module.os, "startfile", create=True) as opener:
                IqcApp._view_log(app)
            opener.assert_called_once_with(str(path))
            self.assertTrue(path.is_file())
            self.assertEqual(app.detail_var.value, str(path))
            self.assertEqual(events[0][0], ("view_log",))

    def test_view_log_falls_back_to_notepad_when_jsonl_has_no_association(self) -> None:
        class _Var:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value) -> None:
                self.value = value

        class _Logger:
            def __init__(self, path: Path) -> None:
                self.path = path

        with TemporaryDirectory() as temp:
            path = Path(temp) / "iqc_ui.jsonl"
            app = object.__new__(IqcApp)
            app.logger = _Logger(path)
            app.detail_var = _Var()
            events = []
            app._append_log = lambda *args, **kwargs: events.append((args, kwargs))
            with patch.object(ui_module.os, "startfile", side_effect=OSError("association missing"), create=True):
                with patch.object(ui_module.subprocess, "Popen") as opener:
                    IqcApp._view_log(app)
            opener.assert_called_once_with(["notepad.exe", str(path)], close_fds=True)
            self.assertTrue(path.is_file())
            self.assertIn("Notepad", events[0][1]["detail"])

    def test_clear_orders_clears_text_and_bound_value(self) -> None:
        class _Text:
            def __init__(self) -> None:
                self.deleted = None

            def delete(self, start, end) -> None:
                self.deleted = (start, end)

        class _Var:
            def __init__(self) -> None:
                self.value = "8478993"

            def set(self, value) -> None:
                self.value = value

        class _Detail:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value) -> None:
                self.value = value

        app = object.__new__(IqcApp)
        app.orders_text = _Text()
        app.orders_var = _Var()
        app.detail_var = _Detail()
        IqcApp._clear_orders(app)
        self.assertEqual(app.orders_text.deleted, ("1.0", ui_module.tk.END))
        self.assertEqual(app.orders_var.value, "")
        self.assertIn("清空", app.detail_var.value)

    def test_select_all_orders_selects_every_tree_row(self) -> None:
        class _Tree:
            def __init__(self) -> None:
                self.selected = None
                self.focused = None
                self.seen = None

            def get_children(self):
                return ("order-1", "order-2")

            def selection_set(self, item_ids) -> None:
                self.selected = item_ids

            def focus(self, item_id) -> None:
                self.focused = item_id

            def see(self, item_id) -> None:
                self.seen = item_id

        app = object.__new__(IqcApp)
        app.tree = _Tree()
        IqcApp._select_all_orders(app)
        self.assertEqual(app.tree.selected, ("order-1", "order-2"))
        self.assertEqual(app.tree.focused, "order-1")
        self.assertEqual(app.tree.seen, "order-1")

    def _reset_stub_app(self, *, busy: bool = False):
        """构造不依赖 Tk 主循环的复位夹具，覆盖 UI 的内存状态边界。"""

        class _Text:
            def __init__(self) -> None:
                self.deleted: list[tuple[object, object]] = []

            def delete(self, start, end) -> None:
                self.deleted.append((start, end))

        class _Combo:
            def __init__(self) -> None:
                self.current_index = None

            def current(self, index: int) -> None:
                self.current_index = index

        class _Tree:
            def __init__(self) -> None:
                self.children = ["order-1"]
                self.selected = ("order-1",)
                self.deleted: list[str] = []

            def get_children(self):
                return tuple(self.children)

            def delete(self, item_id: str) -> None:
                self.deleted.append(item_id)
                if item_id in self.children:
                    self.children.remove(item_id)
                if item_id in self.selected:
                    self.selected = tuple(value for value in self.selected if value != item_id)

            def selection(self):
                return self.selected

            def insert(self, *_args, **_kwargs) -> None:
                self.children.append("inserted")

        app = object.__new__(IqcApp)
        app.state = UiBatchState()
        app.state.stage("2749943", Path("draft.sql"), status=UiOrderStatus.GENERATED_CONFIRM)
        app.source_var = self._Var("source.xlsx")
        app.orders_var = self._Var("2749943")
        app.orders_text = _Text()
        app.engineer_var = self._Var("合成工程师02")
        app.engineer_box = _Combo()
        app.manual_confirm_var = self._Var(False)
        app.skip_errors_var = self._Var(False)
        app.status_var = self._Var("旧状态")
        app.detail_var = self._Var("旧详情")
        app.header_status = self._Header()
        app.tree = _Tree()
        app._tree_orders = {"order-1": "2749943"}
        app._tree_links = {"order-1": {"result": Path("result.xlsx"), "sql": Path("draft.sql")}}
        app._engineer_display_names = ["合成工程师02"]
        # 模拟人工确认后的 provider 缓存；复位后必须清空，下一批不能复用。
        provider = RuntimeGenerationProvider(object(), object())
        provider.snapshot = object()
        provider.windchill_results = {"2749943": ()}
        provider.manual_confirmations = {"ITEM-OLD": object()}
        app.generator = SimpleNamespace(provider=provider)
        app._pending_confirmation_rows = (object(),)
        app._pending_confirmation_tasks = {"2749943": object()}
        app._pending_confirmation_engineer = object()
        app._pending_confirmation_path = Path("confirm.json")
        app._manual_resume_started = True
        app._active_import_orders = ("2749943",)
        app._cancel_event = threading.Event()
        app._close_requested = True
        app._busy = lambda: busy
        app.busy_states: list[bool] = []
        app._set_busy = lambda value: app.busy_states.append(value)
        app._update_action_states = lambda: None
        app.events: list[tuple[tuple[object, ...], dict[str, object]]] = []
        app._append_log = lambda *args, **kwargs: app.events.append((args, kwargs))
        return app

    def test_reset_ui_initializes_controls_and_allows_new_selection(self) -> None:
        """非 busy 复位清掉当前批次，随后可重新输入/选择而不触及日志。"""

        app = self._reset_stub_app()
        self.assertTrue(IqcApp._reset_ui(app))
        self.assertEqual(app.state.snapshot(), ())
        self.assertEqual(app.source_var.get(), "source.xlsx")
        self.assertEqual(app.orders_var.get(), "2749943")
        self.assertEqual(app.engineer_var.get(), "合成工程师02")
        self.assertEqual(app.orders_text.deleted, [])
        self.assertIsNone(app.engineer_box.current_index)
        self.assertEqual(app.manual_confirm_var.get(), True)
        self.assertEqual(app.skip_errors_var.get(), True)
        self.assertEqual(app._pending_confirmation_rows, ())
        self.assertIsNone(app._pending_confirmation_tasks)
        self.assertIsNone(app._pending_confirmation_engineer)
        self.assertIsNone(app._pending_confirmation_path)
        self.assertEqual(app._active_import_orders, ())
        self.assertEqual(app.busy_states, [False])
        self.assertEqual(app._tree_orders, {})
        self.assertEqual(app._tree_links, {})
        self.assertEqual(app.tree.selection(), ())
        self.assertEqual(app.events[-1][0], ("ui_reset",))
        self.assertEqual(app.events[-1][1]["status"], "SUCCESS")
        self.assertIsNone(app.generator.provider.snapshot)
        self.assertEqual(app.generator.provider.windchill_results, {})
        self.assertIsNone(app.generator.provider.manual_confirmations)

        # 复位后树选择映射为空；新的读取流程可重新写入映射，不会误引用旧单号。
        app.tree.selected = ()
        self.assertEqual(IqcApp._selected_orders(app), [])
        app.source_var.set("new-source.xlsx")
        app.orders_var.set("3348144")
        self.assertEqual(app.source_var.get(), "new-source.xlsx")
        self.assertEqual(app.orders_var.get(), "3348144")

    def test_reset_ui_blocks_while_worker_or_result_queue_is_active(self) -> None:
        """后台线程或待处理结果存在时阻断复位，避免旧结果异步回灌。"""

        class _AliveWorker:
            def is_alive(self) -> bool:
                return True

        app = self._reset_stub_app()
        app._worker_queue = ui_module.queue.Queue()
        app._callback_in_progress = False
        app._worker = _AliveWorker()
        # 使用真实 _busy 实现验证活动线程门禁，而非仅检查测试替身返回值。
        app._busy = IqcApp._busy.__get__(app)
        with patch.object(ui_module.messagebox, "showwarning") as showwarning:
            self.assertFalse(IqcApp._reset_ui(app))
            # 线程退出但结果仍在 Tk 队列时也必须阻断；否则旧结果会回灌。
            app._worker = None
            app._worker_queue.put(("read", None, None))
            self.assertFalse(IqcApp._reset_ui(app))
        self.assertEqual(len(app.state.snapshot()), 1)
        self.assertEqual(app.source_var.get(), "source.xlsx")
        self.assertIn("正在处理", app.status_var.get())
        self.assertEqual(app.events[-1][0], ("ui_reset_blocked",))
        self.assertEqual(showwarning.call_count, 2)

    def test_reset_ui_provider_failure_keeps_batch_state_intact(self) -> None:
        """provider 清理失败时不得先清空 UiBatchState，便于安全重试。"""

        class _FailingProvider:
            def reset_runtime_state(self) -> None:
                raise RuntimeError("provider reset failed")

        app = self._reset_stub_app()
        app.generator = SimpleNamespace(provider=_FailingProvider())
        with patch.object(ui_module.messagebox, "showwarning") as showwarning:
            self.assertFalse(IqcApp._reset_ui(app))
        self.assertEqual(len(app.state.snapshot()), 1)
        self.assertEqual(app.source_var.get(), "source.xlsx")
        self.assertEqual(app.orders_var.get(), "2749943")
        self.assertIn("运行时批次状态清理失败", app.status_var.get())
        self.assertEqual(app.events[-1][0], ("ui_reset_blocked",))
        self.assertEqual(app.events[-1][1]["status"], "ERROR")
        showwarning.assert_called_once()

    def test_reset_ui_blocks_uncertain_importing_state_without_clearing_provider(self) -> None:
        """worker 已退出但行仍 IMPORTING 时，保留快照并要求先核查证据。"""

        app = self._reset_stub_app()
        app.state = UiBatchState()
        app.state.stage("2749943", Path("draft.sql"), status=UiOrderStatus.IMPORTING)
        app._busy = lambda: False  # 模拟回调异常后 worker 指针已释放
        previous_snapshot = app.generator.provider.snapshot
        with patch.object(ui_module.messagebox, "showwarning") as showwarning:
            self.assertFalse(IqcApp._reset_ui(app))
        self.assertEqual(app.state.get("2749943").status, UiOrderStatus.IMPORTING)
        self.assertIs(app.generator.provider.snapshot, previous_snapshot)
        self.assertIn("导入结果不确定", app.status_var.get())
        self.assertIn("核查", app.detail_var.get())
        self.assertEqual(app.events[-1][0], ("ui_reset_blocked",))
        self.assertEqual(app.events[-1][1]["status"], "BLOCKED")
        showwarning.assert_called_once()

    def test_reset_ui_after_manual_confirmation_cancel_clears_pending_state(self) -> None:
        """人工确认对话框取消后，复位应清掉挂起引用并恢复初始门禁。"""

        app = self._reset_stub_app()
        app._manual_resume_started = False  # 模拟对话框返回“取消”后的状态
        self.assertTrue(IqcApp._reset_ui(app))
        self.assertFalse(app._manual_resume_started)
        self.assertEqual(app._pending_confirmation_rows, ())
        self.assertIsNone(app._pending_confirmation_tasks)
        self.assertIsNone(app._pending_confirmation_engineer)
        self.assertIsNone(app._pending_confirmation_path)
        self.assertEqual(app.state.snapshot(), ())

    def test_worker_callback_exception_releases_busy_and_keeps_reset_available(self) -> None:
        """回调异常不能卡死队列；状态释放后仍提示可用复位入口。"""

        class _DeadWorker:
            def is_alive(self) -> bool:
                return False

        class _Header:
            def __init__(self) -> None:
                self.values = []

            def configure(self, **kwargs) -> None:
                self.values.append(kwargs)

        app = object.__new__(IqcApp)
        app._worker_queue = ui_module.queue.Queue()
        app._worker = _DeadWorker()
        app._callback_in_progress = False
        app._callback_read = lambda _value, _error: (_ for _ in ()).throw(RuntimeError("回调失败"))
        app._append_log = lambda *args, **kwargs: None
        app.status_var = self._Var("")
        app.header_status = _Header()
        app._set_busy = lambda _busy: None
        app._update_action_states = lambda: None
        app.after_calls: list[tuple[object, object]] = []
        app.after = lambda delay, callback: app.after_calls.append((delay, callback))
        app._worker_queue.put(("read", None, None))

        IqcApp._drain_worker_queue(app)

        self.assertIsNone(app._worker)
        self.assertFalse(app._callback_in_progress)
        self.assertIn("核查当前批次结果", app.status_var.get())
        self.assertTrue(app.after_calls)


class SyntheticGroupUiTests(unittest.TestCase):
    def test_controlled_review_blocked_true_paper_paper_only_and_resume_not_released(self):
        from src.paper_standard_exclusion import PaperStandardMatch, MATCH_EXCLUDED, MATCH_UNDETERMINED
        for review in (False, True):
            with self.subTest(review=review), TemporaryDirectory() as temp:
                app = object.__new__(IqcApp)
                app.entry_client = None
                app.paths = SimpleNamespace(output_dir=Path(temp))
                app.state = UiBatchState()
                app.generator = object()
                app.status_var = UiHelperTests._Var("")
                app.detail_var = UiHelperTests._Var("")
                app.header_status = UiHelperTests._Header()
                app._append_log = lambda *_args, **_kwargs: None
                app._refresh_tree = lambda: None
                app._select_all_orders = lambda: None
                app._set_busy = lambda _busy: None
                app._show_paper_exclusion_dialog = lambda _batch: None
                app._manual_resume_started = False
                app._begin_manual_confirmation = lambda *_args, **_kwargs: self.fail("controlled rows must not enter manual candidate dialog")
                match = PaperStandardMatch(MATCH_UNDETERMINED if review else MATCH_EXCLUDED, item_code="CONTROL", identity_rule_id="DEMO_GROUP_DEMO_CONTROL_X100_BOARD")
                batch = GenerationBatch(paper_only_orders=("ALL",), paper_exclusions={} if review else {"ALL": (match,)}, paper_review_required={"ALL": (match,)} if review else {})
                result = ReadGenerateResult({"ALL": OrderTask("ALL")}, batch, (), object())
                with patch.object(ui_module.messagebox, "showerror", side_effect=AssertionError("unexpected UI exception")):
                    IqcApp._read_finished(app, result, None)
                    self.assertEqual(app.state.get("ALL").status, UiOrderStatus.BLOCKED if review else UiOrderStatus.PAPER_ONLY)
                    app._pending_confirmation_tasks = {"ALL": OrderTask("ALL")}
                    app._pending_confirmation_engineer = object()
                    app._manual_resume_started = True
                    IqcApp._manual_resume_finished(app, batch, None)
                self.assertEqual(app.state.get("ALL").status, UiOrderStatus.BLOCKED if review else UiOrderStatus.PAPER_ONLY)
                self.assertEqual(app._generated_outcomes, {})


if __name__ == "__main__":
    unittest.main()
