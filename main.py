#!/usr/bin/env python3
"""AUR Store — простой графический магазин пакетов Arch User Repository.

Интерфейс загружает базу имён пакетов AUR, ищет по ней, показывает сведения
со страницы пакета, помечает установленные пакеты и запускает aur_helper.py
для установки/переустановки либо pacman для удаления.
"""

from __future__ import annotations

import html
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

try:
    from PyQt6.QtCore import (
        QEvent,
        QObject,
        QProcess,
        QProcessEnvironment,
        QRunnable,
        QSettings,
        QSize,
        Qt,
        QThreadPool,
        QTimer,
        QUrl,
        pyqtSignal,
    )
    from PyQt6.QtGui import (
        QAction,
        QColor,
        QDesktopServices,
        QFontDatabase,
        QIcon,
        QKeySequence,
    )
    from PyQt6.QtWidgets import (
        QApplication,
        QCheckBox,
        QDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
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
import aur_helper

APP_TITLE = "AUR Store"
SEARCH_LIMIT = 500
FROZEN = bool(getattr(sys, "frozen", False))
HELPER_SENTINEL = "--aur-helper"
SCRIPT_DIR = (
    Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    if FROZEN
    else Path(__file__).resolve().parent
)
HELPER_PATH = SCRIPT_DIR / "aur_helper.py"
ICON_PATH = SCRIPT_DIR / "images" / "archicon.png"
WORK_DIR = Path.home() if FROZEN else SCRIPT_DIR
HELPER_AVAILABLE = FROZEN or HELPER_PATH.is_file()
HELPER_COMMAND = (
    [str(Path(sys.executable).resolve()), HELPER_SENTINEL]
    if FROZEN
    else [sys.executable, "-u", str(HELPER_PATH)]
)

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
QPushButton#removeButton {
    color: #b42318;
    border-color: #f0b8b3;
}
QPushButton#removeButton:hover {
    background: #fff1ef;
}
QPushButton#removeButton:disabled {
    color: #8c959f;
    background: #f0f2f4;
    border-color: #d0d7de;
}
QCheckBox {
    color: #24292f;
    font-size: 10pt;
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


def _installed_packages() -> set[str] | None:
    """Вернуть имена установленных пакетов через pacman.

    Пустое множество означает, что pacman нет; None — что запрос не удался
    и прежние отметки лучше не сбрасывать.
    """
    if shutil.which("pacman") is None:
        return set()
    try:
        result = subprocess.run(
            ["pacman", "-Qq"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


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
        self.settings = QSettings("AUR Store", "AUR Store")
        self.packages: list[str] = []
        self.installed: set[str] = set()
        self.info_cache: dict[str, dict] = {}
        self.info_inflight: set[str] = set()
        self.selected_name: str | None = None
        self.pending_info_name: str | None = None
        self.db_loading = False
        self.pkexec_path = shutil.which("pkexec")
        self.pkexec_available = self.pkexec_path is not None
        self._state_timers: list[QTimer] = []

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
        self._refresh_installed()

    def changeEvent(self, event) -> None:
        if (
            event.type() == QEvent.Type.ActivationChange
            and self.isActiveWindow()
        ):
            self._refresh_installed()
        super().changeEvent(event)

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
        subtitle = QLabel("Поиск, установка и удаление пакетов из Arch User Repository")
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
        hint = QLabel("Ctrl+F — поиск")
        hint.setObjectName("subtitle")
        hint.setToolTip("PgUp/PgDown пролистывают сведения о пакете")
        actions.addWidget(hint)
        actions.addStretch(1)
        self.pkexec_checkbox = QCheckBox("Без консоли (pkexec)")
        self.pkexec_checkbox.setToolTip(
            "Запускать установку и удаление без терминала. Пароль "
            "запрашивается графическим агентом polkit через pkexec."
        )
        if self.pkexec_available:
            self.pkexec_checkbox.setChecked(
                bool(self.settings.value("use_pkexec", False, type=bool))
            )
            self.pkexec_checkbox.toggled.connect(
                lambda checked: self.settings.setValue("use_pkexec", checked)
            )
        else:
            self.pkexec_checkbox.setChecked(False)
            self.pkexec_checkbox.setEnabled(False)
            self.pkexec_checkbox.setToolTip(
                "pkexec не найден. Установите polkit, чтобы запускать действия "
                "без терминала."
            )

        self.open_button = QPushButton("Страница на AUR")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_aur_page)
        self.remove_button = QPushButton("Удалить")
        self.remove_button.setObjectName("removeButton")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove_selected)
        self.install_button = QPushButton("Установить")
        self.install_button.setObjectName("installButton")
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(self._install_selected)
        actions.addWidget(self.pkexec_checkbox)
        actions.addWidget(self.open_button)
        actions.addWidget(self.remove_button)
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
            self._update_action_buttons()
            self.details.setHtml(self._welcome_html(len(self.packages)))
            self.statusBar().showMessage(
                "Введите название пакета для поиска по базе AUR."
            )
            return

        shown = aur.search_packages(self.packages, query, limit=SEARCH_LIMIT)
        for name in shown:
            item = QListWidgetItem(self._package_label(name))
            item.setData(Qt.ItemDataRole.UserRole, name)
            if name in self.installed:
                item.setForeground(QColor("#1a7f37"))
            self.results.addItem(item)
        self.results.blockSignals(False)

        if not shown:
            self.selected_name = None
            self._update_action_buttons()
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
            for row in range(self.results.count()):
                item = self.results.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == previous:
                    self.results.setCurrentItem(item)
                    return

        self.selected_name = None
        self._update_action_buttons()

    def _package_label(self, name: str) -> str:
        return f"{name} · установлен" if name in self.installed else name

    def _update_action_buttons(self) -> None:
        name = self.selected_name
        installed = bool(name and name in self.installed)
        self.install_button.setText("Переустановить" if installed else "Установить")
        self.install_button.setEnabled(bool(name))
        self.remove_button.setEnabled(installed)
        self.open_button.setEnabled(bool(name))

    def _focus_first_result(self) -> None:
        if self.results.count():
            self.results.setCurrentRow(0)
            self.results.setFocus()

    def _on_selection_changed(self, current, _previous) -> None:
        name = current.data(Qt.ItemDataRole.UserRole) if current else None
        self.selected_name = name
        self._info_timer.stop()
        self._update_action_buttons()

        if not name:
            self.details.setHtml(self._welcome_html(len(self.packages)))
            return

        if name in self.info_cache:
            self.details.setHtml(
                aur.format_package_html(self.info_cache[name], name in self.installed)
            )
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
            self.details.setHtml(
                aur.format_package_html(info, name in self.installed)
            )
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

    def _refresh_installed(self) -> None:
        worker = _Worker(_installed_packages)
        worker.signals.finished.connect(self._on_installed_loaded)
        worker.signals.failed.connect(lambda _message: None)
        self.thread_pool.start(worker)

    def _on_installed_loaded(self, installed: set[str] | None) -> None:
        if installed is None:
            return
        self.installed = installed
        if self.packages:
            self._apply_filter()
        self._update_action_buttons()

    def _ensure_pkexec(self) -> bool:
        if self.pkexec_available:
            return True
        QMessageBox.critical(
            self,
            "pkexec не найден",
            "Для работы без консоли нужен pkexec (пакет polkit).\n"
            "Установите polkit или выключите режим «Без консоли».",
        )
        return False

    def _install_selected(self) -> None:
        name = self.selected_name
        if not name:
            return
        if not HELPER_AVAILABLE:
            QMessageBox.critical(
                self,
                "Helper не найден",
                f"Не найден файл {HELPER_PATH}.\n"
                "Положите aur_helper.py рядом с main.py.",
            )
            return

        installed = name in self.installed
        no_console = self.pkexec_checkbox.isChecked()
        if no_console and not self._ensure_pkexec():
            return

        if installed:
            title = "Переустановка пакета"
            question = (
                f"Пересобрать и переустановить пакет «{name}»?\n\n"
                "Пакет и все его AUR-зависимости будут склонированы и собраны "
                "заново через makepkg."
            )
        else:
            title = "Установка пакета"
            question = (
                f"Установить пакет «{name}»?\n\n"
                "Пакет и все его AUR-зависимости будут склонированы и собраны "
                "через makepkg."
            )
        password_hint = (
            "Пароль будет запрошен через pkexec (polkit)."
            if no_console
            else "Для установки зависимостей и пакета может потребоваться пароль sudo."
        )
        answer = QMessageBox.question(
            self,
            title,
            f"{question}\n\n{password_hint}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        command = [*HELPER_COMMAND, "install"]
        if installed:
            command.append("--force")
        if no_console:
            command.append("--noconfirm")
        command.append(name)

        action = "Переустановка" if installed else "Установка"
        window_title = f"{APP_TITLE} — {action.lower()} {name}"

        if no_console:
            self._run_with_log_dialog(
                command,
                name,
                title=window_title,
                notice=(
                    "Пароль запрашивается через pkexec (polkit). "
                    "Вывод сборки и установки показывается здесь."
                ),
                action=action,
                env={"PACMAN_AUTH": self.pkexec_path or "pkexec"},
            )
            return

        if self._launch_in_terminal(command, window_title, name, action=action):
            self._watch_package_state(name, expected_installed=True)
        else:
            self._run_with_log_dialog(
                command, name, title=window_title, action=action
            )

    def _remove_selected(self) -> None:
        name = self.selected_name
        if not name or name not in self.installed:
            return

        no_console = self.pkexec_checkbox.isChecked()
        if no_console and not self._ensure_pkexec():
            return

        password_hint = (
            "Пароль будет запрошен через pkexec (polkit)."
            if no_console
            else "Может потребоваться пароль sudo."
        )
        answer = QMessageBox.warning(
            self,
            "Удаление пакета",
            f"Удалить пакет «{name}»?\n\n"
            "Вместе с ним будут удалены зависимости, которые больше не нужны "
            "другим пакетам. Это действие необратимо.\n\n"
            f"{password_hint}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        title = f"{APP_TITLE} — удаление {name}"
        if no_console:
            command = [self.pkexec_path, "pacman", "-Rns", "--noconfirm", name]
            self._run_with_log_dialog(
                command,
                name,
                title=title,
                notice=(
                    "Пароль запрашивается через pkexec (polkit). "
                    "Вывод pacman показывается здесь."
                ),
                action="Удаление",
            )
            return

        command = ["sudo", "pacman", "-Rns", "--noconfirm", name]
        if self._launch_in_terminal(command, title, name, action="Удаление"):
            self._watch_package_state(name, expected_installed=False)
        else:
            self._run_with_log_dialog(command, name, title=title, action="Удаление")

    def _launch_in_terminal(
        self,
        command: list[str],
        title: str,
        package: str,
        *,
        action: str = "Установка",
    ) -> bool:
        script = (
            f"cd {shlex.quote(str(WORK_DIR))} && {shlex.join(command)}; "
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
            f"{action} «{package}»: запущено в терминале."
        )
        return True

    def _watch_package_state(
        self, name: str, *, expected_installed: bool
    ) -> None:
        if shutil.which("pacman") is None:
            return

        timer = QTimer(self)
        timer.setInterval(2000)
        self._state_timers.append(timer)
        deadline = time.monotonic() + 30 * 60

        def stop() -> None:
            timer.stop()
            if timer in self._state_timers:
                self._state_timers.remove(timer)
            timer.deleteLater()

        def check() -> None:
            installed = _installed_packages()
            if installed is not None:
                current = name in installed
                if current == expected_installed:
                    stop()
                    self._on_installed_loaded(installed)
                    return
            if time.monotonic() >= deadline:
                stop()

        timer.timeout.connect(check)
        timer.start()

    def _run_with_log_dialog(
        self,
        command: list[str],
        name: str,
        *,
        title: str | None = None,
        notice: str | None = None,
        action: str = "Установка",
        env: dict[str, str] | None = None,
    ) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(title or f"Установка {name}")
        dialog.resize(820, 520)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)

        notice_label = QLabel(
            notice
            or (
                "Терминальный эмулятор не найден, поэтому вывод показывается "
                "здесь. Интерактивный ввод (например, пароль sudo) в этом режиме "
                "недоступен — установите xterm или запустите helper вручную "
                "из терминала."
            )
        )
        notice_label.setWordWrap(True)
        layout.addWidget(notice_label)

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
        process.setWorkingDirectory(str(WORK_DIR))
        if env:
            process_env = QProcessEnvironment.systemEnvironment()
            for key, value in env.items():
                process_env.insert(key, value)
            process.setProcessEnvironment(process_env)

        def append_output() -> None:
            data = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
            if data:
                output.appendPlainText(ANSI_RE.sub("", data).rstrip("\n"))

        def process_finished(code: int, _status) -> None:
            append_output()
            output.appendPlainText(f"\nПроцесс завершён с кодом {code}.")
            self._refresh_installed()
            if code == 0:
                self.statusBar().showMessage(f"{action} «{name}»: завершено.")
            else:
                self.statusBar().showMessage(
                    f"{action} «{name}»: завершено с ошибкой (код {code})."
                )

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
        self.statusBar().showMessage(f"{action} «{name}»: запущено.")
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
            "зависимости и другую информацию со страницы AUR. Установленные "
            "пакеты помечаются в списке; для них доступны переустановка и "
            "удаление. Кнопка «Установить» запустит <code>aur_helper.py</code>, "
            "который склонирует репозиторий и соберёт пакет через "
            "<code>makepkg</code>. Режим «Без консоли» использует "
            "<code>pkexec</code> для запроса пароля.",
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
    if len(sys.argv) > 1 and sys.argv[1] == HELPER_SENTINEL:
        return aur_helper.main(sys.argv[2:])

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
