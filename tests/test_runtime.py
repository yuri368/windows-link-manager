"""Runtime data placement and first-run checks, isolated in TEMP directories."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from linkmanager import __version__
from linkmanager.runtime import RuntimePaths, resolve_runtime_paths

entrypoint = importlib.import_module("linkmanager.main")


class RuntimePathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="linkmanager-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_source_checkout_keeps_existing_config_and_history(self):
        (self.root / "pyproject.toml").write_text("", encoding="utf-8")
        (self.root / "launcher.ps1").write_text("", encoding="utf-8")
        config = self.root / "软链接.txt"
        config.write_text("# 用户已有配置\n", encoding="utf-8")
        state = self.root / ".state"
        state.mkdir()
        history = state / "history.json"
        history.write_text("[]", encoding="utf-8")
        before = (config.read_bytes(), history.read_bytes())

        with patch.object(sys, "frozen", False, create=True):
            paths = resolve_runtime_paths(self.root)

        self.assertTrue(paths.portable)
        self.assertEqual(paths.config, config)
        self.assertEqual(paths.state, state)
        self.assertEqual((config.read_bytes(), history.read_bytes()), before)

    def test_frozen_uses_executable_directory_and_ignores_extracted_resources(self):
        executable = self.root / "便携目录 含空格" / "WindowsLinkManager.exe"
        extracted = self.root / "_MEI12345"
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(executable)), patch.object(sys, "_MEIPASS", str(extracted), create=True):
            paths = resolve_runtime_paths(extracted)
        self.assertTrue(paths.portable)
        self.assertEqual(paths.config, executable.parent / "软链接.txt")
        self.assertEqual(paths.state, executable.parent / ".state")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_installed_module_uses_local_app_data_and_creates_nothing(self):
        installed = self.root / "site-packages"
        local = self.root / "当前用户 本地数据"
        with patch.object(sys, "frozen", False, create=True), patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
            paths = resolve_runtime_paths(installed)
        self.assertFalse(paths.portable)
        self.assertEqual(paths.data_dir, local / "WindowsLinkManager")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_installed_module_has_user_home_fallback(self):
        with patch.object(sys, "frozen", False, create=True), patch.dict(os.environ, {"LOCALAPPDATA": ""}), patch.object(Path, "home", return_value=self.root):
            paths = resolve_runtime_paths(self.root / "site-packages")
        self.assertEqual(paths.data_dir, self.root / "AppData" / "Local" / "WindowsLinkManager")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_check_missing_default_config_is_read_only(self):
        paths = RuntimePaths(self.root / "first-run", True)
        error = io.StringIO()
        with patch.object(entrypoint, "resolve_runtime_paths", return_value=paths), redirect_stderr(error):
            code = entrypoint.main(["--check"])
        self.assertEqual(code, 1)
        self.assertIn("配置无效", error.getvalue())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_check_custom_config_does_not_create_default_data(self):
        config = self.root / "检查配置.txt"
        config.write_text("# 一个空配置\n", encoding="utf-8")
        before = config.read_bytes()
        paths = RuntimePaths(self.root / "app-data", False)
        output = io.StringIO()
        with patch.object(entrypoint, "resolve_runtime_paths", return_value=paths), redirect_stdout(output):
            code = entrypoint.main(["--check", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertIn("0 条链接", output.getvalue())
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [config])

    def test_version_uses_package_version_without_creating_data(self):
        output = io.StringIO()
        with patch.object(entrypoint, "resolve_runtime_paths", return_value=RuntimePaths(self.root / "data", False)), redirect_stdout(output), self.assertRaises(SystemExit) as ended:
            entrypoint.main(["--version"])
        self.assertEqual(ended.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"Windows Link Manager {__version__}")
        self.assertEqual(list(self.root.iterdir()), [])


class RuntimeStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="linkmanager-first-run-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = RuntimePaths(self.root, True)

    def test_first_run_opens_empty_editable_draft_without_writing_config(self):
        from PySide6.QtWidgets import QApplication, QMessageBox
        from linkmanager.ui import MainWindow
        windows = []

        def open_window(*args):
            window = MainWindow(*args)
            windows.append(window)
            return window

        def event_loop():
            deadline = time.monotonic() + 5
            while windows[0].busy and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            self.assertFalse(windows[0].busy)
            return 0

        try:
            with patch.object(entrypoint, "resolve_runtime_paths", return_value=self.paths), patch("linkmanager.ui.MainWindow", side_effect=open_window), patch.object(QApplication, "exec", side_effect=event_loop), patch.object(QMessageBox, "critical") as failed:
                self.assertEqual(entrypoint.main([]), 0)
                failed.assert_not_called()
            window = windows[0]
            self.assertEqual(window.config_path, self.paths.config)
            self.assertEqual(window.specs, [])
            self.assertEqual(window.current_text(), "")
            self.assertFalse(window.dirty)
            self.assertTrue(window.add_button.isEnabled())
            self.assertFalse(window.preview_button.isEnabled())
            self.assertFalse(self.paths.config.exists())
            self.assertTrue(self.paths.state.is_dir())
            self.assertFalse((self.paths.state / "gui.lock").exists())
        finally:
            for window in windows:
                window.dirty = False
                window.close()
                window.deleteLater()
            self.app.processEvents()

    def test_unwritable_portable_state_reports_actionable_error_without_relocation(self):
        from PySide6.QtWidgets import QApplication, QMessageBox
        with patch.object(entrypoint, "resolve_runtime_paths", return_value=self.paths), patch("linkmanager.core.LinkService", side_effect=PermissionError("TEMP denied")), patch.object(QMessageBox, "critical") as failed, patch.object(QMessageBox, "information") as notice, patch.object(QApplication, "exec") as loop, redirect_stderr(io.StringIO()):
            code = entrypoint.main([])
        self.assertEqual(code, 1)
        message = failed.call_args.args[2]
        self.assertIn(str(self.root), message)
        self.assertIn("可写的位置", message)
        self.assertIn("保留软链接.txt和.state", message)
        notice.assert_not_called()
        loop.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_lock_permission_failure_is_not_reported_as_existing_window(self):
        from PySide6.QtCore import QLockFile
        from PySide6.QtWidgets import QApplication, QMessageBox
        with patch.object(entrypoint, "resolve_runtime_paths", return_value=self.paths), patch("PySide6.QtCore.QLockFile") as lock_type, patch.object(QMessageBox, "critical") as failed, patch.object(QMessageBox, "information") as notice, patch.object(QApplication, "exec") as loop, redirect_stderr(io.StringIO()):
            lock_type.LockError.LockFailedError = QLockFile.LockError.LockFailedError
            lock_type.return_value.tryLock.return_value = False
            lock_type.return_value.error.return_value = QLockFile.LockError.PermissionError
            code = entrypoint.main([])
        self.assertEqual(code, 1)
        self.assertIn("PermissionError", failed.call_args.args[2])
        notice.assert_not_called()
        loop.assert_not_called()
        lock_type.return_value.unlock.assert_not_called()
        self.assertFalse(self.paths.config.exists())


if __name__ == "__main__":
    unittest.main()
