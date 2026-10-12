"""Desktop review of photos received over Wi-Fi, and durable image-copy storage."""

from .i18n import tr
from datetime import datetime
from pathlib import Path
import queue
import shutil
import uuid

import qrcode
from PySide6.QtCore import QSize, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter, QVBoxLayout,
)

from .image_editor import ImageEditorDialog, image_pixmap, load_image
from .phone_server import PhoneReceiver
from .preview import PageView


def image_storage():
    """Persistent local copies are referenced by projects, so never use temp here."""
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)) / "images"


def save_image_copies(paths, folder=None):
    """Create a new batch directory; clean incomplete copies on write failure."""
    root = Path(folder) if folder is not None else image_storage()
    destination = root / f"Pages-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    destination.mkdir(parents=True)
    saved = []
    try:
        for index, path in enumerate(paths, 1):
            target = destination / f"Page-{index:03d}.png"
            shutil.copyfile(path, target)
            saved.append(target)
        return saved
    except Exception:
        shutil.rmtree(destination)
        raise


def save_edited_image(image):
    folder = image_storage() / "edited"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Page-{uuid.uuid4().hex}.png"
    try:
        image.save(path, format="PNG")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def local_addresses():
    addresses = []
    for interface in QNetworkInterface.allInterfaces():
        flags = interface.flags()
        if not flags & QNetworkInterface.InterfaceFlag.IsUp or flags & QNetworkInterface.InterfaceFlag.IsLoopBack:
            continue
        for entry in interface.addressEntries():
            address = entry.ip()
            value = address.toString()
            if address.protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol and not value.startswith("169.254."):
                addresses.append(value)
    return list(dict.fromkeys(addresses)) or ["127.0.0.1"]


