#!/usr/bin/env python3
"""Self-update engine for WinLinx. Pure stdlib, no Qt - the manager,
the apply helper, and the tests all use it.

The flow, in plain words:
  1. common.check_for_update asks GitHub for the latest release.
  2. download_and_verify pulls the release zip AND its SHA-256
     sidecar, and refuses the zip unless the checksum matches.
     A release with no sidecar is refused too: never install a
     blind download.
  3. stage_zip unpacks the verified zip and checks it is a real
     WinLinx release of the version it claims to be.
  4. apply_staged swaps the new files into the installed tree,
     keeping a backup of the old ones so a failed apply rolls back
     instead of leaving a half-updated install.
  5. A staged update the user deferred is recorded in pending.json
     and applied at the next launch, before the window opens.

State lives under ~/.local/share/winlinx/updates/:
  <version>/   the downloaded zip and the unpacked tree
  pending.json the deferred update, if any
  backup-*/    the previous version, kept for rollback
  apply.log    the helper's output (the manager also logs to
               layer.log)
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402

# Files the release zip must contain to be a real WinLinx release
# (relative to the winlinx-v<version>/ top folder).
REQUIRED_MEMBERS = (
    "winlinx/manager.py",
    "winlinx/common.py",
    "winlinx/updater.py",
    "winlinx/apply_update.py",
    "winlinx/run-windows-file",
)

# A failed deferred update is retried this many launches, then given
# up on (the log says why) so a broken stage never retries forever.
MAX_PENDING_ATTEMPTS = 3


# ---------------------------------------------------------------- paths

def updates_dir() -> Path:
    return common.layer_dir() / "updates"


def safe_version(version):
    """A release version safe to use as a folder name: the leading
    "v" stripped, anything outside letters/digits/dot/dash/underscore
    rejected. Returns "" for junk."""
    text = str(version or "").strip().lstrip("vV")
    if not text or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", text):
        return ""
    if ".." in text or "/" in text or "\\" in text:
        return ""
    return text


def version_dir(version) -> Path:
    return updates_dir() / safe_version(version)


def pending_path() -> Path:
    return updates_dir() / "pending.json"


# ---------------------------------------------------------------- verify

def sha256_of(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 256)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def parse_sidecar(text, zip_name):
    """Parse sha256sum output ("<hex>  <name>") and return the
    expected hash for zip_name, or None when it is not there."""
    for line in str(text or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        digest, name = parts[0], parts[-1].lstrip("*")
        if name == zip_name and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            return digest.lower()
    return None


def _version_of_common(path):
    """The __version__ line inside a common.py file, or ""."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    return match.group(1) if match else ""


def download_and_verify(zip_url, sha_url, version, zip_name,
                        progress=None):
    """Download the release zip and its SHA-256 sidecar, and refuse
    the zip unless the checksum matches. progress(done, total) is
    called as the zip arrives; returning False cancels the download.
    Returns (ok, message, zip_path). The message is plain words for
    the user; "cancelled" means the user cancelled. Never raises."""
    version = safe_version(version)
    if not version or not zip_url or not zip_name:
        return False, ("This update cannot be checked, so WinLinx "
                       "will not install it."), None
    if not sha_url:
        return False, ("This update is missing its safety check, so "
                       "WinLinx will not install it."), None
    dest = version_dir(version)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False, ("The update could not be saved. Check that "
                       "there is space on the disk and try again."), None
    zip_path = dest / zip_name
    sha_path = dest / (zip_name + ".sha256")
    ok, message = common.download_file(zip_url, zip_path,
                                       progress=progress)
    if not ok:
        if message == "cancelled":
            return False, "cancelled", None
        common.append_log("Update download failed: %s" % message)
        return False, ("The update could not be downloaded. Check "
                       "your internet connection and try again."), None
    ok, message = common.download_file(sha_url, sha_path)
    if not ok:
        common.append_log("Update sidecar download failed: %s" % message)
        try:
            zip_path.unlink()
        except OSError:
            pass
        return False, ("This update is missing its safety check, so "
                       "WinLinx will not install it."), None
    try:
        expected = parse_sidecar(sha_path.read_text(encoding="utf-8"),
                                 zip_name)
    except OSError:
        expected = None
    if expected is None:
        common.append_log("Update sidecar did not name %s" % zip_name)
        for path in (zip_path, sha_path):
            try:
                path.unlink()
            except OSError:
                pass
        return False, ("This update is missing its safety check, so "
                       "WinLinx will not install it."), None
    actual = sha256_of(zip_path)
    if actual != expected:
        common.append_log("Update checksum mismatch for %s "
                          "(expected %s, got %s)"
                          % (zip_name, expected, actual))
        for path in (zip_path, sha_path):
            try:
                path.unlink()
            except OSError:
                pass
        return False, ("The update did not arrive intact, so it was "
                       "not installed. Try again."), None
    common.append_log("Update %s downloaded and checked" % version)
    return True, "", zip_path


