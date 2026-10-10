"""Shared colors, with system dark/high contrast support."""
from pathlib import Path
import os
import re
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

COLORS = {
    "light": {
        "text": "#25282E", "muted": "#606770", "background": "#F5F5F6",
        "surface": "#FFFFFF", "alternate": "#FAFBFC", "sidebar": "#F0F1F3", "border": "#D6D9DE",
        "accent": "#2563B9", "accent_hover": "#1D5199", "on_accent": "#FFFFFF",
        "selection": "#DFEAF9", "hover": "#EDF1F6", "error": "#B13232",
        "warning": "#845A1C", "good": "#28674A", "disabled": "#727880",
    },
    "dark": {
        "text": "#E8EAEE", "muted": "#B6BBC4", "background": "#24262B",
        "surface": "#292C32", "alternate": "#2D3036", "sidebar": "#22252B", "border": "#484E59",
        "accent": "#76A9EF", "accent_hover": "#94BCF3", "on_accent": "#132740",
        "selection": "#344969", "hover": "#333B47", "error": "#FFA8A4",
        "warning": "#EBC38B", "good": "#A1D1B7", "disabled": "#A0A6B0",
    },
}

STYLE = '''
QWidget { color: @text; font-size: 9pt; }
QMainWindow, QDialog { background: @background; }
QFrame#sidebar { background: @sidebar; border-right: 1px solid @border; }
QFrame#toolbar { border-bottom: 1px solid @border; background: @background; }
QFrame#inspector, QFrame#batchbar { border-top: 1px solid @border; background: @background; }
QLabel#title, QLabel#section { font-size: 9pt; font-weight: 600; }
QLabel#number { color: @text; font-weight: 600; }
QLabel#muted, QLabel#subtitle { color: @muted; }
QLabel#error, QCheckBox#danger { color: @error; }
QLabel#good { color: @good; }
QLabel#badge { color: @muted; padding: 0 4px; }
QLineEdit, QPlainTextEdit, QComboBox, QListWidget {
 background: @surface; border: 1px solid @border; border-radius: 3px;
 padding: 3px 6px; selection-background-color: @selection; selection-color: @text;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border: 1px solid @accent; }
QPushButton, QToolButton { background: @surface; border: 1px solid @border; border-radius: 3px; padding: 3px 9px; min-height: 18px; }
QPushButton:hover, QToolButton:hover { background: @hover; }
QPushButton:pressed, QToolButton:pressed { background: @selection; }
QPushButton:focus, QToolButton:focus { border: 1px solid @accent; }
QPushButton:disabled, QToolButton:disabled { color: @disabled; background: @background; }
QPushButton#primary { background: @accent; color: @on_accent; border-color: @accent; }
QPushButton#primary:hover { background: @accent_hover; }
QPushButton#primary:disabled { background: @background; color: @disabled; border-color: @border; }
QPushButton#danger { color: @error; }
QListWidget#projects { background: transparent; border: none; padding: 0; }
QListWidget#projects::item { padding: 5px 9px; border-radius: 3px; margin: 0 5px; }
QListWidget#projects::item:selected { background: @selection; color: @text; }
QTableWidget { background: @surface; alternate-background-color: @alternate; border: none; gridline-color: @border; selection-background-color: @selection; selection-color: @text; }
QToolButton[autoRaise="true"] { border-color: transparent; background: transparent; padding: 3px 7px; }
QToolButton[autoRaise="true"]:hover { background: @hover; border-color: @border; }
QToolButton[autoRaise="true"]:focus { border-color: @accent; }
QToolButton[autoRaise="true"]:pressed { background: @selection; }
QHeaderView::section { background: @background; border: none; border-right: 1px solid @border; border-bottom: 1px solid @border; padding: 5px 7px; color: @muted; font-weight: 400; }
QCheckBox { spacing: 5px; }
QCheckBox::indicator { width: 14px; height: 14px; }
QScrollBar:vertical { background: @background; width: 10px; }
QScrollBar::handle:vertical { background: @border; border-radius: 3px; min-height: 30px; }
QToolTip { background: @surface; color: @text; border: 1px solid @border; padding: 6px; }
QStatusBar { color: @muted; }
QMenuBar { background: @background; border-bottom: 1px solid @border; }
QMenuBar::item { padding: 3px 8px; }
QMenuBar::item:selected, QMenu::item:selected { background: @selection; }
QMenu { background: @surface; border: 1px solid @border; padding: 3px; }
QMenu::item { padding: 5px 22px; }
QMenu::separator { background: @border; height: 1px; margin: 3px 5px; }
'''


