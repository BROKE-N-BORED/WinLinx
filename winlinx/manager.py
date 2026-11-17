#!/usr/bin/env python3
"""WinLinx Program Manager for Linux - native Qt desktop window.

One place to organize and manage all the Windows stuff on this PC:
  Programs   - every Windows program in the Wine prefix: launch,
               uninstall, open install folder, per-program prefix
               and Windows version (right-click); search, install
               date, size, running, prefix columns.
  Plug-ins   - every bridged plug-in with bridge status, re-sync,
               remove; search.
  Quarantine - files the scanner flagged before they could install
               or run: threat name, Allow (install anyway - the user
               is final authority) or Delete; search.
  Failed Drops - files the watcher could not handle, with the real
               reason: Retry or Forget.
  Activity   - live feed of what the watcher is doing, plus a
               watcher status light.
  System     - Wine version, prefix location, disk used, yabridge and
               scanner status, watcher status, recent log lines,
               manual scan, signature updates, prefix backup/restore,
               debug report, update check.

Plain Qt widgets, no web anything. Slow work (scans, sizes, syncs,
uninstalls, backups) runs in worker threads so the window stays fast.
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
import scanner
import watcher

from PySide6.QtCore import (
    Qt, QLockFile, QSettings, QThread, QTimer, QUrl, Signal,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import (
    QActionGroup, QColor, QDesktopServices, QFont, QIcon,
    QKeySequence, QPainter, QPixmap, QShortcut, QSyntaxHighlighter,
    QTextCharFormat,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressDialog,
    QPushButton, QStyleOptionViewItem, QStyledItemDelegate,
    QTabWidget, QTableWidget, QTableWidgetItem,
    QTextBrowser, QVBoxLayout, QWidget,
)

SERVICE_NAME = "winlinx-watcher.service"
ICON_PATH = Path(__file__).resolve().parent / "icons" / "winlinx-256.png"
SERVER_NAME = "winlinx-manager"
AUTO_REFRESH_MS = 30000
ACTIVITY_POLL_MS = 2000


# ---------------------------------------------------------------- workers

class Worker(QThread):
    """Run a function in a thread; emit finished(result) or error(msg)."""
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, func, parent=None):
        super().__init__(parent)
        self._func = func

    def run(self):
        try:
            self.finished.emit(self._func())
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.failed.emit(str(exc))


class ProgressWorker(QThread):
    """Run func(report) in a thread. report(done, total) emits
    progress and returns False when the user cancelled (the function
    is expected to stop and clean up)."""
    progress = Signal(int, int)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, func, parent=None):
        super().__init__(parent)
        self._func = func
        self.cancelled = False

    def report(self, done, total):
        self.progress.emit(done, total)
        return not self.cancelled

    def run(self):
        try:
            self.finished.emit(self._func(self.report))
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.failed.emit(str(exc))


class SizeItem(QTableWidgetItem):
    """Size cell that sorts by bytes, not by text."""

    def __init__(self, size_bytes):
        text = common.format_size(size_bytes) if size_bytes is not None else ""
        super().__init__(text)
        self._size = size_bytes if size_bytes is not None else -1

    def __lt__(self, other):
        if isinstance(other, SizeItem):
            return self._size < other._size
        return super().__lt__(other)


# ---------------------------------------------------------------- helpers

def confirm_destructive(parent, title, text):
    """A destructive confirmation that defaults to No (U23), so
    pressing Enter never confirms it. Returns True only when the
    user explicitly picks Yes."""
    answer = QMessageBox.question(
        parent, title, text,
        QMessageBox.StandardButton.Yes
        | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


# Per-program glyph colours: muted, distinct, deterministic per name.
_GLYPH_COLORS = ("#5b7fa6", "#7a9a5b", "#a67a5b", "#8a6fa6",
                 "#5ba69a", "#a65b7a", "#9a8a4a", "#6b8a9a")


def make_program_icon(name):
    """A per-program glyph: the program's first letter on a solid
    colour picked from its name. Used when the program has no icon
    of its own, so programs are told apart at a glance instead of
    all showing the app icon."""
    text = (name or "?").strip() or "?"
    letter = text[0].upper()
    colour = _GLYPH_COLORS[sum(text.encode("utf-8"))
                           % len(_GLYPH_COLORS)]
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(colour))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 44, 44, 8, 8)
    painter.setPen(QColor("#ffffff"))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(26)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter,
                     letter)
    painter.end()
    return QIcon(pixmap)


def format_threats_text(target, threats):
    """Threats dialog text: the file once, then the deduplicated
    threat names (never the full path repeated per threat)."""
    names = sorted({name for _, name in threats})
    return "%s\n\nThreats found:\n%s" % (target, "\n".join(names))


class ActivityHighlighter(QSyntaxHighlighter):
    """Colours WARNING lines amber and ERROR lines red in the
    Activity feed, so problems stand out from the routine lines."""

    def __init__(self, document):
        super().__init__(document)
        self._warn = QTextCharFormat()
        self._warn.setForeground(QColor("#9a6a00"))
        self._warn.setFontWeight(QFont.Weight.Bold)
        self._error = QTextCharFormat()
        self._error.setForeground(QColor("#c00000"))
        self._error.setFontWeight(QFont.Weight.Bold)

    def highlightBlock(self, text):
        if "ERROR" in text:
            self.setFormat(0, len(text), self._error)
        elif "WARNING" in text:
            self.setFormat(0, len(text), self._warn)


class MiddleElideDelegate(QStyledItemDelegate):
    """Paints cell text elided in the middle (…/FooApp/foo.exe), so
    the start and the end of a path stay visible. QTableWidget's
    own textElideMode does not take effect in this Qt build (verified
    offscreen: ElideMiddle still rendered as ElideRight), so the
    delegate elides the text itself at paint time, using the live
    cell width."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        text = option.text
        if not text:
            return
        width = option.rect.width() - 8
        if option.icon is not None and not option.icon.isNull():
            width -= option.decorationSize.width() + 4
        if width <= 0:
            return
        option.text = option.fontMetrics.elidedText(
            text, Qt.TextElideMode.ElideMiddle, width)


# ---------------------------------------------------------------- data gathering

def gather_all():
    """Everything the window shows, gathered in one worker pass."""
    programs = common.scan_programs()
    for prog in programs:
        if prog["size_bytes"] is None and prog["location"]:
            prog["size_bytes"] = common.dir_size(prog["location"])
        prog["running"] = common.is_running(prog["exe"])
    prefix = common.wine_prefix()
    return {
        "programs": programs,
        "plugins": common.scan_plugins(),
        "quarantine": scanner.load_quarantine(),
        "failed": watcher.load_failed(),
        "wine_version": common.get_wine_version(),
        "yabridge": common.get_yabridge_status(),
        "scanner": scanner.scanner_status(),
        "prefix": str(prefix),
        "prefix_exists": prefix.is_dir(),
        "prefix_size": common.dir_size(prefix) if prefix.is_dir() else 0,
        "service": common.watcher_service_status(),
        "log": common.read_log_tail(),
    }


# ---------------------------------------------------------------- uninstall steps

def uninstall_steps():
    """The uninstall.sh steps, in Python (for the GUI uninstaller).
    Returns a list of plain-word notes. Never raises."""
    notes = []
    home = common.home()
    layer = common.layer_dir()

    def run(cmd, timeout=60):
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            pass

    run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
    try:
        (home / ".config" / "systemd" / "user"
         / "winlinx-watcher.service").unlink()
    except OSError:
        pass
    run(["systemctl", "--user", "daemon-reload"])
    notes.append("Watcher service stopped")

    wine_handler = (
        Path("/usr/share/applications/wine.desktop").is_file()
        or (home / ".local" / "share" / "applications"
            / "wine.desktop").is_file())
    if wine_handler and shutil.which("xdg-mime"):
        run(["xdg-mime", "default", "wine.desktop",
             "application/x-msdownload"])
        run(["xdg-mime", "default", "wine.desktop",
             "application/x-msi"])
    mimeapps = home / ".config" / "mimeapps.list"
    try:
        if mimeapps.is_file():
            lines = mimeapps.read_text(encoding="utf-8",
                                       errors="replace").splitlines()
            kept = [line for line in lines
                    if "winlinx-runner.desktop" not in line]
            mimeapps.write_text("\n".join(kept) + "\n",
                                encoding="utf-8")
    except OSError:
        pass
    try:
        (home / ".local" / "share" / "mime" / "packages"
         / "winlinx.xml").unlink()
    except OSError:
        pass
    if shutil.which("update-mime-database"):
        run(["update-mime-database",
             str(home / ".local" / "share" / "mime")])
    for name in ("winlinx-manager.desktop", "winlinx-runner.desktop"):
        try:
            (home / ".local" / "share" / "applications"
             / name).unlink()
        except OSError:
            pass
    notes.append("File associations removed")

    qdir = layer / "quarantine"
    try:
        if qdir.is_dir() and any(qdir.iterdir()):
            keep = home / "WinLinx" / "Quarantine"
            keep.mkdir(parents=True, exist_ok=True)
            for entry in qdir.iterdir():
                target = keep / entry.name
                if entry.is_dir() and not entry.is_symlink():
                    shutil.copytree(str(entry), str(target),
                                    dirs_exist_ok=True)
                else:
                    shutil.copy2(str(entry), str(target))
            notes.append(
                "Quarantined files kept in ~/WinLinx/Quarantine")
    except OSError:
        pass
    try:
        shutil.rmtree(layer)
        notes.append("WinLinx files removed")
    except OSError as exc:
        notes.append("Could not remove WinLinx files: %s" % exc)
    return notes


