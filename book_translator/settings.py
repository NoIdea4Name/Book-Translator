"""API profiles, model discovery, and the connection-test dialog.

Network calls run in a worker; the timer applies their results on the UI thread.
"""

from .i18n import tr, language_packs, saved_language, save_language

import queue
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QCheckBox,
    QSpinBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QTabWidget,
    QWidget,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from .preferences import GenerationOptions
from .secure_settings import save_settings
from .config import DEFAULT_MODEL
from .widgets import button, label
from .api_errors import APIError
from .api_rotation import ApiProfile, create_service, redact, DEFAULT_MODELS
from .nvidia_service import DEFAULT_NVIDIA_MODEL


class SettingsDialog(QDialog):
    def __init__(self, parent, key, model, verified=None, options=None, profiles=None, rotation_enabled=True, failure_limit=3):
        super().__init__(parent)
        self.options = options or GenerationOptions()
        self.profiles = list(profiles or [ApiProfile("gemini", model, key)])
        self._editing = 0
        key, model = self.profiles[0].api_key, self.profiles[0].model
        self.setWindowTitle(tr('API settings'))
        self.setMinimumWidth(610)
        self.tested_connection = verified if verified == (key.strip(), model) else None
        self._checking = False
        self._check_results = queue.Queue()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(label(tr('API settings'), "title"))
        language_form = QFormLayout()
        self.interface_language = QComboBox()
        for code, name in language_packs().items():
            self.interface_language.addItem(name, code)
        self.interface_language.setCurrentIndex(max(0, self.interface_language.findData(saved_language())))
        language_form.addRow(tr("Interface language"), self.interface_language)
        language_form.addRow(label(tr("Restart the app to apply the interface language."), "muted"))
        layout.addLayout(language_form)
        outer = layout
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        connection = QWidget()
        self.tabs.addTab(connection, tr('Connection'))
        layout = QVBoxLayout(connection)
        layout.setContentsMargins(8, 14, 8, 8)
        layout.setSpacing(12)
        profile_row = QHBoxLayout()
        self.profile_selector = QComboBox()
        profile_row.addWidget(self.profile_selector, 1)
        self.add_profile_button = button(tr('Add'), self.add_profile)
        self.remove_profile_button = button(tr('Remove'), self.remove_profile)
        self.up_profile_button = button("↑", lambda: self.move_profile(-1), tooltip=tr('Move earlier in rotation'))
        self.down_profile_button = button("↓", lambda: self.move_profile(1), tooltip=tr('Move later in rotation'))
        for control in (self.add_profile_button, self.remove_profile_button, self.up_profile_button, self.down_profile_button):
            profile_row.addWidget(control)
        layout.addLayout(profile_row)
        form = QFormLayout()
        form.setSpacing(12)
        provider_row = QHBoxLayout()
        self.provider = QComboBox()
        self.provider.addItem(tr('Gemini'), "gemini")
        self.provider.addItem(tr('OpenAI / ChatGPT'), "openai")
        self.provider.addItem(tr('NVIDIA NIM'), "nvidia")
        self.provider.setCurrentIndex(self.provider.findData(self.profiles[0].provider))
        provider_row.addWidget(self.provider)
        self.send_temperature = QCheckBox(tr('Send temperature'))
        self.send_temperature.setChecked(self.profiles[0].send_temperature)
        self.send_temperature.setVisible(self.profiles[0].provider != "gemini")
        self.send_temperature.setToolTip(tr('Turn off to use the model’s default sampling. Kimi K3 accepts 0–1; larger temperature settings are capped at 1.'))
        provider_row.addWidget(self.send_temperature)
        form.addRow(tr('Provider'), provider_row)
        self.key = QLineEdit(key)
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText(tr('API key'))
        self.key.setToolTip(
            tr('Save keeps API keys in the OS credential store and remembers this API list. Keys are never written to projects or exports.')
        )
        key_row = QHBoxLayout()
        key_row.addWidget(self.key, 1)
        self.key_visibility = button(tr('Show'), self._toggle_key, "small", tr('Show or hide the API key'))
        self.key_visibility.setCheckable(True)
        key_row.addWidget(self.key_visibility)
        form.addRow(tr('API key'), key_row)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItem(model)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.lineEdit().setPlaceholderText(tr('Model ID'))
        self.model.setToolTip(tr('Load models available to this key, then test the selected model.'))
        form.addRow(tr('Model'), self.model)
        self.connection_form = form
        self.reasoning = QComboBox()
        self.reasoning.addItems(['low', 'high', 'max'])
        self.reasoning.setCurrentText(self.profiles[0].reasoning_effort)
        self.reasoning.setToolTip(tr('Kimi K3 reasoning budget. Low favors speed; max can take longer. Connection tests always use low.'))
        form.addRow(tr('Reasoning'), self.reasoning)
        form.setRowVisible(self.reasoning, self.profiles[0].provider == "nvidia" and model == DEFAULT_NVIDIA_MODEL)
        layout.addLayout(form)
        rotation = QHBoxLayout()
        self.rotation = QCheckBox(tr('Automatic rotation'))
        self.rotation.setChecked(rotation_enabled)
        self.rotation.setToolTip(tr('Try entries in order. Continue with the last working entry. Saving settings restarts at entry 1 and clears paused entries.'))
        rotation.addWidget(self.rotation)
        rotation.addStretch()
        rotation.addWidget(QLabel(tr('Failures before switch')))
        self.failure_limit = QSpinBox()
        self.failure_limit.setRange(1, 5)
        self.failure_limit.setValue(failure_limit)
        self.failure_limit.setToolTip(tr('Consecutive temporary failures per entry; quota, invalid key and configuration errors switch immediately.'))
        rotation.addWidget(self.failure_limit)
        layout.addLayout(rotation)
        checks = QHBoxLayout()
        self.load_models_button = button(tr('Load models'), self.load_models)
        self.test_button = button(
            tr('Test connection'),
            self.test_connection,
            "primary",
            tr('Sends a small sample image and JSON request. Uses API quota; no book content is sent.'),
        )
        checks.addWidget(self.load_models_button)
        checks.addWidget(self.test_button)
        layout.addLayout(checks)
        self.check_status = QPlainTextEdit()
        self.check_status.setReadOnly(True)
        self.check_status.setFixedHeight(138)
        self.check_status.setObjectName("connectionDetails")
        layout.addWidget(self.check_status)
        details = QHBoxLayout()
        link = QLabel(tr('<a href="https://aistudio.google.com/apikey">Gemini key</a> · <a href="https://platform.openai.com/api-keys">OpenAI key</a> · <a href="https://build.nvidia.com/moonshotai/kimi-k3">NVIDIA key</a>'))
        link.setOpenExternalLinks(True)
        details.addWidget(link)
        details.addStretch()
        self.copy_details = button(
            tr('Copy details'),
            self._copy_details,
            "small",
            tr('Copy connection details without the API key'),
        )
        details.addWidget(self.copy_details)
        layout.addLayout(details)
        output = QWidget()
        self.tabs.addTab(output, tr('Instructions && output'))
        output_layout = QVBoxLayout(output)
        output_layout.setContentsMargins(8, 14, 8, 8)
        temperature_form = QFormLayout()
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setDecimals(2)
        self.temperature.setSingleStep(0.1)
        self.temperature.setValue(self.options.temperature)
        self.temperature.setFixedWidth(100)
        self.temperature.setToolTip(
            tr('Lower values reduce randomness. Zero does not guarantee identical responses.')
        )
        temperature_form.addRow(tr('Temperature'), self.temperature)
        output_layout.addLayout(temperature_form)
        self.instruction_tabs = QTabWidget()
        self.common_instructions = QPlainTextEdit(self.options.common)
        self.extraction_instructions = QPlainTextEdit(self.options.extraction)
        self.translation_instructions = QPlainTextEdit(self.options.translation)
        for editor, title, hint in (
            (self.common_instructions, "Both", "Instructions for every AI task"),
            (self.extraction_instructions, "Extraction", "Example: Preserve the original line breaks."),
            (self.translation_instructions, "Translation", "Example: Use natural Vietnamese; keep character names unchanged."),
        ):
            editor.setPlaceholderText(tr(hint))
            editor.setAccessibleName(title + " instructions")
            editor.setMinimumHeight(160)
            self.instruction_tabs.addTab(editor, tr(title))
        output_layout.addWidget(self.instruction_tabs)
        output_layout.addWidget(label(
            tr('Extract & Translate uses all three instruction tabs. Offline PDF extraction uses none.'),
            "muted", True,
        ))
        output_layout.addStretch()
        context_tab = QWidget()
        self.tabs.addTab(context_tab, tr('Context'))
        context_layout = QVBoxLayout(context_tab)
        self.context_enabled = QCheckBox(tr('Include nearby pages'))
        self.context_enabled.setChecked(self.options.context_enabled)
        context_layout.addWidget(self.context_enabled)
        context_form = QFormLayout()
        self.pages_before = QSpinBox()
        self.pages_after = QSpinBox()
        for control, value, title in ((self.pages_before, self.options.pages_before, "Pages before"),
                                      (self.pages_after, self.options.pages_after, "Pages after")):
            control.setRange(0, 10)
            control.setValue(value)
            control.setEnabled(self.options.context_enabled)
            self.context_enabled.toggled.connect(control.setEnabled)
            context_form.addRow(tr(title), control)
        context_layout.addLayout(context_form)
        self.summary_enabled = QCheckBox(tr('Include book summary'))
        self.summary_enabled.setChecked(self.options.summary_enabled)
        context_layout.addWidget(self.summary_enabled)
        self.auto_update_summary = QCheckBox(tr('Update summary with AI responses'))
        self.auto_update_summary.setChecked(self.options.auto_update_summary)
        self.auto_update_summary.setToolTip(
            tr('Includes updated memory in the same extraction, translation or chat response. The next page in a batch receives the updated summary.')
        )
        context_layout.addWidget(self.auto_update_summary)
        self.fix_previous_page = QCheckBox(tr('Allow fixes to the previous translation ending'))
        self.fix_previous_page.setChecked(self.options.fix_previous_page)
        self.fix_previous_page.setEnabled(self.options.context_enabled)
        self.context_enabled.toggled.connect(self.fix_previous_page.setEnabled)
        self.fix_previous_page.setToolTip(tr('Translate and Extract & Translate may correct the preceding page’s ending for continuous narration. Requires Pages before ≥ 1 and an existing translation in the same language.'))
        context_layout.addWidget(self.fix_previous_page)
        context_layout.addWidget(label(
            tr('Uses available page text or embedded PDF text, not neighboring images. Book summary is editable from the Summary button. Context also applies to AI chat.'),
            "muted", True))
        context_layout.addStretch()
        layout = outer
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.key.textChanged.connect(self._invalidate_test)
        self.model.currentTextChanged.connect(self._invalidate_test)
        self.model.currentTextChanged.connect(self._update_provider_fields)
        self._check_timer = QTimer(self)
        self._check_timer.setInterval(100)
        self._check_timer.timeout.connect(self._poll_check)
        self._check_timer.start()
        self.finished.connect(self._check_timer.stop)
        self._refresh_profiles()
        self.profile_selector.currentIndexChanged.connect(self._change_profile)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.send_temperature.toggled.connect(self._invalidate_test)
        self.reasoning.currentTextChanged.connect(self._invalidate_test)
        self._set_status((tr('Connection verified · {p0}', p0=model) if self.tested_connection else ""))

    def _read_profile(self):
        return ApiProfile(self.provider.currentData(), self.model.currentText(), self.key.text(), self.send_temperature.isChecked(), self.reasoning.currentText())

    def _refresh_profiles(self):
        self.profile_selector.blockSignals(True)
        self.profile_selector.clear()
        self.profile_selector.addItems([f"{i + 1}. {profile.label}" for i, profile in enumerate(self.profiles)])
        self.profile_selector.setCurrentIndex(self._editing)
        self.profile_selector.blockSignals(False)
        self.add_profile_button.setEnabled(len(self.profiles) < 10)
        self.remove_profile_button.setEnabled(len(self.profiles) > 1)
        self.up_profile_button.setEnabled(self._editing > 0)
        self.down_profile_button.setEnabled(self._editing + 1 < len(self.profiles))

    def _load_profile(self):
        profile = self.profiles[self._editing]
        self.provider.blockSignals(True)
        self.provider.setCurrentIndex(self.provider.findData(profile.provider))
        self.provider.blockSignals(False)
        self.key.setText(profile.api_key)
        self.model.clear()
        self.model.addItem(profile.model)
        self.send_temperature.setChecked(profile.send_temperature)
        self.reasoning.setCurrentText(profile.reasoning_effort)
        self._update_provider_fields()
        self._invalidate_test()
        self._refresh_profiles()

    def _change_profile(self, index):
        if index < 0 or self._checking:
            return
        self.profiles[self._editing] = self._read_profile()
        self._editing = index
        self._load_profile()

    def _provider_changed(self):
        self.key.clear()
        self.model.setCurrentText(DEFAULT_MODELS[self.provider.currentData()])
        self._update_provider_fields()
        self.profiles[self._editing] = self._read_profile()
        self._refresh_profiles()
        self._invalidate_test()

    def _update_provider_fields(self, *_):
        self.send_temperature.setVisible(self.provider.currentData() != "gemini")
        self.connection_form.setRowVisible(self.reasoning,
            self.provider.currentData() == "nvidia" and self.model.currentText().strip() == DEFAULT_NVIDIA_MODEL)

    def add_profile(self):
        if self._checking or len(self.profiles) >= 10:
            return
        self.profiles[self._editing] = self._read_profile()
        self.profiles.append(ApiProfile(self.provider.currentData(), self.model.currentText(), "", self.send_temperature.isChecked(), self.reasoning.currentText()))
        self._editing = len(self.profiles) - 1
        self._load_profile()

    def remove_profile(self):
        if self._checking or len(self.profiles) == 1:
            return
        del self.profiles[self._editing]
        self._editing = min(self._editing, len(self.profiles) - 1)
        self._load_profile()

    def move_profile(self, direction):
        destination = self._editing + direction
        if self._checking or not 0 <= destination < len(self.profiles):
            return
        self.profiles[self._editing] = self._read_profile()
        self.profiles[self._editing], self.profiles[destination] = self.profiles[destination], self.profiles[self._editing]
        self._editing = destination
        self._refresh_profiles()

    def _toggle_key(self):
        visible = self.key_visibility.isChecked()
        self.key.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self.key_visibility.setText((tr('Hide') if visible else tr('Show')))

    def _copy_details(self):
        QApplication.clipboard().setText(self.check_status.toPlainText())

    def _set_status(self, text):
        self.check_status.setPlainText(text)
        self.check_status.setVisible(bool(text))
        self.copy_details.setEnabled(bool(text) and not self._checking)

    def _invalidate_test(self, *_):
        self.tested_connection = None
        if not self._checking:
            self._set_status("")

    def _set_checking(self, active):
        self._checking = active
        for widget in (
            self.key,
            self.provider, self.send_temperature, self.reasoning, self.profile_selector,
            self.add_profile_button, self.remove_profile_button, self.up_profile_button, self.down_profile_button,
            self.key_visibility,
            self.model,
            self.load_models_button,
            self.test_button,
            self.buttons.button(QDialogButtonBox.StandardButton.Save),
        ):
            widget.setEnabled(not active)
        if not active:
            self._refresh_profiles()
        self.copy_details.setEnabled(not active and bool(self.check_status.toPlainText()))

    def load_models(self):
        self._run_check("models")

    def test_connection(self):
        self._run_check("test")

    def _run_check(self, kind):
        if self._checking:
            return
        try:
            profile = self._read_profile()
            if kind == "models":
                default = DEFAULT_MODELS[profile.provider]
                profile = ApiProfile(profile.provider, default, profile.api_key, profile.send_temperature, profile.reasoning_effort)
            service = create_service(profile.normalized())
        except APIError as exc:
            self._set_status(str(exc))
            return
        if hasattr(service, "cancel_event"):
            self.finished.connect(lambda: service.cancel_event.set())
        self.tested_connection = None
        self._set_checking(True)
        self._set_status((tr('Loading models…') if kind == "models" else tr('Testing {p0}…', p0=service.model)))
        if kind == "test" and profile.provider == "nvidia":
            self._set_status(tr('Testing {p0}…\nNVIDIA can take several minutes to return an answer. Waiting up to 10 minutes. Cancel closes this test; a submitted request may continue on NVIDIA.', p0=service.model))
        results = self._check_results
        secrets = self.profiles + [self._read_profile()]

        def work():
            try:
                value = service.list_models() if kind == "models" else service.test_connection()
                results.put((kind, value, None, (service.api_key, service.model)))
            except Exception as exc:
                results.put((kind, None, redact(str(exc), secrets), None))

        threading.Thread(target=work, daemon=True, name="api-connection-check").start()

    def _poll_check(self):
        try:
            kind, value, error, identity = self._check_results.get_nowait()
        except queue.Empty:
            return
        self._set_checking(False)
        if error:
            self._set_status(error)
        elif kind == "models":
            previous = self.model.currentText().strip().removeprefix("models/")
            self.model.clear()
            self.model.addItems(value)
            if previous in value:
                self.model.setCurrentText(previous)
                note = "Selected model is available."
            else:
                self.model.setCurrentIndex(-1)
                self.model.setEditText("")
                note = "Previous model is unavailable. Choose a model."
            self._set_status(tr('{p0} models loaded. {p1}', p0=len(value), p1=note))
        else:
            self.tested_connection = identity
            self._set_status(value)

    def accept(self):
        if self._checking:
            return
        try:
            self.profiles[self._editing] = self._read_profile()
            profiles = [profile.normalized() for profile in self.profiles]
            if len(profiles) > 1 and any(not profile.api_key for profile in profiles):
                raise APIError("Enter an API key for each entry, or remove unused entries.")
            identities = [(p.provider, p.model, p.api_key, p.send_temperature, p.reasoning_effort) for p in profiles]
            if len(set(identities)) != len(identities):
                raise APIError("Remove duplicate API entries before saving.")
        except APIError as exc:
            QMessageBox.warning(self, tr('Check API settings'), str(exc))
            return
        options = GenerationOptions(
            self.temperature.value(),
            self.common_instructions.toPlainText(),
            self.extraction_instructions.toPlainText(),
            self.translation_instructions.toPlainText(),
            context_enabled=self.context_enabled.isChecked(),
            pages_before=self.pages_before.value(), pages_after=self.pages_after.value(),
            summary_enabled=self.summary_enabled.isChecked(),
            auto_update_summary=self.auto_update_summary.isChecked(),
            fix_previous_page=self.fix_previous_page.isChecked(),
        )
        try:
            warning = save_settings(options, profiles, self.rotation.isChecked(), self.failure_limit.value())
            save_language(self.interface_language.currentData())
        except OSError as exc:
            QMessageBox.warning(self, tr('Could not save settings'), str(exc))
            return
        if warning:
            QMessageBox.warning(self, tr('Settings saved'), warning)
        self.options = options
        self.profiles = profiles
        super().accept()
