"""Window layout. Edit this file to move controls or change their labels.

Builders attach widgets to MainWindow and connect its actions from ui.py.
They create the interface; document state and request handling live in ui.py.
"""

from .i18n import tr

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QSpinBox,
    QSplitter,
    QStackedLayout,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .config import DEFAULT_LANGUAGE, CONFIDENCE_THRESHOLD, LANGUAGES
from .preview import PageView, ThumbnailStrip
from .widgets import ASSETS, app_icon, button, label


def build_interface(window):
    root = QWidget()
    window.setCentralWidget(root)
    layout = QVBoxLayout(root)
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(10)
    layout.addLayout(_build_header(window))
    layout.addWidget(_build_toolbar(window))

    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.setChildrenCollapsible(False)
    splitter.setHandleWidth(10)
    splitter.addWidget(_build_editor(window))
    splitter.addWidget(_build_preview(window))
    splitter.addWidget(_build_thumbnail_strip(window))
    splitter.setSizes([560, 650, 155])
    layout.addWidget(splitter, 1)
    _build_status_bar(window)


def _build_header(window):
    header = QHBoxLayout()
    brand = QLabel()
    brand.setPixmap(app_icon().pixmap(34, 34))
    header.addWidget(brand)
    titles = QVBoxLayout()
    titles.setSpacing(3)
    titles.addWidget(label(tr('Book Translator'), "title"))
    window.document_name = label("", "subtitle")
    window.document_name.hide()
    window.document_name.setMaximumWidth(210)
    titles.addWidget(window.document_name)
    header.addLayout(titles)
    header.addStretch()
    navigation = QFrame()
    navigation.setObjectName("pageNavigation")
    navigation_row = QHBoxLayout(navigation)
    navigation_row.setContentsMargins(3, 2, 8, 2)
    navigation_row.setSpacing(2)
    window.previous_button = button(
        "<", lambda: window.go_to(window.index - 1), "nav", tr('Previous page · Alt+Left')
    )
    window.page_button = button(tr('Page —'), window.jump_to_page, "page", tr('Jump to page · Ctrl+G'))
    window.next_button = button(
        ">", lambda: window.go_to(window.index + 1), "nav", tr('Next page · Alt+Right')
    )
    window.total_label = label("", "muted")
    for widget in (
        window.previous_button,
        window.page_button,
        window.next_button,
        window.total_label,
    ):
        navigation_row.addWidget(widget)
    for widget, name, title in ((window.previous_button, "left", "Previous page"), (window.next_button, "right", "Next page")):
        widget.setText("")
        widget.setIcon(QIcon(str(ASSETS / f"ui-{name}.svg")))
        widget.setIconSize(QSize(16, 16))
        widget.setFixedSize(38, 36)
        widget.setAccessibleName(tr(title))
    header.addWidget(navigation)
    header.addStretch()
    window.open_button = button(
        tr('Open files'), window.open_files, tooltip=tr('Add PDFs or images to this workspace · Ctrl+O')
    )
    window.save_button = button(
        tr('Save'), window.save, tooltip=tr('Save source text and translations · Ctrl+S')
    )
    window.settings_button = button(tr('Settings'), window.settings)
    for widget, name in ((window.open_button, "open"), (window.save_button, "save"), (window.settings_button, "settings")):
        widget.setIcon(QIcon(str(ASSETS / f"ui-{name}.svg")))
        widget.setIconSize(QSize(16, 16))
    header.addWidget(window.open_button)
    header.addWidget(window.save_button)
    header.addWidget(window.settings_button)
    return header


