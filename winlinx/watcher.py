#!/usr/bin/env python3
"""WinLinx watcher.

Polls ~/WinLinx/Programs and ~/WinLinx/Plugins (stdlib only,
no inotify dependency) and makes dropped files Just Work:

  Programs folder:  .exe / .msi  -> scanned, then run with Wine
  Plugins folder:   .exe / .msi  -> scanned, installed with Wine,
                                   then yabridgectl sync
                    .vst3 / .dll -> scanned, copied to the matching
                                   Wine VST folder, then sync
  Dropped folders:  scanned; a .vst3 bundle in Plugins is bridged,
                    anything else is left in place with a log note.

Nothing runs before it is scanned. A flagged file is moved to
quarantine instead — it is never installed or executed. The user
decides its fate in the Program Manager (Allow / Delete).

If ClamAV is missing or a scan errors, the file still goes through but
the log says so in plain words (warn-never-refuse: a missing scanner
must not brick WinLinx).

State lives in ~/.local/share/winlinx/handled.json so a file is
never handled twice; logs go to layer.log in the same folder.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
import scanner

POLL_SECONDS = 5
INSTALLER_TIMEOUT = 1800  # 30 min; installers are GUI apps, be patient
PROGRAM_EXTS = {".exe", ".msi"}
PLUGIN_EXTS = {".exe", ".msi", ".vst3", ".dll"}


# ---------------------------------------------------------------- logging

def log(message):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (stamp, message)
    print(line, flush=True)
    try:
        path = common.log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- handled state

def _load_state():
    try:
        with open(common.state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    path = common.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


def clear_handled(path):
    """Forget a path so the watcher will handle it again (used after
    the user Allows a quarantined item back)."""
    state = _load_state()
    if str(path) in state:
        del state[str(path)]
        _save_state(state)


# ---------------------------------------------------------------- execution

class Executor:
    """Runs external commands. A tiny seam so tests can stub it."""

    def run_detached(self, cmd):
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL,
                                start_new_session=True)

    def run_wait(self, cmd, timeout=None):
        try:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  stdin=subprocess.DEVNULL,
                                  timeout=timeout)
            return proc.returncode
        except subprocess.TimeoutExpired:
            return None

    def sync_bridges(self):
        return common.run_yabridgectl_sync()


# ---------------------------------------------------------------- watcher

class Watcher:
    def __init__(self, executor=None):
        self.executor = executor or Executor()
        self._seen = {}       # path -> fingerprint, for stability checks
        self._busy = set()    # paths currently being handled
        self._lock = threading.Lock()
        self._handled = _load_state()

    # -- public -----------------------------------------------------

    def scan_once(self):
        self._scan_folder(common.programs_folder(), "program")
        self._scan_folder(common.plugins_folder(), "plugin")

    def run_forever(self, interval=POLL_SECONDS):
        log("Watcher started (polling every %ds)" % interval)
        while True:
            try:
                self.scan_once()
            except Exception as exc:  # never die on a scan error
                log("ERROR during scan: %s" % exc)
            time.sleep(interval)

    # -- scanning ---------------------------------------------------

    def _scan_folder(self, folder, kind):
        if not folder.is_dir():
            return
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink() and not entry.exists():
                continue
            if entry.is_dir():
                self._consider(entry, kind, is_dir=True)
            elif entry.is_file():
                allowed = PROGRAM_EXTS if kind == "program" else PLUGIN_EXTS
                if entry.suffix.lower() in allowed:
                    self._consider(entry, kind, is_dir=False)

    def _fingerprint(self, path, is_dir):
        try:
            st = path.stat()
        except OSError:
            return None
        if is_dir:
            return (0, st.st_mtime_ns)
        return (st.st_size, st.st_mtime_ns)

    def _consider(self, path, kind, is_dir):
        key = str(path)
        fingerprint = self._fingerprint(path, is_dir)
        if fingerprint is None:
            return
        record = self._handled.get(key)
        if (record and record.get("size") == fingerprint[0]
                and record.get("mtime") == fingerprint[1]):
            return  # already handled, unchanged
        if self._seen.get(key) != fingerprint:
            # First sighting, or still changing (copy in progress).
            # Wait until it is stable across two polls.
            self._seen[key] = fingerprint
            return
        with self._lock:
            if key in self._busy:
                return
            self._busy.add(key)
        thread = threading.Thread(
            target=self._handle_safe,
            args=(path, kind, is_dir, fingerprint), daemon=True)
        thread.start()

    # -- handling ---------------------------------------------------

    def _handle_safe(self, path, kind, is_dir, fingerprint):
        try:
            self._handle(path, kind, is_dir, fingerprint)
        except Exception as exc:
            log("ERROR handling %s: %s" % (path.name, exc))
        finally:
            with self._lock:
                self._busy.discard(str(path))

    def _mark_handled(self, path, fingerprint, action):
        with self._lock:
            self._handled[str(path)] = {
                "size": fingerprint[0],
                "mtime": fingerprint[1],
                "action": action,
                "time": datetime.now().isoformat(timespec="seconds"),
            }
            _save_state(self._handled)

    def _handle(self, path, kind, is_dir, fingerprint):
        # 1. Scan gate: nothing runs before it is scanned.
        if scanner.is_allowlisted(path):
            log("Allowlisted, skipping scan: %s" % path.name)
        else:
            result = scanner.scan_path(path)
            if result.get("missing") or result.get("error"):
                log("WARNING: %s went through UNSCANNED (%s)"
                    % (path.name, result.get("error")))
            elif not result["clean"]:
                names = sorted({name for _, name in result["threats"]})
                log("QUARANTINED %s: %s" % (path.name, ", ".join(names)))
                scanner.quarantine_item(path, result["threats"])
                self._mark_handled(path, fingerprint, "quarantined")
                return
            else:
                log("Scan clean: %s" % path.name)

        # 2. Normal handling.
        if is_dir:
            if (kind == "plugin"
                    and path.name.lower().endswith(".vst3")):
                self._copy_plugin_dir(path, common.vst3_dir())
                self._sync()
                self._mark_handled(path, fingerprint, "bridged-vst3-dir")
            else:
                log("Folder scanned clean, left in place: %s "
                    "(open it and double-click the program inside to run it)"
                    % path.name)
                self._mark_handled(path, fingerprint, "folder-scanned")
            return

        ext = path.suffix.lower()
        if kind == "program":
            self._run_program(path)
            self._mark_handled(path, fingerprint, "ran")
            return
        if ext in (".exe", ".msi"):
            self._run_installer_then_sync(path)
            self._mark_handled(path, fingerprint, "installed+synced")
        elif ext == ".vst3":
            self._copy_plugin(path, common.vst3_dir())
            self._sync()
            self._mark_handled(path, fingerprint, "bridged-vst3")
        elif ext == ".dll":
            self._copy_plugin(path, common.vst2_dir())
            self._sync()
            self._mark_handled(path, fingerprint, "bridged-vst2")

    def _run_program(self, path):
        if not common.find_wine():
            log("WARNING: Wine is not installed, cannot run %s" % path.name)
            return
        log("Running: %s" % path.name)
        self.executor.run_detached(["wine", str(path)])

    def _run_installer_then_sync(self, path):
        if not common.find_wine():
            log("WARNING: Wine is not installed, cannot install %s"
                % path.name)
            return
        log("Installing: %s (this can take a while)" % path.name)
        code = self.executor.run_wait(["wine", str(path)],
                                      timeout=INSTALLER_TIMEOUT)
        if code is None:
            log("WARNING: installer %s timed out, syncing anyway"
                % path.name)
        else:
            log("Installer finished: %s (exit %s)" % (path.name, code))
        self._sync()

    def _copy_plugin(self, path, dest_dir):
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(path), str(dest_dir / path.name))
            log("Copied %s to %s" % (path.name, dest_dir))
        except OSError as exc:
            log("ERROR copying %s: %s" % (path.name, exc))

    def _copy_plugin_dir(self, path, dest_dir):
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / path.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(str(path), str(target))
            log("Copied %s to %s" % (path.name, dest_dir))
        except OSError as exc:
            log("ERROR copying %s: %s" % (path.name, exc))

    def _sync(self):
        ok, message = self.executor.sync_bridges()
        if ok:
            log("Bridges re-synced")
        else:
            log("WARNING: bridge sync failed (%s)" % message)


# ---------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="WinLinx watcher")
    parser.add_argument("--once", action="store_true",
                        help="run one scan pass and exit")
    parser.add_argument("--rescan", action="store_true",
                        help="forget handled files, then scan again")
    parser.add_argument("--interval", type=int, default=POLL_SECONDS,
                        help="poll interval in seconds (default %d)"
                        % POLL_SECONDS)
    args = parser.parse_args(argv)
    if args.rescan:
        try:
            common.state_path().unlink()
        except OSError:
            pass
        log("Handled history cleared (--rescan)")
    watcher = Watcher()
    if args.once:
        # Two passes in ONE process: the stability gate needs to see a
        # file unchanged twice before handling it, and a fresh process
        # has no memory of the first sighting.
        watcher.scan_once()
        time.sleep(1.0)
        watcher.scan_once()
        # Wait for worker threads (installers can take a while).
        deadline = time.time() + 300
        while watcher._busy and time.time() < deadline:
            time.sleep(0.2)
        time.sleep(0.5)
        return 0
    watcher.run_forever(interval=args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
