#!/usr/bin/env python3
"""WinLinx GUI installer - native Qt window, no terminal needed.

Double-click "Install WinLinx" in the extracted folder. It performs
the same steps as install.sh (Wine, ClamAV, yabridge, PySide6, Wine
prefix + VST folders, WinLinx files + menu entry + watcher), asking
for the admin password through pkexec whenever a step needs it.

Warn-never-refuse, like install.sh: a failed step is logged and the
install carries on; the window says plainly what worked and what
did not. Safe to run again - it redoes the failed steps.

Dry run (smoke tests only): WINLINX_INSTALLER_DRYRUN=1 makes every
step log what it would do without running anything privileged or
writing anything. The shipped installer always does the real steps.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent          # winlinx/
SRC_DIR = HERE.parent                            # extracted folder root
ICON_PATH = HERE / "icons" / "winlinx-256.png"

DRYRUN_ENV = "WINLINX_INSTALLER_DRYRUN"

sys.path.insert(0, str(HERE))
try:
    import common as _common
    __version__ = _common.__version__
except ImportError:  # pragma: no cover - common.py ships next to this file
    __version__ = "unknown"

# yabridge, pinned + verified (never "latest", never unchecked).
# SHA-256 of yabridge-5.1.1.tar.gz, fetched from the GitHub release.
YABRIDGE_VERSION = "5.1.1"
YABRIDGE_SHA256 = (
    "d11f0307412b5566b09a75ed4aa191266db56d22f4de4d20e4fbde6296cd1a47")
YABRIDGE_URL = ("https://github.com/robbert-vdh/yabridge/releases/"
                "download/%s/yabridge-%s.tar.gz"
                % (YABRIDGE_VERSION, YABRIDGE_VERSION))
# WineHQ apt key fingerprint (no spaces), verified with gpg
# --show-keys against the downloaded key before it is trusted.
WINEHQ_KEY_FINGERPRINT = "D43F640145369C51D786DDEA76F1A20FF987672F"

# MIME types the WinLinx runner handles (see desktop/winlinx-mime.xml).
RUNNER_MIME_TYPES = (
    "application/x-msdownload",
    "application/x-msi",
    "application/x-winlinx-vst3",
    "application/x-winlinx-vst2",
    "application/x-winlinx-clap",
)

from PySide6.QtCore import Qt, QSettings, QThread, Signal  # noqa: E402
from PySide6.QtGui import QIcon, QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QAbstractItemView, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QVBoxLayout, QWidget,
)


# ---------------------------------------------------------------- context

class InstallContext:
    """Everything a step needs: logging, command running, dry-run
    stubs. All side effects go through here so a dry run can fake
    them without touching the machine."""

    def __init__(self, log, dryrun=False):
        self._log = log
        self.dryrun = dryrun
        self.cancelled = False
        self.home = Path(os.path.expanduser("~"))

    def log(self, message):
        self._log(message)

    def which(self, name):
        return shutil.which(name)

    # -- commands ---------------------------------------------------

    def run(self, cmd, timeout=None):
        """Run a command, streaming its output to the log.
        Returns the exit code (0 in dry-run)."""
        self.log("  $ %s" % " ".join(str(c) for c in cmd))
        if self.dryrun:
            return 0
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
        except OSError as exc:
            self.log("  !! could not start: %s" % exc)
            return 127
        try:
            for line in proc.stdout:
                self.log("  | " + line.rstrip("\n"))
        except Exception:  # noqa: BLE001 - stream hiccups are not fatal
            pass
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.log("  !! timed out")
            return 124

    def run_output(self, cmd, timeout=60):
        """Run a command, return (exit code, stdout). Dry-run
        returns (0, 'dry-run')."""
        self.log("  $ %s" % " ".join(str(c) for c in cmd))
        if self.dryrun:
            return 0, "dry-run"
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)
        return proc.returncode, (proc.stdout or "").strip()

    def run_admin(self, script, timeout=None):
        """Run a shell script as admin via pkexec (GUI password
        prompt). Returns the exit code (0 in dry-run)."""
        self.log("  $ [admin] bash -c ...")
        for line in script.strip().splitlines():
            self.log("  |   %s" % line)
        if self.dryrun:
            return 0
        if not shutil.which("pkexec"):
            self.log("  !! pkexec not found - cannot run admin steps")
            return 127
        return self.run(["pkexec", "bash", "-c", script],
                        timeout=timeout)

    # -- network ------------------------------------------------------

    def download(self, url, dest, resume=True):
        """Download a URL to a file, with retries and resume
        (U24: a failed or frozen download picks up where it left
        off). Returns True on success. Dry-run pretends success."""
        self.log("  downloading %s" % url)
        if self.dryrun:
            return True
        dest = Path(dest)
        for attempt in range(3):
            try:
                headers = {"User-Agent": "WinLinx-installer/%s"
                           % __version__}
                start = 0
                if resume and dest.is_file():
                    start = dest.stat().st_size
                    if start > 0:
                        headers["Range"] = "bytes=%d-" % start
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=120) as resp:
                    if start and resp.status == 206:
                        mode = "ab"
                    else:
                        mode = "wb"
                        start = 0
                    with open(dest, mode) as fh:
                        while True:
                            chunk = resp.read(1024 * 256)
                            if not chunk:
                                break
                            fh.write(chunk)
                return True
            except Exception as exc:  # noqa: BLE001 - retried, then logged
                self.log("  !! download attempt %d failed: %s"
                         % (attempt + 1, exc))
                if attempt == 2:
                    return False
        return False

    # -- filesystem ---------------------------------------------------

    def mkdir(self, path):
        if self.dryrun:
            self.log("  would create %s" % path)
            return True
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            return True
        except OSError as exc:
            self.log("  !! could not create %s: %s" % (path, exc))
            return False

    def copy(self, src, dest):
        if self.dryrun:
            self.log("  would copy %s -> %s" % (src, dest))
            return True
        try:
            shutil.copy2(str(src), str(dest))
            return True
        except OSError as exc:
            self.log("  !! could not copy %s: %s" % (src, exc))
            return False

    def copy_tree(self, src, dest):
        if self.dryrun:
            self.log("  would copy %s/ -> %s/" % (src, dest))
            return True
        try:
            shutil.copytree(str(src), str(dest), dirs_exist_ok=True)
            return True
        except OSError as exc:
            self.log("  !! could not copy %s: %s" % (src, exc))
            return False

    def write_text(self, path, text):
        if self.dryrun:
            self.log("  would write %s" % path)
            return True
        try:
            Path(path).write_text(text, encoding="utf-8")
            return True
        except OSError as exc:
            self.log("  !! could not write %s: %s" % (path, exc))
            return False

    def chmod_exec(self, path):
        if self.dryrun:
            return True
        try:
            os.chmod(path, 0o755)
            return True
        except OSError as exc:
            self.log("  !! could not chmod %s: %s" % (path, exc))
            return False


# ---------------------------------------------------------------- step results

class StepResult:
    DONE = "done"
    WARN = "warning"
    FAIL = "failed"
    CANCELLED = "cancelled"

    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail


def _codename():
    """Ubuntu base codename (noble on Mint 22.x)."""
    codename = None
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("UBUNTU_CODENAME="):
                    return line.split("=", 1)[1].strip().strip('"')
                if line.startswith("VERSION_CODENAME="):
                    codename = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return codename or "noble"


# ---------------------------------------------------------------- icons

def render_icons_from_svg(svg_path, out_dir):
    """Render winlinx-{256,128,48}.png from the SVG into out_dir.
    Used by the installer when the PNGs are missing (a git clone
    ships only the SVG - the GitHub snapshot push cannot carry
    binary files - while the release zip ships the PNGs too).
    Returns True on success."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
    except ImportError:
        return False
    try:
        renderer = QSvgRenderer(str(svg_path))
        if not renderer.isValid():
            return False
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for size in (256, 128, 48):
            image = QImage(size, size, QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            renderer.render(painter)
            painter.end()
            if not image.save(str(out_dir / ("winlinx-%d.png" % size))):
                return False
        return True
    except Exception:  # noqa: BLE001 - caller logs a warning
        return False


# ---------------------------------------------------------------- verification

def verify_sha256(path, expected):
    """True when the file at path hashes to expected (hex)."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest().lower() == expected.lower()


def normalize_fingerprint(text):
    """A gpg fingerprint with spaces and case removed, for
    comparison ('D43F 6401 ...' -> 'D43F6401...')."""
    return "".join((text or "").split()).upper()


def verify_winehq_key(ctx, key_path):
    """Check the downloaded WineHQ key against the known fingerprint
    with gpg. Returns True only on a match; logs what happened."""
    if ctx.dryrun:
        ctx.log("  would verify the WineHQ key fingerprint")
        return True
    code, out = ctx.run_output(["gpg", "--show-keys",
                                "--with-fingerprint", str(key_path)])
    if code != 0 or not out:
        ctx.log("WARNING: could not read the WineHQ key with gpg - "
                "the WineHQ repository will NOT be added")
        return False
    if WINEHQ_KEY_FINGERPRINT in normalize_fingerprint(out):
        ctx.log("OK: WineHQ key fingerprint matches")
        return True
    ctx.log("WARNING: the WineHQ key fingerprint does NOT match - "
            "the WineHQ repository will NOT be added")
    return False


# ---------------------------------------------------------------- the steps

def step_wine(ctx):
    """Wine Staging (runs Windows programs) + winetricks."""
    ctx.log("Installing Wine (runs Windows programs)…")
    codename = _codename()
    ctx.log("Ubuntu base detected: %s" % codename)
    key_ok = ctx.download("https://dl.winehq.org/wine-builds/winehq.key",
                          "/tmp/winlinx-winehq.key")
    if key_ok:
        key_ok = verify_winehq_key(ctx, "/tmp/winlinx-winehq.key")
    src_ok = ctx.download(
        "https://dl.winehq.org/wine-builds/ubuntu/dists/%s/winehq-%s.sources"
        % (codename, codename), "/tmp/winlinx-winehq.sources")
    lines = ["export DEBIAN_FRONTEND=noninteractive",
             "dpkg --add-architecture i386 || true",
             "mkdir -p /etc/apt/keyrings"]
    if key_ok and src_ok:
        lines.append("cp /tmp/winlinx-winehq.key "
                     "/etc/apt/keyrings/winehq-archive.key")
        lines.append("cp /tmp/winlinx-winehq.sources "
                     "/etc/apt/sources.list.d/winehq-%s.sources"
                     % codename)
        ctx.log("WineHQ repository files downloaded")
    else:
        ctx.log("WARNING: could not fetch the WineHQ repository files "
                "(no internet?) - will try the older Wine from Mint")
    lines.append("apt-get update || true")
    lines.append("apt-get install --install-recommends -y "
                 "winehq-staging winetricks || apt-get install -y wine")
    code = ctx.run_admin("\n".join(lines), timeout=1800)
    if code != 0:
        ctx.log("WARNING: the Wine install step exited with code %s" % code)
    rc, version = ctx.run_output(["wine", "--version"])
    if rc == 0 and version:
        ctx.log("OK: Wine installed: %s" % version)
        return StepResult(StepResult.DONE, version)
    ctx.log("WARNING: Wine could not be installed - Windows programs "
            "will NOT run until Wine is installed")
    return StepResult(StepResult.FAIL,
                      "Wine is not installed afterwards")


def step_clamav(ctx):
    """ClamAV (scans files before they run) + virus signatures."""
    ctx.log("Installing ClamAV (scans files before they run)…")
    script = "\n".join([
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get install -y clamav clamav-freshclam",
        "systemctl stop clamav-freshclam 2>/dev/null || true",
        "freshclam",
        "systemctl enable --now clamav-freshclam 2>/dev/null || true",
    ])
    ctx.log("Downloading virus signatures takes a few minutes…")
    code = ctx.run_admin(script, timeout=1800)
    rc, version = ctx.run_output(["clamscan", "--version"])
    if rc == 0 and version:
        ctx.log("OK: ClamAV installed: %s" % version)
        if code != 0:
            ctx.log("WARNING: signature download or the updater "
                    "service reported a problem - scans may fail "
                    "until signatures download")
            return StepResult(StepResult.WARN,
                              "signatures may be missing")
        return StepResult(StepResult.DONE, version)
    ctx.log("WARNING: ClamAV could not be installed - files will run "
            "UNSCANNED until it is installed")
    return StepResult(StepResult.FAIL, "ClamAV is not installed")


def step_yabridge(ctx):
    """yabridge (bridges Windows VST plug-ins into Linux). Pinned
    release, SHA-256 verified after download, extracted with a
    filter (PEP 706)."""
    ctx.log("Installing yabridge %s (bridges Windows VST plug-ins)…"
            % YABRIDGE_VERSION)
    if ctx.which("yabridgectl"):
        ctx.log("OK: yabridge already installed")
        return StepResult(StepResult.DONE, "already installed")
    if ctx.dryrun:
        ctx.log("Dry run: would download yabridge %s from GitHub, "
                "verify its SHA-256 (%s...), and install it to "
                "~/.local/share/yabridge"
                % (YABRIDGE_VERSION, YABRIDGE_SHA256[:16]))
        return StepResult(StepResult.DONE, "dry-run")
    archive = "/tmp/winlinx-yabridge.tar.gz"
    try:
        if os.path.exists(archive):
            os.unlink(archive)
    except OSError:
        pass
    if not ctx.download(YABRIDGE_URL, archive):
        ctx.log("WARNING: yabridge could not be downloaded (no "
                "internet?) - VST bridging will NOT work until it is "
                "installed")
        return StepResult(StepResult.FAIL, "yabridge not installed")
    if not verify_sha256(archive, YABRIDGE_SHA256):
        ctx.log("WARNING: the yabridge download does NOT match its "
                "SHA-256 checksum - refusing to install it")
        try:
            os.unlink(archive)
        except OSError:
            pass
        return StepResult(StepResult.FAIL, "checksum mismatch")
    ctx.log("OK: yabridge checksum matches")
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpd:
            with tarfile.open(archive) as tf:
                tf.extractall(tmpd, filter="data")
            found = None
            for root, _dirs, files in os.walk(tmpd):
                if "yabridgectl" in files:
                    found = Path(root)
                    break
            if found is not None:
                dest = ctx.home / ".local" / "share" / "yabridge"
                ctx.mkdir(dest)
                ctx.copy_tree(found, dest)
                ctx.mkdir(ctx.home / ".local" / "bin")
                link = ctx.home / ".local" / "bin" / "yabridgectl"
                try:
                    if link.is_symlink() or link.exists():
                        link.unlink()
                    os.symlink(str(dest / "yabridgectl"), link)
                except OSError as exc:
                    ctx.log("  !! could not link yabridgectl: %s"
                            % exc)
                ctx.log("OK: yabridge %s installed" % YABRIDGE_VERSION)
                return StepResult(StepResult.DONE, YABRIDGE_VERSION)
    except Exception as exc:  # noqa: BLE001 - logged, step warns
        ctx.log("WARNING: yabridge archive problem: %s" % exc)
    finally:
        try:
            os.unlink(archive)
        except OSError:
            pass
    ctx.log("WARNING: yabridge could not be installed - VST bridging "
            "will NOT work until it is installed")
    return StepResult(StepResult.FAIL, "yabridge not installed")


def step_pyside6(ctx):
    """PySide6 (the Program Manager window)."""
    ctx.log("Checking PySide6 (the Program Manager window)…")
    rc, _out = ctx.run_output([sys.executable, "-c", "import PySide6"])
    if rc == 0:
        ctx.log("OK: PySide6 already installed")
        return StepResult(StepResult.DONE, "already installed")
    ctx.log("PySide6 missing for %s - trying pip…" % sys.executable)
    code = ctx.run([sys.executable, "-m", "pip", "install", "--user",
                    "--break-system-packages", "PySide6-Essentials"],
                   timeout=600)
    rc, _out = ctx.run_output([sys.executable, "-c", "import PySide6"])
    if code == 0 and rc == 0:
        ctx.log("OK: PySide6 installed (pip)")
        return StepResult(StepResult.DONE, "installed via pip")
    ctx.log("WARNING: PySide6 could not be installed - the Program "
            "Manager window will NOT open until it is installed")
    return StepResult(StepResult.FAIL, "PySide6 missing")


def step_prefix(ctx):
    """Wine prefix + VST folders, registered with yabridge.

    Returns WARN (not DONE) when the step skips its core work: no
    wineboot, a failed wineboot, no yabridgectl, or a failed sync.
    The run then reports "Installed with warning(s)" instead of a
    bare "Installed!" for a prefix that was never created."""
    ctx.log("Setting up the Wine prefix and VST folders…")
    if ctx.dryrun:
        ctx.log("Dry run: would initialise the Wine prefix and "
                "register the VST folders with yabridge")
        return StepResult(StepResult.DONE, "dry-run")
    warned = []
    if ctx.which("wineboot"):
        ctx.log("First Wine start takes a minute…")
        code = ctx.run(["wineboot", "--init"], timeout=300)
        if code == 0:
            ctx.log("OK: Wine prefix ready at %s"
                    % (ctx.home / ".wine"))
        else:
            ctx.log("WARNING: wineboot was slow or failed - the "
                    "prefix may still work")
            warned.append("wineboot failed")
    else:
        ctx.log("WARNING: wineboot not found - skipping prefix setup")
        warned.append("wineboot not found")
    vst_dirs = [
        ctx.home / ".wine" / "drive_c" / "Program Files"
        / "Common Files" / "VST3",
        ctx.home / ".wine" / "drive_c" / "Program Files (x86)"
        / "Common Files" / "VST3",
        ctx.home / ".wine" / "drive_c" / "Program Files"
        / "Common Files" / "VST2",
    ]
    for path in vst_dirs:
        ctx.mkdir(path)
    ctl = ctx.which("yabridgectl")
    if not ctl:
        cand = ctx.home / ".local" / "share" / "yabridge" / "yabridgectl"
        if cand.is_file():
            ctl = str(cand)
    if ctl:
        for path in vst_dirs:
            ctx.run([ctl, "add", str(path)])
        code = ctx.run([ctl, "sync"], timeout=180)
        if code == 0:
            ctx.log("OK: Plug-in folders registered with yabridge")
        else:
            ctx.log("WARNING: yabridgectl sync reported a problem - "
                    "bridging may still work; use Re-sync in WinLinx")
            warned.append("plug-in folder sync failed")
    else:
        ctx.log("WARNING: yabridgectl not found - skipping plug-in "
                "folder setup")
        warned.append("yabridgectl not found")
    if warned:
        return StepResult(StepResult.WARN, "; ".join(warned))
    return StepResult(StepResult.DONE)


def step_files(ctx):
    """WinLinx files, menu entry, file associations, watcher."""
    ctx.log("Installing WinLinx itself…")
    dest = ctx.home / ".local" / "share" / "winlinx"
    ctx.mkdir(dest)
    ok = True
    # Same module list as install.sh (cp winlinx/*.py): every module
    # in the winlinx folder ships, so the two install paths cannot
    # drift apart.
    for name in ("__init__.py", "apply_update.py", "common.py",
                 "installer.py", "manager.py", "scanner.py",
                 "updater.py", "watcher.py"):
        ok = ctx.copy(SRC_DIR / "winlinx" / name, dest / name) and ok
    ok = ctx.copy(SRC_DIR / "winlinx" / "run-windows-file",
                  dest / "run-windows-file") and ok
    ctx.chmod_exec(dest / "run-windows-file")
    ctx.copy_tree(SRC_DIR / "winlinx" / "icons", dest / "icons")
    if not ctx.dryrun:
        svg = dest / "icons" / "winlinx.svg"
        if svg.is_file() and not (dest / "icons"
                                  / "winlinx-256.png").is_file():
            if render_icons_from_svg(svg, dest / "icons"):
                ctx.log("OK: App icons rendered from winlinx.svg")
            else:
                ctx.log("WARNING: could not render app icons from "
                        "the SVG - the menu entry will show a "
                        "generic icon")
    ctx.mkdir(dest / "quarantine")

    apps_dir = ctx.home / ".local" / "share" / "applications"
    ctx.mkdir(apps_dir)
    home = str(ctx.home)
    for name in ("winlinx-manager.desktop", "winlinx-runner.desktop"):
        src = SRC_DIR / "desktop" / name
        try:
            text = src.read_text(encoding="utf-8")
        except OSError as exc:
            ctx.log("  !! could not read %s: %s" % (src, exc))
            ok = False
            continue
        ok = ctx.write_text(apps_dir / name,
                            text.replace("@HOME@", home)) and ok
    # Plug-in file types (.vst3 / .dll / .clap) have no standard MIME
    # type, so WinLinx defines its own and registers the runner for
    # them - double-clicking a plug-in bridges it, like the README
    # promises.
    mime_dir = ctx.home / ".local" / "share" / "mime" / "packages"
    ctx.mkdir(mime_dir)
    ok = ctx.copy(SRC_DIR / "desktop" / "winlinx-mime.xml",
                  mime_dir / "winlinx.xml") and ok
    if ctx.which("update-mime-database"):
        ctx.run(["update-mime-database",
                 str(ctx.home / ".local" / "share" / "mime")])
    if ctx.which("xdg-mime"):
        for mime in RUNNER_MIME_TYPES:
            ctx.run(["xdg-mime", "default", "winlinx-runner.desktop",
                     mime])
        ctx.log("OK: Double-clicking .exe/.msi files now runs them "
                "(scanned first), and double-clicking .vst3/.dll/"
                ".clap plug-ins bridges them")
    else:
        ctx.log("WARNING: xdg-mime not found - double-click may not "
                "work; use the watched folders instead")

    ctx.mkdir(ctx.home / "WinLinx" / "Programs")
    ctx.mkdir(ctx.home / "WinLinx" / "Plugins")
    ctx.log("OK: Watched folders ready: ~/WinLinx/Programs and "
            "~/WinLinx/Plugins")

    unit_dir = ctx.home / ".config" / "systemd" / "user"
    ctx.mkdir(unit_dir)
    ok = ctx.copy(SRC_DIR / "systemd" / "winlinx-watcher.service",
                  unit_dir / "winlinx-watcher.service") and ok
    code = ctx.run(["systemctl", "--user", "daemon-reload"])
    code2 = ctx.run(["systemctl", "--user", "enable", "--now",
                     "winlinx-watcher.service"])
    if code == 0 and code2 == 0:
        ctx.log("OK: Watcher service running")
    else:
        ctx.log("WARNING: could not start the watcher service")
    if not ok:
        return StepResult(StepResult.WARN,
                          "some files could not be copied")
    return StepResult(StepResult.DONE)


STEPS = [
    ("wine", "Wine - runs Windows programs", step_wine),
    ("clamav", "ClamAV + virus signatures - scans files first",
     step_clamav),
    ("yabridge", "yabridge - bridges Windows VST plug-ins",
     step_yabridge),
    ("pyside6", "PySide6 - the Program Manager window", step_pyside6),
    ("prefix", "Wine prefix + VST folders", step_prefix),
    ("files", "WinLinx files, menu entry + watcher", step_files),
]


# ---------------------------------------------------------------- worker

class InstallWorker(QThread):
    log_line = Signal(str)
    step_status = Signal(int, str)
    progress = Signal(int)
    finished = Signal(object)  # (list of StepResult, cancelled bool)

    def __init__(self, dryrun=False, parent=None):
        super().__init__(parent)
        self._dryrun = dryrun
        self._ctx = None
        # Set by cancel() even before run() has created the context,
        # so a cancel clicked the instant the install starts is not
        # lost (the old code only set ctx.cancelled when ctx existed).
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True
        if self._ctx is not None:
            self._ctx.cancelled = True

    def run(self):
        ctx = InstallContext(self.log_line.emit, dryrun=self._dryrun)
        ctx.cancelled = self._cancel_requested
        self._ctx = ctx
        results = []
        total = len(STEPS)
        for index, (_key, title, func) in enumerate(STEPS):
            if ctx.cancelled:
                ctx.log("Cancelled - stopping after %d of %d steps"
                        % (index, total))
                break
            self.step_status.emit(index, "running…")
            ctx.log("")
            ctx.log("=== Step %d of %d: %s ===" % (index + 1, total,
                                                   title))
            try:
                result = func(ctx)
            except Exception as exc:  # noqa: BLE001 - warn, carry on
                ctx.log("WARNING: step failed with an error: %s" % exc)
                result = StepResult(StepResult.FAIL, str(exc))
            if ctx.cancelled and result.status == StepResult.FAIL:
                # The step noticed the cancel flag and bailed out:
                # that is a cancellation, not a failure. Report it
                # as cancelled so the run is never called failed
                # for a stop the user asked for.
                result = StepResult(StepResult.CANCELLED,
                                    result.detail)
            results.append(result)
            self.step_status.emit(index, result.status)
            self.progress.emit(int((index + 1) * 100 / total))
        self.finished.emit((results, ctx.cancelled))


# ---------------------------------------------------------------- window

STATUS_LABEL = {
    "pending": "pending",
    "running…": "running…",
    StepResult.DONE: "done",
    StepResult.WARN: "warning",
    StepResult.FAIL: "failed",
    StepResult.CANCELLED: "cancelled",
}


class InstallerWindow(QMainWindow):
    def __init__(self, dryrun=False):
        super().__init__()
        self._dryrun = dryrun
        self._worker = None
        self._results = None
        self._installing = False
        self.setWindowTitle("Install WinLinx")
        self.resize(720, 560)
        self._settings = QSettings("BrokeNBored", "WinLinxInstaller")
        saved = self._settings.value("geometry")
        if saved is not None:
            self.restoreGeometry(saved)
        if ICON_PATH.is_file():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        root = QWidget()
        layout = QVBoxLayout(root)

        header = QHBoxLayout()
        if ICON_PATH.is_file():
            icon_label = QLabel()
            icon_label.setPixmap(QPixmap(str(ICON_PATH)).scaled(
                64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            header.addWidget(icon_label)
        title = QLabel(
            "<b>Install WinLinx %s</b><br>"
            "Windows programs and VST plug-ins, just working on Linux, "
            "no manual setup per file, ever.<br>"
            "This takes a few minutes and will ask for your password "
            "when a step needs it. Safe to run again." % __version__)
        title.setWordWrap(True)
        header.addWidget(title, 1)
        layout.addLayout(header)

        self.steps_list = QListWidget()
        # Not interactive: nothing is ever selectable here (U6).
        # NoSelection alone still leaves row 0 painted as the
        # "current" item, so clear that too and keep focus off it.
        self.steps_list.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self.steps_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for _key, title_text, _func in STEPS:
            item = QListWidgetItem("%s - pending" % title_text)
            self.steps_list.addItem(item)
        self.steps_list.setCurrentRow(-1)
        # Size the list to its content: six rows, no dead space.
        self.steps_list.setFixedHeight(
            self.steps_list.sizeHintForRow(0)
            * self.steps_list.count()
            + 2 * self.steps_list.frameWidth())
        layout.addWidget(self.steps_list)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)

        row = QHBoxLayout()
        self.install_btn = QPushButton("Install")
        self.install_btn.clicked.connect(self._start_install)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel_install)
        self.cancel_btn.setEnabled(False)
        self.launch_btn = QPushButton("Launch WinLinx")
        self.launch_btn.clicked.connect(self._launch_winlinx)
        self.launch_btn.setEnabled(False)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        for btn in (self.install_btn, self.cancel_btn,
                    self.launch_btn, self.close_btn):
            row.addWidget(btn)
        row.addStretch()
        layout.addLayout(row)

        self.setCentralWidget(root)
        self.statusBar().showMessage("Ready - press Install when "
                                     "you are ready.")
        self._log("WinLinx installer. Press Install to begin.")
        if dryrun:
            self._log("DRY RUN - nothing will be installed or changed.")

    # -- logging ------------------------------------------------------

    def _log(self, message):
        self.log_view.appendPlainText(message)
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum())

    # -- install flow ---------------------------------------------------

    def _start_install(self):
        if self._installing:
            return
        self._installing = True
        self._results = None
        self.install_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.launch_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        for index in range(self.steps_list.count()):
            _key, title, _func = STEPS[index]
            self.steps_list.item(index).setText("%s - pending"
                                               % title)
        self.statusBar().showMessage("Installing…")
        self._worker = InstallWorker(dryrun=self._dryrun, parent=self)
        self._worker.log_line.connect(self._log)
        self._worker.step_status.connect(self._on_step_status)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _cancel_install(self):
        if self._worker is not None:
            self._worker.cancel()
            self.statusBar().showMessage(
                "Cancelling after the current step…")
            self.cancel_btn.setEnabled(False)

    def _on_step_status(self, index, status):
        _key, title, _func = STEPS[index]
        label = STATUS_LABEL.get(status, status)
        self.steps_list.item(index).setText("%s - %s" % (title, label))
        if status == "running…":
            # Busy bar while a step runs: a 30-minute apt step
            # must not sit at 0% the whole time.
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)

    def _on_progress(self, value):
        # A step just finished: back to a determinate bar at the
        # completed fraction.
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(value)

    def _on_finished(self, payload):
        results, cancelled = payload
        self._installing = False
        self._worker = None
        self._results = results
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self.install_btn.setEnabled(True)
        # Back to a determinate bar whatever state the run ended in.
        self.progress_bar.setRange(0, 100)
        done = len([r for r in results
                    if r.status in (StepResult.DONE,
                                    StepResult.WARN)])
        total = len(STEPS)
        if cancelled:
            # A cancelled run is never "Installed!" and never 100%:
            # say plainly how far it got and how to finish it, and
            # leave Launch disabled (a half-done install must not
            # offer to launch an app that may not be there yet).
            self.progress_bar.setValue(int(done * 100 / total))
            self.statusBar().showMessage(
                "Install cancelled - %d of %d steps done. Press "
                "Install to finish." % (done, total))
            self._log("")
            self._log("Install cancelled - %d of %d steps done. "
                      "Press Install to finish." % (done, total))
            return
        failed = [r for r in results if r.status == StepResult.FAIL]
        warned = [r for r in results if r.status == StepResult.WARN]
        if not failed:
            self.progress_bar.setValue(100)
            if warned:
                self.statusBar().showMessage(
                    "Installed with %d warning(s) - see the log"
                    % len(warned))
            else:
                self.statusBar().showMessage("Installed!")
            self.launch_btn.setEnabled(True)
            self._log("")
            self._log("Done! Drop Windows programs into "
                      "~/WinLinx/Programs and VST plug-ins into "
                      "~/WinLinx/Plugins - or just double-click any "
                      ".exe/.msi anywhere.")
            self._log("WinLinx Program Manager is in your app menu.")
        else:
            # A failed run never shows a full bar: stop at the
            # completed fraction, so the bar agrees with the message.
            self.progress_bar.setValue(int(done * 100 / total))
            self.statusBar().showMessage(
                "Finished with %d failed step(s) - see the log"
                % len(failed))
            self._log("")
            self._log("Some steps failed. You can press Install again "
                      "- it redoes the failed steps and leaves the "
                      "rest alone.")
            manager = (Path(os.path.expanduser("~")) / ".local"
                       / "share" / "winlinx" / "manager.py")
            if manager.is_file():
                self.launch_btn.setEnabled(True)

    # -- launch ---------------------------------------------------------

    def _launch_winlinx(self):
        manager = (Path(os.path.expanduser("~")) / ".local"
                   / "share" / "winlinx" / "manager.py")
        if not manager.is_file():
            QMessageBox.warning(
                self, "WinLinx is not installed yet",
                "The Program Manager was not found - the install may "
                "not have finished. Press Install and try again.")
            return
        try:
            subprocess.Popen([sys.executable, str(manager)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             start_new_session=True)
            self.statusBar().showMessage("WinLinx Program Manager "
                                         "launched")
        except OSError as exc:
            QMessageBox.warning(self, "Launch failed", str(exc))

    # -- close guard ----------------------------------------------------

    def closeEvent(self, event):
        if self._installing:
            self.statusBar().showMessage(
                "Install in progress - press Cancel to stop after "
                "the current step.")
            event.ignore()
            return
        self._settings.setValue("geometry", self.saveGeometry())
        event.accept()


def main(argv=None):
    args = sys.argv if argv is None else argv
    if "--version" in args or "-V" in args:
        print("WinLinx installer %s" % __version__)
        return 0
    app = QApplication(args)
    dryrun = bool(os.environ.get(DRYRUN_ENV))
    window = InstallerWindow(dryrun=dryrun)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
