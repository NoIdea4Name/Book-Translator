"""Run with: python main.py [book.pdf | page1.png page2.png ...]"""

import sys


def main():
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication
        from book_translator.ui import MainWindow
    except ImportError as exc:
        print(
            f"A required package is missing: {exc}\nRun: python -m pip install -r requirements.txt"
        )
        return 1
    app = QApplication(sys.argv)
    app.setApplicationName("Book Translator")
    app.setOrganizationName("BookTranslator")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    from book_translator.i18n import install_language
    install_language(app)
    window = MainWindow()
    window.show()
    if len(sys.argv) > 1:
        paths = sys.argv[1:]
        QTimer.singleShot(0, lambda: window.load_paths(paths))
    return app.exec()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
