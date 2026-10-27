from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from pathlib import Path

from src.models import Engineer
from src.models import normalize_procedure_name
from src.sql_generator import SqlAttachedRow, SqlContextRow, SqlOrder, SqlRecord, render_order_sql
from src.mysql_query import (
    ActiveUser,
    ATTACHED_SQL_NULL_SENTINEL,
    ModelMappingError,
    ProcedureModel,
    BatActiveUserProvider,
    MySqlQueryError,
    ReadonlyMysqlClient,
    ReadonlyQueryService,
    READONLY_SQL,
    map_attached_model_row,
    map_procedure_context_row,
    map_procedure_model_row,
    parse_mysql_batch_tsv_with_provenance,
    parse_mysql_batch_tsv,
    parse_active_users_tsv,
    resolve_preconfigured_engineer,
    select_unique_active_model,
    validate_readonly_sql,
)


class MysqlQueryTests(unittest.TestCase):
    def test_readonly_guard_accepts_columns_containing_keyword_substrings(self) -> None:
        sql = validate_readonly_sql(
            "SELECT d.create_datetime, d.last_update_time, d.updated_by FROM draft d;"
        )
        self.assertEqual(
            sql,
            "SELECT d.create_datetime, d.last_update_time, d.updated_by FROM draft d;",
        )

    def test_readonly_guard_rejects_actual_dml_and_ddl_keyword_tokens(self) -> None:
        statements = (
            "CREATE TABLE x (id INT)",
            "INSERT INTO x VALUES (1)",
            "UPDATE x SET id = 2",
            "DELETE FROM x",
            "REPLACE INTO x VALUES (1)",
            "ALTER TABLE x ADD y INT",
            "DROP TABLE x",
            "TRUNCATE TABLE x",
            "GRANT SELECT ON x TO y",
            "WITH x AS (DELETE FROM t RETURNING id) SELECT id FROM x",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                with self.assertRaises(MySqlQueryError):
                    validate_readonly_sql(statement)

    def test_attached_select_marks_nullable_slots_with_explicit_sentinel(self) -> None:
        sql = READONLY_SQL["attached_models"]
        self.assertEqual(sql.count(ATTACHED_SQL_NULL_SENTINEL), 20)
        self.assertIn("IF(inspection_item1 IS NULL", sql)
        self.assertIn("IF(standard_range9 IS NULL", sql)
        # The two model-source metadata fields use the same explicit sentinel
        # while retaining their raw values/provenance; Draft/Record schemas
        # remain unchanged pending the G0 notes/name decision.
        self.assertIn("AS attached_table_name", sql)
        self.assertIn("AS notes", sql)

    def test_parse_mysql_batch_tsv_decodes_embedded_controls_and_backslashes(self) -> None:
        text = "10\t外观\\t尺寸\\n第二行\\r第三\\\\尾\t合格\t1\r\n"
        rows = parse_mysql_batch_tsv(text)
        self.assertEqual(rows, [["10", "外观\t尺寸\n第二行\r第三\\尾", "合格", "1"]])

    def test_parse_mysql_batch_tsv_preserves_unknown_and_dangling_escapes(self) -> None:
        self.assertEqual(parse_mysql_batch_tsv("a\\x\tb\\"), [["a\\x", "b\\"]])
        # MySQL emits two backslashes for a literal ``\\n`` in the source
        # value; the parser must restore the literal pair, not a newline.
        self.assertEqual(parse_mysql_batch_tsv(r"a\\n"), [[r"a\n"]])
        self.assertEqual(parse_mysql_batch_tsv("\t\t\r\n"), [["", "", ""]])
        self.assertEqual(parse_mysql_batch_tsv("a\\\nnext\tb\n"), [["a\\"], ["next", "b"]])
        self.assertEqual(parse_mysql_batch_tsv("a\\\tb\n"), [["a\\", "b"]])
        self.assertEqual(parse_mysql_batch_tsv("\ufeff1\tb\n"), [["1", "b"]])

    def test_attached_tsv_provenance_distinguishes_null_empty_and_na(self) -> None:
        raw_row = ["20", "10"] + ["项目", "", ATTACHED_SQL_NULL_SENTINEL, "NA"] + [""] * 5 + [
            "范围", "", ATTACHED_SQL_NULL_SENTINEL, "NA"
        ] + [""] * 5 + ["附表", "备注"]
        raw_tsv = "\t".join(raw_row) + "\n"
        parsed = parse_mysql_batch_tsv_with_provenance(raw_tsv)
        self.assertEqual(parsed.raw_tsv, raw_tsv.encode("utf-8"))
        self.assertEqual(parsed.raw_rows, (raw_tsv.rstrip("\n"),))
        self.assertEqual(parsed.null_provenance[0][4], True)  # item3 / source col 4
        self.assertFalse(parsed.null_provenance[0][3])  # item2 is an empty string
        self.assertFalse(parsed.null_provenance[0][5])  # item4 is literal NA

        attached = map_attached_model_row(
            parsed[0],
            null_provenance=parsed.null_provenance[0][2:20],
            raw_tsv=parsed.raw_tsv,
            source_row_number=1,
        )
        self.assertEqual(attached.inspection_items[:4], ("项目", "", None, "NA"))
        self.assertEqual(attached.standard_ranges[:4], ("范围", "", None, "NA"))
        self.assertEqual(attached.raw_inspection_items[:4], ("项目", "", ATTACHED_SQL_NULL_SENTINEL, "NA"))
        self.assertEqual(attached.raw_tsv, raw_tsv.encode("utf-8"))
        self.assertEqual(attached.raw_tsv_row, raw_tsv.encode("utf-8"))
        self.assertEqual(attached.source_row_number, 1)
        self.assertEqual(attached.null_source_columns, (5, 14))
        self.assertEqual(attached.metadata_null_provenance, (False, False))

        # Without the parser's explicit mask, a legal business literal NULL
        # must not be guessed as SQL NULL.
        literal_row = list(parsed[0])
        literal_row[4] = "NULL"
        literal_row[13] = "NULL"
        literal = map_attached_model_row(literal_row)
        self.assertEqual(literal.inspection_items[2], "NULL")
        self.assertEqual(literal.standard_ranges[2], "NULL")
        sentinel_literal = map_attached_model_row(parsed[0])
        self.assertEqual(sentinel_literal.inspection_items[2], ATTACHED_SQL_NULL_SENTINEL)

        metadata_row = list(parsed[0])
        metadata_row[20] = ATTACHED_SQL_NULL_SENTINEL
        metadata_row[21] = "备注第一行\\n备注第二行"
        metadata_tsv = "\t".join(metadata_row) + "\n"
        metadata_parsed = parse_mysql_batch_tsv_with_provenance(metadata_tsv)
        metadata = map_attached_model_row(
            metadata_parsed[0],
            null_provenance=metadata_parsed.null_provenance[0],
            raw_tsv=metadata_parsed.raw_tsv,
            source_row_number=1,
        )
        self.assertIsNone(metadata.attached_table_name)
        self.assertEqual(metadata.notes, "备注第一行\n备注第二行")
        self.assertEqual(metadata.raw_attached_table_name, ATTACHED_SQL_NULL_SENTINEL)
        self.assertEqual(metadata.raw_notes, "备注第一行\n备注第二行")
        self.assertEqual(metadata.metadata_null_provenance, (True, False))

        # A literal NULL in metadata, without the explicit mask, remains a
        # literal and is never treated as SQL NULL.
        literal_metadata_row = list(metadata_parsed[0])
        literal_metadata_row[20] = ""
        literal_metadata_row[21] = "NULL"
        literal_metadata = map_attached_model_row(literal_metadata_row)
        self.assertEqual(literal_metadata.attached_table_name, "")
        self.assertEqual(literal_metadata.notes, "NULL")

        generated = SqlAttachedRow.from_template(
            attached.inspection_items,
            attached.standard_ranges,
            inspection_number="DEMO-ORDER-001",
            notes=attached.notes,
        )
        self.assertEqual(json.loads(generated.inspection_content)[0]["inspectionContent3"], "NA")
        record = SqlRecord(
            item_code="M-001",
            item_name="物项",
            quantity=1,
            supplier="供应商",
            procedure_name="检查单",
            procedure_model_id=10,
            engineer_id=1,
            engineer_name="工程师",
            engineer_dept_id="D1",
            trigger_basis="DEMO-ORDER-001",
            check_number=1,
            contexts=(SqlContextRow("1", "外观", "无缺陷"),),
            attached_tables=(generated,),
        )
        rendered = render_order_sql(SqlOrder("DEMO-ORDER-001", (record,)))
        self.assertIn("'NA'", rendered)
        self.assertIn(generated.inspection_content, json.loads(json.dumps(generated.inspection_content)))

    def test_parse_active_users_tsv(self) -> None:
        users = parse_active_users_tsv("1\t合成工程师02\tdemo_user\t1001\tD1\n")
        self.assertEqual(users[0], ActiveUser("1", "合成工程师02", "demo_user", "1001", "D1"))

    def test_parse_active_users_tsv_decodes_mysql_batch_escapes(self) -> None:
        users = parse_active_users_tsv("1\t合成\\t人员\tdemo\\\\user\t1001\tD1\n")
        self.assertEqual(users[0].name, "合成\t人员")
        self.assertEqual(users[0].username, "demo\\user")

    def test_name_only_config_resolves_unique_user(self) -> None:
        engineer = Engineer(user_id="", name="合成工程师02", dept_id="", display_name="合成工程师02")
        user = resolve_preconfigured_engineer(engineer, [ActiveUser("1", "合成工程师02", "demo_user", "1001", "D1")])
        self.assertEqual(user.user_id, "1")

    def test_ambiguous_name_is_rejected(self) -> None:
        engineer = Engineer(user_id="", name="合成工程师02", dept_id="", display_name="合成工程师02")
        with self.assertRaises(MySqlQueryError):
            resolve_preconfigured_engineer(
                engineer,
                [ActiveUser("1", "合成工程师02", "demo_user", "1001", "D1"), ActiveUser("2", "合成工程师02", "demo_user2", "1002", "D2")],
            )

    def test_procedure_name_normalization_supports_prefixed_and_direct_names(self) -> None:
        prefixed = "DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_合成类别06进货检查单"
        self.assertEqual(normalize_procedure_name(prefixed), "合成类别06进货检查单")
        self.assertEqual(
            normalize_procedure_name("DEMO-C-DEMOFORM-DEMOUNIT-PMBG########-01-##_合成类别06进货检查单"),
            "合成类别06进货检查单",
        )
        self.assertEqual(
            normalize_procedure_name("DEMO-C_DEMOFORM-DEMOUNIT-PMBG########-01-##_合成类别06进货检查单"),
            "合成类别06进货检查单",
        )
        self.assertEqual(normalize_procedure_name("《合成类别06进货检查单》"), "合成类别06进货检查单")
        self.assertEqual(
            normalize_procedure_name("合成类别03进货检查单 满足：DEMO-C-DEMOFORM-DEMOUNIT-TEST#G******-01"),
            "合成类别03进货检查单",
        )
        self.assertEqual(
            normalize_procedure_name("DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_《合成类别06进货检查单》"),
            "合成类别06进货检查单",
        )
        self.assertEqual(normalize_procedure_name("合成类别06进货检查单"), "合成类别06进货检查单")

    def test_procedure_name_normalization_cleans_nested_wrappers_suffix_noise_and_is_idempotent(self) -> None:
        cases = {
            # 书名号必须先剥，才能识别书名号内部的文档编号前缀。
            "《DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_合成类别06进货检查单》": "合成类别06进货检查单",
            # 前缀剥离后再剥名称外层书名号。
            "DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_《合成类别06进货检查单》": "合成类别06进货检查单",
            "DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_合成类别03进货检查单 满足：DEMO-C-DEMOFORM-DEMOUNIT-TEST#G******-01": "合成类别03进货检查单",
            # Windchill 有时不在“检查单”和“满足”之间返回空格。
            "DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_合成类别03进货检查单满足:DEMO-C-DEMOFORM-DEMOUNIT-TEST#G******-01": "合成类别03进货检查单",
            "《合成类别03进货检查单 满足：DEMO-C-DEMOFORM-DEMOUNIT-TEST#G******-01》；": "合成类别03进货检查单",
            "《合成类别03进货检查单》满足: DEMO-C-DEMOFORM-DEMOUNIT-TEST#G******-01": "合成类别03进货检查单",
            "DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_合成类别06进货检查单.docx": "合成类别06进货检查单",
            "DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_合成类别06进货检查单；": "合成类别06进货检查单",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                actual = normalize_procedure_name(raw)
                self.assertEqual(actual, expected)
                self.assertEqual(normalize_procedure_name(actual), actual)

    def test_procedure_name_normalization_handles_whitelisted_extensions_and_closing_marks(self) -> None:
        prefix = "DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_合成类别06进货检查单"
        for extension in (".doc", ".DOC", ".xls", ".XLSX", ".pdf", ".PDF"):
            with self.subTest(extension=extension):
                self.assertEqual(
                    normalize_procedure_name(f"《{prefix}{extension}》"),
                    "合成类别06进货检查单",
                )
        for closing_mark in ("》", "）", "】"):
            with self.subTest(closing_mark=closing_mark):
                self.assertEqual(
                    normalize_procedure_name(f"《{prefix}》{closing_mark}"),
                    "合成类别06进货检查单",
                )

    def test_procedure_name_normalization_does_not_strip_unapproved_tail_text(self) -> None:
        # 尾部业务编码和未知扩展名不是白名单噪声，必须保留。
        self.assertEqual(
            normalize_procedure_name("合成类别06进货检查单 DEMO-C-DEMOFORM-DEMOUNIT-01"),
            "合成类别06进货检查单 DEMO-C-DEMOFORM-DEMOUNIT-01",
        )
        self.assertEqual(
            normalize_procedure_name("合成类别06进货检查单.custom"),
            "合成类别06进货检查单.custom",
        )

    def test_procedure_name_normalization_keeps_ordinary_ascii_underscore_name(self) -> None:
        # 回退规则仅接受形似文档编号的多段连字符前缀，并要求名称仍包含中文；
        # 普通 ASCII+下划线名称不能被误剥。
        self.assertEqual(normalize_procedure_name("ABC_foo_bar"), "ABC_foo_bar")
        self.assertEqual(
            normalize_procedure_name("DEMO07-DOC102-DEMOFORM-004_DemoControl Z200 DCS模块进货检验规范"),
            "DemoControl Z200 DCS模块进货检验规范",
        )

    def test_prefixed_windchill_name_matches_unique_active_model(self) -> None:
        model = ProcedureModel("10", "合成类别06进货检查单", "GB", "合格", "1", "0", "2")
        selected = select_unique_active_model(
            [model], "DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_合成类别06进货检查单"
        )
        self.assertEqual(selected.id, "10")

    def test_outer_wrapped_prefixed_name_matches_and_reports_one_normalized_key(self) -> None:
        model = ProcedureModel("10", "合成类别06进货检查单", "GB", "合格", "1", "0", "2")
        events: list[dict[str, object]] = []
        selected = select_unique_active_model(
            [model],
            "《DEMO-C-DEMOFORM-DEMOUNIT-SAMP#E######-01-04_合成类别06进货检查单》",
            diagnostics_callback=events.append,
        )
        self.assertEqual(selected.id, "10")
        self.assertEqual(events[0]["normalized_name"], "合成类别06进货检查单")
        self.assertEqual(events[0]["match_key"], "合成类别06进货检查单")

    def test_model_name_uses_vba_like_contains_semantics(self) -> None:
        model = ProcedureModel("11", "合成类别06进货检查单（电气）", "GB", "合格", "1", "0", "2")
        selected = select_unique_active_model([model], "合成类别06进货检查单")
        self.assertEqual(selected.id, "11")

    def test_exact_canonical_name_wins_over_prefixed_substring_candidate(self) -> None:
        selected = select_unique_active_model(
            [
                ProcedureModel("16", "合成类别04进货检查单", "GB", "合格", "1", "0", "2"),
                ProcedureModel("20", "合成类别05进货检查单", "GB", "合格", "1", "0", "2"),
            ],
            "合成类别05进货检查单",
            prefer_exact=True,
        )
        self.assertEqual(selected.id, "20")

    def test_model_matching_accepts_vba_check_and_inspection_name_alias(self) -> None:
        model = ProcedureModel("12", "合成类别03进货检验单", "GB", "合格", "1", "0", "2")
        selected = select_unique_active_model([model], "合成类别03进货检查单")
        self.assertEqual(selected.id, "12")

    def test_model_matching_diagnostics_expose_expected_key_and_status_counts(self) -> None:
        events: list[dict[str, object]] = []
        model = ProcedureModel("13", "合成类别03进货检查单", "GB", "合格", "1", "0", "2")
        selected = select_unique_active_model(
            [model], "合成类别03进货检查单", diagnostics_callback=events.append
        )
        self.assertEqual(selected.id, "13")
        self.assertEqual(events[0]["status"], "SUCCESS")
        self.assertEqual(events[0]["expected_name"], "合成类别03进货检查单")
        self.assertEqual(events[0]["match_key"], "合成类别03进货检查单")
        self.assertEqual(events[0]["vba_alias_match_key"], "合成类别03进货检验单")
        self.assertEqual(events[0]["name_match_count"], 1)
        self.assertEqual(events[0]["active_name_match_count"], 1)

    def test_model_matching_diagnostics_show_name_hit_but_invalid_status(self) -> None:
        events: list[dict[str, object]] = []
        model = ProcedureModel("14", "合成类别03进货检查单", "GB", "合格", "0", "0", "2")
        with self.assertRaises(ModelMappingError):
            select_unique_active_model(
                [model], "合成类别03进货检查单", diagnostics_callback=events.append
            )
        self.assertEqual(events[0]["status"], "ERROR")
        self.assertEqual(events[0]["name_match_count"], 1)
        self.assertEqual(events[0]["active_name_match_count"], 0)
        self.assertIn("is_lasted_version=0", str(events[0]["matched_model_details"]))

    def test_readonly_sql_rejects_dml(self) -> None:
        with self.assertRaises(MySqlQueryError):
            validate_readonly_sql("DELETE FROM dvadmin_system_users")
        self.assertTrue(validate_readonly_sql("SELECT id FROM dvadmin_system_users").startswith("SELECT"))

    def test_readonly_client_uses_temp_sql_and_parses_tsv_without_running_mysql(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bat = Path(temp) / "run_readonly_query.bat"
            bat.touch()

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                result_file = Path(command[-1])
                sql_file = Path(command[-2])
                self.assertEqual(sql_file.read_text(encoding="utf-8"), "SELECT id FROM t;")
                result_file.write_text("1\t合成\\n人员\r\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            client = ReadonlyMysqlClient(readonly_bat=bat, runner=runner)
            rows = client.execute("SELECT id FROM t")
            self.assertEqual(rows, [["1", "合成\n人员"]])

    def test_cmd_wrappers_are_hidden_on_windows_without_losing_capture_kwargs(self) -> None:
        class StartupInfo:
            def __init__(self) -> None:
                self.dwFlags = 0
                self.wShowWindow = -1

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            readonly_bat = root / "run_readonly_query.bat"
            user_bat = root / "export_active_users.bat"
            tsv_path = root / "active_users.tsv"
            readonly_bat.touch()
            user_bat.touch()
            observed: list[dict[str, object]] = []

            def readonly_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                observed.append(dict(kwargs))
                Path(command[-1]).write_text("1\r\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, b"out", b"")

            def user_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                observed.append(dict(kwargs))
                tsv_path.write_text("u1\tEngineer\tengineer\tW1\tD1\r\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "out", "")

            # Python 3.8 将括号内多个上下文管理器当作 tuple；保持同样隔离及断言。
            with ExitStack() as stack:
                stack.enter_context(patch("src.mysql_query.os.name", "nt"))
                stack.enter_context(patch("src.mysql_query.subprocess.CREATE_NO_WINDOW", 0x83344017, create=True))
                stack.enter_context(patch("src.mysql_query.subprocess.STARTUPINFO", StartupInfo, create=True))
                stack.enter_context(patch("src.mysql_query.subprocess.STARTF_USESHOWWINDOW", 1, create=True))
                stack.enter_context(patch("src.mysql_query.subprocess.SW_HIDE", 0, create=True))
                self.assertEqual(ReadonlyMysqlClient(readonly_bat=readonly_bat, runner=readonly_runner).execute("SELECT 1"), [["1"]])
                users = BatActiveUserProvider(
                    bat_path=user_bat,
                    tsv_path=tsv_path,
                    error_log_path=root / "active_users.err.log",
                    runner=user_runner,
                ).query()

            self.assertEqual(users[0].name, "Engineer")
            for kwargs in observed:
                self.assertEqual(kwargs["creationflags"], 0x83344017)
                self.assertTrue(kwargs["capture_output"])
                self.assertFalse(kwargs["check"])
                startupinfo = kwargs["startupinfo"]
                self.assertEqual(getattr(startupinfo, "dwFlags"), 1)
                self.assertEqual(getattr(startupinfo, "wShowWindow"), 0)

    def test_readonly_client_drops_unsupported_r44_config_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            bat = root / "run_readonly_query.bat"
            bat.touch()
            external = root / "protected" / "mysql_connection.bat"
            external.parent.mkdir()
            external.touch()
            forwarded: list[str | None] = []

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                environment = kwargs.get("env")
                self.assertIsInstance(environment, dict)
                forwarded.append(environment.get("IQC_MYSQL_CONFIG_FILE"))  # type: ignore[union-attr]
                Path(command[-1]).write_text("1\r\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            client = ReadonlyMysqlClient(readonly_bat=bat, runner=runner)
            cases = (
                (str(external), None),
                ("relative/mysql_connection.bat", None),
                (str(root / "missing.bat"), None),
            )
            for configured, expected in cases:
                with patch.dict(
                    "src.mysql_query.os.environ",
                    {"IQC_MYSQL_CONFIG_FILE": configured},
                    clear=True,
                ):
                    self.assertEqual(client.execute("SELECT 1"), [["1"]])
                self.assertEqual(forwarded[-1], expected)

    def test_readonly_client_preserves_path_without_claiming_r44_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            bat = root / "run_readonly_query.bat"
            bat.touch()
            client_dir = root / "mysql-client"
            client_dir.mkdir()
            (client_dir / "mysql.exe").touch()
            observed_paths: list[str] = []

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                environment = kwargs.get("env")
                self.assertIsInstance(environment, dict)
                observed_paths.append(environment.get("PATH", ""))  # type: ignore[union-attr]
                Path(command[-1]).write_text("1\r\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            client = ReadonlyMysqlClient(readonly_bat=bat, runner=runner)
            with patch.dict("src.mysql_query.os.environ", {"PATH": "C:\\Windows\\System32"}, clear=True):
                self.assertEqual(client.execute("SELECT 1"), [["1"]])
            self.assertEqual(
                observed_paths,
                ["C:\\Windows\\System32"],
            )

    def test_readonly_client_surfaces_redacted_stderr_and_err_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bat = root / "run_readonly_query.bat"
            bat.touch()

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                result_file = Path(command[-1])
                Path(f"{result_file}.err.log").write_text(
                    "ERROR 1045 --password=secret-from-err host=127.0.0.1 user=root\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    command,
                    1,
                    "",
                    "MYSQL_PASSWORD=secret-from-stderr database=example_database_legacy",
                )

            client = ReadonlyMysqlClient(readonly_bat=bat, runner=runner)
            with self.assertRaises(MySqlQueryError) as context:
                client.execute("SELECT id FROM t", query_label="procedure_models")
            error = context.exception
            self.assertEqual(error.query_stage, "procedure_models")
            self.assertEqual(error.exit_code, 1)
            self.assertNotIn("secret-from-err", str(error))
            self.assertNotIn("secret-from-stderr", error.safe_detail)
            self.assertNotIn("example_database_legacy", error.safe_detail)
            self.assertIn("credential redacted", error.safe_detail)

    def test_readonly_client_decodes_gb18030_and_writes_traceable_diagnostic(self) -> None:
        """现场中文 Windows 输出必须保留可读错误，并留下编码/哈希证据。"""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bat = root / "run_readonly_query.bat"
            bat.touch()
            diagnostic_path = root / "logs" / "mysql_query_diagnostic.jsonl"
            expected = "MySQL 只读查询失败：字符集设置失败"

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                result_file = Path(command[-1])
                Path(f"{result_file}.err.log").write_bytes(
                    (expected + " --password=secret-from-err").encode("gb18030")
                )
                return subprocess.CompletedProcess(
                    command,
                    255,
                    b"",
                    (expected + " MYSQL_PASSWORD=secret-from-stderr").encode("gb18030"),
                )

            client = ReadonlyMysqlClient(
                readonly_bat=bat,
                runner=runner,
                diagnostic_log_path=diagnostic_path,
            )
            with self.assertRaises(MySqlQueryError) as context:
                client.execute("SELECT id FROM t", query_label="procedure_models")

            error = context.exception
            self.assertIn(expected, str(error))
            self.assertNotIn("���", str(error))
            events = [
                json.loads(line)
                for line in diagnostic_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual([event["event"] for event in events], [
                "mysql_readonly_query_start",
                "mysql_readonly_query_result",
            ])
            result_fields = events[-1]["fields"]
            self.assertIn(result_fields["stderr_encoding"], {"cp936", "gb18030"})
            self.assertIn(result_fields["err_log_encoding"], {"cp936", "gb18030"})
            self.assertEqual(result_fields["exit_code"], "255")
            self.assertIn(expected, result_fields["safe_detail"])
            self.assertNotIn("secret-from-err", json.dumps(events, ensure_ascii=False))
            self.assertNotIn("secret-from-stderr", json.dumps(events, ensure_ascii=False))
            self.assertNotIn("SELECT id FROM t", json.dumps(events, ensure_ascii=False))

    def test_readonly_client_diagnostic_matches_r44_config_and_client_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            bat = root / "run_readonly_query.bat"
            bat.touch()
            (root / "mysql_connection.bat").write_text("placeholder", encoding="utf-8")
            external = root / "protected" / "mysql_connection.bat"
            external.parent.mkdir()
            external.write_text("external-secret-must-not-be-reported", encoding="utf-8")
            client_path = root / "mysql-client" / "mysql.exe"
            client_path.parent.mkdir()
            client_path.write_bytes(b"mysql-client-fixture")
            diagnostic_path = root / "mysql_query_diagnostic.jsonl"

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                Path(command[-1]).write_text("1\r\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, b"", b"")

            client = ReadonlyMysqlClient(
                readonly_bat=bat,
                runner=runner,
                diagnostic_log_path=diagnostic_path,
            )
            with patch.dict("src.mysql_query.os.environ", {"IQC_MYSQL_CONFIG_FILE": str(external)}, clear=True):
                self.assertEqual(client.execute("SELECT 1", query_label="active_users"), [["1"]])

            events = [
                json.loads(line)
                for line in diagnostic_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            start_fields = events[0]["fields"]
            result_fields = events[1]["fields"]
            self.assertEqual(start_fields["config_source"], "package_default")
            self.assertEqual(start_fields["config_exists"], "True")
            self.assertEqual(start_fields["config_path"], str(root / "mysql_connection.bat"))
            self.assertEqual(start_fields["config_override_supported"], "False")
            self.assertEqual(start_fields["config_override_ignored"], "True")
            self.assertEqual(start_fields["config_sha256"], hashlib.sha256(b"placeholder").hexdigest())
            self.assertEqual(start_fields["mysql_client_source"], "config_mysql_exe")
            self.assertEqual(start_fields["mysql_client_exists"], "not_evaluated")
            self.assertEqual(start_fields["mysql_client_path"], "")
            self.assertEqual(start_fields["mysql_client_path_fallback_supported"], "False")
            self.assertEqual(start_fields["mysql_client_version_probe"], "not_run_config_not_executed")
            self.assertEqual(start_fields["bundled_mysql_candidate_exists"], "True")
            self.assertEqual(start_fields["bundled_mysql_candidate_sha256"], hashlib.sha256(b"mysql-client-fixture").hexdigest())
            self.assertNotIn("external-secret-must-not-be-reported", json.dumps(events))
            self.assertNotIn(str(external), json.dumps(events))
            self.assertEqual(result_fields["status"] if "status" in result_fields else events[1]["status"], "SUCCESS")
            self.assertEqual(result_fields["row_count"], "1")

    def test_model_rows_are_mapped_and_unique_active_model_is_required(self) -> None:
        model = map_procedure_model_row(["10", "检查单_示例", "GB", "合格", "1", "0", "2"])
        self.assertEqual(model, ProcedureModel("10", "检查单_示例", "GB", "合格", "1", "0", "2"))
        context = map_procedure_context_row(["10", "1", "外观", "无缺陷"])
        self.assertEqual(context.context_number, "1")
        attached = map_attached_model_row(["20", "10", *(["项目"] * 9), *(["范围"] * 9)])
        self.assertEqual(len(attached.inspection_items), 9)
        attached_with_metadata = map_attached_model_row(
            ["21", "10", *( ["项目"] * 9), *( ["范围"] * 9), "电源附表", "备注"]
        )
        self.assertEqual(attached_with_metadata.attached_table_name, "电源附表")
        self.assertEqual(attached_with_metadata.notes, "备注")
        selected = select_unique_active_model([model], "检查单_示例")
        self.assertEqual(selected.id, "10")
        with self.assertRaises(ModelMappingError):
            select_unique_active_model([model, model], "检查单_示例")

    def test_readonly_query_service_loads_all_four_snapshot_parts(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.labels: list[str | None] = []

            def execute(self, sql: str, *, query_label: str | None = None) -> list[list[str]]:
                self.calls.append(sql)
                self.labels.append(query_label)
                if "qcauto_procedure_model_check_context" in sql:
                    return [["10", "1", "外观", "无缺陷"]]
                if "qcauto_entry_attached_table_model" in sql:
                    return [["20", "10", *(["项目"] * 9), *(["范围"] * 9)]]
                if "dvadmin_system_users" in sql:
                    return [["1", "合成工程师02", "demo_user", "1001", "D1"]]
                return [["10", "检查单_示例", "GB", "合格", "1", "0", "2"]]

        client = FakeClient()
        snapshot = ReadonlyQueryService(client).load_snapshot()  # type: ignore[arg-type]
        self.assertEqual(len(snapshot.procedure_models), 1)
        self.assertEqual(snapshot.active_users[0].name, "合成工程师02")
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(client.labels, ["procedure_models", "procedure_contexts", "attached_models", "active_users"])

    def test_readonly_query_service_keeps_attached_raw_and_metadata_provenance(self) -> None:
        raw_row = ["20", "10"] + ["项目", "", ATTACHED_SQL_NULL_SENTINEL] + [""] * 6 + ["范围"] + [""] * 8 + [ATTACHED_SQL_NULL_SENTINEL, "备注第一行\\n备注第二行"]
        raw_tsv = "\t".join(raw_row) + "\n"
        attached_rows = parse_mysql_batch_tsv_with_provenance(raw_tsv)

        class FakeClient:
            def execute(self, sql: str, *, query_label: str | None = None):
                if query_label == "procedure_models":
                    return [["10", "检查单_示例", "GB", "合格", "1", "0", "2"]]
                if query_label == "procedure_contexts":
                    return [["10", "1", "外观", "无缺陷"]]
                if query_label == "attached_models":
                    return attached_rows
                return [["1", "合成工程师02", "demo_user", "1001", "D1"]]

        snapshot = ReadonlyQueryService(FakeClient()).load_snapshot()  # type: ignore[arg-type]
        attached = snapshot.attached_models[0]
        self.assertIsNone(attached.inspection_items[2])
        self.assertEqual(attached.inspection_items[1], "")
        self.assertEqual(attached.raw_inspection_items[2], ATTACHED_SQL_NULL_SENTINEL)
        self.assertIsNone(attached.attached_table_name)
        self.assertEqual(attached.notes, "备注第一行\n备注第二行")
        self.assertEqual(attached.raw_attached_table_name, ATTACHED_SQL_NULL_SENTINEL)
        self.assertEqual(attached.raw_notes, "备注第一行\n备注第二行")
        self.assertEqual(attached.metadata_null_provenance, (True, False))
        self.assertEqual(attached.raw_tsv, raw_tsv.encode("utf-8"))

    def test_readonly_query_service_emits_structured_events_for_each_select(self) -> None:
        class FakeClient:
            def execute(self, sql: str, *, query_label: str | None = None) -> list[list[str]]:
                if query_label == "procedure_models":
                    return [["10", "检查单_示例", "GB", "合格", "1", "0", "2"]]
                if query_label == "procedure_contexts":
                    return [["10", "1", "外观", "无缺陷"]]
                if query_label == "attached_models":
                    return [["20", "10", *( ["项目"] * 9), *( ["范围"] * 9)]]
                return [["1", "合成工程师02", "demo_user", "1001", "D1"]]

        events: list[dict[str, object]] = []
        snapshot = ReadonlyQueryService(FakeClient(), event_callback=events.append).load_snapshot()  # type: ignore[arg-type]
        self.assertEqual(len(snapshot.procedure_models), 1)
        starts = [event for event in events if event["name"] == "readonly_query_start"]
        successes = [event for event in events if event["name"] == "readonly_query_success"]
        self.assertEqual([event["query_label"] for event in starts], ["procedure_models", "procedure_contexts", "attached_models", "active_users"])
        self.assertEqual([event["query_label"] for event in successes], ["procedure_models", "procedure_contexts", "attached_models", "active_users"])
        self.assertEqual([event["row_count"] for event in successes], [1, 1, 1, 1])

    def test_readonly_query_service_error_event_keeps_safe_query_diagnostics(self) -> None:
        class FailingClient:
            def execute(self, sql: str, *, query_label: str | None = None) -> list[list[str]]:
                raise MySqlQueryError(
                    "MySQL 只读查询失败",
                    query_stage=query_label,
                    exit_code=7,
                    safe_detail="表不存在；credential redacted",
                )

        events: list[dict[str, object]] = []
        with self.assertRaises(MySqlQueryError):
            ReadonlyQueryService(FailingClient(), event_callback=events.append).load_snapshot()  # type: ignore[arg-type]
        error = next(event for event in events if event["name"] == "readonly_query_error")
        self.assertEqual(error["query_label"], "procedure_models")
        self.assertEqual(error["query_stage"], "procedure_models")
        self.assertEqual(error["exit_code"], "7")
        self.assertIn("表不存在", str(error["safe_detail"]))


class SyntheticGroupStrictModelTests(unittest.TestCase):
    TARGET = "附录 A DemoLogic定型优化PCM进货检验记录表"

    def model(self, model_id="258", name=None, latest="1", frozen="0", state="2"):
        return ProcedureModel(model_id, self.TARGET if name is None else name, "-1", "0", latest, frozen, state)

    def test_strict_selects_only_unique_active_literal_name(self):
        selected = self.model()
        events = []
        self.assertIs(select_unique_active_model((self.model("21", "合成类别02进货检查单"), selected), self.TARGET, strict_exact=True, diagnostics_callback=events.append), selected)
        self.assertEqual(events[0]["selected_model_id"], "258")
        self.assertIs(select_unique_active_model((selected, self.model("99", frozen="1")), " " + self.TARGET + " ", strict_exact=True), selected)

    def test_strict_rejects_missing_ambiguous_inactive_and_near_names(self):
        variants = ((), (self.model(), self.model("259")), (self.model(latest="0"),), (self.model(frozen="1"),), (self.model(state="1"),), (self.model(name=self.TARGET.replace("附录 A", "附录A")),), (self.model(name=self.TARGET + "补充"),), (self.model(name="前缀" + self.TARGET),), (self.model("21", "合成类别02进货检查单"),))
        for models in variants:
            with self.subTest(models=models):
                events = []
                with self.assertRaises(ModelMappingError):
                    select_unique_active_model(models, self.TARGET, strict_exact=True, diagnostics_callback=events.append)
                self.assertEqual(events[0]["status"], "BLOCKED")
                self.assertEqual(events[0]["selected_model_id"], "")

    def test_default_mode_retains_contains_matching(self):
        candidate = self.model(name="前缀" + self.TARGET + "后缀")
        self.assertIs(select_unique_active_model((candidate,), self.TARGET), candidate)


if __name__ == "__main__":
    unittest.main()

