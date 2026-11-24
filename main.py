#!/usr/bin/env python3
"""AUR Store — простой графический магазин пакетов Arch User Repository.

Интерфейс загружает базу имён пакетов AUR, ищет по ней, показывает сведения
со страницы пакета и запускает aur_helper.py для сборки и установки.
"""

from __future__ import annotations

import html
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

try:
    from PyQt6.QtCore import (
        QObject,
        QProcess,
        QRunnable,
        QSize,
        Qt,
        QThreadPool,
        QTimer,
        QUrl,
        pyqtSignal,
    )
    from PyQt6.QtGui import (
        QAction,
        QDesktopServices,
        QFontDatabase,
        QIcon,
        QKeySequence,
    )
    from PyQt6.QtWidgets import (
        QApplication,
        QDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSplitter,
        QTextBrowser,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:
    if getattr(exc, "name", "").startswith("PyQt6"):
        raise SystemExit(
            "PyQt6 не установлен. Установите зависимости: pip install -r requirements.txt"
        ) from exc
    raise

import aur

APP_TITLE = "AUR Store"
SEARCH_LIMIT = 500
SCRIPT_DIR = Path(__file__).resolve().parent
HELPER_PATH = SCRIPT_DIR / "aur_helper.py"
ICON_PATH = SCRIPT_DIR / "images" / "archicon.png"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

STYLE_SHEET = """
QMainWindow, QWidget {
    background: #f5f6f8;
    color: #1f2328;
}
QLabel#title {
    font-size: 20pt;
    font-weight: 700;
}
QLabel#subtitle {
    color: #57606a;
    font-size: 10pt;
}
QLineEdit {
    background: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 11pt;
    selection-background-color: #1793d1;
}
QLineEdit:focus {
    border: 1px solid #1793d1;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 10pt;
}
QPushButton:hover {
    background: #eef1f4;
}
QPushButton:disabled {
    color: #8c959f;
    background: #f0f2f4;
}
QPushButton#installButton {
    background: #1793d1;
    border: 1px solid #1793d1;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#installButton:hover {
    background: #0f7fb5;
}
QPushButton#installButton:disabled {
    background: #a7d5ec;
    border-color: #a7d5ec;
    color: #f5fbff;
}
QListWidget {
    background: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 8px;
    padding: 4px;
    outline: none;
}
QListWidget::item {
    padding: 7px 10px;
    border-radius: 6px;
}
QListWidget::item:hover {
    background: #eef6fb;
}
QListWidget::item:selected {
    background: #1793d1;
    color: #ffffff;
}
QTextBrowser {
    background: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 8px;
    padding: 10px;
}
QStatusBar {
    background: #f5f6f8;
    color: #57606a;
}
"""


class _WorkerSignals(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


class _Worker(QRunnable):
    """Выполняет функцию в фоновом потоке QThreadPool."""

    def __init__(self, function, *args, **kwargs):
        super().__init__()
        self._function = function
        self._args = args
        self._kwargs = kwargs
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            result = self._function(*self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 — ошибка уходит в GUI
            self.signals.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.signals.finished.emit(result)


def _thousands(value: int) -> str:
    return f"{value:,}".replace(",", " ")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1000, 660)
        self.setMinimumSize(760, 500)
        self.setStyleSheet(STYLE_SHEET)

        self.thread_pool = QThreadPool.globalInstance()
        self.packages: list[str] = []
        self.info_cache: dict[str, dict] = {}
        self.info_inflight: set[str] = set()
        self.selected_name: str | None = None
        self.pending_info_name: str | None = None
        self.db_loading = False

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(140)
        self._search_timer.timeout.connect(self._apply_filter)

        self._info_timer = QTimer(self)
        self._info_timer.setSingleShot(True)
        self._info_timer.setInterval(160)
        self._info_timer.timeout.connect(self._fetch_pending_info)

        self._build_ui()
        self._install_shortcuts()
        self._load_db()

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 12)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        if ICON_PATH.exists():
            icon_label = QLabel()
            icon_label.setPixmap(QIcon(str(ICON_PATH)).pixmap(QSize(46, 46)))
            header.addWidget(icon_label)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel(APP_TITLE)
        title.setObjectName("title")
        subtitle = QLabel("Поиск и установка пакетов из Arch User Repository")
        subtitle.setObjectName("subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        root.addLayout(header)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Введите название пакета, например yay")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(38)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        self.search.returnPressed.connect(self._focus_first_result)
        self.refresh_button = QPushButton("Обновить базу")
        self.refresh_button.setMinimumHeight(38)
        self.refresh_button.setToolTip("Загрузить свежий список пакетов с AUR")
        self.refresh_button.clicked.connect(lambda: self._load_db(force=True))
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.refresh_button)
        root.addLayout(search_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.results = QListWidget()
        self.results.setUniformItemSizes(True)
        self.results.setMinimumWidth(250)
        self.results.currentItemChanged.connect(self._on_selection_changed)

        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(True)
        self.details.setMinimumWidth(380)
        self.details.setHtml(self._message_html("AUR Store", "Загрузка базы пакетов…"))

        splitter.addWidget(self.results)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 700])
        root.addWidget(splitter, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        hint = QLabel("Совет: PgUp/PgDown пролистывают сведения, Ctrl+F — поиск")
        hint.setObjectName("subtitle")
        actions.addWidget(hint)
        actions.addStretch(1)
        self.open_button = QPushButton("Страница на AUR")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_aur_page)
        self.install_button = QPushButton("Установить")
        self.install_button.setObjectName("installButton")
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(self._install_selected)
        actions.addWidget(self.open_button)
        actions.addWidget(self.install_button)
        root.addLayout(actions)

        self._db_label = QLabel("База: загрузка…")
        self.statusBar().addPermanentWidget(self._db_label)
        self.statusBar().showMessage("Загрузка базы пакетов AUR…")

    def _install_shortcuts(self) -> None:
        find_action = QAction(self)
        find_action.setShortcut(QKeySequence("Ctrl+F"))
        find_action.triggered.connect(self.search.setFocus)
        self.addAction(find_action)

        refresh_action = QAction(self)
        refresh_action.setShortcut(QKeySequence("F5"))
        refresh_action.triggered.connect(lambda: self._load_db(force=True))
        self.addAction(refresh_action)

    def _load_db(self, force: bool = False) -> None:
        if self.db_loading:
            return
        self.db_loading = True
        if force:
            self.info_cache.clear()
        self.refresh_button.setEnabled(False)
        self.search.setEnabled(False)
        self._db_label.setText("База: загрузка…")
        self.statusBar().showMessage("Загрузка базы пакетов AUR…")
        if not self.packages:
            self.details.setHtml(
                self._message_html("AUR Store", "Загрузка базы пакетов…")
            )

        worker = _Worker(aur.load_package_db, force)
        worker.signals.finished.connect(self._on_db_loaded)
        worker.signals.failed.connect(self._on_db_failed)
        self.thread_pool.start(worker)

    def _on_db_loaded(self, names: list[str]) -> None:
        self.db_loading = False
        self.packages = names
        self.refresh_button.setEnabled(True)
        self.search.setEnabled(True)
        self.search.setFocus()
        self._db_label.setText(f"Пакетов: {_thousands(len(names))}")
        if self.search.text().strip():
            self._apply_filter()
        else:
            self.details.setHtml(self._welcome_html(len(names)))
            self.statusBar().showMessage(
                "База загружена. Начните вводить название пакета."
            )

    def _on_db_failed(self, message: str) -> None:
        self.db_loading = False
        self.refresh_button.setEnabled(True)
        self.search.setEnabled(bool(self.packages))
        self._db_label.setText("База: ошибка")
        self.statusBar().showMessage("Не удалось загрузить базу пакетов.")
        self.details.setHtml(
            self._message_html(
                "Не удалось загрузить базу пакетов",
                f"{html.escape(message)}<br><br>Проверьте подключение к интернету "
                "и попробуйте обновить базу ещё раз.",
            )
        )
        QMessageBox.warning(
            self,
            "Ошибка загрузки",
            f"Не удалось загрузить базу пакетов AUR.\n\n{message}",
        )

    def _apply_filter(self) -> None:
        if not self.packages:
            return

        query = self.search.text().strip()
        previous = self.selected_name
        self._info_timer.stop()

        self.results.blockSignals(True)
        self.results.clear()

        if not query:
            self.results.blockSignals(False)
            self.selected_name = None
            self.install_button.setEnabled(False)
            self.open_button.setEnabled(False)
            self.details.setHtml(self._welcome_html(len(self.packages)))
            self.statusBar().showMessage(
                "Введите название пакета для поиска по базе AUR."
            )
            return

        shown = aur.search_packages(self.packages, query, limit=SEARCH_LIMIT)
        self.results.addItems(shown)
        self.results.blockSignals(False)

        if not shown:
            self.selected_name = None
            self.install_button.setEnabled(False)
            self.open_button.setEnabled(False)
            self.details.setHtml(
                self._message_html(
                    "Ничего не найдено",
                    f"По запросу «{html.escape(query)}» нет пакетов в базе AUR.",
                )
            )
            self.statusBar().showMessage("Ничего не найдено.")
            return

        if len(shown) >= SEARCH_LIMIT:
            self.statusBar().showMessage(
                f"Показаны первые {SEARCH_LIMIT} совпадений. Уточните запрос."
            )
        else:
            self.statusBar().showMessage(f"Найдено пакетов: {len(shown)}.")

        if previous:
            matches = self.results.findItems(previous, Qt.MatchFlag.MatchExactly)
            if matches:
                self.results.setCurrentItem(matches[0])
                return

        self.selected_name = None
        self.install_button.setEnabled(False)
        self.open_button.setEnabled(False)

    def _focus_first_result(self) -> None:
        if self.results.count():
            self.results.setCurrentRow(0)
            self.results.setFocus()

    def _on_selection_changed(self, current, _previous) -> None:
        name = current.text() if current else None
        self.selected_name = name
        self._info_timer.stop()
        self.install_button.setEnabled(bool(name))
        self.open_button.setEnabled(bool(name))

        if not name:
            self.details.setHtml(self._welcome_html(len(self.packages)))
            return

        if name in self.info_cache:
            self.details.setHtml(aur.format_package_html(self.info_cache[name]))
            return

        self.details.setHtml(
            self._message_html(
                "Загрузка…",
                f"Загружаем сведения о пакете «{html.escape(name)}»…",
            )
        )
        self.pending_info_name = name
        self._info_timer.start()

    def _fetch_pending_info(self) -> None:
        name = self.pending_info_name
        if not name or name in self.info_cache or name in self.info_inflight:
            return

        self.info_inflight.add(name)
        worker = _Worker(aur.fetch_package_info, name)
        worker.signals.finished.connect(
            lambda info, package=name: self._on_info_loaded(package, info)
        )
        worker.signals.failed.connect(
            lambda message, package=name: self._on_info_failed(package, message)
        )
        self.thread_pool.start(worker)

    def _on_info_loaded(self, name: str, info: dict) -> None:
        self.info_inflight.discard(name)
        self.info_cache[name] = info
        if name == self.selected_name:
            self.details.setHtml(aur.format_package_html(info))
            self.statusBar().showMessage(f"Сведения о «{name}» загружены.")

    def _on_info_failed(self, name: str, message: str) -> None:
        self.info_inflight.discard(name)
        if name == self.selected_name:
            self.details.setHtml(
                self._message_html(
                    "Не удалось получить сведения",
                    f"Пакет «{html.escape(name)}»: {html.escape(message)}",
                )
            )
            self.statusBar().showMessage("Ошибка загрузки сведений о пакете.")

    def _install_selected(self) -> None:
        name = self.selected_name
        if not name:
            return
        if not HELPER_PATH.is_file():
            QMessageBox.critical(
                self,
                "Helper не найден",
                f"Не найден файл {HELPER_PATH}.\n"
                "Положите aur_helper.py рядом с main.py.",
            )
            return

        answer = QMessageBox.question(
            self,
            "Установка пакета",
            f"Установить пакет «{name}»?\n\n"
            "Пакет и все его AUR-зависимости будут склонированы и собраны "
            "через makepkg. Для установки зависимостей и пакета может "
            "потребоваться пароль sudo.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        command = [sys.executable, "-u", str(HELPER_PATH), "install", name]
        title = f"{APP_TITLE} — установка {name}"
        if not self._launch_in_terminal(command, title, name):
            self._run_with_log_dialog(command, name)

    def _launch_in_terminal(
        self, command: list[str], title: str, package: str
    ) -> bool:
        script = (
            f"cd {shlex.quote(str(SCRIPT_DIR))} && {shlex.join(command)}; "
            "status=$?; echo; "
            "if [ \"$status\" -eq 0 ]; then echo 'Готово.'; "
            "else echo \"Ошибка (код $status).\"; fi; "
            "printf 'Нажмите Enter, чтобы закрыть окно…'; read -r _"
        )
        arguments = _terminal_command(title, script)
        if arguments is None:
            return False
        try:
            subprocess.Popen(
                arguments,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return False
        self.statusBar().showMessage(
            f"Установка «{package}» запущена в терминале."
        )
        return True

    def _run_with_log_dialog(self, command: list[str], name: str) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Установка {name}")
        dialog.resize(820, 520)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)

        notice = QLabel(
            "Терминальный эмулятор не найден, поэтому вывод показывается здесь. "
            "Интерактивный ввод (например, пароль sudo) в этом режиме недоступен — "
            "установите xterm или запустите helper вручную из терминала."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)

        output = QPlainTextEdit()
        output.setReadOnly(True)
        output.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(output, 1)

        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(dialog.accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        process = QProcess(dialog)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.setWorkingDirectory(str(SCRIPT_DIR))

        def append_output() -> None:
            data = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
            if data:
                output.appendPlainText(ANSI_RE.sub("", data).rstrip("\n"))

        def process_finished(code: int, _status) -> None:
            append_output()
            output.appendPlainText(f"\nПроцесс завершён с кодом {code}.")

        process.readyReadStandardOutput.connect(append_output)
        process.finished.connect(process_finished)
        process.errorOccurred.connect(
            lambda _error: output.appendPlainText(
                f"\nНе удалось запустить процесс: {process.errorString()}"
            )
        )

        def close_dialog() -> None:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(1000)

        dialog.finished.connect(lambda _result: close_dialog())

        process.start(command[0], command[1:])
        self.statusBar().showMessage(f"Установка «{name}» запущена.")
        dialog.exec()

    def _open_aur_page(self) -> None:
        name = self.selected_name
        if not name:
            return
        url = f"{aur.AUR_BASE}/packages/{urllib.parse.quote(name)}"
        QDesktopServices.openUrl(QUrl(url))

    @staticmethod
    def _welcome_html(count: int) -> str:
        return MainWindow._message_html(
            "Добро пожаловать в AUR Store",
            "Введите часть названия пакета в строке поиска — список обновится "
            "сразу по базе Arch User Repository.<br><br>"
            f"В локальной базе: <b>{_thousands(count)}</b> пакетов.<br><br>"
            "Выберите пакет, чтобы посмотреть описание, версию, сопровождающего, "
            "зависимости и другую информацию со страницы AUR. Кнопка "
            "«Установить» запустит <code>aur_helper.py</code>, который склонирует "
            "репозиторий и соберёт пакет через <code>makepkg</code>.",
        )

    @staticmethod
    def _message_html(title: str, body: str) -> str:
        return f"""
<div style="font-size:10.5pt; color:#1f2328;">
  <div style="font-size:15pt; font-weight:700; margin-bottom:10px;">{title}</div>
  <div style="color:#24292f; line-height:1.6;">{body}</div>
</div>
"""


def _terminal_command(title: str, script: str) -> list[str] | None:
    custom = shlex.split(os.environ.get("TERMINAL", ""))
    candidates: list[list[str]] = [custom] if custom else []
    candidates += [[name] for name in (
        "gnome-terminal",
        "konsole",
        "xfce4-terminal",
        "alacritty",
        "kitty",
        "xterm",
        "foot",
    )]

    for spec in candidates:
        executable = shutil.which(spec[0])
        if not executable:
            continue
        name = Path(executable).name
        extra = spec[1:]

        if name == "gnome-terminal":
            return [executable, "--title", title, "--", "bash", "-lc", script]
        if name == "konsole":
            return [executable, "--title", title, "-e", "bash", "-lc", script]
        if name == "xfce4-terminal":
            return [executable, "--title", title, "-x", "bash", "-lc", script]
        if name in ("kitty", "foot"):
            return [executable, *extra, "bash", "-lc", script]
        return [executable, *extra, "-e", "bash", "-lc", script]

    return None


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName(APP_TITLE)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
