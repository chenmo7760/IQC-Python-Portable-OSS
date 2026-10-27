from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from src.models import WindchillName
from src.windchill_adapter import (
    MockWindchillAdapter,
    XlrdInformationXlsBackend,
    WindchillAdapterError,
    WindchillEmptyResultError,
    WindchillTechnicalSpecificationExporterAdapter,
    assert_success,
)


class _FakeXlsBackend:
    def __init__(self, responses: list[WindchillName]) -> None:
        self.responses = responses
        self.events: list[str] = []
        self.codes: list[str] = []

    def write_codes(self, path: Path, item_codes: list[str]) -> None:
        self.events.append("write_closed")
        self.codes = list(item_codes)

    def read_results(self, path: Path) -> list[WindchillName]:
        self.events.append("read_closed")
        return self.responses


def _write_v11_information_xls(path: Path) -> None:
    """写一个合法的 Windchill v1.1 A:E BIFF8 夹具，不触碰运行时旧文件。"""

    import xlwt

    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("Sheet1")
    header = XlrdInformationXlsBackend().header
    for column, value in enumerate(header):
        sheet.write(0, column, value)
    # 预置完整 A:E 行，随后由 write_codes 清空并回写查询编码。
    rows = (
        ("OLD-CODE", "旧检查单", "OLD-LEGACY", " QA1 ", " SUCCESS "),
        ("QA1-LOW", "小写 QA1", "LEGACY-LOW", "qa1", "success"),
        ("QA1-SPACE", "空格 QA1", "LEGACY-SPACE", " Qa1 ", " SUCCESS "),
        ("QA1-FALLBACK", "空值回退", "LEGACY-FALLBACK", "", " success "),
        ("QA2", "非 QA1", "LEGACY-QA2", " QA2 ", " SUCCESS "),
    )
    for row, values in enumerate(rows, start=1):
        for column, value in enumerate(values):
            sheet.write(row, column, value)
    workbook.save(str(path))


