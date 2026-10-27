from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.models import normalize_procedure_name
from src.procedure_alias import (
    ProcedureAlias,
    ProcedureAliasError,
    ProcedureAliasResolver,
    load_procedure_aliases,
)


def _alias(**overrides: object) -> ProcedureAlias:
    values: dict[str, object] = {
        "enabled": True,
        "item_code": "",
        "source_name": "",
        "normalized_name": "",
        "target_procedure_name": "目标检查单",
        "note": "测试",
        "version": "v1",
        "effective_date": "2026-08-31",
        "rule_id": "test",
    }
    values.update(overrides)
    return ProcedureAlias(**values)  # type: ignore[arg-type]


class ProcedureAliasTests(unittest.TestCase):
    def test_public_alias_configuration_is_empty(self) -> None:
        path = Path(__file__).resolve().parents[1] / "config/procedure_aliases.json"
        resolver = load_procedure_aliases(path)
        self.assertEqual(resolver.aliases, ())
        self.assertIsNone(resolver.resolve(item_code="DEMO-MAT-001", source_name="合成检查单"))

    def test_loads_utf8_json_and_resolves_item_code_before_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "aliases.json"
            path.write_text(
                json.dumps(
                    {
                        "aliases": [
                            {
                                "enabled": True,
                                "item_code": "MAT-1",
                                "source_name": "源名",
                                "normalized_name": "规范名",
                                "target_procedure_name": "编码目标检查单",
                                "note": "编码优先",
                                "version": "v1",
                                "effective_date": "2026-08-31",
                            },
                            {
                                "enabled": True,
                                "item_code": "",
                                "source_name": "源名",
                                "normalized_name": "规范名",
                                "target_procedure_name": "名称目标检查单",
                                "note": "名称",
                                "version": "v1",
                                "effective_date": "2026-08-31",
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            resolver = load_procedure_aliases(path)
        result = resolver.resolve(item_code="MAT-1", source_name="源名", normalized_name="规范名")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.matched_by, "item_code")
        self.assertEqual(result.target_procedure_name, "编码目标检查单")
        by_name = resolver.resolve(item_code="MAT-2", source_name="源名", normalized_name="规范名")
        self.assertIsNotNone(by_name)
        assert by_name is not None
        self.assertEqual(by_name.matched_by, "source_name")

    def test_loads_gb18030_json_for_legacy_editor_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "aliases.json"
            payload = {
                "aliases": [
                    {
                        "enabled": True,
                        "item_code": "MAT-GBK",
                        "target_procedure_name": "中文目标检查单",
                    }
                ]
            }
            path.write_bytes(json.dumps(payload, ensure_ascii=False).encode("gb18030"))
            resolver = load_procedure_aliases(path)
        result = resolver.resolve(item_code="MAT-GBK", source_name="任意")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.target_procedure_name, "中文目标检查单")

    def test_normalized_name_key_is_exact_and_normalized(self) -> None:
        resolver = ProcedureAliasResolver(
            [_alias(normalized_name="《文档_名称检查单》", target_procedure_name="规范目标检查单")]
        )
        result = resolver.resolve(
            item_code="MAT-2",
            source_name="前缀不会模糊命中",
            normalized_name=normalize_procedure_name("《文档_名称检查单》"),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.matched_by, "normalized_name")
        self.assertEqual(result.target_procedure_name, "规范目标检查单")
        self.assertIsNone(
            resolver.resolve(item_code="MAT-3", source_name="名称检查单扩展", normalized_name="名称检查单扩展")
        )

    def test_conflict_and_disabled_aliases_fail_closed(self) -> None:
        with self.assertRaises(ProcedureAliasError):
            ProcedureAliasResolver(
                [
                    _alias(item_code="MAT-1", target_procedure_name="目标1"),
                    _alias(item_code="MAT-1", target_procedure_name="目标2"),
                ]
            )
        resolver = ProcedureAliasResolver([_alias(item_code="MAT-1", enabled=False)])
        with self.assertRaises(ProcedureAliasError):
            resolver.resolve(item_code="MAT-1", source_name="任意名称", normalized_name="任意名称")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            path.write_text(json.dumps({"aliases": [{"enabled": True, "item_code": "MAT-1"}]}), encoding="utf-8")
            with self.assertRaises(ProcedureAliasError):
                load_procedure_aliases(path)

    def test_different_item_specific_targets_on_one_name_are_rejected(self) -> None:
        with self.assertRaises(ProcedureAliasError):
            ProcedureAliasResolver(
                [
                    _alias(item_code="MAT-1", source_name="同一名称", target_procedure_name="目标1"),
                    _alias(item_code="MAT-2", source_name="同一名称", target_procedure_name="目标2"),
                ]
            )

    def test_empty_or_invalid_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            path.write_text(json.dumps({"aliases": [{"enabled": True, "target_procedure_name": "目标"}]}), encoding="utf-8")
            with self.assertRaises(ProcedureAliasError):
                load_procedure_aliases(path)


class SyntheticGroupAliasTests(unittest.TestCase):
    CODES = frozenset(("DEMO-MAT-017", "DEMO-MAT-002", "DEMO-MAT-013", "DEMO-MAT-015", "DEMO-MAT-022", "DEMO-MAT-019", "DEMO-MAT-024", "DEMO-MAT-035"))
    TARGET = "附录 A DemoLogic定型优化PCM进货检验记录表"

    def test_synthetic_group_is_complete_and_strict(self):
        from tests.synthetic_aliases import synthetic_alias_payload
        rows = synthetic_alias_payload()["aliases"]
        self.assertEqual({row["item_code"] for row in rows}, self.CODES)
        self.assertTrue(all(row["strict_target_match"] for row in rows))

    def test_strict_defaults_false_and_non_boolean_values_rejected(self):
        self.assertIs(_alias(item_code="OTHER").strict_target_match, False)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "aliases.json"
            path.write_text(json.dumps({"aliases": [{"item_code": "OTHER", "target_procedure_name": "目标"}]}), encoding="utf-8")
            self.assertIs(load_procedure_aliases(path).aliases[0].strict_target_match, False)
            for value in ("true", "false", 1, 0, None):
                with self.subTest(value=value):
                    path.write_text(json.dumps({"aliases": [{"item_code": "OTHER", "target_procedure_name": "目标", "strict_target_match": value}]}), encoding="utf-8")
                    with self.assertRaises(ProcedureAliasError):
                        load_procedure_aliases(path)

    def test_partial_duplicate_disabled_and_inconsistent_migration_rejected(self):
        import copy
        root = Path(__file__).resolve().parents[1]
        from tests.synthetic_aliases import synthetic_alias_payload
        payload = synthetic_alias_payload()
        rows = [row for row in payload["aliases"] if row.get("item_code") in self.CODES]
        variants = [rows[:-1], rows + [rows[0]]]
        for field, value in (("strict_target_match", False), ("enabled", False), ("rule_id", "changed"), ("target_procedure_name", "合成类别02进货检查单"), ("source_name", "近似名称"), ("normalized_name", "近似名称")):
            variant = copy.deepcopy(rows)
            variant[0][field] = value
            variants.append(variant)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "aliases.json"
            for index, variant in enumerate(variants):
                with self.subTest(index=index):
                    path.write_text(json.dumps({"aliases": variant}, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaises(ProcedureAliasError):
                        load_procedure_aliases(path)
        with self.assertRaises(ProcedureAliasError):
            ProcedureAliasResolver([_alias(item_code="A", source_name="同名", strict_target_match=True), _alias(item_code="B", source_name="同名", target_procedure_name="另一个目标")])


if __name__ == "__main__":
    unittest.main()
