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

    def _run_runner(self, target, env=None):
        subprocess.run(["/bin/bash", str(RUNNER), str(target)],
                       env=env if env is not None else self.env,
                       timeout=60,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        # Notifications and the detached wine run are backgrounded.
        time.sleep(0.5)

    def _run_watcher_once(self, env=None):
        subprocess.run(
            [sys.executable, str(REPO / "winlinx" / "watcher.py"),
             "--once"],
            env=env if env is not None else self.env, timeout=300,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False)
        time.sleep(0.5)

    def _handled_state(self):
        path = self.layer / "handled.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

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
        # The success is recorded, so the watcher will not copy and
        # sync it again (v0.4 task 1).
        record = self._handled_state()[str(target)]
        self.assertEqual(record["action"], "bridged-vst3")
        self.assertEqual(record["size"], target.stat().st_size)
        self.assertEqual(record["mtime"], target.stat().st_mtime_ns)

    def test_clap_with_working_yabridge_names_clap_folder(self):
        # v0.4 task 7: yabridge 5.1.1 bridges a .clap sitting in
        # the VST3 folder into ~/.clap, verified live with real
        # yabridgectl and a real .clap plug-in, so the message
        # names ~/.clap.
        self._add_fake("yabridgectl", "exit 0")
        target = self.downloads / "ClickFx.clap"
        target.write_bytes(b"CLAPDATA")
        self._run_runner(target)
        notes = self._notifications()
        self.assertIn("ClickFx.clap was bridged into ~/.clap for "
                      "your DAW.", notes)
        self.assertNotIn("could not be bridged", notes)
        record = self._handled_state()[str(target)]
        self.assertEqual(record["action"], "bridged-clap")

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

    def test_exe_with_wine_runs_and_records_handled(self):
        self._add_fake("wine", "echo \"WINE: $*\" >> %s" % self.wine_log)
        target = self.downloads / "clickme.exe"
        target.write_bytes(b"FAKEEXE")
        self._run_runner(target)
        self.assertTrue(self.wine_log.is_file())
        self.assertIn(str(target),
                      self.wine_log.read_text(encoding="utf-8"))
        self.assertEqual(self._failed(), {})
        # The success is recorded in the watcher's format (v0.4
        # task 1).
        record = self._handled_state()[str(target)]
        self.assertEqual(record["action"], "ran")
        self.assertEqual(record["size"], target.stat().st_size)
        self.assertEqual(record["mtime"], target.stat().st_mtime_ns)
        self.assertTrue(record["time"])

    def test_success_clears_earlier_failed_record(self):
        # A file that failed before and now works is not still
        # listed as failed (v0.4 task 1).
        self._add_fake("wine", "echo \"WINE: $*\" >> %s" % self.wine_log)
        target = self.downloads / "clickme.exe"
        target.write_bytes(b"FAKEEXE")
        # First double-click: no wine on PATH yet - remove the fake
        # by running with a PATH that has only notify-send.
        bindir = self.home / "nowine-bin"
        bindir.mkdir()
        shutil.copy2(self.fakebin / "notify-send",
                     bindir / "notify-send")
        env = dict(self.env)
        env["PATH"] = "%s:/usr/bin:/bin" % bindir
        self._run_runner(target, env=env)
        self.assertIn("clickme.exe", self._failed())
        # Wine appears; the second double-click succeeds.
        self._run_runner(target)
        self.assertEqual(self._failed(), {})
        record = self._handled_state()[str(target)]
        self.assertEqual(record["action"], "ran")
        self.assertNotIn("attempts", record)
        self.assertNotIn("reason", record)

    # -- double-click inside a watched folder (v0.4 task 1, R2) ------

    def test_double_click_in_programs_runs_once(self):
        # The v0.3 review's R2: a program double-clicked from inside
        # ~/WinLinx/Programs ran TWICE (runner, then watcher). The
        # runner now records the success, so the watcher pass runs
        # nothing.
        self._add_fake("wine", "echo \"WINE: $*\" >> %s" % self.wine_log)
        programs = self.home / "WinLinx" / "Programs"
        programs.mkdir(parents=True)
        target = programs / "game2.exe"
        target.write_bytes(b"FAKEEXE")
        self._run_runner(target)
        # The record is there immediately after the double-click.
        record = self._handled_state()[str(target)]
        self.assertEqual(record["action"], "ran")
        self.assertEqual(self.wine_log.read_text(
            encoding="utf-8").count("WINE:"), 1)
        self._run_watcher_once()
        self.assertEqual(self.wine_log.read_text(
            encoding="utf-8").count("WINE:"), 1)

    def test_plugin_double_click_in_plugins_syncs_once(self):
        # Same root cause for plug-ins: a .vst3 double-clicked from
        # inside ~/WinLinx/Plugins was re-copied and re-synced by
        # the watcher.
        yab_log = self.home / "yab.log"
        self._add_fake("yabridgectl",
                       "echo \"YAB: $*\" >> %s" % yab_log)
        plugins = self.home / "WinLinx" / "Plugins"
        plugins.mkdir(parents=True)
        target = plugins / "ClickSynth.vst3"
        target.write_bytes(b"VST3DATA")
        self._run_runner(target)
        record = self._handled_state()[str(target)]
        self.assertEqual(record["action"], "bridged-vst3")
        self.assertEqual(yab_log.read_text(
            encoding="utf-8").count("YAB:"), 1)
        self._run_watcher_once()
        self.assertEqual(yab_log.read_text(
            encoding="utf-8").count("YAB:"), 1)

    def test_bundle_double_click_in_plugins_syncs_once(self):
        # A .vst3 bundle folder is recorded with the watcher's
        # directory fingerprint (size 0), or the watcher would see a
        # mismatched record and sync it again.
        yab_log = self.home / "yab.log"
        self._add_fake("yabridgectl",
                       "echo \"YAB: $*\" >> %s" % yab_log)
        plugins = self.home / "WinLinx" / "Plugins"
        plugins.mkdir(parents=True)
        bundle = plugins / "ClickBundle.vst3"
        (bundle / "Contents").mkdir(parents=True)
        (bundle / "Contents" / "Bundle.so").write_bytes(b"BUNDLEBIN")
        self._run_runner(bundle)
        record = self._handled_state()[str(bundle)]
        self.assertEqual(record["action"], "bridged-vst3-dir")
        self.assertEqual(record["size"], 0)
        self.assertEqual(record["mtime"], bundle.stat().st_mtime_ns)
        self.assertEqual(yab_log.read_text(
            encoding="utf-8").count("YAB:"), 1)
        self._run_watcher_once()
        self.assertEqual(yab_log.read_text(
            encoding="utf-8").count("YAB:"), 1)

    # -- notifications prefer notify-send (v0.4 task 9) ---------------

    def test_notify_send_used_when_zenity_fails(self):
        # A zenity that exists but cannot show a dialog used to lose
        # the notification entirely. notify-send is now preferred,
        # so the broken zenity is never even called.
        zen_log = self.home / "zenity.log"
        self._add_fake("zenity",
                       "echo \"ZENITY: $*\" >> %s\nexit 1" % zen_log)
        target = self.downloads / "clickme.exe"
        target.write_bytes(b"FAKEEXE")
        self._run_runner(target)
        self.assertIn("Wine is not installed, so clickme.exe cannot "
                      "run.", self._notifications())
        self.assertFalse(zen_log.is_file())

    def test_zenity_fallback_without_notify_send(self):
        # With no notify-send anywhere on PATH, a working zenity is
        # still used. The PATH here is a bare directory with only
        # the tools the runner needs, so a real notify-send in
        # /usr/bin cannot leak into the test.
        zendir = self.home / "zen-only"
        zendir.mkdir()
        zen_log = self.home / "zenity.log"
        (zendir / "zenity").write_text(
            "#!/bin/bash\n"
            "echo \"ZENITY: $*\" >> %s\n" % zen_log)
        (zendir / "zenity").chmod(0o755)
        for tool in ("python3", "tr", "basename", "date"):
            src = shutil.which(tool)
            self.assertIsNotNone(src, tool)
            os.symlink(src, zendir / tool)
        env = dict(self.env)
        env["PATH"] = str(zendir)
        target = self.downloads / "clickme.exe"
        target.write_bytes(b"FAKEEXE")
        self._run_runner(target, env=env)
        self.assertTrue(zen_log.is_file())
        self.assertIn("Wine is not installed, so clickme.exe cannot "
                      "run.", zen_log.read_text(encoding="utf-8"))
        self.assertFalse(self.notify_log.is_file())

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
        self.assertIn("WinLinx runner 0.4", proc.stdout)


if __name__ == "__main__":
    unittest.main()
