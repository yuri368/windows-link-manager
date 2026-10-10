from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile

from . import __version__
from .runtime import resolve_runtime_paths

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    runtime = resolve_runtime_paths(ROOT)
    parser = argparse.ArgumentParser(description="Windows 链接管理器 V2")
    parser.add_argument("--version", action="version", version=f"Windows Link Manager {__version__}")
    parser.add_argument("--config", type=Path, default=runtime.config)
    parser.add_argument("--demo", action="store_true", help="内存演示，不修改真实链接或原配置")
    parser.add_argument("--check", action="store_true", help="只检查配置语法")
    parser.add_argument("--screenshot", type=Path, help="演示窗口截图并退出")
    args = parser.parse_args(argv)
    if args.screenshot and (not args.demo or args.check):
        parser.error("--screenshot 需与 --demo 一起使用，不能与 --check 一起使用")
    from .config import load_config
    if args.check:
        try:
            specs = load_config(args.config)
            print(f"配置有效：{len(set(spec.project for spec in specs))} 个项目，{len(specs)} 条链接。")
            return 0
        except Exception as exc:
            print(f"配置无效：{exc}", file=sys.stderr)
            return 1
    if args.screenshot:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    from .theme import apply_theme
    from .ui import MainWindow
    app = QApplication.instance() or QApplication([sys.argv[0]])
    apply_theme(app)
    temporary = None
    instance_lock = None
    lock_acquired = False
    code = 0
    try:
        config = args.config.absolute()
        if args.demo:
            from .demo import DEMO_TEXT, DemoService
            temporary = tempfile.TemporaryDirectory(prefix="linkmanager-demo-")
            config = Path(temporary.name) / "演示配置.txt"
            config.write_text(DEMO_TEXT, encoding="utf-8")
            service = DemoService()
        else:
            from .core import LinkService
            try:
                service = LinkService(runtime.state, protected_paths=[ROOT, runtime.data_dir, config])
            except OSError as exc:
                raise OSError(runtime.write_error(exc)) from exc
            instance_lock = QLockFile(str(runtime.state / "gui.lock"))
            lock_acquired = instance_lock.tryLock(0)
            if not lock_acquired:
                if instance_lock.error() != QLockFile.LockError.LockFailedError:
                    raise OSError(runtime.write_error(f"无法创建实例锁：{instance_lock.error().name}"))
                QMessageBox.information(None, "链接管理器已运行", "链接管理器已经打开，请切换到现有窗口。每次只允许一个正式窗口管理链接与操作历史。")
                return 0
        window = MainWindow(service, config, args.demo)
        window.show()
        if args.screenshot:
            def capture():
                nonlocal code
                if window.busy:
                    QTimer.singleShot(50, capture)
                    return
                try:
                    output = args.screenshot.absolute()
                    output.parent.mkdir(parents=True, exist_ok=True)
                    if not window.grab().save(str(output)):
                        raise OSError("无法保存截图")
                    print(f"演示截图已保存：{output}")
                except Exception as exc:
                    code = 1
                    print(str(exc), file=sys.stderr)
                app.quit()
            QTimer.singleShot(350, capture)
        code = app.exec() or code
    except Exception as exc:
        if sys.stderr:
            print(str(exc), file=sys.stderr)
        QMessageBox.critical(None, "链接管理器启动失败", str(exc))
        code = 1
    finally:
        if temporary:
            temporary.cleanup()
        if instance_lock and lock_acquired:
            instance_lock.unlock()
    return code
