"""Credential persistence tests use an isolated fake vault, never real user keys."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from book_translator.api_rotation import ApiProfile
from book_translator.preferences import GenerationOptions
from book_translator.secure_settings import SettingsStore, SettingsError, SERVICE
from book_translator.settings import SettingsDialog
from book_translator.ui import MainWindow


class FakeVault:
    def __init__(self):
        self.secrets = {}
        self.fail_write = False
        self.locked = False
        self.fail_delete = False

    def get_password(self, service, account):
        if self.locked: raise RuntimeError('SECRET_BACKEND_ERROR')
        return self.secrets.get((service, account))

    def set_password(self, service, account, password):
        if self.fail_write: raise RuntimeError(password)
        self.secrets[service, account] = password

    def delete_password(self, service, account):
        if self.fail_delete: raise RuntimeError('SECRET_BACKEND_ERROR')
        self.secrets.pop((service, account), None)


class SecureSettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'settings.json'
        self.vault = FakeVault()
        self.store = SettingsStore(self.path, self.vault)
        self.options = GenerationOptions(.3, 'Preserve names', 'Keep lines', 'Natural Vietnamese')
        self.profiles = [ApiProfile('nvidia', 'moonshotai/kimi-k3', 'SECRET_ONE', False, 'low'),
                         ApiProfile('openai', 'gpt-4.1-mini', 'SECRET_TWO')]

    def save(self):
        return self.store.save(self.options, self.profiles, False, 2)

    def test_round_trip_restarts_and_no_plaintext_keys(self):
        self.assertIsNone(self.store.load())
        self.assertEqual(self.save(), '')
        restored = SettingsStore(self.path, self.vault).load()
        self.assertEqual(restored.profiles, self.profiles)
        self.assertEqual(restored.options, self.options)
        self.assertFalse(restored.rotation_enabled)
        self.assertEqual(restored.failure_limit, 2)
        raw = self.path.read_text()
        self.assertNotIn('SECRET_ONE', raw)
        self.assertNotIn('SECRET_TWO', raw)
        self.assertNotIn('api_key', raw)
        self.assertEqual(len(self.vault.secrets), 2)

    def test_reorder_edit_remove_and_clear_keys_remove_old_credentials(self):
        self.save()
        old_ids = set(self.vault.secrets)
        edited = [ApiProfile('openai', 'gpt-4.1-mini', 'NEW_KEY')]
        self.store.save(self.options, edited, True, 4)
        self.assertTrue(old_ids.isdisjoint(self.vault.secrets))
        self.assertEqual(list(self.vault.secrets.values()), ['NEW_KEY'])
        self.store.save(self.options, [ApiProfile('gemini', 'gemini-model', '')], True, 3)
        self.assertEqual(self.vault.secrets, {})
        self.assertEqual(self.store.load().profiles[0].api_key, '')

    def test_vault_failure_preserves_previous_settings_and_redacts_errors(self):
        self.save()
        before = self.path.read_bytes()
        secrets = self.vault.secrets.copy()
        self.vault.fail_write = True
        with self.assertRaises(SettingsError) as error: self.save()
        self.assertNotIn('SECRET', str(error.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.vault.secrets, secrets)

    def test_disk_failure_rolls_back_staged_credentials(self):
        self.save()
        before = self.path.read_bytes()
        secrets = self.vault.secrets.copy()
        with patch.object(self.store, '_commit', side_effect=OSError('SECRET_ONE')):
            with self.assertRaises(SettingsError) as error: self.save()
        self.assertNotIn('SECRET_ONE', str(error.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.vault.secrets, secrets)

    def test_locked_store_load_preserves_profiles_and_cannot_save_blanks(self):
        self.save()
        before = self.path.read_bytes()
        self.vault.locked = True
        loaded = self.store.load()
        self.assertTrue(loaded.warning)
        self.assertNotIn('SECRET', loaded.warning)
        self.assertEqual([p.api_key for p in loaded.profiles], ['', ''])
        self.assertEqual(loaded.profiles[0].provider, 'nvidia')
        with self.assertRaises(SettingsError):
            self.store.save(loaded.options, loaded.profiles, True, 3)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_key_reports_affected_entry_without_dropping_other_settings(self):
        self.save()
        first_id = json.loads(self.path.read_text())['profiles'][0]['credential_id']
        del self.vault.secrets[SERVICE, first_id]
        restored = self.store.load()
        self.assertIn('entries 1', restored.warning)
        self.assertEqual(restored.profiles[1].api_key, 'SECRET_TWO')
        self.assertEqual(restored.options, self.options)

    def test_corrupt_or_future_file_is_not_silently_overwritten(self):
        for value in ('bad json', '{"version":99}', '{"version":1,"profiles":[]}'):
            self.path.write_text(value)
            with self.assertRaises(SettingsError): self.store.load()
            with self.assertRaises(SettingsError): self.save()
            self.assertEqual(self.path.read_text(), value)
            self.assertEqual(self.vault.secrets, {})

    def test_failed_old_credential_cleanup_reports_saved_state(self):
        self.save()
        self.vault.fail_delete = True
        warning = self.store.save(self.options, [self.profiles[0]], True, 3)
        self.assertIn('Settings saved', warning)
        self.assertEqual(self.store.load().profiles, [self.profiles[0]])
        self.assertNotIn('SECRET', warning)

    def test_secure_backend_unavailable_never_uses_plaintext_fallback(self):
        with patch('book_translator.secure_settings.native_vault', side_effect=SettingsError('Unavailable')):
            with self.assertRaises(SettingsError):
                SettingsStore(self.path).save(self.options, self.profiles, True, 3)
        self.assertFalse(self.path.exists())


class SecureSettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        for widget in self.app.topLevelWidgets(): widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_save_failure_stays_open_and_cancel_never_saves(self):
        dialog = SettingsDialog(None, 'key', 'gemini-model')
        with patch('book_translator.settings.save_settings', side_effect=SettingsError('Locked')), patch('book_translator.settings.QMessageBox.warning') as warning:
            dialog.accept()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        warning.assert_called_once()
        with patch('book_translator.settings.save_settings') as save:
            dialog.reject()
        save.assert_not_called()

    def test_startup_restores_saved_profiles_options_and_rotation_over_environment(self):
        from book_translator.secure_settings import SavedSettings
        options = GenerationOptions(.4, 'Keep names')
        profiles = [ApiProfile('nvidia', 'moonshotai/kimi-k3', 'saved-key', False, 'high')]
        saved = SavedSettings(options, profiles, False, 4)
        with patch('book_translator.ui.load_settings', return_value=saved), patch.dict(os.environ, {'GEMINI_API_KEY': 'env-key'}):
            window = MainWindow()
        self.assertEqual(window.api_profiles, profiles)
        self.assertEqual(window.generation_options, options)
        self.assertFalse(window.rotation_enabled)
        self.assertEqual(window.failure_limit, 4)
        self.assertIsNone(window._verified_connection)
        self.assertIsNone(window._router)
        window.close()

    def test_startup_survives_unreadable_settings_with_visible_warning(self):
        with patch('book_translator.ui.load_settings', side_effect=SettingsError('Could not read saved settings')):
            window = MainWindow()
        self.assertIn('Could not read', window.statusBar().currentMessage())
        window.close()
