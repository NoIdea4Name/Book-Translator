"""Same-response memory: schemas, parsing, batches and failure isolation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from PIL import Image
from PySide6.QtWidgets import QApplication
from book_translator.confidence import Assessment, RESULT_SCHEMA
from book_translator.chat import ChatReply, CHAT_SCHEMA
from book_translator.core import BookDocument
from book_translator.gemini_service import GeminiService
from book_translator.memory import MemoryOptions, memory_contract, parse_memory
from book_translator.preferences import GenerationOptions
from book_translator.ui import MainWindow


class IncrementalSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_contract_and_transient_metadata(self):
        options = MemoryOptions(previous_summary='Mina is a pilot', memory_page='2')
        schema, prompt = memory_contract(options, RESULT_SCHEMA, 'Translate')
        self.assertIn('book_summary', schema['required'])
        self.assertNotIn('book_summary', RESULT_SCHEMA['properties'])
        self.assertIn('Mina is a pilot', prompt)
        report = parse_memory(dict(text='Hello', confidence=.9, regions=[], book_summary='Mina flies'), Assessment.from_dict, options)
        self.assertEqual(report.memory_update, ('Mina is a pilot', 'Mina flies', ''))
        self.assertNotIn('memory_update', asdict(report))

    def test_invalid_memory_keeps_valid_text(self):
        for value in (None, '', 7, 'x' * 16001):
            report = parse_memory(dict(text='Valid', confidence=.9, regions=[], book_summary=value), Assessment.from_dict, MemoryOptions(previous_summary='Old'))
            self.assertEqual(report.text, 'Valid')
            self.assertIsNone(report.memory_update[1])
            self.assertTrue(report.memory_update[2])

    def test_chat_uses_same_contract(self):
        options = MemoryOptions(previous_summary='Old')
        schema, _ = memory_contract(options, CHAT_SCHEMA, 'Chat')
        reply = parse_memory(dict(answer='Explanation', replacement=None, book_summary='New'), ChatReply.from_dict, options)
        self.assertIn('book_summary', schema['required'])
        self.assertEqual(reply.memory_update[1], 'New')
        self.assertEqual(reply.answer, 'Explanation')
        self.assertNotIn('memory_update', asdict(reply))

    def test_disabled_has_no_summary_field(self):
        schema, prompt = memory_contract(GenerationOptions(auto_update_summary=False), RESULT_SCHEMA, 'Original')
        self.assertNotIn('book_summary', schema['properties'])
        self.assertEqual(prompt, 'Original')

    def test_batch_updates_memory_without_any_summarize_call(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for i in range(2):
                path = Path(folder) / f'{i}.png'
                Image.new('RGB', (80, 100), 'white').save(path)
                paths.append(path)
            window = MainWindow()
            window.api_profiles = []
            window.api_key = 'test-key'
            window.generation_options = GenerationOptions(context_enabled=False)
            window.set_book(BookDocument.open(paths))
            window.book.ai_summary = 'Original'
            for i, page in enumerate(window.book.pages):
                page.source = f'SOURCE_{i}'
            window._show_page(render=False)
            contexts = []
            def translate(service, source, target, *images):
                contexts.append(service.options)
                return parse_memory(dict(text='Translated '+source, confidence=.9, regions=[],
                                         book_summary=f'Memory {len(contexts)}'), Assessment.from_dict, service.options)
            try:
                with patch.object(GeminiService, 'translate', autospec=True, side_effect=translate) as call, patch.object(GeminiService, 'summarize') as summary:
                    window.thumbnails.selectAll()
                    window.translate()
                    end = time.monotonic() + 4
                    while window._busy and time.monotonic() < end:
                        self.app.processEvents()
                        window._poll_results()
                        time.sleep(.005)
                    self.assertFalse(window._busy)
                    self.assertEqual(call.call_count, 2)
                    summary.assert_not_called()
                self.assertEqual(contexts[1].previous_summary, 'Memory 1')
                self.assertIn('Memory 1', contexts[1].common)
                self.assertEqual(window.book.ai_summary, 'Memory 2')
            finally:
                window.cancel_request()
                window.dirty = False
                window.close()

    def test_all_providers_return_memory_in_one_generation_request(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from book_translator.openai_service import OpenAIService
        from book_translator.nvidia_service import NvidiaService
        options = MemoryOptions(previous_summary='Earlier memory', memory_page='2')
        for cls, model in ((GeminiService, 'gemini-test'), (OpenAIService, 'gpt-test'), (NvidiaService, 'test/model')):
            for task in ('extract', 'translate', 'extract_and_translate'):
                with self.subTest(provider=cls.__name__, task=task):
                    assessment = dict(text='Text', confidence=.95, regions=[])
                    data = dict(extraction=assessment, translation=assessment) if task == 'extract_and_translate' else dict(assessment)
                    data['book_summary'] = 'Updated memory'
                    service = cls('test-key', model, options)
                    if cls is GeminiService:
                        client = MagicMock()
                        client.models.generate_content.return_value = SimpleNamespace(candidates=[SimpleNamespace(finish_reason='STOP')], text=json.dumps(data))
                        manager = MagicMock()
                        manager.__enter__.return_value = client
                        transport = patch.object(service, '_client', return_value=manager)
                    else:
                        response = ({'status':'completed', 'output':[{'type':'message', 'content':[{'type':'output_text', 'text':json.dumps(data)}]}]}
                                    if cls is OpenAIService else {'choices':[{'finish_reason':'stop', 'message':{'content':json.dumps(data)}}]})
                        transport = patch.object(service, '_request', return_value=response)
                    with transport as request, patch.object(service, 'summarize') as summary:
                        args = ('Source', 'English') if task == 'translate' else (b'image', 'image/png', 'English') if task == 'extract_and_translate' else (b'image', 'image/png')
                        result = getattr(service, task)(*args)
                        summary.assert_not_called()
                    self.assertEqual(result.memory_update[1], 'Updated memory')
                    if cls is GeminiService:
                        client.models.generate_content.assert_called_once()
                        config = client.models.generate_content.call_args.kwargs['config']
                        self.assertIn('book_summary', config.response_json_schema['required'])
                    else:
                        request.assert_called_once()
                        self.assertIn('book_summary', json.dumps(request.call_args.kwargs['json']))
