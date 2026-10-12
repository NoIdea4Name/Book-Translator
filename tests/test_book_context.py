import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication

from book_translator.book_context import (
    book_signature, options_for_page, reference_context, summary_material,
)
from book_translator.confidence import Assessment
from book_translator.core import BookDocument, save_project, read_project
from book_translator.preferences import GenerationOptions
from book_translator.gemini_service import GeminiService
from book_translator.openai_service import OpenAIService
from book_translator.nvidia_service import NvidiaService
from book_translator.summary_panel import SummaryDialog
from book_translator.settings import SettingsDialog
from book_translator.ui import MainWindow


class ContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = []
        for n in range(4):
            path = Path(self.temp.name) / f"page-{n}.png"
            Image.new("RGB", (80, 100), "white").save(path)
            self.paths.append(path)
        self.book = BookDocument.open(self.paths)
        for n, page in enumerate(self.book.pages):
            page.source, page.translation = f"SOURCE_{n}", f"TRANSLATION_{n}"
        self.book.ai_summary = "Mina: main character. Keep names unchanged."

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def test_range_edges_off_and_order(self):
        options = GenerationOptions()
        data = json.loads(reference_context(self.book, 1, options))
        self.assertEqual([p["page"] for p in data["nearby_pages"]], [1, 3])
        self.assertNotIn("SOURCE_1", json.dumps(data))
        data = json.loads(reference_context(self.book, 0, options))
        self.assertEqual([p["page"] for p in data["nearby_pages"]], [2])
        self.assertEqual(reference_context(self.book, 1, GenerationOptions(context_enabled=False, summary_enabled=False)), "")
        data = json.loads(reference_context(self.book, 1, GenerationOptions(context_enabled=False)))
        self.assertEqual(set(data), {"book_summary"})
        self.book.reorder([3, 2, 1, 0])
        data = json.loads(reference_context(self.book, 1, GenerationOptions(pages_before=0, pages_after=2)))
        self.assertEqual([p["source"] for p in data["nearby_pages"]], ["SOURCE_1", "SOURCE_0"])

    def test_clipping_and_summary_chunks_cover_all_text(self):
        self.book.pages[0].source = "X" * 80000 + "THE_END"
        data = json.loads(reference_context(self.book, 1, GenerationOptions()))
        self.assertTrue(data["nearby_pages"][0]["source_clipped"])
        self.assertTrue(data["nearby_pages"][0]["source"].endswith("THE_END"))
        chunks = summary_material(self.book, 0)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 30000 for chunk in chunks))
        self.assertIn("X" * 80000 + "THE_END", "".join(chunks))
        self.assertNotIn("SOURCE_1", "".join(chunks))

    def test_project_roundtrip_and_old_project(self):
        self.book.summary_signature = book_signature(self.book)
        path = Path(self.temp.name) / "saved.btproject"
        save_project(path, self.book, 1, "Vietnamese", "gemini-test")
        loaded = read_project(path)
        self.assertEqual(loaded.ai_summary, self.book.ai_summary)
        restored = BookDocument.open(self.paths)
        restored.restore_project(loaded)
        self.assertEqual(restored.summary_signature, self.book.summary_signature)
        restored.close()
        data = json.loads(path.read_text())
        del data["ai_summary"], data["summary_signature"]
        path.write_text(json.dumps(data))
        self.assertEqual(read_project(path).ai_summary, "")

    def test_providers_receive_reference_and_summary_contract(self):
        options = options_for_page(self.book, 1, GenerationOptions())
        for cls, model in ((GeminiService, "gemini-test"), (OpenAIService, "gpt-test"), (NvidiaService, "test/model")):
            service = cls("test-key", model, options)
            with patch.object(service, "_generate", return_value=Assessment("Result", .9, [])) as request:
                service.translate("Current source", "Vietnamese")
                instruction = request.call_args.args[1]
                self.assertIn("SOURCE_0", instruction)
                self.assertIn("SOURCE_2", instruction)
                self.assertIn("Mina", instruction)
                self.assertNotIn("SOURCE_1", instruction)
            service = cls("test-key", model, GenerationOptions())
            with patch.object(service, "_generate", return_value=Assessment("Names: Mina", .9, [])) as request:
                self.assertEqual(service.summarize("Book text", "Earlier notes", "Vietnamese"), "Names: Mina")
                self.assertIn("Earlier notes", request.call_args.args[0])

    def test_settings_context_controls_save_and_pdf_fallback(self):
        dialog = SettingsDialog(None, "test-key", "gemini-test")
        dialog.context_enabled.setChecked(False)
        dialog.pages_before.setValue(2)
        dialog.pages_after.setValue(3)
        dialog.summary_enabled.setChecked(False)
        self.assertFalse(dialog.pages_before.isEnabled())
        with patch("book_translator.settings.save_settings", return_value="") as save:
            dialog.accept()
        options = save.call_args.args[0]
        self.assertEqual((options.context_enabled, options.pages_before, options.pages_after, options.summary_enabled),
                         (False, 2, 3, False))
        self.book.pages[0].source = ""
        self.book.refs[0] = (Path("embedded.pdf"), 0)
        with patch.object(self.book, "extract_pdf_text", return_value="Embedded fallback"):
            data = reference_context(self.book, 1, GenerationOptions())
            self.assertIn("Embedded fallback", data)
        self.assertEqual(self.book.pages[0].source, "")

    def test_context_validation(self):
        for values in ({"pages_before": -1}, {"pages_after": 11}, {"pages_before": True}, {"context_enabled": 1}):
            with self.assertRaises(ValueError):
                GenerationOptions(**values)

    def test_summary_draft_save_cancel_and_original_page_context(self):
        window = MainWindow()
        window.api_profiles = []
        window.api_key = "test-key"
        window.set_book(self.book)
        dialog = SummaryDialog(window)
        try:
            # Service context must follow the batch page, not the visible page.
            self.assertIn("SOURCE_2", window._service(1).options.common)
            self.assertNotIn("SOURCE_1", window._service(1).options.common)
            with patch.object(GeminiService, "summarize", return_value="Names: Mina\nStyle: natural"):
                dialog.generate()
                end = time.monotonic() + 3
                while dialog.pending and time.monotonic() < end:
                    self.app.processEvents()
                    dialog.poll_results()
                    time.sleep(.01)
            self.assertFalse(dialog.pending)
            self.assertEqual(dialog.editor.toPlainText(), "Names: Mina\nStyle: natural")
            self.assertNotEqual(self.book.ai_summary, dialog.editor.toPlainText())
            dialog.accept()
            self.assertEqual(self.book.ai_summary, "Names: Mina\nStyle: natural")
            self.assertTrue(window.dirty)
            cancelled = SummaryDialog(window)
            cancelled.editor.setPlainText("Discard this")
            cancelled.reject()
            self.assertNotEqual(self.book.ai_summary, "Discard this")
        finally:
            dialog.reject()
            window.dirty = False
            window.close()
