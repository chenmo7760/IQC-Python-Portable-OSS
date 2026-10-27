from __future__ import annotations

import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from .models import WindchillName


class WindchillAdapterError(RuntimeError):
    pass


class WindchillEmptyResultError(WindchillAdapterError):
    """Windchill completed but returned no rows for a non-empty request."""

    pass


class InformationXlsBackend(Protocol):
    """information.xls 的读写边界。

    实现必须先清理旧的 A2:E 内容、写入新的编码，并保证 ``write_codes`` 返回时工作簿已经保存并彻底关闭，
    ``read_results`` 必须以只读方式打开并在返回前关闭工作簿。这样编排器
    才能在两个阶段之间检查文件没有被 Python 自己占用。
    """

    def write_codes(self, path: Path, item_codes: Sequence[str]) -> None:
        ...

    def read_results(self, path: Path) -> list[WindchillName]:
        ...


@dataclass(frozen=True)
class XlrdInformationXlsBackend:
    """已确认 BIFF8 ``information.xls`` 协议的最小读写实现。

    只处理 ``Sheet1!A:E``：清空旧查询行、按顺序写入编码，再只读返回
    编码、检验规范名称、旧物项识别码、质保等级和状态。写出使用同目录
    临时文件替换，确保 backend 返回时源 workbook 已释放；缺少依赖或表头
    变化会阻断，而不是猜测列映射。
    """

    sheet_name: str = "Sheet1"
    header: tuple[str, str, str, str, str] = (
        "集团编码",
        "进/退货检验要求",
        "旧物项识别码",
        "质保等级",
        "状态",
    )

    @staticmethod
    def _modules():
        try:
            import xlrd  # type: ignore[import-not-found]
            from xlutils.copy import copy as copy_workbook  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - package build guard
            raise WindchillAdapterError("缺少 information.xls BIFF8 backend 依赖") from exc
        return xlrd, copy_workbook

    @classmethod
    def dependencies_available(cls) -> bool:
        try:
            cls._modules()
        except WindchillAdapterError:
            return False
        return True

    @staticmethod
    def _text(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    def _open(self, path: Path):
        xlrd, _copy = self._modules()
        if not path.is_file():
            raise WindchillAdapterError(f"information.xls 不存在：{path}")
        try:
            book = xlrd.open_workbook(str(path), on_demand=False)
            sheet = book.sheet_by_name(self.sheet_name)
        except Exception as exc:
            raise WindchillAdapterError(f"无法读取 information.xls：{type(exc).__name__}") from exc
        actual = tuple(self._text(sheet.cell_value(0, index)) for index in range(min(5, sheet.ncols)))
        if actual != self.header:
            book.release_resources()
            raise WindchillAdapterError("information.xls Sheet1 表头不符合已确认协议，拒绝猜测列映射")
        return book, sheet

    def write_codes(self, path: Path, item_codes: Sequence[str]) -> None:
        book, sheet = self._open(path)
        _xlrd, copy_workbook = self._modules()
        writable = None
        temporary: Path | None = None
        book_released = False
        try:
            writable = copy_workbook(book)
            target = writable.get_sheet(sheet.number)
            for row in range(1, sheet.nrows):
                for col in range(5):
                    target.write(row, col, "")
            for row, code in enumerate(_dedupe_codes(item_codes), start=1):
                target.write(row, 0, code)
            with tempfile.NamedTemporaryFile(prefix="information.", suffix=".xls", dir=str(path.parent), delete=False) as handle:
                temporary = Path(handle.name)
            writable.save(str(temporary))
            # Windows 下原 workbook 仍持有 information.xls 时，直接替换会
            # 触发共享冲突。先释放只读句柄，再替换原文件；临时文件仍由
            # finally 统一清理，确保保存/替换任一步失败都不留垃圾文件。
            book.release_resources()
            book_released = True
            temporary.replace(path)
        except WindchillAdapterError:
            raise
        except Exception as exc:
            raise WindchillAdapterError(f"无法写入 information.xls：{type(exc).__name__}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if not book_released:
                try:
                    book.release_resources()
                except Exception:
                    # 原始异常优先；释放失败不能掩盖真正的写出错误。
                    pass

    def read_results(self, path: Path) -> list[WindchillName]:
        book, sheet = self._open(path)
        try:
            results: list[WindchillName] = []
            for row in range(1, sheet.nrows):
                item_code = self._text(sheet.cell_value(row, 0))
                if not item_code:
                    continue
                # ``_text`` 用于键和显示匹配会去除首尾空格；D/E 同时保留
                # 未修剪的 raw 值，以便 QA1 判断和现场审计能还原导出原文。
                raw_name = "" if sheet.cell_value(row, 1) is None else str(sheet.cell_value(row, 1))
                raw_legacy = "" if sheet.cell_value(row, 2) is None else str(sheet.cell_value(row, 2))
                raw_grade = "" if sheet.cell_value(row, 3) is None else str(sheet.cell_value(row, 3))
                raw_status = "" if sheet.cell_value(row, 4) is None else str(sheet.cell_value(row, 4))
                effective_grade = raw_grade.strip().upper()
                grade_basis = "strip().upper() == 'QA1'" if effective_grade == "QA1" else "strip().upper() != 'QA1'"
                status_text = raw_status.strip().upper()
                results.append(
                    WindchillName(
                        item_code=item_code,
                        name=self._text(sheet.cell_value(row, 1)),
                        status=status_text,
                        legacy_item_code=raw_legacy,
                        quality_grade=raw_grade,
                        quality_grade_effective=effective_grade,
                        quality_grade_basis=grade_basis,
                        status_raw=raw_status,
                    )
                )
            return results
        finally:
            book.release_resources()


def _default_file_release_probe(path: Path) -> bool:
    """轻量本地探针；真实 Windows 进程占用检查由后续环境适配层注入。"""

    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


def _wait_until_released(
    path: Path,
    probe: Callable[[Path], bool],
    timeout_seconds: float,
    poll_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if probe(path):
            return
        time.sleep(poll_seconds)
    raise WindchillAdapterError(f"information.xls 在限定时间内仍被占用：{path}")


@dataclass(frozen=True)
class MockWindchillAdapter:
    """仅用于本地测试，不读写 information.xls，也不启动 EXE。"""

    names: Mapping[str, str]

    def query_names(self, item_codes: list[str]) -> list[WindchillName]:
        seen: set[str] = set()
        result: list[WindchillName] = []
        for item_code in item_codes:
            code = item_code.strip()
            if not code or code in seen:
                continue
            seen.add(code)
            name = self.names.get(code, "")
            result.append(WindchillName(item_code=code, name=name, status="SUCCESS" if name else "ERROR"))
        return result


def assert_success(results: list[WindchillName], expected_codes: list[str]) -> None:
    if expected_codes and not results:
        raise WindchillEmptyResultError(
            "Windchill查询为空；expected_codes=" + ",".join(str(code) for code in expected_codes)
        )
    duplicates = _duplicate_codes(results)
    by_code = {result.item_code: result for result in results}
    missing = [code for code in expected_codes if code not in by_code]
    failed = [code for code in expected_codes if code in by_code and by_code[code].status != "SUCCESS"]
    blank_names = [
        code
        for code in expected_codes
        if code in by_code and by_code[code].status == "SUCCESS" and not by_code[code].name.strip()
    ]
    unexpected = [result.item_code for result in results if result.item_code not in set(expected_codes)]
    if duplicates or missing or failed or blank_names or unexpected:
        raise WindchillAdapterError(
            "Windchill 返回校验失败："
            f"duplicates={duplicates}, missing={missing}, failed={failed}, "
            f"blank_names={blank_names}, unexpected={unexpected}"
        )


def _duplicate_codes(results: Sequence[WindchillName]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for result in results:
        if result.item_code in seen and result.item_code not in duplicates:
            duplicates.append(result.item_code)
        seen.add(result.item_code)
    return duplicates


_WINDCHILL_LOCK = threading.Lock()


@dataclass
class WindchillTechnicalSpecificationExporterAdapter:
    """串行编排固定 information.xls 与 Windchill EXE。

    该类是 WO-05 的真实调用边界，但不会在测试或导入流程中自动触发。
    通过 ``backend`` 注入具体的旧版 XLS 读写实现，避免在没有确认依赖时
    猜测 BIFF 文件格式。``runner`` 和 ``release_probe`` 仅用于本地模拟与
    后续受控联调，默认分别使用 subprocess.run 和轻量文件可读探针。
    """

    exe_path: Path
    information_xls: Path
    backend: InformationXlsBackend
    timeout_seconds: float = 120.0
    release_timeout_seconds: float = 30.0
    poll_seconds: float = 0.1
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None
    release_probe: Callable[[Path], bool] = _default_file_release_probe

    def query_names(self, item_codes: Sequence[str]) -> list[WindchillName]:
        codes = _dedupe_codes(item_codes)
        if not codes:
            return []
        if not self.exe_path.exists():
            raise WindchillAdapterError(f"Windchill EXE 不存在：{self.exe_path}")
        if not self.information_xls.exists():
            raise WindchillAdapterError(f"information.xls 不存在：{self.information_xls}")

        with _WINDCHILL_LOCK:
            # 1) 先确认旧文件未被其他程序占用；不强杀 Excel/WPS。
            _wait_until_released(
                self.information_xls,
                self.release_probe,
                self.release_timeout_seconds,
                self.poll_seconds,
            )
            # 2) backend 内部必须完成写入、保存、关闭。
            self.backend.write_codes(self.information_xls, codes)
            _wait_until_released(
                self.information_xls,
                self.release_probe,
                self.release_timeout_seconds,
                self.poll_seconds,
            )
            # 3) subprocess.run 会等待 EXE 结束，不允许并发启动。
            run = self.runner or subprocess.run
            command = [str(self.exe_path)]
            try:
                completed = run(
                    command,
                    cwd=str(self.exe_path.parent),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired as exc:
                raise WindchillAdapterError("Windchill EXE 执行超时") from exc
            except OSError as exc:
                raise WindchillAdapterError(f"无法启动 Windchill EXE：{exc}") from exc
            if completed.returncode != 0:
                raise WindchillAdapterError(f"Windchill EXE 非零退出：{completed.returncode}")
            # 4) EXE 结束后仍需等待其释放 information.xls。
            _wait_until_released(
                self.information_xls,
                self.release_probe,
                self.release_timeout_seconds,
                self.poll_seconds,
            )
            # 5) backend 以只读方式读取 A:E，并在返回前关闭文件。
            results = self.backend.read_results(self.information_xls)
            assert_success(results, codes)
            by_code = {result.item_code: result for result in results}
            return [by_code[code] for code in codes]


def _dedupe_codes(item_codes: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item_code in item_codes:
        code = str(item_code).strip()
        if code and code not in seen:
            seen.add(code)
            result.append(code)
    return result
