"""Direct image translation must not require or emit an extraction result."""
import unittest
from unittest.mock import patch
from book_translator.confidence import Assessment, RegionAssessment
from book_translator.gemini_service import GeminiService
from book_translator.openai_service import OpenAIService
from book_translator.nvidia_service import NvidiaService
from book_translator.prompts import translation_instructions


class ImageTranslationTests(unittest.TestCase):
    def test_provider_image_only_path_and_region_boxes(self):
        for cls in (GeminiService, OpenAIService, NvidiaService):
            with self.subTest(provider=cls.__name__):
                service = cls('key', 'test/model' if cls is NvidiaService else 'test-model')
                report = Assessment('Hello', .9, [RegionAssessment('', 'Hello', .7, 'Blurry', (1,2,300,400))])
                with patch.object(service, '_generate', return_value=report) as generate, patch.object(service, 'extract') as extract, patch.object(service, 'extract_and_translate') as combined:
                    result = service.translate(' \n', 'English', b'image', 'image/png')
                generate.assert_called_once()
                extract.assert_not_called()
                combined.assert_not_called()
                self.assertIn('IMAGE is authoritative', generate.call_args.args[1])
                self.assertNotIn('source TEXT is authoritative', generate.call_args.args[1])
                self.assertEqual(result.regions[0].box, (1,2,300,400))
                self.assertEqual(result.text, 'Hello')

    def test_text_keeps_priority_and_both_inputs_missing_are_rejected(self):
        for cls in (GeminiService, OpenAIService, NvidiaService):
            service = cls('key', 'test/model' if cls is NvidiaService else 'test-model')
            with patch.object(service, '_generate', return_value=Assessment('Hello', .9, [])) as generate:
                service.translate('Edited source', 'English', b'image')
                self.assertIn('source TEXT is authoritative', generate.call_args.args[1])
                generate.reset_mock()
                with self.assertRaises(Exception):
                    service.translate('', 'English')
                generate.assert_not_called()

    def test_bad_output_quote_still_loses_its_box(self):
        service = GeminiService('key', 'model')
        report = Assessment('Hello', .9, [RegionAssessment('', 'Invented', .7, '', (1,2,300,400))])
        with patch.object(service, '_generate', return_value=report):
            result = service.translate('', 'English', b'image')
        self.assertEqual(result.regions[0].box, ())
