from io import BytesIO
import shutil
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
from book_translator.local_ocr import extract_image, parse_tsv, executable_path


class LocalOcrTests(unittest.TestCase):
    def test_words_paragraphs_confidence_and_coordinates(self):
        tsv = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        tsv += '5\t1\t1\t1\t1\t1\t10\t20\t50\t20\t95\tHello\n'
        tsv += '5\t1\t1\t1\t1\t2\t70\t20\t50\t20\t35\tworld\n'
        tsv += '5\t1\t1\t2\t1\t1\t10\t70\t50\t20\t80\tAgain\n'
        result = parse_tsv(tsv, 200, 100)
        self.assertEqual(result.text, 'Hello world\n\nAgain')
        self.assertAlmostEqual(result.confidence, .7)
        self.assertEqual(result.regions[1].box, (200, 350, 400, 600))
        self.assertEqual(result.regions[1].confidence, .35)

    def test_missing_engine_and_no_text(self):
        with self.assertRaisesRegex(RuntimeError, 'Tesseract was not found'):
            executable_path('/no-such-ocr-executable')
        with self.assertRaisesRegex(RuntimeError, 'found no text'):
            parse_tsv('level\ttext\n', 100, 100)

    def test_cancel_before_work(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            extract_image(b'', '', 'eng', cancel)

    @unittest.skipUnless(shutil.which('tesseract'), 'Tesseract not installed')
    def test_real_ocr_and_missing_language(self):
        image = Image.new('RGB', (1200, 240), 'white')
        try:
            font = ImageFont.truetype('DejaVuSans.ttf', 52)
        except OSError:
            font = ImageFont.load_default(size=52)
        ImageDraw.Draw(image).text((50, 70), 'Book translation works offline.', fill='black', font=font)
        buffer = BytesIO(); image.save(buffer, 'PNG')
        report = extract_image(buffer.getvalue(), '', 'eng', threading.Event())
        self.assertIn('Book translation works offline', report.text)
        self.assertTrue(report.regions)
        with self.assertRaisesRegex(RuntimeError, 'language data'):
            extract_image(buffer.getvalue(), '', 'missinglangxyz', threading.Event())
