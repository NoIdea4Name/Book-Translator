from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import pymupdf

from book_translator.core import (
    BookDocument,
    PasswordRequired,
    atomic_write,
    export_text,
    read_project,
    save_project,
)


def make_pdf(path):
    with pymupdf.open() as pdf:
        for text in (
            "Chapter One\nThe rain stopped at dawn.",
            "Chapter Two\nA small boat crossed the bay.",
            "",
        ):
            page = pdf.new_page(width=420, height=595)
            if text:
                page.insert_text((40, 70), text, fontsize=14)
        pdf.save(path)


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pdf = self.root / "book.pdf"
        make_pdf(self.pdf)
        self.book = BookDocument.open([self.pdf])

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def test_extract_correct_page_and_blank_page(self):
        self.assertEqual(len(self.book.pages), 3)
        self.assertIn("rain stopped", self.book.extract_pdf_text(0))
        self.assertNotIn("rain", self.book.extract_pdf_text(1))
        self.assertIn("small boat", self.book.extract_pdf_text(1))
        self.assertEqual(self.book.extract_pdf_text(2), "")

    def test_pdf_render_is_bounded_valid_image(self):
        with Image.open(BytesIO(self.book.render_page(0, 800))) as image:
            self.assertLessEqual(max(image.size), 802)
            self.assertEqual(image.mode, "RGB")
        with self.assertRaises(IndexError):
            self.book.render_page(3)

    def test_natural_image_order_and_multiframe_tiff(self):
        files = []
        for name in ("page10.png", "page2.png", "page1.png"):
            path = self.root / name
            Image.new("RGBA", (40, 70), (255, 0, 0, 128)).save(path)
            files.append(path)
        book = BookDocument.open(files)
        self.assertEqual([p.name for p in book.paths], ["page1.png", "page2.png", "page10.png"])
        with Image.open(BytesIO(book.render_page(0))) as image:
            self.assertEqual(image.mode, "RGB")
        with self.assertRaises(ValueError):
            book.extract_pdf_text(0)
        book.close()
        path = self.root / "scan.tiff"
        Image.new("RGB", (10, 20), "red").save(
            path, save_all=True, append_images=[Image.new("RGB", (10, 20), "blue")]
        )
        book = BookDocument.open([path])
        self.assertEqual(len(book.pages), 2)
        with Image.open(BytesIO(book.render_page(1))) as image:
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 255))
        book.close()

    def test_project_round_trip_unicode_and_relative_sources(self):
        page = self.book.pages[1]
        page.source = "日本語の本"
        page.translation = "Một cuốn sách tiếng Nhật"
        page.translated_from = page.source
        page.target_language = "Vietnamese"
        path = self.root / "work.btproject"
        save_project(path, self.book, 1, "Vietnamese", "gemini-2.5-flash")
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["files"], ["book.pdf"])
        self.assertNotIn("api_key", raw)
        project = read_project(path)
        self.assertEqual(project.paths, [self.pdf])
        self.assertEqual(project.pages[1], page)
        self.assertEqual(project.index, 1)
        self.assertFalse(project.pages[1].translation_is_stale)
        page.source += " updated"
        self.assertTrue(page.translation_is_stale)
        self.assertIn("Source edited after translation", export_text(self.book))
        self.assertIn("[Not translated]", export_text(self.book))

    def test_atomic_save_failure_preserves_existing_project(self):
        path = self.root / "work.btproject"
        path.write_text("old project", encoding="utf-8")
        with patch("book_translator.core.os.replace", side_effect=OSError("Disk error")):
            with self.assertRaises(OSError):
                atomic_write(path, "new project")
        self.assertEqual(path.read_text(), "old project")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_bad_project_is_rejected(self):
        path = self.root / "bad.btproject"
        path.write_text('{"format": "something-else"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            read_project(path)
        save_project(path, self.book, 0, "Vietnamese", "gemini-2.5-flash")
        data = json.loads(path.read_text())
        data["pages"][0]["source"] = 3
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            read_project(path)

    def test_encrypted_pdf_password(self):
        path = self.root / "encrypted.pdf"
        with pymupdf.open(self.pdf) as pdf:
            pdf.save(
                path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="reader"
            )
        with self.assertRaises(PasswordRequired):
            BookDocument.open([path])
        with self.assertRaises(PasswordRequired):
            BookDocument.open([path], "wrong")
        book = BookDocument.open([path], "reader")
        self.assertIn("Chapter One", book.extract_pdf_text(0))
        book.close()


if __name__ == "__main__":
    unittest.main()