def _build_toolbar(window):
    toolbar = QFrame()
    toolbar.setObjectName("toolbar")
    row = QHBoxLayout(toolbar)
    row.setContentsMargins(10, 7, 10, 7)
    row.setSpacing(8)
    row.addWidget(label(tr('Method'), "muted"))
    window.method = QComboBox()
    window.method.addItems([tr('PDF text · offline'), tr('AI OCR')])
    window.method.setMinimumWidth(138)
    window.method.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
    window.method.setAccessibleName(tr('Extraction method'))
    window.method.setToolTip(tr('Method for Extract. Extract & Translate always uses AI OCR.'))
    row.addWidget(window.method)
    row.addSpacing(4)
    row.addWidget(label(tr('To'), "muted"))
    window.language = QComboBox()
    window.language.setEditable(True)
    window.language.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    window.language.addItems(LANGUAGES)
    window.language.setCurrentText(DEFAULT_LANGUAGE)
    window.language.setFixedWidth(148)
    window.language.setAccessibleName(tr('Translation target language'))
    window.language.setToolTip(tr('Target language'))
    window.language.lineEdit().setMaxLength(80)
    window.language.currentTextChanged.connect(window._language_changed)
    row.addWidget(window.language)
    separator = QFrame()
    separator.setObjectName("toolbarDivider")
    separator.setFixedSize(1, 22)
    row.addWidget(separator)
    window.extract_button = button(
        tr('Extract'), window.extract, tooltip=tr('Extract selected pages · Ctrl+Shift+E')
    )
    window.translate_button = button(
        tr('Translate'), window.translate,
        tooltip=tr("Translate text or page images · Ctrl+Enter"),
    )
    window.combined_button = button(
        tr('Extract && Translate'), window.extract_and_translate, "primary",
        tr('Extract and translate selected pages, one combined AI request per attempt · Ctrl+Shift+Enter'),
    )
    for widget in (window.extract_button, window.translate_button, window.combined_button):
        row.addWidget(widget)
    row.addStretch()
    window.summary_button = button(tr('Summary'), window.show_summary, "small", tr('View and edit book context · Ctrl+Shift+M'))
    row.addWidget(window.summary_button)
    window.batch_button = button(tr("Batch"), window.show_batch, "small", tr("Batch processing · Ctrl+Shift+B"))
    row.addWidget(window.batch_button)
    from .page_history import show_history
    window.history_button = button(tr("History"), lambda: show_history(window), "small", tr("Page history"))
    row.addWidget(window.history_button)
    window.selection_label = label("", "muted")

    return toolbar


def _build_editor(window):
    left = QFrame()
    left.setObjectName("panel")
    left.setMinimumWidth(320)
    left_layout = QVBoxLayout(left)
    left_layout.setContentsMargins(1, 1, 1, 1)
    left_layout.setSpacing(0)
    left_header = QHBoxLayout()
    left_header.setSpacing(6)
    left_header.setContentsMargins(12, 10, 12, 4)
    left_header.addWidget(label(tr('TEXT'), "eyebrow"))
    left_header.addStretch()
    window.chat_button = button(tr('Ask AI'), lambda: window.chat_action.trigger(), "small", tr('Ask about selected text · Ctrl+J'))
    window.chat_button.setIcon(QIcon(str(ASSETS / "ui-chat.svg")))
    window.chat_button.setIconSize(QSize(14, 14))
    left_header.addWidget(window.chat_button)
    window.copy_button = button(tr('Copy'), window.copy_text, "small", tr('Copy current page text'))
    left_header.addWidget(window.copy_button)
    left_layout.addLayout(left_header)
    window.tabs = QTabWidget()
    window.source_editor = QPlainTextEdit()
    window.translation_editor = QPlainTextEdit()
    window.source_editor.setPlaceholderText(tr('Source text'))
    window.translation_editor.setPlaceholderText(tr('Translation'))
    window.source_editor.setAccessibleName(tr('Current page source text'))
    window.translation_editor.setAccessibleName(tr('Current page translation'))
    window.tabs.addTab(window.source_editor, tr('Source text'))
    window.tabs.addTab(window.translation_editor, tr('Translation'))
    window.tabs.currentChanged.connect(window._update_page_details)
    window.source_editor.document().contentsChange.connect(
        lambda position, removed, added: window._record_review_edit("source", position, removed, added))
    window.translation_editor.document().contentsChange.connect(
        lambda position, removed, added: window._record_review_edit("translation", position, removed, added))
    window.source_editor.textChanged.connect(window._source_changed)
    window.translation_editor.textChanged.connect(window._translation_changed)
    from .page_find import PageFind
    window.page_find = PageFind(window)
    left_layout.addWidget(window.page_find)
    left_layout.addWidget(window.tabs, 1)
    window.review_panel = QWidget()
    review_layout = QVBoxLayout(window.review_panel)
    review_layout.setContentsMargins(0, 0, 0, 0)
    review_layout.setSpacing(0)
    window.review_toggle = QToolButton()
    window.review_toggle.setObjectName("reviewToggle")
    window.review_toggle.setFixedSize(28, 28)
    window.review_toggle.setAccessibleName(tr('Collapse or expand review passages'))
    window.review_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    window.review_toggle.setCheckable(True)
    window.review_toggle.setChecked(True)
    window.review_toggle.setArrowType(Qt.ArrowType.UpArrow)
    window.review_toggle.toggled.connect(window._toggle_review_panel)
    review_header = QHBoxLayout()
    review_header.setContentsMargins(12, 4, 6, 4)
    review_layout.addLayout(review_header)
    window.review_body = QWidget()
    review_body_layout = QVBoxLayout(window.review_body)
    review_body_layout.setContentsMargins(0, 0, 0, 0)
    review_body_layout.setSpacing(0)
    review_layout.addWidget(window.review_body)
    left_layout.addWidget(window.review_panel)
    window.review_panel.hide()
    window.confidence_label = label("", "muted", True)
    window.confidence_label.setWordWrap(False)
    window.confidence_label.hide()
    window.confidence_label.setContentsMargins(0, 0, 0, 0)
    window.confidence_label.setToolTip(
        tr('AI confidence scores are self-estimates, not measured accuracy. Local PDF extraction has no AI confidence score.')
    )
    review_header.addWidget(window.confidence_label)
    review_header.addStretch()
    review_header.addWidget(window.review_toggle)
    window.review_list = QListWidget()
    window.review_list.setObjectName("reviews")
    window.review_list.setMaximumHeight(116)
    window.review_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    window.review_list.setToolTip(
        tr('Click a flagged passage to zoom to its location. Hover for the explanation.')
    )
    window.review_list.itemClicked.connect(window._focus_review)
    window.review_list.hide()
    review_body_layout.addWidget(window.review_list)
    window.page_note = label("", "muted", True)
    window.page_note.hide()
    window.page_note.setContentsMargins(18, 8, 18, 0)
    left_layout.addWidget(window.page_note)
    window.word_count = label(tr('0 words · 0 characters'), "muted")
    window.word_count.setContentsMargins(14, 7, 14, 9)
    left_layout.addWidget(window.word_count)
    return left


