from __future__ import annotations

from pathlib import Path
from datetime import datetime

from PySide6.QtCore import QEvent, Qt, QUrl, QSize, QTimer
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QAbstractTextDocumentLayout, QPalette, QTextDocument, QTextOption
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from .config import LinkSpec, parse_config, read_config_text, save_config, serialize_config
from .draft import ConfigDraft
from .models import PlanAction, can_recover, can_undo, record_needs_attention
from .paths import normalize_path
from .tasks import TaskRunner
from .theme import theme_color

KINDS = {
    "junction": "目录交接点 · Junction",
    "dir_symlink": "目录符号链接",
    "file_symlink": "文件符号链接",
    "hardlink": "文件硬链接",
}
TIPS = {
    "junction": "适合把应用数据目录映射到其他磁盘。源必须是已有文件夹。",
    "dir_symlink": "适合目录映射。Windows 可能需要开发者模式或管理员权限。",
    "file_symlink": "适合单个文件映射。Windows 可能需要开发者模式或管理员权限。",
    "hardlink": "两个文件入口共享同一份内容，必须在同一个卷。仅删除入口不会删除另一入口。",
}
SHORT_KINDS = {"junction": "交接点", "dir_symlink": "目录符号链接",
               "file_symlink": "文件符号链接", "hardlink": "硬链接"}


class DesktopItemDelegate(QStyledItemDelegate):
    def __init__(self, parent=None, row_lines=False):
        super().__init__(parent)
        self.row_lines = row_lines

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.state &= ~QStyle.StateFlag.State_HasFocus

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if self.row_lines:
            painter.save()
            color = theme_color("border")
            color.setAlpha(110)
            painter.setPen(color)
            painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
            painter.restore()


class WrappedPathDelegate(QStyledItemDelegate):
    """Confirmation tables never elide a path, even without whitespace."""
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.textElideMode = Qt.TextElideMode.ElideNone

    def document(self, option, index):
        document = QTextDocument()
        document.setDefaultFont(option.font)
        document.setDocumentMargin(0)
        document.setPlainText(index.data() or "")
        text_option = document.defaultTextOption()
        text_option.setWrapMode(QTextOption.WrapMode.WrapAnywhere)
        document.setDefaultTextOption(text_option)
        width = max(80, self.parent().columnWidth(index.column()) - 14)
        document.setTextWidth(width)
        return document, width

    def sizeHint(self, option, index):
        document, width = self.document(option, index)
        return QSize(width + 14, max(32, int(document.size().height()) + 10))

    def paint(self, painter, option, index):
        cell_option = QStyleOptionViewItem(option)
        self.initStyleOption(cell_option, index)
        document, _ = self.document(cell_option, index)
        cell_option.text = ""
        style = cell_option.widget.style() if cell_option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, cell_option, painter, cell_option.widget)
        painter.save()
        painter.translate(option.rect.left() + 5, option.rect.top() + 3)
        painter.setClipRect(0, 0, option.rect.width() - 10, option.rect.height() - 6)
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette = cell_option.palette
        if cell_option.state & QStyle.StateFlag.State_Selected:
            context.palette.setColor(QPalette.ColorRole.Text, cell_option.palette.color(QPalette.ColorRole.HighlightedText))
        document.documentLayout().draw(painter, context)
        painter.restore()


def confirmation_table(table):
    table.setWordWrap(True)
    table.setTextElideMode(Qt.TextElideMode.ElideNone)
    table.setItemDelegate(WrappedPathDelegate(table))
    table.horizontalHeader().sectionResized.connect(lambda *_: table.resizeRowsToContents())
    table.resizeRowsToContents()


def local_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)


def label(text, name=None, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text, callback, name=None):
    widget = QPushButton(text)
    if name:
        widget.setObjectName(name)
    widget.clicked.connect(callback)
    return widget


def tool_button(text, callback, icon=None):
    widget = QToolButton()
    widget.setText(text)
    widget.setAutoRaise(True)
    widget.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    widget.setIconSize(QSize(14, 14))
    if icon is not None:
        widget.setIcon(widget.style().standardIcon(icon))
    widget.clicked.connect(callback)
    return widget


