"""Build on Windows: py -3.12 build_exe.py [--onefile] [--console]."""
from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
APP_NAME = 'BookTranslator'


def build_command(root: Path, *, onefile=False, console=False):
    """Keep resources beside their package so existing __file__ paths work."""
    command = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm', '--clean', '--noupx',
        '--name', APP_NAME,
        '--onefile' if onefile else '--onedir',
        '--console' if console else '--windowed',
        '--distpath', str(root / 'dist'),
        '--workpath', str(root / 'build' / 'pyinstaller'),
        '--specpath', str(root / 'build'),
        '--paths', str(root),
        '--icon', str(root / 'build' / 'app.ico'),
        '--hidden-import', 'keyring.backends.Windows',
        '--hidden-import', 'PySide6.QtSvg',
        '--collect-submodules', 'google.genai',
        '--copy-metadata', 'google-genai',
        '--copy-metadata', 'keyring',
        '--collect-all', 'pymupdf',
        '--collect-data', 'docx',
    ]
    for source, destination in (
        ('book_translator/assets', 'book_translator/assets'),
        ('book_translator/locales', 'book_translator/locales'),
        ('book_translator/styles.qss', 'book_translator'),
    ):
        command += ['--add-data', f'{root / source}:{destination}']
    # Prevent a developer's unrelated Qt installation from entering this app.
    for package in ('PyQt5', 'PyQt6', 'PySide2'):
        command += ['--exclude-module', package]
    command.append(str(root / 'main.py'))
    return command


def make_icon(root: Path):
    """Render the existing SVG logo to a Windows multi-resolution icon."""
    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    app = QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(str(root / 'book_translator' / 'assets' / 'app-icon.svg'))
    if not renderer.isValid():
        raise RuntimeError('The app icon SVG could not be loaded.')
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, 'PNG'):
        raise RuntimeError('Could not render the app icon.')
    destination = root / 'build' / 'app.ico'
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(BytesIO(bytes(buffer.data()))) as icon:
        icon.save(destination, format='ICO', sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)])
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--onefile', action='store_true', help='Create dist/BookTranslator.exe instead of an app folder.')
    parser.add_argument('--console', action='store_true', help='Keep a console window for debugging.')
    parser.add_argument('--skip-install', action='store_true', help='Use dependencies already installed in this Python interpreter.')
    parser.add_argument('--dry-run', action='store_true', help='Print commands without installing, writing files or building.')
    args = parser.parse_args(argv)
    if sys.platform != 'win32' and not args.dry_run:
        parser.error('Run this script on Windows to create a Windows .exe. Cross-compiling from Linux/macOS is not supported.')
    if sys.version_info < (3, 12):
        parser.error('Use Python 3.12 or newer (64-bit Python 3.12 recommended).')

    install = [sys.executable, '-m', 'pip', 'install', '-r', str(ROOT / 'requirements-build.txt')]
    command = build_command(ROOT, onefile=args.onefile, console=args.console)
    if args.dry_run:
        if not args.skip_install:
            print(subprocess.list2cmdline(install))
        print('Render book_translator/assets/app-icon.svg to build/app.ico')
        print(subprocess.list2cmdline(command))
        return 0

    try:
        if not args.skip_install:
            subprocess.run(install, cwd=ROOT, check=True)
        make_icon(ROOT)
        subprocess.run(command, cwd=ROOT, check=True)
        output = ROOT / 'dist' / (APP_NAME + '.exe') if args.onefile else ROOT / 'dist' / APP_NAME / (APP_NAME + '.exe')
        if not output.is_file():
            raise RuntimeError(f'Build finished but the expected executable is missing: {output}')
        shutil.copy2(ROOT / 'BUILDING.md', output.parent / 'BUILDING.md')
    except (OSError, ImportError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'\nBuild failed: {exc}', file=sys.stderr)
        print('Read the error above. Install requirements-build.txt or rerun without --skip-install.', file=sys.stderr)
        return 1
    print(f'\nBuilt: {output}')
    if not args.onefile:
        print('Share the entire dist/BookTranslator folder, including _internal; do not copy only the .exe.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