class WindchillMockTests(unittest.TestCase):
    def test_mock_adapter_returns_success_without_external_call(self) -> None:
        adapter = MockWindchillAdapter({"M001": "检查单_示例"})
        results = adapter.query_names(["M001", "M001"])
        self.assertEqual(len(results), 1)
        assert_success(results, ["M001"])

    def test_empty_windchill_result_has_dedicated_diagnostic(self) -> None:
        with self.assertRaisesRegex(WindchillEmptyResultError, "Windchill查询为空"):
            assert_success([], ["M001"])

    def test_xls_backend_replaces_temp_copy_without_starting_exe(self) -> None:
        """仅验证合法 v1.1 A:E BIFF 临时写回/释放；不构造或调用 Windchill EXE。"""

        if not XlrdInformationXlsBackend.dependencies_available():
            self.skipTest("information.xls BIFF backend dependency unavailable")
        try:
            import xlwt  # noqa: F401
        except ImportError:
            self.skipTest("xlwt BIFF8 test-fixture dependency unavailable")
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "information.xls"
            _write_v11_information_xls(target)
            backend = XlrdInformationXlsBackend()
            backend.write_codes(target, ["M001", "M001", "M002"])
            results = backend.read_results(target)
        self.assertEqual([item.item_code for item in results[:2]], ["M001", "M002"])

    def test_xls_backend_reads_v11_a_to_e_provenance_and_qa1(self) -> None:
        """A:E 读取保留 D/E 原文，并按 D 的规范化值判断 QA1。"""

        if not XlrdInformationXlsBackend.dependencies_available():
            self.skipTest("information.xls BIFF backend dependency unavailable")
        try:
            import xlwt  # noqa: F401
        except ImportError:
            self.skipTest("xlwt BIFF8 test-fixture dependency unavailable")
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "information.xls"
            _write_v11_information_xls(target)
            results = XlrdInformationXlsBackend().read_results(target)
        by_code = {item.item_code: item for item in results}
        self.assertEqual(by_code["OLD-CODE"].legacy_item_code, "OLD-LEGACY")
        self.assertEqual(by_code["OLD-CODE"].quality_grade, " QA1 ")
        self.assertEqual(by_code["OLD-CODE"].quality_grade_effective, "QA1")
        self.assertTrue(by_code["OLD-CODE"].is_qa1)
        self.assertTrue(by_code["QA1-LOW"].is_qa1)
        self.assertTrue(by_code["QA1-SPACE"].is_qa1)
        # 空 D 只在 raw 为空时回退 effective；本行 effective 也为空，因此不是 QA1。
        self.assertFalse(by_code["QA1-FALLBACK"].is_qa1)
        self.assertFalse(by_code["QA2"].is_qa1)
        self.assertEqual(by_code["QA1-LOW"].status, "SUCCESS")
        self.assertEqual(by_code["QA1-LOW"].status_raw, "success")

    def test_legacy_a_c_runtime_fixture_is_rejected_explicitly(self) -> None:
        """旧版 A:C 文件只作拒绝协议的回归样本，不能放宽生产 A:E 门禁。

        The checked-in runtime is intentionally v1.1 (A:E), so construct the
        legacy three-column BIFF fixture in a temporary directory instead of
        treating the production runtime file as a legacy sample.
        """

        if not XlrdInformationXlsBackend.dependencies_available():
            self.skipTest("information.xls BIFF backend dependency unavailable")
        try:
            import xlwt
        except ImportError:
            self.skipTest("xlwt BIFF8 test-fixture dependency unavailable")
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "legacy_information.xls"
            workbook = xlwt.Workbook()
            sheet = workbook.add_sheet("Sheet1")
            for column, value in enumerate(XlrdInformationXlsBackend().header[:3]):
                sheet.write(0, column, value)
            sheet.write(1, 0, "M001")
            sheet.write(1, 1, "旧检查单")
            sheet.write(1, 2, "OLD-CODE")
            workbook.save(str(source))
            with self.assertRaisesRegex(WindchillAdapterError, "表头不符合已确认协议"):
                XlrdInformationXlsBackend().read_results(source)

    def test_real_adapter_orchestrates_serial_protocol_with_injected_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exe = root / "WindchillTechnicalSpecificationExporter.exe"
            info = root / "information.xls"
            exe.touch()
            info.touch()
            backend = _FakeXlsBackend(
                [
                    WindchillName("M001", "检查单_1", "SUCCESS"),
                    WindchillName("M002", "检查单_2", "SUCCESS"),
                ]
            )
            events: list[str] = []

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                events.append("exe")
                self.assertEqual(command, [str(exe)])
                return subprocess.CompletedProcess(command, 0, "", "")

            adapter = WindchillTechnicalSpecificationExporterAdapter(
                exe_path=exe,
                information_xls=info,
                backend=backend,
                runner=runner,
                release_probe=lambda path: events.append("released") or True,
                poll_seconds=0,
            )
            results = adapter.query_names(["M001", "M001", "M002"])

            self.assertEqual([item.item_code for item in results], ["M001", "M002"])
            self.assertEqual(backend.codes, ["M001", "M002"])
            self.assertEqual(backend.events, ["write_closed", "read_closed"])
            self.assertEqual(events, ["released", "released", "exe", "released"])

    def test_real_adapter_rejects_duplicate_or_failed_business_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exe = root / "WindchillTechnicalSpecificationExporter.exe"
            info = root / "information.xls"
            exe.touch()
            info.touch()
            backend = _FakeXlsBackend(
                [
                    WindchillName("M001", "检查单_1", "SUCCESS"),
                    WindchillName("M001", "检查单_1_重复", "SUCCESS"),
                ]
            )
            adapter = WindchillTechnicalSpecificationExporterAdapter(
                exe_path=exe,
                information_xls=info,
                backend=backend,
                runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
                release_probe=lambda path: True,
                poll_seconds=0,
            )
            with self.assertRaises(WindchillAdapterError):
                adapter.query_names(["M001"])

    def test_real_adapter_surfaces_empty_result_as_dedicated_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exe = root / "WindchillTechnicalSpecificationExporter.exe"
            info = root / "information.xls"
            exe.touch()
            info.touch()
            adapter = WindchillTechnicalSpecificationExporterAdapter(
                exe_path=exe,
                information_xls=info,
                backend=_FakeXlsBackend([]),
                runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
                release_probe=lambda path: True,
                poll_seconds=0,
            )
            with self.assertRaisesRegex(WindchillEmptyResultError, "Windchill查询为空"):
                adapter.query_names(["M001"])

    def test_real_adapter_rejects_failed_e_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exe = root / "WindchillTechnicalSpecificationExporter.exe"
            info = root / "information.xls"
            exe.touch()
            info.touch()
            backend = _FakeXlsBackend(
                [WindchillName("M001", "检查单_1", "ERROR", status_raw=" failed ")]
            )
            adapter = WindchillTechnicalSpecificationExporterAdapter(
                exe_path=exe,
                information_xls=info,
                backend=backend,
                runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
                release_probe=lambda path: True,
                poll_seconds=0,
            )
            with self.assertRaisesRegex(WindchillAdapterError, r"failed=\['M001'\]"):
                adapter.query_names(["M001"])

    def test_real_adapter_surfaces_process_failure_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exe = root / "WindchillTechnicalSpecificationExporter.exe"
            info = root / "information.xls"
            exe.touch()
            info.touch()

            def make_adapter(runner):
                return WindchillTechnicalSpecificationExporterAdapter(
                    exe_path=exe,
                    information_xls=info,
                    backend=_FakeXlsBackend([WindchillName("M001", "检查单_1", "SUCCESS")]),
                    runner=runner,
                    release_probe=lambda path: True,
                    poll_seconds=0,
                )

            with self.assertRaisesRegex(WindchillAdapterError, "非零退出"):
                make_adapter(lambda command, **kwargs: subprocess.CompletedProcess(command, 9, "", "")).query_names(["M001"])

            def timeout_runner(command, **kwargs):
                raise subprocess.TimeoutExpired(command, 1)

            with self.assertRaisesRegex(WindchillAdapterError, "超时"):
                make_adapter(timeout_runner).query_names(["M001"])


if __name__ == "__main__":
    unittest.main()
