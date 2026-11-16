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
        self.assertEqual(common.__version__, "0.2.1")

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
        self.assertIn("WinLinx version: 0.2.1", text)
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


if __name__ == "__main__":
    unittest.main()