def stage_zip(zip_path, version):
    """Unpack a verified zip and check it is a real WinLinx release
    of the version it claims to be. Returns (ok, message, tree_dir)
    where tree_dir is the winlinx-v<version>/ folder. Never raises."""
    version = safe_version(version)
    if not version:
        return False, ("This update cannot be checked, so WinLinx "
                       "will not install it."), None
    top = "winlinx-v%s" % version
    tree_root = version_dir(version) / "tree"
    try:
        if tree_root.exists():
            shutil.rmtree(tree_root)
        tree_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False, ("The update could not be unpacked. Check that "
                       "there is space on the disk and try again."), None
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            missing = [m for m in REQUIRED_MEMBERS
                       if "%s/%s" % (top, m) not in names]
            if missing:
                common.append_log("Update zip is missing: %s"
                                  % ", ".join(missing))
                return False, ("The update file is not a WinLinx "
                               "release, so it was not installed."), None
            # No absolute paths, no ".." escapes, nothing outside
            # the top folder: a hostile zip must not write outside
            # the staged tree. (zipfile has no filter= parameter
            # on Python 3.12, so the check is done by hand.)
            for name in names:
                parts = Path(name).parts
                if not parts or parts[0] != top \
                        or ".." in parts \
                        or os.path.isabs(name):
                    common.append_log("Update zip has an unsafe "
                                      "entry: %s" % name)
                    return False, ("The update file is damaged, so "
                                   "it was not installed."), None
            zf.extractall(tree_root)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        common.append_log("Update zip could not be unpacked: %s" % exc)
        return False, ("The update file could not be opened, so it "
                       "was not installed."), None
    tree = tree_root / top
    staged_version = _version_of_common(tree / "winlinx" / "common.py")
    if staged_version != version:
        common.append_log("Update zip says version %r, expected %r"
                          % (staged_version, version))
        return False, ("The update file is for a different version, "
                       "so it was not installed."), None
    # A syntax error in the new code would leave the user with a
    # manager that cannot start at all: compile-check every module
    # before the update is ever applied.
    for module in sorted((tree / "winlinx").glob("*.py")):
        try:
            compile(module.read_text(encoding="utf-8"),
                    str(module), "exec")
        except (OSError, SyntaxError) as exc:
            common.append_log("Update module %s is damaged: %s"
                              % (module.name, exc))
            return False, ("The update file is damaged, so it was "
                           "not installed."), None
    return True, "", tree


# ---------------------------------------------------------------- apply

