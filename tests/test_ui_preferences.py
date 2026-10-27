from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.ui_preferences import UiPreferenceStore


class UiPreferenceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ui_preferences.json"
        self.store = UiPreferenceStore(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_save_and_load_all_user_preferences_round_trip(self) -> None:
        self.assertEqual(self.store.save_iqc_source_path("  C:/iqc  "), (True, ""))
        self.assertEqual(self.store.save_pqc_source_path("D:/pqc"), (True, ""))
        self.assertEqual(self.store.save_engineer_name("  合成工程师03  "), (True, ""))
        self.assertEqual(self.store.save_paper_standard_catalog_path("  E:/paper  "), (True, ""))

        loaded = self.store.load()
        self.assertEqual(loaded.last_iqc_source_path, "C:/iqc")
        self.assertEqual(loaded.last_pqc_source_path, "D:/pqc")
        self.assertEqual(loaded.last_engineer_name, "合成工程师03")
        self.assertEqual(loaded.last_paper_standard_catalog_path, "E:/paper")

    def test_unknown_json_fields_are_preserved_across_saves(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "last_active_module": "PQC",
                    "last_iqc_source_path": "C:/old-iqc",
                    "last_pqc_source_path": "D:/old-pqc",
                    "last_engineer_name": "合成工程师03",
                    "last_paper_standard_catalog_path": "E:/paper",
                    "custom_note": "keep-me",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertEqual(self.store.save_iqc_source_path("C:/new-iqc"), (True, ""))
        self.assertEqual(self.store.save_pqc_source_path("D:/new-pqc"), (True, ""))

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["custom_note"], "keep-me")
        self.assertEqual(payload["last_iqc_source_path"], "C:/new-iqc")
        self.assertEqual(payload["last_pqc_source_path"], "D:/new-pqc")
        self.assertEqual(payload["last_engineer_name"], "合成工程师03")
        self.assertEqual(payload["last_paper_standard_catalog_path"], "E:/paper")

    def test_iqc_and_pqc_paths_do_not_overwrite_each_other(self) -> None:
        self.assertEqual(self.store.save_iqc_source_path("C:/iqc"), (True, ""))
        self.assertEqual(self.store.save_pqc_source_path("D:/pqc"), (True, ""))

        self.assertEqual(self.store.load().last_iqc_source_path, "C:/iqc")
        self.assertEqual(self.store.load().last_pqc_source_path, "D:/pqc")


if __name__ == "__main__":
    unittest.main()
