"""Tests for build_zip.py and source hygiene.

The release zip must be reproducible from the repo alone (missing
icon PNGs are rendered from the SVG), with correct Unix modes, and
exec bits surviving a fresh extraction. Shipped files must carry no
em-dashes, no REAPER, no "seamless", and no secrets.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_zip  # noqa: E402


class BuildZipTests(unittest.TestCase):
    def _copy_tree_without_pngs(self, dest):
        shutil.copytree(ROOT, dest, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.zip", "*.pyc"))
        for png in (dest / "winlinx" / "icons").glob("*.png"):
            png.unlink()

    def test_build_from_clean_clone_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "winlinx"
            self._copy_tree_without_pngs(tree)
            self.assertFalse((tree / "winlinx" / "icons"
                              / "winlinx-256.png").exists())
            env = dict(os.environ)
            env["QT_QPA_PLATFORM"] = "offscreen"
            proc = subprocess.run(
                [sys.executable, "build_zip.py"], cwd=str(tree),
                capture_output=True, text=True, env=env, timeout=300)
            self.assertEqual(proc.returncode, 0,
                             proc.stdout + proc.stderr)
            zip_path = tree / "winlinx-v0.2.zip"
            self.assertTrue(zip_path.is_file())
            # The PNGs were rendered as part of the build.
            for size in (256, 128, 48):
                self.assertTrue(
                    (tree / "winlinx" / "icons"
                     / ("winlinx-%d.png" % size)).is_file())
            # Modes: scripts/.desktop 0o100755, everything else
            # 0o100644, with full file-type bits.
            with zipfile.ZipFile(zip_path) as zf:
                for info in zf.infolist():
                    mode = info.external_attr >> 16
                    name = info.filename.split("/", 1)[1]
                    if name in build_zip.EXECUTABLES:
                        self.assertEqual(mode, 0o100755, name)
                    else:
                        self.assertEqual(mode, 0o100644, name)
            # Exec bits survive a fresh extraction (via unzip, the
            # same extractor a file manager uses; Python's zipfile
            # does not restore Unix modes).
            if shutil.which("unzip") is None:
                self.skipTest("unzip not installed")
            out = Path(tmp) / "extracted"
            out.mkdir()
            subprocess.run(["unzip", "-q", str(zip_path)],
                           cwd=str(out), check=True, timeout=120)
            for name in ("install.sh", "Install WinLinx.desktop",
                         "winlinx/run-windows-file"):
                path = out / "winlinx-v0.2" / name
                self.assertTrue(os.access(path, os.X_OK), name)


class SourceHygieneTests(unittest.TestCase):
    def _shipped_files(self):
        # What goes to Joshua's PC: the zip contents plus the
        # build tooling. Tests are not shipped, so they are out of
        # scope here (and the hygiene tests name the very words they
        # check for).
        files = [ROOT / rel for rel in build_zip.INCLUDE]
        files.append(ROOT / "build_zip.py")
        files.append(ROOT / "smoke_installer.py")
        files.append(ROOT / "screenshot_manager.py")
        files.append(ROOT / ".github" / "workflows" / "tests.yml")
        return [p for p in files if p.is_file()]

    def test_no_em_dash_in_shipped_files(self):
        bad = []
        for path in self._shipped_files():
            if path.suffix == ".png":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "—" in text:
                bad.append(str(path.relative_to(ROOT)))
        self.assertEqual(bad, [])

    def test_no_reaper_or_seamless_in_shipped_files(self):
        bad = []
        for path in self._shipped_files():
            if path.suffix == ".png":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "REAPER" in text or "seamless" in text.lower():
                bad.append(str(path.relative_to(ROOT)))
        self.assertEqual(bad, [])

    def test_no_secrets_in_shipped_files(self):
        patterns = (
            re.compile(r"BEGIN .*PRIVATE KEY"),
            re.compile(r"api[_-]?key\s*=\s*['\"][^'\"]+['\"]", re.I),
            re.compile(r"ghp_[A-Za-z0-9]{20,}"),
            re.compile(r"sk-[A-Za-z0-9]{20,}"),
        )
        bad = []
        for path in self._shipped_files():
            if path.suffix == ".png":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in patterns:
                if pattern.search(text):
                    bad.append("%s: %s" % (
                        path.relative_to(ROOT), pattern.pattern))
        self.assertEqual(bad, [])

    def test_version_single_sourced(self):
        # No hardcoded "0.1" left in code or install scripts (the
        # CHANGELOG legitimately mentions 0.1 as history).
        bad = []
        for rel in ("winlinx/common.py", "winlinx/installer.py",
                    "winlinx/manager.py", "winlinx/scanner.py",
                    "winlinx/watcher.py", "winlinx/run-windows-file",
                    "build_zip.py", "install.sh", "uninstall.sh",
                    "install-winlinx.py", "render_icon.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            if "0.1" in text:
                bad.append(rel)
        self.assertEqual(bad, [])

    def test_shell_version_flags(self):
        # Every shell entry point reports the single-sourced
        # version, even before anything is installed (the runner
        # used to print "unknown" from the repo layout).
        for script in ("winlinx/run-windows-file", "install.sh",
                       "uninstall.sh"):
            proc = subprocess.run(
                ["bash", str(ROOT / script), "--version"],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, script)
            self.assertIn("0.2", proc.stdout, script)
            self.assertNotIn("unknown", proc.stdout, script)

    def test_python_version_flags(self):
        # The Python CLIs report the same version.
        for script in ("winlinx/scanner.py", "winlinx/watcher.py",
                       "install-winlinx.py"):
            proc = subprocess.run(
                [sys.executable, str(ROOT / script), "--version"],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, script)
            self.assertIn("0.2", proc.stdout, script)


if __name__ == "__main__":
    unittest.main()
