from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.sql_generator import (
    SqlAttachedRow,
    SqlContextRow,
    SqlGenerationError,
    SqlOrder,
    SqlRecord,
    TARGET_TABLES,
    generate_order_artifacts,
    render_order_bat,
    render_order_sql,
    validate_generated_bat,
    validate_generated_sql,
)


def _sample_order() -> SqlOrder:
    return SqlOrder(
        order_number="DEMO'ORDER",
        records=(
            SqlRecord(
                item_code="M001\\A",
                item_name="阀体 'QA1' \\ 特殊",
                quantity="12.500",
                supplier="供应商\nA",
                procedure_name="检查单_示例",
                procedure_model_id=42,
                engineer_id=7,
                engineer_name="合成工程师02",
                engineer_dept_id="1001",
                sequence_code="001-003",
                trigger_basis="DEMO'ORDER",
                check_number=5,
                accept_standard=0,
                check_plan=-1,
                contexts=(
                    SqlContextRow("1", "外观", "无裂纹 'A'"),
                ),
                attached_tables=(
                    SqlAttachedRow(
                        inspection_items=("长度",) + ("",) * 8,
                        standard_ranges=("10\\20",) + ("",) * 8,
                    ),
                ),
            ),
        ),
    )


def _split_sql_values(value_text: str) -> tuple[str, ...]:
    """Split one INSERT value list without splitting quoted JSON/function commas."""

    values: list[str] = []
    current: list[str] = []
    in_string = False
    parenthesis_depth = 0
    index = 0
    while index < len(value_text):
        character = value_text[index]
        if in_string:
            current.append(character)
            if character == "\\" and index + 1 < len(value_text):
                index += 1
                current.append(value_text[index])
            elif character == "'":
                if index + 1 < len(value_text) and value_text[index + 1] == "'":
                    index += 1
                    current.append(value_text[index])
                else:
                    in_string = False
        else:
            if character == "'":
                in_string = True
                current.append(character)
            elif character == "(":
                parenthesis_depth += 1
                current.append(character)
            elif character == ")":
                parenthesis_depth -= 1
                current.append(character)
            elif character == "," and parenthesis_depth == 0:
                values.append("".join(current).strip())
                current = []
            else:
                current.append(character)
        index += 1
    values.append("".join(current).strip())
    return tuple(values)


