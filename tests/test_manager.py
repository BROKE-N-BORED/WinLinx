"""Tests for winlinx/manager.py — the Qt Program Manager window.
Runs offscreen (QT_QPA_PLATFORM=offscreen); fake home via WINLINX_HOME."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "winlinx")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakehome  # noqa: E402
import scanner  # noqa: E402

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402
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
        self.window = manager.ManagerWindow()
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
        self.assertIn("0.2", self.window.windowTitle())
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
        self.assertEqual(table.item(row, 5).text(), "Shared")

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


if __name__ == "__main__":
    unittest.main()
