"""Render AI input in an isolated process: PyMuPDF handles never cross threads."""
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from threading import Lock
from pathlib import Path
from io import BytesIO
import atexit

_pool = None
_lock = Lock()


def render_file(path, frame, password=''):
    import pymupdf
    from PIL import Image, ImageOps
    if Path(path).suffix.lower() == '.pdf':
        with pymupdf.open(path) as pdf:
            if pdf.needs_pass and not pdf.authenticate(password):
                raise ValueError('Unable to unlock PDF for rendering.')
            page = pdf[frame]
            scale = min(3.0, 3200 / max(page.rect.width, page.rect.height, 1))
            data = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False).tobytes('png')
    else:
        with Image.open(path) as original:
            original.seek(frame)
            image = ImageOps.exif_transpose(original)
            image.thumbnail((3200, 3200), Image.Resampling.LANCZOS)
            rgba = image.convert('RGBA')
            flattened = Image.new('RGB', rgba.size, 'white')
            flattened.paste(rgba, mask=rgba.getchannel('A'))
            buffer = BytesIO(); flattened.save(buffer, format='PNG'); data = buffer.getvalue()
    if len(data) <= 12_000_000:
        return data, 'image/png'
    with Image.open(BytesIO(data)) as image:
        buffer = BytesIO(); image.convert('RGB').save(buffer, 'JPEG', quality=90)
        return buffer.getvalue(), 'image/jpeg'


def render_request(path, frame, password, cancel):
    """Called only by an AI worker. Waiting here never blocks Qt."""
    global _pool
    from concurrent.futures import TimeoutError
    if cancel.is_set():
        raise RuntimeError('Request cancelled.')
    with _lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(max_workers=1, mp_context=get_context('spawn'))
        future = _pool.submit(render_file, str(path), frame, password)
    while True:
        if cancel.is_set():
            future.cancel()
            raise RuntimeError('Request cancelled.')
        try:
            return future.result(timeout=0.1)
        except TimeoutError:
            continue


def shutdown():
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


atexit.register(shutdown)
