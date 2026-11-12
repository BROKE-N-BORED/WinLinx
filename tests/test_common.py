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
