from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from launcher import LauncherSettings, PortableLauncher, _launcher_root


class LauncherTests(unittest.TestCase):
    def test_launcher_config_accepts_cp936_chinese_unc_path(self) -> None:
        """Win7/Chinese Notepad ANSI saves must not make launcher unstartable."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_file = root / "config" / "launcher.json"
            config_file.parent.mkdir()
            config_file.write_bytes(
                json.dumps(
                    {"shared_root": r"\\192.0.2.10\合成组织\示例共享\share"},
                    ensure_ascii=False,
                ).encode("cp936")
            )
            settings = LauncherSettings.from_root(root, config_path=config_file)
            self.assertEqual(str(settings.shared_root), r"\\192.0.2.10\合成组织\示例共享\share")

    def _package(self, root: Path, *, traversal: bool = False) -> tuple[Path, str]:
        package = root / "shared" / "IQC_update.zip"
        package.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("portable_entry.py", "print('self-test fixture')\n")
            archive.writestr("VERSION", "1.2.0\n")
            if traversal:
                archive.writestr("../escape.txt", "must reject")
        return package, hashlib.sha256(package.read_bytes()).hexdigest()

    def test_update_uses_manifest_hash_and_preserves_old_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            current.mkdir()
            (current / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (current / "portable_entry.py").write_text("old", encoding="utf-8")
            package, sha256 = self._package(root)
            manifest = root / "shared" / "manifest.json"
            manifest.write_text(
                json.dumps({"latest_version": "1.2.0", "package": package.name, "sha256": sha256}),
                encoding="utf-8",
            )
            started: list[Path] = []
            settings = LauncherSettings(current, shared_root=package.parent, versions_root=root / "versions")
            launcher = PortableLauncher(
                settings,
                self_test_runner=lambda candidate: (candidate / "portable_entry.py").is_file(),
                process_starter=started.append,
            )
            result = launcher.run(confirm_update=lambda _manifest: True)
            self.assertEqual(result.status, "UPDATED")
            self.assertTrue((current / "portable_entry.py").read_text(encoding="utf-8") == "old")
            self.assertEqual(started, [root / "versions" / "1.2.0"])
            self.assertEqual((current / "current.txt").read_text(encoding="utf-8").strip(), "1.2.0")
            # 成功切换后仅保留版本目录/current 指针；临时 staging 与复制的
            # package.zip 必须清理，避免下次启动误把半成品当成版本。
            self.assertFalse(any(path.name.startswith(".iqc-update-") for path in (root / "versions").iterdir()))
            audit = current / "日志" / "launcher_update.jsonl"
            self.assertTrue(audit.is_file())
            events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertTrue(events)
            event_names = [event["event"] for event in events]
            for name in ("update_probe", "update_manifest_read", "update_package_probe", "update_package_hash", "update_switched"):
                self.assertIn(name, event_names)
            fields = events[-1]["fields"]
            for key in ("source_manifest", "source_url", "version", "package_sha256", "active", "current", "result"):
                self.assertIn(key, fields)

    def test_invalid_hash_or_zip_traversal_blocks_without_starting_new_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            current.mkdir()
            (current / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (current / "portable_entry.py").write_text("old", encoding="utf-8")
            package, _sha256 = self._package(root, traversal=True)
            manifest = root / "shared" / "manifest.json"
            manifest.write_text(
                json.dumps({"latest_version": "1.2.0", "package": package.name, "sha256": "0" * 64}),
                encoding="utf-8",
            )
            started: list[Path] = []
            launcher = PortableLauncher(
                LauncherSettings(current, shared_root=package.parent, versions_root=root / "versions"),
                self_test_runner=lambda _candidate: True,
                process_starter=started.append,
            )
            result = launcher.run(confirm_update=lambda _manifest: True)
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(started, [])
            self.assertFalse((root / "versions" / "1.2.0").exists())

    def test_version_mismatch_blocks_install_before_switching_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            current.mkdir()
            (current / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (current / "portable_entry.py").write_text("old", encoding="utf-8")
            package, _sha256 = self._package(root)
            # Recreate the fixture with a single, deliberately mismatched
            # VERSION entry (avoid duplicate ZIP names and their warning).
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("portable_entry.py", "print('self-test fixture')\n")
                archive.writestr("VERSION", "9.9.9\n")
            sha256 = hashlib.sha256(package.read_bytes()).hexdigest()
            (root / "shared" / "manifest.json").write_text(
                json.dumps({"latest_version": "1.2.0", "package": package.name, "sha256": sha256}),
                encoding="utf-8",
            )
            started: list[Path] = []
            launcher = PortableLauncher(
                LauncherSettings(current, shared_root=package.parent, versions_root=root / "versions"),
                self_test_runner=lambda _candidate: True,
                process_starter=started.append,
            )
            result = launcher.run(confirm_update=lambda _manifest: True)
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(started, [])
            self.assertFalse((root / "versions" / "1.2.0").exists())

    def test_self_test_failure_keeps_current_pointer_and_old_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            current.mkdir()
            (current / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (current / "portable_entry.py").write_text("old", encoding="utf-8")
            package, sha256 = self._package(root)
            (root / "shared" / "manifest.json").write_text(
                json.dumps({"latest_version": "1.2.0", "package": package.name, "sha256": sha256}),
                encoding="utf-8",
            )
            started: list[Path] = []
            launcher = PortableLauncher(
                LauncherSettings(current, shared_root=package.parent, versions_root=root / "versions"),
                self_test_runner=lambda _candidate: False,
                process_starter=started.append,
            )
            result = launcher.run(confirm_update=lambda _manifest: True)
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(started, [])
            self.assertFalse((current / "current.txt").exists())
            self.assertEqual((current / "portable_entry.py").read_text(encoding="utf-8"), "old")

    def test_new_process_failure_restores_previous_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            current.mkdir()
            (current / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (current / "portable_entry.py").write_text("old", encoding="utf-8")
            versions = root / "versions"
            old_dir = versions / "1.0.0"
            old_dir.mkdir(parents=True)
            (old_dir / "portable_entry.py").write_text("old-version", encoding="utf-8")
            (old_dir / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (current / "current.txt").write_text("1.0.0\n", encoding="utf-8")
            package, sha256 = self._package(root)
            (root / "shared" / "manifest.json").write_text(
                json.dumps({"latest_version": "1.2.0", "package": package.name, "sha256": sha256}),
                encoding="utf-8",
            )
            launcher = PortableLauncher(
                LauncherSettings(current, shared_root=package.parent, versions_root=root / "versions"),
                self_test_runner=lambda _candidate: True,
                process_starter=lambda _candidate: (_ for _ in ()).throw(OSError("start failed")),
            )
            result = launcher.run(confirm_update=lambda _manifest: True)
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual((current / "current.txt").read_text(encoding="utf-8").strip(), "1.0.0")
            self.assertTrue((root / "versions" / "1.2.0").is_dir())

    def test_manifest_invalid_or_shared_unavailable_blocks_without_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            current.mkdir()
            (current / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (current / "portable_entry.py").write_text("old", encoding="utf-8")
            shared = root / "shared"
            shared.mkdir()
            (shared / "manifest.json").write_text(json.dumps({"latest_version": "bad"}), encoding="utf-8")
            started: list[Path] = []
            launcher = PortableLauncher(
                LauncherSettings(current, shared_root=shared, versions_root=root / "versions"),
                process_starter=started.append,
            )
            self.assertEqual(launcher.check_for_update().status, "INVALID")
            self.assertEqual(launcher.run(confirm_update=lambda _manifest: True).status, "CURRENT_FALLBACK")
            self.assertEqual(started, [current])
            started.clear()
            unavailable = PortableLauncher(
                LauncherSettings(
                    current,
                    shared_root=root / "missing-shared",
                    versions_root=root / "versions",
                ),
                process_starter=started.append,
            )
            self.assertEqual(unavailable.check_for_update().status, "UNAVAILABLE")
            self.assertEqual(unavailable.run(confirm_update=lambda _manifest: True).status, "CURRENT_FALLBACK")
            self.assertEqual(started, [current])

    def test_user_decline_starts_current_without_installing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            current.mkdir()
            (current / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (current / "portable_entry.py").write_text("old", encoding="utf-8")
            package, sha256 = self._package(root)
            (root / "shared" / "manifest.json").write_text(
                json.dumps({"latest_version": "1.2.0", "package": package.name, "sha256": sha256}),
                encoding="utf-8",
            )
            started: list[Path] = []
            launcher = PortableLauncher(
                LauncherSettings(current, shared_root=package.parent, versions_root=root / "versions"),
                process_starter=started.append,
            )
            result = launcher.run(confirm_update=lambda _manifest: False)
            self.assertEqual(result.status, "DECLINED")
            self.assertEqual(started, [current])
            self.assertFalse((root / "versions" / "1.2.0").exists())

    def test_start_process_prefers_packaged_exe_then_source_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "portable_entry.py").write_text("source", encoding="utf-8")
            exe = root / "IQC-Python-Portable.exe"
            exe.write_bytes(b"fake")
            launcher = PortableLauncher(LauncherSettings(root))
            with patch("launcher.subprocess.Popen") as popen:
                launcher._start_process(root)
            command = popen.call_args.args[0]
            self.assertEqual(command, [str(exe), "--launched-by-launcher"])
            exe.unlink()
            with patch("launcher.subprocess.Popen") as popen:
                launcher._start_process(root)
            command = popen.call_args.args[0]
            self.assertEqual(command[-2:], [str(root / "portable_entry.py"), "--launched-by-launcher"])

    def test_packaged_root_exe_is_valid_active_entry_and_self_test_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exe = root / "IQC-Python-Portable.exe"
            exe.write_bytes(b"fake")
            launcher = PortableLauncher(LauncherSettings(root))
            self.assertTrue(launcher._entry_exists(root))
            with patch("launcher.subprocess.run") as run:
                run.return_value.returncode = 0
                self.assertTrue(launcher._run_self_test(root))
            command = run.call_args.args[0]
            self.assertEqual(command, [str(exe), "--self-test"])

    def test_frozen_launcher_root_comes_from_executable_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exe = root / "IQC-Python-Portable.exe"
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(exe)):
                self.assertEqual(_launcher_root(), root)


if __name__ == "__main__":
    unittest.main()