def _run_quiet(argv):
    """Run a helper command; return its exit code, or None when the
    tool is missing. Never raises."""
    if not shutil.which(argv[0]):
        return None
    try:
        proc = subprocess.run(argv, timeout=60,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        return proc.returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def _backup_plan(layer):
    """What apply_staged backs up, as (backup_relpath, live_path)
    pairs plus the icons flag. Only files that exist are listed."""
    home = common.home()
    pairs = []
    for name in sorted(p.name for p in layer.glob("*.py")):
        pairs.append(("layer/%s" % name, layer / name))
    if (layer / "run-windows-file").exists():
        pairs.append(("layer/run-windows-file",
                      layer / "run-windows-file"))
    externals = [
        (home / ".local" / "share" / "applications"
         / "winlinx-manager.desktop"),
        (home / ".local" / "share" / "applications"
         / "winlinx-runner.desktop"),
        (home / ".local" / "share" / "mime" / "packages"
         / "winlinx.xml"),
        (home / ".config" / "systemd" / "user"
         / "winlinx-watcher.service"),
    ]
    for path in externals:
        if path.exists():
            pairs.append(("external/%s" % path.name, path))
    return pairs, (layer / "icons").is_dir()


def _write_backup_manifest(backup, pairs, has_icons):
    manifest = {
        "pairs": [[rel, str(live)] for rel, live in pairs],
        "icons": has_icons,
    }
    (backup / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")


def _read_backup_manifest(backup):
    try:
        return json.loads((backup / "manifest.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return {"pairs": [], "icons": False}


def restore_backup(backup_dir):
    """Put a backup back: every backed-up file returns to its live
    spot, and files the update added are removed. Returns True when
    the restore ran (best effort - a missing backup file is logged
    and skipped). Never raises."""
    backup = Path(backup_dir)
    manifest = _read_backup_manifest(backup)
    layer = common.layer_dir()
    restored = set()
    for rel, live in manifest.get("pairs") or []:
        src = backup / rel
        dest = Path(live)
        try:
            if src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                restored.add(dest.name)
        except OSError as exc:
            common.append_log("Rollback could not restore %s: %s"
                              % (dest, exc))
    if manifest.get("icons"):
        src = backup / "icons"
        dest = layer / "icons"
        try:
            if src.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src, dest)
        except OSError as exc:
            common.append_log("Rollback could not restore icons: %s"
                              % exc)
    # Files the update added (not in the backup) go away again.
    for path in layer.glob("*.py"):
        if path.name not in restored:
            try:
                path.unlink()
            except OSError:
                pass
    if (layer / "run-windows-file").exists() and \
            "run-windows-file" not in restored:
        try:
            (layer / "run-windows-file").unlink()
        except OSError:
            pass
    _run_quiet(["systemctl", "--user", "daemon-reload"])
    _run_quiet(["systemctl", "--user", "try-restart",
                "winlinx-watcher.service"])
    return True


def prune_backups(keep=1):
    """Keep only the newest `keep` backup folders."""
    try:
        backups = sorted(
            (p for p in updates_dir().glob("backup-*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in backups[keep:]:
        try:
            shutil.rmtree(old)
        except OSError:
            pass


def apply_staged(tree_dir, version):
    """Swap a staged release into the installed tree, keeping a
    backup of the old files so a failed apply rolls back. Also
    refreshes the menu entries, the file associations, and the
    watcher service, and restarts the watcher so it runs the new
    code. Returns (ok, message, backup_dir). The message is plain
    words. Never raises."""
    version = safe_version(version)
    tree = Path(tree_dir)
    layer = common.layer_dir()
    if not version or not (tree / "winlinx" / "manager.py").is_file():
        return False, ("The staged update is incomplete, so it was "
                       "not installed."), None
    try:
        updates_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        return False, ("The update could not be saved. Check that "
                       "there is space on the disk and try again."), None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = updates_dir() / ("backup-%s-%s"
                              % (common.__version__, stamp))
    try:
        backup.mkdir(parents=True, exist_ok=True)
        pairs, has_icons = _backup_plan(layer)
        for rel, live in pairs:
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live, dest)
        if has_icons:
            shutil.copytree(layer / "icons", backup / "icons")
        _write_backup_manifest(backup, pairs, has_icons)
    except OSError as exc:
        common.append_log("Update backup failed: %s" % exc)
        return False, ("The update could not be installed - the "
                       "current version is untouched."), None
    try:
        staged_modules = sorted(
            p.name for p in (tree / "winlinx").glob("*.py"))
        for name in staged_modules:
            shutil.copy2(tree / "winlinx" / name, layer / name)
        # Modules the new version dropped must not linger.
        for path in layer.glob("*.py"):
            if path.name not in staged_modules:
                path.unlink()
        runner = tree / "winlinx" / "run-windows-file"
        shutil.copy2(runner, layer / "run-windows-file")
        os.chmod(layer / "run-windows-file", 0o755)
        icons_src = tree / "winlinx" / "icons"
        if icons_src.is_dir():
            if (layer / "icons").exists():
                shutil.rmtree(layer / "icons")
            shutil.copytree(icons_src, layer / "icons")
        home = common.home()
        apps_dir = home / ".local" / "share" / "applications"
        for name in ("winlinx-manager.desktop",
                     "winlinx-runner.desktop"):
            src = tree / "desktop" / name
            if src.is_file():
                apps_dir.mkdir(parents=True, exist_ok=True)
                text = src.read_text(encoding="utf-8")
                (apps_dir / name).write_text(
                    text.replace("@HOME@", str(home)),
                    encoding="utf-8")
        mime_src = tree / "desktop" / "winlinx-mime.xml"
        if mime_src.is_file():
            mime_dir = home / ".local" / "share" / "mime" / "packages"
            mime_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(mime_src, mime_dir / "winlinx.xml")
            _run_quiet(["update-mime-database",
                        str(home / ".local" / "share" / "mime")])
        service_src = tree / "systemd" / "winlinx-watcher.service"
        if service_src.is_file():
            unit_dir = home / ".config" / "systemd" / "user"
            unit_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(service_src,
                         unit_dir / "winlinx-watcher.service")
            _run_quiet(["systemctl", "--user", "daemon-reload"])
            _run_quiet(["systemctl", "--user", "try-restart",
                        "winlinx-watcher.service"])
    except OSError as exc:
        common.append_log("Update apply failed, rolling back: %s" % exc)
        restore_backup(backup)
        return False, ("The update could not be installed - the "
                       "current version is untouched."), None
    prune_backups(keep=1)
    common.append_log("Update applied: WinLinx %s" % version)
    return True, "", backup


# ---------------------------------------------------------------- pending

def write_pending(version, tree_dir):
    """Record a staged update to apply at the next launch. Stale
    staged versions (not backups) are pruned first."""
    version = safe_version(version)
    if not version:
        return False
    try:
        updates_dir().mkdir(parents=True, exist_ok=True)
        for old in updates_dir().iterdir():
            if old.is_dir() and old.name != safe_version(version) \
                    and not old.name.startswith("backup-"):
                shutil.rmtree(old, ignore_errors=True)
        pending_path().write_text(json.dumps(
            {"version": version, "tree": str(tree_dir),
             "attempts": 0,
             "time": datetime.now().isoformat(timespec="seconds")},
            indent=2), encoding="utf-8")
    except OSError as exc:
        common.append_log("Could not record the deferred update: %s"
                          % exc)
        return False
    return True


def read_pending():
    """The deferred update, or None. A record whose staged tree is
    gone is cleared (nothing left to apply). Never raises."""
    try:
        data = json.loads(pending_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("version") \
            or not data.get("tree"):
        return None
    if not Path(data["tree"]).is_dir():
        common.append_log("Deferred update %s has no staged files "
                          "left - dropping it" % data.get("version"))
        clear_pending()
        return None
    return data


def clear_pending():
    try:
        pending_path().unlink()
    except OSError:
        pass


def apply_pending():
    """Apply the deferred update, if any. Returns (version, message,
    backup_dir): version is set when an update was applied, message
    is plain words when one failed. Never raises."""
    pending = read_pending()
    if pending is None:
        return None, "", None
    version = safe_version(pending.get("version"))
    ok, message, backup = apply_staged(pending["tree"], version)
    if ok:
        clear_pending()
        shutil.rmtree(version_dir(version), ignore_errors=True)
        return version, "", backup
    attempts = int(pending.get("attempts") or 0) + 1
    if attempts >= MAX_PENDING_ATTEMPTS:
        common.append_log("Deferred update %s failed %d times - "
                          "giving up on it" % (version, attempts))
        clear_pending()
        shutil.rmtree(version_dir(version), ignore_errors=True)
        return None, message, None
    try:
        pending["attempts"] = attempts
        pending_path().write_text(json.dumps(pending, indent=2),
                                  encoding="utf-8")
    except OSError:
        pass
    return None, message, None


# ---------------------------------------------------------------- helper

def helper_argv(tree_dir, wait_pid, manager_path):
    """The command line that runs the apply helper from the staged
    tree: it waits for the manager to exit, applies the update, and
    restarts the manager."""
    helper = Path(tree_dir) / "winlinx" / "apply_update.py"
    return [sys.executable, str(helper),
            "--wait-pid", str(wait_pid),
            "--manager", str(manager_path)]
