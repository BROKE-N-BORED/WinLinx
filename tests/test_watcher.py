"""Tests for winlinx/watcher.py — folder polling, scan gating,
Wine runs, VST bridging. Wine and clamscan are stubbed out; the
executor records every call."""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "winlinx")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import scanner  # noqa: E402
import watcher  # noqa: E402
import fakehome  # noqa: E402


class StubExecutor:
    def __init__(self):
        self.detached = []
        self.waited = []
        self.syncs = 0
        self._lock = threading.Lock()

    def run_detached(self, cmd):
        with self._lock:
            self.detached.append(list(cmd))

    def run_wait(self, cmd, timeout=None):
        with self._lock:
            self.waited.append(list(cmd))
        return 0

    def sync_bridges(self):
        with self._lock:
            self.syncs += 1
        return True, ""


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WINLINX_HOME")
        os.environ["WINLINX_HOME"] = self._tmp.name
        self.home = Path(self._tmp.name)
        fakehome.build_fake_home(self.home)
        self.programs = self.home / "WinLinx" / "Programs"
        self.plugins = self.home / "WinLinx" / "Plugins"
        self.executor = StubExecutor()
        self.w = watcher.Watcher(executor=self.executor)
        self._patches = []
        self._scan_results = {}

    def tearDown(self):
        for p in self._patches:
            p.stop()
        if self._old_home is None:
            os.environ.pop("WINLINX_HOME", None)
        else:
            os.environ["WINLINX_HOME"] = self._old_home
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------

    def _stub_scan(self, flagged_names=()):
        def fake_scan(path, timeout=None):
            name = Path(path).name
            if name in flagged_names:
                return {"clean": False,
                        "threats": [(str(path), "Evil.Thing")],
                        "error": "", "missing": False}
            return {"clean": True, "threats": [], "error": "",
                    "missing": False}
        p = mock.patch.object(watcher.scanner, "scan_path",
                              side_effect=fake_scan)
        started = p.start()
        self._patches.append(p)
        return started

    def _stub_wine(self, present=True):
        p = mock.patch.object(
            watcher.common, "find_wine",
            return_value="/usr/bin/wine" if present else None)
        p.start()
        self._patches.append(p)

    def _drop(self, folder, name, content=b"x"):
        target = folder / name
        target.write_bytes(content)
        return target

    def _settle(self):
        """Two scan passes (stability gate) + wait for worker threads."""
        self.w.scan_once()
        self.w.scan_once()
        _wait_for(lambda: not self.w._busy, timeout=5.0)
        time.sleep(0.05)

    # -- tests --------------------------------------------------------

    def test_program_exe_runs_once(self):
        self._stub_scan()
        self._stub_wine()
        target = self._drop(self.programs, "game.exe")
        self._settle()
        self.assertEqual(self.executor.detached,
                         [["wine", str(target)]])
        # Third pass: already handled, not run again.
        self._settle()
        self.assertEqual(len(self.executor.detached), 1)

    def test_unrelated_files_ignored(self):
        self._stub_scan()
        self._stub_wine()
        self._drop(self.programs, "notes.txt")
        self._drop(self.programs, "photo.jpg")
        self._settle()
        self.assertEqual(self.executor.detached, [])
        self.assertEqual(self.executor.waited, [])

    def test_plugin_installer_runs_then_syncs(self):
        self._stub_scan()
        self._stub_wine()
        target = self._drop(self.plugins, "synth-setup.exe")
        self._settle()
        self.assertEqual(self.executor.waited, [["wine", str(target)]])
        self.assertEqual(self.executor.syncs, 1)

    def test_vst3_file_copied_and_synced(self):
        self._stub_scan()
        target = self._drop(self.plugins, "Chorus.vst3", b"VST3DATA")
        self._settle()
        copied = common.vst3_dir() / "Chorus.vst3"
        self.assertTrue(copied.exists())
        self.assertEqual(copied.read_bytes(), b"VST3DATA")
        self.assertEqual(self.executor.syncs, 1)
        self.assertEqual(self.executor.detached, [])

    def test_dll_file_copied_to_vst2_and_synced(self):
        self._stub_scan()
        target = self._drop(self.plugins, "OldVerb.dll", b"DLLDATA")
        self._settle()
        copied = common.vst2_dir() / "OldVerb.dll"
        self.assertTrue(copied.exists())
        self.assertEqual(self.executor.syncs, 1)

    def test_flagged_file_quarantined_never_runs(self):
        self._stub_scan(flagged_names={"nasty.exe"})
        self._stub_wine()
        target = self._drop(self.programs, "nasty.exe")
        self._settle()
        self.assertFalse(target.exists())
        self.assertEqual(self.executor.detached, [])
        items = scanner.load_quarantine()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["original"], str(target))
        self.assertEqual(items[0]["threats"], ["Evil.Thing"])

    def test_missing_scanner_fails_open_with_warning(self):
        def missing_scan(path, timeout=None):
            return {"clean": None, "threats": [],
                    "error": "ClamAV is not installed", "missing": True}
        p = mock.patch.object(watcher.scanner, "scan_path",
                              side_effect=missing_scan)
        p.start()
        self._patches.append(p)
        self._stub_wine()
        target = self._drop(self.programs, "game.exe")
        self._settle()
        self.assertEqual(self.executor.detached, [["wine", str(target)]])
        log = common.log_path().read_text(encoding="utf-8")
        self.assertIn("UNSCANNED", log)

    def test_allowlisted_file_skips_scan(self):
        scan = self._stub_scan()
        self._stub_wine()
        target = self._drop(self.programs, "trusted.exe")
        # Allowlist it first via the scanner's own mechanism.
        data = {"files": {scanner.sha256_file(target): "trusted.exe"},
                "folders": []}
        scanner._save_json(scanner.allowlist_path(), data)
        self._settle()
        scan.assert_not_called()
        self.assertEqual(self.executor.detached, [["wine", str(target)]])

    def test_dropped_folder_scanned_and_left(self):
        self._stub_scan()
        folder = self.programs / "PortableGame"
        folder.mkdir()
        (folder / "game.exe").write_bytes(b"x")
        self._settle()
        self.assertTrue(folder.exists())
        self.assertEqual(self.executor.detached, [])
        log = common.log_path().read_text(encoding="utf-8")
        self.assertIn("left in place", log)

    def test_vst3_bundle_folder_bridged(self):
        self._stub_scan()
        bundle = self.plugins / "Fancy.vst3"
        bundle.mkdir()
        (bundle / "plugin.bin").write_bytes(b"x")
        self._settle()
        self.assertTrue((common.vst3_dir() / "Fancy.vst3"
                         / "plugin.bin").exists())
        self.assertEqual(self.executor.syncs, 1)

    def test_wine_missing_logs_warning(self):
        self._stub_scan()
        self._stub_wine(present=False)
        self._drop(self.programs, "game.exe")
        self._settle()
        self.assertEqual(self.executor.detached, [])
        log = common.log_path().read_text(encoding="utf-8")
        self.assertIn("Wine is not installed", log)

    def test_clear_handled_reprocesses(self):
        self._stub_scan()
        self._stub_wine()
        target = self._drop(self.programs, "game.exe")
        self._settle()
        self.assertEqual(len(self.executor.detached), 1)
        watcher.clear_handled(str(target))
        # Fresh watcher (fresh _seen) re-processes after stability.
        self.w = watcher.Watcher(executor=self.executor)
        self._settle()
        self.assertEqual(len(self.executor.detached), 2)


if __name__ == "__main__":
    unittest.main()
