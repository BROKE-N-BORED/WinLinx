"""Tests for winlinx/scanner.py — ClamAV gate, quarantine, allowlist.
Uses a fake `clamscan` shell script on PATH (the sandbox has no real
ClamAV); real-clamscan behaviour was verified against the man pages,
not executed here."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "winlinx")))

import scanner  # noqa: E402

FAKE_CLAMSCAN = """#!/bin/sh
# Fake clamscan: flags come first, target is the last argument.
if [ "$1" = "--version" ]; then
  echo "ClamAV 1.5.3/28062/Thu Jul 16 02:25:13 2026"
  exit 0
fi
for last; do :; done
case "$last" in
  *infected*) echo "$last: Eicar-Test-Signature FOUND"; exit 1;;
  *error*) echo "fake scan error" >&2; exit 2;;
esac
if [ -d "$last" ]; then
  found=""
  for f in $(find "$last" -type f 2>/dev/null); do
    case "$f" in
      *infected*) echo "$f: Eicar-Test-Signature FOUND"; found=1;;
    esac
  done
  [ -n "$found" ] && exit 1
fi
exit 0
"""


class ScannerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WINLINX_HOME")
        self._old_path = os.environ.get("PATH", "")
        os.environ["WINLINX_HOME"] = self._tmp.name
        self.home = Path(self._tmp.name)
        self.bindir = self.home / "bin"
        self.bindir.mkdir()
        self._install_fake_clamscan()

    def _install_fake_clamscan(self):
        script = self.bindir / "clamscan"
        script.write_text(FAKE_CLAMSCAN, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        os.environ["PATH"] = str(self.bindir) + os.pathsep + self._old_path

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WINLINX_HOME", None)
        else:
            os.environ["WINLINX_HOME"] = self._old_home
        os.environ["PATH"] = self._old_path
        self._tmp.cleanup()


class ScanTests(ScannerTestBase):
    def test_clean_file(self):
        target = self.home / "clean.exe"
        target.write_bytes(b"hello")
        result = scanner.scan_path(target)
        self.assertTrue(result["clean"])
        self.assertEqual(result["threats"], [])

    def test_infected_file(self):
        target = self.home / "infected.exe"
        target.write_bytes(b"hello")
        result = scanner.scan_path(target)
        self.assertFalse(result["clean"])
        self.assertEqual(result["threats"],
                         [(str(target), "Eicar-Test-Signature")])

    def test_infected_folder_recursive(self):
        folder = self.home / "bundle"
        folder.mkdir()
        (folder / "good.dll").write_bytes(b"x")
        bad = folder / "infected.dll"
        bad.write_bytes(b"x")
        result = scanner.scan_path(folder)
        self.assertFalse(result["clean"])
        self.assertEqual(result["threats"],
                         [(str(bad), "Eicar-Test-Signature")])

    def test_scan_error(self):
        target = self.home / "error.exe"
        target.write_bytes(b"x")
        result = scanner.scan_path(target)
        self.assertIsNone(result["clean"])
        self.assertIn("error", result["error"].lower())

    def test_missing_clamscan(self):
        os.environ["PATH"] = str(self.home)  # no clamscan here
        result = scanner.scan_path(self.home / "clean.exe")
        self.assertTrue(result["missing"])
        self.assertIsNone(result["clean"])

    def test_scanner_status(self):
        status = scanner.scanner_status()
        self.assertTrue(status["installed"])
        self.assertIn("ClamAV 1.5.3", status["version"])
        self.assertEqual(status["sig_version"], "28062")
        self.assertIn("Jul 16", status["sig_date"])


class QuarantineTests(ScannerTestBase):
    def test_quarantine_and_delete(self):
        target = self.home / "infected.exe"
        target.write_bytes(b"x")
        entry = scanner.quarantine_item(
            target, [(str(target), "Eicar-Test-Signature")])
        self.assertFalse(target.exists())
        self.assertTrue(Path(entry["quarantined"]).exists())
        self.assertEqual(entry["threats"], ["Eicar-Test-Signature"])
        self.assertEqual(entry["original"], str(target))
        self.assertEqual(len(scanner.load_quarantine()), 1)
        self.assertTrue(scanner.delete_quarantined(entry["id"]))
        self.assertFalse(Path(entry["quarantined"]).exists())
        self.assertEqual(scanner.load_quarantine(), [])

    def test_quarantine_name_collision(self):
        first = self.home / "dup.exe"
        first.write_bytes(b"1")
        scanner.quarantine_item(first, [(str(first), "T")])
        second = self.home / "dup.exe"
        second.write_bytes(b"2")
        entry = scanner.quarantine_item(second, [(str(second), "T")])
        self.assertNotEqual(entry["quarantined"],
                            str(scanner.quarantine_dir() / "dup.exe"))
        self.assertEqual(len(scanner.load_quarantine()), 2)

    def test_quarantine_id_unique_same_name_same_second(self):
        # Regression: ids used to be timestamp-second + file name, so
        # two same-name files quarantined in the same second shared
        # an id - and deleting one deleted both.
        first = self.home / "a" / "nasty.exe"
        first.parent.mkdir()
        first.write_bytes(b"1")
        entry1 = scanner.quarantine_item(
            first, [(str(first), "T")])
        second = self.home / "b" / "nasty.exe"
        second.parent.mkdir()
        second.write_bytes(b"2")
        entry2 = scanner.quarantine_item(
            second, [(str(second), "T")])
        self.assertNotEqual(entry1["id"], entry2["id"])
        self.assertEqual(len(scanner.load_quarantine()), 2)
        # Deleting one leaves the other (entry and file) untouched.
        self.assertTrue(scanner.delete_quarantined(entry1["id"]))
        remaining = scanner.load_quarantine()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], entry2["id"])
        self.assertTrue(Path(entry2["quarantined"]).exists())
        self.assertFalse(Path(entry1["quarantined"]).exists())

    def test_allow_one_of_two_same_name_items(self):
        first = self.home / "a" / "dup.exe"
        first.parent.mkdir()
        first.write_bytes(b"1")
        entry1 = scanner.quarantine_item(
            first, [(str(first), "T")])
        second = self.home / "b" / "dup.exe"
        second.parent.mkdir()
        second.write_bytes(b"2")
        entry2 = scanner.quarantine_item(
            second, [(str(second), "T")])
        restored = scanner.allow_item(entry1)
        self.assertEqual(restored, first)
        remaining = scanner.load_quarantine()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], entry2["id"])
        self.assertTrue(Path(entry2["quarantined"]).exists())

    def test_allowlist_flow(self):
        target = self.home / "infected.exe"
        target.write_bytes(b"content")
        entry = scanner.quarantine_item(
            target, [(str(target), "Eicar-Test-Signature")])
        self.assertFalse(scanner.is_allowlisted(
            Path(entry["quarantined"])))
        restored = scanner.allow_item(entry)
        self.assertEqual(restored, target)
        self.assertTrue(target.exists())
        self.assertTrue(scanner.is_allowlisted(target))
        self.assertEqual(scanner.load_quarantine(), [])
        # Change the file: the hash no longer matches, scan again.
        target.write_bytes(b"different content")
        self.assertFalse(scanner.is_allowlisted(target))

    def test_allow_folder(self):
        folder = self.home / "badbundle"
        folder.mkdir()
        (folder / "a.exe").write_bytes(b"x")
        entry = scanner.quarantine_item(
            folder, [(str(folder / "a.exe"), "T")])
        restored = scanner.allow_item(entry)
        self.assertEqual(restored, folder)
        self.assertTrue(scanner.is_allowlisted(folder))

    def test_gate_cli(self):
        clean = self.home / "clean.exe"
        clean.write_bytes(b"x")
        self.assertEqual(scanner.gate_cli(clean), 0)
        bad = self.home / "infected.exe"
        bad.write_bytes(b"x")
        self.assertEqual(scanner.gate_cli(bad), 1)
        self.assertFalse(bad.exists())
        self.assertEqual(len(scanner.load_quarantine()), 1)
        os.environ["PATH"] = str(self.home)
        self.assertEqual(scanner.gate_cli(clean), 2)


if __name__ == "__main__":
    unittest.main()