# ---------------------------------------------------------------- How to Use (U22)

HOW_TO_USE = [
    ("Getting started",
     "WinLinx makes Windows programs and VST plug-ins just work on "
     "Linux. There is nothing to set up per file: drop a file in a "
     "watched folder, or double-click it, and WinLinx scans it and "
     "then runs or bridges it.\n\n"
     "The watched folders are ~/WinLinx/Programs (programs and "
     "installers) and ~/WinLinx/Plugins (plug-in installers and "
     "plug-in files). The watcher runs in the background and picks "
     "up anything you drop there."),
    ("Installing a program",
     "Drop a .exe or .msi into ~/WinLinx/Programs, double-click it "
     "anywhere on the PC, or use the Install Program button in the "
     "Programs tab. It is scanned first, then it runs with Wine.\n\n"
     "Installed programs show in the Programs tab. Launch, Uninstall, "
     "or Open Install Folder from the buttons, or right-click a "
     "program for more options."),
    ("Installing a plug-in",
     "Drop a plug-in installer (.exe / .msi) or a plug-in file "
     "(.vst3 / .dll / .clap) into ~/WinLinx/Plugins, or "
     "double-click the file. It is scanned, installed, and bridged "
     "with yabridge into the standard ~/.vst3, ~/.vst, and ~/.clap "
     "folders, so whatever DAW you use can find it. No DAW is "
     "assumed.\n\n"
     "The Plug-ins tab shows every bridged plug-in and its status. "
     "Re-sync Bridges rebuilds the bridges if a DAW cannot see a "
     "plug-in."),
    ("Quarantine",
     "When the scanner flags a file, it is moved to quarantine and "
     "never installs or runs. The Quarantine tab lists flagged files "
     "with the threat name.\n\n"
     "Allow puts a file you trust back where it came from - it will "
     "then install or run as normal. Delete removes it for good. "
     "Allowed files are remembered by their contents: if a file "
     "changes afterwards, it is scanned again."),
    ("Failed drops",
     "When a dropped file cannot be handled (Wine is not installed, "
     "a copy failed, a bridge sync failed), it shows in the Failed "
     "Drops tab with the reason. WinLinx retries failed drops on its "
     "own, so installing Wine later fixes them without you doing "
     "anything.\n\n"
     "Retry tries a failed drop again right away. Forget tells "
     "WinLinx to leave the file alone unless it changes."),
    ("Per-program prefixes",
     "Every program normally runs in the shared Wine prefix (~/.wine). "
     "Right-click a program and pick 'Use its own prefix' to give it "
     "a private prefix instead, with its own settings and its own "
     "Windows version (Windows 10, 11, 7, XP, or 8).\n\n"
     "A program with its own prefix still launches from the Programs "
     "tab exactly the same way. Use 'Use its own prefix' again to "
     "switch it back to the shared prefix."),
    ("Backing up",
     "File menu, Back Up Prefix: saves the shared prefix and every "
     "per-program prefix into ~/WinLinx/Backups as one file.\n\n"
     "File menu, Restore Prefix: picks a backup and puts it back. "
     "The current prefix is moved aside (not deleted) first, so a "
     "restore can be undone by hand. The aside copy sits next to "
     "the prefix with .before-restore- in its name, and WinLinx "
     "keeps only the newest aside copy."),
    ("Updates",
     "Help menu, Check for Updates: asks GitHub for the latest "
     "WinLinx release. When a newer version exists, you can download "
     "it (it lands in ~/Downloads - unzip it and double-click "
     "Install WinLinx) or open the release page."),
    ("When something goes wrong",
     "If ClamAV is missing, a banner at the top of the window says "
     "so, with an Install button - files run unscanned until it is "
     "installed.\n\n"
     "The System tab shows Wine, yabridge, scanner, and watcher "
     "status, plus the recent log. Help menu, Debug Report writes a "
     "scrubbed report (no personal details) you can send with a bug "
     "report."),
]


class HowToUseDialog(QDialog):
    """The built-in How to Use index (U22): topics on the left,
    plain words on the right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("How to Use WinLinx")
        self.resize(720, 480)
        outer = QVBoxLayout(self)
        body = QHBoxLayout()
        self.index = QListWidget()
        for title, _text in HOW_TO_USE:
            self.index.addItem(QListWidgetItem(title))
        self.index.currentRowChanged.connect(self._show_topic)
        body.addWidget(self.index, 1)
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(True)
        body.addWidget(self.text, 2)
        outer.addLayout(body, 1)
        row = QHBoxLayout()
        row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        outer.addLayout(row)
        self.index.setCurrentRow(0)

    def _show_topic(self, row):
        if 0 <= row < len(HOW_TO_USE):
            title, text = HOW_TO_USE[row]
            self.text.setPlainText(title + "\n\n" + text)


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About WinLinx")
        layout = QVBoxLayout(self)
        label = QLabel(
            "<b>WinLinx %s</b><br><br>"
            "Windows programs and VST plug-ins on Linux, zero setup.<br><br>"
            "Drop a file in ~/WinLinx/Programs or ~/WinLinx/Plugins, "
            "or double-click it - it is scanned, then it runs or is "
            "bridged.<br><br>"
            "Broke n' Bored Studio" % common.__version__)
        label.setWordWrap(True)
        layout.addWidget(label)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)


class UninstallDialog(QDialog):
    """The GUI uninstaller: its own window (U1), a confirm that
    defaults to No (U23), progress while it works (U26)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Uninstall WinLinx")
        self._worker = None
        layout = QVBoxLayout(self)
        label = QLabel(
            "This removes WinLinx: the watcher service, the file "
            "associations, the menu entry, and WinLinx's own files.\n\n"
            "It leaves alone, on purpose: Wine, yabridge, ClamAV, "
            "your Wine prefix (~/.wine), ~/WinLinx, and quarantined "
            "files (moved to ~/WinLinx/Quarantine).\n\n"
            "Per-program prefixes live inside WinLinx's own files, "
            "so they are removed too - back them up first (File menu, "
            "Back Up Prefix) if you need them.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.status_label = QLabel("")
        layout.addWidget(self.status_label)
        row = QHBoxLayout()
        self.uninstall_btn = QPushButton("Uninstall")
        self.uninstall_btn.clicked.connect(self._do_uninstall)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        # The safe button has the focus and is the default, so Enter
        # never starts the uninstall (the confirm behind it defaults
        # to No as well, but the first step should be safe too).
        self.cancel_btn.setDefault(True)
        self.cancel_btn.setFocus()
        row.addWidget(self.uninstall_btn)
        row.addWidget(self.cancel_btn)
        row.addStretch()
        layout.addLayout(row)

    def _do_uninstall(self):
        if not confirm_destructive(
                self, "Uninstall WinLinx",
                "Uninstall WinLinx?\n\nWine, yabridge, ClamAV, your "
                "Wine prefix, and ~/WinLinx stay put."):
            return
        self.uninstall_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.status_label.setText("Uninstalling…")
        self._worker = Worker(uninstall_steps, self)

        def done(notes):
            self._worker = None
            QMessageBox.information(
                self, "WinLinx uninstalled",
                "WinLinx is uninstalled.\n\n" + "\n".join(notes))
            self.accept()
            QApplication.instance().quit()

        def failed(message):
            self._worker = None
            self.status_label.setText("Error: %s" % message)
            self.uninstall_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)

        self._worker.finished.connect(done)
        self._worker.failed.connect(failed)
        self._worker.start()


# ---------------------------------------------------------------- window

class ManagerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WinLinx Program Manager %s"
                            % common.__version__)
        if ICON_PATH.is_file():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(960, 600)
        self._settings = QSettings("BrokeNBored", "WinLinx")
        saved = self._settings.value("geometry")
        if saved is not None:
            self.restoreGeometry(saved)
        self._worker = None
        self._busy_buttons = []
        self._programs = []
        self._plugins = []
        self._quarantine = []
        self._failed = []
        self._last_activity = ""
        self._activity_full_text = ""
        # An action result to show after the next refresh finishes
        # (a refresh overwrites the status bar, so a message set
        # before one would never be seen).
        self._pending_message = None
        self._howto_dialog = None

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        self._build_banner(root_layout)
        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self._build_programs_tab()
        self._build_plugins_tab()
        self._build_quarantine_tab()
        self._build_failed_tab()
        self._build_activity_tab()
        self._build_system_tab()
        # Auto-refresh every 30 s (View menu toggles it).
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(AUTO_REFRESH_MS)
        self._auto_timer.timeout.connect(
            lambda: self.refresh(quiet=True))
        if self._settings.value("auto_refresh", True, type=bool):
            self._auto_timer.start()
        # Activity feed poll.
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(ACTIVITY_POLL_MS)
        self._activity_timer.timeout.connect(self._poll_activity)
        self._activity_timer.start()
        self._build_menus()
        self._build_shortcuts()
        self.statusBar().showMessage("Ready")
        self.refresh()

    # -- construction ----------------------------------------------

    def _register_busy(self, button):
        self._busy_buttons.append(button)
        return button

    def _set_busy(self, busy):
        for button in self._busy_buttons:
            button.setEnabled(not busy)
        if not busy:
            self._update_action_buttons()

    def _update_action_buttons(self):
        """Action buttons that need a row are enabled only when a
        row is selected (and nothing is busy). Buttons that do not
        need a selection are left alone."""
        busy = self._worker is not None and self._worker.isRunning()
        prog = self._selected_row_data(self.programs_table) is not None
        plug = self._selected_row_data(self.plugins_table) is not None
        quar = self._selected_row_data(self.quarantine_table) is not None
        fail = self._selected_row_data(self.failed_table) is not None
        self.launch_btn.setEnabled(not busy and prog)
        self.uninstall_btn.setEnabled(not busy and prog)
        self.open_folder_btn.setEnabled(not busy and prog)
        self.remove_plugin_btn.setEnabled(not busy and plug)
        self.allow_btn.setEnabled(not busy and quar)
        self.delete_btn.setEnabled(not busy and quar)
        self.retry_btn.setEnabled(not busy and fail)
        self.forget_btn.setEnabled(not busy and fail)

    def _refresh_with_message(self, message):
        """Refresh, then show this result message once the refresh
        has finished (so the refresh summary does not eat it)."""
        self._pending_message = message
        self.refresh()

    def _show_summary_status(self):
        self.statusBar().showMessage(
            "%d programs, %d plug-ins, %d quarantined, %d failed"
            % (len(self._programs), len(self._plugins),
               len(self._quarantine), len(self._failed)))

    def _build_banner(self, layout):
        """The unscanned warning (item 7): visible whenever ClamAV
        is missing, with an Install button. Fail-open stays, but it
        is visible now, not just a log line."""
        self.banner = QWidget()
        self.banner.setStyleSheet(
            "background-color: #fff3cd; color: #664d03;")
        row = QHBoxLayout(self.banner)
        row.setContentsMargins(8, 4, 8, 4)
        self.banner_label = QLabel(
            "ClamAV is not installed, so files run unscanned. "
            "Install it to scan files before they run.")
        self.banner_label.setWordWrap(True)
        row.addWidget(self.banner_label, 1)
        self.banner_install_btn = QPushButton("Install ClamAV")
        self.banner_install_btn.clicked.connect(self._install_clamav)
        row.addWidget(self.banner_install_btn)
        self.banner.setVisible(False)
        layout.addWidget(self.banner)

    def _delete_shortcut(self, table, slot):
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), table)
        shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        shortcut.activated.connect(slot)
        return shortcut

    def _build_programs_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search programs…")
        self.search_box.textChanged.connect(self._filter_programs)
        layout.addWidget(self.search_box)
        self.programs_empty_label = QLabel(
            "No programs yet. Drop a Windows program into "
            "~/WinLinx/Programs, or press Install Program. "
            "Press F1 for How to Use.")
        self.programs_empty_label.setWordWrap(True)
        layout.addWidget(self.programs_empty_label)
        self.programs_table = QTableWidget(0, 6)
        self.programs_table.setHorizontalHeaderLabels(
            ["Name", "Installed", "Size", "Location", "Running",
             "Prefix"])
        self.programs_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.programs_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.programs_table.setSortingEnabled(True)
        self.programs_table.setTextElideMode(
            Qt.TextElideMode.ElideMiddle)
        self.programs_table.setItemDelegate(
            MiddleElideDelegate(self.programs_table))
        self.programs_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.programs_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        self.programs_table.setColumnWidth(1, 100)
        self.programs_table.setColumnWidth(2, 80)
        self.programs_table.setColumnWidth(4, 95)
        self.programs_table.setColumnWidth(5, 110)
        # activated covers Enter and double-click (one connection, so
        # a double-click never launches twice).
        self.programs_table.activated.connect(self._launch_selected)
        self.programs_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.programs_table.customContextMenuRequested.connect(
            self._programs_menu)
        self.programs_table.selectionModel().selectionChanged.connect(
            lambda *_: self._update_action_buttons())
        self._delete_shortcut(self.programs_table,
                              self._uninstall_selected)
        layout.addWidget(self.programs_table)
        row = QHBoxLayout()
        self.launch_btn = self._register_busy(QPushButton("Launch"))
        self.launch_btn.clicked.connect(self._launch_selected)
        self.install_program_btn = self._register_busy(
            QPushButton("Install Program…"))
        self.install_program_btn.clicked.connect(self._install_program)
        self.uninstall_btn = self._register_busy(QPushButton("Uninstall"))
        self.uninstall_btn.clicked.connect(self._uninstall_selected)
        self.open_folder_btn = self._register_busy(
            QPushButton("Open Install Folder"))
        self.open_folder_btn.clicked.connect(self._open_folder_selected)
        self.programs_refresh_btn = self._register_busy(
            QPushButton("Refresh"))
        self.programs_refresh_btn.clicked.connect(self.refresh)
        for btn in (self.launch_btn, self.install_program_btn,
                    self.uninstall_btn, self.open_folder_btn,
                    self.programs_refresh_btn):
            row.addWidget(btn)
        row.addStretch()
        layout.addLayout(row)
        self.tabs.addTab(page, "Programs")

    def _build_plugins_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.plugins_search_box = QLineEdit()
        self.plugins_search_box.setPlaceholderText("Search plug-ins…")
        self.plugins_search_box.textChanged.connect(self._filter_plugins)
        layout.addWidget(self.plugins_search_box)
        self.plugins_empty_label = QLabel(
            "No plug-ins yet. Drop a plug-in into ~/WinLinx/Plugins, "
            "or press Install Plug-in. Press F1 for How to Use.")
        self.plugins_empty_label.setWordWrap(True)
        layout.addWidget(self.plugins_empty_label)
        self.plugins_hint_label = QLabel(
            "Not bridged? Press Re-sync Bridges. If a plug-in still "
            "does not show up in your DAW, yabridge may not be "
            "installed.")
        self.plugins_hint_label.setWordWrap(True)
        layout.addWidget(self.plugins_hint_label)
        self.plugins_table = QTableWidget(0, 4)
        self.plugins_table.setHorizontalHeaderLabels(
            ["Name", "Type", "Status", "Source File"])
        self.plugins_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.plugins_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.plugins_table.setSortingEnabled(True)
        self.plugins_table.setTextElideMode(
            Qt.TextElideMode.ElideMiddle)
        self.plugins_table.setItemDelegate(
            MiddleElideDelegate(self.plugins_table))
        self.plugins_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.plugins_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        self.plugins_table.setColumnWidth(1, 70)
        self.plugins_table.setColumnWidth(2, 115)
        self.plugins_table.selectionModel().selectionChanged.connect(
            lambda *_: self._update_action_buttons())
        self._delete_shortcut(self.plugins_table, self._remove_plugin)
        layout.addWidget(self.plugins_table)
        row = QHBoxLayout()
        self.install_plugin_btn = self._register_busy(
            QPushButton("Install Plug-in…"))
        self.install_plugin_btn.clicked.connect(self._install_plugin)
        self.resync_btn = self._register_busy(
            QPushButton("Re-sync Bridges"))
        self.resync_btn.clicked.connect(self._resync)
        self.remove_plugin_btn = self._register_busy(QPushButton("Remove"))
        self.remove_plugin_btn.clicked.connect(self._remove_plugin)
        self.plugins_refresh_btn = self._register_busy(
            QPushButton("Refresh"))
        self.plugins_refresh_btn.clicked.connect(self.refresh)
        for btn in (self.install_plugin_btn, self.resync_btn,
                    self.remove_plugin_btn, self.plugins_refresh_btn):
            row.addWidget(btn)
        row.addStretch()
        layout.addLayout(row)
        self.tabs.addTab(page, "Plug-ins")

    def _build_quarantine_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Files the scanner flagged before they could install or run. "
            "Allow restores a file you trust - it will then install or run "
            "as normal. Delete removes it for good.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.quarantine_search_box = QLineEdit()
        self.quarantine_search_box.setPlaceholderText(
            "Search quarantined files…")
        self.quarantine_search_box.textChanged.connect(
            self._filter_quarantine)
        layout.addWidget(self.quarantine_search_box)
        self.quarantine_nomatch_label = QLabel()
        self.quarantine_nomatch_label.setWordWrap(True)
        self.quarantine_nomatch_label.setVisible(False)
        layout.addWidget(self.quarantine_nomatch_label)
        self.quarantine_table = QTableWidget(0, 4)
        self.quarantine_table.setHorizontalHeaderLabels(
            ["Name", "Threat", "Original Location", "Date"])
        self.quarantine_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.quarantine_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.quarantine_table.setSortingEnabled(True)
        self.quarantine_table.setTextElideMode(
            Qt.TextElideMode.ElideMiddle)
        self.quarantine_table.setItemDelegate(
            MiddleElideDelegate(self.quarantine_table))
        self.quarantine_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.quarantine_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        self.quarantine_table.setColumnWidth(1, 220)
        self.quarantine_table.setColumnWidth(3, 100)
        self.quarantine_table.selectionModel().selectionChanged.connect(
            lambda *_: self._update_action_buttons())
        self._delete_shortcut(self.quarantine_table,
                              self._delete_quarantined)
        layout.addWidget(self.quarantine_table)
        row = QHBoxLayout()
        self.allow_btn = self._register_busy(
            QPushButton("Allow (Install Anyway)"))
        self.allow_btn.clicked.connect(self._allow_selected)
        self.delete_btn = self._register_busy(QPushButton("Delete"))
        self.delete_btn.clicked.connect(self._delete_quarantined)
        self.quarantine_refresh_btn = self._register_busy(
            QPushButton("Refresh"))
        self.quarantine_refresh_btn.clicked.connect(self.refresh)
        for btn in (self.allow_btn, self.delete_btn,
                    self.quarantine_refresh_btn):
            row.addWidget(btn)
        row.addStretch()
        layout.addLayout(row)
        self.tabs.addTab(page, "Quarantine")

    def _build_failed_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Files the watcher could not handle, with the reason. "
            "WinLinx retries them on its own. Retry tries one right "
            "away; Forget tells WinLinx to leave it alone unless it "
            "changes.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.failed_search_box = QLineEdit()
        self.failed_search_box.setPlaceholderText(
            "Search failed drops…")
        self.failed_search_box.textChanged.connect(self._filter_failed)
        layout.addWidget(self.failed_search_box)
        self.failed_table = QTableWidget(0, 4)
        self.failed_table.setHorizontalHeaderLabels(
            ["File", "Reason", "Attempts", "Last Tried"])
        self.failed_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.failed_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.failed_table.setSortingEnabled(True)
        self.failed_table.setTextElideMode(
            Qt.TextElideMode.ElideMiddle)
        self.failed_table.setItemDelegate(
            MiddleElideDelegate(self.failed_table))
        self.failed_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.failed_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.failed_table.setColumnWidth(1, 260)
        self.failed_table.setColumnWidth(2, 80)
        self.failed_table.setColumnWidth(3, 150)
        self.failed_table.selectionModel().selectionChanged.connect(
            lambda *_: self._update_action_buttons())
        self._delete_shortcut(self.failed_table, self._forget_failed)
        layout.addWidget(self.failed_table)
        row = QHBoxLayout()
        self.retry_btn = self._register_busy(QPushButton("Retry"))
        self.retry_btn.clicked.connect(self._retry_failed)
        self.forget_btn = self._register_busy(QPushButton("Forget"))
        self.forget_btn.clicked.connect(self._forget_failed)
        self.failed_refresh_btn = self._register_busy(
            QPushButton("Refresh"))
        self.failed_refresh_btn.clicked.connect(self.refresh)
        for btn in (self.retry_btn, self.forget_btn,
                    self.failed_refresh_btn):
            row.addWidget(btn)
        row.addStretch()
        layout.addLayout(row)
        self.tabs.addTab(page, "Failed Drops")

    def _build_activity_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        self.activity_dot = QLabel()
        self.activity_dot.setFixedSize(12, 12)
        self.activity_dot.setStyleSheet(
            "background-color: #888888; border-radius: 6px;")
        top.addWidget(self.activity_dot)
        self.activity_label = QLabel(
            "Watching ~/WinLinx/Programs and ~/WinLinx/Plugins")
        top.addWidget(self.activity_label, 1)
        self.activity_warnings_only = QCheckBox("Warnings only")
        self.activity_warnings_only.setToolTip(
            "Show only warning and error lines")
        self.activity_warnings_only.toggled.connect(
            self._render_activity)
        top.addWidget(self.activity_warnings_only)
        layout.addLayout(top)
        self.activity_view = QPlainTextEdit()
        self.activity_view.setReadOnly(True)
        self._activity_highlighter = ActivityHighlighter(
            self.activity_view.document())
        layout.addWidget(self.activity_view)
        self.tabs.addTab(page, "Activity")

    def _build_system_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)
        self.watcher_hint_label = QLabel()
        self.watcher_hint_label.setWordWrap(True)
        layout.addWidget(self.watcher_hint_label)
        repair_row = QHBoxLayout()
        self.repair_btn = self._register_busy(
            QPushButton("Install or Repair Wine and yabridge…"))
        self.repair_btn.setToolTip(
            "Opens the installer, which sets up Wine and yabridge "
            "again.")
        self.repair_btn.clicked.connect(self._repair_install)
        repair_row.addWidget(self.repair_btn)
        repair_row.addStretch()
        layout.addLayout(repair_row)
        layout.addWidget(QLabel("Recent activity:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)
        row = QHBoxLayout()
        self.open_programs_btn = self._register_busy(
            QPushButton("Open Programs Folder"))
        self.open_programs_btn.clicked.connect(
            lambda: self._open_path(common.programs_folder()))
        self.open_plugins_btn = self._register_busy(
            QPushButton("Open Plug-ins Folder"))
        self.open_plugins_btn.clicked.connect(
            lambda: self._open_path(common.plugins_folder()))
        self.scan_btn = self._register_busy(
            QPushButton("Scan a File or Folder…"))
        self.scan_btn.clicked.connect(self._scan_picker)
        for btn in (self.open_programs_btn, self.open_plugins_btn,
                    self.scan_btn):
            row.addWidget(btn)
        row.addStretch()
        layout.addLayout(row)
        row2 = QHBoxLayout()
        self.update_sigs_btn = self._register_busy(
            QPushButton("Update Signatures"))
        self.update_sigs_btn.clicked.connect(self._update_signatures)
        self.backup_btn = self._register_busy(
            QPushButton("Back Up Prefix…"))
        self.backup_btn.clicked.connect(self._backup_prefix)
        self.restore_btn = self._register_busy(
            QPushButton("Restore Prefix…"))
        self.restore_btn.clicked.connect(self._restore_prefix)
        self.system_refresh_btn = self._register_busy(
            QPushButton("Refresh"))
        self.system_refresh_btn.clicked.connect(self.refresh)
        for btn in (self.update_sigs_btn, self.backup_btn,
                    self.restore_btn, self.system_refresh_btn):
            row2.addWidget(btn)
        row2.addStretch()
        layout.addLayout(row2)
        self.tabs.addTab(page, "System")

    def _build_menus(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        install_prog = file_menu.addAction("Install Program…")
        install_prog.setShortcut(QKeySequence("Ctrl+I"))
        install_prog.triggered.connect(self._install_program)
        install_plug = file_menu.addAction("Install Plug-in…")
        install_plug.triggered.connect(self._install_plugin)
        file_menu.addSeparator()
        backup = file_menu.addAction("Back Up Prefix…")
        backup.triggered.connect(self._backup_prefix)
        restore = file_menu.addAction("Restore Prefix…")
        restore.triggered.connect(self._restore_prefix)
        file_menu.addSeparator()
        uninstall = file_menu.addAction("Uninstall WinLinx…")
        uninstall.triggered.connect(self._open_uninstaller)
        file_menu.addSeparator()
        exit_action = file_menu.addAction("Exit")
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.close)

        view_menu = menubar.addMenu("View")
        refresh_action = view_menu.addAction("Refresh")
        refresh_action.setShortcut(QKeySequence("Ctrl+R"))
        refresh_action.triggered.connect(self.refresh)
        self.auto_refresh_action = view_menu.addAction("Auto-refresh")
        self.auto_refresh_action.setCheckable(True)
        self.auto_refresh_action.setChecked(
            self._auto_timer.isActive())
        self.auto_refresh_action.toggled.connect(
            self._toggle_auto_refresh)

        help_menu = menubar.addMenu("Help")
        howto = help_menu.addAction("How to Use")
        howto.setShortcut(QKeySequence("F1"))
        howto.triggered.connect(self._open_howto)
        debug = help_menu.addAction("Debug Report")
        debug.triggered.connect(self._debug_report)
        updates2 = help_menu.addAction("Check for Updates")
        updates2.triggered.connect(self._check_updates)
        help_menu.addSeparator()
        about = help_menu.addAction("About WinLinx")
        about.triggered.connect(self._open_about)

    def _build_shortcuts(self):
        refresh_f5 = QShortcut(QKeySequence(Qt.Key.Key_F5), self)
        refresh_f5.activated.connect(self.refresh)

    # -- worker plumbing --------------------------------------------

    def _run_worker(self, func, on_done, busy_message, quiet=False):
        if self._worker is not None and self._worker.isRunning():
            if not quiet:
                self.statusBar().showMessage("Already working, hold on…")
            return
        self._set_busy(True)
        self.statusBar().showMessage(busy_message)
        self._worker = Worker(func, self)

        def done(result):
            worker = self._worker
            self._set_busy(False)
            self._worker = None
            on_done(result)
            if worker is not None:
                worker.deleteLater()

        def failed(message):
            worker = self._worker
            self._set_busy(False)
            self._worker = None
            # The raw error goes in the log only; the dialog says it
            # in plain words, never as a bare exception string.
            common.append_log("ERROR in background task: %s" % message)
            self.statusBar().showMessage("Something went wrong")
            QMessageBox.warning(
                self, "Something went wrong",
                "Something went wrong while working. The details are "
                "in the log on the System tab.")
            if worker is not None:
                worker.deleteLater()

        self._worker.finished.connect(done)
        self._worker.failed.connect(failed)
        self._worker.start()

    # -- refresh -----------------------------------------------------

    def refresh(self, quiet=False):
        self._run_worker(gather_all, self._populate, "Scanning…",
                         quiet=quiet)

    def _save_table_state(self, table):
        """Selected row key + scroll position, so a refresh can put
        the view back where it was (U28)."""
        selected = None
        rows = table.selectionModel().selectedRows()
        if rows:
            item = table.item(rows[0].row(), 0)
            if item is not None:
                data = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(data, dict):
                    selected = (data.get("name") or data.get("path")
                                or data.get("id"))
        return {
            "selected": selected,
            "h": table.horizontalScrollBar().value(),
            "v": table.verticalScrollBar().value(),
        }

    def _restore_table_state(self, table, state):
        if state["selected"] is not None:
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                if item is None:
                    continue
                data = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(data, dict) and (
                        data.get("name") == state["selected"]
                        or data.get("path") == state["selected"]
                        or data.get("id") == state["selected"]):
                    table.selectRow(row)
                    break
        table.horizontalScrollBar().setValue(state["h"])
        table.verticalScrollBar().setValue(state["v"])

    def _populate(self, data):
        self._programs = data["programs"]
        self._plugins = data["plugins"]
        self._quarantine = data["quarantine"]
        self._failed = data["failed"]
        states = {table: self._save_table_state(table) for table in (
            self.programs_table, self.plugins_table,
            self.quarantine_table, self.failed_table)}
        self._fill_programs()
        self._fill_plugins()
        self._fill_quarantine()
        self._fill_failed()
        for table, state in states.items():
            self._restore_table_state(table, state)
        scanstat = data["scanner"]
        self.banner.setVisible(not scanstat["installed"])
        if scanstat["installed"]:
            sig = scanstat["sig_date"] or "unknown date"
            scanner_line = "ClamAV %s (signatures: %s)" % (
                scanstat["version"].split("/")[0].replace("ClamAV ", ""),
                sig)
        else:
            scanner_line = "ClamAV not installed"
        if data["prefix_exists"]:
            prefix_line = "%s (%s used)" % (
                data["prefix"], common.format_size(data["prefix_size"]))
        else:
            prefix_line = "%s (No prefix yet)" % data["prefix"]
        self.info_label.setText(
            "Wine: %s\n"
            "yabridge: %s\n"
            "Scanner: %s\n"
            "Prefix: %s\n"
            "Watcher service: %s\n"
            "Quarantined items: %d\n"
            "Failed drops: %d" % (
                data["wine_version"] or "Not installed",
                data["yabridge"],
                scanner_line,
                prefix_line,
                data["service"],
                len(data["quarantine"]),
                len(data["failed"])))
        service = data["service"]
        if service == "active":
            watcher_hint = ("Watcher is running: dropped files are "
                            "picked up automatically.")
        elif service == "unknown":
            watcher_hint = ("Watcher status is unknown. If dropped "
                            "files are not picked up, press Install "
                            "or Repair.")
        else:
            watcher_hint = ("Watcher is %s, so dropped files will not "
                            "be picked up. Press Install or Repair to "
                            "fix it." % service)
        self.watcher_hint_label.setText(watcher_hint)
        self.log_view.setPlainText(data["log"])
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum())
        self._show_summary_status()
        n_quar = len(data["quarantine"])
        n_fail = len(data["failed"])
        self.tabs.setTabText(
            2, "Quarantine" if n_quar == 0
            else "Quarantine (%d)" % n_quar)
        self.tabs.setTabText(
            3, "Failed Drops" if n_fail == 0
            else "Failed Drops (%d)" % n_fail)
        self.programs_empty_label.setVisible(
            len(data["programs"]) == 0)
        self.plugins_empty_label.setVisible(
            len(data["plugins"]) == 0)
        self._update_activity_dot(data["service"])
        self._filter_programs()
        self._filter_plugins()
        self._filter_quarantine()
        self._filter_failed()
        self._update_action_buttons()
        if self._pending_message:
            self.statusBar().showMessage(self._pending_message)
            self._pending_message = None
        self._poll_activity(force=True)

    def _fill_programs(self):
        table = self.programs_table
        table.setSortingEnabled(False)
        table.setRowCount(len(self._programs))
        for row, prog in enumerate(self._programs):
            name_item = QTableWidgetItem(prog["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, prog)
            icon_path = prog.get("icon")
            if icon_path:
                name_item.setIcon(QIcon(icon_path))
            else:
                name_item.setIcon(make_program_icon(prog["name"]))
            table.setItem(row, 0, name_item)
            table.setItem(row, 1, QTableWidgetItem(prog["install_date"]))
            table.setItem(row, 2, SizeItem(prog["size_bytes"]))
            location_item = QTableWidgetItem(prog["location"] or "")
            location_item.setToolTip(prog["location"] or "")
            table.setItem(row, 3, location_item)
            table.setItem(row, 4, QTableWidgetItem(
                "Running" if prog.get("running") else "Not running"))
            if prog.get("prefix"):
                prefix_text = "Own prefix"
                prefix_tip = ("Own prefix: this program runs in its own "
                              "Wine prefix. See How to Use, "
                              "Per-program prefixes.")
            else:
                prefix_text = "Shared prefix"
                prefix_tip = ("Shared prefix: this program runs in the "
                              "shared Wine prefix. See How to Use, "
                              "Per-program prefixes.")
            prefix_item = QTableWidgetItem(prefix_text)
            prefix_item.setToolTip(prefix_tip)
            table.setItem(row, 5, prefix_item)
        table.setSortingEnabled(True)

    def _fill_plugins(self):
        table = self.plugins_table
        table.setSortingEnabled(False)
        table.setRowCount(len(self._plugins))
        for row, plug in enumerate(self._plugins):
            name_item = QTableWidgetItem(plug["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, plug)
            table.setItem(row, 0, name_item)
            table.setItem(row, 1, QTableWidgetItem(plug["kind"]))
            status_item = QTableWidgetItem(plug["status"])
            if plug["status"] == "Not bridged":
                status_item.setToolTip(
                    "Not bridged yet. Press Re-sync Bridges to bridge "
                    "it for your DAW.")
            elif plug["status"] == "Source missing":
                status_item.setToolTip(
                    "The Windows plug-in file is gone. Reinstall the "
                    "plug-in, or remove this entry.")
            else:
                status_item.setToolTip(
                    "Bridged. Your DAW can find it.")
            table.setItem(row, 2, status_item)
            shown = plug.get("dropped") or plug["source"]
            source_item = QTableWidgetItem(shown)
            if (plug.get("dropped") and plug["source"]
                    and plug["dropped"] != plug["source"]):
                source_item.setToolTip(
                    "Dropped file: %s\nInstalled copy: %s"
                    % (plug["dropped"], plug["source"]))
            else:
                source_item.setToolTip(shown)
            table.setItem(row, 3, source_item)
        table.setSortingEnabled(True)

    def _fill_quarantine(self):
        table = self.quarantine_table
        table.setSortingEnabled(False)
        table.setRowCount(len(self._quarantine))
        for row, entry in enumerate(self._quarantine):
            name_item = QTableWidgetItem(entry["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, entry)
            table.setItem(row, 0, name_item)
            threats_text = ", ".join(entry["threats"])
            threat_item = QTableWidgetItem(threats_text)
            threat_item.setToolTip(threats_text)
            table.setItem(row, 1, threat_item)
            original_item = QTableWidgetItem(entry["original"])
            original_item.setToolTip(entry["original"])
            table.setItem(row, 2, original_item)
            table.setItem(row, 3, QTableWidgetItem(entry["date"][:10]))
        table.setSortingEnabled(True)

    def _fill_failed(self):
        table = self.failed_table
        table.setSortingEnabled(False)
        table.setRowCount(len(self._failed))
        for row, item in enumerate(self._failed):
            name_item = QTableWidgetItem(item["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, item)
            table.setItem(row, 0, name_item)
            reason = common.plain_drop_reason(item["reason"])
            reason_item = QTableWidgetItem(reason)
            reason_item.setToolTip(reason)
            table.setItem(row, 1, reason_item)
            table.setItem(row, 2,
                          QTableWidgetItem(str(item["attempts"])))
            when = item["time"][:16].replace("T", " ")
            table.setItem(row, 3, QTableWidgetItem(when))
        table.setSortingEnabled(True)

    # -- filters ------------------------------------------------------

    def _filter_table(self, table, query, fields):
        query = query.strip().lower()
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is None:
                continue
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            parts = []
            for field in fields:
                value = data.get(field, "")
                if isinstance(value, (list, tuple)):
                    parts.append(" ".join(str(v) for v in value))
                else:
                    parts.append(str(value))
            hay = " ".join(parts)
            table.setRowHidden(row, query not in hay.lower())

    def _filter_programs(self):
        self._filter_table(self.programs_table, self.search_box.text(),
                           ("name", "location"))

    def _filter_plugins(self):
        self._filter_table(self.plugins_table,
                           self.plugins_search_box.text(),
                           ("name", "kind", "status", "source"))

    def _filter_quarantine(self):
        query = self.quarantine_search_box.text()
        self._filter_table(self.quarantine_table, query,
                           ("name", "original", "threats"))
        table = self.quarantine_table
        visible = sum(1 for row in range(table.rowCount())
                      if not table.isRowHidden(row))
        if query.strip():
            self.quarantine_nomatch_label.setText(
                'No quarantined files match "%s".'
                % query.strip())
            self.quarantine_nomatch_label.setVisible(visible == 0)
            self.statusBar().showMessage(
                "Showing %d of %d quarantined items"
                % (visible, table.rowCount()))
        else:
            self.quarantine_nomatch_label.setVisible(False)
            self._show_summary_status()

    def _filter_failed(self):
        self._filter_table(self.failed_table,
                           self.failed_search_box.text(),
                           ("name", "reason"))

    # -- activity ------------------------------------------------------

    def _update_activity_dot(self, service):
        active = service == "active"
        color = "#2fa84f" if active else "#888888"
        self.activity_dot.setStyleSheet(
            "background-color: %s; border-radius: 6px;" % color)
        self.activity_label.setText(
            "Watching ~/WinLinx/Programs and ~/WinLinx/Plugins - "
            "watcher %s" % service)

    def _poll_activity(self, force=False):
        text = common.read_log_tail(200)
        if text != self._last_activity or force:
            self._last_activity = text
            self._activity_full_text = text
            self._render_activity()

    def _render_activity(self):
        """Show the Activity feed, honouring the warnings-only
        filter. The System tab keeps the full log either way."""
        text = self._activity_full_text
        if self.activity_warnings_only.isChecked():
            lines = [line for line in text.splitlines()
                     if "WARNING" in line or "ERROR" in line]
            text = "\n".join(lines)
        self.activity_view.setPlainText(text)
        self.activity_view.verticalScrollBar().setValue(
            self.activity_view.verticalScrollBar().maximum())

    # -- selection helpers -------------------------------------------

    def _selected_row_data(self, table):
        rows = table.selectionModel().selectedRows()
        if not rows:
            return None
        item = table.item(rows[0].row(), 0)
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    # -- program actions ----------------------------------------------

    def _programs_menu(self, pos):
        prog = self._selected_row_data(self.programs_table)
        if prog is None:
            return
        menu = QMenu(self)
        launch = menu.addAction("Launch")
        launch.triggered.connect(self._launch_selected)
        uninstall = menu.addAction("Uninstall")
        uninstall.triggered.connect(self._uninstall_selected)
        folder = menu.addAction("Open Install Folder")
        folder.triggered.connect(self._open_folder_selected)
        kill = menu.addAction("Kill Process")
        kill.setEnabled(bool(prog.get("running")))
        kill.triggered.connect(self._kill_selected)
        menu.addSeparator()
        own = menu.addAction("Use its own prefix")
        own.setCheckable(True)
        own.setChecked(bool(prog.get("prefix")))
        own.triggered.connect(
            lambda checked: self._toggle_own_prefix(prog, checked))
        versions = menu.addMenu("Windows version")
        group = QActionGroup(versions)
        group.setExclusive(True)
        current = common.program_windows_version(prog.get("exe"))
        for key in common.WINDOWS_VERSIONS:
            action = versions.addAction(
                common.WINDOWS_VERSION_LABELS[key])
            action.setCheckable(True)
            action.setChecked(current == key)
            group.addAction(action)
            action.triggered.connect(
                lambda _checked, v=key: self._set_windows_version(
                    prog, v))
        menu.exec(self.programs_table.mapToGlobal(pos))

    def _launch_selected(self):
        prog = self._selected_row_data(self.programs_table)
        if prog is None:
            self.statusBar().showMessage("Select a program first")
            return
        exe = prog.get("exe")
        if not exe:
            QMessageBox.information(
                self, "Can't launch",
                "No launcher (.exe) was found for %s." % prog["name"])
            return
        if not common.find_wine():
            self.statusBar().showMessage(
                "Could not launch %s: Wine is not installed"
                % prog["name"])
            QMessageBox.warning(
                self, "Wine is not installed",
                "Wine is not installed, so %s cannot run. Re-run the "
                "installer to set it up." % prog["name"])
            return
        try:
            subprocess.Popen(["wine", exe], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             env=common.launch_env(exe),
                             start_new_session=True)
            self.statusBar().showMessage("Launched %s" % prog["name"])
        except OSError as exc:
            common.append_log("Launch failed for %s: %s"
                              % (prog["name"], exc))
            self.statusBar().showMessage(
                "Could not launch %s" % prog["name"])
            QMessageBox.warning(
                self, "Launch failed",
                "%s could not be started. Try again, or re-run the "
                "installer." % prog["name"])

    def _uninstall_selected(self):
        prog = self._selected_row_data(self.programs_table)
        if prog is None:
            self.statusBar().showMessage("Select a program first")
            return
        cmd = common.uninstall_command(prog)
        if not cmd:
            box = QMessageBox(self)
            box.setWindowTitle("Can't uninstall")
            box.setText(
                "No uninstaller was recorded for %s.\n\n"
                "You can still delete its folder from the install "
                "location." % prog["name"])
            folder_btn = None
            if prog.get("location"):
                folder_btn = box.addButton(
                    "Open Install Folder",
                    QMessageBox.ButtonRole.ActionRole)
            box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if (folder_btn is not None
                    and box.clickedButton() is folder_btn):
                self._open_path(prog["location"])
            return
        if not confirm_destructive(
                self, "Uninstall %s" % prog["name"],
                "Uninstall %s? Its own uninstaller will run."
                % prog["name"]):
            return

        def do_uninstall():
            try:
                proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL,
                                      timeout=900)
                return proc.returncode
            except subprocess.TimeoutExpired:
                return None

        def done(code):
            self._refresh_with_message("Uninstaller finished")

        self._run_worker(do_uninstall, done, "Uninstalling…")

    def _open_folder_selected(self):
        prog = self._selected_row_data(self.programs_table)
        if prog is None:
            self.statusBar().showMessage("Select a program first")
            return
        location = prog.get("location")
        if not location:
            self.statusBar().showMessage("No install folder recorded")
            return
        self._open_path(location)

    def _kill_selected(self):
        prog = self._selected_row_data(self.programs_table)
        if prog is None:
            self.statusBar().showMessage("Select a program first")
            return
        if not prog.get("exe"):
            self.statusBar().showMessage(
                "No launcher (.exe) was found for %s" % prog["name"])
            return
        if not confirm_destructive(
                self, "Kill %s" % prog["name"],
                "Stop %s right now?\n\nUnsaved work in it will be "
                "lost." % prog["name"]):
            return
        ok, message = common.kill_program(prog["exe"])
        if ok:
            self._refresh_with_message("Stopped %s" % prog["name"])
        elif "is not running" in message:
            self._refresh_with_message(message)
        else:
            QMessageBox.warning(self, "Could not stop it", message)
            self.refresh()

    def _toggle_own_prefix(self, prog, enable):
        exe = prog.get("exe")
        if not exe:
            return
        if not enable:
            common.set_program_prefix(exe, None)
            self._refresh_with_message(
                "%s runs in the shared prefix now" % prog["name"])
            return

        def do_create():
            return common.ensure_program_prefix(exe)

        def done(result):
            ok, message = result
            if ok:
                self._refresh_with_message(
                    "%s has its own prefix now" % prog["name"])
            else:
                QMessageBox.warning(self, "Could not create prefix",
                                    message)
                self.refresh()

        self._run_worker(
            do_create, done,
            "Creating a prefix for %s (first Wine start takes a "
            "minute)…" % prog["name"])

    def _set_windows_version(self, prog, version):
        exe = prog.get("exe")
        if not exe:
            return
        if not prog.get("prefix"):
            if not confirm_destructive(
                    self, "Windows version",
                    "%s runs in the shared prefix, so this changes "
                    "the Windows version for every program in it.\n\n"
                    "Set the Windows version to %s?"
                    % (prog["name"],
                       common.WINDOWS_VERSION_LABELS[version])):
                return

        def do_set():
            return common.set_program_windows_version(exe, version)

        def done(result):
            ok, message = result
            if ok:
                self._refresh_with_message(
                    "%s is now set to %s"
                    % (prog["name"],
                       common.WINDOWS_VERSION_LABELS[version]))
            else:
                QMessageBox.warning(self, "Could not set Windows "
                                    "version", message)
                self.refresh()

        self._run_worker(do_set, done, "Setting Windows version…")

    # -- install buttons ----------------------------------------------

    def _install_program(self):
        target, _ = QFileDialog.getOpenFileName(
            self, "Choose a program to install", "",
            "Windows programs (*.exe *.msi)")
        if target:
            self._drop_into_folder(target, common.programs_folder(),
                                   "Programs")

    def _install_plugin(self):
        target, _ = QFileDialog.getOpenFileName(
            self, "Choose a plug-in to install", "",
            "Plug-ins (*.exe *.msi *.vst3 *.dll *.clap)")
        if target:
            self._drop_into_folder(target, common.plugins_folder(),
                                   "Plugins")

    def _drop_into_folder(self, target, folder, label):
        def do_copy():
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, str(folder / Path(target).name))
            return Path(target).name

        def done(name):
            self.statusBar().showMessage(
                "Dropped %s into %s - the watcher will scan it and "
                "take it from here" % (name, label))

        self._run_worker(do_copy, done,
                         "Copying %s…" % Path(target).name)

    # -- plugin actions -----------------------------------------------

    def _resync(self):
        def do_sync():
            return common.run_yabridgectl_sync()

        def done(result):
            ok, message = result
            if ok:
                self._refresh_with_message("Bridges re-synced")
            else:
                if not common.find_yabridgectl():
                    message = ("yabridge is not installed, so "
                               "plug-ins can't be bridged. Re-run the "
                               "installer to set it up.")
                QMessageBox.warning(self, "Sync failed", message)
                self.refresh()

        self._run_worker(do_sync, done, "Re-syncing bridges…")

    def _remove_plugin(self):
        plug = self._selected_row_data(self.plugins_table)
        if plug is None:
            self.statusBar().showMessage("Select a plug-in first")
            return
        if not confirm_destructive(
                self, "Remove %s" % plug["name"],
                "Remove %s?\n\nThis deletes the bridged copy and the "
                "Windows plug-in file itself." % plug["name"]):
            return

        def do_remove():
            for key in ("wrapper", "source"):
                target = plug.get(key)
                if not target:
                    continue
                path = Path(target)
                try:
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    elif path.exists() or path.is_symlink():
                        path.unlink()
                except OSError:
                    pass
            return common.run_yabridgectl_sync()

        def done(result):
            self._refresh_with_message("Removed %s" % plug["name"])

        self._run_worker(do_remove, done, "Removing…")

    # -- quarantine actions -------------------------------------------

    def _allow_selected(self):
        entry = self._selected_row_data(self.quarantine_table)
        if entry is None:
            self.statusBar().showMessage("Select a quarantined item first")
            return
        if not confirm_destructive(
                self, "Allow %s" % entry["name"],
                "Allow %s?\n\nIt will be put back where it came from and "
                "then install or run as normal. Only do this if you trust "
                "the file." % entry["name"]):
            return

        def do_allow():
            restored = scanner.allow_item(entry)
            if restored is not None:
                watcher.clear_handled(str(restored))
            return restored

        def done(restored):
            if restored is None:
                QMessageBox.warning(
                    self, "Allow failed",
                    "The quarantined copy is gone - nothing to restore.")
                self.refresh()
            else:
                self._refresh_with_message(
                    "Restored %s - the watcher will pick it up"
                    % entry["name"])

        self._run_worker(do_allow, done, "Restoring…")

    def _delete_quarantined(self):
        entry = self._selected_row_data(self.quarantine_table)
        if entry is None:
            self.statusBar().showMessage("Select a quarantined item first")
            return
        if not confirm_destructive(
                self, "Delete %s" % entry["name"],
                "Permanently delete %s from quarantine?" % entry["name"]):
            return
        scanner.delete_quarantined(entry["id"])
        self._refresh_with_message("Deleted %s" % entry["name"])

    # -- failed drops ---------------------------------------------------

    def _retry_failed(self):
        item = self._selected_row_data(self.failed_table)
        if item is None:
            self.statusBar().showMessage("Select a failed drop first")
            return
        watcher.clear_handled(item["path"])
        self._refresh_with_message(
            "Retrying %s - the watcher will pick it up" % item["name"])

    def _forget_failed(self):
        item = self._selected_row_data(self.failed_table)
        if item is None:
            self.statusBar().showMessage("Select a failed drop first")
            return
        if not confirm_destructive(
                self, "Forget %s" % item["name"],
                "Forget %s? WinLinx will leave it alone unless it "
                "changes." % item["name"]):
            return
        watcher.ignore_path(item["path"])
        self._refresh_with_message("Forgot %s" % item["name"])

    # -- system actions ------------------------------------------------

    def _open_path(self, path):
        path = Path(path)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _scan_picker(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Scan a File or Folder")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("What do you want to scan?"))
        file_btn = QPushButton("Scan a file…")
        folder_btn = QPushButton("Scan a folder…")
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        layout.addWidget(file_btn)
        layout.addWidget(folder_btn)
        layout.addWidget(cancel_btn)

        def pick_file():
            target, _ = QFileDialog.getOpenFileName(
                self, "Choose a file to scan")
            dialog.accept()
            if target:
                self._scan_target(target)

        def pick_folder():
            target = QFileDialog.getExistingDirectory(
                self, "Choose a folder to scan")
            dialog.accept()
            if target:
                self._scan_target(target)

        file_btn.clicked.connect(pick_file)
        folder_btn.clicked.connect(pick_folder)
        dialog.exec()

    def _scan_target(self, target):
        def do_scan():
            return scanner.scan_path(target)

        def done(result):
            if result.get("missing") or result.get("error"):
                QMessageBox.warning(
                    self, "Scan failed",
                    result.get("error") or "ClamAV is not installed.")
                return
            if result["clean"]:
                QMessageBox.information(
                    self, "Scan clean",
                    "%s\n\nNo threats found." % target)
                return
            box = QMessageBox(self)
            box.setWindowTitle("Threats found")
            box.setText(format_threats_text(target, result["threats"]))
            box.setInformativeText(
                "Quarantine it so it can't install or run?")
            quarantine_btn = box.addButton(
                "Quarantine", QMessageBox.ButtonRole.DestructiveRole)
            leave_btn = box.addButton(
                "Leave it", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(leave_btn)
            box.exec()
            if box.clickedButton() is quarantine_btn:
                scanner.quarantine_item(target, result["threats"])
                self._refresh_with_message("Quarantined %s" % target)

        self._run_worker(do_scan, done, "Scanning %s…" % target)

    def _update_signatures(self):
        if not scanner.find_clamscan():
            box = QMessageBox(self)
            box.setWindowTitle("ClamAV is not installed")
            box.setText(
                "ClamAV isn't installed yet, so there are no virus "
                "signatures to update.")
            install_btn = box.addButton(
                "Install ClamAV", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is install_btn:
                self._install_clamav()
            return
        if not shutil.which("pkexec"):
            QMessageBox.information(
                self, "Update signatures",
                "The password prompt is not available on this system, "
                "so WinLinx cannot update the signatures itself. Run "
                "this in a terminal instead:\n\n"
                "sudo systemctl stop clamav-freshclam\n"
                "sudo freshclam\n"
                "sudo systemctl start clamav-freshclam")
            return

        def do_update():
            cmd = ["pkexec", "bash", "-c",
                   "systemctl stop clamav-freshclam; "
                   "freshclam; "
                   "systemctl start clamav-freshclam"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=600)
                return proc.returncode, (proc.stdout or "")[-2000:]
            except subprocess.TimeoutExpired:
                return -1, "Timed out."

        def done(result):
            code, output = result
            if code == 0:
                QMessageBox.information(
                    self, "Signatures updated",
                    "Virus signatures are up to date.")
            else:
                common.append_log(
                    "Signature update failed (code %s): %s"
                    % (code, output))
                QMessageBox.warning(
                    self, "Update failed",
                    "The virus signatures could not be updated. Check "
                    "your internet connection and try again.")
            self.refresh()

        self._run_worker(do_update, done,
                         "Updating signatures (a password prompt may appear)…")

    def _install_clamav(self):
        if not shutil.which("pkexec"):
            QMessageBox.information(
                self, "Install ClamAV",
                "The password prompt is not available on this system, "
                "so WinLinx cannot install ClamAV itself. Run this in "
                "a terminal instead:\n\n"
                "sudo apt install clamav clamav-freshclam")
            return

        def do_install():
            cmd = ["pkexec", "bash", "-c",
                   "export DEBIAN_FRONTEND=noninteractive; "
                   "apt-get install -y clamav clamav-freshclam; "
                   "systemctl stop clamav-freshclam 2>/dev/null || true; "
                   "freshclam; "
                   "systemctl enable --now clamav-freshclam "
                   "2>/dev/null || true"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=1800)
                return proc.returncode
            except subprocess.TimeoutExpired:
                return -1

        def done(code):
            if code == 0:
                self._refresh_with_message("ClamAV installed")
            else:
                common.append_log(
                    "ClamAV install failed (exit code %s)" % code)
                QMessageBox.warning(
                    self, "Install failed",
                    "ClamAV could not be installed. Try again, or "
                    "install it from your software manager.")
                self.refresh()

        self._run_worker(do_install, done,
                         "Installing ClamAV (a password prompt may appear)…")

    def _repair_install(self):
        """Re-run the installer GUI, which sets up Wine and yabridge
        again (the installer is safe to re-run over an existing
        install)."""
        installer = Path(__file__).resolve().parent / "installer.py"
        try:
            subprocess.Popen([sys.executable, str(installer)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             start_new_session=True)
            self.statusBar().showMessage("Opening the installer…")
        except OSError as exc:
            common.append_log("Could not open the installer: %s" % exc)
            QMessageBox.warning(
                self, "Could not open the installer",
                "The installer could not be opened. Please try again.")

    # -- backup / restore ----------------------------------------------

    def _backup_prefix(self):
        default = common.backups_dir() / (
            "winlinx-prefix-%s.tar.gz"
            % datetime.now().strftime("%Y%m%d-%H%M%S"))
        target, _ = QFileDialog.getSaveFileName(
            self, "Back up prefix", str(default),
            "Backups (*.tar.gz)")
        if not target:
            return
        progress = QProgressDialog(
            "Backing up the Wine prefix…", "Cancel", 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        worker = ProgressWorker(
            lambda report: common.backup_prefixes(target,
                                                  progress=report),
            self)

        def on_progress(done, total):
            progress.setLabelText(
                "Backing up: %d files (%s)…"
                % (done, common.format_size(total)))

        def on_done(result):
            progress.close()
            ok, message = result
            if ok:
                QMessageBox.information(
                    self, "Backed up",
                    "Saved to %s.\n\n%s" % (target, message))
            else:
                QMessageBox.warning(self, "Backup failed", message)

        def on_failed(message):
            progress.close()
            QMessageBox.warning(self, "Backup failed", message)

        worker.progress.connect(on_progress)
        worker.finished.connect(on_done)
        worker.failed.connect(on_failed)
        progress.canceled.connect(
            lambda: setattr(worker, "cancelled", True))
        worker.start()

    def _restore_prefix(self):
        target, _ = QFileDialog.getOpenFileName(
            self, "Restore prefix backup",
            str(common.backups_dir()), "Backups (*.tar.gz)")
        if not target:
            return
        if not confirm_destructive(
                self, "Restore prefix",
                "Restore the prefix from this backup?\n\nThe current "
                "prefix will be moved aside (not deleted), and the "
                "backup will take its place."):
            return
        progress = QProgressDialog(
            "Restoring the Wine prefix…", "Cancel", 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        worker = ProgressWorker(
            lambda report: common.restore_prefixes(target,
                                                   progress=report),
            self)

        def on_progress(done, _total):
            progress.setLabelText(
                "Restoring: %d files…" % done)

        def on_done(result):
            progress.close()
            ok, message = result
            if ok:
                QMessageBox.information(
                    self, "Restored",
                    "The prefix was restored.\n\n%s" % message)
            else:
                QMessageBox.warning(self, "Restore failed", message)
            self.refresh()

        def on_failed(message):
            progress.close()
            QMessageBox.warning(self, "Restore failed", message)

        worker.progress.connect(on_progress)
        worker.finished.connect(on_done)
        worker.failed.connect(on_failed)
        progress.canceled.connect(
            lambda: setattr(worker, "cancelled", True))
        worker.start()

    # -- debug report / updates -----------------------------------------

    def _debug_report(self):
        def done(path):
            if path is None:
                QMessageBox.warning(
                    self, "Debug report",
                    "The report could not be written.")
                return
            box = QMessageBox(self)
            box.setWindowTitle("Debug report")
            box.setText("Saved to %s" % path)
            open_btn = box.addButton(
                "Open Folder", QMessageBox.ButtonRole.ActionRole)
            box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is open_btn:
                QDesktopServices.openUrl(QUrl.fromLocalFile(
                    str(Path(path).parent)))

        self._run_worker(common.write_debug_report, done,
                         "Writing debug report…")

    def _check_updates(self):
        def done(result):
            if result.get("error"):
                common.append_log("Update check failed: %s"
                                  % result["error"])
                QMessageBox.warning(
                    self, "Update check failed",
                    "WinLinx could not check for updates. Check your "
                    "internet connection and try again.")
                return
            if not result.get("available"):
                QMessageBox.information(
                    self, "Up to date",
                    "WinLinx %s is the latest version."
                    % common.__version__)
                return
            box = QMessageBox(self)
            box.setWindowTitle("Update available")
            box.setText("WinLinx %s is available (you have %s)."
                        % (result["latest"], common.__version__))
            box.setInformativeText("Download it now?")
            download_btn = box.addButton(
                "Download", QMessageBox.ButtonRole.AcceptRole)
            page_btn = box.addButton(
                "Open Release Page", QMessageBox.ButtonRole.ActionRole)
            box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(download_btn)
            box.exec()
            if box.clickedButton() is download_btn:
                self._download_update(result)
            elif box.clickedButton() is page_btn:
                QDesktopServices.openUrl(QUrl(result["url"]))

        self._run_worker(common.check_for_update, done,
                         "Checking for updates…")

    def _download_update(self, result):
        zip_url = result.get("zip_url")
        if not zip_url:
            QDesktopServices.openUrl(QUrl(result["url"]))
            return
        dest = common.home() / "Downloads" / (
            "winlinx-%s.zip" % result["latest"])
        progress = QProgressDialog(
            "Downloading WinLinx %s…" % result["latest"],
            "Cancel", 0, 100, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        worker = ProgressWorker(
            lambda report: common.download_file(zip_url, dest,
                                                progress=report),
            self)

        def on_progress(done, total):
            if total:
                progress.setValue(int(done * 100 / total))
                progress.setLabelText(
                    "Downloading WinLinx %s: %s of %s"
                    % (result["latest"], common.format_size(done),
                       common.format_size(total)))

        def on_done(res):
            progress.close()
            ok, message = res
            if ok:
                QMessageBox.information(
                    self, "Downloaded",
                    "Saved to %s.\n\nUnzip it and double-click "
                    "Install WinLinx to update." % dest)
            else:
                QMessageBox.warning(self, "Download failed",
                                    message or "The download failed.")

        def on_failed(message):
            progress.close()
            QMessageBox.warning(self, "Download failed", message)

        worker.progress.connect(on_progress)
        worker.finished.connect(on_done)
        worker.failed.connect(on_failed)
        progress.canceled.connect(
            lambda: setattr(worker, "cancelled", True))
        worker.start()

    # -- menu windows ----------------------------------------------------

    def _toggle_auto_refresh(self, checked):
        self._settings.setValue("auto_refresh", checked)
        if checked:
            self._auto_timer.start()
        else:
            self._auto_timer.stop()

    def _open_howto(self):
        if self._howto_dialog is None:
            self._howto_dialog = HowToUseDialog(self)
            self._howto_dialog.setAttribute(
                Qt.WidgetAttribute.WA_DeleteOnClose)
            self._howto_dialog.destroyed.connect(self._howto_closed)
        self._howto_dialog.show()
        self._howto_dialog.raise_()
        self._howto_dialog.activateWindow()

    def _howto_closed(self, _obj=None):
        self._howto_dialog = None

    def _open_about(self):
        AboutDialog(self).exec()

    def _open_uninstaller(self):
        UninstallDialog(self).exec()

    # -- close guard ----------------------------------------------------

    def closeEvent(self, event):
        self._settings.setValue("geometry", self.saveGeometry())
        event.accept()


def main(argv=None):
    args = sys.argv if argv is None else argv
    if "--version" in args or "-V" in args:
        print("WinLinx Program Manager %s" % common.__version__)
        return 0
    app = QApplication(args)
    app.setApplicationName("WinLinx")
    app.setOrganizationName("BrokeNBored")

    # Single instance (U27): a second launch pokes the running window
    # and exits instead of starting a second copy.
    lock_path = common.layer_dir() / "manager.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    lock = QLockFile(str(lock_path))
    if not lock.tryLock(0):
        if not lock_path.exists():
            # The lock file could not even be created (the layer
            # folder is not writable): nothing is running, so do
            # not claim WinLinx is already running - say so
            # plainly and exit instead of hanging on a dialog.
            print("WinLinx cannot start: cannot create %s"
                  % lock_path, file=sys.stderr)
            return 1
        sock = QLocalSocket()
        sock.connectToServer(SERVER_NAME)
        if sock.waitForConnected(1000):
            sock.write(b"raise")
            sock.waitForBytesWritten(1000)
        else:
            QMessageBox.information(
                None, "WinLinx", "WinLinx is already running.")
        return 0
    server = QLocalServer()
    QLocalServer.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)
    window = ManagerWindow()

    def on_connection():
        peer = server.nextPendingConnection()
        if peer is not None:
            peer.waitForReadyRead(1000)
            window.show()
            window.raise_()
            window.activateWindow()
            peer.disconnectFromServer()

    server.newConnection.connect(on_connection)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
