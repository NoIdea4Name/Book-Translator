"""Zoomable page canvas and a bounded, lazy-loading thumbnail strip."""

from .i18n import tr
from collections import OrderedDict
import html

from PySide6.QtCore import QBuffer, QIODevice, QItemSelectionModel, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QGraphicsScene,
    QGraphicsView,
    QListView,
    QListWidget,
    QListWidgetItem,
    QStyledItemDelegate,
)

COLORS = {"source": "#cb8b0b", "translation": "#8254ce"}

REVIEW_COUNT_ROLE = int(Qt.ItemDataRole.UserRole) + 1
CHECKED_ROLE = REVIEW_COUNT_ROLE + 1
MANUAL_FLAG_ROLE = REVIEW_COUNT_ROLE + 2


class ThumbnailDelegate(QStyledItemDelegate):
    """Draw a review badge separately from the cached original-page image."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        count = index.data(REVIEW_COUNT_ROLE) or 0
        checked = bool(index.data(CHECKED_ROLE))
        manual = bool(index.data(MANUAL_FLAG_ROLE))
        badges = (["review"] if count and not checked else []) + (["manual"] if manual else [])
        if not badges:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for offset, kind in enumerate(badges):
            painter.save()
            painter.translate(option.rect.left() + 6 + offset * 26, option.rect.top() + 6)
            manual = kind == "manual"
            painter.setPen(QPen(QColor("#c94646" if manual else "#d7a733"), 1))
            painter.setBrush(QColor("#ef4444" if manual else "#fff0b3"))
            painter.drawRoundedRect(QRectF(0, 0, 22, 22), 5, 5)
            painter.setPen(QPen(QColor("#ffffff" if manual else "#725000"), 2))
            if manual:
                painter.drawLine(QPointF(7, 5), QPointF(7, 18))
                painter.setBrush(QColor("#ffffff"))
                painter.drawPolygon(QPolygonF([QPointF(8, 5), QPointF(17, 5), QPointF(14, 9), QPointF(17, 12), QPointF(8, 12)]))
            else:
                painter.drawLine(QPointF(11, 5), QPointF(11, 12))
                painter.setBrush(QColor("#725000"))
                painter.drawEllipse(QRectF(10, 16, 2, 2))
            painter.restore()
        painter.restore()


class PageView(QGraphicsView):
    zoom_changed = Signal(int)
    review_clicked = Signal(object)
    region_changed = Signal()
    region_mode_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setToolTip(tr('Drag to pan · Scroll to zoom · Double-click to fit'))
        self.fit_timer = QTimer(self)
        self.fit_timer.setSingleShot(True)
        self.fit_timer.timeout.connect(self._fit_if_needed)
        self.fit_mode = True
        self.item = None
        self.overlays = []
        self.region_item = None
        self.region_start = None
        self.region_mode = False

    def select_region(self, enabled=True):
        self.region_mode = enabled
        self.region_start = None
        self.setDragMode(QGraphicsView.DragMode.NoDrag if enabled else QGraphicsView.DragMode.ScrollHandDrag)
        self.viewport().setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.OpenHandCursor)
        self.region_mode_changed.emit(enabled)

    def clear_region(self):
        if self.region_item is not None:
            self.scene().removeItem(self.region_item)
        self.region_item = None
        self.region_start = None
        self.region_changed.emit()

    def region_image(self):
        """Crop original pixels, excluding all review/selection overlays."""
        if self.item is None or self.region_item is None:
            return None
        rect = self.region_item.rect().toAlignedRect().intersected(self.item.pixmap().rect())
        if rect.width() < 2 or rect.height() < 2:
            return None
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.item.pixmap().copy(rect).save(buffer, "PNG")
        return bytes(buffer.data())

    def set_image(self, data: bytes):
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            raise ValueError("Could not display this page.")
        self.select_region(False)
        self.clear_region()
        self.overlays = []
        self.scene().clear()
        self.item = self.scene().addPixmap(pixmap)
        self.item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        rect = self.item.boundingRect()
        # Extra canvas makes dragging work even when the full page is fitted.
        self.scene().setSceneRect(
            rect.adjusted(-rect.width(), -rect.height(), rect.width(), rect.height())
        )
        self.fit_mode = True
        self.fit_page()
        self.fit_timer.start(0)

    def _fit_if_needed(self):
        if self.fit_mode:
            self.fit_page()

    def fit_page(self):
        self.fit_mode = True
        if self.item is not None:
            # Discard any translation left by mouse-anchored zoom before fitting.
            self.resetTransform()
            self.fitInView(
                self.item.boundingRect().adjusted(-22, -22, 22, 22),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
            self.centerOn(self.item)
            self.zoom_changed.emit(round(self.transform().m11() * 100))

    def zoom(self, factor, anchor=None):
        if self.item is None:
            return
        current = self.transform().m11()
        target = max(0.03, min(6.0, current * factor))
        point = anchor if anchor is not None else self.viewport().rect().center()
        before = self.mapToScene(point)
        self.fit_mode = False
        self.scale(target / current, target / current)
        after = self.mapToScene(point)
        delta = after - before
        self.translate(delta.x(), delta.y())
        self.zoom_changed.emit(round(target * 100))

    def page_rect(self, box):
        if self.item is None or not box:
            return QRectF()
        top, left, bottom, right = box
        bounds = self.item.boundingRect()
        return QRectF(
            left * bounds.width() / 1000,
            top * bounds.height() / 1000,
            (right - left) * bounds.width() / 1000,
            (bottom - top) * bounds.height() / 1000,
        )

    def set_flags(self, flags):
        for item in self.overlays:
            self.scene().removeItem(item)
        self.overlays = []
        if self.item is None:
            return
        for flag in flags:
            if not flag.box:
                continue
            color = QColor(COLORS[flag.kind])
            pen = QPen(color, 2.3)
            pen.setCosmetic(True)
            if flag.kind == "translation":
                pen.setStyle(Qt.PenStyle.DashLine)
            fill = QColor(color)
            fill.setAlpha(34)
            overlay = self.scene().addRect(self.page_rect(flag.box), pen, fill)
            overlay.setData(0, flag)
            overlay.setZValue(10 if flag.kind == "source" else 11)
            overlay.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            overlay.setAcceptHoverEvents(True)
            overlay.setToolTip(
                "<qt>" + html.escape(flag.tooltip[:2500]).replace("\n", "<br>") + "</qt>"
            )
            self.overlays.append(overlay)

    def focus_box(self, box):
        rect = self.page_rect(box)
        if rect.isEmpty():
            return
        self.fit_mode = False
        padding = max(40, rect.width() * 0.2)
        self.fitInView(
            rect.adjusted(-padding, -padding, padding, padding), Qt.AspectRatioMode.KeepAspectRatio
        )
        scale = self.transform().m11()
        if scale > 6:
            self.scale(6 / scale, 6 / scale)
        self.centerOn(rect.center())
        self.zoom_changed.emit(round(self.transform().m11() * 100))

    def resizeEvent(self, event):
        visible_center = self.mapToScene(self.viewport().rect().center())
        super().resizeEvent(event)
        if self.fit_mode:
            self.fit_page()
        elif self.item is not None:
            # Keep the page horizontally centered when the panel changes width,
            # while retaining the current zoom and vertical reading position.
            visible_center.setX(self.item.sceneBoundingRect().center().x())
            self.centerOn(visible_center)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            self.zoom(1.15 ** (delta / 120), event.position().toPoint())
        event.accept()

    def mousePressEvent(self, event):
        if self.item is not None and event.button() == Qt.MouseButton.LeftButton and (
            self.region_mode or event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.select_region(True)
            self.clear_region()
            point = self.mapToScene(event.position().toPoint())
            if self.item.boundingRect().contains(point):
                self.region_start = point
                pen = QPen(QColor("#16816b"), 2)
                pen.setCosmetic(True)
                self.region_item = self.scene().addRect(QRectF(point, point), pen, QColor(22, 129, 107, 35))
                self.region_item.setZValue(20)
                self.region_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            event.accept()
            return
        self._review_press = event.position().toPoint()
        self._pressed_review = next((item.data(0) for item in self.items(self._review_press) if item.data(0) is not None), None)
        if event.button() == Qt.MouseButton.LeftButton:
            self.fit_mode = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.region_start is not None:
            rect = QRectF(self.region_start, self.mapToScene(event.position().toPoint())).normalized()
            self.region_item.setRect(rect.intersected(self.item.boundingRect()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.region_mode and event.button() == Qt.MouseButton.LeftButton:
            if self.region_start is not None:
                self.mouseMoveEvent(event)
            self.select_region(False)
            if self.region_item is not None and (self.region_item.rect().width() < 2 or self.region_item.rect().height() < 2):
                self.clear_region()
            else:
                self.region_changed.emit()
            event.accept()
            return
        flag = getattr(self, "_pressed_review", None)
        clicked = event.button() == Qt.MouseButton.LeftButton and flag is not None and (event.position().toPoint() - self._review_press).manhattanLength() <= 4
        self._pressed_review = None
        super().mouseReleaseEvent(event)
        if clicked:
            self.review_clicked.emit(flag)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.fit_page()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.select_region(False)
            self.clear_region()
            event.accept()
            return
        super().keyPressEvent(event)


class ThumbnailStrip(QListWidget):
    """Render only visible/nearby pages, one per event-loop tick.

    PyMuPDF remains on the GUI thread. Up to 48 small page icons are retained;
    scrolling through a large book cannot cache a full-size image for every page.
    """

    move_requested = Signal(list, int)
    CACHE_LIMIT = 48

    def __init__(self):
        super().__init__()
        self.book = None
        self.cache = OrderedDict()
        self.setObjectName("thumbnails")
        self.setItemDelegate(ThumbnailDelegate(self))
        self.setViewMode(QListView.ViewMode.IconMode)
        # One full-width cell per row; IconMode centers the card in that cell.
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setIconSize(QSize(104, 140))
        self.setGridSize(QSize(128, 174))
        self.setUniformItemSizes(True)
        self.setWordWrap(True)
        self.setSpacing(5)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAccessibleName(tr('Page thumbnails'))
        self.setToolTip(tr('Ctrl-click to select · Alt-click to preview without changing selection · Shift-click for a range · Drag to reorder · Right-click for page actions'))
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.editing_enabled = True
        self._drag_rows = []
        self._drop_position = None
        self._scroll_direction = 0
        self.drag_scroll = QTimer(self)
        self.drag_scroll.setInterval(40)
        self.drag_scroll.timeout.connect(self._scroll_drag)
        self.placeholder = self._placeholder()
        self.timer = QTimer(self)
        self.timer.setInterval(15)
        self.timer.timeout.connect(self._render_next)
        self.verticalScrollBar().valueChanged.connect(self.schedule)

    def mousePressEvent(self, event):
        self._preview_only_click = event.button() == Qt.MouseButton.LeftButton and bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        if self._preview_only_click:
            item = self.itemAt(event.position().toPoint())
            if item is not None:
                self.setCurrentRow(self.row(item), QItemSelectionModel.SelectionFlag.NoUpdate)
            event.accept()
            return
        self._press_position = event.position().toPoint() if event.button() == Qt.MouseButton.LeftButton and self.itemAt(event.position().toPoint()) else None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        origin = getattr(self, '_press_position', None)
        if event.buttons() & Qt.MouseButton.LeftButton and self.editing_enabled:
            if getattr(self, '_pointer_drag', False):
                self._update_pointer_drop(event.position().toPoint())
                event.accept()
                return
            if origin is not None and not getattr(self, '_preview_only_click', False):
                if (event.position().toPoint() - origin).manhattanLength() >= QApplication.startDragDistance():
                    self.startDrag(Qt.DropAction.MoveAction)
                    self._update_pointer_drop(event.position().toPoint())
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_position = None
        if getattr(self, '_pointer_drag', False):
            point = event.position().toPoint()
            rows = self._drag_rows.copy()
            destination = self._insertion_at(point)
            inside = self.viewport().rect().contains(point)
            self._end_pointer_drag()
            if inside and self.editing_enabled:
                self.move_requested.emit(rows, destination)
            event.accept()
            return
        if getattr(self, '_preview_only_click', False):
            self._preview_only_click = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and getattr(self, '_pointer_drag', False):
            self._end_pointer_drag()
            event.accept()
            return
        super().keyPressEvent(event)

    def set_editing_enabled(self, enabled):
        self.editing_enabled = enabled
        if not enabled:
            self._end_pointer_drag()
        # Reordering is handled by pointer events, not the platform drag service.
        self.setDragEnabled(False)
        self.setAcceptDrops(False)

    def startDrag(self, supported_actions):
        if not self.editing_enabled:
            return
        self._drag_rows = sorted(self.row(item) for item in self.selectedItems())
        self._pointer_drag = bool(self._drag_rows)
        if self._pointer_drag:
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)

    def _end_pointer_drag(self):
        self._pointer_drag = False
        self._press_position = None
        self._drag_rows = []
        self.viewport().unsetCursor()
        self.setState(QAbstractItemView.State.NoState)
        self._clear_drop()

    def _insertion_at(self, point):
        for row in range(self.count()):
            if point.y() < self.visualItemRect(self.item(row)).center().y():
                return row
        return self.count()

    def _update_pointer_drop(self, point):
        self._drop_position = self._insertion_at(point)
        self._scroll_direction = -1 if point.y() < 28 else (
            1 if point.y() > self.viewport().height() - 28 else 0
        )
        if self._scroll_direction:
            self.drag_scroll.start()
        else:
            self.drag_scroll.stop()
        self.viewport().update()

    def _scroll_drag(self):
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + 16 * self._scroll_direction)
        # Update the drop marker as the list moves beneath the pointer.
        from PySide6.QtGui import QCursor
        point = self.viewport().mapFromGlobal(QCursor.pos())
        self._drop_position = self._insertion_at(point)
        self.viewport().update()

    def _clear_drop(self):
        self.drag_scroll.stop()
        self._drop_position = None
        self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop_position is None or not self.count():
            return
        row = min(self._drop_position, self.count() - 1)
        rect = self.visualItemRect(self.item(row))
        y = rect.top() - 2 if self._drop_position < self.count() else rect.bottom() + 2
        painter = QPainter(self.viewport())
        painter.setPen(QPen(QColor("#16816b"), 3))
        painter.drawLine(6, max(2, y), self.viewport().width() - 6, max(2, y))
        painter.end()

    @staticmethod
    def _placeholder():
        pixmap = QPixmap(104, 140)
        pixmap.fill(QColor("#ffffff"))
        painter = QPainter(pixmap)
        painter.setPen(QColor("#dce3e5"))
        painter.drawRect(0, 0, 103, 139)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "…")
        painter.end()
        return ThumbnailStrip._page_icon(pixmap)

    @staticmethod
    def _page_icon(pixmap):
        # Keep the scan's original colors; the card border indicates selection.
        icon = QIcon(pixmap)
        icon.addPixmap(pixmap, QIcon.Mode.Selected, QIcon.State.Off)
        icon.addPixmap(pixmap, QIcon.Mode.Selected, QIcon.State.On)
        return icon

    def set_book(self, book):
        self.timer.stop()
        self.blockSignals(True)
        try:
            self.clear()
            self.cache.clear()
            self.book = book
            for index in range(len(book.pages)):
                item = QListWidgetItem(self.placeholder, book.page_name(index))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setToolTip(book.page_description(index))
                self.addItem(item)
        finally:
            self.blockSignals(False)
        self.schedule()

    def set_current_page(self, index):
        self.blockSignals(True)
        if self.currentRow() != index:
            # Navigation must not collapse an existing batch selection.
            command = (
                QItemSelectionModel.SelectionFlag.NoUpdate
                if len(self.selectedItems()) > 1
                else QItemSelectionModel.SelectionFlag.ClearAndSelect
            )
            self.setCurrentRow(index, command)
        self.blockSignals(False)
        self.scrollToItem(self.item(index), QAbstractItemView.ScrollHint.EnsureVisible)
        self.schedule()

    def set_review_count(self, index, count):
        if not 0 <= index < self.count():
            return
        name = self.book.page_name(index)
        display = name if len(name) <= 32 else name[:29] + "…"
        item = self.item(index)
        item.setData(REVIEW_COUNT_ROLE, count)
        item.setData(CHECKED_ROLE, self.book.pages[index].is_reviewed)
        item.setData(MANUAL_FLAG_ROLE, self.book.pages[index].manual_flag)
        tooltip = name + "\n" + self.book.page_description(index)
        if self.book.pages[index].manual_flag:
            tooltip += tr('\nFlagged by you.')
        if self.book.pages[index].is_reviewed:
            tooltip += tr('\nChecked by you. AI confidence scores are unchanged.')
        if count:
            tooltip += f"\nNeeds review: {count} low-confidence flag(s)."
            page = self.book.pages[index]
            for title, report, text, valid in (
                ("Extraction", page.extraction_assessment, page.source, True),
                ("Translation", page.translation_assessment, page.translation, not page.translation_is_stale),
            ):
                if report and report.text == text and valid:
                    tooltip += f"\n{title}: {report.confidence:.0%} estimated confidence"
        tooltip += tr('\nAlt-click: preview without changing selected pages. Ctrl-click: add/remove selection.')
        item.setToolTip(tooltip)
        item.setText(display)

    def schedule(self, *_):
        if self.book and not getattr(self, "render_paused", False) and not self.timer.isActive():
            self.timer.start()

    def _wanted(self):
        first = self.indexAt(QPoint(8, 8)).row()
        if first < 0:
            first = max(0, self.verticalScrollBar().value() // self.gridSize().height())
        visible = self.viewport().height() // self.gridSize().height() + 3
        return range(max(0, first - 1), min(self.count(), first + visible))

    def _render_next(self):
        if not self.book:
            self.timer.stop()
            return
        wanted = list(self._wanted())
        pending = [row for row in wanted if row not in self.cache]
        if not pending:
            for row in wanted:
                self.cache.move_to_end(row)
            self.timer.stop()
            return
        row = pending[0]
        try:
            pixmap = QPixmap()
            if not pixmap.loadFromData(self.book.render_page(row, 220)):
                raise ValueError("Invalid page image")
            icon = self._page_icon(pixmap)
        except Exception as exc:
            icon = self.placeholder
            self.item(row).setToolTip(f"Preview unavailable: {exc}")
        self.cache[row] = icon
        self.item(row).setIcon(icon)
        while len(self.cache) > self.CACHE_LIMIT:
            old, _ = self.cache.popitem(last=False)
            self.item(old).setIcon(self.placeholder)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Use the full available column width so Qt centers each thumbnail
        # inside the panel, including when a vertical scrollbar appears.
        self.setGridSize(QSize(self.viewport().width(), 174))
        self.schedule()
