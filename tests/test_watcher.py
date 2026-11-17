"""Tests for winlinx/watcher.py - folder polling, scan gating,
Wine runs, VST bridging. Wine and clamscan are stubbed out; the
executor records every call."""

import os
import subprocess
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

REPO = Path(__file__).resolve().parent.parent


class StubExecutor:
    def __init__(self, sync_result=(True, "")):
        self.detached = []
        self.waited = []
        self.syncs = 0
        self.sync_result = sync_result
        self._lock = threading.Lock()

    def run_detached(self, cmd, env=None):
        with self._lock:
            self.detached.append(list(cmd))

    def run_wait(self, cmd, timeout=None, env=None):
        with self._lock:
            self.waited.append(list(cmd))
        return 0

    def sync_bridges(self):
        with self._lock:
            self.syncs += 1
        return self.sync_result


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

    def _stub_notify(self):
        p = mock.patch.object(watcher.common, "notify")
        started = p.start()
        self._patches.append(p)
        return started

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

    def test_runner_success_visible_to_running_watcher(self):
        # v0.4 task 1, daemon case: this Watcher instance was created
        # BEFORE the double-click. It must still see the runner's
        # record (via the per-pass reload) and not run the file a
        # second time. Drives the real runner with a fake wine.
        self._stub_scan()
        self._stub_wine()
        target = self._drop(self.programs, "game.exe")
        fakebin = self.home / "fakebin"
        fakebin.mkdir()
        wine_log = self.home / "wine.log"
        (fakebin / "wine").write_text(
            "#!/bin/bash\necho \"WINE: $*\" >> %s\n" % wine_log)
        (fakebin / "wine").chmod(0o755)
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["WINLINX_HOME"] = str(self.home)
        env["PATH"] = "%s:/usr/bin:/bin" % fakebin
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        subprocess.run(
            ["/bin/bash",
             str(REPO / "winlinx" / "run-windows-file"), str(target)],
            env=env, timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False)
        time.sleep(0.5)
        self.assertEqual(wine_log.read_text(encoding="utf-8")
                         .count("WINE:"), 1)
        state = watcher._load_state()
        self.assertEqual(state[str(target)]["action"], "ran")
        self._settle()
        self.assertEqual(self.executor.detached, [])

    def test_unrelated_files_do_not_run_but_show_as_failed(self):
        # Unsupported drops are no longer ignored: they never run,
        # but they are logged and recorded as failed so the manager
        # shows them (v0.3 task 6).
        self._stub_scan()
        self._stub_wine()
        self._drop(self.programs, "notes.txt")
        self._drop(self.programs, "photo.jpg")
        self._settle()
        self.assertEqual(self.executor.detached, [])
        self.assertEqual(self.executor.waited, [])
        failed = {entry["name"]: entry
                  for entry in watcher.load_failed()}
        self.assertEqual(set(failed), {"notes.txt", "photo.jpg"})
        self.assertIn("WinLinx handles", failed["notes.txt"]["reason"])

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

    def test_failed_item_recorded_with_reason_not_handled(self):
        # Regression: a program Wine could not run was recorded as
        # "ran" and never retried.
        self._stub_scan()
        self._stub_wine(present=False)
        target = self._drop(self.programs, "game.exe")
        self._settle()
        self.assertEqual(self.executor.detached, [])
        failed = watcher.load_failed()
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["name"], "game.exe")
        self.assertIn("Wine is not installed", failed[0]["reason"])
        self.assertEqual(failed[0]["attempts"], 1)
        state = watcher._load_state()
        self.assertEqual(state[str(target)]["action"], "failed")

    def test_failed_item_not_retried_before_backoff(self):
        self._stub_scan()
        self._stub_wine(present=False)
        target = self._drop(self.programs, "game.exe")
        self._settle()
        self._settle()
        state = watcher._load_state()
        self.assertEqual(state[str(target)]["attempts"], 1)

    def test_failed_item_retried_when_wine_appears(self):
        self._stub_scan()
        wine_here = {"present": False}
        p = mock.patch.object(
            watcher.common, "find_wine",
            side_effect=lambda: ("/usr/bin/wine"
                                 if wine_here["present"] else None))
        p.start()
        self._patches.append(p)
        target = self._drop(self.programs, "game.exe")
        self._settle()
        self.assertEqual(self.executor.detached, [])
        self.assertEqual(len(watcher.load_failed()), 1)
        # Wine appears; force the retry time into the past.
        wine_here["present"] = True
        state = watcher._load_state()
        state[str(target)]["next_retry"] = 0
        watcher._save_state(state)
        self.w = watcher.Watcher(executor=self.executor)
        self.w.scan_once()
        _wait_for(lambda: not self.w._busy, timeout=5.0)
        time.sleep(0.05)
        self.assertEqual(self.executor.detached,
                         [["wine", str(target)]])
        self.assertEqual(watcher.load_failed(), [])

    def test_retry_clears_failed_record(self):
        # clear_handled is what the manager's Retry button calls.
        self._stub_scan()
        self._stub_wine(present=False)
        target = self._drop(self.programs, "game.exe")
        self._settle()
        self.assertEqual(len(watcher.load_failed()), 1)
        watcher.clear_handled(str(target))
        self.assertEqual(watcher.load_failed(), [])

    def test_forget_marks_ignored(self):
        self._stub_scan()
        self._stub_wine(present=False)
        target = self._drop(self.programs, "game.exe")
        self._settle()
        watcher.ignore_path(str(target))
        self.assertEqual(watcher.load_failed(), [])
        state = watcher._load_state()
        self.assertEqual(state[str(target)]["action"], "ignored")
        # A later pass leaves it alone.
        self._settle()
        self.assertEqual(self.executor.detached, [])

    def test_clap_file_copied_and_synced(self):
        self._stub_scan()
        target = self._drop(self.plugins, "Synth.clap", b"CLAPDATA")
        self._settle()
        copied = common.vst3_dir() / "Synth.clap"
        self.assertTrue(copied.exists())
        self.assertEqual(copied.read_bytes(), b"CLAPDATA")
        self.assertEqual(self.executor.syncs, 1)
        self.assertEqual(self.executor.detached, [])

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

    # -- unsupported drops (v0.3 task 6) -----------------------------

    def test_zip_drop_logged_and_recorded_failed(self):
        self._stub_scan()
        target = self._drop(self.plugins, "plugin.zip")
        self._settle()
        failed = watcher.load_failed()
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["name"], "plugin.zip")
        self.assertEqual(failed[0]["reason"],
                         watcher.UNSUPPORTED_ZIP_REASON)
        self.assertIn("Unzip it first", failed[0]["reason"])
        log = common.log_path().read_text(encoding="utf-8")
        self.assertIn("Unsupported file dropped: plugin.zip", log)
        self.assertIn("Unzip it first", log)

    def test_txt_drop_reason_does_not_mention_unzipping(self):
        self._stub_scan()
        self._drop(self.programs, "readme.txt")
        self._settle()
        failed = watcher.load_failed()
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reason"],
                         watcher.UNSUPPORTED_REASON)
        self.assertNotIn("Unzip", failed[0]["reason"])

    def test_txt_drop_reason_says_left_in_place(self):
        # v0.4 task 15: the reason says what happened to the file.
        self._stub_scan()
        self._drop(self.programs, "readme.txt")
        self._settle()
        failed = watcher.load_failed()
        self.assertEqual(len(failed), 1)
        self.assertEqual(
            failed[0]["reason"],
            "This file type isn't handled - it was left in place. "
            "WinLinx handles .exe, .msi, .vst3, .dll and .clap files.")

    def test_unsupported_file_not_scanned(self):
        scan = self._stub_scan()
        self._drop(self.plugins, "plugin.zip")
        self._settle()
        scan.assert_not_called()

    def test_unsupported_file_not_retried_automatically(self):
        self._stub_scan()
        target = self._drop(self.plugins, "plugin.zip")
        self._settle()
        # Even with the retry time forced into the past, a permanent
        # failure is not retried: retrying cannot help.
        state = watcher._load_state()
        self.assertTrue(state[str(target)].get("permanent"))
        state[str(target)]["next_retry"] = 0
        watcher._save_state(state)
        self._settle()
        state = watcher._load_state()
        self.assertEqual(state[str(target)]["attempts"], 1)

    # -- dropped folders (v0.3 tasks 33, 34) -------------------------

    def test_dropped_folder_recorded_as_failed_drop(self):
        self._stub_scan()
        self._stub_wine()
        folder = self.programs / "PortableGame"
        folder.mkdir()
        (folder / "game.exe").write_bytes(b"x")
        self._settle()
        self.assertTrue(folder.exists())
        self.assertEqual(self.executor.detached, [])
        failed = watcher.load_failed()
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["name"], "PortableGame")
        self.assertEqual(failed[0]["reason"], watcher.FOLDER_REASON)
        self.assertIn("double-click", failed[0]["reason"])

    def test_folder_log_says_left_in_place_when_unscanned(self):
        def missing_scan(path, timeout=None):
            return {"clean": None, "threats": [],
                    "error": "ClamAV is not installed", "missing": True}
        p = mock.patch.object(watcher.scanner, "scan_path",
                              side_effect=missing_scan)
        p.start()
        self._patches.append(p)
        folder = self.programs / "RandomFolder"
        folder.mkdir()
        self._settle()
        log = common.log_path().read_text(encoding="utf-8")
        self.assertIn("Folder left in place: RandomFolder", log)
        self.assertIn("UNSCANNED", log)
        self.assertNotIn("Folder scanned clean", log)

    def test_folder_log_says_scanned_clean_when_scan_ran(self):
        self._stub_scan()
        folder = self.programs / "ScannedFolder"
        folder.mkdir()
        self._settle()
        log = common.log_path().read_text(encoding="utf-8")
        self.assertIn("Folder scanned clean, left in place: "
                      "ScannedFolder", log)

    def test_vst3_folder_in_programs_points_at_plugins(self):
        self._stub_scan()
        bundle = self.programs / "Misplaced.vst3"
        bundle.mkdir()
        (bundle / "plugin.bin").write_bytes(b"x")
        self._settle()
        failed = watcher.load_failed()
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reason"],
                         watcher.FOLDER_VST3_IN_PROGRAMS_REASON)

    def test_vst3_folder_in_programs_log_uses_folder_reason(self):
        # v0.4 task 14: the log line for a .vst3 bundle in Programs
        # must not say "double-click the program inside to run it" -
        # there is no program inside a plug-in bundle.
        self._stub_scan()
        bundle = self.programs / "Misplaced.vst3"
        bundle.mkdir()
        (bundle / "plugin.bin").write_bytes(b"x")
        self._settle()
        log = common.log_path().read_text(encoding="utf-8")
        self.assertIn("Folder scanned clean, left in place: "
                      "Misplaced.vst3 (Plug-ins go in the Plugins "
                      "folder", log)
        self.assertNotIn("double-click the program inside", log)

    # -- sync failures (v0.3 tasks 35, 32) ---------------------------

    def test_sync_failure_reason_is_plain_words(self):
        # A failed sync records a plain reason, never the tool name.
        self._stub_scan()
        self.executor = StubExecutor(
            sync_result=(False, "yabridgectl not found"))
        self.w = watcher.Watcher(executor=self.executor)
        self._drop(self.plugins, "Chorus.vst3", b"VST3DATA")
        self._settle()
        failed = watcher.load_failed()
        self.assertEqual(len(failed), 1)
        self.assertNotIn("yabridgectl", failed[0]["reason"])
        # Same wording the manager shows for this failure (the shared
        # plain_drop_reason mapper): watcher, manager, and runner
        # all say the same thing.
        self.assertEqual(
            failed[0]["reason"],
            "yabridge is not installed, so the plug-in could not be "
            "bridged")
        log = common.log_path().read_text(encoding="utf-8")
        self.assertNotIn("yabridgectl", log)

    def test_sync_failure_logged_once_in_plain_words(self):
        # v0.4 task 4: a failed sync is logged once, in plain words.
        # The log line comes from common.run_yabridgectl_sync itself
        # (real executor, no yabridge); the watcher adds no second
        # line. The Failed Drops reason stays the watcher's wording.
        self._stub_scan()
        self.w = watcher.Watcher(executor=watcher.Executor())
        p = mock.patch.object(watcher.common, "find_yabridgectl",
                              return_value=None)
        p.start()
        self._patches.append(p)
        self._drop(self.plugins, "Chorus.vst3", b"VST3DATA")
        self._settle()
        log = common.log_path().read_text(encoding="utf-8")
        self.assertEqual(
            log.count("yabridge is not installed, so plug-ins can't "
                      "be bridged"), 1)
        self.assertNotIn("yabridgectl", log)
        self.assertNotIn("bridge sync failed", log)
        failed = watcher.load_failed()
        self.assertEqual(len(failed), 1)
        self.assertEqual(
            failed[0]["reason"],
            "yabridge is not installed, so the plug-in could not be "
            "bridged")

    def test_installer_sync_failure_notifies_once(self):
        # An installer .exe in Plugins that installs fine but fails
        # the sync is marked installed (never re-run) AND the user is
        # told once, with what to do (v0.3 task 35).
        notify = self._stub_notify()
        self._stub_scan()
        self._stub_wine()
        self.executor = StubExecutor(
            sync_result=(False, "yabridgectl not found"))
        self.w = watcher.Watcher(executor=self.executor)
        target = self._drop(self.plugins, "synth-setup.exe")
        self._settle()
        self.assertEqual(self.executor.waited, [["wine", str(target)]])
        state = watcher._load_state()
        self.assertEqual(state[str(target)]["action"], "installed")
        self.assertEqual(watcher.load_failed(), [])
        notify.assert_called_once()
        message = notify.call_args[0][1]
        self.assertIn("synth-setup.exe installed, but the plug-in "
                      "folders could not be re-synced", message)
        self.assertIn("Re-sync Bridges", message)
        self.assertNotIn("yabridgectl", message)
        # A later pass does not notify again.
        self._settle()
        notify.assert_called_once()

    # -- batched failure notifications (v0.3 task 32) ----------------

    def test_same_reason_failures_coalesce(self):
        # v0.4 task 8: three or fewer files sharing a reason are
        # named instead of counted.
        notify = self._stub_notify()
        self._stub_scan()
        self._stub_wine(present=False)
        self._drop(self.programs, "one.exe")
        self._drop(self.programs, "two.exe")
        self._drop(self.programs, "three.exe")
        self._settle()
        self.w._flush_failure_notifications()
        notify.assert_called_once_with(
            "WinLinx",
            "one.exe, three.exe and two.exe could not be handled: "
            "Wine is not installed - see the WinLinx Program "
            "Manager.")

    def test_two_failures_name_both_files(self):
        notify = self._stub_notify()
        self._stub_scan()
        self._stub_wine(present=False)
        self._drop(self.programs, "one.exe")
        self._drop(self.programs, "two.exe")
        self._settle()
        self.w._flush_failure_notifications()
        notify.assert_called_once_with(
            "WinLinx",
            "one.exe and two.exe could not be handled: Wine is not "
            "installed - see the WinLinx Program Manager.")

    def test_four_failures_use_count(self):
        notify = self._stub_notify()
        self._stub_scan()
        self._stub_wine(present=False)
        for name in ("a.exe", "b.exe", "c.exe", "d.exe"):
            self._drop(self.programs, name)
        self._settle()
        self.w._flush_failure_notifications()
        notify.assert_called_once_with(
            "WinLinx",
            "4 files could not be handled: Wine is not installed - "
            "see the WinLinx Program Manager.")

    def test_single_failure_notification_names_the_file(self):
        notify = self._stub_notify()
        self._stub_scan()
        self._stub_wine(present=False)
        self._drop(self.programs, "game.exe")
        self._settle()
        self.w._flush_failure_notifications()
        notify.assert_called_once_with(
            "WinLinx",
            "game.exe could not be handled: Wine is not installed - "
            "see the WinLinx Program Manager.")

    def test_different_reasons_notify_once_as_summary(self):
        # v0.4 task 8: one summary notification per drop when many
        # reasons fire at once, one line per reason.
        notify = self._stub_notify()
        self._stub_scan()
        self._stub_wine(present=False)
        self._drop(self.programs, "game.exe")
        self._drop(self.plugins, "plugin.zip")
        self._settle()
        self.w._flush_failure_notifications()
        notify.assert_called_once()
        message = notify.call_args[0][1]
        self.assertIn("game.exe could not be handled: Wine is not "
                      "installed", message)
        self.assertIn("plugin.zip could not be handled: Unzip it "
                      "first", message)
        self.assertIn("See the WinLinx Program Manager.", message)

    def test_next_scan_flushes_failure_notifications(self):
        notify = self._stub_notify()
        self._stub_scan()
        self._stub_wine(present=False)
        self._drop(self.programs, "game.exe")
        self._settle()
        # Held back for batching: nothing sent yet.
        notify.assert_not_called()
        # The next scan pass sends it.
        self.w.scan_once()
        notify.assert_called_once()
        self.assertIn("game.exe could not be handled",
                      notify.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
