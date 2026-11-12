#!/usr/bin/env python3
"""WinLinx Program Manager for Linux — native Qt desktop window.

One place to organize and manage all the Windows stuff on this PC:
  Programs   — every Windows program in the Wine prefix: launch,
               uninstall, open install folder; search, install date,
               size columns.
  Plug-ins   — every bridged VST with bridge status, re-sync, remove.
  Quarantine — files the scanner flagged before they could install
               or run: threat name, Allow (install anyway — the user
               is final authority) or Delete.
  System     — Wine version, prefix location, disk used, yabridge and
               scanner status, watcher status, recent log lines,
               manual scan, signature updates.

Plain Qt widgets, no web anything. Slow work (scans, sizes, syncs,
uninstalls) runs in worker threads so the window stays fast.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
import scanner
import watcher

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

SERVICE_NAME = "winlinx-watcher.service"
ICON_PATH = Path(__file__).resolve().parent / "icons" / "winlinx-256.png"


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


# ---------------------------------------------------------------- data gathering

def watcher_service_status():
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME],
            capture_output=True, text=True, timeout=10)
        state = (proc.stdout or "").strip()
        return state if state else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def read_log_tail(max_lines=200):
    try:
        with open(common.log_path(), "r", encoding="utf-8",
                  errors="replace") as fh:
            lines = fh.readlines()
        return "".join(lines[-max_lines:])
    except OSError:
        return ""


def gather_all():
    """Everything the window shows, gathered in one worker pass."""
    programs = common.scan_programs()
    for prog in programs:
        if prog["size_bytes"] is None and prog["location"]:
            prog["size_bytes"] = common.dir_size(prog["location"])
    prefix = common.wine_prefix()
    return {
        "programs": programs,
        "plugins": common.scan_plugins(),
        "quarantine": scanner.load_quarantine(),
        "wine_version": common.get_wine_version(),
        "yabridge": common.get_yabridge_status(),
        "scanner": scanner.scanner_status(),
        "prefix": str(prefix),
        "prefix_size": common.dir_size(prefix) if prefix.is_dir() else 0,
        "service": watcher_service_status(),
        "log": read_log_tail(),
    }


# ---------------------------------------------------------------- window

class ManagerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WinLinx Program Manager")
        if ICON_PATH.is_file():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(960, 600)
        self._worker = None
        self._busy_buttons = []
        self._programs = []
        self._plugins = []
        self._quarantine = []

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._build_programs_tab()
        self._build_plugins_tab()
        self._build_quarantine_tab()
        self._build_system_tab()
        self.statusBar().showMessage("Ready")
        self.refresh()

    # -- construction ----------------------------------------------

    def _register_busy(self, button):
        self._busy_buttons.append(button)
        return button

    def _set_busy(self, busy):
        for button in self._busy_buttons:
            button.setEnabled(not busy)

    def _build_programs_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search programs…")
        self.search_box.textChanged.connect(self._filter_programs)
        layout.addWidget(self.search_box)
        self.programs_table = QTableWidget(0, 4)
        self.programs_table.setHorizontalHeaderLabels(
            ["Name", "Installed", "Size", "Location"])
        self.programs_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.programs_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.programs_table.setSortingEnabled(True)
        self.programs_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.programs_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        self.programs_table.doubleClicked.connect(self._launch_selected)
        layout.addWidget(self.programs_table)
        row = QHBoxLayout()
        self.launch_btn = self._register_busy(QPushButton("Launch"))
        self.launch_btn.clicked.connect(self._launch_selected)
        self.uninstall_btn = self._register_busy(QPushButton("Uninstall"))
        self.uninstall_btn.clicked.connect(self._uninstall_selected)
        self.open_folder_btn = self._register_busy(
            QPushButton("Open Install Folder"))
        self.open_folder_btn.clicked.connect(self._open_folder_selected)
        self.programs_refresh_btn = self._register_busy(
            QPushButton("Refresh"))
        self.programs_refresh_btn.clicked.connect(self.refresh)
        for btn in (self.launch_btn, self.uninstall_btn,
                    self.open_folder_btn, self.programs_refresh_btn):
            row.addWidget(btn)
        row.addStretch()
        layout.addLayout(row)
        self.tabs.addTab(page, "Programs")

    def _build_plugins_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.plugins_table = QTableWidget(0, 4)
        self.plugins_table.setHorizontalHeaderLabels(
            ["Name", "Type", "Status", "Source File"])
        self.plugins_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.plugins_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.plugins_table.setSortingEnabled(True)
        self.plugins_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.plugins_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.plugins_table)
        row = QHBoxLayout()
        self.resync_btn = self._register_busy(
            QPushButton("Re-sync Bridges"))
        self.resync_btn.clicked.connect(self._resync)
        self.remove_plugin_btn = self._register_busy(QPushButton("Remove"))
        self.remove_plugin_btn.clicked.connect(self._remove_plugin)
        self.plugins_refresh_btn = self._register_busy(
            QPushButton("Refresh"))
        self.plugins_refresh_btn.clicked.connect(self.refresh)
        for btn in (self.resync_btn, self.remove_plugin_btn,
                    self.plugins_refresh_btn):
            row.addWidget(btn)
        row.addStretch()
        layout.addLayout(row)
        self.tabs.addTab(page, "Plug-ins")

    def _build_quarantine_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Files the scanner flagged before they could install or run. "
            "Allow restores a file you trust — it will then install or run "
            "as normal. Delete removes it for good.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.quarantine_table = QTableWidget(0, 4)
        self.quarantine_table.setHorizontalHeaderLabels(
            ["Name", "Threat", "Original Location", "Date"])
        self.quarantine_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.quarantine_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.quarantine_table.setSortingEnabled(True)
        self.quarantine_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.quarantine_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
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

    def _build_system_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)
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
        self.resync2_btn = self._register_busy(
            QPushButton("Re-sync Bridges"))
        self.resync2_btn.clicked.connect(self._resync)
        self.scan_btn = self._register_busy(
            QPushButton("Scan a File or Folder…"))
        self.scan_btn.clicked.connect(self._scan_picker)
        self.update_sigs_btn = self._register_busy(
            QPushButton("Update Signatures"))
        self.update_sigs_btn.clicked.connect(self._update_signatures)
        self.system_refresh_btn = self._register_busy(
            QPushButton("Refresh"))
        self.system_refresh_btn.clicked.connect(self.refresh)
        for btn in (self.open_programs_btn, self.open_plugins_btn,
                    self.resync2_btn, self.scan_btn,
                    self.update_sigs_btn, self.system_refresh_btn):
            row.addWidget(btn)
        row.addStretch()
        layout.addLayout(row)
        self.tabs.addTab(page, "System")

    # -- worker plumbing --------------------------------------------

    def _run_worker(self, func, on_done, busy_message):
        if self._worker is not None and self._worker.isRunning():
            self.statusBar().showMessage("Already working, hold on…")
            return
        self._set_busy(True)
        self.statusBar().showMessage(busy_message)
        self._worker = Worker(func, self)

        def done(result):
            self._set_busy(False)
            self._worker = None
            on_done(result)

        def failed(message):
            self._set_busy(False)
            self._worker = None
            self.statusBar().showMessage("Error: %s" % message)
            QMessageBox.warning(self, "Something went wrong", message)

        self._worker.finished.connect(done)
        self._worker.failed.connect(failed)
        self._worker.start()

    # -- refresh -----------------------------------------------------

    def refresh(self):
        self._run_worker(gather_all, self._populate, "Scanning…")

    def _populate(self, data):
        self._programs = data["programs"]
        self._plugins = data["plugins"]
        self._quarantine = data["quarantine"]
        self._fill_programs()
        self._fill_plugins()
        self._fill_quarantine()
        scanstat = data["scanner"]
        if scanstat["installed"]:
            sig = scanstat["sig_date"] or "unknown date"
            scanner_line = "ClamAV %s (signatures: %s)" % (
                scanstat["version"].split("/")[0].replace("ClamAV ", ""),
                sig)
        else:
            scanner_line = "ClamAV not installed"
        self.info_label.setText(
            "Wine: %s\n"
            "yabridge: %s\n"
            "Scanner: %s\n"
            "Prefix: %s (%s used)\n"
            "Watcher service: %s\n"
            "Quarantined items: %d" % (
                data["wine_version"] or "Not installed",
                data["yabridge"],
                scanner_line,
                data["prefix"],
                common.format_size(data["prefix_size"]),
                data["service"],
                len(data["quarantine"])))
        self.log_view.setPlainText(data["log"])
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum())
        self.statusBar().showMessage(
            "%d programs, %d plug-ins, %d quarantined"
            % (len(data["programs"]), len(data["plugins"]),
               len(data["quarantine"])))
        self.tabs.setTabText(2, "Quarantine (%d)" % len(data["quarantine"]))
        self._filter_programs()

    def _fill_programs(self):
        table = self.programs_table
        table.setSortingEnabled(False)
        table.setRowCount(len(self._programs))
        for row, prog in enumerate(self._programs):
            name_item = QTableWidgetItem(prog["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, prog)
            table.setItem(row, 0, name_item)
            table.setItem(row, 1, QTableWidgetItem(prog["install_date"]))
            table.setItem(row, 2, SizeItem(prog["size_bytes"]))
            table.setItem(row, 3, QTableWidgetItem(prog["location"] or ""))
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
            table.setItem(row, 2, QTableWidgetItem(plug["status"]))
            table.setItem(row, 3, QTableWidgetItem(plug["source"]))
        table.setSortingEnabled(True)

    def _fill_quarantine(self):
        table = self.quarantine_table
        table.setSortingEnabled(False)
        table.setRowCount(len(self._quarantine))
        for row, entry in enumerate(self._quarantine):
            name_item = QTableWidgetItem(entry["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, entry)
            table.setItem(row, 0, name_item)
            table.setItem(row, 1,
                          QTableWidgetItem(", ".join(entry["threats"])))
            table.setItem(row, 2, QTableWidgetItem(entry["original"]))
            table.setItem(row, 3, QTableWidgetItem(entry["date"][:10]))
        table.setSortingEnabled(True)

    def _filter_programs(self):
        query = self.search_box.text().strip().lower()
        table = self.programs_table
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is None:
                continue
            prog = item.data(Qt.ItemDataRole.UserRole) or {}
            hay = "%s %s" % (prog.get("name", ""),
                             prog.get("location", ""))
            table.setRowHidden(row, query not in hay.lower())

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
        try:
            subprocess.Popen(["wine", exe], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             start_new_session=True)
            self.statusBar().showMessage("Launched %s" % prog["name"])
        except OSError as exc:
            QMessageBox.warning(self, "Launch failed", str(exc))

    def _uninstall_selected(self):
        prog = self._selected_row_data(self.programs_table)
        if prog is None:
            self.statusBar().showMessage("Select a program first")
            return
        cmd = common.uninstall_command(prog)
        if not cmd:
            QMessageBox.information(
                self, "Can't uninstall",
                "No uninstaller was recorded for %s.\n\n"
                "You can still delete its folder from the install "
                "location." % prog["name"])
            return
        answer = QMessageBox.question(
            self, "Uninstall %s" % prog["name"],
            "Uninstall %s? Its own uninstaller will run."
            % prog["name"])
        if answer != QMessageBox.StandardButton.Yes:
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
            self.statusBar().showMessage("Uninstaller finished")
            self.refresh()

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

    # -- plugin actions -----------------------------------------------

    def _resync(self):
        def do_sync():
            return common.run_yabridgectl_sync()

        def done(result):
            ok, message = result
            if ok:
                self.statusBar().showMessage("Bridges re-synced")
            else:
                QMessageBox.warning(self, "Sync failed", message)
            self.refresh()

        self._run_worker(do_sync, done, "Re-syncing bridges…")

    def _remove_plugin(self):
        plug = self._selected_row_data(self.plugins_table)
        if plug is None:
            self.statusBar().showMessage("Select a plug-in first")
            return
        answer = QMessageBox.question(
            self, "Remove %s" % plug["name"],
            "Remove %s?\n\nThis deletes the bridged copy and the "
            "Windows plug-in file itself." % plug["name"])
        if answer != QMessageBox.StandardButton.Yes:
            return

        def do_remove():
            for key in ("wrapper", "source"):
                target = plug.get(key)
                if not target:
                    continue
                path = Path(target)
                try:
                    if path.is_dir() and not path.is_symlink():
                        import shutil as _shutil
                        _shutil.rmtree(path)
                    elif path.exists() or path.is_symlink():
                        path.unlink()
                except OSError:
                    pass
            return common.run_yabridgectl_sync()

        def done(result):
            self.statusBar().showMessage("Removed %s" % plug["name"])
            self.refresh()

        self._run_worker(do_remove, done, "Removing…")

    # -- quarantine actions -------------------------------------------

    def _allow_selected(self):
        entry = self._selected_row_data(self.quarantine_table)
        if entry is None:
            self.statusBar().showMessage("Select a quarantined item first")
            return
        answer = QMessageBox.question(
            self, "Allow %s" % entry["name"],
            "Allow %s?\n\nIt will be put back where it came from and "
            "then install or run as normal. Only do this if you trust "
            "the file." % entry["name"])
        if answer != QMessageBox.StandardButton.Yes:
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
                    "The quarantined copy is gone — nothing to restore.")
            else:
                self.statusBar().showMessage(
                    "Restored %s — the watcher will pick it up"
                    % entry["name"])
            self.refresh()

        self._run_worker(do_allow, done, "Restoring…")

    def _delete_quarantined(self):
        entry = self._selected_row_data(self.quarantine_table)
        if entry is None:
            self.statusBar().showMessage("Select a quarantined item first")
            return
        answer = QMessageBox.question(
            self, "Delete %s" % entry["name"],
            "Permanently delete %s from quarantine?" % entry["name"])
        if answer != QMessageBox.StandardButton.Yes:
            return
        scanner.delete_quarantined(entry["id"])
        self.statusBar().showMessage("Deleted %s" % entry["name"])
        self.refresh()

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
        layout.addWidget(file_btn)
        layout.addWidget(folder_btn)

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
            names = sorted({name for _, name in result["threats"]})
            box = QMessageBox(self)
            box.setWindowTitle("Threats found")
            box.setText("%s\n\n%s" % (target, "\n".join(
                "%s — %s" % (f, t) for f, t in result["threats"])))
            box.setInformativeText(
                "Quarantine it so it can't install or run?")
            quarantine_btn = box.addButton(
                "Quarantine", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("Leave it", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is quarantine_btn:
                scanner.quarantine_item(target, result["threats"])
                self.statusBar().showMessage("Quarantined %s" % target)
                self.refresh()

        self._run_worker(do_scan, done, "Scanning %s…" % target)

    def _update_signatures(self):
        import shutil as _shutil
        if not _shutil.which("pkexec"):
            QMessageBox.information(
                self, "Update signatures",
                "Run this in a terminal:\n\n"
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
                QMessageBox.warning(
                    self, "Update failed",
                    output or ("freshclam exited with code %s" % code))
            self.refresh()

        self._run_worker(do_update, done,
                         "Updating signatures (a password prompt may appear)…")


def main(argv=None):
    app = QApplication(sys.argv if argv is None else argv)
    window = ManagerWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
