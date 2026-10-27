from __future__ import annotations

import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

from src.config import (
    AppPaths,
    application_root,
    load_entry_api_base_url,
    load_import_authorization,
    load_paper_standard_catalog_path,
    resolve_readonly_mysql_config_path,
)


class ConfigTests(unittest.TestCase):
    def test_output_dir_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config" / "launcher.json"
            config.parent.mkdir()
            self.assertEqual(AppPaths.from_project_root(root).output_dir, (root / "输出").resolve())
            config.write_text(json.dumps({"output_dir": "custom"}), encoding="utf-8")
            self.assertEqual(AppPaths.from_project_root(root).output_dir, (root / "custom").resolve())
            config.write_text(json.dumps({"output_dir": "D:/example-user/downloads/x"}), encoding="utf-8")
            self.assertEqual(AppPaths.from_project_root(root).output_dir, (root / "输出").resolve())
            with patch.dict("os.environ", {"IQC_OUTPUT_DIR": "C:/Explicit"}):
                self.assertEqual(AppPaths.from_project_root(root).output_dir, Path("C:/Explicit").resolve())

    def test_source_paths_are_relative_to_project_root(self) -> None:
        root = Path("C:/portable/IQC-Python-Portable")
        paths = AppPaths.from_project_root(root)
        self.assertEqual(paths.output_dir, root / "输出")
        self.assertEqual(paths.log_dir, root / "日志")
        self.assertEqual(paths.procedure_aliases, root / "config" / "procedure_aliases.json")

    def test_frozen_root_uses_executable_directory(self) -> None:
        with patch("src.config.sys.frozen", True, create=True), patch(
            "src.config.sys.executable", "C:/portable/IQC-Python-Portable/IQC-Python-Portable.exe"
        ):
            self.assertEqual(application_root(), Path("C:/portable/IQC-Python-Portable"))

    def test_import_authorization_reads_only_explicit_flag_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "mysql_connection.bat"
            config.write_text(
                'set "MYSQL_PASSWORD=not-for-logs"\n'
                'set "IQC_IMPORT_AUTHORIZED=1"\n',
                encoding="utf-8",
            )
            self.assertTrue(load_import_authorization(config))
            config.write_text('set "IQC_IMPORT_AUTHORIZED=0"\n', encoding="utf-8")
            self.assertFalse(load_import_authorization(config))
            config.write_text('set "IQC_IMPORT_AUTHORIZED=maybe"\n', encoding="utf-8")
            self.assertFalse(load_import_authorization(config))
            self.assertFalse(load_import_authorization(Path(temp) / "missing.bat"))

    def test_entry_api_base_url_reads_utf8_bom_and_cp936_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "launcher.json"
            config.write_text('{"entry_api_base_url": " http://api.example:8080/ "}', encoding="utf-8")
            self.assertEqual(load_entry_api_base_url(config), "http://api.example:8080/")
            config.write_bytes('{"entry_api_base_url": "http://测试主机:8080"}'.encode("cp936"))
            self.assertEqual(load_entry_api_base_url(config), "http://测试主机:8080")
            config.write_bytes(b"\xef\xbb\xbf{\"entry_api_base_url\": \"http://bom.example\"}")
            self.assertEqual(load_entry_api_base_url(config), "http://bom.example")

    def test_entry_api_base_url_fails_closed_for_invalid_or_non_string_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "launcher.json"
            config.write_text("not-json", encoding="utf-8")
            self.assertEqual(load_entry_api_base_url(config), "")
            config.write_text('{"entry_api_base_url": 123}', encoding="utf-8")
            self.assertEqual(load_entry_api_base_url(config), "")
            self.assertEqual(load_entry_api_base_url(Path(temp) / "missing.json"), "")

    def test_paper_standard_catalog_path_reads_utf8_bom_and_cp936_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "launcher.json"
            config.write_text(
                '{"paper_standard_catalog_path": " D:\\\\paper\\\\catalog.xlsx "}',
                encoding="utf-8",
            )
            self.assertEqual(load_paper_standard_catalog_path(config), r"D:\paper\catalog.xlsx")
            config.write_bytes(
                '{"paper_standard_catalog_path": "D:\\\\资料\\\\进货检查线下规范清单.xlsx"}'.encode("cp936")
            )
            self.assertEqual(load_paper_standard_catalog_path(config), r"D:\资料\进货检查线下规范清单.xlsx")
            config.write_bytes(
                b"\xef\xbb\xbf{\"paper_standard_catalog_path\": \"D:\\\\paper\\\\bom.xlsx\"}"
            )
            self.assertEqual(load_paper_standard_catalog_path(config), r"D:\paper\bom.xlsx")

    def test_paper_standard_catalog_path_fails_closed_for_invalid_or_non_string_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "launcher.json"
            config.write_text("not-json", encoding="utf-8")
            self.assertEqual(load_paper_standard_catalog_path(config), "")
            config.write_text('{"paper_standard_catalog_path": ["D:/paper.xlsx"]}', encoding="utf-8")
            self.assertEqual(load_paper_standard_catalog_path(config), "")
            config.write_text('[]', encoding="utf-8")
            self.assertEqual(load_paper_standard_catalog_path(config), "")
            self.assertEqual(load_paper_standard_catalog_path(Path(temp) / "missing.json"), "")

    def test_readonly_mysql_config_uses_r44_local_file_even_with_existing_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            default = root / "运行时" / "db" / "mysql_connection.bat"
            external = root / "protected" / "mysql_connection.bat"
            external.parent.mkdir(parents=True)
            external.touch()
            self.assertEqual(
                resolve_readonly_mysql_config_path(
                    root, {"IQC_MYSQL_CONFIG_FILE": str(external)}
                ),
                default,
            )
            self.assertEqual(
                resolve_readonly_mysql_config_path(
                    root, {"IQC_MYSQL_CONFIG_FILE": "relative/mysql_connection.bat"}
                ),
                default,
            )
            self.assertEqual(
                resolve_readonly_mysql_config_path(
                    root, {"IQC_MYSQL_CONFIG_FILE": str(root / "missing.bat")}
                ),
                default,
            )


if __name__ == "__main__":
    unittest.main()
