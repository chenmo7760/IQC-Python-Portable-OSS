"""IQC 便携目录稳定启动器（源码阶段）。

启动器只负责“发现共享 manifest → 用户确认 → 下载/校验/解压/自检 →
切换 current → 启动新版”这一条可回滚链，不绑定任何旧版 ZIP 文件名，也
不接触数据库、BAT 或 auto-factory。现场打包阶段可将本文件作为固定快捷方式
入口；旧版本目录永不覆盖或删除，更新失败则保留旧版并退出。

manifest 中的 SHA-256 仅证明传输文件完整性；没有签名时不能证明发布者
身份。``signature_verifier`` 只是预留的部署校验接口，签名算法/密钥策略
仍需现场确认，本源码不宣称已实现发布者身份认证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Mapping, Sequence
import uuid
import zipfile


class LauncherError(RuntimeError):
    """manifest、校验、解压或版本切换失败。"""


@dataclass(frozen=True)
class LauncherSettings:
    """启动器配置；共享目录只从 JSON/环境变量显式注入。"""

    current_root: Path
    shared_root: Path | None = None
    versions_root: Path | None = None
    manifest_name: str = "manifest.json"
    version_file: str = "VERSION"
    current_pointer_name: str = "current.txt"
    entry_name: str = "portable_entry.py"
    executable_name: str = "IQC-Python-Portable.exe"
    self_test_timeout_seconds: float = 90.0

    @classmethod
    def from_root(cls, root: Path, *, config_path: Path | None = None) -> "LauncherSettings":
        """读取 ``config/launcher.json`` 或环境变量，不写死共享 ZIP 路径。"""

        root = Path(root).resolve()
        config_file = config_path or (root / "config" / "launcher.json")
        values: Mapping[str, object] = {}
        try:
            if config_file.is_file():
                # Windows 7/Chinese Notepad may save a hand-edited launcher
                # config as the system ANSI code page (GBK/cp936).  The
                # shipped file is UTF-8, but accepting this narrow fallback
                # keeps a Chinese UNC ``shared_root`` from making the stable
                # launcher unstartable.  Any JSON/schema error still blocks.
                try:
                    # ``utf-8-sig`` accepts both a clean UTF-8 file and the
                    # UTF-8-BOM form emitted by older Notepad versions.
                    config_text = config_file.read_bytes().decode("utf-8-sig")
                except UnicodeDecodeError:
                    config_text = config_file.read_bytes().decode("cp936")
                decoded = json.loads(config_text)
                if not isinstance(decoded, Mapping):
                    raise LauncherError("launcher.json 根节点必须是对象")
                values = decoded
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LauncherError(f"启动器配置无法读取：{type(exc).__name__}") from exc

        shared_value = os.environ.get("IQC_SHARED_ROOT", "").strip() or values.get("shared_root")
        shared_root = Path(str(shared_value)).expanduser() if shared_value else None
        if shared_root is not None and not shared_root.is_absolute():
            shared_root = (root / shared_root).resolve()
        versions_value = values.get("versions_root")
        versions_root = Path(str(versions_value)).expanduser() if versions_value else root / "versions"
        if not versions_root.is_absolute():
            versions_root = (root / versions_root).resolve()
        timeout_value = values.get("self_test_timeout_seconds", 90.0)
        try:
            timeout = max(1.0, float(timeout_value))
        except (TypeError, ValueError):
            raise LauncherError("self_test_timeout_seconds 必须是正数")
        return cls(
            current_root=root,
            shared_root=shared_root,
            versions_root=versions_root,
            manifest_name=str(values.get("manifest_name", "manifest.json")),
            version_file=str(values.get("version_file", "VERSION")),
            current_pointer_name=str(values.get("current_pointer_name", "current.txt")),
            entry_name=str(values.get("entry_name", "portable_entry.py")),
            executable_name=str(values.get("executable_name", "IQC-Python-Portable.exe")),
            self_test_timeout_seconds=timeout,
        )


@dataclass(frozen=True)
class UpdateManifest:
    """经过严格校验的共享版本清单。

    SHA-256 只用于完整性校验；发布者身份认证需由部署方注入签名校验器。
    """

    version: str
    package: str
    sha256: str
    signature: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "UpdateManifest":
        raw_version = payload.get("latest_version", payload.get("version"))
        raw_package = payload.get("package", payload.get("artifact"))
        raw_sha = payload.get("sha256", payload.get("sha"))
        if not raw_version or not raw_package or not raw_sha:
            raise LauncherError("manifest 缺少 latest_version/package/sha256")
        version = str(raw_version).strip()
        package = _safe_relative_name(str(raw_package).strip())
        sha256 = str(raw_sha).strip().lower()
        if not _valid_version(version):
            raise LauncherError(f"manifest 版本号无效：{version!r}")
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise LauncherError("manifest sha256 必须是 64 位十六进制")
        signature = str(payload.get("signature", "")).strip()
        return cls(version, package, sha256, signature, dict(payload))


@dataclass(frozen=True)
class UpdateCheck:
    status: str
    current_version: str
    manifest: UpdateManifest | None = None
    detail: str = ""


@dataclass(frozen=True)
class LaunchResult:
    status: str
    root: Path
    detail: str = ""


def _valid_version(value: str) -> bool:
    parts = value.strip().split(".")
    return bool(parts) and all(part.isdigit() for part in parts)


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.strip().split("."))


def _safe_relative_name(value: str) -> str:
    """校验 manifest/ZIP 相对路径，拒绝绝对路径、盘符和 ``..``。"""

    normal = value.replace("\\", "/").strip()
    parsed = PurePosixPath(normal)
    if not parsed.parts or parsed.is_absolute() or any(":" in part for part in parsed.parts):
        raise LauncherError(f"路径必须是相对路径：{value!r}")
    if any(part in {"", ".", ".."} for part in parsed.parts):
        raise LauncherError(f"路径包含非法片段：{value!r}")
    return "/".join(parsed.parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(path: Path) -> UpdateManifest:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LauncherError(f"manifest 读取失败：{type(exc).__name__}") from exc
    if not isinstance(payload, Mapping):
        raise LauncherError("manifest 根节点必须是对象")
    # 支持可选 versions 列表，但只接受清单明确给出的最高版本；不猜测
    # 目录中“最新” ZIP，版本比较完全由 manifest 控制。
    if "latest_version" not in payload and isinstance(payload.get("versions"), list):
        entries = [item for item in payload["versions"] if isinstance(item, Mapping)]
        if not entries:
            raise LauncherError("manifest versions 列表为空")
        if any(not _valid_version(str(item.get("version", "")).strip()) for item in entries):
            raise LauncherError("manifest versions 含非法版本号")
        entries.sort(key=lambda item: _version_key(str(item.get("version", "0"))), reverse=True)
        payload = entries[0]
    return UpdateManifest.from_mapping(payload)


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    """安全解压，不允许 ZIP 条目逃逸 staging 目录。"""

    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for member in archive.infolist():
        name = _safe_relative_name(member.filename)
        target = (destination / Path(*PurePosixPath(name).parts)).resolve()
        try:
            common = os.path.commonpath([str(destination), str(target)])
        except ValueError as exc:
            raise LauncherError("ZIP 条目路径跨越卷边界") from exc
        if common != str(destination):
            raise LauncherError(f"ZIP 条目逃逸 staging：{member.filename!r}")
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member, "r") as source, target.open("wb") as sink:
            shutil.copyfileobj(source, sink)


class PortableLauncher:
    """可注入文件复制、自检和进程启动器的更新流程。"""

    def __init__(
        self,
        settings: LauncherSettings,
        *,
        signature_verifier: Callable[[UpdateManifest, Path], bool] | None = None,
        self_test_runner: Callable[[Path], bool] | None = None,
        process_starter: Callable[[Path], None] | None = None,
        event_logger: Callable[[str, Mapping[str, object]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.signature_verifier = signature_verifier
        self.self_test_runner = self_test_runner or self._run_self_test
        self.process_starter = process_starter or self._start_process
        self.event_logger = event_logger

    def _entry_exists(self, root: Path) -> bool:
        """判断版本根目录是否存在可启动入口。

        源码目录通常只有 ``portable_entry.py``，正式 PyInstaller onedir
        则以根部 ``IQC-Python-Portable.exe`` 为入口且不会复制源码脚本。
        两种布局都属于受控候选；绝不因为缺少其中一种而误判版本无效。
        """

        root = Path(root)
        return (root / self.settings.executable_name).is_file() or (
            root / self.settings.entry_name
        ).is_file()

    def current_version(self) -> str:
        # current.txt 是唯一切换指针；切换后必须从 active 版本目录读取
        # VERSION，否则每次启动都会误判为 AVAILABLE 并重复安装同版本。
        path = self.resolve_active_root() / self.settings.version_file
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return "0"
        return value if _valid_version(value) else "0"

    def check_for_update(self) -> UpdateCheck:
        current = self.current_version()
        shared = self.settings.shared_root
        if shared is None:
            return UpdateCheck("DISABLED", current, detail="未配置共享更新目录，直接使用当前版本")
        try:
            manifest_name = _safe_relative_name(self.settings.manifest_name)
        except LauncherError as exc:
            self._log("update_fallback", {"result": "CURRENT_FALLBACK", "error": str(exc)})
            return UpdateCheck("INVALID", current, detail=str(exc))
        manifest_path = shared / Path(*PurePosixPath(manifest_name).parts)
        manifest_exists = manifest_path.is_file()
        self._log(
            "update_probe",
            {
                "result": "PROBE",
                "source_manifest": str(manifest_path),
                "current_version": current,
                "manifest_exists": manifest_exists,
            },
        )
        if not manifest_exists:
            self._log(
                "update_fallback",
                {"result": "CURRENT_FALLBACK", "source_manifest": str(manifest_path), "error": "manifest_unavailable"},
            )
            return UpdateCheck("UNAVAILABLE", current, detail="共享目录或 manifest 不可用，继续使用当前旧版")
        try:
            manifest = _read_manifest(manifest_path)
        except LauncherError as exc:
            self._log(
                "update_fallback",
                {"result": "CURRENT_FALLBACK", "source_manifest": str(manifest_path), "error": str(exc)},
            )
            return UpdateCheck("INVALID", current, detail=str(exc))
        try:
            manifest_size = manifest_path.stat().st_size
        except OSError:
            manifest_size = -1
        self._log(
            "update_manifest_read",
            {
                "result": "READ",
                "source_manifest": str(manifest_path),
                "current_version": current,
                "version": manifest.version,
                "package": manifest.package,
                "package_sha256": manifest.sha256,
                "manifest_size": manifest_size,
            },
        )
        if _version_key(manifest.version) <= _version_key(current):
            self._log(
                "update_current",
                {
                    "result": "CURRENT",
                    "source_manifest": str(manifest_path),
                    "version": manifest.version,
                    "package": manifest.package,
                    "package_sha256": manifest.sha256,
                },
            )
            return UpdateCheck("CURRENT", current, manifest, detail="当前版本已是最新")
        self._log(
            "update_available",
            {
                "result": "AVAILABLE",
                "source_manifest": str(manifest_path),
                "version": manifest.version,
                "package": manifest.package,
                "package_sha256": manifest.sha256,
            },
        )
        return UpdateCheck("AVAILABLE", current, manifest, detail=f"发现新版本 {manifest.version}")

    def run(self, *, confirm_update: Callable[[UpdateManifest], bool] | None = None) -> LaunchResult:
        check = self.check_for_update()
        if check.status in {"DISABLED", "CURRENT"}:
            root = self.resolve_active_root()
            if not self._entry_exists(root):
                return LaunchResult("BLOCKED", root, "当前版本入口不存在，无法安全启动")
            self.process_starter(root)
            self._log("launch_current", {"result": "CURRENT", "version": check.current_version})
            return LaunchResult("CURRENT", root, check.detail)
        if check.status in {"UNAVAILABLE", "INVALID"}:
            # 共享目录不可用/manifest 校验失败时不启动半更新目录，也不删除
            # 当前版本；稳定入口回退启动 active 旧版，避免用户必须重启/换包。
            root = self.resolve_active_root()
            if not self._entry_exists(root):
                return LaunchResult("BLOCKED", root, "当前版本入口不存在，无法安全回退")
            self.process_starter(root)
            self._log("launch_current", {"result": "CURRENT_FALLBACK", "version": check.current_version})
            return LaunchResult("CURRENT_FALLBACK", root, check.detail)
        if check.status != "AVAILABLE" or check.manifest is None:
            return LaunchResult("BLOCKED", self.settings.current_root, check.detail)
        if confirm_update is not None and not confirm_update(check.manifest):
            root = self.resolve_active_root()
            if not self._entry_exists(root):
                return LaunchResult("BLOCKED", root, "当前版本入口不存在，无法安全启动")
            self.process_starter(root)
            self._log("launch_current", {"result": "DECLINED", "version": check.current_version})
            return LaunchResult("DECLINED", root, "用户未确认更新，继续使用当前版本")
        previous_pointer = self._read_current_pointer()
        try:
            with self._update_lock():
                root = self._install(check.manifest)
                try:
                    self.process_starter(root)
                except Exception:
                    # 新进程无法启动时恢复旧 current 指针；新目录仍保留
                    # 供后续人工取证，不删除旧版本，也不宣称已切换。
                    self._restore_current_pointer(previous_pointer)
                    self._log(
                        "update_rollback",
                        {
                            "version": check.manifest.version,
                            "package": check.manifest.package,
                            "package_sha256": check.manifest.sha256,
                            "reason": "process_start_failed",
                        },
                    )
                    raise
                self._log(
                    "update_switched",
                    {
                        "version": check.manifest.version,
                        "package": check.manifest.package,
                        "package_sha256": check.manifest.sha256,
                        "root": str(root),
                    },
                )
            return LaunchResult("UPDATED", root, f"已切换并启动 {check.manifest.version}")
        except Exception as exc:
            # 只清理本次明确创建的 staging 目录；旧版本及其审计文件保持不动。
            self._log(
                "update_rollback",
                {
                    "version": check.manifest.version,
                    "package": check.manifest.package,
                    "package_sha256": check.manifest.sha256,
                    "reason": "install_failed",
                },
            )
            self._log(
                "update_failed",
                {
                    "version": check.manifest.version,
                    "package": check.manifest.package,
                    "package_sha256": check.manifest.sha256,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return LaunchResult("BLOCKED", self.settings.current_root, f"更新失败：{type(exc).__name__}；旧版保留并退出")

    def resolve_active_root(self) -> Path:
        value = self._read_current_pointer()
        if not value or not _valid_version(value):
            return self.settings.current_root
        versions = Path(self.settings.versions_root or (self.settings.current_root / "versions"))
        candidate = (versions / value).resolve()
        if candidate.is_dir() and self._entry_exists(candidate):
            return candidate
        return self.settings.current_root

    def _read_current_pointer(self) -> str | None:
        pointer = self.settings.current_root / self.settings.current_pointer_name
        try:
            value = pointer.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None
        return value or None

    def _restore_current_pointer(self, previous: str | None) -> None:
        pointer = self.settings.current_root / self.settings.current_pointer_name
        if previous is None:
            try:
                pointer.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # 指针恢复失败只能留给现场核查；不覆盖/删除任何版本目录。
                pass
            return
        pointer_tmp = pointer.with_name(pointer.name + f".{uuid.uuid4().hex}.rollback.tmp")
        try:
            pointer_tmp.write_text(previous + "\n", encoding="utf-8")
            os.replace(str(pointer_tmp), str(pointer))
        except OSError:
            try:
                pointer_tmp.unlink()
            except OSError:
                pass

    def _install(self, manifest: UpdateManifest) -> Path:
        shared = self.settings.shared_root
        if shared is None:
            raise LauncherError("未配置共享更新目录")
        package = (shared / Path(*PurePosixPath(manifest.package).parts)).resolve()
        if os.path.commonpath([str(shared.resolve()), str(package)]) != str(shared.resolve()):
            raise LauncherError("manifest package 逃逸共享目录")
        package_exists = package.is_file()
        try:
            package_size = package.stat().st_size if package_exists else -1
        except OSError:
            package_size = -1
        self._log(
            "update_package_probe",
            {
                "result": "PROBE",
                "version": manifest.version,
                "package": manifest.package,
                "package_path": str(package),
                "package_exists": package_exists,
                "package_size": package_size,
                "package_sha256": manifest.sha256,
            },
        )
        if not package_exists:
            raise LauncherError("manifest package 不存在")
        actual_sha256 = _sha256(package)
        self._log(
            "update_package_hash",
            {
                "result": "MATCH" if actual_sha256 == manifest.sha256 else "MISMATCH",
                "version": manifest.version,
                "package": manifest.package,
                "package_sha256": manifest.sha256,
                "actual_sha256": actual_sha256,
            },
        )
        if actual_sha256 != manifest.sha256:
            raise LauncherError("ZIP SHA-256 校验失败")
        if manifest.signature and self.signature_verifier is None:
            raise LauncherError("manifest 含签名但未配置签名校验器")
        if manifest.signature and not self.signature_verifier(manifest, package):
            raise LauncherError("manifest 签名校验失败")
        versions = Path(self.settings.versions_root or (self.settings.current_root / "versions"))
        versions.mkdir(parents=True, exist_ok=True)
        install_dir = versions / manifest.version
        if install_dir.exists():
            raise LauncherError("目标版本目录已存在，拒绝覆盖")
        staging = Path(tempfile.mkdtemp(prefix=".iqc-update-", dir=str(versions)))
        self._log(
            "update_staging",
            {
                "result": "STAGING",
                "version": manifest.version,
                "package": manifest.package,
                "package_sha256": manifest.sha256,
            },
        )
        try:
            archive_path = staging / "package.zip"
            shutil.copy2(package, archive_path)
            if _sha256(archive_path) != manifest.sha256:
                raise LauncherError("本地临时 ZIP SHA-256 校验失败")
            payload = staging / "payload"
            with zipfile.ZipFile(archive_path, "r") as archive:
                _safe_extract(archive, payload)
            candidate = payload
            if not self._entry_exists(candidate):
                children = [path for path in candidate.iterdir() if path.is_dir()]
                if len(children) == 1 and self._entry_exists(children[0]):
                    candidate = children[0]
            if not self._entry_exists(candidate):
                raise LauncherError("新版目录缺少稳定入口文件")
            version_path = candidate / self.settings.version_file
            try:
                candidate_version = version_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise LauncherError("新版目录缺少可读取的 VERSION 文件") from exc
            if not _valid_version(candidate_version) or candidate_version != manifest.version:
                raise LauncherError(
                    f"新版 VERSION 与 manifest 不一致：{candidate_version!r} != {manifest.version!r}"
                )
            if not self.self_test_runner(candidate):
                raise LauncherError("新版 self-test 未通过")
            os.replace(str(candidate), str(install_dir))
            pointer = self.settings.current_root / self.settings.current_pointer_name
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer_tmp = pointer.with_name(pointer.name + f".{uuid.uuid4().hex}.tmp")
            pointer_tmp.write_text(manifest.version + "\n", encoding="utf-8")
            os.replace(str(pointer_tmp), str(pointer))
            # staging 只包含本次下载副本；成功切换后立即清理，旧版本目录
            # 和 current 指针之外不留下可误用的半成品 ZIP。
            shutil.rmtree(staging, ignore_errors=True)
            return install_dir
        except Exception:
            # staging 是本次唯一可安全删除的临时目录，绝不触碰 current_root
            # 或已存在的旧版本目录。
            shutil.rmtree(staging, ignore_errors=True)
            raise

    @contextmanager
    def _update_lock(self):
        """跨进程互斥，避免两个旧快捷方式同时切换 current 指针。"""

        lock_path = self.settings.current_root / ".iqc-launcher.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor: int | None = None
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise LauncherError("已有启动器正在更新，拒绝并发切换") from exc
        try:
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii", "replace"))
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # 锁文件清理失败不触碰版本目录；下次启动将以显式并发阻断
                # 告知现场，避免两个进程继续竞争 current。
                pass

    def _log(self, event: str, fields: Mapping[str, object]) -> None:
        enriched = {
            "source_manifest": str(self.settings.shared_root / self.settings.manifest_name)
            if self.settings.shared_root is not None
            else "",
            "source_url": str(self.settings.shared_root) if self.settings.shared_root is not None else "",
            "version": "",
            "package_sha256": "",
            "active": str(self.resolve_active_root()),
            "current": str(self.settings.current_root),
            "result": event,
            **dict(fields),
        }
        payload = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "event": event,
            "fields": enriched,
        }
        # 默认写入当前安装目录日志；失败仅表示无法追加审计，不改变更新
        # 事务，现场可通过共享 manifest/文件 SHA 继续核查。
        log_path = self.settings.current_root / "日志" / "launcher_update.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
        except (OSError, UnicodeError, TypeError):
            pass
        if self.event_logger is None:
            return
        try:
            self.event_logger(event, enriched)
        except Exception:
            # 日志观察者不得改变更新事务；审计失败由调用方自行留证。
            pass

    def _run_self_test(self, root: Path) -> bool:
        # 正式 onedir 包优先自检根 EXE；源码目录才使用 Python 入口。
        executable = root / self.settings.executable_name
        command = (
            [str(executable), "--self-test"]
            if executable.is_file()
            else [sys.executable, str(root / self.settings.entry_name), "--self-test"]
        )
        try:
            completed = subprocess.run(
                command,
                cwd=str(root),
                timeout=self.settings.self_test_timeout_seconds,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def _start_process(self, root: Path) -> None:
        packaged_exe = root / self.settings.executable_name
        if packaged_exe.is_file():
            # PyInstaller 目录优先走根 EXE；参数仍标记为 launcher 子进程，
            # 防止 EXE 内嵌 portable_entry 再次触发更新检查。
            command = [str(packaged_exe), "--launched-by-launcher"]
        else:
            # 源码目录没有 EXE 时回退 Python 入口，行为与旧快捷方式一致。
            command = [sys.executable, str(root / self.settings.entry_name), "--launched-by-launcher"]
        subprocess.Popen(command, cwd=str(root), close_fds=True)


def _prompt_update(manifest: UpdateManifest) -> bool:
    # 正式便携入口按部署要求“发现新版后提示并强制更新”；这里仍保留
    # 一个独立函数作为 UI/测试 seam，但不提供默认 N、环境变量拒绝或
    # ``--no-update`` 绕过。更新失败时 PortableLauncher 自身会回退旧版。
    print(f"发现 IQC 新版本 {manifest.version}，将自动校验并更新。")
    return True


def _launcher_root() -> Path:
    """解析 launcher 配置根目录，兼容源码与 PyInstaller 冻结态。

    PyInstaller 冻结后模块 ``__file__`` 可能位于 ``_internal``/临时解包
    目录；正式 manifest、config、VERSION/current 必须相对真正的 EXE 根
    解析。源码运行时则继续使用 launcher.py 所在项目目录，便于本地测试。
    """

    if bool(getattr(sys, "frozen", False)):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    root = _launcher_root()
    settings = LauncherSettings.from_root(root)
    launcher = PortableLauncher(settings)
    launcher._log(
        "launcher_start",
        {
            "result": "START",
            "manifest_name": settings.manifest_name,
            "shared_root": str(settings.shared_root) if settings.shared_root is not None else "",
            "current_version": launcher.current_version(),
        },
    )
    # ``--no-update`` 不再作为正式入口的绕过开关；保留其它参数透传给
    # 未来入口扩展，但发现可用版本时始终走提示后确认的强制更新链。
    result = launcher.run(confirm_update=_prompt_update)
    if result.status == "BLOCKED":
        print(result.detail, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
