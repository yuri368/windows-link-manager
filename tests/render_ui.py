"""Render demonstration windows for visual inspection without desktop input."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
import json
import time
import argparse

from PySide6.QtWidgets import QApplication

from linkmanager.demo import DEMO_TEXT, DemoService
from linkmanager.config import LinkSpec, serialize_config
from linkmanager.paths import normalize_path, path_key
from linkmanager.demo import VirtualEntry
from linkmanager.theme import apply_theme
from linkmanager.ui import ConfigEditor, EntryDialog, HistoryDialog, MainWindow, PreviewDialog, RecoveryDialog


def settle(app):
    for _ in range(4):
        app.processEvents()


def wait_idle(app, window):
    deadline = time.monotonic() + 10
    while window.busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    if window.busy:
        raise TimeoutError("Rendering task did not complete")
    settle(app)


def save_widget(app, widget, destination):
    widget.show()
    settle(app)
    if not widget.grab().save(str(destination)):
        raise OSError(str(destination))


def table_metrics(window):
    viewport = window.table.viewport().height()
    height = window.table.rowHeight(0) if window.table.rowCount() else 0
    return {"size": [window.width(), window.height()], "viewport_height": viewport,
            "row_height": height, "row_capacity": viewport // height if height else 0,
            "complete_rows": sum(0 <= window.table.rowViewportPosition(row)
                                 and window.table.rowViewportPosition(row) + window.table.rowHeight(row) <= viewport
                                 for row in range(window.table.rowCount()))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).absolute().parents[1] / "artifacts" / "desktop-redesign")
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication([])
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=True)
    metrics = {}
    with tempfile.TemporaryDirectory(prefix="linkmanager-render-") as temp:
        config = Path(temp) / "演示配置.txt"
        config.write_text(DEMO_TEXT, encoding="utf-8")
        for mode in ("light", "dark", "high_contrast"):
            apply_theme(app, mode)
            service = DemoService()
            window = MainWindow(service, config, True)
            wait_idle(app, window)
            window.select_visible(True)
            save_widget(app, window, output / f"main-{mode}.png")
            metrics[mode] = {"default": table_metrics(window)}
            window.resize(1080, 730)
            save_widget(app, window, output / f"comparison-{mode}.png")
            metrics[mode]["comparison"] = table_metrics(window)
            window.resize(900, 560)
            save_widget(app, window, output / f"minimum-{mode}.png")
            metrics[mode]["minimum"] = table_metrics(window)
            assert window.width() <= 900 and window.height() <= 560, metrics[mode]
            assert metrics[mode]["minimum"]["complete_rows"] >= 6, metrics[mode]
            window.detail_toggle.setChecked(False)
            save_widget(app, window, output / f"minimum-collapsed-{mode}.png")
            metrics[mode]["collapsed"] = table_metrics(window)
            if mode == "light":
                plan = service.preview(window.specs, backup_existing=True)
                dialogs = [("preview.png", PreviewDialog(plan, window, True)),
                           ("editor.png", ConfigEditor(DEMO_TEXT, window)),
                           ("entry.png", EntryDialog(window, window.specs[-1], ("开发工具",)))]
                service.execute(plan)
                service.records[0].update(state="pending", recovery_required=True, success=False,
                                          message="演示中断记录：需要核对入口与备份。")
                dialogs.append(("history.png", HistoryDialog(service.history(), window)))
                dialogs.append(("recovery.png", RecoveryDialog(service.preview_recovery(service.records[0]["id"]), window)))
                for filename, dialog in dialogs:
                    if isinstance(dialog, (PreviewDialog, RecoveryDialog)):
                        dialog.consent.setChecked(True)
                    if isinstance(dialog, HistoryDialog) and dialog.table.rowCount():
                        dialog.table.selectRow(0)
                    save_widget(app, dialog, output / filename)
                    dialog.close()
                    dialog.deleteLater()
                destructive = PreviewDialog(DemoService().preview(window.specs, delete_existing=True), window, True)
                destructive.consent.setChecked(True)
                destructive.delete_consent.setChecked(True)
                save_widget(app, destructive, output / "delete-preview.png")
                destructive.close()
                destructive.deleteLater()
            window.close()
            window.deleteLater()
            settle(app)
        # More rows make information density measurable even when the normal
        # seven-entry demo fits completely. All states below remain in memory.
        apply_theme(app, "light")
        specs = []
        service = DemoService()
        for index in range(40):
            kind = ("junction", "dir_symlink", "file_symlink", "hardlink")[index % 4]
            leaf = f"Tool{index + 1:02d}" + (".ini" if kind in ("file_symlink", "hardlink") else "")
            source = str(Path(r"H:\Data\Development") / leaf)
            target = str(Path(r"H:\Workspace" if kind == "hardlink" else r"C:\Workspace") / leaf)
            spec = LinkSpec(("开发工具", "应用数据", "文档资料")[index % 3], source, target, kind)
            specs.append(spec)
            if index % 5 == 0:
                service.states[path_key(normalize_path(target))] = VirtualEntry(kind, source)
        config.write_text(serialize_config(specs), encoding="utf-8")
        dense = MainWindow(service, config, True)
        wait_idle(app, dense)
        for name, size in (("comparison", (1080, 730)), ("minimum", (900, 560))):
            dense.resize(*size)
            save_widget(app, dense, output / f"dense-{name}.png")
            metrics[f"dense_{name}"] = table_metrics(dense)
        dense.close()
        dense.deleteLater()
        settle(app)
        long_source = '"' + r"H:\Data\Projects\Research and Development\Very long configuration directory\Application Support\Shared resources\Local profiles\Current\settings.json" + '"'
        long_target = r"C:\Users\%USERNAME%\AppData\Local\Projects\Research and Development\Very long configuration directory\Application Support\Local profiles\settings.json"
        config.write_text(serialize_config([LinkSpec("长路径与变量", long_source, long_target, "file_symlink")]), encoding="utf-8")
        long_window = MainWindow(DemoService(), config, True)
        wait_idle(app, long_window)
        save_widget(app, long_window, output / "long-path.png")
        long_window.inspector_splitter.setSizes([180, 310])
        save_widget(app, long_window, output / "long-path-expanded.png")
        assert long_source in long_window.detail_text.text()
        assert long_target in long_window.detail_text.text()
        long_window.close()
        long_window.deleteLater()
        settle(app)
    (output / "layout-metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
