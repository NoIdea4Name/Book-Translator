# Build a Windows executable

Use **64-bit Python 3.12 on Windows**. Open PowerShell or Command Prompt in the
folder containing `main.py` and `build_exe.py`:

```powershell
py -3.12 build_exe.py
```

The script installs app dependencies and PyInstaller into that Python interpreter,
renders the existing app icon, and builds a windowed app. It does not create a
virtual environment or require a batch launcher. Internet access is needed for
dependencies that are not already installed. Building may take several minutes.

The result is:

```
dist/BookTranslator/BookTranslator.exe
```

Run that executable. To share it, zip the **whole `dist/BookTranslator` folder**,
including `_internal`. Recipients do not need Python installed. This folder build
starts faster and is easier to inspect than a single-file build.

## Other options

One executable (resources unpack into a temporary directory at launch):

```powershell
py -3.12 build_exe.py --onefile
```

Output: `dist/BookTranslator.exe`.

Keep a console visible to diagnose startup problems:

```powershell
py -3.12 build_exe.py --console
```

Reuse installed build dependencies:

```powershell
py -3.12 build_exe.py --skip-install
```

Print commands without installing or building:

```powershell
py -3.12 build_exe.py --dry-run
```

Options can be combined. Run `--help` for details. Builds replace the matching
output in `dist`; close a running copy before rebuilding. Intermediate files and
the generated PyInstaller spec are kept in `build`. Edit `build_exe.py` for lasting
build changes; the spec is regenerated. No source files are deleted.

## What is bundled

The Python runtime, Qt widgets/image/SVG support, PDF/image dependencies, API clients,
Windows credential-store backend, icons, styles, phone capture page, and all JSON
language packs present at build time. Personal projects, images, API keys, local
settings, tests and development logs are not added as app data.

Add new language packs to `book_translator/locales` before building. For the folder
build, bundled packs are under `_internal/book_translator/locales`; restart the app
after editing them. For one-file distribution, rebuild to include changed packs.
Settings and credentials continue to use the existing per-user settings and OS
credential store, not the executable folder. Phone capture still needs the Windows
firewall to allow local network access when prompted.

## Check your Windows build

Launch it from a different folder. Open an image and PDF, check thumbnails, switch
to Vietnamese and restart, open Settings and Save Your Work, test your configured
API, and check phone capture. Use `--console` if the window does not open. Windows
may warn about an unsigned executable; this script does not sign the program or
create an installer.

This script must run on Windows to produce a Windows executable. It was checked
for command generation and asset bundling in the development environment; the
Windows executable itself must be built and tested on Windows.

References: https://pyinstaller.org/en/stable/usage.html and
https://pyinstaller.org/en/stable/runtime-information.html
