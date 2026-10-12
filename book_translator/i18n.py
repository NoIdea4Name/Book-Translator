"""Editable JSON language packs. UI language never changes book text or AI prompts."""
import json
from pathlib import Path
from string import Formatter
from PySide6.QtCore import QSettings, QTranslator

LOCALES = Path(__file__).with_name('locales')
_messages = {}
_language = 'en'


def placeholders(text):
    return {(name, spec, conversion) for _, name, spec, conversion in Formatter().parse(text) if name is not None}


def language_packs():
    packs = {'en': 'English'}
    for path in sorted(LOCALES.glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('name'), str) and isinstance(data.get('messages'), dict):
                packs[path.stem] = data['name']
        except (OSError, ValueError, TypeError):
            continue
    return packs


def saved_language():
    return QSettings('BookTranslator', 'BookTranslator').value('interface_language', 'en')


def save_language(code):
    store = QSettings('BookTranslator', 'BookTranslator')
    store.setValue('interface_language', code)
    store.sync()
    if store.status() != QSettings.Status.NoError:
        raise OSError('Could not save interface language.')


def set_language(code):
    global _messages, _language
    _messages, _language = {}, 'en'
    if code == 'en' or code not in language_packs():
        return
    try:
        data = json.loads((LOCALES / (code + '.json')).read_text(encoding='utf-8'))
        for source, translated in data['messages'].items():
            try:
                if isinstance(translated, str) and translated and placeholders(source) == placeholders(translated):
                    _messages[source] = translated
            except ValueError:
                continue
        _language = code
    except (OSError, ValueError, TypeError):
        pass


def tr(source, **values):
    text = _messages.get(source, source)
    return text.format(**values) if values else text


class QtLanguage(QTranslator):
    """Translate standard controls only; preserve Qt's internal diagnostic strings."""
    CONTROL_CONTEXTS = {"QPlatformTheme", "QDialogButtonBox", "QMessageBox"}

    def isEmpty(self):
        return not bool(_messages)

    def translate(self, context, sourceText, disambiguation=None, n=-1):
        # A Python empty string is a real, empty translation, not "not found".
        # Some Qt image plugins use translated error strings to detect failure;
        # erasing those errors can make a plugin accept the wrong image format.
        if context == "QGuiApplication" and sourceText == "QT_LAYOUT_DIRECTION":
            return "LTR"
        if context in self.CONTROL_CONTEXTS:
            return _messages.get(sourceText.replace('&', ''), sourceText)
        return sourceText


def install_language(app):
    previous = getattr(app, "ui_translator", None)
    if previous is not None:
        app.removeTranslator(previous)
    app.ui_translator = None
    set_language(saved_language())
    # English uses Qt's original behavior, exactly as before localization.
    if _language != 'en':
        app.ui_translator = QtLanguage(app)
        app.installTranslator(app.ui_translator)
