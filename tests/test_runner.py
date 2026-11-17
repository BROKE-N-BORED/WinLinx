"""Tests for winlinx/run-windows-file - the double-click handler.

Runs the real script in a subprocess against a fake home with a
logging fake notify-send (the review's driver pattern). No Wine, no
yabridge, no ClamAV on this machine: fakes stand in where a success
path needs one.

Covers v0.3 tasks 3 (no false "bridged"), 36 (double-click failures
recorded as failed drops) and 45 (sync-failure log carries the
reason).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "winlinx")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import watcher  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
RUNNER = REPO / "winlinx" / "run-windows-file"


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._old_home = os.environ.get("WINLINX_HOME")
        os.environ["WINLINX_HOME"] = str(self.home)
        # Installed layout, as the installer leaves it.
        self.layer = self.home / ".local" / "share" / "winlinx"
        self.layer.mkdir(parents=True, exist_ok=True)
        for name in ("common.py", "scanner.py", "watcher.py"):
            shutil.copy2(REPO / "winlinx" / name, self.layer / name)
        self.downloads = self.home / "Downloads"
        self.downloads.mkdir(parents=True, exist_ok=True)
        # Fake bin: a logging notify-send, plus fakes added per test.
        self.fakebin = self.home / "fakebin"
        self.fakebin.mkdir(parents=True, exist_ok=True)
        self.notify_log = self.home / "notify.log"
        (self.fakebin / "notify-send").write_text(
            "#!/bin/bash\n"
            "echo \"NOTIFY: $*\" >> %s\n" % self.notify_log)
        (self.fakebin / "notify-send").chmod(0o755)
        self.wine_log = self.home / "wine.log"
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.home)
        self.env["WINLINX_HOME"] = str(self.home)
        self.env["PATH"] = "%s:/usr/bin:/bin" % self.fakebin
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WINLINX_HOME", None)
        else:
            os.environ["WINLINX_HOME"] = self._old_home
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------

    def _add_fake(self, name, body):
        script = self.fakebin / name
        script.write_text("#!/bin/bash\n%s\n" % body)
        script.chmod(0o755)

    def _run_runner(self, target):
        subprocess.run(["bash", str(RUNNER), str(target)],
                       env=self.env, timeout=60,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        # Notifications and the detached wine run are backgrounded.
        time.sleep(0.5)

    def _notifications(self):
        if not self.notify_log.is_file():
            return ""
        return self.notify_log.read_text(encoding="utf-8")

    def _log(self):
        path = self.layer / "layer.log"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def _failed(self):
        return {entry["name"]: entry for entry in watcher.load_failed()}

    # -- plug-in double-clicks with no yabridge (tasks 3, 36, 45) ----

    def test_vst3_no_yabridge_notifies_failure(self):
        target = self.downloads / "ClickSynth.vst3"
        target.write_bytes(b"VST3DATA")
        self._run_runner(target)
        notes = self._notifications()
        self.assertIn("ClickSynth.vst3 could not be bridged - yabridge "
                      "is not installed. See the WinLinx Program "
                      "Manager.", notes)
        self.assertNotIn("was bridged", notes)
        self.assertNotIn("yabridgectl", notes)
        self.assertIn("WARNING: bridge sync failed "
                      "(yabridge is not installed)", self._log())
        # The copy still happened; only the sync failed.
        copied = (self.home / ".wine" / "drive_c" / "Program Files"
                  / "Common Files" / "VST3" / "ClickSynth.vst3")
        self.assertTrue(copied.is_file())
        failed = self._failed()
        self.assertIn("ClickSynth.vst3", failed)
        self.assertEqual(
            failed["ClickSynth.vst3"]["reason"],
            "yabridge is not installed, so the plug-in could not be "
            "bridged")
        self.assertEqual(failed["ClickSynth.vst3"]["attempts"], 1)

    def test_dll_no_yabridge_notifies_failure(self):
        target = self.downloads / "click.dll"
        target.write_bytes(b"DLLDATA")
        self._run_runner(target)
        notes = self._notifications()
        self.assertIn("click.dll could not be bridged - yabridge is "
                      "not installed.", notes)
        self.assertNotIn("was bridged", notes)
        failed = self._failed()
        self.assertIn("click.dll", failed)
        copied = (self.home / ".wine" / "drive_c" / "Program Files"
                  / "Common Files" / "VST2" / "click.dll")
        self.assertTrue(copied.is_file())

    def test_clap_no_yabridge_notifies_failure(self):
        target = self.downloads / "click.clap"
        target.write_bytes(b"CLAPDATA")
        self._run_runner(target)
        notes = self._notifications()
        self.assertIn("click.clap could not be bridged - yabridge is "
                      "not installed.", notes)
        self.assertNotIn("was bridged", notes)
        self.assertIn("click.clap", self._failed())

    def test_vst3_bundle_no_yabridge_notifies_failure(self):
        bundle = self.downloads / "ClickBundle.vst3"
        (bundle / "Contents").mkdir(parents=True)
        (bundle / "Contents" / "Bundle.so").write_bytes(b"BUNDLEBIN")
        self._run_runner(bundle)
        notes = self._notifications()
        self.assertIn("ClickBundle.vst3 could not be bridged - "
                      "yabridge is not installed.", notes)
        self.assertNotIn("was bridged", notes)
        copied = (self.home / ".wine" / "drive_c" / "Program Files"
                  / "Common Files" / "VST3" / "ClickBundle.vst3"
                  / "Contents" / "Bundle.so")
        self.assertTrue(copied.is_file())
        self.assertIn("ClickBundle.vst3", self._failed())

    # -- plug-in double-clicks with a yabridge stand-in --------------

    def test_vst3_with_working_yabridge_notifies_bridged(self):
        self._add_fake("yabridgectl", "exit 0")
        target = self.downloads / "ClickSynth.vst3"
        target.write_bytes(b"VST3DATA")
        self._run_runner(target)
        notes = self._notifications()
        self.assertIn("ClickSynth.vst3 was bridged into ~/.vst3 for "
                      "your DAW.", notes)
        self.assertNotIn("could not be bridged", notes)
        self.assertIn("Bridges re-synced", self._log())
        self.assertEqual(self._failed(), {})

    def test_vst3_with_failing_yabridge_names_the_sync(self):
        self._add_fake("yabridgectl", "exit 1")
        target = self.downloads / "ClickSynth.vst3"
        target.write_bytes(b"VST3DATA")
        self._run_runner(target)
        notes = self._notifications()
        self.assertIn("ClickSynth.vst3 could not be bridged - the "
                      "plug-in folders could not be re-synced.",
                      notes)
        self.assertNotIn("was bridged", notes)
        self.assertIn("WARNING: bridge sync failed (the plug-in "
                      "folders could not be re-synced)", self._log())
        failed = self._failed()
        self.assertIn("ClickSynth.vst3", failed)
        self.assertEqual(failed["ClickSynth.vst3"]["reason"],
                         "the plug-in folders could not be re-synced")

    def test_copy_failure_records_failed(self):
        # The VST3 destination is a plain file, so the copy cannot
        # land.
        dest_dir = (self.home / ".wine" / "drive_c" / "Program Files"
                    / "Common Files")
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "VST3").write_bytes(b"not a directory")
        target = self.downloads / "ClickSynth.vst3"
        target.write_bytes(b"VST3DATA")
        self._run_runner(target)
        self.assertIn("ERROR copying ClickSynth.vst3", self._log())
        self.assertNotIn("was bridged", self._notifications())
        failed = self._failed()
        self.assertIn("ClickSynth.vst3", failed)
        self.assertEqual(failed["ClickSynth.vst3"]["reason"],
                         "copy failed")

    # -- program double-clicks (task 36) ------------------------------

    def test_exe_no_wine_records_failed(self):
        target = self.downloads / "clickme.exe"
        target.write_bytes(b"FAKEEXE")
        self._run_runner(target)
        self.assertIn("Wine is not installed, so clickme.exe cannot "
                      "run.", self._notifications())
        failed = self._failed()
        self.assertIn("clickme.exe", failed)
        self.assertEqual(failed["clickme.exe"]["reason"],
                         "Wine is not installed")
        self.assertEqual(failed["clickme.exe"]["attempts"], 1)

    def test_msi_no_wine_records_failed(self):
        target = self.downloads / "setup2.msi"
        target.write_bytes(b"FAKEMSI")
        self._run_runner(target)
        failed = self._failed()
        self.assertIn("setup2.msi", failed)
        self.assertEqual(failed["setup2.msi"]["reason"],
                         "Wine is not installed")

    def test_exe_with_wine_runs_and_records_nothing(self):
        self._add_fake("wine", "echo \"WINE: $*\" >> %s" % self.wine_log)
        target = self.downloads / "clickme.exe"
        target.write_bytes(b"FAKEEXE")
        self._run_runner(target)
        self.assertTrue(self.wine_log.is_file())
        self.assertIn(str(target),
                      self.wine_log.read_text(encoding="utf-8"))
        self.assertEqual(self._failed(), {})

    # -- the record matches the watcher format (task 36) --------------

    def test_failed_record_matches_watcher_format(self):
        # watcher.load_failed() is what the manager's Failed Drops
        # tab reads: if it sees the runner's record, the manager
        # shows it.
        target = self.downloads / "clickme.exe"
        target.write_bytes(b"FAKEEXE")
        self._run_runner(target)
        state = json.loads(
            (self.layer / "handled.json").read_text(encoding="utf-8"))
        record = state[str(target)]
        self.assertEqual(record["action"], "failed")
        self.assertEqual(record["size"], target.stat().st_size)
        self.assertEqual(record["mtime"], target.stat().st_mtime_ns)
        self.assertGreater(record["next_retry"], 0)
        self.assertTrue(record["time"])
        # A second failed double-click counts as attempt 2.
        self._run_runner(target)
        state = json.loads(
            (self.layer / "handled.json").read_text(encoding="utf-8"))
        self.assertEqual(state[str(target)]["attempts"], 2)

    def test_version(self):
        proc = subprocess.run(["bash", str(RUNNER), "--version"],
                              env=self.env, timeout=30,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("WinLinx runner 0.3", proc.stdout)


if __name__ == "__main__":
    unittest.main()
