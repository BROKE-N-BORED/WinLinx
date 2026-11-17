"""Tests for winlinx/common.py — prefix scanning, registry parsing,
path conversion, plugin listing. Fake home via WINLINX_HOME."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "winlinx")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import fakehome  # noqa: E402


class CommonTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WINLINX_HOME")
        os.environ["WINLINX_HOME"] = self._tmp.name
        self.home = Path(self._tmp.name)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WINLINX_HOME", None)
        else:
            os.environ["WINLINX_HOME"] = self._old_home
        self._tmp.cleanup()


class WinPathTests(CommonTestBase):
    def test_win_to_unix(self):
        fakehome.build_fake_home(self.home)
        got = common.win_to_unix(r"C:\Program Files\FooApp\foo.exe")
        want = (self.home / ".wine" / "drive_c" / "Program Files"
                / "FooApp" / "foo.exe")
        self.assertEqual(got, want)

    def test_win_to_unix_unknown_drive(self):
        fakehome.build_fake_home(self.home)
        self.assertIsNone(common.win_to_unix(r"D:\Games\game.exe"))

    def test_win_to_unix_garbage(self):
        self.assertIsNone(common.win_to_unix("not a path"))
        self.assertIsNone(common.win_to_unix(""))
        self.assertIsNone(common.win_to_unix(None))


class RegistryTests(CommonTestBase):
    def test_parse_system_reg(self):
        fakehome.build_fake_home(self.home)
        sections = common.parse_system_reg(
            self.home / ".wine" / "system.reg")
        key = ("Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
               "\\FooApp")
        self.assertIn(key, sections)
        self.assertEqual(sections[key]["DisplayName"], "FooApp 2.0")
        self.assertEqual(sections[key]["EstimatedSize"], 1024)
        self.assertEqual(sections[key]["InstallDate"], "20260115")

    def test_parse_missing_reg(self):
        self.assertEqual(common.parse_system_reg(
            self.home / ".wine" / "system.reg"), {})


class ScanProgramsTests(CommonTestBase):
    def test_registry_programs(self):
        fakehome.build_fake_home(self.home)
        programs = common.scan_programs()
        names = [p["name"] for p in programs]
        self.assertEqual(names, ["FooApp 2.0", "MSI Thing"])
        # Hidden Runtime has SystemComponent set: must be filtered out.
        self.assertNotIn("Hidden Runtime", names)
        foo = programs[0]
        self.assertEqual(foo["install_date"], "2026-01-15")
        self.assertEqual(foo["size_bytes"], 1024 * 1024)
        self.assertTrue(foo["exe"].endswith("foo.exe"))
        self.assertIn("FooApp", foo["location"])
        msi = programs[1]
        self.assertEqual(msi["install_date"], "2026-02-01")
        self.assertEqual(msi["size_bytes"], 2048 * 1024)

    def test_uninstall_command_msi(self):
        fakehome.build_fake_home(self.home)
        programs = common.scan_programs()
        msi = [p for p in programs if p["name"] == "MSI Thing"][0]
        self.assertEqual(
            common.uninstall_command(msi),
            ["wine", "uninstaller", "--remove",
             "{12345678-1234-1234-1234-123456789012}"])

    def test_uninstall_command_exe(self):
        fakehome.build_fake_home(self.home)
        programs = common.scan_programs()
        foo = [p for p in programs if p["name"] == "FooApp 2.0"][0]
        cmd = common.uninstall_command(foo)
        self.assertEqual(cmd[0], "wine")
        self.assertTrue(cmd[1].endswith("uninstall.exe"))

    def test_uninstall_command_none(self):
        self.assertIsNone(common.uninstall_command({"uninstall": ""}))

    def test_fallback_folder_scan(self):
        fakehome.build_fake_home(self.home, with_reg=False,
                                 with_desktops=False)
        programs = common.scan_programs()
        names = sorted(p["name"] for p in programs)
        self.assertEqual(names, ["FooApp", "OldApp"])
        foo = [p for p in programs if p["name"] == "FooApp"][0]
        self.assertTrue(foo["exe"].endswith("foo.exe"))

    def test_folder_fallback_merges_with_registry(self):
        # A portable folder shows up even when registry programs
        # exist, and a folder that belongs to a registry program is
        # never listed twice.
        fakehome.build_fake_home(self.home)
        portable = (self.home / ".wine" / "drive_c" / "Program Files"
                    / "PortableThing")
        portable.mkdir()
        (portable / "thing.exe").write_bytes(b"X" * 100)
        programs = common.scan_programs()
        names = [p["name"] for p in programs]
        self.assertEqual(names, ["FooApp 2.0", "MSI Thing",
                                 "PortableThing"])
        thing = [p for p in programs
                 if p["name"] == "PortableThing"][0]
        self.assertTrue(thing["exe"].endswith("thing.exe"))

    def test_pick_exe_skips_uninstallers(self):
        folder = self.home / "SomeApp"
        folder.mkdir()
        (folder / "uninstall.exe").write_bytes(b"X" * 10000)
        (folder / "updater.exe").write_bytes(b"X" * 5000)
        (folder / "SomeApp.exe").write_bytes(b"X" * 100)
        self.assertTrue(
            common._pick_exe(folder).endswith("SomeApp.exe"))
        # Only uninstallers: no launch beats launching an uninstaller.
        folder2 = self.home / "OnlyUninstaller"
        folder2.mkdir()
        (folder2 / "uninstall.exe").write_bytes(b"X" * 100)
        self.assertIsNone(common._pick_exe(folder2))

    def test_uninstall_command_msi_no_guid(self):
        # Registry key is the GUID: still uninstallable.
        prog = {"uninstall": "MsiExec.exe /X",
                "key": "{12345678-1234-1234-1234-123456789012}"}
        self.assertEqual(
            common.uninstall_command(prog),
            ["wine", "uninstaller", "--remove",
             "{12345678-1234-1234-1234-123456789012}"])
        # msiexec naming an .msi file: run msiexec /x on it.
        fakehome.build_fake_home(self.home)
        prog = {"uninstall": 'MsiExec.exe /X "C:\\Setup\\thing.msi"',
                "key": "Thing"}
        cmd = common.uninstall_command(prog)
        self.assertEqual(cmd[0:2], ["wine", "msiexec"])
        self.assertEqual(cmd[2], "/x")
        self.assertTrue(cmd[3].endswith("thing.msi"))
        # msiexec with nothing usable: open Wine's uninstaller list.
        prog = {"uninstall": "MsiExec.exe", "key": "Thing"}
        self.assertEqual(common.uninstall_command(prog),
                         ["wine", "uninstaller"])

    def test_no_prefix_no_programs(self):
        self.assertEqual(common.scan_programs(), [])


class ScanPluginsTests(CommonTestBase):
    def test_plugin_statuses(self):
        fakehome.build_fake_home(self.home)
        plugins = common.scan_plugins()
        by_name = {p["name"]: p for p in plugins}
        self.assertEqual(by_name["Remix.vst3"]["status"], "Bridged")
        self.assertEqual(by_name["Remix.vst3"]["kind"], "VST3")
        self.assertEqual(by_name["Broken.vst3"]["status"],
                         "Source missing")
        self.assertEqual(by_name["Lonely.vst3"]["status"], "Not bridged")

    def test_no_plugins(self):
        self.assertEqual(common.scan_plugins(), [])


class VersionTests(CommonTestBase):
    def test_version_is_single_sourced(self):
        self.assertEqual(common.__version__, "0.3")

    def test_parse_version(self):
        self.assertEqual(common._parse_version("v0.2"), (0, 2))
        self.assertEqual(common._parse_version("0.2.1"), (0, 2, 1))
        self.assertEqual(common._parse_version("garbage"), (0,))
        self.assertGreater(common._parse_version("0.3"),
                           common._parse_version("0.2"))


class PrefixMapTests(CommonTestBase):
    def test_prefix_map_round_trip(self):
        fakehome.build_fake_home(self.home)
        prefix = self.home / ".local" / "share" / "winlinx" / "prefixes"
        prefix.mkdir(parents=True)
        own = prefix / "game.exe"
        own.mkdir()
        self.assertIsNone(common.prefix_for_exe("game.exe"))
        common.set_program_prefix("C:\\Games\\game.exe", own)
        self.assertEqual(common.prefix_for_exe("GAME.EXE"), str(own))
        self.assertEqual(common.effective_prefix("game.exe"), own)
        self.assertEqual(common.effective_prefix("other.exe"),
                         common.wine_prefix())
        common.set_program_prefix("game.exe", None)
        self.assertIsNone(common.prefix_for_exe("game.exe"))

    def test_prefix_for_exe_ignores_missing_dir(self):
        common.set_program_prefix(
            "game.exe", self.home / "no-such-prefix")
        self.assertIsNone(common.prefix_for_exe("game.exe"))


class BackupRestoreTests(CommonTestBase):
    def test_backup_and_restore_round_trip(self):
        fakehome.build_fake_home(self.home)
        marker = (self.home / ".wine" / "drive_c" / "Program Files"
                  / "FooApp" / "foo.exe")
        dest = self.home / "backup.tar.gz"
        ok, _msg = common.backup_prefixes(dest)
        self.assertTrue(ok)
        self.assertTrue(dest.is_file())
        # Wipe the prefix, restore, and the files come back.
        import shutil
        shutil.rmtree(self.home / ".wine")
        self.assertFalse(marker.exists())
        ok, _msg = common.restore_prefixes(dest)
        self.assertTrue(ok)
        self.assertTrue(marker.exists())

    def test_restore_moves_old_prefix_aside(self):
        fakehome.build_fake_home(self.home)
        dest = self.home / "backup.tar.gz"
        common.backup_prefixes(dest)
        ok, _msg = common.restore_prefixes(dest)
        self.assertTrue(ok)
        aside = [p for p in self.home.iterdir()
                 if p.name.startswith(".wine.before-restore-")]
        self.assertEqual(len(aside), 1)

    def test_restore_rejects_foreign_entries(self):
        import tarfile
        dest = self.home / "evil.tar.gz"
        with tarfile.open(dest, "w:gz") as tf:
            info = tarfile.TarInfo("etc/passwd")
            info.size = 0
            import io
            tf.addfile(info, io.BytesIO(b""))
        ok, msg = common.restore_prefixes(dest)
        self.assertFalse(ok)
        self.assertIn("unexpected entry", msg)


class DebugLogTests(CommonTestBase):
    def test_append_log_and_tail(self):
        common.append_log("hello world")
        common.append_log("second line")
        tail = common.read_log_tail(10)
        self.assertIn("hello world", tail)
        self.assertIn("second line", tail)
        self.assertIn("hello world", common.RING.text())

    def test_log_rotation(self):
        old_max = common.LOG_MAX_BYTES
        common.LOG_MAX_BYTES = 100
        try:
            for i in range(20):
                common.append_log("line %d padding padding" % i)
            rotated = common.log_path().with_name("layer.log.1")
            self.assertTrue(rotated.is_file())
        finally:
            common.LOG_MAX_BYTES = old_max

    def test_debug_report_is_scrubbed(self):
        common.append_log("started with api_key=SHOULDNOTAPPEAR")
        report = common.write_debug_report()
        self.assertIsNotNone(report)
        self.assertTrue(report.is_file())
        text = report.read_text(encoding="utf-8")
        self.assertIn("WinLinx version: 0.3", text)
        self.assertNotIn("SHOULDNOTAPPEAR", text)
        self.assertNotIn(str(self.home), text)
        self.assertIn("~", text)


class ProcessTests(CommonTestBase):
    def test_is_running_false_for_nonsense(self):
        self.assertFalse(
            common.is_running("/nope/definitely-not-a-process-xyz.exe"))

    def test_is_running_true_for_this_process(self):
        import shutil
        import sys
        if not shutil.which("pgrep"):
            self.skipTest("pgrep not installed")
        exe = sys.executable
        self.assertTrue(common.is_running(exe))

    def test_notify_never_raises(self):
        self.assertIn(common.notify("WinLinx", "test"), (True, False))


class FormatTests(CommonTestBase):
    def test_format_size(self):
        self.assertEqual(common.format_size(None), "")
        self.assertEqual(common.format_size(0), "0 B")
        self.assertEqual(common.format_size(512), "512 B")
        self.assertEqual(common.format_size(2048), "2.0 KB")
        self.assertEqual(common.format_size(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(common.format_size(3 * 1024 ** 3), "3.0 GB")

    def test_format_install_date(self):
        self.assertEqual(common.format_install_date("20260115"),
                         "2026-01-15")
        self.assertEqual(common.format_install_date(""), "")
        self.assertEqual(common.format_install_date("garbage"), "garbage")

    def test_dir_size(self):
        d = self.home / "sized"
        d.mkdir()
        (d / "a.bin").write_bytes(b"x" * 100)
        (d / "b.bin").write_bytes(b"y" * 250)
        self.assertEqual(common.dir_size(d), 350)
        self.assertEqual(common.dir_size(self.home / "missing"), 0)


class SyncPlainWordsTests(CommonTestBase):
    """Task 5: the bridge-sync failure reason is plain words for
    the user; the tool name lives in the log only."""

    def test_sync_missing_yabridge_is_plain_words(self):
        import unittest.mock as mock
        with mock.patch.object(common, "find_yabridgectl",
                               return_value=None):
            ok, message = common.run_yabridgectl_sync()
        self.assertFalse(ok)
        self.assertNotIn("yabridgectl", message)
        self.assertIn("yabridge is not installed", message)
        self.assertIn("can't be bridged", message)

    def test_sync_failure_logs_tool_name(self):
        import unittest.mock as mock
        with mock.patch.object(common, "find_yabridgectl",
                               return_value=None):
            common.run_yabridgectl_sync()
        self.assertIn("yabridgectl",
                      common.read_log_tail(10))

    def test_plain_drop_reason(self):
        self.assertEqual(
            common.plain_drop_reason(
                "bridge sync failed (yabridgectl not found)"),
            "yabridge is not installed, so the plug-in could not "
            "be bridged")
        self.assertEqual(
            common.plain_drop_reason("bridge sync failed (timeout)"),
            "the plug-in folders could not be re-synced")
        # Unknown reasons pass through untouched.
        self.assertEqual(
            common.plain_drop_reason("Wine is not installed"),
            "Wine is not installed")


class KillProgramTests(CommonTestBase):
    """Task 10: Kill must not claim success when nothing ran."""

    def test_kill_not_running_says_so(self):
        ok, message = common.kill_program(
            "/tmp/definitely-not-a-real-process-xyz.exe")
        self.assertFalse(ok)
        self.assertIn("is not running", message)

    def test_kill_no_program(self):
        ok, _message = common.kill_program("")
        self.assertFalse(ok)


class WindowsVersionOrderTests(CommonTestBase):
    """Task 41: newest first."""

    def test_versions_newest_first(self):
        self.assertEqual(common.WINDOWS_VERSIONS,
                         ("win11", "win10", "win8", "win7", "winxp"))


class DroppedPluginTests(CommonTestBase):
    """Task 21: scan_plugins records the dropped file."""

    def test_dropped_file_recorded(self):
        fakehome.build_fake_home(self.home)
        dropped = self.home / "WinLinx" / "Plugins" / "Remix.vst3"
        dropped.write_bytes(b"FAKEVST3")
        plugins = common.scan_plugins()
        by_name = {p["name"]: p for p in plugins}
        self.assertEqual(by_name["Remix.vst3"]["dropped"], str(dropped))
        self.assertEqual(by_name["Lonely.vst3"]["dropped"], "")


class DebugReportExtraTests(CommonTestBase):
    """Task 43: watcher status, counts, system info."""

    def test_report_has_watcher_counts_and_system(self):
        fakehome.build_fake_home(self.home)
        text = common.debug_report_text()
        self.assertIn("Watcher service:", text)
        self.assertIn("Quarantined items: 0", text)
        self.assertIn("Failed drops: 0", text)
        self.assertIn("System:", text)
        self.assertIn("Memory:", text)

    def test_system_info_reads_os_and_ram(self):
        os_name, ram = common.system_info()
        self.assertTrue(os_name)
        self.assertTrue(ram)
        self.assertIn("B", ram)


class BackupProgressBytesTests(CommonTestBase):
    """Task 44: backup progress reports by bytes too."""

    def test_progress_reports_by_bytes(self):
        fakehome.build_fake_home(self.home)
        big_dir = (self.home / ".wine" / "drive_c" / "Program Files"
                   / "FooApp")
        for i in range(3):
            (big_dir / ("big%d.bin" % i)).write_bytes(b"X" * 1024 * 1024)
        calls = []

        def progress(done_files, done_bytes):
            calls.append((done_files, done_bytes))
            return True

        dest = self.home / "backup.tar.gz"
        ok, _msg = common.backup_prefixes(dest, progress=progress)
        self.assertTrue(ok)
        # A few huge files must still produce several progress
        # reports (the old code reported only every 50 files).
        self.assertGreaterEqual(len(calls), 3)
        seen_bytes = [b for _f, b in calls]
        self.assertEqual(seen_bytes, sorted(seen_bytes))
        self.assertGreater(seen_bytes[-1], 3 * 1024 * 1024)


class AsidePrefixTests(CommonTestBase):
    """Task 37: restore names the aside copy and keeps only the
    newest one."""

    def test_restore_names_aside_location(self):
        fakehome.build_fake_home(self.home)
        dest = self.home / "backup.tar.gz"
        common.backup_prefixes(dest)
        ok, message = common.restore_prefixes(dest)
        self.assertTrue(ok)
        self.assertIn(".before-restore-", message)

    def test_restore_prunes_old_aside_copies(self):
        fakehome.build_fake_home(self.home)
        dest = self.home / "backup.tar.gz"
        common.backup_prefixes(dest)
        old1 = self.home / ".wine.before-restore-20200101-000000"
        old2 = self.home / ".wine.before-restore-20210101-000000"
        old1.mkdir()
        (old1 / "marker.txt").write_text("old")
        old2.mkdir()
        (old2 / "marker.txt").write_text("old")
        ok, _message = common.restore_prefixes(dest)
        self.assertTrue(ok)
        self.assertFalse(old1.exists())
        self.assertFalse(old2.exists())
        aside = common.list_aside_prefixes()
        self.assertEqual(len(aside), 1)

    def test_prune_keeps_latest_per_prefix(self):
        root = self.home / ".local" / "share" / "winlinx" / "prefixes"
        root.mkdir(parents=True)
        wine_old = self.home / ".wine.before-restore-20200101-000000"
        wine_new = self.home / ".wine.before-restore-20210101-000000"
        pref_old = root.parent / "prefixes.before-restore-20200101-000000"
        pref_new = root.parent / "prefixes.before-restore-20210101-000000"
        for d in (wine_old, wine_new, pref_old, pref_new):
            d.mkdir()
        removed = common.prune_aside_prefixes(keep_latest=1)
        self.assertEqual(len(removed), 2)
        self.assertTrue(wine_new.is_dir())
        self.assertTrue(pref_new.is_dir())
        self.assertFalse(wine_old.exists())
        self.assertFalse(pref_old.exists())


if __name__ == "__main__":
    unittest.main()
