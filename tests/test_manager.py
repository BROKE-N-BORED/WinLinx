"""Tests for winlinx/manager.py — the Qt Program Manager window.
Runs offscreen (QT_QPA_PLATFORM=offscreen); fake home via WINLINX_HOME."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "winlinx")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakehome  # noqa: E402
import scanner  # noqa: E402

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtNetwork import QLocalSocket  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QFileDialog, QMessageBox, QPushButton,
    QTableWidget, QWidget,
)
import manager  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WINLINX_HOME")
        os.environ["WINLINX_HOME"] = self._tmp.name
        self.home = Path(self._tmp.name)
        fakehome.build_fake_home(self.home)
        bad = self.home / "nasty.exe"
        bad.write_bytes(b"x")
        scanner.quarantine_item(bad, [(str(bad), "Evil.Thing")])
        # One failed drop for the Failed Drops tab.
        failed_path = (self.home / "WinLinx" / "Programs"
                       / "broken.exe")
        state = {str(failed_path): {
            "size": 1, "mtime": 1, "action": "failed",
            "reason": "Wine is not installed", "attempts": 2,
            "next_retry": 0, "time": "2026-11-12T09:04:00"}}
        (self.home / ".local" / "share" / "winlinx"
         / "handled.json").write_text(json.dumps(state))
        self.window = manager.ManagerWindow(startup_update_check=False)
        self.window.show()
        self._wait_for_refresh()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        _APP.processEvents()
        if self._old_home is None:
            os.environ.pop("WINLINX_HOME", None)
        else:
            os.environ["WINLINX_HOME"] = self._old_home
        self._tmp.cleanup()

    def _wait_for_refresh(self, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _APP.processEvents()
            if self.window._worker is None:
                return True
            time.sleep(0.05)
        return self.window._worker is None

    def test_tabs_exist(self):
        labels = [self.window.tabs.tabText(i)
                  for i in range(self.window.tabs.count())]
        self.assertEqual(len(labels), 6)
        self.assertEqual(labels[0], "Programs")
        self.assertEqual(labels[1], "Plug-ins")
        self.assertTrue(labels[2].startswith("Quarantine"))
        self.assertTrue(labels[3].startswith("Failed Drops"))
        self.assertEqual(labels[4], "Activity")
        self.assertEqual(labels[5], "System")

    def test_window_title_has_version(self):
        self.assertIn(manager.common.__version__,
                      self.window.windowTitle())
        self.assertIn("WinLinx Program Manager",
                      self.window.windowTitle())

    def test_menu_bar_exists(self):
        titles = [action.text()
                  for action in self.window.menuBar().actions()]
        self.assertEqual(titles, ["File", "View", "Help"])

    def test_banner_visible_without_clamav(self):
        self.assertTrue(self.window.banner.isVisible())
        self.assertIn("unscanned", self.window.banner_label.text())

    def test_programs_table_populated(self):
        table = self.window.programs_table
        self.assertEqual(table.rowCount(), 2)
        names = sorted(table.item(r, 0).text()
                       for r in range(table.rowCount()))
        self.assertEqual(names, ["FooApp 2.0", "MSI Thing"])
        # Install date + size columns filled from the registry.
        row = next(r for r in range(2)
                   if table.item(r, 0).text() == "FooApp 2.0")
        self.assertEqual(table.item(row, 1).text(), "2026-01-15")
        self.assertEqual(table.item(row, 2).text(), "1.0 MB")
        # Running + Prefix columns.
        self.assertEqual(table.item(row, 4).text(), "Not running")
        self.assertEqual(table.item(row, 5).text(), "Shared prefix")

    def test_plugins_table_populated(self):
        table = self.window.plugins_table
        self.assertEqual(table.rowCount(), 3)
        by_name = {table.item(r, 0).text(): table.item(r, 2).text()
                   for r in range(table.rowCount())}
        self.assertEqual(by_name["Remix.vst3"], "Bridged")
        self.assertEqual(by_name["Broken.vst3"], "Source missing")
        self.assertEqual(by_name["Lonely.vst3"], "Not bridged")

    def test_quarantine_table_populated(self):
        table = self.window.quarantine_table
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.item(0, 0).text(), "nasty.exe")
        self.assertEqual(table.item(0, 1).text(), "Evil.Thing")
        self.assertIn("Quarantine (1)", self.window.tabs.tabText(2))

    def test_system_tab_shows_status(self):
        text = self.window.info_label.text()
        self.assertIn("Wine: Not installed", text)
        self.assertIn("yabridge: Not installed", text)
        self.assertIn("Scanner: ClamAV not installed", text)
        self.assertIn("Prefix:", text)
        self.assertIn("Quarantined items: 1", text)
        self.assertIn("Watcher started", self.window.log_view.toPlainText())

    def test_search_filters_programs(self):
        table = self.window.programs_table
        self.window.search_box.setText("foo")
        visible = [r for r in range(table.rowCount())
                   if not table.isRowHidden(r)]
        self.assertEqual(len(visible), 1)
        self.assertEqual(table.item(visible[0], 0).text(), "FooApp 2.0")
        self.window.search_box.setText("")
        visible = [r for r in range(table.rowCount())
                   if not table.isRowHidden(r)]
        self.assertEqual(len(visible), 2)

    def test_search_filters_plugins_and_quarantine(self):
        table = self.window.plugins_table
        self.window.plugins_search_box.setText("remix")
        visible = [r for r in range(table.rowCount())
                   if not table.isRowHidden(r)]
        self.assertEqual(len(visible), 1)
        self.window.plugins_search_box.setText("")
        qtable = self.window.quarantine_table
        self.window.quarantine_search_box.setText("nasty")
        visible = [r for r in range(qtable.rowCount())
                   if not qtable.isRowHidden(r)]
        self.assertEqual(len(visible), 1)
        self.window.quarantine_search_box.setText("zzz-no-match")
        visible = [r for r in range(qtable.rowCount())
                   if not qtable.isRowHidden(r)]
        self.assertEqual(len(visible), 0)

    def test_failed_tab_populated(self):
        table = self.window.failed_table
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.item(0, 0).text(), "broken.exe")
        self.assertIn("Wine is not installed",
                      table.item(0, 1).text())
        self.assertEqual(table.item(0, 2).text(), "2")
        self.assertIn("Failed Drops (1)",
                      self.window.tabs.tabText(3))

    def test_activity_tab_shows_log(self):
        self.assertIn("Watcher started",
                      self.window.activity_view.toPlainText())

    def test_confirm_defaults_to_no_on_enter(self):
        # U23 regression: Enter on a destructive confirm used to fire
        # Yes. The timer presses Enter on the modal dialog; the
        # confirm must come back False (No).
        def press_enter():
            dialog = QApplication.activeModalWidget()
            self.assertIsNotNone(dialog)
            QTest.keyClick(dialog, Qt.Key.Key_Enter)

        QTimer.singleShot(300, press_enter)
        result = manager.confirm_destructive(
            self.window, "Delete test", "Delete it?")
        self.assertFalse(result)

    def test_no_em_dash_in_visible_text(self):
        texts = []
        for widget in self.window.findChildren(QWidget):
            text = widget.text() if hasattr(widget, "text") else ""
            if text:
                texts.append(text)
        for i in range(self.window.tabs.count()):
            texts.append(self.window.tabs.tabText(i))
        for action in self.window.menuBar().actions():
            texts.append(action.text())
        joined = "\n".join(texts)
        self.assertNotIn("—", joined)

    def test_howto_dialog_topics(self):
        dialog = manager.HowToUseDialog(self.window)
        try:
            self.assertEqual(dialog.index.count(),
                             len(manager.HOW_TO_USE))
            self.assertGreaterEqual(dialog.index.count(), 5)
            dialog.index.setCurrentRow(1)
            self.assertIn("Installing a program",
                          dialog.text.toPlainText())
        finally:
            dialog.close()

    # -- v0.3 regression tests ------------------------------------

    def _wait_until(self, predicate, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _APP.processEvents()
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def _select_row(self, table, row=0):
        table.selectRow(row)
        _APP.processEvents()

    def test_launch_without_wine_is_plain_words(self):
        # Task 4: no raw "[Errno 2] ... 'wine'" in the dialog, and
        # no stale "Select a program first" left in the status bar.
        self._select_row(self.window.programs_table)
        seen = []
        with mock.patch.object(manager.common, "find_wine",
                               return_value=None):
            with mock.patch.object(
                    QMessageBox, "warning",
                    lambda *args: seen.append(args[2])):
                self.window._launch_selected()
        self.assertEqual(len(seen), 1)
        self.assertIn("Wine is not installed", seen[0])
        self.assertIn("cannot run", seen[0])
        self.assertNotIn("Errno", seen[0])
        self.assertNotIn("'wine'", seen[0])
        status = self.window.statusBar().currentMessage()
        self.assertIn("Could not launch", status)
        self.assertNotIn("Select a program first", status)

    def test_resync_failure_is_plain_words(self):
        # Task 5: no `yabridgectl` in the dialog.
        seen = []
        with mock.patch.object(
                QMessageBox, "warning",
                lambda *args: seen.append(args[2])):
            self.window._resync()
            self.assertTrue(self._wait_until(lambda: seen))
        self.assertIn("yabridge is not installed", seen[0])
        self.assertNotIn("yabridgectl", seen[0])
        self._wait_for_refresh()

    def test_failed_reason_plain_words(self):
        # Task 5: an old-style Failed Drops reason is shown plain.
        failed_path = (self.home / "WinLinx" / "Plugins"
                       / "BigSynth.vst3")
        state = {str(failed_path): {
            "size": 1, "mtime": 1, "action": "failed",
            "reason": "bridge sync failed (yabridgectl not found)",
            "attempts": 1, "next_retry": 0,
            "time": "2026-11-16T11:30:00"}}
        (self.home / ".local" / "share" / "winlinx"
         / "handled.json").write_text(json.dumps(state))
        self.window.refresh()
        self._wait_for_refresh()
        table = self.window.failed_table
        self.assertEqual(table.rowCount(), 1)
        reason = table.item(0, 1).text()
        self.assertNotIn("yabridgectl", reason)
        self.assertIn("yabridge is not installed", reason)

    def test_action_messages_survive_refresh(self):
        # Task 7: Delete, Retry and Forget confirmations are still
        # in the status bar after the refresh finishes.
        with mock.patch.object(manager, "confirm_destructive",
                               return_value=True):
            self._select_row(self.window.quarantine_table)
            self.window._delete_quarantined()
            self._wait_for_refresh()
            self.assertEqual(
                self.window.statusBar().currentMessage(),
                "Deleted nasty.exe")
            self._select_row(self.window.failed_table)
            self.window._retry_failed()
            self._wait_for_refresh()
            self.assertIn(
                "Retrying broken.exe",
                self.window.statusBar().currentMessage())
            # Retry cleared the entry; put it back for Forget.
            failed_path = (self.home / "WinLinx" / "Programs"
                           / "broken.exe")
            state = {str(failed_path): {
                "size": 1, "mtime": 1, "action": "failed",
                "reason": "Wine is not installed", "attempts": 2,
                "next_retry": 0, "time": "2026-11-12T09:04:00"}}
            (self.home / ".local" / "share" / "winlinx"
             / "handled.json").write_text(json.dumps(state))
            self.window.refresh()
            self._wait_for_refresh()
            self._select_row(self.window.failed_table)
            self.window._forget_failed()
            self._wait_for_refresh()
            self.assertEqual(
                self.window.statusBar().currentMessage(),
                "Forgot broken.exe")

    def test_action_buttons_need_selection(self):
        # Task 17: with nothing selected the row actions are
        # disabled; selecting a row enables them.
        window = self.window
        for btn in (window.launch_btn, window.uninstall_btn,
                    window.open_folder_btn, window.remove_plugin_btn,
                    window.allow_btn, window.delete_btn,
                    window.retry_btn, window.forget_btn):
            self.assertFalse(btn.isEnabled(), btn.text())
        self.assertTrue(window.install_program_btn.isEnabled())
        self.assertTrue(window.programs_refresh_btn.isEnabled())
        self._select_row(window.programs_table)
        self.assertTrue(window.launch_btn.isEnabled())
        self.assertTrue(window.uninstall_btn.isEnabled())
        self.assertTrue(window.open_folder_btn.isEnabled())
        self.assertFalse(window.allow_btn.isEnabled())
        self._select_row(window.plugins_table)
        self.assertTrue(window.remove_plugin_btn.isEnabled())
        self._select_row(window.quarantine_table)
        self.assertTrue(window.allow_btn.isEnabled())
        self.assertTrue(window.delete_btn.isEnabled())
        self._select_row(window.failed_table)
        self.assertTrue(window.retry_btn.isEnabled())
        self.assertTrue(window.forget_btn.isEnabled())

    def test_program_icons_are_per_program_glyphs(self):
        # Task 18: no more identical app icon on every row.
        icon_a = manager.make_program_icon("Alpha")
        icon_b = manager.make_program_icon("Beta")
        self.assertFalse(icon_a.isNull())
        self.assertFalse(icon_b.isNull())
        img_a = icon_a.pixmap(48, 48).toImage()
        img_b = icon_b.pixmap(48, 48).toImage()
        self.assertNotEqual(img_a, img_b)
        table = self.window.programs_table
        app_key = QIcon(str(manager.ICON_PATH)).cacheKey()
        for row in range(table.rowCount()):
            item_icon = table.item(row, 0).icon()
            self.assertFalse(item_icon.isNull())
            self.assertNotEqual(item_icon.cacheKey(), app_key)

    def test_prefix_column_explains_itself(self):
        # Task 19: "Own prefix" / "Shared prefix", with a tooltip
        # pointing at the How to Use topic.
        table = self.window.programs_table
        item = table.item(0, 5)
        self.assertIn("prefix", item.text())
        self.assertIn("How to Use", item.toolTip())

    def test_plugins_hint_and_status_tooltips(self):
        # Task 20: the Plug-ins tab says what to do about
        # "Not bridged".
        self.assertIn("Re-sync Bridges",
                      self.window.plugins_hint_label.text())
        table = self.window.plugins_table
        tips = {table.item(r, 0).text(): table.item(r, 2).toolTip()
                for r in range(table.rowCount())}
        self.assertIn("Re-sync Bridges", tips["Lonely.vst3"])
        self.assertIn("gone", tips["Broken.vst3"])
        self.assertIn("DAW", tips["Remix.vst3"])

    def test_source_file_shows_dropped_file(self):
        # Task 21: when the dropped file is still in
        # ~/WinLinx/Plugins, that path is what the column shows.
        dropped = self.home / "WinLinx" / "Plugins" / "Remix.vst3"
        dropped.write_bytes(b"FAKEVST3")
        self.window.refresh()
        self._wait_for_refresh()
        table = self.window.plugins_table
        row = next(r for r in range(table.rowCount())
                   if table.item(r, 0).text() == "Remix.vst3")
        shown = table.item(row, 3).text()
        self.assertIn("WinLinx/Plugins", shown)
        self.assertIn("Remix.vst3", shown)
        self.assertIn("Installed copy",
                      table.item(row, 3).toolTip())

    def test_activity_warnings_filter(self):
        # Task 22: WARNING/ERROR lines are highlighted, and the
        # warnings-only filter hides the routine lines.
        self.assertIsNotNone(self.window._activity_highlighter)
        log = self.home / ".local" / "share" / "winlinx" / "layer.log"
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("[2026-11-16 12:00:00] WARNING: something odd\n")
            fh.write("[2026-11-16 12:01:00] ERROR: something broke\n")
            fh.write("[2026-11-16 12:02:00] routine line\n")
        self.window._poll_activity(force=True)
        full = self.window.activity_view.toPlainText()
        self.assertIn("routine line", full)
        self.window.activity_warnings_only.setChecked(True)
        filtered = self.window.activity_view.toPlainText()
        self.assertIn("something odd", filtered)
        self.assertIn("something broke", filtered)
        self.assertNotIn("routine line", filtered)
        self.window.activity_warnings_only.setChecked(False)
        self.assertIn("routine line",
                      self.window.activity_view.toPlainText())

    def test_system_tab_repair_button_and_no_duplicates(self):
        # Tasks 23 + 31: one Install/Repair action; Re-sync Bridges,
        # Check for Updates and Debug Report no longer duplicated
        # on the System tab.
        self.assertIn("Install or Repair",
                      self.window.repair_btn.text())
        for attr in ("resync2_btn", "updates_btn", "debug_btn"):
            self.assertFalse(hasattr(self.window, attr), attr)
        hint = self.window.watcher_hint_label.text()
        self.assertIn("Watcher", hint)

    def test_quarantine_search_covers_threats(self):
        # Task 24: searching a threat name finds the row; a
        # no-match search says so and the status bar reflects it.
        table = self.window.quarantine_table
        self.window.quarantine_search_box.setText("evil")
        visible = [r for r in range(table.rowCount())
                   if not table.isRowHidden(r)]
        self.assertEqual(len(visible), 1)
        self.window.quarantine_search_box.setText("zzz-no-match")
        visible = [r for r in range(table.rowCount())
                   if not table.isRowHidden(r)]
        self.assertEqual(len(visible), 0)
        self.window.tabs.setCurrentIndex(2)
        _APP.processEvents()
        self.assertTrue(
            self.window.quarantine_nomatch_label.isVisible())
        self.assertIn("zzz-no-match",
                      self.window.quarantine_nomatch_label.text())
        self.assertIn(
            "Showing 0 of 1",
            self.window.statusBar().currentMessage())
        self.window.quarantine_search_box.setText("")
        self.assertFalse(
            self.window.quarantine_nomatch_label.isVisible())

    def test_forget_needs_confirmation(self):
        # Task 25: Forget asks first; No leaves the drop alone.
        import watcher
        self._select_row(self.window.failed_table)
        with mock.patch.object(manager, "confirm_destructive",
                               return_value=False):
            self.window._forget_failed()
        self.assertEqual(len(watcher.load_failed()), 1)
        with mock.patch.object(manager, "confirm_destructive",
                               return_value=True):
            self.window._forget_failed()
            self._wait_for_refresh()
        self.assertEqual(len(watcher.load_failed()), 0)

    def test_cant_uninstall_offers_open_folder(self):
        # Task 26: the dialog has an Open Install Folder button,
        # and pressing it opens the folder.
        opened = []
        captured = {}

        def fake_exec(box):
            captured["box"] = box
            buttons = [b for b in box.buttons()
                       if b.text() == "Open Install Folder"]
            captured["has_button"] = bool(buttons)
            if buttons:
                buttons[0].click()
            return QDialog.DialogCode.Rejected

        row = next(r for r in range(self.window.programs_table.rowCount())
                   if self.window.programs_table.item(r, 0).text()
                   == "FooApp 2.0")
        self._select_row(self.window.programs_table, row)
        with mock.patch.object(manager.common, "uninstall_command",
                               return_value=None):
            with mock.patch.object(QMessageBox, "exec", fake_exec):
                with mock.patch.object(
                        self.window, "_open_path",
                        lambda path: opened.append(str(path))):
                    self.window._uninstall_selected()
        self.assertTrue(captured.get("has_button"))
        self.assertEqual(len(opened), 1)
        self.assertIn("FooApp", opened[0])

    def test_howto_dialog_has_close_button(self):
        # Task 27: the How to Use dialog has a real Close button.
        dialog = manager.HowToUseDialog(self.window)
        try:
            buttons = [b.text() for b in
                       dialog.findChildren(QPushButton)]
            self.assertIn("Close", buttons)
        finally:
            dialog.close()

    def test_scan_picker_has_cancel_button(self):
        # Task 27: the scan picker has a Cancel button.
        captured = {}

        def fake_exec(dialog):
            captured["dialog"] = dialog
            return QDialog.DialogCode.Rejected

        with mock.patch.object(QDialog, "exec", fake_exec):
            self.window._scan_picker()
        buttons = [b.text() for b in
                   captured["dialog"].findChildren(QPushButton)]
        self.assertIn("Cancel", buttons)

    def test_threats_text_shows_file_once(self):
        # Task 28: the file once, threat names deduplicated.
        text = manager.format_threats_text(
            "/tmp/x/file.exe",
            [("/tmp/x/file.exe", "Trojan.A"),
             ("/tmp/x/file.exe", "Trojan.A"),
             ("/tmp/x/other.dll", "Worm.B")])
        self.assertEqual(text.count("/tmp/x/file.exe"), 1)
        self.assertEqual(text.count("Trojan.A"), 1)
        self.assertIn("Worm.B", text)

    def test_uninstall_dialog_safe_button_default(self):
        # Task 29: Cancel, not Uninstall, is the default button.
        dialog = manager.UninstallDialog(self.window)
        try:
            self.assertTrue(dialog.cancel_btn.isDefault())
            self.assertFalse(dialog.uninstall_btn.isDefault())
        finally:
            dialog.close()

    def test_update_signatures_without_clamav(self):
        # Task 11: with no ClamAV, Update Signatures says so and
        # offers the install instead of freshclam commands.
        captured = {}
        installed = []

        def fake_exec(box):
            captured["box"] = box
            buttons = [b for b in box.buttons()
                       if b.text() == "Install ClamAV"]
            captured["has_install"] = bool(buttons)
            if buttons:
                buttons[0].click()
            return QDialog.DialogCode.Rejected

        with mock.patch.object(QMessageBox, "exec", fake_exec):
            with mock.patch.object(
                    self.window, "_install_clamav",
                    lambda: installed.append(True)):
                self.window._update_signatures()
        self.assertIn("isn't installed", captured["box"].text())
        self.assertTrue(captured.get("has_install"))
        self.assertEqual(installed, [True])

    def test_pkexec_missing_explains_itself(self):
        # Task 30: the terminal fallback says why it is needed.
        seen = []
        with mock.patch("shutil.which", return_value=None):
            with mock.patch.object(
                    QMessageBox, "information",
                    lambda *args: seen.append(args[2])):
                self.window._install_clamav()
                self.assertIn("password prompt", seen[-1])
                self.assertIn("terminal", seen[-1])
            with mock.patch.object(
                    manager.scanner, "find_clamscan",
                    return_value="/usr/bin/clamscan"):
                with mock.patch.object(
                        QMessageBox, "information",
                        lambda *args: seen.append(args[2])):
                    self.window._update_signatures()
                    self.assertIn("password prompt", seen[-1])
                    self.assertIn("terminal", seen[-1])

    def test_check_updates_in_help_menu_only(self):
        # Task 31: File menu and System tab no longer carry it.
        menus = {action.text(): action.menu()
                 for action in self.window.menuBar().actions()}
        file_texts = [a.text() for a in
                      menus["File"].actions()]
        help_texts = [a.text() for a in
                      menus["Help"].actions()]
        self.assertNotIn("Check for Updates", file_texts)
        self.assertEqual(help_texts.count("Check for Updates"), 1)
        self.assertFalse(hasattr(self.window, "updates_btn"))

    def test_check_updates_failure_is_plain_words(self):
        # Task 42: no raw "Could not reach GitHub: ..." text.
        seen = []
        result = {"available": False, "latest": "", "url": "",
                  "zip_url": "", "error": "boom 123"}
        with mock.patch.object(manager.common, "check_for_update",
                               return_value=result):
            with mock.patch.object(
                    QMessageBox, "warning",
                    lambda *args: seen.append(args[2])):
                self.window._check_updates()
                self.assertTrue(self._wait_until(lambda: seen))
        self.assertNotIn("boom", seen[0])
        self.assertNotIn("GitHub", seen[0])
        self.assertIn("internet", seen[0])
        self._wait_for_refresh()

    def test_clamav_install_failure_is_plain_words(self):
        # Task 42: no "exit code %s" in the failure dialog.
        import types
        seen = []
        fake_proc = types.SimpleNamespace(returncode=1, stdout="",
                                          stderr="")
        with mock.patch("shutil.which",
                        return_value="/usr/bin/pkexec"):
            with mock.patch("subprocess.run",
                            return_value=fake_proc):
                with mock.patch.object(
                        QMessageBox, "warning",
                        lambda *args: seen.append(args[2])):
                    self.window._install_clamav()
                    self.assertTrue(self._wait_until(lambda: seen))
        self.assertNotIn("exit code", seen[0])
        self.assertIn("could not be installed", seen[0])
        self._wait_for_refresh()

    def test_kill_not_running_says_so(self):
        # Task 10: Kill on a program that is not running says so
        # instead of claiming "Stopped".
        self._select_row(self.window.programs_table)
        warned = []
        with mock.patch.object(manager, "confirm_destructive",
                               return_value=True):
            with mock.patch.object(
                    QMessageBox, "warning",
                    lambda *args: warned.append(args[2])):
                self.window._kill_selected()
                self._wait_for_refresh()
        self.assertEqual(warned, [])
        self.assertIn("is not running",
                      self.window.statusBar().currentMessage())

    def test_column_widths_and_middle_elision(self):
        # Task 16: wider defaults where the content was cut off,
        # paths elided in the middle, full text in tooltips.
        failed = self.window.failed_table
        self.assertGreaterEqual(failed.columnWidth(1), 200)
        self.assertGreaterEqual(failed.columnWidth(3), 130)
        quarantine = self.window.quarantine_table
        self.assertGreaterEqual(quarantine.columnWidth(1), 150)
        for table in (self.window.programs_table,
                      self.window.plugins_table,
                      self.window.quarantine_table,
                      self.window.failed_table):
            self.assertEqual(table.textElideMode(),
                             Qt.TextElideMode.ElideMiddle)
        location = None
        for r in range(self.window.programs_table.rowCount()):
            if (self.window.programs_table.item(r, 0).text()
                    == "FooApp 2.0"):
                location = self.window.programs_table.item(r, 3)
        self.assertIsNotNone(location)
        self.assertEqual(location.toolTip(), location.text())
        self.assertIn("FooApp", location.text())

    def test_middle_elide_delegate_keeps_filename(self):
        # Task 16: the delegate elides paths in the middle, so the
        # filename stays visible (QTableWidget's own textElideMode
        # does not take effect in this Qt build).
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QStyleOptionViewItem
        table = self.window.programs_table
        row = next(r for r in range(table.rowCount())
                   if table.item(r, 0).text() == "FooApp 2.0")
        delegate = manager.MiddleElideDelegate(table)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 200, 30)
        delegate.initStyleOption(option, table.model().index(row, 3))
        self.assertIn("FooApp", option.text)
        self.assertIn("…", option.text)

    # -- v0.4 regression tests ------------------------------------

    def test_tables_are_single_selection(self):
        # Task 3: every action uses only the first selected row, so
        # multi-select is meaningless - and it is what let two rows
        # stay selected after a refresh.
        for table in (self.window.programs_table,
                      self.window.plugins_table,
                      self.window.quarantine_table,
                      self.window.failed_table):
            self.assertEqual(
                table.selectionMode(),
                QTableWidget.SelectionMode.SingleSelection)

    def test_no_row_number_gutter(self):
        # Task 11: the vertical header (1, 2, 3...) is hidden on
        # all four tables.
        for table in (self.window.programs_table,
                      self.window.plugins_table,
                      self.window.quarantine_table,
                      self.window.failed_table):
            self.assertTrue(table.verticalHeader().isHidden())

    def test_system_log_has_activity_colouring(self):
        # Task 12: the System tab log carries the same WARNING /
        # ERROR highlighter as the Activity tab.
        highlighter = self.window._log_highlighter
        self.assertIsNotNone(highlighter)
        self.assertIsInstance(highlighter, manager.ActivityHighlighter)
        self.assertIs(highlighter.document(),
                      self.window.log_view.document())

    def test_log_placeholders(self):
        # Task 13: both log views say "No activity yet" when empty.
        self.assertEqual(self.window.activity_view.placeholderText(),
                         "No activity yet")
        self.assertEqual(self.window.log_view.placeholderText(),
                         "No activity yet")
        # Warnings-only with activity but no warnings says so
        # instead of pretending the log is empty.
        self.window.activity_warnings_only.setChecked(True)
        self.assertEqual(self.window.activity_view.placeholderText(),
                         "No warnings yet")
        self.assertEqual(self.window.activity_view.toPlainText(), "")
        self.window.activity_warnings_only.setChecked(False)
        self.assertEqual(self.window.activity_view.placeholderText(),
                         "No activity yet")

    def test_own_prefix_failure_names_program_and_next_step(self):
        # Task 5: the own-prefix failure dialog matches the launch
        # dialog - program name and next step, not a bare message.
        prog = next(p for p in self.window._programs
                    if p["name"] == "FooApp 2.0")
        seen = []
        with mock.patch.object(
                manager.common, "ensure_program_prefix",
                return_value=(False, "Wine is not installed")):
            with mock.patch.object(
                    QMessageBox, "warning",
                    lambda *args: seen.append(args[2])):
                self.window._toggle_own_prefix(prog, True)
                self.assertTrue(self._wait_until(lambda: seen))
        self.assertEqual(len(seen), 1)
        self.assertIn("FooApp 2.0", seen[0])
        self.assertIn("own prefix", seen[0])
        self.assertIn("Re-run the installer", seen[0])
        self._wait_for_refresh()

    def test_windows_version_failure_names_program_and_next_step(self):
        # Task 5: same treatment for the Windows-version dialog.
        prog = next(p for p in self.window._programs
                    if p["name"] == "FooApp 2.0")
        seen = []
        with mock.patch.object(manager, "confirm_destructive",
                               return_value=True):
            with mock.patch.object(
                    manager.common, "set_program_windows_version",
                    return_value=(False, "Wine is not installed")):
                with mock.patch.object(
                        QMessageBox, "warning",
                        lambda *args: seen.append(args[2])):
                    self.window._set_windows_version(prog, "win10")
                    self.assertTrue(self._wait_until(lambda: seen))
        self.assertEqual(len(seen), 1)
        self.assertIn("FooApp 2.0", seen[0])
        self.assertIn("Windows version", seen[0])
        self.assertIn("Re-run the installer", seen[0])
        self._wait_for_refresh()

    def test_other_prefix_failure_still_names_program(self):
        # Task 5: a non-Wine failure still names the program and
        # keeps the real reason.
        prog = next(p for p in self.window._programs
                    if p["name"] == "FooApp 2.0")
        seen = []
        with mock.patch.object(
                manager.common, "ensure_program_prefix",
                return_value=(False,
                              "the new prefix folder could not be "
                              "created")):
            with mock.patch.object(
                    QMessageBox, "warning",
                    lambda *args: seen.append(args[2])):
                self.window._toggle_own_prefix(prog, True)
                self.assertTrue(self._wait_until(lambda: seen))
        self.assertIn("FooApp 2.0", seen[0])
        self.assertIn("could not be created", seen[0])
        self._wait_for_refresh()

    def test_folder_threats_text_lists_each_file(self):
        # Task 6: each infected file with its own threats, grouped.
        text = manager.format_folder_threats_text(
            "/tmp/scanme",
            [("/tmp/scanme/a.exe", "Win.Trojan.A"),
             ("/tmp/scanme/b.dll", "Win.Trojan.B"),
             ("/tmp/scanme/c.vst3", "PUA.Adware.C"),
             ("/tmp/scanme/a.exe", "Win.Trojan.A")])
        self.assertIn("/tmp/scanme", text)
        for name in ("a.exe", "b.dll", "c.vst3",
                     "Win.Trojan.A", "Win.Trojan.B", "PUA.Adware.C"):
            self.assertIn(name, text)
        self.assertEqual(text.count("Win.Trojan.A"), 1)
        self.assertLess(text.index("a.exe"),
                        text.index("Win.Trojan.A"))
        self.assertLess(text.index("Win.Trojan.A"),
                        text.index("b.dll"))
        self.assertLess(text.index("b.dll"),
                        text.index("Win.Trojan.B"))

    def test_folder_scan_dialog_lists_files_and_counts(self):
        # Task 6: the folder dialog shows the file-to-threat
        # mapping and asks a plural question; Leave it stays the
        # default.
        folder = self.home / "Downloads-fake" / "scanme"
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("a.exe", "b.dll", "c.vst3"):
            (folder / name).write_bytes(b"x")
        result = {"clean": False, "missing": False, "error": "",
                  "threats": [
                      (str(folder / "a.exe"), "Win.Trojan.A"),
                      (str(folder / "b.dll"), "Win.Trojan.B"),
                      (str(folder / "c.vst3"), "PUA.Adware.C")]}
        captured = {}

        def fake_exec(box):
            captured["text"] = box.text()
            captured["info"] = box.informativeText()
            default = box.defaultButton()
            captured["default"] = default.text() if default else None
            return QDialog.DialogCode.Rejected

        with mock.patch.object(manager.scanner, "scan_path",
                               return_value=result):
            with mock.patch.object(QMessageBox, "exec", fake_exec):
                self.window._scan_target(str(folder))
                self.assertTrue(self._wait_until(lambda: captured))
        for name in ("a.exe", "b.dll", "c.vst3",
                     "Win.Trojan.A", "Win.Trojan.B", "PUA.Adware.C"):
            self.assertIn(name, captured["text"])
        self.assertEqual(
            captured["info"],
            "Quarantine these 3 files so they can't install or run?")
        self.assertEqual(captured["default"], "Leave it")

    def test_single_file_scan_dialog_keeps_singular_question(self):
        # Task 6: the single-file dialog is unchanged.
        target = self.home / "Downloads-fake" / "one.exe"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        result = {"clean": False, "missing": False, "error": "",
                  "threats": [(str(target), "Trojan.A"),
                              (str(target), "Worm.B")]}
        captured = {}

        def fake_exec(box):
            captured["text"] = box.text()
            captured["info"] = box.informativeText()
            return QDialog.DialogCode.Rejected

        with mock.patch.object(manager.scanner, "scan_path",
                               return_value=result):
            with mock.patch.object(QMessageBox, "exec", fake_exec):
                self.window._scan_target(str(target))
                self.assertTrue(self._wait_until(lambda: captured))
        self.assertIn("Trojan.A", captured["text"])
        self.assertIn("Worm.B", captured["text"])
        self.assertEqual(captured["info"],
                         "Quarantine it so it can't install or run?")

    def test_quarantine_status_shows_filename_not_path(self):
        # Task 10: "Quarantined evil.exe", not the full path.
        target = self.home / "Downloads-fake" / "evil.exe"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        result = {"clean": False, "missing": False, "error": "",
                  "threats": [(str(target), "Evil.Thing")]}

        def fake_exec(box):
            buttons = [b for b in box.buttons()
                       if b.text() == "Quarantine"]
            self.assertTrue(buttons)
            buttons[0].click()
            return QDialog.DialogCode.Rejected

        with mock.patch.object(manager.scanner, "scan_path",
                               return_value=result):
            with mock.patch.object(QMessageBox, "exec", fake_exec):
                self.window._scan_target(str(target))
                self.assertTrue(self._wait_until(
                    lambda: self.window.statusBar().currentMessage()
                    == "Quarantined evil.exe"))
        self.assertEqual(self.window.statusBar().currentMessage(),
                         "Quarantined evil.exe")

    def test_retry_inside_watched_folder_promises_watcher(self):
        # Task 2: inside a watched folder the old behaviour is
        # right - clear the record, the watcher picks it up.
        import watcher
        self._select_row(self.window.failed_table)
        self.window._retry_failed()
        self._wait_for_refresh()
        status = self.window.statusBar().currentMessage()
        self.assertIn("Retrying broken.exe", status)
        self.assertIn("watcher will pick it up", status)
        self.assertEqual(len(watcher.load_failed()), 0)


class EmptyHomeTests(unittest.TestCase):
    """The manager on a fresh home: empty-state guidance, hidden
    zero counts, and "No prefix yet" (tasks 8, 39, 40)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WINLINX_HOME")
        os.environ["WINLINX_HOME"] = self._tmp.name
        self.home = Path(self._tmp.name)
        (self.home / "WinLinx" / "Programs").mkdir(parents=True)
        (self.home / "WinLinx" / "Plugins").mkdir(parents=True)
        (self.home / ".local" / "share" / "winlinx").mkdir(parents=True)
        self.window = manager.ManagerWindow(startup_update_check=False)
        self.window.show()
        self._wait_for_refresh()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        _APP.processEvents()
        if self._old_home is None:
            os.environ.pop("WINLINX_HOME", None)
        else:
            os.environ["WINLINX_HOME"] = self._old_home
        self._tmp.cleanup()

    def _wait_for_refresh(self, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _APP.processEvents()
            if self.window._worker is None:
                return True
            time.sleep(0.05)
        return self.window._worker is None

    def test_empty_state_guidance(self):
        # Task 8: fresh home says what to do next.
        self.assertEqual(self.window.programs_table.rowCount(), 0)
        self.assertEqual(self.window.plugins_table.rowCount(), 0)
        self.assertTrue(
            self.window.programs_empty_label.isVisible())
        self.assertIn("No programs yet",
                      self.window.programs_empty_label.text())
        self.assertIn("Install Program",
                      self.window.programs_empty_label.text())
        self.assertIn("F1", self.window.programs_empty_label.text())
        self.window.tabs.setCurrentIndex(1)
        _APP.processEvents()
        self.assertTrue(self.window.plugins_empty_label.isVisible())
        self.assertIn("No plug-ins yet",
                      self.window.plugins_empty_label.text())
        self.assertIn("F1", self.window.plugins_empty_label.text())

    def test_tab_counts_hidden_when_zero(self):
        # Task 39: no "(0)" noise on a fresh home.
        self.assertEqual(self.window.tabs.tabText(2), "Quarantine")
        self.assertEqual(self.window.tabs.tabText(3), "Failed Drops")

    def test_no_prefix_yet(self):
        # Task 40: no "(0 B used)" for a prefix that does not exist.
        text = self.window.info_label.text()
        self.assertIn("No prefix yet", text)
        self.assertNotIn("0 B used", text)

    def test_watcher_hint_and_repair_button(self):
        # Task 23: the watcher status is explained, and there is an
        # Install/Repair action.
        self.assertIn("Watcher",
                      self.window.watcher_hint_label.text())
        self.assertIn("Install or Repair",
                      self.window.repair_btn.text())

    def test_empty_log_views_have_placeholder(self):
        # v0.4 task 13: on a fresh home the Activity and System log
        # views are not blank white boxes.
        self.assertEqual(self.window.activity_view.toPlainText(), "")
        self.assertEqual(self.window.log_view.toPlainText(), "")
        self.assertEqual(self.window.activity_view.placeholderText(),
                         "No activity yet")
        self.assertEqual(self.window.log_view.placeholderText(),
                         "No activity yet")


class ManagerMainTests(unittest.TestCase):
    """Regression tests for the real entry point (v0.2.1 P0 #1).

    v0.2 shipped a manager that could not start: main() called
    QLockFile.tryLock() with no argument, which raises TypeError on
    PySide6 6.12, and no test ever called main() - the suite only
    ever built ManagerWindow directly. These tests spawn the real
    `python3 winlinx/manager.py` as a subprocess, offscreen, in a
    fresh fake home.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._procs = []
        self._env = dict(os.environ)
        self._env["QT_QPA_PLATFORM"] = "offscreen"
        self._env["WINLINX_HOME"] = str(self.home)
        self._env["HOME"] = str(self.home)

    def tearDown(self):
        for proc in self._procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=10)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        self._tmp.cleanup()

    def _launch(self):
        root = Path(__file__).resolve().parent.parent
        proc = subprocess.Popen(
            [sys.executable, str(root / "winlinx" / "manager.py")],
            env=self._env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self._procs.append(proc)
        return proc

    def _wait_until_up(self, proc, timeout=20.0):
        # Up = the single-instance lock file exists and the process
        # is still alive. The lock is taken before the window is
        # built, so this also proves main() got past tryLock().
        lock = (self.home / ".local" / "share" / "winlinx"
                / "manager.lock")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return False
            if lock.exists():
                return True
            time.sleep(0.1)
        return lock.exists() and proc.poll() is None

    def _fail_with_stderr(self, proc, message):
        _out, err = proc.communicate(timeout=5)
        self.fail("%s (rc=%s): %s"
                  % (message, proc.returncode, err[-2000:]))

    def test_main_starts_and_stays_up(self):
        proc = self._launch()
        if not self._wait_until_up(proc):
            self._fail_with_stderr(proc,
                                   "manager exited during startup")
        # Still up a moment later: it did not crash after opening.
        time.sleep(2)
        self.assertIsNone(proc.poll(), "manager died after starting")
        proc.terminate()
        _out, err = proc.communicate(timeout=10)
        self.assertNotIn("Traceback", err)

    def test_second_launch_pokes_running_window(self):
        first = self._launch()
        if not self._wait_until_up(first):
            self._fail_with_stderr(first,
                                   "first manager exited at startup")
        # Wait until the running instance is listening, probing it
        # the same way a second launch does.
        sock = QLocalSocket()
        deadline = time.time() + 10
        connected = False
        while time.time() < deadline:
            sock.connectToServer(manager.SERVER_NAME)
            if sock.waitForConnected(1000):
                connected = True
                break
            sock.abort()
            time.sleep(0.2)
        self.assertTrue(connected,
                        "running manager never started listening")
        sock.write(b"raise")
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        # A second launch must exit promptly with code 0, and the
        # first instance must still be alive afterwards. (60 s, not
        # 20: on a loaded box a fresh Python+Qt start alone can
        # exceed 20 s, which used to fail this test as a flake.)
        root = Path(__file__).resolve().parent.parent
        second = subprocess.run(
            [sys.executable, str(root / "winlinx" / "manager.py")],
            env=self._env, capture_output=True, text=True,
            timeout=60)
        self.assertEqual(second.returncode, 0, second.stderr[-2000:])
        self.assertNotIn("Traceback", second.stderr)
        self.assertIsNone(first.poll(),
                          "first instance died after second launch")

    def test_unwritable_layer_dir_exits_with_plain_error(self):
        # If the lock file cannot even be created, main() must say
        # so and exit 1 - not claim WinLinx is already running and
        # hang on a modal dialog (seen live during verification:
        # a transient /tmp write freeze left the manager sitting
        # on the "already running" dialog for a home it never
        # wrote to).
        env = dict(self._env)
        env["WINLINX_HOME"] = "/proc/winlinx-cannot-write-here"
        root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, str(root / "winlinx" / "manager.py")],
            env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stderr[-2000:])
        self.assertIn("cannot create", proc.stderr)
        self.assertNotIn("already running", proc.stderr)


class V04RetryOutsideTests(unittest.TestCase):
    """v0.4 task 2: Retry on a file OUTSIDE the watched folders
    re-runs it through the runner path (fake wine proves it), and
    never claims the watcher will pick it up. Adapted from the
    v0.3 reviewer's drive_runner.py R3 scenario."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WINLINX_HOME")
        os.environ["WINLINX_HOME"] = self._tmp.name
        self.home = Path(self._tmp.name)
        fakehome.build_fake_home(self.home)
        downloads = self.home / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        (downloads / "dl2.exe").write_bytes(b"FAKEEXE")
        (downloads / "dl-nowine.exe").write_bytes(b"FAKEEXE")
        (downloads / "notes.txt").write_text("hello")
        # gone.exe has a record but no file.
        state = {}
        for name, attempts in (("dl2.exe", 1), ("dl-nowine.exe", 1),
                               ("notes.txt", 1), ("gone.exe", 1)):
            state[str(downloads / name)] = {
                "size": 1, "mtime": 1, "action": "failed",
                "reason": "Wine is not installed", "attempts": attempts,
                "next_retry": 0, "time": "2026-11-17T09:04:00"}
        (self.home / ".local" / "share" / "winlinx"
         / "handled.json").write_text(json.dumps(state))
        # Fake wine: logs every invocation, exits 0.
        self.fakebin = Path(self._tmp.name) / "fakebin"
        self.fakebin.mkdir()
        self.wine_log = self.fakebin / "wine.log"
        (self.fakebin / "wine").write_text(
            "#!/bin/bash\n"
            "echo \"WINE: $*\" >> %s\n"
            "exit 0\n" % self.wine_log)
        (self.fakebin / "wine").chmod(0o755)
        self.window = manager.ManagerWindow(startup_update_check=False)
        self.window.show()
        self._wait_for_refresh()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        _APP.processEvents()
        if self._old_home is None:
            os.environ.pop("WINLINX_HOME", None)
        else:
            os.environ["WINLINX_HOME"] = self._old_home
        self._tmp.cleanup()

    def _wait_for_refresh(self, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _APP.processEvents()
            if self.window._worker is None:
                return True
            time.sleep(0.05)
        return self.window._worker is None

    def _wait_until(self, predicate, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _APP.processEvents()
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def _select_failed(self, name):
        table = self.window.failed_table
        for row in range(table.rowCount()):
            if table.item(row, 0).text() == name:
                table.selectRow(row)
                _APP.processEvents()
                return
        self.fail("no Failed Drops row named %s" % name)

    def _wine_invocations(self, name):
        if not self.wine_log.exists():
            return 0
        return self.wine_log.read_text().count(name)

    def test_retry_outside_runs_the_file(self):
        # R3: Retry on dl2.exe (in Downloads, outside the watched
        # folders) runs it - fake wine is invoked - and the status
        # never mentions the watcher. A watcher pass afterwards
        # does not run it a second time.
        self._select_failed("dl2.exe")
        path = (str(self.fakebin) + os.pathsep
                + os.environ["PATH"])
        with mock.patch.dict(os.environ, {"PATH": path}):
            self.window._retry_failed()
            self.assertTrue(self._wait_until(
                lambda: self.window._worker is None
                and "Retried dl2.exe"
                in self.window.statusBar().currentMessage()))
        self.assertEqual(self._wine_invocations("dl2.exe"), 1)
        self.assertNotIn(
            "watcher",
            self.window.statusBar().currentMessage().lower())
        env = dict(os.environ)
        env["WINLINX_HOME"] = str(self.home)
        env["HOME"] = str(self.home)
        env["PATH"] = path
        root = Path(__file__).resolve().parent.parent
        subprocess.run(
            [sys.executable, str(root / "winlinx" / "watcher.py"),
             "--once"],
            env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(self._wine_invocations("dl2.exe"), 1)

    def test_retry_outside_without_wine_keeps_record_and_says_so(self):
        if shutil.which("wine"):
            self.skipTest("wine is installed here")
        self._select_failed("dl-nowine.exe")
        self.window._retry_failed()
        self.assertTrue(self._wait_until(
            lambda: self.window._worker is None
            and "Still could not handle"
            in self.window.statusBar().currentMessage()))
        status = self.window.statusBar().currentMessage()
        self.assertIn("dl-nowine.exe", status)
        self.assertIn("Wine is not installed", status)
        self.assertNotIn("watcher will pick it up", status)
        import watcher
        failed = [e for e in watcher.load_failed()
                  if e["name"] == "dl-nowine.exe"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["attempts"], 2)

    def test_retry_unsupported_file_keeps_record(self):
        import watcher
        self._select_failed("notes.txt")
        seen = []
        with mock.patch.object(
                QMessageBox, "information",
                lambda *args: seen.append(args[2])):
            self.window._retry_failed()
        self.assertEqual(len(seen), 1)
        self.assertIn("notes.txt", seen[0])
        self.assertIn("only handles", seen[0])
        self.assertIn("Cannot retry",
                      self.window.statusBar().currentMessage())
        failed = [e for e in watcher.load_failed()
                  if e["name"] == "notes.txt"]
        self.assertEqual(len(failed), 1)

    def test_retry_missing_file_keeps_record(self):
        import watcher
        self._select_failed("gone.exe")
        self.window._retry_failed()
        self.assertIn("no longer there",
                      self.window.statusBar().currentMessage())
        failed = [e for e in watcher.load_failed()
                  if e["name"] == "gone.exe"]
        self.assertEqual(len(failed), 1)


class V04TriggerTests(unittest.TestCase):
    """v0.4 task 3: the reviewer's trigger sequence (W2 / X1 / X2
    drivers) followed by select + refresh on each table leaves
    exactly one row selected, and actions act on it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WINLINX_HOME")
        os.environ["WINLINX_HOME"] = self._tmp.name
        self.home = Path(self._tmp.name)
        fakehome.build_fake_home(self.home)
        bad = self.home / "nasty.exe"
        bad.write_bytes(b"x")
        scanner.quarantine_item(bad, [(str(bad), "Evil.Thing")])
        shady = self.home / "shady.dll"
        shady.write_bytes(b"x")
        scanner.quarantine_item(shady, [(str(shady), "Sneaky.Worm")])
        state = {}
        for folder, name in (("Programs", "broken.exe"),
                             ("Plugins", "BigSynth.vst3")):
            path = self.home / "WinLinx" / folder / name
            state[str(path)] = {
                "size": 1, "mtime": 1, "action": "failed",
                "reason": "Wine is not installed", "attempts": 2,
                "next_retry": 0, "time": "2026-11-12T09:04:00"}
        (self.home / ".local" / "share" / "winlinx"
         / "handled.json").write_text(json.dumps(state))
        self.window = manager.ManagerWindow(startup_update_check=False)
        self.window.show()
        self._wait_for_refresh()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        _APP.processEvents()
        if self._old_home is None:
            os.environ.pop("WINLINX_HOME", None)
        else:
            os.environ["WINLINX_HOME"] = self._old_home
        self._tmp.cleanup()

    def _wait_for_refresh(self, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _APP.processEvents()
            if self.window._worker is None:
                return True
            time.sleep(0.05)
        return self.window._worker is None

    def _row_for(self, table, name):
        for row in range(table.rowCount()):
            if table.item(row, 0).text() == name:
                return row
        self.fail("no row named %s" % name)

    def test_trigger_sequence_keeps_one_selection_per_table(self):
        window = self.window
        window.activateWindow()
        window.raise_()
        _APP.processEvents()
        # W2 history: Ctrl+R, F5, F1 open/close, Ctrl+I.
        QTest.keyClick(window, Qt.Key.Key_R,
                       Qt.KeyboardModifier.ControlModifier)
        self._wait_for_refresh()
        QTest.keyClick(window, Qt.Key.Key_F5)
        self._wait_for_refresh()
        QTest.keyClick(window, Qt.Key.Key_F1)
        _APP.processEvents()
        time.sleep(0.3)
        if window._howto_dialog is not None:
            window._howto_dialog.close()
        _APP.processEvents()
        window.activateWindow()
        window.raise_()
        _APP.processEvents()
        pick = self.home / "Downloads-fake" / "ctrl-i.exe"
        pick.parent.mkdir(parents=True, exist_ok=True)
        pick.write_bytes(b"FAKEEXE")
        orig_open = QFileDialog.getOpenFileName
        QFileDialog.getOpenFileName = staticmethod(
            lambda *a, **k: (str(pick), ""))
        try:
            QTest.keyClick(window, Qt.Key.Key_I,
                           Qt.KeyboardModifier.ControlModifier)
            self._wait_for_refresh()
        finally:
            QFileDialog.getOpenFileName = orig_open
        # The trigger really ran: Ctrl+I copied the file.
        self.assertTrue((self.home / "WinLinx" / "Programs"
                         / "ctrl-i.exe").exists())
        # X2: Ctrl+R four more times.
        for _ in range(4):
            QTest.keyClick(window, Qt.Key.Key_R,
                           Qt.KeyboardModifier.ControlModifier)
            self._wait_for_refresh()
        cases = [
            (window.programs_table, "MSI Thing"),
            (window.plugins_table, "Remix.vst3"),
            (window.quarantine_table, "shady.dll"),
            (window.failed_table, "BigSynth.vst3"),
        ]
        for table, name in cases:
            table.selectRow(self._row_for(table, name))
            _APP.processEvents()
            window.refresh()
            self._wait_for_refresh()
            selected = table.selectionModel().selectedRows()
            self.assertEqual(len(selected), 1,
                             "%s: %s selected after refresh"
                             % (name, len(selected)))
            self.assertEqual(
                table.item(selected[0].row(), 0).text(), name)
            data = window._selected_row_data(table)
            self.assertIsNotNone(data, name)
            self.assertEqual(data.get("name"), name)

    def test_refresh_after_selected_row_vanishes_selects_nothing(self):
        # Found while screenshotting v0.4: when the selected row's
        # record disappears (e.g. a successful Retry), Qt moved the
        # selection to row 0 - a row the user never chose - and the
        # action buttons stayed enabled for it.
        import watcher
        window = self.window
        table = window.failed_table
        table.selectRow(self._row_for(table, "BigSynth.vst3"))
        _APP.processEvents()
        watcher.clear_handled(
            str(self.home / "WinLinx" / "Plugins" / "BigSynth.vst3"))
        window.refresh()
        self._wait_for_refresh()
        self.assertEqual(table.selectionModel().selectedRows(), [])
        self.assertIsNone(window._selected_row_data(table))
        self.assertFalse(window.retry_btn.isEnabled())
        self.assertFalse(window.forget_btn.isEnabled())


class V04SortTests(unittest.TestCase):
    """v0.4 task 16: tables sort A-to-Z by default, and remember
    the user's last sort."""

    def setUp(self):
        from PySide6.QtCore import QSettings
        self._settings = QSettings("BrokeNBored", "WinLinx")
        for key in ("programs", "plugins", "quarantine", "failed"):
            self._settings.remove("sort/" + key)
        self._settings.sync()
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WINLINX_HOME")
        os.environ["WINLINX_HOME"] = self._tmp.name
        self.home = Path(self._tmp.name)
        fakehome.build_fake_home(self.home)
        bad = self.home / "nasty.exe"
        bad.write_bytes(b"x")
        scanner.quarantine_item(bad, [(str(bad), "Evil.Thing")])
        self.window = manager.ManagerWindow(startup_update_check=False)
        self.window.show()
        self._wait_for_refresh()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        _APP.processEvents()
        for key in ("programs", "plugins", "quarantine", "failed"):
            self._settings.remove("sort/" + key)
        self._settings.sync()
        if self._old_home is None:
            os.environ.pop("WINLINX_HOME", None)
        else:
            os.environ["WINLINX_HOME"] = self._old_home
        self._tmp.cleanup()

    def _wait_for_refresh(self, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _APP.processEvents()
            if self.window._worker is None:
                return True
            time.sleep(0.05)
        return self.window._worker is None

    def _names(self, table):
        return [table.item(r, 0).text()
                for r in range(table.rowCount())]

    def test_tables_sort_ascending_by_default(self):
        self.assertEqual(self._names(self.window.programs_table),
                         ["FooApp 2.0", "MSI Thing"])
        self.assertEqual(self._names(self.window.plugins_table),
                         ["Broken.vst3", "Lonely.vst3", "Remix.vst3"])
        for table in (self.window.programs_table,
                      self.window.plugins_table,
                      self.window.quarantine_table,
                      self.window.failed_table):
            header = table.horizontalHeader()
            self.assertEqual(header.sortIndicatorSection(), 0)
            self.assertEqual(header.sortIndicatorOrder(),
                             Qt.SortOrder.AscendingOrder)

    def test_last_sort_is_remembered(self):
        table = self.window.programs_table
        table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        _APP.processEvents()
        self.assertEqual(self._names(table),
                         ["MSI Thing", "FooApp 2.0"])
        # A refresh keeps the user's sort.
        self.window.refresh()
        self._wait_for_refresh()
        self.assertEqual(self._names(table),
                         ["MSI Thing", "FooApp 2.0"])
        # A new window remembers it too.
        self.window.close()
        self.window.deleteLater()
        _APP.processEvents()
        self.window = manager.ManagerWindow(startup_update_check=False)
        self.window.show()
        self._wait_for_refresh()
        self.assertEqual(self._names(self.window.programs_table),
                         ["MSI Thing", "FooApp 2.0"])
        self.assertEqual(
            self.window.programs_table.horizontalHeader()
            .sortIndicatorOrder(),
            Qt.SortOrder.DescendingOrder)


if __name__ == "__main__":
    unittest.main()
