from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from linkmanager.config import LinkSpec, parse_config
from linkmanager.core import LinkService
from linkmanager.demo import DEMO_TEXT, DemoService
from linkmanager.models import PlanAction
from linkmanager.theme import apply_theme, theme_color
from linkmanager.ui import ConfigEditor, EntryDialog, HistoryDialog, MainWindow, PreviewDialog, RecoveryDialog


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="linkmanager-ui-")
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "配置 含空格.txt"
        self.config.write_text(DEMO_TEXT, encoding="utf-8")
        self.service = DemoService()
        self.window = MainWindow(self.service, self.config, True)
        self.addCleanup(self.dispose)
        self.wait_task()

    def dispose(self):
        self.wait_task()
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def wait_task(self):
        deadline = time.monotonic() + 5
        while self.window.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertFalse(self.window.busy, "后台任务未正常结束")
        self.app.processEvents()

    def show_real_inspection(self, spec):
        """Render one state inspected by LinkService, using paths in this test's TEMP tree."""
        self.wait_task()
        service = LinkService(Path(self.temp.name) / "real-service-state", protected_paths=[Path(self.temp.name)])
        state = service.inspect(spec)
        self.window.service = service
        self.window.specs = [spec]
        self.window.statuses = {0: state}
        self.window.checked.clear()
        self.window.current_project = None
        self.window.populate_projects()
        self.window.show_detail()
        return state, self.window.detail_text.text()

    def test_real_inspection_detail_shows_each_raw_path_once(self):
        source = Path(self.temp.name) / "ordinary source"
        source.mkdir()
        target = Path(self.temp.name) / "missing target"
        spec = LinkSpec("TEMP 检查", str(source), str(target), "junction")

        state, text = self.show_real_inspection(spec)

        self.assertEqual(state["target_kind"], "missing")
        lines = text.splitlines()
        self.assertEqual(sum(line == "源：" + spec.source for line in lines), 1, text)
        self.assertEqual(sum(line == "目标：" + spec.target for line in lines), 1, text)
        self.assertFalse(any(line.startswith("实际源路径：") for line in lines), text)
        self.assertFalse(any(line.startswith("实际目标路径：") for line in lines), text)

    def test_table_refresh_updates_detail_for_already_selected_first_row(self):
        self.wait_task()
        self.window.table.selectRow(0)
        self.assertEqual(self.window.selected_index(), 0)
        refreshed_detail = "刷新后状态说明"
        state = dict(self.window.statuses.get(0, {}))
        state["detail"] = refreshed_detail
        self.window.statuses[0] = state

        self.window.populate_table()

        self.assertEqual(self.window.selected_index(), 0)
        self.assertIn(refreshed_detail, self.window.detail_text.text())

    def test_refresh_and_temporary_filter_keep_current_record(self):
        self.window.table.selectRow(4)
        selected = self.window.specs[4]
        self.window.refresh()
        self.wait_task()
        self.assertEqual(self.window.specs[self.window.selected_index()], selected)
        self.window.search.setText(self.window.specs[0].source)
        self.assertNotIn(4, self.window.visible_indices)
        self.window.search.clear()
        self.assertEqual(self.window.specs[self.window.selected_index()], selected)

    def test_first_status_read_runs_in_background_and_does_not_block_browsing(self):
        entered, release = threading.Event(), threading.Event()
        service = DemoService()
        original = service.inspect
        calls = []
        def inspect(spec):
            calls.append(threading.get_ident())
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release inspection")
            return original(spec)
        service.inspect = inspect
        window = MainWindow(service, self.config, True)
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(window.busy)
            self.assertFalse(window.add_button.isEnabled())
            self.assertFalse(window.edit_button.isEnabled())
            self.assertFalse(window.backup.isEnabled())
            self.assertFalse(window.policy.isEnabled())
            self.assertFalse(window.save_action.isEnabled())
            self.assertTrue(window.search.isEnabled())
            self.assertTrue(window.projects.isEnabled())
            self.assertTrue(window.table.isEnabled())
            before = list(window.specs)
            with patch("linkmanager.ui.EntryDialog") as editor:
                window.edit_entry()
                window.add_entry()
                editor.assert_not_called()
            self.assertEqual(window.specs, before)
            window.search.setText("Roaming")
            self.assertEqual(window.table.rowCount(), 1)
            window.copy_paths()
            self.assertIn("Roaming", self.app.clipboard().text())
            self.assertNotEqual(calls[0], threading.get_ident())
        finally:
            release.set()
            deadline = time.monotonic() + 5
            while window.busy and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.005)
            self.assertFalse(window.busy)
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_quoted_source_opens_normalized_existing_directory(self):
        source = Path(self.temp.name) / "source with spaces"
        source.mkdir()
        self.window.specs = [LinkSpec("quoted", '"' + str(source) + '"', str(source / "target"), "junction")]
        self.window.populate_projects()
        with patch("linkmanager.ui.QDesktopServices.openUrl", return_value=True) as opened, patch.object(QMessageBox, "information") as notice:
            self.window.open_selected(False)
            self.assertEqual(opened.call_args.args[0].toLocalFile(), QUrl.fromLocalFile(str(source)).toLocalFile())
            notice.assert_not_called()

    def test_minimum_window_keeps_six_complete_rows_and_all_columns(self):
        self.window.resize(900, 560)
        self.window.show()
        for _ in range(4):
            self.app.processEvents()
        self.assertEqual((self.window.width(), self.window.height()), (900, 560))
        viewport = self.window.table.viewport().height()
        complete_rows = sum(self.window.table.rowHeight(row) for row in range(6))
        self.assertLessEqual(self.window.table.rowHeight(0), 32)
        self.assertGreaterEqual(viewport, complete_rows)
        header = self.window.table.horizontalHeader()
        self.assertLessEqual(header.sectionViewportPosition(5) + header.sectionSize(5), self.window.table.viewport().width())
        expanded_height = viewport
        self.window.detail_toggle.setChecked(False)
        for _ in range(3):
            self.app.processEvents()
        self.assertFalse(self.window.detail_body.isVisible())
        self.assertGreater(self.window.table.viewport().height(), expanded_height)

    def test_dense_desktop_list_shows_at_least_twelve_complete_rows(self):
        self.window.specs = [LinkSpec("列表", fr"H:\Data\Tool{i:02}", fr"C:\Workspace\Tool{i:02}", "junction") for i in range(40)]
        self.window.statuses.clear()
        self.window.populate_projects()
        self.window.resize(1080, 730)
        self.window.show()
        for _ in range(4):
            self.app.processEvents()
        table = self.window.table
        complete = sum(0 <= table.rowViewportPosition(row)
                       and table.rowViewportPosition(row) + table.rowHeight(row) <= table.viewport().height()
                       for row in range(table.rowCount()))
        self.assertGreaterEqual(complete, 12)

    def test_filter_scrollbar_change_keeps_status_column_visible(self):
        self.window.specs = [LinkSpec("列表", fr"H:\Data\Tool{i:02}", fr"C:\Workspace\Tool{i:02}", "junction") for i in range(40)]
        self.window.statuses.clear()
        self.window.populate_projects()
        self.window.search.setText("Tool00")
        self.window.resize(900, 560)
        self.window.show()
        for _ in range(4):
            self.app.processEvents()
        table = self.window.table
        self.assertEqual(table.rowCount(), 1)
        wide_viewport = table.viewport().width()
        self.window.search.clear()
        for _ in range(4):
            self.app.processEvents()
        self.assertEqual(table.rowCount(), 40)
        self.assertLess(table.viewport().width(), wide_viewport)
        self.assertLessEqual(sum(table.columnWidth(column) for column in range(6)), table.viewport().width())
        self.assertEqual(table.horizontalScrollBar().maximum(), 0)

    def test_path_inspector_can_resize_collapse_and_restore_height(self):
        self.window.show()
        for _ in range(4):
            self.app.processEvents()
        self.window.inspector_splitter.setSizes([200, 260])
        self.app.processEvents()
        expanded = self.window.inspector_splitter.sizes()[1]
        self.assertGreater(self.window.detail_body.height(), 160)
        self.window.detail_toggle.setChecked(False)
        self.app.processEvents()
        self.assertLess(self.window.inspector_splitter.sizes()[1], 60)
        self.window.detail_toggle.setChecked(True)
        self.app.processEvents()
        self.assertAlmostEqual(self.window.inspector_splitter.sizes()[1], expanded, delta=3)

    def test_state_logic_uses_machine_fields_and_not_localized_status_text(self):
        self.assertTrue(self.window.is_healthy({"correct": True, "status": "任意展示文字"}))
        self.assertFalse(self.window.is_healthy({"correct": False, "status": "链接正确"}))
        self.window.update_pending([{"state": "failed", "message": "回滚未完成", "recovery_required": False}])
        self.assertTrue(self.window.pending_banner.isHidden())
        self.window.update_pending([{"state": "failed", "message": "arbitrary", "recovery_required": True}])
        self.assertFalse(self.window.pending_banner.isHidden())

    def test_variable_paths_keep_raw_and_resolved_values_in_detail(self):
        root = Path(self.temp.name) / "expanded root"
        source = root / "source directory"
        source.mkdir(parents=True)
        target = root / "target directory"
        raw_root = "%LINKMANAGER_UI_TEST_ROOT%"
        spec = LinkSpec(
            "变量路径",
            raw_root + "\\source directory",
            raw_root + "\\target directory",
            "junction",
        )

        with patch.dict(os.environ, {"LINKMANAGER_UI_TEST_ROOT": str(root)}):
            state, text = self.show_real_inspection(spec)

        lines = text.splitlines()
        self.assertEqual(sum(line == "源：" + spec.source for line in lines), 1, text)
        self.assertEqual(sum(line == "目标：" + spec.target for line in lines), 1, text)
        self.assertEqual(sum(line == "实际源路径：" + str(source) for line in lines), 1, text)
        self.assertEqual(sum(line == "实际目标路径：" + str(target) for line in lines), 1, text)
        self.assertIn("%LINKMANAGER_UI_TEST_ROOT%", spec.source)
        self.assertIn("%LINKMANAGER_UI_TEST_ROOT%", spec.target)
        self.assertTrue(state["source_exists"])
        self.assertFalse(state["target_exists"])

    def test_inspection_error_detail_is_preserved_in_path_panel(self):
        spec = LinkSpec(
            "错误详情",
            "%LINKMANAGER_UI_TEST_UNDEFINED%\\source",
            str(Path(self.temp.name) / "error target"),
            "junction",
        )

        state, text = self.show_real_inspection(spec)

        self.assertIn("LINKMANAGER_UI_TEST_UNDEFINED", state["detail"])
        self.assertIn(state["detail"], text)
        self.assertEqual(sum(line == "源：" + spec.source for line in text.splitlines()), 1, text)
        self.assertEqual(sum(line == "目标：" + spec.target for line in text.splitlines()), 1, text)

    def test_search_and_grouping_keep_checked_records(self):
        self.window.select_visible(True)
        self.assertEqual(len(self.window.checked), 7)
        self.window.projects.setCurrentRow(1)
        self.assertEqual(self.window.table.rowCount(), 2)
        self.assertEqual(len(self.window.checked), 7)
        self.window.search.setText("Roaming")
        self.assertEqual(self.window.table.rowCount(), 1)
        self.window.select_visible(False)
        self.assertEqual(len(self.window.checked), 0)

    def test_structured_edit_invalidates_original_raw_draft(self):
        self.window.raw_text = DEMO_TEXT + "\n# 保留原始注释\n"
        self.window._raw_draft = True
        self.window.update_dirty()
        self.window.specs.pop()
        self.window.draft_changed()
        self.wait_task()
        self.assertFalse(self.window._raw_draft)
        self.assertTrue(self.window.save())
        self.assertEqual(len(parse_config(self.config.read_text(encoding="utf-8"))), 6)
        self.assertTrue(list((self.config.parent / ".backups").iterdir()))

    def test_raw_editor_retains_comments_until_explicit_save(self):
        text = DEMO_TEXT + "\n# 注释应保留\n"
        self.window.raw_text = text
        self.window.specs = parse_config(text)
        self.window._raw_draft = True
        self.window.update_dirty()
        self.assertEqual(self.window.current_text(), text)
        self.assertNotIn("注释应保留", self.config.read_text(encoding="utf-8"))
        self.assertTrue(self.window.save())
        self.assertIn("注释应保留", self.config.read_text(encoding="utf-8"))

    def test_preview_requires_confirmation_and_blocks_invalid_batch(self):
        plan = self.service.preview(self.window.specs, backup_existing=False)
        dialog = PreviewDialog(plan, self.window, True)
        dialog.consent.setChecked(True)
        self.assertFalse(dialog.run_button.isEnabled())
        dialog.deleteLater()
        valid = PreviewDialog(self.service.preview(self.window.specs, backup_existing=True), self.window, True)
        self.assertFalse(valid.run_button.isEnabled())
        valid.consent.setChecked(True)
        self.assertTrue(valid.run_button.isEnabled())
        valid.deleteLater()

    def test_backup_and_permanent_delete_are_mutually_exclusive(self):
        self.window.backup.setChecked(True)
        self.window.delete_existing.setChecked(True)
        self.assertFalse(self.window.backup.isChecked())
        self.assertTrue(self.window.delete_existing.isChecked())
        self.window.backup.setChecked(True)
        self.assertFalse(self.window.delete_existing.isChecked())
        self.assertEqual(self.window.policy.currentIndex(), 1)

    def test_visible_policy_forwards_each_choice_to_service(self):
        self.window.select_visible(True)
        for choice, flags in enumerate(((False, False, False), (True, False, False),
                                       (False, True, False), (False, False, True))):
            with self.subTest(choice=choice):
                self.window.policy.setCurrentIndex(choice)
                with patch.object(self.service, "preview", wraps=self.service.preview) as preview, patch.object(PreviewDialog, "exec", return_value=QDialog.DialogCode.Rejected):
                    self.window.preview_selected()
                    self.wait_task()
                self.assertEqual(preview.call_args.kwargs, {"backup_existing": flags[0], "delete_existing": flags[1], "move_existing": flags[2]})
                self.assertEqual((self.window.backup.isChecked(), self.window.delete_existing.isChecked(),
                                  self.window.move_existing.isChecked()), flags)
        self.assertFalse(self.service.history())

    def test_move_policy_is_exclusive_and_preview_explains_direction(self):
        self.window.backup.setChecked(True)
        self.window.move_existing.setChecked(True)
        self.assertFalse(self.window.backup.isChecked())
        self.assertFalse(self.window.delete_existing.isChecked())
        self.assertEqual(self.window.policy.currentIndex(), 3)
        spec = next(spec for spec in self.window.specs if 'Pictures' in spec.target)
        plan = self.service.preview([spec], move_existing=True)
        dialog = PreviewDialog(plan, self.window, True)
        self.addCleanup(dialog.deleteLater)
        detail = dialog.table.item(0, 3).text()
        self.assertIn(spec.target + ' → ' + spec.source, detail)
        self.assertIn('同名文件不覆盖', detail)
        self.assertIn('演示仅模拟迁移', detail)
        self.assertFalse(dialog.delete_consent.isVisible())
        dialog.consent.setChecked(True)
        self.assertTrue(dialog.run_button.isEnabled())

    def test_delete_preview_marks_only_actual_data_loss_as_danger(self):
        plan = self.service.preview(self.window.specs, delete_existing=True)
        dialog = PreviewDialog(plan, self.window, False)
        self.addCleanup(dialog.deleteLater)
        seen = set()
        for row, item in enumerate(plan):
            action = item["action_code"]
            seen.add(action)
            detail = dialog.table.item(row, 3).text()
            if action == PlanAction.DELETE_DATA:
                self.assertIn("永久删除", detail)
                self.assertEqual(dialog.table.item(row, 1).foreground().color(), theme_color("error"))
                self.assertEqual(dialog.table.item(row, 3).foreground().color(), theme_color("error"))
            else:
                self.assertNotIn("普通目标内容永久删除", detail)
                self.assertNotEqual(dialog.table.item(row, 1).foreground().color(), theme_color("error"))
            if action == PlanAction.DELETE_LINK:
                self.assertIn("源数据保留", detail)
            if action == PlanAction.CREATE:
                self.assertIn("目标不存在", detail)
        self.assertTrue({PlanAction.DELETE_DATA, PlanAction.DELETE_LINK, PlanAction.CREATE}.issubset(seen))
        dialog.consent.setChecked(True)
        self.assertFalse(dialog.run_button.isEnabled())

    def test_delete_mode_requires_explicit_data_loss_consent(self):
        plan = self.service.preview(self.window.specs, delete_existing=True)
        dialog = PreviewDialog(plan, self.window, False)
        dialog.consent.setChecked(True)
        self.assertFalse(dialog.run_button.isEnabled())
        dialog.delete_consent.setChecked(True)
        self.assertTrue(dialog.run_button.isEnabled())
        dialog.consent.setChecked(False)
        self.assertFalse(dialog.run_button.isEnabled())
        dialog.deleteLater()

    def test_gui_forwards_delete_mode_and_history_disallows_data_restore(self):
        self.window.select_visible(True)
        self.window.delete_existing.setChecked(True)
        with patch.object(PreviewDialog, "exec", return_value=QDialog.DialogCode.Accepted), patch.object(self.window, "operation_done") as done:
            self.window.preview_selected()
            self.wait_task()
            done.assert_called_once()
            result, _ = done.call_args.args[0]
            self.assertTrue(result["success"])
            self.assertTrue(result["irreversible"])
            self.assertIn("永久删除", result["message"])
        history = HistoryDialog(self.service.history(), self.window)
        history.table.selectRow(0)
        self.assertFalse(history.undo_button.isEnabled())
        self.assertIn("无法恢复", history.detail.toPlainText())
        history.deleteLater()

    def test_empty_config_is_editable_without_action_buttons(self):
        self.window.specs = []
        self.window.checked.clear()
        self.window.populate_projects()
        self.assertEqual(self.window.table.rowCount(), 0)
        self.assertFalse(self.window.preview_button.isEnabled())
        self.assertFalse(self.window.edit_button.isEnabled())

    def test_background_callback_can_start_next_task(self):
        outputs = []
        def first_done(value):
            outputs.append(value)
            self.window.start_task(lambda: "second", outputs.append, "second")
        self.window.start_task(lambda: "first", first_done, "first")
        self.wait_task()
        self.assertEqual(outputs, ["first", "second"])

    def test_failed_task_and_callback_release_thread_and_reenable_editor(self):
        def fail():
            raise ValueError("isolated failure")
        with patch.object(QMessageBox, "critical") as notice:
            self.window.start_task(fail, lambda _: self.fail("callback must not run"), "test")
            self.wait_task()
            self.assertIsNone(self.window.tasks.worker)
            self.assertTrue(self.window.add_button.isEnabled())
            self.assertIn("isolated failure", self.window.feedback.text())
            notice.assert_called_once()
            self.window.start_task(lambda: None, lambda _: fail(), "callback failure")
            self.wait_task()
            self.assertIsNone(self.window.tasks.worker)
            self.assertIn("无法显示结果", self.window.feedback.text())

    def test_close_during_task_retains_worker_until_finished(self):
        entered, release = threading.Event(), threading.Event()
        self.window.show()
        def pending():
            entered.set()
            return release.wait(3)
        try:
            self.window.start_task(pending, lambda _: None, "waiting", read_only=True)
            self.assertTrue(entered.wait(1))
            worker = self.window.tasks.worker
            with patch.object(QMessageBox, "information") as notice:
                self.assertFalse(self.window.close())
                notice.assert_called_once()
            self.assertIs(self.window.tasks.worker, worker)
            self.assertTrue(worker.isRunning())
        finally:
            release.set()
            self.wait_task()
        self.assertIsNone(self.window.tasks.worker)
        self.assertTrue(self.window.close())

    def test_recovery_previews_then_requires_confirmation_before_execution(self):
        calls = []
        def preview(record_id):
            calls.append(("preview", record_id))
            return {"success": True, "message": "reviewed", "results": [{"target": "TEMP", "message": "restore"}]}
        def recover(record_id):
            calls.append(("recover", record_id))
            return {"success": True, "message": "restored", "results": []}
        with patch.object(self.service, "preview_recovery", side_effect=preview), patch.object(self.service, "recover", side_effect=recover), patch.object(self.window, "operation_done") as done:
            with patch.object(RecoveryDialog, "exec", return_value=QDialog.DialogCode.Rejected):
                self.window.preview_recovery("record")
                self.wait_task()
                self.assertEqual(calls, [("preview", "record")])
                done.assert_not_called()
            with patch.object(RecoveryDialog, "exec", return_value=QDialog.DialogCode.Accepted):
                self.window.preview_recovery("record")
                self.wait_task()
                self.assertEqual(calls[-2:], [("preview", "record"), ("recover", "record")])
                done.assert_called_once()
        valid = RecoveryDialog({"success": True, "message": "review", "results": []}, self.window)
        self.assertFalse(valid.run_button.isEnabled())
        valid.consent.setChecked(True)
        self.assertTrue(valid.run_button.isEnabled())
        valid.deleteLater()

    def test_failed_recovery_plan_cannot_run_even_when_checked(self):
        result = {"success": False, "message": "changed path", "results": []}
        dialog = RecoveryDialog(result, self.window)
        dialog.consent.setChecked(True)
        self.assertFalse(dialog.run_button.isEnabled())
        dialog.deleteLater()
        with patch.object(self.service, "preview_recovery", return_value=result), patch.object(self.service, "recover") as recovered, patch.object(RecoveryDialog, "exec", return_value=QDialog.DialogCode.Accepted):
            self.window.preview_recovery("record")
            self.wait_task()
            recovered.assert_not_called()

    def test_history_routes_selected_recovery_to_preview(self):
        def choose_recovery(dialog):
            dialog.recovery_id = "pending-record"
            return QDialog.DialogCode.Accepted
        with patch.object(HistoryDialog, "exec", new=choose_recovery), patch.object(self.window, "preview_recovery") as preview:
            self.window.show_history()
            self.wait_task()
            preview.assert_called_once_with("pending-record")

    def test_light_dark_and_high_contrast_body_colors_are_readable(self):
        def luminance(value):
            def linear(channel):
                return channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
            return sum(weight * linear(channel) for weight, channel in zip((.2126, .7152, .0722), QColor(value).getRgbF()[:3]))
        def ratio(first, second):
            a, b = luminance(first), luminance(second)
            return (max(a, b) + .05) / (min(a, b) + .05)
        try:
            for mode in ("light", "dark", "high_contrast"):
                apply_theme(self.app, mode)
                colors = self.app.property("linkmanager_colors")
                for foreground in ("text", "muted", "good", "warning", "error"):
                    for background in ("background", "surface", "sidebar", "selection"):
                        with self.subTest(mode=mode, foreground=foreground, background=background):
                            self.assertGreaterEqual(ratio(colors[foreground], colors[background]), 4.5)
                if mode != "high_contrast":
                    self.assertGreaterEqual(ratio(colors["on_accent"], colors["accent"]), 4.5)
                    self.assertGreaterEqual(ratio(colors["on_accent"], colors["accent_hover"]), 4.5)
                    self.assertNotIn("_hover", self.app.styleSheet())
                    self.assertNotIn("@", self.app.styleSheet())
                else:
                    self.assertEqual(self.app.styleSheet(), "")
                    palette = self.app.palette()
                    self.assertGreaterEqual(ratio(palette.color(QPalette.ColorRole.Text).name(), palette.color(QPalette.ColorRole.Base).name()), 4.5)
        finally:
            apply_theme(self.app, "light")

    def test_preview_execution_reaches_memory_service_and_refreshes(self):
        self.window.select_visible(True)
        self.window.backup.setChecked(True)
        with patch.object(PreviewDialog, "exec", return_value=QDialog.DialogCode.Accepted), patch.object(self.window, "operation_done") as done:
            self.window.preview_selected()
            self.wait_task()
            done.assert_called_once()
            result, states = done.call_args.args[0]
            self.assertTrue(result["success"])
            self.assertEqual(len(states), 7)
            self.assertEqual(self.config.read_text(encoding="utf-8"), DEMO_TEXT)

    def test_config_editor_and_entry_validate_before_accepting(self):
        editor = ConfigEditor("【项目】\nC:\\only-one-path\n", self.window)
        self.assertFalse(editor.validate())
        editor.deleteLater()
        entry = EntryDialog(self.window)
        entry.source.setText("# 会被当作注释")
        entry.target.setText(r"C:\Data\Link")
        entry.validate()
        self.assertNotEqual(entry.result(), QDialog.DialogCode.Accepted)
        entry.deleteLater()

    def test_existing_instance_lock_blocks_second_formal_window(self):
        import importlib
        from PySide6.QtCore import QLockFile
        from PySide6.QtWidgets import QMessageBox
        entrypoint = importlib.import_module("linkmanager.main")
        from linkmanager.runtime import RuntimePaths
        state = Path(self.temp.name) / ".state"
        state.mkdir()
        lock = QLockFile(str(state / "gui.lock"))
        self.assertTrue(lock.tryLock(0))
        try:
            with patch.object(entrypoint, "resolve_runtime_paths", return_value=RuntimePaths(Path(self.temp.name), True)), patch.object(QMessageBox, "information") as notice, patch.object(QApplication, "exec") as loop:
                code = entrypoint.main(["--config", str(self.config)])
                self.assertEqual(code, 0)
                notice.assert_called_once()
                loop.assert_not_called()
            challenger = QLockFile(str(state / "gui.lock"))
            self.assertFalse(challenger.tryLock(0), "被拒绝的实例不能解除首个实例的锁")
        finally:
            lock.unlock()


if __name__ == "__main__":
    unittest.main()
