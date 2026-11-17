"""Tests for the WinLinx self-update (v0.4 auto-update).

The engine (winlinx/updater.py) is tested against a fake GitHub:
a local HTTP server serving a fake release (zip + SHA-256 sidecar
+ /releases/latest JSON). The GUI flow is driven offscreen with
real dialogs, and the full journey (launch, popup, tap Update,
verified pull, restart prompt, restart, new version) runs as a
subprocess against a fake installed tree.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "winlinx"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_zip  # noqa: E402
import common  # noqa: E402
import fakehome  # noqa: E402
import updater  # noqa: E402

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
import manager  # noqa: E402

_APP = None


def setUpModule():
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


def make_fake_release_zip(version, dest, common_version=None):
    """A release zip for `version`, built from the real repo tree
    with common.py's version line patched. common_version patches
    the version line to something else (for the wrong-version
    test); the top folder always matches `version`."""
    top = "winlinx-v%s" % version
    patched = common_version if common_version is not None else version
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in build_zip.INCLUDE:
            data = (ROOT / rel).read_bytes()
            if rel == "winlinx/common.py":
                data = re.sub(
                    rb'__version__ = "[^"]+"',
                    ('__version__ = "%s"' % patched).encode(),
                    data, count=1)
            zf.writestr("%s/%s" % (top, rel), data)
    return dest


def install_tree_into_fake_home(home, version=None):
    """Mimic install.sh step 6: copy the installed subset of the
    repo tree into a fake home. version patches the installed
    common.py (None keeps the repo version). Returns the layer dir."""
    layer = home / ".local" / "share" / "winlinx"
    (layer / "icons").mkdir(parents=True, exist_ok=True)
    for path in (ROOT / "winlinx").glob("*.py"):
        data = path.read_bytes()
        if version is not None and path.name == "common.py":
            data = re.sub(
                rb'__version__ = "[^"]+"',
                ('__version__ = "%s"' % version).encode(), data,
                count=1)
        (layer / path.name).write_bytes(data)
    shutil.copy2(ROOT / "winlinx" / "run-windows-file",
                 layer / "run-windows-file")
    os.chmod(layer / "run-windows-file", 0o755)
    for icon in (ROOT / "winlinx" / "icons").iterdir():
        shutil.copy2(icon, layer / "icons" / icon.name)
    (layer / "quarantine").mkdir(exist_ok=True)
    apps = home / ".local" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    for name in ("winlinx-manager.desktop", "winlinx-runner.desktop"):
        text = (ROOT / "desktop" / name).read_text(encoding="utf-8")
        (apps / name).write_text(text.replace("@HOME@", str(home)),
                                 encoding="utf-8")
    mime_dir = home / ".local" / "share" / "mime" / "packages"
    mime_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "desktop" / "winlinx-mime.xml",
                 mime_dir / "winlinx.xml")
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "systemd" / "winlinx-watcher.service",
                 unit_dir / "winlinx-watcher.service")
    return layer


def installed_version(layer):
    text = (Path(layer) / "common.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    return match.group(1) if match else ""


class FakeGitHub:
    """A local HTTP server pretending to be GitHub Releases."""

    def __init__(self, tag="v0.5", zip_bytes=None, with_sha=True,
                 bad_sha=False):
        self.tag = tag
        self.zip_bytes = zip_bytes
        self.with_sha = with_sha
        self.bad_sha = bad_sha
        self.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0),
                                           self._make_handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    @property
    def api_url(self):
        return self.base + "/releases/latest"

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def _make_handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                fake.requests.append(self.path)
                tag = fake.tag
                if self.path == "/releases/latest":
                    assets = []
                    if fake.zip_bytes is not None:
                        assets.append({
                            "name": "winlinx-%s.zip" % tag,
                            "browser_download_url":
                                fake.base + "/winlinx-%s.zip" % tag})
                        if fake.with_sha:
                            assets.append({
                                "name":
                                    "winlinx-%s.zip.sha256" % tag,
                                "browser_download_url":
                                    fake.base
                                    + "/winlinx-%s.zip.sha256" % tag})
                    payload = json.dumps({
                        "tag_name": tag,
                        "html_url": fake.base + "/releases/" + tag,
                        "assets": assets}).encode("utf-8")
                    self._send(200, payload, "application/json")
                elif self.path == "/winlinx-%s.zip" % tag \
                        and fake.zip_bytes is not None:
                    self._send(200, fake.zip_bytes, "application/zip")
                elif self.path == "/winlinx-%s.zip.sha256" % tag \
                        and fake.with_sha \
                        and fake.zip_bytes is not None:
                    if fake.bad_sha:
                        digest = "0" * 64
                    else:
                        digest = hashlib.sha256(
                            fake.zip_bytes).hexdigest()
                    payload = ("%s  winlinx-%s.zip\n"
                               % (digest, tag)).encode("utf-8")
                    self._send(200, payload, "text/plain")
                else:
                    self._send(404, b"not found", "text/plain")

        return Handler


class _HomeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WINLINX_HOME")
        self._old_api = os.environ.get("WINLINX_UPDATE_API_URL")
        os.environ["WINLINX_HOME"] = self._tmp.name
        self.home = Path(self._tmp.name)
        self._server = None

    def tearDown(self):
        if self._server is not None:
            self._server.stop()
            self._server = None
        if self._old_home is None:
            os.environ.pop("WINLINX_HOME", None)
        else:
            os.environ["WINLINX_HOME"] = self._old_home
        if self._old_api is None:
            os.environ.pop("WINLINX_UPDATE_API_URL", None)
        else:
            os.environ["WINLINX_UPDATE_API_URL"] = self._old_api
        self._tmp.cleanup()

    def _fake_zip(self, version="0.5", common_version=None):
        dest = self.home / ("winlinx-v%s.zip" % version)
        return make_fake_release_zip(version, dest, common_version)

    def _start_server(self, tag="v0.5", zip_bytes=None,
                      with_sha=True, bad_sha=False):
        self._server = FakeGitHub(tag=tag, zip_bytes=zip_bytes,
                                  with_sha=with_sha, bad_sha=bad_sha)
        self._server.start()
        os.environ["WINLINX_UPDATE_API_URL"] = self._server.api_url
        return self._server


class UpdaterEngineTests(_HomeBase):
    """The updater engine against the fake GitHub (no Qt)."""

    def test_check_finds_update_with_sidecar(self):
        zip_bytes = self._fake_zip().read_bytes()
        self._start_server(zip_bytes=zip_bytes)
        result = common.check_for_update()
        self.assertTrue(result["available"])
        self.assertEqual(result["latest"], "v0.5")
        self.assertEqual(result["zip_name"], "winlinx-v0.5.zip")
        self.assertTrue(result["zip_url"].endswith(
            "/winlinx-v0.5.zip"))
        self.assertTrue(result["sha_url"].endswith(
            "/winlinx-v0.5.zip.sha256"))

    def test_check_up_to_date(self):
        self._start_server(tag="v%s" % common.__version__,
                           zip_bytes=self._fake_zip(
                               common.__version__).read_bytes())
        result = common.check_for_update()
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "")

    def test_check_network_failure_is_plain_error(self):
        os.environ["WINLINX_UPDATE_API_URL"] = (
            "http://127.0.0.1:9/releases/latest")
        result = common.check_for_update(timeout=5)
        self.assertFalse(result["available"])
        self.assertNotEqual(result["error"], "")

    def test_download_and_verify_ok(self):
        zip_bytes = self._fake_zip().read_bytes()
        server = self._start_server(zip_bytes=zip_bytes)
        result = common.check_for_update()
        ok, message, zip_path = updater.download_and_verify(
            result["zip_url"], result["sha_url"], "0.5",
            result["zip_name"])
        self.assertTrue(ok, message)
        self.assertTrue(Path(zip_path).is_file())
        self.assertIn("/winlinx-v0.5.zip", server.requests)
        self.assertIn("/winlinx-v0.5.zip.sha256", server.requests)

    def test_download_and_verify_bad_checksum_refused(self):
        zip_bytes = self._fake_zip().read_bytes()
        self._start_server(zip_bytes=zip_bytes, bad_sha=True)
        result = common.check_for_update()
        ok, message, zip_path = updater.download_and_verify(
            result["zip_url"], result["sha_url"], "0.5",
            result["zip_name"])
        self.assertFalse(ok)
        self.assertIsNone(zip_path)
        self.assertIn("did not arrive intact", message)
        # Nothing staged: the bad zip is deleted, not left lying
        # around.
        self.assertEqual(
            list(updater.version_dir("0.5").glob("*.zip")), [])

    def test_download_and_verify_missing_sidecar_refused(self):
        zip_bytes = self._fake_zip().read_bytes()
        server = self._start_server(zip_bytes=zip_bytes,
                                    with_sha=False)
        result = common.check_for_update()
        self.assertEqual(result["sha_url"], "")
        ok, message, zip_path = updater.download_and_verify(
            result["zip_url"], result["sha_url"], "0.5",
            result["zip_name"])
        self.assertFalse(ok)
        self.assertIsNone(zip_path)
        self.assertIn("safety check", message)
        # The zip was never even downloaded.
        self.assertNotIn("/winlinx-v0.5.zip", server.requests)

    def test_download_and_verify_sidecar_404_refused(self):
        zip_bytes = self._fake_zip().read_bytes()
        server = self._start_server(zip_bytes=zip_bytes)
        result = common.check_for_update()
        ok, message, zip_path = updater.download_and_verify(
            result["zip_url"], server.base + "/no-such.sha256",
            "0.5", result["zip_name"])
        self.assertFalse(ok)
        self.assertIsNone(zip_path)
        self.assertIn("safety check", message)

    def test_stage_zip_ok(self):
        zip_path = self._fake_zip()
        ok, message, tree = updater.stage_zip(zip_path, "0.5")
        self.assertTrue(ok, message)
        self.assertTrue((tree / "winlinx" / "manager.py").is_file())
        self.assertTrue(
            (tree / "winlinx" / "apply_update.py").is_file())

    def test_stage_zip_wrong_version_refused(self):
        zip_path = self._fake_zip("0.5", common_version="0.4")
        ok, message, _tree = updater.stage_zip(zip_path, "0.5")
        self.assertFalse(ok)
        self.assertIn("different version", message)

    def test_stage_zip_missing_member_refused(self):
        dest = self.home / "thin.zip"
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("winlinx-v0.5/winlinx/manager.py", b"x")
            zf.writestr("winlinx-v0.5/winlinx/common.py",
                        b'__version__ = "0.5"\n')
        ok, message, _tree = updater.stage_zip(dest, "0.5")
        self.assertFalse(ok)
        self.assertIn("not a WinLinx release", message)

    def test_stage_zip_unsafe_entry_refused(self):
        dest = self.home / "evil.zip"
        with zipfile.ZipFile(dest, "w") as zf:
            for rel in build_zip.INCLUDE:
                zf.writestr("winlinx-v0.5/%s" % rel, b"x")
            zf.writestr("../evil.txt", b"evil")
        ok, message, _tree = updater.stage_zip(dest, "0.5")
        self.assertFalse(ok)
        self.assertIn("damaged", message)
        self.assertFalse((self.home / "evil.txt").exists())

    def test_stage_zip_syntax_error_refused(self):
        dest = self.home / "broken.zip"
        with zipfile.ZipFile(dest, "w") as zf:
            for rel in build_zip.INCLUDE:
                data = b"x"
                if rel == "winlinx/common.py":
                    data = b'__version__ = "0.5"\n'
                elif rel == "winlinx/manager.py":
                    data = b"def broken(:\n"
                zf.writestr("winlinx-v0.5/%s" % rel, data)
        ok, message, _tree = updater.stage_zip(dest, "0.5")
        self.assertFalse(ok)
        self.assertIn("damaged", message)

    def _staged_tree(self, version="0.5"):
        zip_path = self._fake_zip(version)
        ok, message, tree = updater.stage_zip(zip_path, version)
        self.assertTrue(ok, message)
        return tree

    def test_apply_swaps_files_and_keeps_state(self):
        layer = install_tree_into_fake_home(self.home)
        (layer / "handled.json").write_text('{"kept": true}')
        (layer / "layer.log").write_text("old log\n")
        tree = self._staged_tree()
        ok, message, backup = updater.apply_staged(tree, "0.5")
        self.assertTrue(ok, message)
        self.assertEqual(installed_version(layer), "0.5")
        self.assertEqual((layer / "handled.json").read_text(),
                         '{"kept": true}')
        # The log keeps its old lines (the apply appends its own).
        self.assertTrue((layer / "layer.log").read_text().startswith(
            "old log\n"))
        # The backup holds the old version.
        self.assertIsNotNone(backup)
        self.assertEqual(installed_version(backup / "layer"), "0.4")
        # Menu entries refreshed with this home baked in.
        entry = (self.home / ".local" / "share" / "applications"
                 / "winlinx-manager.desktop").read_text()
        self.assertIn(str(self.home), entry)
        self.assertNotIn("@HOME@", entry)

    def test_apply_rolls_back_on_failure(self):
        layer = install_tree_into_fake_home(self.home)
        marker = (layer / "manager.py").read_text() + "\n# OLD-MARKER\n"
        (layer / "manager.py").write_text(marker)
        tree = self._staged_tree()
        with mock.patch("os.chmod",
                        side_effect=OSError("disk is full")):
            ok, message, _backup = updater.apply_staged(tree, "0.5")
        self.assertFalse(ok)
        self.assertIn("untouched", message)
        # The old files are back, marker and all.
        self.assertEqual(installed_version(layer), "0.4")
        self.assertIn("# OLD-MARKER",
                      (layer / "manager.py").read_text())

    def test_pending_round_trip(self):
        layer = install_tree_into_fake_home(self.home)
        tree = self._staged_tree()
        self.assertTrue(updater.write_pending("0.5", tree))
        pending = updater.read_pending()
        self.assertEqual(pending["version"], "0.5")
        version, message, _backup = updater.apply_pending()
        self.assertEqual(version, "0.5", message)
        self.assertEqual(installed_version(layer), "0.5")
        self.assertIsNone(updater.read_pending())
        self.assertFalse(updater.pending_path().exists())

    def test_pending_missing_tree_is_dropped(self):
        updater.write_pending("0.5", self.home / "no-such-tree")
        self.assertIsNone(updater.read_pending())
        self.assertFalse(updater.pending_path().exists())

    def test_pending_gives_up_after_repeated_failures(self):
        broken = self.home / "broken-tree"
        broken.mkdir()
        updater.write_pending("0.5", broken)
        for _ in range(2):
            version, _message, _b = updater.apply_pending()
            self.assertIsNone(version)
            self.assertIsNotNone(updater.read_pending())
        version, _message, _b = updater.apply_pending()
        self.assertIsNone(version)
        self.assertIsNone(updater.read_pending())

    def test_safe_version_rejects_junk(self):
        self.assertEqual(updater.safe_version("v0.5"), "0.5")
        self.assertEqual(updater.safe_version("0.5.1"), "0.5.1")
        self.assertEqual(updater.safe_version("../evil"), "")
        self.assertEqual(updater.safe_version("0.5/x"), "")
        self.assertEqual(updater.safe_version(""), "")


class UpdateGuiTests(_HomeBase):
    """The manager's update flow, driven offscreen with real
    dialogs against the fake GitHub."""

    def setUp(self):
        super().setUp()
        fakehome.build_fake_home(self.home)
        self.layer = install_tree_into_fake_home(self.home)
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self.window.close()
            self.window.deleteLater()
            _APP.processEvents()
        super().tearDown()

    def _wait_until(self, cond, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _APP.processEvents()
            if cond():
                return True
            time.sleep(0.05)
        _APP.processEvents()
        return bool(cond())

    def _wait_for_refresh(self, timeout=20.0):
        return self._wait_until(
            lambda: self.window._worker is None, timeout)

    def _arm_click(self, title, text, seen):
        """Arm a timer that clicks `text` in the modal dialog
        titled `title` as soon as it appears (a modal dialog's own
        event loop runs the timer, so this works while exec() is
        blocking the test). Records what it saw in `seen`. The
        retries stop after 30 s so a chain can never outlive its
        test and click a dialog in a later one."""
        armed_at = time.time()

        def _try_click():
            widget = _APP.activeModalWidget()
            if widget is not None \
                    and widget.windowTitle() == title:
                seen["title"] = title
                seen["text"] = widget.text()
                for button in widget.buttons():
                    if button.text() == text:
                        QTest.mouseClick(
                            button, Qt.MouseButton.LeftButton)
                        seen["clicked"] = True
                        return
                return
            if time.time() - armed_at < 30.0:
                QTimer.singleShot(100, _try_click)

        QTimer.singleShot(200, _try_click)

    def _wait_for_startup_check(self, timeout=20.0):
        """Wait until the startup check has had time to run (its
        timer fires 1.5 s after construction) and finished. The
        plain "worker is None" wait would pass vacuously before the
        timer fires, so the elapsed time is part of the condition."""
        started = self._window_opened_at
        return self._wait_until(
            lambda: time.time() - started > 2.5
            and self.window._update_check_worker is None, timeout)

    def _open_window(self, startup_check):
        self.window = manager.ManagerWindow(
            startup_update_check=startup_check)
        self._window_opened_at = time.time()
        self.window.show()
        self._wait_for_refresh()

    def test_startup_check_up_to_date_is_silent(self):
        zip_bytes = self._fake_zip(common.__version__).read_bytes()
        self._start_server(tag="v%s" % common.__version__,
                           zip_bytes=zip_bytes)
        self._open_window(startup_check=True)
        self.assertTrue(self._wait_for_startup_check())
        time.sleep(0.5)
        _APP.processEvents()
        self.assertIsNone(_APP.activeModalWidget())
        self.assertFalse(updater.updates_dir().exists())

    def test_startup_check_no_network_is_silent(self):
        os.environ["WINLINX_UPDATE_API_URL"] = (
            "http://127.0.0.1:9/releases/latest")
        self._open_window(startup_check=True)
        self.assertTrue(self._wait_for_startup_check())
        time.sleep(0.5)
        _APP.processEvents()
        self.assertIsNone(_APP.activeModalWidget())
        self.assertTrue(self.window.isVisible())

    def test_startup_check_available_prompts_but_downloads_nothing(self):
        zip_bytes = self._fake_zip().read_bytes()
        self._start_server(zip_bytes=zip_bytes)
        seen = {}
        self._arm_click("Update available", "Later", seen)
        self._open_window(startup_check=True)
        self.assertTrue(self._wait_until(
            lambda: seen.get("clicked"), timeout=20.0))
        self.assertIn("0.5", seen["text"])
        self.assertIn("0.4", seen["text"])
        # Nothing downloaded until the user taps Update.
        self.assertEqual(
            list(updater.updates_dir().glob("**/*.zip"))
            if updater.updates_dir().exists() else [], [])

    def test_help_check_pulls_and_stages(self):
        zip_bytes = self._fake_zip().read_bytes()
        self._start_server(zip_bytes=zip_bytes)
        self._open_window(startup_check=False)
        seen_update, seen_restart = {}, {}
        self._arm_click("Update available", "Update", seen_update)
        self._arm_click("Update ready", "Later", seen_restart)
        self.window._check_updates()
        self.assertTrue(self._wait_until(
            lambda: updater.read_pending() is not None,
            timeout=30.0))
        self.assertTrue(seen_update.get("clicked"))
        self.assertTrue(seen_restart.get("clicked"))
        self.assertIn("0.5", seen_restart["text"])
        # Staged, but the installed files are untouched until the
        # restart (the user picked Later).
        self.assertEqual(installed_version(self.layer), "0.4")
        pending = updater.read_pending()
        self.assertTrue(Path(pending["tree"]).is_dir())
        self.assertIn("next time WinLinx starts",
                      self.window.statusBar().currentMessage())

    def test_restart_later_applies_at_next_launch(self):
        zip_bytes = self._fake_zip().read_bytes()
        self._start_server(zip_bytes=zip_bytes)
        self._open_window(startup_check=False)
        self._arm_click("Update available", "Update", {})
        self._arm_click("Update ready", "Later", {})
        self.window._check_updates()
        self.assertTrue(self._wait_until(
            lambda: updater.read_pending() is not None,
            timeout=30.0))
        # What main() does at the next launch:
        version, message, _backup = updater.apply_pending()
        self.assertEqual(version, "0.5", message)
        self.assertEqual(installed_version(self.layer), "0.5")
        self.assertIsNone(updater.read_pending())

    def test_pull_refuses_bad_checksum(self):
        zip_bytes = self._fake_zip().read_bytes()
        self._start_server(zip_bytes=zip_bytes, bad_sha=True)
        self._open_window(startup_check=False)
        seen_warn = {}
        self._arm_click("Update available", "Update", {})
        self._arm_click("Update not installed", "OK", seen_warn)
        self.window._check_updates()
        self.assertTrue(self._wait_until(
            lambda: seen_warn.get("clicked"), timeout=30.0))
        self.assertIn("did not arrive intact", seen_warn["text"])
        self.assertIsNone(updater.read_pending())
        self.assertEqual(
            list(updater.updates_dir().glob("**/*.zip"))
            if updater.updates_dir().exists() else [], [])
        self.assertEqual(installed_version(self.layer), "0.4")

    def test_pull_refuses_missing_sidecar(self):
        zip_bytes = self._fake_zip().read_bytes()
        self._start_server(zip_bytes=zip_bytes, with_sha=False)
        self._open_window(startup_check=False)
        seen_warn = {}
        self._arm_click("Update available", "Update", {})
        self._arm_click("Update not installed", "OK", seen_warn)
        self.window._check_updates()
        self.assertTrue(self._wait_until(
            lambda: seen_warn.get("clicked"), timeout=30.0))
        self.assertIn("safety check", seen_warn["text"])
        self.assertIsNone(updater.read_pending())
        self.assertEqual(installed_version(self.layer), "0.4")

    def test_restart_now_spawns_helper_and_quits(self):
        zip_path = self._fake_zip()
        ok, message, tree = updater.stage_zip(zip_path, "0.5")
        self.assertTrue(ok, message)
        self._open_window(startup_check=False)
        seen = {}
        self._arm_click("Update ready", "Restart Now", seen)
        with mock.patch("subprocess.Popen") as mock_popen:
            with mock.patch.object(_APP, "quit") as mock_quit:
                self.window._ask_restart("0.5", tree)
        self.assertTrue(seen.get("clicked"))
        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1],
                         str(Path(tree) / "winlinx"
                             / "apply_update.py"))
        self.assertEqual(argv[argv.index("--wait-pid") + 1],
                         str(os.getpid()))
        self.assertEqual(argv[argv.index("--manager") + 1],
                         str(Path(manager.__file__).resolve()))
        mock_quit.assert_called_once()
        pending = updater.read_pending()
        self.assertEqual(pending["version"], "0.5")


class UpdateEndToEndTests(_HomeBase):
    """The full journey as a subprocess: launch the installed
    manager, tap Update, tap Restart Now, and prove the helper
    applied the update and relaunched the new version."""

    def setUp(self):
        super().setUp()
        fakehome.build_fake_home(self.home)
        self.layer = install_tree_into_fake_home(self.home)
        self._procs = []
        self._env = dict(os.environ)
        self._env["QT_QPA_PLATFORM"] = "offscreen"
        self._env["WINLINX_HOME"] = str(self.home)
        self._env["HOME"] = str(self.home)

    def tearDown(self):
        for proc in self._procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=10)
        # The helper relaunches a manager of its own: kill anything
        # still running the fake installed copy.
        subprocess.run(["pkill", "-f", str(self.layer / "manager.py")],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        super().tearDown()

    def _wait_until(self, cond, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.2)
        return bool(cond())

    def test_full_journey_restart_now(self):
        zip_bytes = self._fake_zip().read_bytes()
        server = self._start_server(zip_bytes=zip_bytes)
        self._env["WINLINX_UPDATE_API_URL"] = server.api_url
        driver = ROOT / "tests" / "drive_update_e2e.py"
        proc = subprocess.run(
            [sys.executable, str(driver)], env=self._env,
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        # The helper applied the update and relaunched the manager.
        self.assertTrue(self._wait_until(
            lambda: installed_version(self.layer) == "0.5",
            timeout=30.0))
        self.assertTrue(self._wait_until(
            lambda: subprocess.run(
                ["pgrep", "-f", str(self.layer / "manager.py")],
                stdout=subprocess.DEVNULL).returncode == 0,
            timeout=30.0))
        version = subprocess.run(
            [sys.executable, str(self.layer / "manager.py"),
             "--version"], env=self._env, capture_output=True,
            text=True, timeout=60)
        self.assertIn("0.5", version.stdout)
        log = (self.layer / "layer.log").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn("Update applied", log)

    def test_deferred_update_applies_at_startup(self):
        zip_bytes = self._fake_zip().read_bytes()
        server = self._start_server(zip_bytes=zip_bytes)
        self._env["WINLINX_UPDATE_API_URL"] = server.api_url
        # Stage the update and defer it, as if the user picked Later.
        result = common.check_for_update()
        ok, message, zip_path = updater.download_and_verify(
            result["zip_url"], result["sha_url"], "0.5",
            result["zip_name"])
        self.assertTrue(ok, message)
        ok, message, tree = updater.stage_zip(zip_path, "0.5")
        self.assertTrue(ok, message)
        self.assertTrue(updater.write_pending("0.5", tree))
        # Launch the installed manager: main() applies the pending
        # update and restarts on the new code before the window
        # opens.
        proc = subprocess.Popen(
            [sys.executable, str(self.layer / "manager.py")],
            env=self._env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self._procs.append(proc)
        lock = self.layer / "manager.lock"
        # Wait for the apply to FINISH, not just for the version to
        # flip: common.py is copied early in the swap, while the
        # pending marker is cleared at the end. Waiting on the
        # version alone catches the apply half-done.
        self.assertTrue(self._wait_until(
            lambda: installed_version(self.layer) == "0.5"
            and updater.read_pending() is None
            and lock.exists(), timeout=30.0))
        self.assertIsNone(proc.poll(),
                          "manager died after applying the update")
        self.assertIsNone(updater.read_pending())
        proc.terminate()
        _out, err = proc.communicate(timeout=10)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