class PhoneImportDialog(QDialog):
    def __init__(self, import_images, parent=None, receiver=None):
        super().__init__(parent)
        self.setWindowTitle(tr('Receive from phone'))
        self.resize(1000, 800)
        self.setMinimumSize(780, 620)
        self.import_images = import_images
        self.receiver = receiver or PhoneReceiver()
        self.pending = []
        self._stopped = False
        layout = QVBoxLayout(self)
        connect = QHBoxLayout()
        self.qr = QLabel()
        self.qr.setFixedSize(148, 148)
        connect.addWidget(self.qr)
        details = QVBoxLayout()
        title = QLabel(tr('Scan to send photos'))
        title.setStyleSheet("font-size:22px;font-weight:600")
        details.addWidget(title)
        details.addWidget(QLabel(tr('Connect both devices to the same Wi-Fi and keep this window open.')))
        network = QHBoxLayout()
        self.address = QComboBox()
        self.address.addItems(local_addresses())
        self.address.setToolTip(tr('Choose the computer’s Wi-Fi/LAN address if it has multiple networks.'))
        self.url = QLineEdit()
        self.url.setReadOnly(True)
        copy = QPushButton(tr('Copy link'))
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.url.text()))
        network.addWidget(self.address)
        network.addWidget(self.url, 1)
        network.addWidget(copy)
        details.addLayout(network)
        hint = QLabel(tr('If blocked, allow Python on your private network in the computer’s firewall.'))
        hint.setWordWrap(True)
        details.addWidget(hint)
        connect.addLayout(details, 1)
        layout.addLayout(connect)
        self.status = QLabel(tr('Waiting for photos…'))
        layout.addWidget(self.status)
        splitter = QSplitter()
        self.photos = QListWidget()
        self.photos.setIconSize(QSize(100, 112))
        self.photos.setMinimumWidth(165)
        self.photos.currentRowChanged.connect(self.show_photo)
        self.photos.itemDoubleClicked.connect(lambda _: self.edit_photo())
        splitter.addWidget(self.photos)
        self.preview = PageView()
        splitter.addWidget(self.preview)
        splitter.setSizes([190, 760])
        layout.addWidget(splitter, 1)
        actions = QHBoxLayout()
        self.edit_button = QPushButton(tr('Edit image…'))
        self.edit_button.clicked.connect(self.edit_photo)
        self.remove_button = QPushButton(tr('Remove'))
        self.remove_button.clicked.connect(self.remove_photo)
        actions.addWidget(self.edit_button)
        actions.addWidget(self.remove_button)
        actions.addStretch()
        self.save_button = QPushButton(tr('Save images…'))
        self.add_button = QPushButton(tr('Add to workspace'))
        self.both_button = QPushButton(tr('Save && add…'))
        self.both_button.setObjectName("primary")
        for button, mode in [(self.save_button, "save"), (self.add_button, "add"), (self.both_button, "both")]:
            button.clicked.connect(lambda checked=False, m=mode: self.process_images(m))
            actions.addWidget(button)
        layout.addLayout(actions)
        self.address.currentTextChanged.connect(self.update_link)
        self.update_link()
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.receive_batches)
        self.timer.start()
        self.refresh_buttons()

    def update_link(self):
        url = self.receiver.url(self.address.currentText())
        self.url.setText(url)
        qr = qrcode.make(url).convert("RGB")
        self.qr.setPixmap(image_pixmap(qr).scaled(148, 148, Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.FastTransformation))
        if self.address.currentText() == "127.0.0.1":
            self.status.setText(tr('No LAN address found. Connect to Wi-Fi and reopen this window.'))

    def receive_batches(self):
        received = False
        while True:
            try:
                batch = self.receiver.batches.get_nowait()
            except queue.Empty:
                break
            for image in batch:
                self.pending.append(image)
                thumb = load_image(image.path)
                thumb.thumbnail((100, 112))
                item = QListWidgetItem(QIcon(image_pixmap(thumb)), f"Photo {len(self.pending)}")
                self.photos.addItem(item)
            received = True
        if received:
            self.status.setText(tr('{p0} photo(s) ready. Save or add all received images below.', p0=len(self.pending)))
            if self.photos.currentRow() < 0:
                self.photos.setCurrentRow(0)
            self.refresh_buttons()

    def refresh_buttons(self):
        for button in (self.save_button, self.add_button, self.both_button):
            button.setEnabled(bool(self.pending))
        for button in (self.edit_button, self.remove_button):
            button.setEnabled(0 <= self.photos.currentRow() < len(self.pending))

    def show_photo(self, index):
        if 0 <= index < len(self.pending):
            self.preview.set_image(self.pending[index].path.read_bytes())
        else:
            self.preview.scene().clear()
            self.preview.item = None
        self.refresh_buttons()

    def edit_photo(self):
        index = self.photos.currentRow()
        if not 0 <= index < len(self.pending):
            return
        image = self.pending[index]
        try:
            dialog = ImageEditorDialog(load_image(image.path), self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            # Atomic replacement leaves the original staged photo intact on failure.
            replacement = image.path.with_suffix(".editing.png")
            dialog.result_image.save(replacement)
            replacement.replace(image.path)
            thumb = dialog.result_image.copy()
            thumb.thumbnail((100, 112))
            self.photos.item(index).setIcon(QIcon(image_pixmap(thumb)))
            self.show_photo(index)
        except Exception as exc:
            QMessageBox.warning(self, tr('Could not edit image'), str(exc))

    def remove_photo(self):
        index = self.photos.currentRow()
        if 0 <= index < len(self.pending):
            image = self.pending.pop(index)
            self.photos.takeItem(index)
            self.receiver.release([image])
            for row in range(self.photos.count()):
                self.photos.item(row).setText(tr('Photo {p0}', p0=row + 1))
            self.show_photo(self.photos.currentRow())
            self.status.setText(tr('{p0} photo(s) ready.', p0=len(self.pending)))

    def process_images(self, mode):
        if not self.pending:
            return
        # Snapshot: photos arriving during a folder picker belong to the next batch.
        batch = self.pending.copy()
        folder = None
        if mode in {"save", "both"}:
            folder = QFileDialog.getExistingDirectory(self, tr('Save received images'))
            if not folder:
                return
        saved = []
        try:
            saved = save_image_copies([image.path for image in batch], folder)
            if mode in {"add", "both"}:
                self.import_images(saved)
        except Exception as exc:
            if saved and mode == "add":
                shutil.rmtree(saved[0].parent)
            detail = f"\nImages were saved in {saved[0].parent}." if saved and mode == "both" else ""
            QMessageBox.warning(self, tr('Could not process images'), str(exc) + detail)
            return
        self.photos.blockSignals(True)
        for image in batch:
            index = self.pending.index(image)
            self.pending.pop(index)
            self.photos.takeItem(index)
        for row in range(self.photos.count()):
            self.photos.item(row).setText(tr('Photo {p0}', p0=row + 1))
        self.photos.blockSignals(False)
        self.receiver.release(batch)
        self.photos.setCurrentRow(0 if self.pending else -1)
        self.show_photo(self.photos.currentRow())
        verb = {"save": "Saved", "add": "Added", "both": "Saved and added"}[mode]
        location = f"  {saved[0].parent}" if mode != "add" else ""
        self.status.setText(tr('{p0} {p1} photo(s).{p2}', p0=verb, p1=len(batch), p2=location))
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def stop(self):
        if not self._stopped:
            self._stopped = True
            self.timer.stop()
            self.receiver.close()

    def reject(self):
        self.receive_batches()
        if self.pending:
            choice = QMessageBox.question(self, tr('Discard received photos?'),
                tr('{p0} photo(s) have not been saved or added. Discard them and close the receiver?', p0=len(self.pending)),
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Discard:
                return
        self.stop()
        super().reject()
