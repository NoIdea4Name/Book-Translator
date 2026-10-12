"""Persistent preferences with secrets kept exclusively in the native OS vault.

Save new credentials first, atomically commit their references, then remove old
credentials. A failed save never replaces the previous settings or credentials.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys
import uuid

from PySide6.QtCore import QIODevice, QLockFile, QSaveFile, QStandardPaths

from .api_rotation import ApiProfile
from .preferences import GenerationOptions

SERVICE = "BookTranslator.API"


class SettingsError(OSError):
    """Safe, user-facing error. Never include a backend exception or API key."""


def native_vault():
    # Explicit selection avoids user-installed plaintext or remote keyring backends.
    try:
        if sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring
            return WinVaultKeyring()
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
            return Keyring()
        if sys.platform.startswith("linux"):
            from keyring.backends.SecretService import Keyring
            return Keyring()
    except Exception:
        pass
    raise SettingsError("Secure credential storage is unavailable. Install requirements.txt and unlock your operating system's credential store, then try again.")


@dataclass
class SavedSettings:
    options: GenerationOptions
    profiles: list[ApiProfile]
    rotation_enabled: bool
    failure_limit: int
    warning: str = ""


class SettingsStore:
    def __init__(self, path=None, vault=None):
        self.path = Path(path) if path is not None else Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericConfigLocation)
        ) / "BookTranslator" / "settings.json"
        self._vault = vault

    @property
    def vault(self):
        if self._vault is None:
            self._vault = native_vault()
        return self._vault

    @contextmanager
    def locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise SettingsError("Could not open the settings folder.") from None
        lock = QLockFile(str(self.path) + ".lock")
        if not lock.tryLock(1000):
            raise SettingsError("Another app instance is saving settings. Try again shortly.")
        try:
            yield
        finally:
            lock.unlock()

    def _read(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data["version"] != 1 or not 1 <= len(data["profiles"]) <= 10:
                raise ValueError()
            GenerationOptions(**data["options"])
            if type(data["rotation_enabled"]) is not bool or type(data["failure_limit"]) is not int or not 1 <= data["failure_limit"] <= 5:
                raise ValueError()
            for item in data["profiles"]:
                if set(item) != {"provider", "model", "send_temperature", "reasoning_effort", "credential_id"}:
                    raise ValueError()
                if type(item["send_temperature"]) is not bool:
                    raise ValueError()
                ApiProfile(item["provider"], item["model"], "", item["send_temperature"], item["reasoning_effort"]).normalized()
                if item["credential_id"] is not None and str(uuid.UUID(item["credential_id"])) != item["credential_id"]:
                    raise ValueError()
            return data
        except Exception:
            raise SettingsError("Saved settings could not be read. They have been left unchanged. Restore your settings.json backup or rename the damaged file in your BookTranslator configuration folder.") from None

    def load(self):
        if not self.path.exists():
            return None  # First launch: no credential-store access is needed.
        with self.locked():
            data = self._read()
            if data is None:
                return None
            profiles, missing = [], []
            for index, item in enumerate(data["profiles"]):
                key = ""
                if item["credential_id"]:
                    try:
                        key = self.vault.get_password(SERVICE, item["credential_id"]) or ""
                    except Exception:
                        key = ""
                    if not key:
                        missing.append(str(index + 1))
                profiles.append(ApiProfile(item["provider"], item["model"], key, item["send_temperature"], item["reasoning_effort"]))
            warning = ("Could not unlock saved API keys for entries " + ", ".join(missing)
                       + ". Unlock the OS credential store and restart, or re-enter the keys in Settings. Saved credentials are unchanged.") if missing else ""
            return SavedSettings(GenerationOptions(**data["options"]), profiles, data["rotation_enabled"], data["failure_limit"], warning)

    def _delete(self, credential_ids):
        failed = False
        for credential_id in credential_ids:
            try:
                # Missing credentials are already removed.
                if self.vault.get_password(SERVICE, credential_id) is not None:
                    self.vault.delete_password(SERVICE, credential_id)
            except Exception:
                failed = True
        return failed

    def _commit(self, data):
        encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        output = QSaveFile(str(self.path))
        # Never allow a failed atomic save to overwrite the existing file directly.
        output.setDirectWriteFallback(False)
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            raise SettingsError("Could not save settings. Previous settings are unchanged.")
        if output.write(encoded) != len(encoded):
            output.cancelWriting()
            raise SettingsError("Could not finish saving settings. Previous settings are unchanged.")
        if not output.commit():
            raise SettingsError("Could not commit settings. Previous settings are unchanged.")

    def save(self, options, profiles, rotation_enabled, failure_limit):
        if not 1 <= len(profiles) <= 10 or type(rotation_enabled) is not bool or type(failure_limit) is not int or not 1 <= failure_limit <= 5:
            raise SettingsError("Invalid API settings.")
        profiles = [profile.normalized() for profile in profiles]
        data = dict(version=1, options=asdict(options), profiles=[], rotation_enabled=rotation_enabled, failure_limit=failure_limit)
        with self.locked():
            previous = self._read()
            staged = []
            try:
                # Do not replace an existing configuration while its vault is
                # locked, even when every visible key field is currently blank.
                if previous:
                    for item in previous["profiles"]:
                        if item["credential_id"]:
                            self.vault.get_password(SERVICE, item["credential_id"])
                for profile in profiles:
                    credential_id = None
                    if profile.api_key:
                        credential_id = str(uuid.uuid4())
                        staged.append(credential_id)
                        self.vault.set_password(SERVICE, credential_id, profile.api_key)
                        if self.vault.get_password(SERVICE, credential_id) != profile.api_key:
                            raise SettingsError("Credential verification failed.")
                    # Explicit fields: asdict(profile) would leak the key to disk.
                    data["profiles"].append(dict(provider=profile.provider, model=profile.model,
                        send_temperature=profile.send_temperature, reasoning_effort=profile.reasoning_effort,
                        credential_id=credential_id))
                self._commit(data)
            except Exception:
                cleanup_failed = self._delete(staged)
                detail = " Some unused credentials could not be removed from the OS credential store." if cleanup_failed else ""
                raise SettingsError("Settings were not saved. Check that the OS credential store is unlocked and the settings folder is writable. Previous settings are unchanged; no keys were written to a plain-text file." + detail) from None
            old_ids = [item["credential_id"] for item in previous["profiles"] if item["credential_id"]] if previous else []
            if self._delete(old_ids):
                return "Settings saved, but some old credentials could not be removed from the OS credential store. They are no longer used by the app."
            return ""


def load_settings():
    return SettingsStore().load()


def save_settings(options, profiles, rotation_enabled, failure_limit):
    return SettingsStore().save(options, profiles, rotation_enabled, failure_limit)