def _attached_insert_parts(sql: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return exact columns/values from the generated attached-table INSERT."""

    import re

    line = next(
        line
        for line in sql.splitlines()
        if line.startswith("INSERT INTO qcauto_entry_attached_table_draft")
    )
    match = re.fullmatch(
        r"INSERT INTO qcauto_entry_attached_table_draft \((?P<columns>.*?)\) VALUES \((?P<values>.*)\);",
        line,
    )
    if match is None:
        raise AssertionError(f"无法解析附表 INSERT：{line}")
    columns = tuple(item.strip() for item in match.group("columns").split(","))
    return columns, _split_sql_values(match.group("values"))


def _draft_insert_parts(sql: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return exact columns/values from the generated main draft INSERT."""

    import re

    line = next(
        line
        for line in sql.splitlines()
        if line.startswith("INSERT INTO qcauto_procedure_draft ")
    )
    match = re.fullmatch(
        r"INSERT INTO qcauto_procedure_draft \((?P<columns>.*?)\) VALUES \((?P<values>.*)\);",
        line,
    )
    if match is None:
        raise AssertionError(f"无法解析主草稿 INSERT：{line}")
    columns = tuple(item.strip() for item in match.group("columns").split(","))
    return columns, _split_sql_values(match.group("values"))


class SqlGeneratorTests(unittest.TestCase):
    def test_attached_row_from_template_copies_headers_and_fills_na(self) -> None:
        row = SqlAttachedRow.from_template(
            ("输入电压", "", None, "频率", "", "", "", "", ""),
            ("24V", "", None, "50Hz", "", "", "", "", ""),
            inspection_number="DEMO-ORDER-001",
            notes="电源类附表备注",
        )
        self.assertEqual(
            row.inspection_items,
            ("输入电压", "NA", "NA", "频率", "NA", "NA", "NA", "NA", "NA"),
        )
        self.assertEqual(
            row.standard_ranges,
            ("24V", "NA", "NA", "50Hz", "NA", "NA", "NA", "NA", "NA"),
        )
        self.assertEqual(row.inspection_number, "DEMO-ORDER-001")
        self.assertEqual(row.notes, "电源类附表备注")
        content = json.loads(row.inspection_content)
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["inspectionContent1"], "")
        self.assertEqual(content[0]["inspectionContent2"], "NA")
        self.assertEqual(content[0]["inspectionContent4"], "")
        self.assertEqual(content[0]["inspectionContent5"], "NA")

    def test_attached_slot_literal_null_is_preserved_without_global_text_replacement(self) -> None:
        row = SqlAttachedRow.from_template(
            ("项目", "", None, "NULL", "NA", "", "", "", ""),
            ("范围", "错位范围", "", "NULL", "NA", "", "", "", ""),
            inspection_number="DEMO-ORDER-001",
            notes="NULL",
        )
        self.assertEqual(row.inspection_items[:5], ("项目", "NA", "NA", "NULL", "NA"))
        # A range in a different slot remains in that slot; no global replace
        # or pair-shifting is performed while G0 pairing rules are pending.
        self.assertEqual(row.standard_ranges[:5], ("范围", "错位范围", "NA", "NULL", "NA"))
        content = json.loads(row.inspection_content)
        self.assertEqual(content[0]["inspectionContent1"], "")
        self.assertEqual(content[0]["inspectionContent2"], "NA")
        self.assertEqual(content[0]["inspectionContent4"], "")
        self.assertEqual(row.notes, "NULL")

        source = _sample_order().records[0]
        rendered = render_order_sql(
            SqlOrder(
                "DEMO-ORDER-001",
                (
                    replace(
                        source,
                        check_number=0,
                        inspection_conclusion="NULL",
                        attached_tables=(row,),
                    ),
                ),
            )
        )
        self.assertIn("'NULL'", rendered)  # notes/conclusion remain literal text
        _, attached_values = _attached_insert_parts(rendered)
        self.assertEqual(attached_values[7], "NULL")

    def test_attached_slot_sql_null_and_empty_cells_still_render_na(self) -> None:
        """None/empty source cells retain the established NA display rule."""

        row = SqlAttachedRow.from_template(
            ("项目", "", None, "NA", "", "", "", "", ""),
            ("范围", "", None, "NA", "", "", "", "", ""),
        )
        self.assertEqual(row.inspection_items[:4], ("项目", "NA", "NA", "NA"))
        self.assertEqual(row.standard_ranges[:4], ("范围", "NA", "NA", "NA"))
        content = json.loads(row.inspection_content)
        self.assertEqual(content[0]["inspectionContent1"], "")
        self.assertEqual(content[0]["inspectionContent2"], "NA")
        self.assertEqual(content[0]["inspectionContent3"], "NA")
        self.assertEqual(content[0]["inspectionContent4"], "NA")

    def test_attached_row_requires_frontend_array_contract(self) -> None:
        with self.assertRaises(SqlGenerationError):
            SqlAttachedRow(inspection_content='{"sequenceCode":""}')

    def test_attached_insert_preserves_column_order_inherited_values_na_and_json_roundtrip(self) -> None:
        source = _sample_order().records[0]
        attached = SqlAttachedRow.from_template(
            ("输入电压", "", "", "频率", "", "", "", "", ""),
            ("24V", "", "", "50Hz", "", "", "", "", ""),
            inspection_number="DEMO-ORDER-001",
            notes="电源类附表备注",
        )
        record = replace(
            source,
            item_code="MAT-001",
            trigger_basis="DEMO-ORDER-001-1",
            check_number=13,
            attached_tables=(attached,),
        )
        sql = render_order_sql(SqlOrder("DEMO-ORDER-001", (record,)))
        columns, values = _attached_insert_parts(sql)

        self.assertEqual(
            columns,
            (
                "description", "modifier", "dept_belong_id", "update_datetime", "create_datetime",
                "inspection_number", "item_code", "check_number", "testing_tools", "inspection_conclusion",
                "qc_engineer", "completion_time", "creator_id", "procedure_draft_id",
                "inspection_item1", "inspection_item2", "inspection_item3", "inspection_item4",
                "inspection_item5", "inspection_item6", "inspection_item7", "inspection_item8", "inspection_item9",
                "standard_range1", "standard_range2", "standard_range3", "standard_range4", "standard_range5",
                "standard_range6", "standard_range7", "standard_range8", "standard_range9", "notes",
                "inspection_content",
            ),
        )
        self.assertEqual(len(values), len(columns))
        self.assertEqual(record.check_number, 13)
        self.assertEqual(values[5:8], ("'DEMO-ORDER-001-1'", "'MAT-001'", "NULL"))
        self.assertEqual(
            values[14:23],
            ("'输入电压'", "'NA'", "'NA'", "'频率'", "'NA'", "'NA'", "'NA'", "'NA'", "'NA'"),
        )
        self.assertEqual(
            values[23:32],
            ("'24V'", "'NA'", "'NA'", "'50Hz'", "'NA'", "'NA'", "'NA'", "'NA'", "'NA'"),
        )
        self.assertEqual(values[32], "'电源类附表备注'")

        # JSONField 的数据库值必须是外层 JSON 字符串；读取后再 json.loads
        # 一次得到 auto-factory 使用的一元素数组。SQL 形状与既有 BAS
        # 的 JSON_QUOTE(CAST(... AS CHAR)) 保持一致。
        outer_expression = values[33]
        self.assertTrue(outer_expression.startswith("JSON_QUOTE(CAST('"))
        self.assertTrue(outer_expression.endswith("' AS CHAR))"))
        # v1.2 约定附表 JSON 的 sequenceCode 必须跟随主记录的同一
        # canonical sequence；SqlAttachedRow 夹具的默认空 sequence 只是
        # 未渲染前的占位，不能再作为 SQL 输出断言。
        encoded_json = outer_expression[len("JSON_QUOTE(CAST('") : -len("' AS CHAR))")]
        rendered_content = json.loads(encoded_json)
        self.assertEqual(rendered_content[0]["sequenceCode"], record.sequence_code)
        database_driver_value = json.loads(json.dumps(attached.inspection_content, ensure_ascii=False))
        self.assertIsInstance(database_driver_value, str)
        self.assertEqual(json.loads(database_driver_value), json.loads(attached.inspection_content))

    def test_attached_insert_inherits_final_trigger_basis_even_if_row_identity_is_stale(self) -> None:
        source = _sample_order().records[0]
        row = SqlAttachedRow(
            inspection_number="undefined",
            inspection_items=("项目",) + ("NA",) * 8,
            standard_ranges=("范围",) + ("NA",) * 8,
        )
        record = replace(source, trigger_basis="S-DEMO-ORDER-001-1/3", attached_tables=(row,))
        sql = render_order_sql(SqlOrder("DEMO-ORDER-001", (record,)))
        _, values = _attached_insert_parts(sql)
        self.assertEqual(values[5], "'S-DEMO-ORDER-001-1/3'")

    def test_diagnostic_keeps_alias_trace_fields(self) -> None:
        source = _sample_order()
        record = replace(
            source.records[0],
            alias_applied=True,
            source_procedure_name="Windchill原始名",
            target_procedure_name="数据库当前模板名",
            alias_matched_by="item_code",
            alias_rule_id="confirmed_test",
            alias_version="v1",
            alias_effective_date="2026-08-31",
        )
        order = replace(source, records=(record,))
        order = replace(
            order,
            warnings=("送检单 DEMO'ORDER、物料 M001\\A：M码起止值固定段（M+前7位）不一致；已保留起止端点继续生成",),
        )
        with tempfile.TemporaryDirectory() as temp:
            artifacts = generate_order_artifacts(order, Path(temp))
            diagnostic = json.loads(artifacts.diagnostic_path.read_text(encoding="utf-8"))
        item = diagnostic["records"][0]
        self.assertTrue(item["alias_applied"])
        self.assertEqual(item["source_procedure_name"], "Windchill原始名")
        self.assertEqual(item["target_procedure_name"], "数据库当前模板名")
        self.assertEqual(item["alias_matched_by"], "item_code")
        self.assertEqual(item["alias_version"], "v1")
        self.assertEqual(item["alias_effective_date"], "2026-08-31")
        self.assertEqual(len(diagnostic["warnings"]), 1)
        self.assertIn("固定段（M+前7位）不一致", diagnostic["warnings"][0])

    def test_render_sql_is_scoped_transaction_and_omits_cancelled_field(self) -> None:
        sql = render_order_sql(_sample_order())
        self.assertEqual(sql.upper().count("START TRANSACTION;"), 1)
        self.assertEqual(sql.upper().count("COMMIT;"), 1)
        self.assertIn("'COMMITTED' AS log_status", sql)
        self.assertNotIn("measurement_unit", sql.lower())
        self.assertEqual(
            set(__import__("re").findall(r"INSERT INTO ([A-Za-z0-9_]+)", sql)),
            set(TARGET_TABLES),
        )
        self.assertIn("qcauto_procedure_draft (", sql)
        self.assertIn("qcauto_procedure_draft_check_context (", sql)
        self.assertIn("qcauto_entry_attached_table_draft (", sql)

    def test_tk_blank_supplier_is_na_in_sql_and_diagnostic_with_raw_provenance(self) -> None:
        source = _sample_order().records[0]
        record = replace(
            source,
            supplier="NA",
            raw_supplier="",
            supplier_defaulted=True,
            sequence_code="M99999999001;M99999999002",
        )
        order = SqlOrder("TK99999999001", (record,))
        sql = render_order_sql(order)
        self.assertIn("'NA'", sql)
        self.assertNotIn("M99999999001;M99999999002", sql)
        self.assertIn("M99999999001/M99999999002", sql)
        with tempfile.TemporaryDirectory() as temp:
            diagnostic = json.loads(
                generate_order_artifacts(order, Path(temp)).diagnostic_path.read_text(encoding="utf-8")
            )
        item = diagnostic["records"][0]
        self.assertEqual(item["supplier"], "NA")
        self.assertEqual(item["raw_supplier"], "")
        self.assertTrue(item["supplier_defaulted"])
        self.assertEqual(item["sequence_code"], "M99999999001/M99999999002")

    def test_non_tk_supplier_and_non_m_sequence_are_unchanged(self) -> None:
        source = _sample_order().records[0]
        record = replace(source, supplier="供应商", raw_supplier="供应商", sequence_code="S001-S003")
        sql = render_order_sql(SqlOrder("DN99999999001", (record,)))
        self.assertIn("'供应商'", sql)
        self.assertIn("'S001-S003'", sql)

    def test_sql_quotes_and_backslashes_are_escaped(self) -> None:
        sql = render_order_sql(_sample_order())
        self.assertIn("DEMO''ORDER", sql)
        self.assertIn("M001\\\\A", sql)
        self.assertIn("阀体 ''QA1'' \\\\ 特殊", sql)

    def test_inspection_conclusion_has_no_default_and_explicit_value_is_preserved(self) -> None:
        sql = render_order_sql(_sample_order())
        # 月度输入/当前结果模板没有结论列；未明确提供时必须保持空值，
        # 不能再由 Python 擅自写入“合格”。附表同样遵循该规则。
        self.assertNotIn("合格", sql)
        self.assertIn("testing_tools, inspection_conclusion", sql)

        source = _sample_order().records[0]
        record = SqlRecord(
            **{
                **source.__dict__,
                "inspection_conclusion": "待定",
                "attached_tables": (SqlAttachedRow(inspection_conclusion="待定"),),
            }
        )
        explicit_sql = render_order_sql(SqlOrder("DEMO-ORDER-001", (record,)))
        self.assertGreaterEqual(explicit_sql.count("'待定'"), 2)

    def test_sql_keywords_inside_business_text_are_not_misclassified(self) -> None:
        source = _sample_order().records[0]
        record = SqlRecord(**{**source.__dict__, "item_name": "DROP ROLLBACK UPDATE START TRANSACTION; COMMIT;"})
        validate_generated_sql(render_order_sql(SqlOrder("DEMO-ORDER-001", (record,))))

    def test_invalid_integer_is_rejected_before_writing_sql(self) -> None:
        order = _sample_order()
        bad = SqlRecord(
            **{**order.records[0].__dict__, "engineer_id": "7;DROP TABLE users"}
        )
        with self.assertRaises(SqlGenerationError):
            render_order_sql(SqlOrder(order.order_number, (bad,)))

    def test_required_trigger_check_number_and_context_are_enforced(self) -> None:
        source = _sample_order().records[0]
        for overrides in (
            {"trigger_basis": ""},
            {"contexts": ()},
        ):
            bad = SqlRecord(**{**source.__dict__, **overrides})
            with self.assertRaises(SqlGenerationError):
                render_order_sql(SqlOrder("DEMO-ORDER-001", (bad,)))
        nullable = SqlRecord(**{**source.__dict__, "check_number": None})
        self.assertIn(", NULL, -1, 0,", render_order_sql(SqlOrder("DEMO-ORDER-001", (nullable,))))

    def test_check_number_is_preserved_in_record_but_null_at_sql_boundaries(self) -> None:
        record = replace(_sample_order().records[0], supplier="供应商A")
        sql = render_order_sql(SqlOrder("DEMO-ORDER-001", (record,)))
        draft_columns, draft_values = _draft_insert_parts(sql)
        attached_columns, attached_values = _attached_insert_parts(sql)

        self.assertEqual(record.check_number, 5)
        self.assertEqual(draft_values[draft_columns.index("check_number")], "NULL")
        self.assertEqual(attached_values[attached_columns.index("check_number")], "NULL")

    def test_control_character_and_invalid_date_are_rejected(self) -> None:
        source = _sample_order().records[0]
        for overrides in (
            {"item_name": "bad\x01value"},
            {"completion_time": "2026-02-30"},
        ):
            bad = SqlRecord(**{**source.__dict__, **overrides})
            with self.assertRaises(SqlGenerationError):
                render_order_sql(SqlOrder("DEMO-ORDER-001", (bad,)))

    def test_sql_static_gate_rejects_out_of_scope_and_cancelled_field(self) -> None:
        with self.assertRaises(SqlGenerationError):
            validate_generated_sql("START TRANSACTION; INSERT INTO users(id) VALUES (1); COMMIT;")
        with self.assertRaises(SqlGenerationError):
            validate_generated_sql(
                "START TRANSACTION; INSERT INTO qcauto_procedure_draft(id, measurement_unit) VALUES (1, 'x'); COMMIT;"
            )
        with self.assertRaises(SqlGenerationError):
            validate_generated_sql(
                "START TRANSACTION; INSERT INTO qcauto_procedure_draft(id) VALUES (1); CALL dangerous_proc(); COMMIT;"
            )
        with self.assertRaises(SqlGenerationError):
            validate_generated_sql(
                "START TRANSACTION; INSERT INTO qcauto_procedure_draft(id) VALUES (1); SET GLOBAL sql_mode=''; COMMIT;"
            )

    def test_bat_has_no_connection_values_and_only_wraps_existing_runner(self) -> None:
        bat = render_order_bat("DEMO-ORDER-001")
        validate_generated_bat(bat)
        self.assertIn("run_generated_sql.bat", bat.lower())
        self.assertIn("%SQL_FILE%", bat)
        self.assertNotIn("MYSQL_PASSWORD", bat.upper())
        self.assertNotIn("--PASSWORD", bat.upper())
        with self.assertRaises(SqlGenerationError):
            validate_generated_bat(bat + 'call "evil.bat" "%SQL_FILE%"\n')
        with self.assertRaises(SqlGenerationError):
            render_order_bat(None)  # type: ignore[arg-type]
        with self.assertRaises(SqlGenerationError):
            render_order_bat(123)  # type: ignore[arg-type]

    def test_generate_artifacts_writes_three_files_and_not_execute(self) -> None:
        order = _sample_order()
        with tempfile.TemporaryDirectory() as temp:
            artifacts = generate_order_artifacts(order, Path(temp))
            self.assertTrue(artifacts.sql_path.is_file())
            self.assertTrue(artifacts.bat_path.is_file())
            self.assertTrue(artifacts.diagnostic_path.is_file())
            self.assertTrue(artifacts.bat_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            diagnostic = json.loads(artifacts.diagnostic_path.read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["status"], "PREPARED_NOT_EXECUTED")
            self.assertTrue(diagnostic["execution_prohibited_in_this_stage"])
            self.assertEqual(diagnostic["bat_encoding"], "UTF-8 with BOM")
            self.assertEqual(diagnostic["target_tables"], list(TARGET_TABLES))
            self.assertEqual(diagnostic["record_count"], 1)
            self.assertEqual(diagnostic["context_row_count"], 1)
            self.assertEqual(diagnostic["attached_row_count"], 1)
            self.assertNotIn("measurement_unit", artifacts.sql_path.read_text(encoding="utf-8").lower())

    def test_empty_order_or_unsafe_order_is_rejected(self) -> None:
        with self.assertRaises(SqlGenerationError):
            SqlOrder("DEMO-ORDER-001", ())
        with self.assertRaises(SqlGenerationError):
            SqlOrder("../bad", (_sample_order().records[0],))
        for metacharacter in "%&^!();":
            with self.assertRaises(SqlGenerationError):
                SqlOrder(f"2608{metacharacter}032", (_sample_order().records[0],))
        for reserved in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT1"):
            with self.assertRaises(SqlGenerationError):
                SqlOrder(reserved, (_sample_order().records[0],))


if __name__ == "__main__":
    unittest.main()

