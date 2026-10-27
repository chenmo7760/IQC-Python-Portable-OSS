from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.engineer_service import load_engineers


class EngineerServiceTests(unittest.TestCase):
    def test_only_enabled_preconfigured_engineers_are_returned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "engineers.json"
            path.write_text(
                json.dumps(
                    {
                        "engineers": [
                            {"user_id": "1", "name": "A", "dept_id": "D1", "display_name": "A", "enabled": True},
                            {"user_id": "2", "name": "B", "dept_id": "D2", "display_name": "B", "enabled": False},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            engineers = load_engineers(path)
            self.assertEqual([item.user_id for item in engineers], ["1"])


if __name__ == "__main__":
    unittest.main()

