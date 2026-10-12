"""Temporary, token-protected LAN receiver. No Qt or Gemini code runs here."""
from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import queue
import secrets
import tempfile
import threading
from urllib.parse import urlsplit
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 20 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_IMAGES = 100
MAX_SESSION_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True)
class ReceivedImage:
    id: str
    path: Path


class PhoneReceiver:
    """Owns an HTTP server and staging files until the desktop dialog closes."""

    def __init__(self, host="0.0.0.0", port=0):
        self.token = secrets.token_urlsafe(24)
        self.batches = queue.Queue()
        self._temp = tempfile.TemporaryDirectory(prefix="book-translator-phone-")
        self._lock = threading.Lock()
        self._images = {}
        self._sizes = {}
        self._upload_ids = {}
        self._committed = set()
        self._receipts = {}
        self._bytes = 0
        self._closed = False
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Do not log session tokens or photos.

            def setup(self):
                super().setup()
                self.connection.settimeout(20)

            def reply(self, status, data, content_type="application/json"):
                body = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                prefix = f"/{receiver.token}/"
                return urlsplit(self.path).path.startswith(prefix)

            def do_GET(self):
                if not self.authorized() or urlsplit(self.path).path != f"/{receiver.token}/":
                    self.reply(404, {"error": "This receiving session is unavailable."})
                    return
                self.reply(200, Path(__file__).with_name("assets").joinpath("phone.html").read_bytes(), "text/html; charset=utf-8")

            def do_POST(self):
                if not self.authorized():
                    self.reply(404, {"error": "This receiving session is unavailable."})
                    return
                origin = self.headers.get("Origin")
                if origin and origin != f"http://{self.headers.get('Host')}":
                    self.reply(403, {"error": "Use the receiving page to send photos."})
                    return
                try:
                    route = urlsplit(self.path).path.rsplit("/", 1)[-1]
                    if route not in {"upload", "commit"}:
                        self.reply(404, {"error": "Unknown action."})
                        return
                    size = int(self.headers.get("Content-Length", "0"))
                    limit = MAX_UPLOAD if route == "upload" else 16_384
                    if not 0 < size <= limit:
                        self.reply(413, {"error": "Photo is too large (20 MB maximum)."})
                        return
                    upload_key = self.headers.get("X-Photo-Id")
                    if upload_key is not None and (not upload_key.isalnum() or len(upload_key) > 80):
                        raise ValueError("Invalid photo identifier.")
                    # Serialize uploads to bound image decoding memory and session totals.
                    with receiver._lock:
                        if receiver._closed:
                            raise ValueError("The receiving session has closed.")
                        if route == "upload" and upload_key not in receiver._upload_ids and (len(receiver._images) >= MAX_IMAGES or receiver._bytes + size > MAX_SESSION_BYTES):
                            raise ValueError("Receiver is full. Process the received photos on the computer first.")
                        body = self.rfile.read(size)
                        if len(body) != size:
                            raise ValueError("Incomplete upload. Please retry.")
                        if route == "upload":
                            result = receiver._store(body, upload_key)
                        else:
                            result = receiver._commit(json.loads(body))
                    self.reply(200, result)
                except (ValueError, KeyError, TypeError, UnidentifiedImageError, Image.DecompressionBombError):
                    self.reply(400, {"error": "Could not accept these photos. Use JPG, PNG or WebP (up to 40 megapixels), or process pending photos on the computer and retry."})
                except (OSError, TimeoutError):
                    try:
                        self.reply(400, {"error": "Upload interrupted. Check Wi-Fi and retry."})
                    except OSError:
                        pass

        try:
            self.httpd = ThreadingHTTPServer((host, port), Handler)
        except Exception:
            self._temp.cleanup()
            raise
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        self.thread.start()

    def url(self, address):
        return f"http://{address}:{self.port}/{self.token}/"

    def _store(self, data, upload_key=None):
        if upload_key is not None and upload_key in self._upload_ids:
            return {"id": self._upload_ids[upload_key]}
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS or image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Unsupported photo.")
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((4000, 4000))
            identifier = uuid.uuid4().hex
            path = Path(self._temp.name) / f"{identifier}.png"
            image.save(path)
        size = path.stat().st_size
        if self._bytes + size > MAX_SESSION_BYTES:
            path.unlink()
            raise ValueError("Receiver is full.")
        self._images[identifier] = ReceivedImage(identifier, path)
        self._sizes[identifier] = size
        if upload_key is not None:
            self._upload_ids[upload_key] = identifier
        self._bytes += size
        return {"id": identifier}

    def _commit(self, data):
        if not isinstance(data, dict):
            raise ValueError("Invalid batch.")
        batch = data.get("batch")
        ids = data.get("ids")
        if not isinstance(batch, str) or not 1 <= len(batch) <= 80:
            raise ValueError("Invalid batch identifier.")
        if batch in self._receipts:
            if ids != self._receipts[batch]:
                raise ValueError("Batch changed.")
            return {"received": len(ids)}
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_IMAGES or any(not isinstance(i, str) for i in ids):
            raise ValueError("Invalid photo list.")
        if len(set(ids)) != len(ids) or any(i not in self._images or i in self._committed for i in ids):
            raise ValueError("Missing or repeated photo.")
        self._receipts[batch] = ids.copy()
        self._committed.update(ids)
        self.batches.put([self._images[i] for i in ids])
        return {"received": len(ids)}

    def release(self, images):
        with self._lock:
            for image in images:
                if self._images.pop(image.id, None) is not None:
                    self._bytes -= self._sizes.pop(image.id, 0)
                    image.path.unlink(missing_ok=True)
                    self._committed.discard(image.id)
                    self._upload_ids = {key: value for key, value in self._upload_ids.items() if value != image.id}
            self._bytes = max(0, self._bytes)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        with self._lock:
            self._closed = True
            self._temp.cleanup()