def _build_preview(window):
    right = QFrame()
    right.setObjectName("panel")
    right.setMinimumWidth(400)
    right_layout = QVBoxLayout(right)
    right_layout.setContentsMargins(1, 1, 1, 1)
    right_layout.setSpacing(0)
    preview_header = QHBoxLayout()
    preview_header.setSpacing(5)
    preview_header.setContentsMargins(12, 10, 12, 9)
    preview_header.addWidget(label(tr('ORIGINAL'), "eyebrow"))
    preview_header.addStretch()
    window.preview = PageView()
    window.preview.review_clicked.connect(window._focus_review_flag)
    window.region_button = button(tr('Region'), lambda: window.preview.select_region(not window.preview.region_mode), "small", tr('Select an image region for chat · Shift-drag · Esc to clear'))
    window.region_button.setCheckable(True)
    window.preview.region_mode_changed.connect(window.region_button.setChecked)
    preview_header.addWidget(window.region_button)
    window.zoom_label = label(tr('Fit'), "muted")
    window.zoom_label.setMinimumWidth(42)
    window.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    window.zoom_out = button("−", lambda: window.preview.zoom(1 / 1.2), "small", tr('Zoom out'))
    window.zoom_in = button("+", lambda: window.preview.zoom(1.2), "small", tr('Zoom in'))
    window.fit_button = button(tr('Fit'), window.preview.fit_page, "small", tr('Fit entire page'))
    for widget in (window.zoom_out, window.zoom_in):
        widget.setFixedSize(27, 27)
    for widget in (window.zoom_out, window.zoom_label, window.zoom_in, window.fit_button):
        preview_header.addWidget(widget)
    right_layout.addLayout(preview_header)
    review_controls = QHBoxLayout()
    review_controls.setContentsMargins(12, 0, 12, 9)
    review_controls.setSpacing(8)
    window.show_flags = QCheckBox(tr('Flags'))
    window.show_flags.setChecked(True)
    window.show_flags.toggled.connect(window._update_reviews)
    review_controls.addWidget(window.show_flags)
    extraction_key = label(tr('■ Extraction'))
    extraction_key.setObjectName("extractionLegend")
    translation_key = label(tr('■ Translation'))
    translation_key.setObjectName("translationLegend")
    review_controls.addWidget(extraction_key)
    review_controls.addWidget(translation_key)
    review_controls.addStretch()
    review_controls.addWidget(label(tr('Below'), "muted"))
    window.threshold = QSpinBox()
    window.threshold.setRange(1, 100)
    window.threshold.setSuffix("%")
    window.threshold.setValue(CONFIDENCE_THRESHOLD)
    window.threshold.setFixedWidth(72)
    window.threshold.setToolTip(tr('Flag passages whose confidence is below this threshold.'))
    window.threshold.valueChanged.connect(window._threshold_changed)
    review_controls.addWidget(window.threshold)
    right_layout.addLayout(review_controls)
    window.preview.zoom_changed.connect(lambda zoom: window.zoom_label.setText(tr('{p0}%', p0=zoom)))
    preview_stack_widget = QWidget()
    window.preview_stack = QStackedLayout(preview_stack_widget)
    empty = QWidget()
    empty.setObjectName("emptyPreview")
    empty_layout = QVBoxLayout(empty)
    empty_layout.addStretch()
    window.empty_message = label(tr('Open a PDF or images'), "muted", True)
    window.empty_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
    empty_layout.addWidget(window.empty_message)
    empty_layout.addStretch()
    window.preview_stack.addWidget(empty)
    window.preview_stack.addWidget(window.preview)
    right_layout.addWidget(preview_stack_widget, 1)
    window.preview.setToolTip(tr('Drag to pan · Scroll to zoom · Double-click to fit'))
    window.preview_caption = label("", "muted")
    window.preview_caption.hide()
    window.preview_caption.setContentsMargins(12, 8, 12, 9)
    right_layout.addWidget(window.preview_caption)
    return right


