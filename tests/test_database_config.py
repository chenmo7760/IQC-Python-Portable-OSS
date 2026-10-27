"""Public-release configuration boundaries; no deployed BAT files required."""
import tempfile
import unittest
from pathlib import Path
from src.config import load_import_authorization, load_entry_api_base_url
from src.inspection_adjustment import DEFAULT_A_PATH, DEFAULT_B_PATH

class PublicConfigurationTests(unittest.TestCase):
    def test_absent_configuration_disables_external_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing"
            self.assertFalse(load_import_authorization(missing))
            self.assertEqual(load_entry_api_base_url(missing), "")
        self.assertEqual(DEFAULT_A_PATH, "")
        self.assertEqual(DEFAULT_B_PATH, "")

    def test_public_tree_has_no_deployment_runtime(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "运行时").exists())
        self.assertFalse((root / "config/mysql_connection.bat").exists())
