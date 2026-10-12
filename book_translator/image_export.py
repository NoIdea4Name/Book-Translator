"""Export a page image without preview overlays or changes to the workspace."""
from pathlib import Path
import re
import tempfile

from PIL import Image, ImageOps


def default_image_name(page_name):
    """Keep the page name, replacing characters Windows cannot use in filenames."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', page_name).strip().rstrip('. ')
    name = name or 'Page'
    if re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]', name.split('.')[0], re.IGNORECASE):
        name = '_' + name
    return name + '.png'


def save_page_image(book, index, destination):
    """Save full-resolution images, or a PDF page rendered up to 3200 pixels."""
    destination = Path(destination)
    path, frame = book.refs[index]
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.png', delete=False) as output:
            temporary = Path(output.name)
            if path.suffix.lower() == '.pdf':
                output.write(book.render_page(index, max_edge=3200))
            else:
                with Image.open(path) as original:
                    original.seek(frame)
                    image = ImageOps.exif_transpose(original)
                    if image.mode not in {'1', 'L', 'LA', 'P', 'RGB', 'RGBA', 'I', 'I;16'}:
                        image = image.convert('RGB')
                    image.save(output, format='PNG')
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_page_copy(book, index, folder, used_names):
    """Reserve a unique filename, so duplicate names never overwrite other files."""
    folder = Path(folder)
    filename = Path(default_image_name(book.page_name(index)))
    destination = folder / filename
    number = 2
    while True:
        if destination.name.casefold() not in used_names:
            try:
                with destination.open("xb"):
                    pass
                break
            except FileExistsError:
                pass
        used_names.add(destination.name.casefold())
        destination = folder / f"{filename.stem} ({number}).png"
        number += 1
    try:
        save_page_image(book, index, destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    used_names.add(destination.name.casefold())
    return destination
