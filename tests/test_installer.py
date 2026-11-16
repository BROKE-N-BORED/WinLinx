"""Tests for winlinx/installer.py — the Qt GUI installer.

Runs offscreen (QT_QPA_PLATFORM=offscreen). The install steps are
exercised in dry-run mode only: no apt installs, no pkexec, no
writes outside a temp dir. A dry run must never touch the machine.
"""

import importlib.util
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

import installer  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


class StepListTests(unittest.TestCase):
    def test_six_steps_in_order(self):
        keys = [key for key, _title, _func in installer.STEPS]
        self.assertEqual(keys, ["wine", "clamav", "yabridge",
                                "pyside6", "prefix", "files"])

    def test_step_titles(self):
        titles = [title for _key, title, _func in installer.STEPS]
        self.assertIn("Wine", titles[0])
        self.assertIn("ClamAV", titles[1])
        self.assertIn("yabridge", titles[2])
        self.assertIn("PySide6", titles[3])
        self.assertIn("Wine prefix", titles[4])
        self.assertIn("WinLinx files", titles[5])


class DryRunContextTests(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.ctx = installer.InstallContext(self.lines.append,
                                            dryrun=True)

    def test_run_does_not_execute(self):
        marker = Path(tempfile.gettempdir()) / "winlinx-dryrun-marker"
        if marker.exists():
            marker.unlink()
        code = self.ctx.run(["touch", str(marker)])
        self.assertEqual(code, 0)
        self.assertFalse(marker.exists())
        self.assertTrue(any("touch" in line for line in self.lines))

    def test_run_admin_does_not_execute(self):
        code = self.ctx.run_admin("rm -rf /", timeout=10)
        self.assertEqual(code, 0)
        self.assertTrue(any("rm -rf /" in line for line in self.lines))

    def test_download_pretends_success_without_writing(self):
        dest = Path(tempfile.gettempdir()) / "winlinx-dryrun-dl"
        if dest.exists():
            dest.unlink()
        self.assertTrue(self.ctx.download("https://example.invalid/x",
                                          str(dest)))
        self.assertFalse(dest.exists())

    def test_filesystem_ops_do_not_write(self):
        base = Path(tempfile.mkdtemp()) / "sub"
        self.assertTrue(self.ctx.mkdir(base))
        self.assertFalse(base.exists())
        target = Path(tempfile.gettempdir()) / "winlinx-dryrun-file"
        if target.exists():
            target.unlink()
        self.assertTrue(self.ctx.write_text(target, "hello"))
        self.assertFalse(target.exists())


class DryRunStepsTests(unittest.TestCase):
    def test_all_steps_succeed_in_dryrun(self):
        lines = []
        ctx = installer.InstallContext(lines.append, dryrun=True)
        for key, _title, func in installer.STEPS:
            result = func(ctx)
            self.assertEqual(result.status, installer.StepResult.DONE,
                             "step %s: %s" % (key, result.detail))

    def test_yabridge_step_is_pinned_and_verified(self):
        lines = []
        ctx = installer.InstallContext(lines.append, dryrun=True)
        installer.step_yabridge(ctx)
        blob = "\n".join(lines)
        self.assertIn("5.1.1", blob)
        self.assertIn("SHA-256", blob)
        self.assertNotIn("latest", blob.lower())

    def test_verify_sha256(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as fh:
            fh.write(b"hello world")
            path = fh.name
        try:
            import hashlib
            digest = hashlib.sha256(b"hello world").hexdigest()
            self.assertTrue(installer.verify_sha256(path, digest))
            self.assertFalse(installer.verify_sha256(
                path, "0" * 64))
            self.assertFalse(installer.verify_sha256(
                path + "-missing", digest))
        finally:
            os.unlink(path)

    def test_normalize_fingerprint(self):
        self.assertEqual(
            installer.normalize_fingerprint(
                "D43F 6401 4536 9C51 D786 DDEA 76F1 A20F F987 672F"),
            "D43F640145369C51D786DDEA76F1A20FF987672F")

    def test_wine_step_verifies_key_fingerprint(self):
        lines = []
        ctx = installer.InstallContext(lines.append, dryrun=True)
        installer.step_wine(ctx)
        blob = "\n".join(lines)
        self.assertIn("fingerprint", blob.lower())

    def test_wine_step_logs_the_real_admin_script(self):
        lines = []
        ctx = installer.InstallContext(lines.append, dryrun=True)
        installer.step_wine(ctx)
        blob = "\n".join(lines)
        self.assertIn("winehq-staging", blob)
        self.assertIn("dpkg --add-architecture i386", blob)
        self.assertIn("winehq-archive.key", blob)
        self.assertIn("apt-get install", blob)

    def test_clamav_step_logs_freshclam(self):
        lines = []
        ctx = installer.InstallContext(lines.append, dryrun=True)
        installer.step_clamav(ctx)
        blob = "\n".join(lines)
        self.assertIn("clamav", blob)
        self.assertIn("freshclam", blob)

    def test_files_step_references_winlinx_paths(self):
        lines = []
        ctx = installer.InstallContext(lines.append, dryrun=True)
        installer.step_files(ctx)
        blob = "\n".join(lines)
        self.assertIn(".local/share/winlinx", blob)
        self.assertIn("winlinx-watcher.service", blob)
        self.assertIn("WinLinx/Programs", blob)
        self.assertIn("winlinx-manager.desktop", blob)


class InstallerWindowTests(unittest.TestCase):
    def setUp(self):
        self.window = installer.InstallerWindow(dryrun=True)
        self.window.show()
        _APP.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        _APP.processEvents()

    def test_window_renders_with_six_steps(self):
        self.assertEqual(self.window.windowTitle(), "Install WinLinx")
        self.assertEqual(self.window.steps_list.count(), 6)
        self.assertTrue(self.window.install_btn.isEnabled())
        self.assertFalse(self.window.launch_btn.isEnabled())
        self.assertEqual(self.window.progress_bar.value(), 0)

    def test_dryrun_install_completes(self):
        self.window.install_btn.click()
        deadline = time.time() + 30
        while time.time() < deadline:
            _APP.processEvents()
            if self.window._worker is None and not self.window._installing:
                break
            time.sleep(0.05)
        self.assertFalse(self.window._installing)
        self.assertEqual(self.window.progress_bar.value(), 100)
        for index in range(6):
            text = self.window.steps_list.item(index).text()
            self.assertIn("done", text, "step %d: %s" % (index, text))
        self.assertTrue(self.window.launch_btn.isEnabled())
        self.assertIn("WinLinx Program Manager is in your app menu",
                      self.window.log_view.toPlainText())


class InstallerCancelTests(unittest.TestCase):
    """Cancelling must never report "Installed!" (v0.2.1 P0 #2).

    Two variants, both driven through the real window offscreen:
    (a) cancel during a step that then completes normally, and
    (b) cancel during a step that notices the flag and returns FAIL
    (which must be reported as cancelled, not failed). The fake
    steps replace STEPS so the timing is controllable; the real
    STEPS list is restored in tearDown.
    """

    def setUp(self):
        self._orig_steps = installer.STEPS
        self.window = None

    def tearDown(self):
        installer.STEPS = self._orig_steps
        if self.window is not None:
            self.window.close()
            self.window.deleteLater()
            _APP.processEvents()

    def _start_with_steps(self, step0):
        def quick_done(ctx):
            return installer.StepResult(installer.StepResult.DONE)

        installer.STEPS = [
            ("step0", "First step (slow)", step0),
        ] + [("step%d" % i, "Step %d" % i, quick_done)
             for i in range(1, 6)]
        self.window = installer.InstallerWindow(dryrun=True)
        self.window.show()
        _APP.processEvents()
        self.window.install_btn.click()
        # Wait until step 0 is running, then cancel mid-step.
        deadline = time.time() + 10
        while time.time() < deadline:
            _APP.processEvents()
            if "running" in self.window.steps_list.item(0).text():
                break
            time.sleep(0.05)
        self.assertIn("running",
                      self.window.steps_list.item(0).text())
        self.window.cancel_btn.click()
        deadline = time.time() + 30
        while time.time() < deadline:
            _APP.processEvents()
            if (self.window._worker is None
                    and not self.window._installing):
                break
            time.sleep(0.05)
        self.assertFalse(self.window._installing)

    def test_cancel_during_step_that_completes(self):
        def slow_step(ctx):
            # Ignores the cancel flag and finishes normally.
            time.sleep(1.5)
            return installer.StepResult(installer.StepResult.DONE)

        self._start_with_steps(slow_step)
        self.assertEqual(
            self.window.statusBar().currentMessage(),
            "Install cancelled - 1 of 6 steps done. Press "
            "Install to finish.")
        self.assertFalse(self.window.launch_btn.isEnabled())
        self.assertEqual(self.window.progress_bar.value(), 16)
        self.assertIn("done",
                      self.window.steps_list.item(0).text())
        for index in range(1, 6):
            self.assertIn(
                "pending", self.window.steps_list.item(index).text())
        log = self.window.log_view.toPlainText()
        self.assertNotIn("Some steps failed", log)
        self.assertNotIn("Installed!", log)
        self.assertTrue(self.window.install_btn.isEnabled())

    def test_cancel_during_step_that_noticed(self):
        def noticing_step(ctx):
            # Watches the flag and bails out with FAIL: the worker
            # must report this as cancelled, not failed.
            deadline = time.time() + 5
            while time.time() < deadline:
                if ctx.cancelled:
                    return installer.StepResult(
                        installer.StepResult.FAIL,
                        "cancelled by user")
                time.sleep(0.05)
            return installer.StepResult(installer.StepResult.DONE)

        self._start_with_steps(noticing_step)
        self.assertEqual(
            self.window.statusBar().currentMessage(),
            "Install cancelled - 0 of 6 steps done. Press "
            "Install to finish.")
        self.assertFalse(self.window.launch_btn.isEnabled())
        self.assertEqual(self.window.progress_bar.value(), 0)
        self.assertIn("cancelled",
                      self.window.steps_list.item(0).text())
        log = self.window.log_view.toPlainText()
        self.assertNotIn("Some steps failed", log)
        self.assertNotIn("Installed!", log)


class LauncherTests(unittest.TestCase):
    def test_launcher_sees_pyside6(self):
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "install_winlinx", root / "install-winlinx.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module.have_pyside6())
        self.assertTrue(module.ensure_pyside6())


class IconRenderTests(unittest.TestCase):
    def test_render_icons_from_svg(self):
        root = Path(__file__).resolve().parent.parent
        svg = root / "winlinx" / "icons" / "winlinx.svg"
        self.assertTrue(svg.is_file())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertTrue(installer.render_icons_from_svg(svg, out))
            for size in (256, 128, 48):
                png = out / ("winlinx-%d.png" % size)
                self.assertTrue(png.is_file())
                self.assertGreater(png.stat().st_size, 500)
            # The window frame renders blue, not black (Qt used to
            # paint clipPath contents — the SVG has no clipPath now).
            from PySide6.QtGui import QImage
            image = QImage(str(out / "winlinx-256.png"))
            self.assertEqual(image.pixelColor(128, 100).name(),
                             "#1b6ec2")
            self.assertEqual(image.pixelColor(60, 60).name(),
                             "#7fc4fd")


if __name__ == "__main__":
    unittest.main()
