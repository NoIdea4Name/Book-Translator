import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from book_translator import i18n


class LanguageTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language('en')

    def test_vietnamese_and_placeholders(self):
        i18n.set_language('vi')
        self.assertEqual(i18n.tr('Settings'), 'Cài đặt')
        self.assertEqual(i18n.tr('{count} pages selected', count=3), 'Đã chọn 3 trang')
        self.assertEqual(i18n.tr('Custom book content'), 'Custom book content')
        self.assertEqual(i18n.tr('low'), 'low')

    def test_all_pack_placeholders_match_template(self):
        english = json.loads((i18n.LOCALES/'en.json').read_text())['messages']
        vietnamese = json.loads((i18n.LOCALES/'vi.json').read_text())['messages']
        self.assertEqual(set(english), set(vietnamese))
        for key, value in vietnamese.items():
            self.assertEqual(i18n.placeholders(key), i18n.placeholders(value), key)

    def test_external_pack_discovery_and_safe_fallback(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(i18n, 'LOCALES', Path(folder)):
            Path(folder,'fr.json').write_text(json.dumps({'name':'Français','messages':{'Settings':'Paramètres','{count} pages':'{wrong} pages'}}))
            Path(folder,'broken.json').write_text('{broken')
            self.assertIn('fr',i18n.language_packs())
            self.assertNotIn('broken',i18n.language_packs())
            i18n.set_language('fr')
            self.assertEqual(i18n.tr('Settings'),'Paramètres')
            self.assertEqual(i18n.tr('{count} pages',count=2),'2 pages')
            i18n.set_language('../vi')
            self.assertEqual(i18n.tr('Settings'),'Settings')