def _system_high_contrast():
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    class HighContrast(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD),
                    ("lpszDefaultScheme", wintypes.LPWSTR)]
    state = HighContrast()
    state.cbSize = ctypes.sizeof(state)
    return bool(ctypes.windll.user32.SystemParametersInfoW(0x42, state.cbSize, ctypes.byref(state), 0) and state.dwFlags & 1)


def theme_color(name):
    app = QApplication.instance()
    colors = app.property("linkmanager_colors") if app else None
    return QColor((colors or COLORS["light"])[name])


def apply_theme(app, mode=None):
    for name in ("msyh.ttc", "simhei.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if path.exists() and QFontDatabase.addApplicationFont(str(path)) >= 0:
            break
    if not app.property("linkmanager_system_palette"):
        app.setProperty("linkmanager_system_palette", app.palette())
    app.setFont(QFont("Microsoft YaHei UI", 9))
    if mode == "high_contrast" or (mode is None and _system_high_contrast()):
        app.setStyleSheet("")
        app.setStyle("windows")
        # When the OS owns the contrast theme, preserve its actual palette.
        # Explicit preview/test mode uses a deterministic monochrome palette.
        palette = QPalette(app.property("linkmanager_system_palette"))
        if mode == "high_contrast":
            for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base,
                         QPalette.ColorRole.Button, QPalette.ColorRole.AlternateBase):
                palette.setColor(role, QColor("#FFFFFF"))
            for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                         QPalette.ColorRole.ButtonText):
                palette.setColor(role, QColor("#000000"))
            palette.setColor(QPalette.ColorRole.Highlight, QColor("#000000"))
            palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
        app.setPalette(palette)
        colors = {name: palette.color(QPalette.ColorRole.Text).name() for name in COLORS["light"]}
        colors.update(background=palette.color(QPalette.ColorRole.Window).name(),
                      surface=palette.color(QPalette.ColorRole.Base).name(),
                      sidebar=palette.color(QPalette.ColorRole.Window).name(),
                      selection=palette.color(QPalette.ColorRole.Base).name(),
                      hover=palette.color(QPalette.ColorRole.Base).name(),
                      on_accent=palette.color(QPalette.ColorRole.HighlightedText).name())
        app.setProperty("linkmanager_colors", colors)
        return
    mode = mode or ("dark" if app.styleHints().colorScheme() == Qt.ColorScheme.Dark else "light")
    colors = COLORS[mode]
    app.setProperty("linkmanager_colors", colors)
    app.setStyle("Fusion")
    palette = app.palette()
    for role, token in ((QPalette.ColorRole.Window, "background"), (QPalette.ColorRole.Base, "surface"),
                        (QPalette.ColorRole.WindowText, "text"), (QPalette.ColorRole.Text, "text"),
                        (QPalette.ColorRole.Button, "surface"), (QPalette.ColorRole.ButtonText, "text"),
                        (QPalette.ColorRole.Highlight, "selection"), (QPalette.ColorRole.HighlightedText, "text")):
        palette.setColor(role, QColor(colors[token]))
    app.setPalette(palette)
    app.setStyleSheet(re.sub(r"@(\w+)", lambda match: colors[match.group(1)], STYLE))
