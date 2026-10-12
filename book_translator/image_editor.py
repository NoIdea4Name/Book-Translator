"""Crop, rotate and manually straighten a photographed page.

Perspective correction maps four page corners onto a rectangle. It does not
flatten curved paper. All edits work on copies, never on the source file.
"""
from __future__ import annotations

from .i18n import tr

from io import BytesIO
import math

import numpy as np
from PIL import Image, ImageOps
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QComboBox, QDialog, QGraphicsEllipseItem, QGraphicsItem, QGraphicsScene,
    QGraphicsView, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
)


def load_image(path):
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def image_pixmap(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue())
    return pixmap


def full_corners(image):
    return [(0, 0), (image.width, 0), (image.width, image.height), (0, image.height)]


def transform_image(image, points, mode):
    """Return a cropped/rectified copy; points are clockwise from top left."""
    if len(points) != 4 or any(not math.isfinite(v) for p in points for v in p):
        raise ValueError("Choose four valid page corners.")
    if any(not 0 <= x <= image.width or not 0 <= y <= image.height for x, y in points):
        raise ValueError("Keep all corners inside the image.")
    cross = []
    for i in range(4):
        a, b, c = points[i], points[(i+1) % 4], points[(i+2) % 4]
        cross.append((b[0]-a[0])*(c[1]-b[1]) - (b[1]-a[1])*(c[0]-b[0]))
    if min(cross) <= 1:
        raise ValueError("Corners must form a rectangle or a convex page outline without crossing.")
    if mode == "Crop":
        box = (round(points[0][0]), round(points[0][1]), round(points[2][0]), round(points[2][1]))
        if box[2]-box[0] < 4 or box[3]-box[1] < 4:
            raise ValueError("Select an area at least 4 pixels wide and high.")
        return image.crop(box)
    width = round(max(math.dist(points[0], points[1]), math.dist(points[3], points[2])))
    height = round(max(math.dist(points[0], points[3]), math.dist(points[1], points[2])))
    if min(width, height) < 4 or width * height > 40_000_000:
        raise ValueError("The corrected page must be between 4 pixels and 40 megapixels.")
    # Pillow needs the inverse map: output rectangle -> original quadrilateral.
    output = [(0, 0), (width, 0), (width, height), (0, height)]
    matrix, values = [], []
    for (x, y), (u, v) in zip(output, points):
        matrix.extend([[x, y, 1, 0, 0, 0, -u*x, -u*y], [0, 0, 0, x, y, 1, -v*x, -v*y]])
        values.extend([u, v])
    try:
        coefficients = np.linalg.solve(matrix, values)
    except np.linalg.LinAlgError as exc:
        raise ValueError("Choose a wider page outline.") from exc
    return image.transform((width, height), Image.Transform.PERSPECTIVE, coefficients,
                           Image.Resampling.BICUBIC)


class CornerHandle(QGraphicsEllipseItem):
    def __init__(self, canvas, index):
        super().__init__(-8, -8, 16, 16)
        self.canvas, self.index = canvas, index
        self.setBrush(QColor("#ffffff"))
        self.setPen(QPen(QColor("#2863ce"), 3))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setZValue(2)

    def mousePressEvent(self, event):
        event.accept()

    def mouseMoveEvent(self, event):
        self.canvas.move_corner(self.index, event.scenePos())
        event.accept()