def _build_thumbnail_strip(window):
    thumb_panel = QFrame()
    thumb_panel.setObjectName("panel")
    thumb_panel.setMinimumWidth(148)
    thumb_panel.setMaximumWidth(182)
    thumb_layout = QVBoxLayout(thumb_panel)
    thumb_layout.setContentsMargins(1, 1, 1, 1)
    thumb_layout.setSpacing(0)
    window.thumbnail_title = label(tr('PAGES'), "eyebrow")
    header = QHBoxLayout()
    header.setContentsMargins(12, 10, 8, 10)
    header.setSpacing(3)
    header.addWidget(window.thumbnail_title)
    header.addStretch()
    window.thumbnails = ThumbnailStrip()
    window.thumbnails.currentRowChanged.connect(window.go_to)
    window.thumbnails.itemSelectionChanged.connect(window._selection_changed)
    window.thumbnails.itemDoubleClicked.connect(
        lambda item: window.rename_page(window.thumbnails.row(item))
    )
    window.thumbnails.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    window.thumbnails.customContextMenuRequested.connect(window.page_context_menu)
    window.thumbnails.move_requested.connect(window.move_pages)
    window.select_all_button = button(
        "", window.thumbnails.selectAll, "iconButton", tr('Select all pages · Ctrl+A in thumbnails')
    )
    window.select_all_button.setIcon(QIcon(str(ASSETS / "select-all.svg")))
    window.select_all_button.setAccessibleName(tr('Select all pages'))
    window.select_all_button.setFixedSize(28, 28)
    window.select_all_button.setIconSize(QSize(17, 17))
    header.addWidget(window.select_all_button)
    thumb_layout.addLayout(header)
    thumb_layout.addWidget(window.thumbnails, 1)
    return thumb_panel


def _build_status_bar(window):
    window.progress = QProgressBar()
    window.progress.setRange(0, 0)
    window.progress.setFixedWidth(90)
    window.progress.setTextVisible(False)
    window.progress.hide()
    window.cancel_button = button(
        tr('Cancel'),
        window.cancel_request,
        "small",
        tr('Ignore the result. A request already sent to the provider may still use quota.'),
    )
    window.cancel_button.hide()
    window.connection_label = label(
        (tr('Gemini · untested') if window.api_key else tr('Gemini · no key')), "muted"
    )
    window.connection_label.setToolTip(tr('Configure and test the API connection in Settings.'))
    window.token_total = 0
    window.token_requests = 0
    window.token_label = label(tr('Tokens: —'), "muted")
    window.token_label.setToolTip(tr('Provider-reported usage for completed page and chat responses this session. No usage reported yet.'))
    window.statusBar().addPermanentWidget(window.selection_label)
    window.statusBar().addPermanentWidget(window.token_label)
    window.statusBar().addPermanentWidget(window.progress)
    window.statusBar().addPermanentWidget(window.cancel_button)
    window.statusBar().addPermanentWidget(window.connection_label)
