"""Small Qt helpers shared by the main layout and the settings dialog."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QLabel, QPushButton

ASSETS = Path(__file__).parent / "assets"


def load_stylesheet() -> str:
    """Use absolute asset paths so startup works from any folder."""
    stylesheet = Path(__file__).with_name("styles.qss").read_text(encoding="utf-8")
    return stylesheet.replace("@ASSETS@", ASSETS.as_posix())


def app_icon() -> QIcon:
    return QIcon(str(ASSETS / "app-icon.svg"))


def label(text: str, style: str = "", wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    if style:
        widget.setObjectName(style)
    return widget


def button(text, callback, style="", tooltip=""):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    if style:
        widget.setObjectName(style)
    widget.setToolTip(tooltip)
    return widget