class EditCanvas(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#e3eaf3"))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.mode = "Crop"
        self.image = None
        self.points = []
        self.handles = []
        self.setMinimumSize(420, 320)

    def set_image(self, image):
        self.image = image
        self.scene().clear()
        self.scene().addPixmap(image_pixmap(image))
        self.points = full_corners(image)
        pen = QPen(QColor("#2863ce"), 2)
        pen.setCosmetic(True)
        self.outline = self.scene().addPolygon(QPolygonF(), pen, QColor(40, 99, 206, 25))
        self.handles = [CornerHandle(self, i) for i in range(4)]
        for handle in self.handles:
            self.scene().addItem(handle)
        self.update_outline()
        margin = max(image.size) * .035
        self.scene().setSceneRect(-margin, -margin, image.width + 2*margin, image.height + 2*margin)
        self.fit()

    def move_corner(self, index, position):
        x = min(self.image.width, max(0, position.x()))
        y = min(self.image.height, max(0, position.y()))
        if self.mode == "Crop":
            opposite = self.points[(index + 2) % 4]
            x = min(x, opposite[0] - 4) if index in (0, 3) else max(x, opposite[0] + 4)
            y = min(y, opposite[1] - 4) if index in (0, 1) else max(y, opposite[1] + 4)
            self.points[index] = (x, y)
            neighbor_x = {0:3, 1:2, 2:1, 3:0}[index]
            neighbor_y = {0:1, 1:0, 2:3, 3:2}[index]
            self.points[neighbor_x] = (x, self.points[neighbor_x][1])
            self.points[neighbor_y] = (self.points[neighbor_y][0], y)
        else:
            self.points[index] = (x, y)
        self.update_outline()

    def update_outline(self):
        self.outline.setPolygon(QPolygonF([QPointF(*p) for p in self.points]))
        for handle, point in zip(self.handles, self.points):
            handle.setPos(*point)

    def fit(self):
        if self.image:
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        if .02 < self.transform().m11() * factor < 20:
            self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
            self.scale(factor, factor)
        event.accept()


class ImageEditorDialog(QDialog):
    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('Edit image'))
        self.resize(960, 780)
        self.original = image.convert("RGB").copy()
        self.current = self.original.copy()
        self.result_image = None
        layout = QVBoxLayout(self)
        tools = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems([tr('Crop'), tr('Dewarp · four corners')])
        tools.addWidget(self.mode)
        for title, callback in [("Rotate left", lambda: self.rotate(90)), ("Rotate right", lambda: self.rotate(-90)), ("Reset", self.reset)]:
            button = QPushButton(tr(title))
            button.clicked.connect(callback)
            tools.addWidget(button)
        tools.addStretch()
        self.apply_button = QPushButton(tr('Apply selection'))
        self.apply_button.clicked.connect(self.apply_selection)
        tools.addWidget(self.apply_button)
        layout.addLayout(tools)
        self.hint = QLabel(tr('Drag the corners to crop. Scroll to zoom.'))
        layout.addWidget(self.hint)
        self.canvas = EditCanvas()
        layout.addWidget(self.canvas, 1)
        buttons = QHBoxLayout()
        fit = QPushButton(tr('Fit'))
        fit.clicked.connect(self.canvas.fit)
        buttons.addWidget(fit)
        buttons.addStretch()
        cancel, use = QPushButton(tr('Cancel')), QPushButton(tr('Use image'))
        cancel.clicked.connect(self.reject)
        use.clicked.connect(self.accept_image)
        use.setDefault(True)
        buttons.addWidget(cancel)
        buttons.addWidget(use)
        layout.addLayout(buttons)
        self.mode.currentIndexChanged.connect(self.change_mode)
        self.canvas.set_image(self.current)

    def change_mode(self):
        self.canvas.mode = "Crop" if self.mode.currentIndex() == 0 else "Perspective"
        self.canvas.set_image(self.current)
        self.hint.setText((tr('Drag the corners to crop. Scroll to zoom.') if self.canvas.mode == "Crop" else tr('Place a handle on each page corner to straighten perspective. Curved paper is not flattened.')))

    def apply_selection(self):
        try:
            self.current = transform_image(self.current, self.canvas.points, self.canvas.mode)
            self.canvas.set_image(self.current)
            return True
        except ValueError as exc:
            QMessageBox.warning(self, tr('Adjust the selection'), str(exc))
            return False

    def rotate(self, angle):
        self.current = self.current.rotate(angle, expand=True)
        self.canvas.set_image(self.current)

    def reset(self):
        self.current = self.original.copy()
        self.canvas.set_image(self.current)

    def accept_image(self):
        if self.canvas.points != full_corners(self.current) and not self.apply_selection():
            return
        self.result_image = self.current.copy()
        self.accept()
