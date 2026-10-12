"""Check build orchestration without claiming to cross-compile Windows binaries."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('build_exe', Path(__file__).resolve().parents[1] / 'build_exe.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuildScriptTests(unittest.TestCase):
    def test_command_keeps_resource_paths_and_windows_backend(self):
        root = Path('/a folder/book app')
        command = builder.build_command(root)
        self.assertIn('--onedir', command)
        self.assertIn('--windowed', command)
        self.assertIn('keyring.backends.Windows', command)
        self.assertIn('PySide6.QtSvg', command)
        self.assertEqual(command[-1], str(root / 'main.py'))
        for source, destination in [('assets', 'assets'), ('locales', 'locales')]:
            self.assertIn(f'{root / "book_translator" / source}:book_translator/{destination}', command)
        onefile = builder.build_command(root, onefile=True, console=True)
        self.assertIn('--onefile', onefile)
        self.assertIn('--console', onefile)
        self.assertNotIn('--onedir', onefile)
        self.assertNotIn('--windowed', onefile)

    def test_dry_run_never_installs_or_builds(self):
        with patch.object(builder.subprocess, 'run') as run, patch.object(builder, 'make_icon') as icon, patch('builtins.print'):
            self.assertEqual(builder.main(['--dry-run', '--onefile']), 0)
        run.assert_not_called()
        icon.assert_not_called()

    def test_build_failure_stops_without_success_message(self):
        with patch.object(builder.sys, 'platform', 'win32'), patch.object(builder, 'make_icon', side_effect=RuntimeError('Icon failed')), patch.object(builder.subprocess, 'run') as run, patch('builtins.print'):
            self.assertEqual(builder.main(['--skip-install']), 1)
        run.assert_not_called()