class EntryDialog(QDialog):
    def __init__(self, parent=None, spec=None, projects=()):
        super().__init__(parent)
        self.setWindowTitle("编辑链接配置" if spec else "新增链接配置")
        self.resize(660, 310)
        layout = QVBoxLayout(self)
        layout.addWidget(label("源路径是数据所在的位置；目标路径是应用访问的链接入口。", "muted", True))
        form = QFormLayout()
        self.project = QComboBox()
        self.project.setEditable(True)
        self.project.addItems(list(projects))
        self.project.setCurrentText(spec.project if spec else (next(iter(projects), "新项目")))
        self.kind = QComboBox()
        for key, name in KINDS.items():
            self.kind.addItem(name, key)
        self.kind.setCurrentIndex(self.kind.findData(spec.kind if spec else "junction"))
        self.source = QLineEdit(spec.source if spec else "")
        self.target = QLineEdit(spec.target if spec else "")
        self.source.setPlaceholderText(r"例如 H:\Data\MyApp")
        self.target.setPlaceholderText(r"例如 C:\Users\%USERNAME%\AppData\Local\MyApp")
        form.addRow("所属项目", self.project)
        form.addRow("链接类型", self.kind)
        for text, field, callback in (("源 · 实际数据", self.source, self.browse_source), ("目标 · 链接入口", self.target, self.browse_target)):
            row = QHBoxLayout()
            row.addWidget(field)
            row.addWidget(button("浏览…", callback))
            form.addRow(text, row)
        layout.addLayout(form)
        self.tip = label("", "muted", True)
        self.kind.currentIndexChanged.connect(self.update_tip)
        self.update_tip()
        layout.addWidget(self.tip)
        self.error = label("", "error", True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存到待编辑配置")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def update_tip(self):
        self.tip.setText(TIPS[self.kind.currentData()])

    def browse_source(self):
        if self.kind.currentData() in ("file_symlink", "hardlink"):
            path, _ = QFileDialog.getOpenFileName(self, "选择实际数据文件", self.source.text())
        else:
            path = QFileDialog.getExistingDirectory(self, "选择实际数据文件夹", self.source.text())
        if path:
            self.source.setText(str(Path(path)))

    def browse_target(self):
        path = QFileDialog.getExistingDirectory(self, "选择链接入口的父文件夹")
        if path:
            name = Path(self.target.text() or self.source.text()).name or "新链接"
            self.target.setText(str(Path(path) / name))

    def spec(self):
        return LinkSpec(self.project.currentText().strip(), self.source.text().strip(), self.target.text().strip(), self.kind.currentData())

    def validate(self):
        try:
            spec = self.spec()
            if not spec.project or not spec.source or not spec.target:
                raise ValueError("请填写项目名、源路径和目标路径。")
            if parse_config(serialize_config([spec])) != [spec]:
                raise ValueError("项目名或路径与文本配置格式冲突，请检查输入。")
        except Exception as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class ConfigEditor(QDialog):
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑原始配置")
        self.resize(790, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(label("原始配置", "section"))
        layout.addWidget(label("【项目名称】分组；每对第一行源路径、第二行目标路径。非交接点可在配对前加 # @type=file_symlink 等类型标记。", "muted", True))
        self.editor = QPlainTextEdit(text)
        layout.addWidget(self.editor, 1)
        self.feedback = label("采用后只更新界面中的草稿；主界面点击“保存配置”才写入文件。", "muted", True)
        layout.addWidget(self.feedback)
        row = QHBoxLayout()
        row.addWidget(button("校验语法", self.validate))
        row.addStretch()
        row.addWidget(button("取消", self.reject))
        row.addWidget(button("采用此配置", self.apply, "primary"))
        layout.addLayout(row)

    def validate(self):
        try:
            specs = parse_config(self.editor.toPlainText())
            self.feedback.setText(f"语法有效：{len(specs)} 条链接配置。路径状态会在预览时检查。")
            return True
        except Exception as exc:
            self.feedback.setText(str(exc))
            return False

    def apply(self):
        if self.validate():
            self.accept()


class PreviewDialog(QDialog):
    def __init__(self, plan, parent=None, demo=False):
        super().__init__(parent)
        self.setWindowTitle("执行前预览")
        self.resize(920, 530)
        layout = QVBoxLayout(self)
        valid = bool(plan) and all(item.get("valid", False) for item in plan)
        destructive = any(item.get("delete_existing", False) for item in plan)
        layout.addWidget(label("检查本次变更", "section"))
        layout.addWidget(label("逐项列出链接入口、实际数据与备份位置。执行前会重新检查状态，避免覆盖预览后出现的新内容。", "muted", True))
        self.table = QTableWidget(len(plan), 4)
        self.table.setHorizontalHeaderLabels(["项目 / 类型", "计划操作", "目标 · 链接入口", "源 / 迁移 / 备份 / 校验信息"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(1, 140)
        self.table.verticalHeader().hide()
        for row, item in enumerate(plan):
            spec = item["spec"]
            detail = str(item.get("error") or ("源：" + item.get("source", spec.source)))
            if item.get("backup"):
                detail += "\n备份：" + str(item["backup"])
            elif item.get("action_code") == PlanAction.MOVE_DATA:
                detail += "\n迁移：" + item.get("target", spec.target) + " → " + item.get("source", spec.source)
                entries = item.get("move_manifest", {}).get("target_entries")
                if entries is not None:
                    files = sum(entry.get("kind") == "file" for entry in entries.values())
                    folders = sum(path != "." and entry.get("kind") == "directory" for path, entry in entries.items())
                    detail += f"\n预检内容：{files} 个文件、{folders} 个子目录。"
                detail += "\n先合并目标内容，再创建交接点；同名文件不覆盖。"
                detail += "\n撤销时移回本次迁入的内容，源目录原有内容保留。"
                if demo:
                    detail += "\n演示仅模拟迁移，不检查或移动真实文件。"
            elif item.get("action_code") == PlanAction.DELETE_DATA:
                detail += "\n不备份；普通目标内容永久删除，无法恢复。"
            elif item.get("action_code") == PlanAction.DELETE_LINK:
                detail += "\n仅移除链接入口后重建，源数据保留。"
            elif item.get("action_code") == PlanAction.CREATE:
                detail += "\n目标不存在，创建链接入口。"
            values = [spec.project + "\n" + KINDS.get(spec.kind, spec.kind), item.get("action", ""), item.get("target", spec.target), detail]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                if not item.get("valid") or (item.get("action_code") == PlanAction.DELETE_DATA and col in (1, 3)):
                    cell.setForeground(theme_color("error"))
                self.table.setItem(row, col, cell)
        confirmation_table(self.table)
        layout.addWidget(self.table, 1)
        self.consent = QCheckBox("我已检查路径，确认按以上计划创建或替换链接。" if not demo else "确认模拟此计划（演示模式不操作真实链接）。")
        layout.addWidget(self.consent)
        self.delete_consent = QCheckBox("我确认永久删除列出的目标及其内容，不备份，已删除的数据不能撤销恢复。")
        self.delete_consent.setObjectName("danger")
        self.delete_consent.setVisible(destructive)
        if destructive:
            layout.addWidget(label("原版功能：先永久删除目标，再创建所选链接。已有目录内的链接只移除入口，源数据保留；普通目标数据不会进入回收站。", "error", True))
        layout.addWidget(self.delete_consent)
        if not valid:
            layout.addWidget(label("存在校验问题，本批次不会执行。请修正红色条目后重新预览。", "error", True))
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(button("返回修改", self.reject))
        self.run_button = button("确认并执行" if not demo else "模拟执行", self.accept, "primary")
        self.run_button.setEnabled(False)
        def update_consent():
            self.run_button.setEnabled(self.consent.isChecked() and valid and (not destructive or self.delete_consent.isChecked()))
        self.consent.toggled.connect(update_consent)
        self.delete_consent.toggled.connect(update_consent)
        buttons.addWidget(self.run_button)
        layout.addLayout(buttons)


class HistoryDialog(QDialog):
    def __init__(self, records, parent=None):
        super().__init__(parent)
        self.records = records
        self.undo_id = None
        self.recovery_id = None
        self.setWindowTitle("操作历史与恢复")
        self.resize(900, 540)
        layout = QVBoxLayout(self)
        layout.addWidget(label("操作历史", "section"))
        layout.addWidget(label("创建记录可尝试撤销：先核对记录，再移除入口并恢复备份或移回本次迁入的内容。发生外部变化时会停止。", "muted", True))
        self.table = QTableWidget(len(records), 4)
        self.table.setHorizontalHeaderLabels(["时间", "操作", "结果", "说明"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 190)
        self.table.setColumnWidth(1, 110)
        for row, record in enumerate(records):
            success = record.get("success")
            state = ("已撤销" if record.get("undone") else "已恢复" if record.get("recovered")
                     else "待核对" if record_needs_attention(record) else "完成" if success else "未完成 / 失败")
            action = {"create": "创建链接", "unlink": "移除链接", "undo": "撤销创建", "recover": "恢复中断"}.get(record.get("action"), record.get("action", ""))
            values = [local_time(record.get("time", "")), action, state, record.get("message", "")]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        layout.addWidget(self.table, 1)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(130)
        layout.addWidget(self.detail)
        self.table.itemSelectionChanged.connect(self.show_detail)
        row = QHBoxLayout()
        row.addWidget(label("历史仅用于核对已记录的操作；备份文件可从对应路径手动查看。", "muted", True), 1)
        self.undo_button = button("预检并撤销选中记录", self.select_undo, "danger")
        self.undo_button.setEnabled(False)
        row.addWidget(self.undo_button)
        self.recovery_button = button("检查并恢复未完成记录", self.select_recovery, "primary")
        self.recovery_button.setEnabled(False)
        row.addWidget(self.recovery_button)
        row.addWidget(button("关闭", self.reject))
        layout.addLayout(row)

    def show_detail(self):
        row = self.table.currentRow()
        self.undo_button.setEnabled(row >= 0 and can_undo(self.records[row]))
        self.recovery_button.setEnabled(row >= 0 and can_recover(self.records[row]))
        if row >= 0:
            record = self.records[row]
            details = [str(record.get("message", ""))]
            if record.get("irreversible"):
                details.append("此记录包含永久删除，普通目标数据无法恢复，不支持撤销。")
            elif record_needs_attention(record):
                details.append("此记录需要核对。先检查恢复计划，再确认恢复；路径发生外部变化时会停止。")
            for item in record.get("results", []):
                details.append(str(item.get("target", "")) + "\n" + str(item.get("message", "")))
            self.detail.setPlainText("\n\n".join(details))

    def select_undo(self):
        row = self.table.currentRow()
        if row < 0:
            return
        if QMessageBox.question(self, "确认撤销", "将核对所选创建记录，恢复备份或移回本次迁入的内容。链接、备份或迁移内容已被外部修改时会拒绝。继续？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.undo_id = self.records[row]["id"]
            self.accept()

    def select_recovery(self):
        row = self.table.currentRow()
        if row >= 0 and can_recover(self.records[row]):
            self.recovery_id = self.records[row]["id"]
            self.accept()


class RecoveryDialog(QDialog):
    """Read-only recovery plan; acceptance authorizes only this reviewed plan."""
    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.setWindowTitle("恢复前核对")
        self.resize(820, 440)
        layout = QVBoxLayout(self)
        layout.addWidget(label("检查未完成操作的恢复计划", "section"))
        layout.addWidget(label(str(result.get("message", "")), "muted", True))
        table = self.table = QTableWidget(len(result.get("results", [])), 2)
        table.setHorizontalHeaderLabels(["目标 · 链接入口", "核对与恢复计划"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().hide()
        for column in range(2):
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for row, item in enumerate(result.get("results", [])):
            for column, key in enumerate(("target", "message")):
                cell = QTableWidgetItem(str(item.get(key, "")))
                cell.setToolTip(cell.text())
                table.setItem(row, column, cell)
        confirmation_table(table)
        layout.addWidget(table, 1)
        layout.addWidget(label("确认后会再次检查链接、备份和迁移内容，发生外部变化时拒绝恢复。永久删除的数据不能恢复。", "muted", True))
        self.consent = QCheckBox("我已核对以上路径及计划，确认执行恢复。")
        layout.addWidget(self.consent)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("返回", self.reject))
        self.run_button = button("确认恢复", self.accept, "primary")
        self.run_button.setEnabled(False)
        self.consent.toggled.connect(lambda checked: self.run_button.setEnabled(checked and bool(result.get("success"))))
        actions.addWidget(self.run_button)
        layout.addLayout(actions)


class MainWindow(QMainWindow):
    # Existing UI callbacks can use these names while the draft owns all
    # editable state. No second copy of configuration state lives in Qt.
    @property
    def specs(self):
        return self.draft.specs

    @specs.setter
    def specs(self, value):
        self.draft.specs = value

    @property
    def raw_text(self):
        return self.draft.raw_text

    @raw_text.setter
    def raw_text(self, value):
        self.draft.raw_text = value

    @property
    def dirty(self):
        return self.draft.dirty

    @dirty.setter
    def dirty(self, value):
        self.draft.dirty = value

    @property
    def _raw_draft(self):
        return self.draft.raw_draft

    @_raw_draft.setter
    def _raw_draft(self, value):
        self.draft.raw_draft = value

    @property
    def busy(self):
        return self.tasks.busy

    def __init__(self, service, config_path: Path, demo=False):
        super().__init__()
        self.service = service
        self.config_path = Path(config_path)
        self.demo = demo
        self.draft = ConfigDraft.from_text(read_config_text(self.config_path) if self.config_path.exists() else "")
        self.current_project = None
        self.checked = set()
        self.statuses = {}
        self.visible_indices = []
        self.tasks = TaskRunner(self)
        self.tasks.started.connect(self.task_started)
        self.tasks.finished.connect(self.task_finished)
        self.tasks.failed.connect(self.task_failed)
        self.mutating_controls = []
        self.mutating_actions = []
        self._selected_identity = None
        self.setWindowTitle("链接管理器 V2")
        self.resize(1120, 720)
        self.setMinimumSize(900, 560)
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(168)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(0, 12, 0, 8)
        side.setSpacing(6)
        side_heading = label("项目", "muted")
        side_heading.setContentsMargins(14, 0, 0, 5)
        side.addWidget(side_heading)
        self.projects = QListWidget()
        self.projects.setObjectName("projects")
        self.projects.setItemDelegate(DesktopItemDelegate(self.projects))
        self.projects.currentItemChanged.connect(self.change_project)
        side.addWidget(self.projects, 1)
        self.mode_label = label("演示 · 仅模拟" if demo else "本地配置", "muted")
        self.mode_label.setContentsMargins(14, 0, 8, 4)
        side.addWidget(self.mode_label)
        root.addWidget(self.sidebar)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        toolbar_frame = QFrame()
        toolbar_frame.setObjectName("toolbar")
        toolbar = QHBoxLayout(toolbar_frame)
        toolbar.setContentsMargins(10, 7, 10, 7)
        toolbar.setSpacing(5)
        self.add_button = tool_button("＋ 新增", self.add_entry)
        self.edit_button = tool_button("编辑", self.edit_entry)
        self.delete_button = tool_button("删配置", self.delete_entry)
        self.delete_button.setToolTip("删除选中的配置记录；不操作磁盘")
        self.save_button = tool_button("保存", self.save, QStyle.StandardPixmap.SP_DialogSaveButton)
        self.save_button.setEnabled(False)
        for control in (self.add_button, self.edit_button, self.delete_button, self.save_button):
            toolbar.addWidget(control)
            self.mutating_controls.append(control)
        toolbar.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索项目或路径  Ctrl+F")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("搜索项目或路径")
        self.search.setMinimumWidth(150)
        self.search.setMaximumWidth(270)
        self.search.textChanged.connect(self.populate_table)
        toolbar.addWidget(self.search, 1)
        history_button = tool_button("历史", self.show_history)
        history_button.setToolTip("操作历史与恢复")
        self.mutating_controls.append(history_button)
        toolbar.addWidget(history_button)
        body.addWidget(toolbar_frame)
        context_frame = QWidget()
        context = QHBoxLayout(context_frame)
        context.setContentsMargins(12, 6, 10, 6)
        context.setSpacing(5)
        self.title = label("全部链接", "title")
        context.addWidget(self.title)
        context.addStretch(1)
        self.count_labels = []
        for text in ("条配置", "条正确", "条待检查"):
            count = label("—", "number")
            self.count_labels.append(count)
            context.addWidget(count)
            context.addWidget(label(text, "muted"))
            context.addSpacing(7)
        body.addWidget(context_frame)
        self.pending_banner = label("检测到中断或未完整恢复的操作。请在操作历史中核对记录及备份位置。", "error", True)
        self.pending_banner.setContentsMargins(12, 4, 12, 6)
        self.pending_banner.hide()
        body.addWidget(self.pending_banner)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", "项目", "类型", "源 · 实际数据", "目标 · 链接入口", "状态"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setItemDelegate(DesktopItemDelegate(self.table, row_lines=True))
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(28)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        for column, width in enumerate((30, 94, 98, 256, 256, 106)):
            self.table.setColumnWidth(column, width)
        self.table.itemChanged.connect(self.checked_changed)
        self.table.itemSelectionChanged.connect(self.remember_selection)
        self.table.itemDoubleClicked.connect(lambda _: self.edit_entry())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.table_context_menu)
        self.table.viewport().installEventFilter(self)
        list_area = QWidget()
        list_layout = QVBoxLayout(list_area)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(0)
        list_layout.addWidget(self.table, 1)
        selection_row = QWidget()
        selection = QHBoxLayout(selection_row)
        selection.setContentsMargins(12, 4, 10, 4)
        self.selection_info = label("", "muted")
        selection.addWidget(self.selection_info, 1)
        self.select_button = QToolButton()
        self.select_button.setText("勾选…")
        self.select_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        selection_menu = QMenu(self.select_button)
        selection_menu.addAction("勾选当前列表", lambda: self.select_visible(True))
        selection_menu.addAction("清空全部勾选", lambda: self.select_visible(False))
        self.select_button.setMenu(selection_menu)
        selection.addWidget(self.select_button)
        list_layout.addWidget(selection_row)
        detail_panel = QFrame()
        self.detail_panel = detail_panel
        detail_panel.setObjectName("inspector")
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(10, 5, 10, 7)
        detail_layout.setSpacing(4)
        detail_top = QHBoxLayout()
        detail_top.setSpacing(5)
        self.detail_toggle = QToolButton()
        self.detail_toggle.setAutoRaise(True)
        self.detail_toggle.setText("详情")
        self.detail_toggle.setCheckable(True)
        self.detail_toggle.setChecked(True)
        self.detail_toggle.setArrowType(Qt.ArrowType.DownArrow)
        self.detail_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        detail_top.addWidget(self.detail_toggle)
        self.detail_title = label("路径详情", "muted")
        detail_top.addWidget(self.detail_title, 1)
        self.path_buttons = [tool_button("打开源", lambda: self.open_selected(False), QStyle.StandardPixmap.SP_DirOpenIcon),
                             tool_button("目标位置", lambda: self.open_selected(True)),
                             tool_button("复制路径", self.copy_paths)]
        for path_button in self.path_buttons:
            detail_top.addWidget(path_button)
        detail_layout.addLayout(detail_top)
        self.detail_text = label("选中记录查看完整路径。", "muted", True)
        self.detail_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.detail_text.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.detail_text.setAccessibleName("选中链接的完整路径和检查说明")
        self.detail_text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.detail_body = QScrollArea()
        self.detail_body.setFrameShape(QFrame.Shape.NoFrame)
        self.detail_body.setWidgetResizable(True)
        self.detail_body.setMinimumHeight(70)
        self.detail_body.setWidget(self.detail_text)
        detail_layout.addWidget(self.detail_body)
        self.detail_toggle.toggled.connect(self.toggle_detail)
        self.inspector_splitter = QSplitter(Qt.Orientation.Vertical)
        self.inspector_splitter.setChildrenCollapsible(False)
        self.inspector_splitter.setHandleWidth(4)
        self.inspector_splitter.addWidget(list_area)
        self.inspector_splitter.addWidget(detail_panel)
        self.inspector_splitter.setStretchFactor(0, 1)
        self.inspector_splitter.setStretchFactor(1, 0)
        self.inspector_splitter.handle(1).setToolTip("拖动调整路径详情的高度")
        self._expanded_detail_height = 124
        self._detail_initialized = False
        body.addWidget(self.inspector_splitter, 1)
        # Keep the semantic checkbox interface for existing callers and safety
        # logic; the visible control offers one mutually exclusive policy.
        self.backup = QCheckBox("备份后替换", self)
        self.delete_existing = QCheckBox("永久删除后创建", self)
        self.move_existing = QCheckBox("移入源目录后创建交接点", self)
        self.backup.hide()
        self.delete_existing.hide()
        self.move_existing.hide()
        batch_frame = QFrame()
        batch_frame.setObjectName("batchbar")
        batch = QHBoxLayout(batch_frame)
        batch.setContentsMargins(10, 8, 10, 8)
        batch.setSpacing(6)
        batch.addWidget(label("目标策略", "muted"))
        self.policy = QComboBox()
        self.policy.addItems(["保护普通目标", "备份后替换", "永久删除后创建", "移入源目录后创建交接点"])
        self.policy.setMinimumWidth(155)
        self.policy.setToolTip("保护普通目标、备份后替换、永久删除，或先将目标内容移入源目录再创建交接点。")
        self.policy.currentIndexChanged.connect(self.set_policy)
        self.backup.toggled.connect(self.sync_policy)
        self.delete_existing.toggled.connect(self.sync_policy)
        self.move_existing.toggled.connect(self.sync_policy)
        batch.addWidget(self.policy)
        self.mutating_controls.extend((self.backup, self.delete_existing, self.move_existing, self.policy))
        batch.addStretch(1)
        self.refresh_button = button("刷新", self.refresh)
        batch.addWidget(self.refresh_button)
        self.unlink_button = button("仅移除链接", self.unlink_selected)
        batch.addWidget(self.unlink_button)
        self.preview_button = button("预览并创建", self.preview_selected, "primary")
        batch.addWidget(self.preview_button)
        body.addWidget(batch_frame)
        self.feedback = label("", "muted")
        self.feedback.setMaximumWidth(580)
        self.feedback.setToolTip("操作状态")
        self.statusBar().addPermanentWidget(self.feedback, 1)
        self.statusBar().setSizeGripEnabled(True)
        root.addWidget(content, 1)
        self.content = content
        self.setup_menu()
        self.populate_projects()
        self.update_dirty(False)
        self.refresh(initial=True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "table"):
            QTimer.singleShot(0, self.resize_path_columns)

    def eventFilter(self, watched, event):
        if watched is self.table.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.resize_path_columns)
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._detail_initialized:
            self._detail_initialized = True
            QTimer.singleShot(0, self.initialize_detail_size)

    def initialize_detail_size(self):
        self.inspector_splitter.setSizes([max(1, self.inspector_splitter.height() - 128), 124])
        if not self.detail_toggle.isChecked():
            self.toggle_detail(False)

    def resize_path_columns(self):
        # Keep the user's relative source/target widths as the window changes;
        # all metadata columns remain visible at the minimum desktop size.
        available = self.table.viewport().width()
        fixed = sum(self.table.columnWidth(column) for column in (0, 1, 2, 5))
        remaining = max(160, available - fixed)
        current = self.table.columnWidth(3) + self.table.columnWidth(4)
        ratio = self.table.columnWidth(3) / current if current else .5
        source_width = max(80, min(remaining - 80, round(remaining * ratio)))
        self.table.setColumnWidth(3, source_width)
        self.table.setColumnWidth(4, remaining - source_width)

    def setup_menu(self):
        menu = self.menuBar().addMenu("配置")
        self.save_action = QAction("保存配置", self)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_action.triggered.connect(self.save)
        menu.addAction(self.save_action)
        for text, handler in (("打开其他配置…", self.import_config), ("导出当前配置…", self.export_config), ("编辑原始配置…", self.edit_raw)):
            action = menu.addAction(text)
            action.triggered.connect(handler)
            if handler != self.export_config:
                self.mutating_actions.append(action)
        menu.addSeparator()
        menu.addAction("打开项目文件夹", self.open_project)
        edit = self.menuBar().addMenu("编辑")
        for text, handler, shortcut in (("新增配置…", self.add_entry, "Ctrl+N"),
                                         ("编辑选中配置…", self.edit_entry, "Ctrl+E"),
                                         ("删除选中配置…", self.delete_entry, "Delete")):
            action = edit.addAction(text, handler)
            action.setShortcut(QKeySequence(shortcut))
            self.mutating_actions.append(action)
        edit.addSeparator()
        edit.addAction("复制选中路径", self.copy_paths).setShortcut(QKeySequence("Ctrl+Shift+C"))
        view = self.menuBar().addMenu("查看")
        view.addAction("搜索", lambda: self.search.setFocus()).setShortcut(QKeySequence("Ctrl+F"))
        view.addAction("刷新状态", self.refresh).setShortcut(QKeySequence("F5"))
        view.addAction("显示 / 收起详情", lambda: self.detail_toggle.setChecked(not self.detail_toggle.isChecked()))
        history = view.addAction("操作历史与恢复", self.show_history)
        self.mutating_actions.append(history)
        self.menuBar().addMenu("帮助").addAction("使用帮助", self.show_help)

    def set_policy(self, index):
        for choice, control in enumerate((self.backup, self.delete_existing, self.move_existing), 1):
            control.blockSignals(True)
            control.setChecked(index == choice)
            control.blockSignals(False)
        self.sync_policy()

    def sync_policy(self, *_):
        controls = (self.backup, self.delete_existing, self.move_existing)
        selected = self.sender()
        if selected in controls and selected.isChecked():
            for control in controls:
                if control is not selected:
                    control.blockSignals(True)
                    control.setChecked(False)
                    control.blockSignals(False)
        index = next((choice for choice, control in enumerate(controls, 1) if control.isChecked()), 0)
        self.policy.blockSignals(True)
        self.policy.setCurrentIndex(index)
        self.policy.blockSignals(False)
        self.policy.setToolTip("将普通目标目录的内容移入源目录，再创建交接点；递归合并目录，同名文件或类型冲突会阻止整批执行。" if index == 3 else
                               "永久删除普通目标及其全部内容，不备份且不可恢复；执行前须再次确认。" if index == 2 else
                               "同目录重命名保留普通目标，预览显示备份路径。" if index == 1 else
                               "拒绝覆盖普通文件或文件夹；仅创建或修正链接入口。")

    def table_context_menu(self, position):
        item = self.table.itemAt(position)
        if item is None:
            return
        self.table.selectRow(item.row())
        index = self.selected_index()
        if index is None:
            return
        menu = QMenu(self.table)
        menu.addAction("取消勾选" if index in self.checked else "勾选此记录", lambda: self.toggle_checked(index))
        menu.addSeparator()
        menu.addAction("编辑配置…", self.edit_entry).setEnabled(not self.busy)
        menu.addAction("删除配置…", self.delete_entry).setEnabled(not self.busy)
        menu.addSeparator()
        menu.addAction("复制源路径", lambda: self.copy_path(False))
        menu.addAction("复制目标路径", lambda: self.copy_path(True))
        menu.addAction("复制两条路径", self.copy_paths)
        menu.addSeparator()
        menu.addAction("打开源位置", lambda: self.open_selected(False))
        menu.addAction("打开目标位置", lambda: self.open_selected(True))
        menu.exec(self.table.viewport().mapToGlobal(position))

    def toggle_checked(self, index):
        if index in self.checked:
            self.checked.discard(index)
        else:
            self.checked.add(index)
        self.populate_table()

    def copy_path(self, target):
        index = self.selected_index()
        if index is not None:
            spec = self.specs[index]
            QApplication.clipboard().setText(spec.target if target else spec.source)
            self.feedback.setText("已复制目标路径。" if target else "已复制源路径。")

    def update_dirty(self, value=True):
        self.dirty = value
        self.save_action.setEnabled(value and not self.busy)
        self.save_button.setEnabled(value and not self.busy)
        self.setWindowTitle("链接管理器 V2" + (" · 配置未保存" if value else ""))
        self.statusBar().showMessage(self.config_path.name + (" · 未保存" if value else ""))
        self.statusBar().setToolTip(str(self.config_path))

    def populate_projects(self):
        self.projects.blockSignals(True)
        self.projects.clear()
        all_item = QListWidgetItem(f"全部链接    {len(self.specs)}")
        all_item.setData(Qt.ItemDataRole.UserRole, None)
        self.projects.addItem(all_item)
        names = list(dict.fromkeys(spec.project for spec in self.specs))
        active = 0
        for row, name in enumerate(names, 1):
            item = QListWidgetItem(name + "    " + str(sum(spec.project == name for spec in self.specs)))
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.projects.addItem(item)
            if name == self.current_project:
                active = row
        self.projects.setCurrentRow(active)
        self.projects.blockSignals(False)
        self.current_project = self.projects.currentItem().data(Qt.ItemDataRole.UserRole)
        self.title.setText(self.current_project or "全部链接")
        self.populate_table()

    def change_project(self, current, previous=None):
        self.current_project = current.data(Qt.ItemDataRole.UserRole) if current else None
        self.title.setText(self.current_project or "全部链接")
        self.populate_table()

    def populate_table(self):
        identity = self._selected_identity
        previous_scroll = self.table.verticalScrollBar().value()
        query = self.search.text().casefold()
        self.visible_indices = [i for i, spec in enumerate(self.specs) if (self.current_project is None or spec.project == self.current_project) and (not query or query in (spec.project + spec.source + spec.target + KINDS.get(spec.kind, "")).casefold())]
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.visible_indices))
        for row, index in enumerate(self.visible_indices):
            spec = self.specs[index]
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            check.setCheckState(Qt.CheckState.Checked if index in self.checked else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check)
            state = self.statuses.get(index, {})
            values = [spec.project, SHORT_KINDS.get(spec.kind, spec.kind), spec.source, spec.target, state.get("status", "待检查")]
            for col, value in enumerate(values, 1):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value) if col != 5 else str(state.get("detail", value)))
                if col == 5:
                    item.setForeground(theme_color("good" if self.is_healthy(state) else "warning"))
                self.table.setItem(row, col, item)
            self.table.setRowHeight(row, 30)
        self.table.blockSignals(False)
        self.update_selection_info()
        total = len(self.specs)
        healthy = sum(self.is_healthy(state) for state in self.statuses.values())
        for widget, count in zip(self.count_labels, (total, healthy, total - healthy)):
            widget.setText(str(count))
        if self.table.rowCount():
            selected_row = next((row for row, index in enumerate(self.visible_indices)
                                 if self.specs[index] == identity), 0)
            self.table.selectRow(selected_row)
            self.table.verticalScrollBar().setValue(previous_scroll)
            # Keep the identity while it is hidden by a temporary filter.
            if identity is not None and identity not in [self.specs[i] for i in self.visible_indices]:
                self._selected_identity = identity
        self.show_detail()

    @staticmethod
    def is_healthy(state):
        return bool(state.get("correct", False))

    def checked_changed(self, item):
        if item.column() != 0:
            return
        index = self.visible_indices[item.row()]
        if item.checkState() == Qt.CheckState.Checked:
            self.checked.add(index)
        else:
            self.checked.discard(index)
        self.update_selection_info()

    def update_selection_info(self):
        self.selection_info.setText(f"显示 {len(self.visible_indices)} / {len(self.specs)} 条 · 已勾选 {len(self.checked)} 条（跨项目保留）")
        self.preview_button.setEnabled(bool(self.checked) and not self.busy)
        self.unlink_button.setEnabled(bool(self.checked) and not self.busy)

    def select_visible(self, checked):
        if checked:
            self.checked.update(self.visible_indices)
        else:
            self.checked.clear()
        self.populate_table()

    def selected_index(self):
        row = self.table.currentRow()
        return self.visible_indices[row] if 0 <= row < len(self.visible_indices) else None

    def show_detail(self):
        index = self.selected_index()
        self.edit_button.setEnabled(index is not None and not self.busy)
        self.delete_button.setEnabled(index is not None and not self.busy)
        for path_button in self.path_buttons:
            path_button.setEnabled(index is not None)
        if index is None:
            self.detail_title.setText("路径详情")
            self.detail_text.setText("没有匹配的记录。可新增链接配置，或调整项目与搜索条件。")
            return
        spec = self.specs[index]
        state = self.statuses.get(index, {})
        self.detail_title.setText(spec.project + " · " + KINDS.get(spec.kind, spec.kind))
        lines = ["源：" + spec.source, "目标：" + spec.target]
        for key, original, title in (("resolved_source", spec.source, "实际源路径："),
                                     ("resolved_target", spec.target, "实际目标路径：")):
            resolved = state.get(key)
            if resolved and resolved != original:
                lines.append(title + str(resolved))
        if state.get("actual_target"):
            lines.append("当前指向：" + str(state["actual_target"]))
        lines.append(str(state.get("detail") or TIPS.get(spec.kind, "")))
        self.detail_text.setText("\n".join(lines))

    def remember_selection(self):
        index = self.selected_index()
        if index is not None:
            self._selected_identity = self.specs[index]
        self.show_detail()

    def toggle_detail(self, expanded):
        if not expanded and self.detail_body.isVisible():
            self._expanded_detail_height = self.inspector_splitter.sizes()[1]
        self.detail_body.setVisible(expanded)
        if expanded:
            self.detail_panel.setMaximumHeight(16777215)
            height = self._expanded_detail_height
        else:
            height = self.detail_panel.minimumSizeHint().height()
            self.detail_panel.setMaximumHeight(height)
        self.inspector_splitter.setSizes([max(1, self.inspector_splitter.height() - height - 4), height])
        self.detail_toggle.setText("详情")
        self.detail_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def draft_changed(self):
        self.draft.structured_changed()
        self.checked.clear()
        self.statuses.clear()
        self.update_dirty()
        self.populate_projects()
        self.refresh()

    def add_entry(self):
        if self.busy:
            return
        dialog = EntryDialog(self, projects=dict.fromkeys(spec.project for spec in self.specs))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.specs.append(dialog.spec())
            self.draft_changed()

    def edit_entry(self):
        if self.busy:
            return
        index = self.selected_index()
        if index is None:
            return
        dialog = EntryDialog(self, self.specs[index], dict.fromkeys(spec.project for spec in self.specs))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.specs[index] = dialog.spec()
            self.draft_changed()

    def delete_entry(self):
        if self.busy:
            return
        index = self.selected_index()
        if index is not None and QMessageBox.question(self, "删除配置记录", "从配置草稿中删除这条记录？此操作只编辑配置，不操作磁盘上的链接或数据。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.specs.pop(index)
            self.draft_changed()

    def edit_raw(self):
        if self.busy:
            return
        dialog = ConfigEditor(self.current_text(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.draft.adopt_text(dialog.editor.toPlainText())
            self.checked.clear()
            self.statuses.clear()
            self.update_dirty()
            self.populate_projects()
            self.refresh()

    def current_text(self):
        return self.draft.text()

    def save(self):
        if self.busy:
            return False
        try:
            text = self.current_text()
            save_config(self.config_path, text)
            self.draft.saved(text)
            self.update_dirty(False)
            self.feedback.setText("配置已保存；旧配置已备份。")
            return True
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return False

    def confirm_dirty(self):
        if not self.dirty:
            return True
        choice = QMessageBox.question(self, "配置尚未保存", "是否保存当前配置修改？", QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save)
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        return self.save() if choice == QMessageBox.StandardButton.Save else True

    def import_config(self):
        if self.busy:
            return
        if not self.confirm_dirty():
            return
        path, _ = QFileDialog.getOpenFileName(self, "打开链接配置", str(self.config_path.parent), "文本配置 (*.txt);;所有文件 (*)")
        if not path:
            return
        try:
            text = read_config_text(Path(path))
            draft = ConfigDraft.from_text(text)
            if not self.demo:
                self.config_path = Path(path)
                if hasattr(self.service, "protected_paths"):
                    self.service.protected_paths.append(self.config_path.absolute())
            self.draft = draft
            self.checked.clear()
            self.statuses.clear()
            self.current_project = None
            self.update_dirty(self.demo)
            if self.demo:
                self._raw_draft = True
            self.populate_projects()
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", str(exc))

    def export_config(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出当前配置", str(self.config_path.with_name("软链接-导出.txt")), "文本配置 (*.txt)")
        if path:
            try:
                save_config(Path(path), self.current_text())
                self.feedback.setText("已导出配置：" + path)
            except Exception as exc:
                QMessageBox.critical(self, "导出失败", str(exc))

    def start_task(self, operation, callback, message, *, read_only=False):
        return self.tasks.start(operation, callback, message, read_only=read_only)

    def task_started(self, read_only, message):
        for control in self.mutating_controls:
            control.setEnabled(False)
        for action in self.mutating_actions:
            action.setEnabled(False)
        self.save_action.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.feedback.setText(message)
        self.update_selection_info()
        # A status read cannot modify the draft. Browsing, filtering, selecting,
        # and copying remain available while the immutable snapshot is checked.
        self.show_detail()

    def task_finished(self):
        for control in self.mutating_controls:
            control.setEnabled(True)
        for action in self.mutating_actions:
            action.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.update_dirty(self.dirty)
        self.show_detail()
        self.update_selection_info()

    def task_failed(self, message):
        self.feedback.setText("操作失败：" + message)
        QMessageBox.critical(self, "操作失败", message)

    def collect_status(self, specs=None):
        results = {}
        for index, spec in enumerate(tuple(self.specs) if specs is None else specs):
            try:
                results[index] = self.service.inspect(spec)
            except Exception as exc:
                results[index] = {"status": "检查失败", "correct": False, "detail": str(exc)}
        return results

    def refresh(self, initial=False):
        specs = tuple(self.specs)
        def read_view():
            statuses = self.collect_status(specs)
            try:
                records = self.service.history()
            except Exception as exc:
                records = exc
            return statuses, records
        def updated(data):
            statuses, records = data
            self.statuses = statuses
            self.populate_table()
            self.feedback.setText("演示模式：操作仅用于模拟。" if self.demo else "已读取链接状态。勾选记录后可预览本次变更。")
            self.update_pending(records)
        self.start_task(read_view, updated, "正在读取链接状态…", read_only=True)

    def selected_specs(self):
        return [self.specs[index] for index in sorted(self.checked) if index < len(self.specs)]

    def preview_selected(self):
        specs = self.selected_specs()
        if not specs:
            return
        backup = self.backup.isChecked()
        delete_existing = self.delete_existing.isChecked()
        move_existing = self.move_existing.isChecked()
        def ready(plan):
            dialog = PreviewDialog(plan, self, self.demo)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.start_task(lambda: (self.service.execute(plan), self.collect_status()), self.operation_done, "正在创建链接并验证结果…")
        self.start_task(lambda: self.service.preview(specs, backup_existing=backup, delete_existing=delete_existing, move_existing=move_existing), ready, "正在校验所选路径与变更计划…", read_only=True)

    def unlink_selected(self):
        specs = self.selected_specs()
        if not specs:
            return
        paths = "\n".join(spec.target for spec in specs)
        dialog = QMessageBox(QMessageBox.Icon.Warning, "确认仅移除链接", f"将检查并移除以下 {len(specs)} 个链接入口：\n\n{paths}\n\n源数据保留。普通文件或文件夹会被拒绝。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        if dialog.exec() == QMessageBox.StandardButton.Yes:
            self.start_task(lambda: (self.service.unlink(specs), self.collect_status()), self.operation_done, "正在检查并移除链接入口…")

    def operation_done(self, data):
        result, statuses = data
        self.statuses = statuses
        self.populate_table()
        self.feedback.setText(str(result.get("message", "操作结束")))
        self.update_pending()
        text = str(result.get("message", ""))
        details = "\n\n".join(str(row.get("target", "")) + "\n" + str(row.get("message", "")) for row in result.get("results", []))
        box = QMessageBox(QMessageBox.Icon.Information if result.get("success") else QMessageBox.Icon.Warning, "操作结果", text, QMessageBox.StandardButton.Ok, self)
        box.setDetailedText(details)
        box.exec()

    def update_pending(self, records=None):
        try:
            if isinstance(records, Exception):
                raise records
            if records is None:
                records = self.service.history()
            unfinished = any(record_needs_attention(record) for record in records)
            self.pending_banner.setText("检测到中断或未完整恢复的操作。请在“操作历史与恢复”中核对记录及备份位置。")
            self.pending_banner.setVisible(unfinished)
        except Exception as exc:
            self.pending_banner.setText("无法读取操作历史：" + str(exc))
            self.pending_banner.show()

    def show_history(self):
        if self.busy:
            return
        def ready(records):
            dialog = HistoryDialog(sorted(records, key=lambda record: str(record.get("time", "")), reverse=True), self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if dialog.undo_id:
                record_id = dialog.undo_id
                self.start_task(lambda: (self.service.undo(record_id), self.collect_status()), self.operation_done, "正在核对历史并撤销…")
            elif dialog.recovery_id:
                self.preview_recovery(dialog.recovery_id)
        self.start_task(self.service.history, ready, "正在读取操作历史…", read_only=True)

    def preview_recovery(self, record_id):
        def ready(result):
            dialog = RecoveryDialog(result, self)
            if result.get("success") and dialog.exec() == QDialog.DialogCode.Accepted:
                self.start_task(lambda: (self.service.recover(record_id), self.collect_status()),
                                self.operation_done, "正在重新核对路径并恢复…")
            elif not result.get("success"):
                # Failed plans still remain reviewable, with confirmation disabled.
                dialog.exec()
        self.start_task(lambda: self.service.preview_recovery(record_id), ready,
                        "正在检查恢复路径与备份…", read_only=True)

    def open_project(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(__file__).resolve().parent.parent)))

    def open_selected(self, target):
        index = self.selected_index()
        if index is None:
            return
        spec = self.specs[index]
        try:
            path = normalize_path(spec.target if target else spec.source)
        except ValueError as exc:
            QMessageBox.information(self, "路径不可用", str(exc))
            return
        if target or not path.is_dir():
            path = path.parent
        if path.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(self, "路径不可用", "文件夹不存在：" + str(path))

    def copy_paths(self):
        index = self.selected_index()
        if index is not None:
            spec = self.specs[index]
            QApplication.clipboard().setText("源：" + spec.source + "\n目标：" + spec.target)
            self.feedback.setText("已复制源路径与目标路径。")

    def show_help(self):
        QMessageBox.information(self, "链接管理器使用帮助", "1. 按项目管理链接配置，勾选需要处理的记录。\n2. 源是已有数据位置，目标是应用访问的链接入口。\n3. 点击“预览并创建”，检查路径后确认执行。\n4. 不勾选替换选项时保护普通数据；可选“备份后替换”“永久删除后创建”，或“移入源目录后创建交接点”（仅 Junction；同名文件不覆盖）。\n5. 原版删除功能：先永久删除目标及其内容，再创建链接，不备份，不可恢复；已有正确链接也会移除后重建。\n6. “仅移除链接”保留源数据。含永久删除的创建记录不能撤销恢复普通数据。\n7. 编辑配置不会立即操作磁盘；Ctrl+S 保存配置。\n\n类型：目录交接点、目录符号链接、文件符号链接、文件硬链接。\n符号链接可能需要开发者模式或管理员权限；硬链接必须同卷。")

    def closeEvent(self, event):
        if self.busy:
            QMessageBox.information(self, "操作进行中", "请等待当前操作完成后关闭窗口。")
            event.ignore()
        elif self.confirm_dirty():
            event.accept()
        else:
            event.ignore()
