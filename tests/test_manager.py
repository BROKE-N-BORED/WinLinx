"""Tests for winlinx/manager.py — the Qt Program Manager window.
Runs offscreen (QT_QPA_PLATFORM=offscreen); fake home via WINLINX_HOME."""

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

from PySide6.QtWidgets import QApplication  # noqa: E402
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
        self.assertEqual(len(labels), 4)
        self.assertEqual(labels[0], "Programs")
        self.assertEqual(labels[1], "Plug-ins")
        self.assertTrue(labels[2].startswith("Quarantine"))
        self.assertEqual(labels[3], "System")

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


if __name__ == "__main__":
    unittest.main()
