"""One background task at a time, with ownership lasting until QThread finishes."""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot


class Worker(QThread):
    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.output = None
        self.error = None

    def run(self):
        try:
            self.output = self.operation()
        except Exception as exc:
            self.error = str(exc)


class TaskRunner(QObject):
    started = Signal(bool, str)
    finished = Signal()
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.callback = None
        self.busy = False

    def start(self, operation, callback, message, *, read_only=False):
        if self.busy:
            return False
        self.busy = True
        self.callback = callback
        self.worker = Worker(operation, self)
        self.worker.finished.connect(self._complete)
        self.started.emit(read_only, message)
        self.worker.start()
        return True

    @Slot()
    def _complete(self):
        worker, callback = self.worker, self.callback
        output, error = worker.output, worker.error
        # The finished signal is delivered after run() returns. Release this task
        # before the callback so a preview can start its execution task safely.
        self.worker = self.callback = None
        self.busy = False
        worker.deleteLater()
        self.finished.emit()
        if error:
            self.failed.emit(error)
            return
        try:
            callback(output)
        except Exception as exc:
            self.failed.emit("无法显示结果：" + str(exc))
