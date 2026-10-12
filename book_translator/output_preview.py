"""Read-only, copyable preview of the same text used by file export."""

from .i18n import tr

from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QPlainTextEdit, QVBoxLayout

from .core import export_text
from .widgets import button, label


class OutputPreviewDialog(QDialog):
    def __init__(self, parent, book):
        super().__init__(parent)
        self.book = book
        self.setWindowTitle(tr('Final output preview'))
        self.setMinimumSize(560, 380)
        screen = self.screen().availableGeometry()
        self.resize(min(900, screen.width() - 40), min(700, screen.height() - 60))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(label(tr('Output preview'), "title"))
        header.addStretch()
        self.mode = QComboBox()
        self.mode.addItem(tr('Translations'), "translation")
        self.mode.addItem(tr('Source text'), "source")
        self.mode.addItem(tr('Source and translations'), "both")
        self.mode.setAccessibleName(tr('Output contents'))
        self.mode.currentIndexChanged.connect(self.refresh)
        header.addWidget(self.mode)
        layout.addLayout(header)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setAccessibleName(tr('Combined output for all pages'))
        layout.addWidget(self.text, 1)
        self.summary = label("", "muted", True)
        layout.addWidget(self.summary)
        footer = QHBoxLayout()
        self.refresh_button = button(
            tr('Refresh'), self.refresh, tooltip=tr('Include the latest completed batch results')
        )
        footer.addWidget(self.refresh_button)
        self.show_headers = QCheckBox(tr('Show page headers'))
        self.show_headers.setChecked(True)
        self.show_headers.setToolTip(tr('Include page numbers and page-level labels in the copied output'))
        self.show_headers.toggled.connect(self.refresh)
        options = QHBoxLayout()
        options.addWidget(self.show_headers)
        self.show_placeholders = QCheckBox(tr('Show missing-text placeholders'))
        self.show_placeholders.setChecked(True)
        self.show_placeholders.toggled.connect(self.refresh)
        options.addWidget(self.show_placeholders)
        options.addStretch()
        layout.addLayout(options)
        self.copy_status = label("", "muted")
        footer.addWidget(self.copy_status)
        footer.addStretch()
        self.copy_button = button(tr('Copy all'), self.copy_all, "primary")
        footer.addWidget(self.copy_button)
        footer.addWidget(button(tr('Close'), self.accept))
        layout.addLayout(footer)
        self.refresh()

    def refresh(self, *_):
        """Refresh explicitly so arriving batch results never disrupt text selection."""
        self.text.setPlainText(export_text(
            self.book, self.mode.currentData(),
            show_page_headers=self.show_headers.isChecked(),
            show_placeholders=self.show_placeholders.isChecked(),
        ))
        count = len(self.book.pages)
        mode = self.mode.currentData()
        details = [f"{count} page{'s' if count != 1 else ''}"]
        if mode in ("source", "both"):
            missing = sum(not page.source.strip() for page in self.book.pages)
            if missing:
                details.append(f"{missing} missing source text")
        if mode in ("translation", "both"):
            missing = sum(not page.translation.strip() for page in self.book.pages)
            stale = sum(page.translation_is_stale for page in self.book.pages)
            if missing:
                details.append(f"{missing} untranslated")
            if stale:
                details.append(f"{stale} with edited source")
        self.summary.setText(" · ".join(details))
        self.copy_status.clear()
        self.copy_button.setEnabled(bool(self.text.toPlainText().strip()))

    def copy_all(self):
        QApplication.clipboard().setText(self.text.toPlainText())
        self.copy_status.setText(tr('Copied'))


class ExportOptionsDialog(QDialog):
    """Choose text content and headings before choosing the destination file."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('Export'))
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        self.file_format = QComboBox()
        self.file_format.addItem(tr("Plain text (.txt)"), "txt")
        self.file_format.addItem(tr("Word document (.docx)"), "docx")
        self.file_format.setAccessibleName(tr("Export format"))
        layout.addWidget(self.file_format)
        self.page_breaks = QCheckBox(tr("Start each book page on a new sheet"))
        self.page_breaks.setVisible(False)
        self.file_format.currentIndexChanged.connect(lambda: self.page_breaks.setVisible(self.file_format.currentData() == "docx"))
        layout.addWidget(self.page_breaks)
        self.mode = QComboBox()
        for title, mode in (
            ("Translations", "translation"),
            ("Source text", "source"),
            ("Source and translations", "both"),
        ):
            self.mode.addItem(tr(title), mode)
        self.mode.setAccessibleName(tr('Export contents'))
        layout.addWidget(self.mode)
        self.show_headers = QCheckBox(tr('Show page headers'))
        self.show_headers.setChecked(True)
        layout.addWidget(self.show_headers)
        self.show_placeholders = QCheckBox(tr('Show missing-text placeholders'))
        self.show_placeholders.setChecked(True)
        layout.addWidget(self.show_placeholders)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr('Continue'))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
