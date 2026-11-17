#!/usr/bin/env python3
"""WinLinx watcher.

Polls ~/WinLinx/Programs and ~/WinLinx/Plugins (stdlib only,
no inotify dependency) and makes dropped files Just Work:

  Programs folder:  .exe / .msi  -> scanned, then run with Wine
  Plugins folder:   .exe / .msi  -> scanned, installed with Wine,
                                   then yabridgectl sync
                    .vst3 / .dll / .clap -> scanned, copied to the
                                   matching Wine VST folder,
                                   then sync
  Dropped folders:  scanned; a .vst3 bundle in Plugins is bridged,
                    anything else is recorded as a failed drop so it
                    shows in the Program Manager instead of vanishing.
  Unsupported files (.zip, .txt, ...): logged and recorded as failed
                    with a plain reason, so they show in Failed Drops
                    too. They are never retried automatically: a retry
                    cannot make an unsupported file supported.

Nothing runs before it is scanned. A flagged file is moved to
quarantine instead - it is never installed or executed. The user
decides its fate in the Program Manager (Allow / Delete).

If ClamAV is missing or a scan errors, the file still goes through but
the log says so in plain words (warn-never-refuse: a missing scanner
must not brick WinLinx).

A file that fails (no Wine, a copy error, a failed sync) is recorded
as failed with the real reason - never as handled - and retried on
later passes with backoff. Failed drops show in the Program Manager
with a Retry button.

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
PLUGIN_EXTS = {".exe", ".msi", ".vst3", ".dll", ".clap"}
# Retry backoff for failed items, in seconds (attempt 1, 2, 3, 4+).
RETRY_BACKOFF = (60, 300, 1800, 3600)

# Plain reasons for files and folders WinLinx cannot handle. No tool
# names, no em-dashes: these strings show in Failed Drops.
UNSUPPORTED_ZIP_REASON = (
    "Unzip it first - WinLinx handles .exe, .msi, .vst3, .dll and "
    ".clap files")
UNSUPPORTED_REASON = (
    "WinLinx handles .exe, .msi, .vst3, .dll and .clap files")
FOLDER_REASON = (
    "WinLinx cannot open folders - open it and double-click the "
    "program inside to run it")
FOLDER_VST3_IN_PROGRAMS_REASON = (
    "Plug-ins go in the Plugins folder - move this folder to "
    "~/WinLinx/Plugins")


def _unsupported_reason(path):
    if path.suffix.lower() == ".zip":
        return UNSUPPORTED_ZIP_REASON
    return UNSUPPORTED_REASON


def _folder_reason(path, kind):
    if path.name.lower().endswith(".vst3") and kind == "program":
        return FOLDER_VST3_IN_PROGRAMS_REASON
    return FOLDER_REASON


def _plain_sync_reason(message):
    """User-facing reason for a failed bridge sync, in plain words:
    the tool name never reaches anything the user reads (Failed
    Drops, notifications). Delegates to the shared mapper in common
    when it exists, so the watcher, the manager, and the runner all
    say the same thing; the local fallback covers an older common."""
    text = message or ""
    mapper = getattr(common, "plain_drop_reason", None)
    if mapper is not None:
        mapped = mapper(text)
        if mapped != text:
            return mapped
    if text.startswith("yabridge is not installed"):
        return text
    if "yabridgectl" in text:
        return ("yabridge is not installed, so the plug-in could not "
                "be bridged")
    if "bridge sync failed" in text.lower():
        return "the plug-in folders could not be re-synced"
    if text:
        return "bridge sync failed (%s)" % text
    return "bridge sync failed"


# ---------------------------------------------------------------- logging

def log(message):
    # common.append_log writes the rotating log and feeds the ring
    # buffer; stdout goes to the systemd journal.
    line = common.append_log(message)
    print(line, flush=True)


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
    the user Allows a quarantined item back, or presses Retry on a
    failed drop)."""
    state = _load_state()
    if str(path) in state:
        del state[str(path)]
        _save_state(state)


def ignore_path(path):
    """Mark a path ignored: the watcher will not handle it again
    unless the file changes. Used by the Failed Drops view."""
    state = _load_state()
    key = str(path)
    record = state.get(key) or {}
    try:
        st = Path(path).stat()
        record["size"] = st.st_size
        record["mtime"] = st.st_mtime_ns
    except OSError:
        pass
    record["action"] = "ignored"
    record["time"] = datetime.now().isoformat(timespec="seconds")
    state[key] = record
    _save_state(state)


def load_failed():
    """Failed drops, newest first: [{path, name, reason, attempts,
    time, next_retry}]. The manager's Failed Drops tab reads this."""
    state = _load_state()
    out = []
    for key, record in state.items():
        if not isinstance(record, dict):
            continue
        if record.get("action") != "failed":
            continue
        out.append({
            "path": key,
            "name": Path(key).name,
            "reason": record.get("reason", "unknown error"),
            "attempts": record.get("attempts", 1),
            "time": record.get("time", ""),
            "next_retry": record.get("next_retry", 0),
        })
    out.sort(key=lambda e: e["time"], reverse=True)
    return out


def _backoff_seconds(attempts):
    index = min(max(attempts - 1, 0), len(RETRY_BACKOFF) - 1)
    return RETRY_BACKOFF[index]


# ---------------------------------------------------------------- execution

class Executor:
    """Runs external commands. A tiny seam so tests can stub it."""

    def run_detached(self, cmd, env=None):
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL,
                                env=env, start_new_session=True)

    def run_wait(self, cmd, timeout=None, env=None):
        try:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  stdin=subprocess.DEVNULL,
                                  env=env, timeout=timeout)
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
        # First-attempt failures waiting to be notified, grouped by
        # reason so one drop produces one notification per reason.
        # They are flushed at the start of the next scan pass (and
        # by --once before it exits): no timer thread, so nothing
        # can fire after the watcher is gone.
        self._pending_failures = {}
        self._pending_lock = threading.Lock()

    # -- public -----------------------------------------------------

    def scan_once(self):
        # Send the batched failure notifications queued since the
        # last pass before looking for new work. A slow worker that
        # has not finished yet reports at a later pass.
        self._flush_failure_notifications()
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
                if entry.name.startswith("."):
                    continue  # hidden files are not drops
                # Every file is considered, not just allow-listed
                # extensions: unsupported files are logged and
                # recorded as failed instead of vanishing silently.
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
            if record.get("action") == "failed":
                if record.get("permanent"):
                    # It will never succeed by retrying (an
                    # unsupported file, a folder): leave it for the
                    # user to Forget, or Retry after changing it.
                    return
                if time.time() < record.get("next_retry", 0):
                    return  # not time to retry yet
                # Retry time: the file is unchanged, so it is stable
                # by definition - skip the stability gate.
                self._dispatch(path, kind, is_dir, fingerprint)
                return
            return  # already handled (or ignored), unchanged
        if self._seen.get(key) != fingerprint:
            # First sighting, or still changing (copy in progress).
            # Wait until it is stable across two polls.
            self._seen[key] = fingerprint
            return
        self._dispatch(path, kind, is_dir, fingerprint)

    def _dispatch(self, path, kind, is_dir, fingerprint):
        key = str(path)
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

    def _mark_handled(self, path, fingerprint, action, quiet=False):
        with self._lock:
            old = self._handled.get(str(path)) or {}
            was_failed = old.get("action") == "failed"
            self._handled[str(path)] = {
                "size": fingerprint[0],
                "mtime": fingerprint[1],
                "action": action,
                "time": datetime.now().isoformat(timespec="seconds"),
            }
            _save_state(self._handled)
        if was_failed and not quiet:
            # It works now after failing before: say so, once.
            common.notify("WinLinx", "%s is working now."
                          % path.name)

    def _mark_failed(self, path, fingerprint, reason, permanent=False):
        """Record the real outcome: failed, with the reason, an
        attempt count, and the next retry time. Never recorded as
        handled. A permanent failure (an unsupported file, a folder)
        is never retried automatically: only a change to the file,
        or the user's Retry, brings it back."""
        with self._lock:
            old = self._handled.get(str(path)) or {}
            attempts = int(old.get("attempts", 0)) + 1
            record = {
                "size": fingerprint[0],
                "mtime": fingerprint[1],
                "action": "failed",
                "reason": reason,
                "attempts": attempts,
                "next_retry": time.time() + _backoff_seconds(attempts),
                "time": datetime.now().isoformat(timespec="seconds"),
            }
            if permanent:
                record["permanent"] = True
            self._handled[str(path)] = record
            _save_state(self._handled)
        if attempts == 1:
            self._queue_failure_notification(path.name, reason)

    # -- batched failure notifications -------------------------------

    def _queue_failure_notification(self, name, reason):
        """Hold a first-attempt failure so same-reason failures from
        one drop land as one notification ("3 files could not be
        handled: Wine is not installed") instead of a stack of
        popups. Flushed at the start of the next scan pass."""
        with self._pending_lock:
            self._pending_failures.setdefault(reason, []).append(name)

    def _flush_failure_notifications(self):
        """Send one notification per pending failure reason, each
        pointing at the Program Manager. Called at the start of
        every scan pass, and by --once before it exits."""
        with self._pending_lock:
            pending = self._pending_failures
            self._pending_failures = {}
        for reason, names in sorted(pending.items()):
            if len(names) == 1:
                message = ("%s could not be handled: %s - see the "
                           "WinLinx Program Manager."
                           % (names[0], reason))
            else:
                message = ("%d files could not be handled: %s - see "
                           "the WinLinx Program Manager."
                           % (len(names), reason))
            common.notify("WinLinx", message)

    def _handle(self, path, kind, is_dir, fingerprint):
        # 0. Unsupported files: never scanned, never run. Logged and
        # recorded as failed so they show in Failed Drops instead of
        # vanishing silently (a .zip is the most common way VSTs and
        # programs are distributed).
        if not is_dir:
            allowed = PROGRAM_EXTS if kind == "program" else PLUGIN_EXTS
            if path.suffix.lower() not in allowed:
                reason = _unsupported_reason(path)
                log("Unsupported file dropped: %s - %s"
                    % (path.name, reason))
                self._mark_failed(path, fingerprint, reason,
                                  permanent=True)
                return

        # 1. Scan gate: nothing runs before it is scanned.
        scanned_clean = False
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
                common.notify(
                    "WinLinx",
                    "%s was quarantined and did not run: %s"
                    % (path.name, ", ".join(names)))
                return
            else:
                log("Scan clean: %s" % path.name)
                scanned_clean = True

        # 2. Normal handling. Helpers report (ok, reason); a failure
        # is recorded as failed, never as handled.
        if is_dir:
            if (kind == "plugin"
                    and path.name.lower().endswith(".vst3")):
                ok, reason = self._copy_plugin_dir(path,
                                                   common.vst3_dir())
                if not ok:
                    self._mark_failed(path, fingerprint, reason)
                    return
                ok, reason = self._sync()
                if not ok:
                    self._mark_failed(path, fingerprint, reason)
                    return
                self._mark_handled(path, fingerprint, "bridged-vst3-dir")
            else:
                # A dropped folder is not ignored: it is recorded as
                # a failed drop so the manager shows it, with what to
                # do next. Only claim a scan ran when one did.
                if scanned_clean:
                    log("Folder scanned clean, left in place: %s "
                        "(open it and double-click the program inside "
                        "to run it)" % path.name)
                else:
                    log("Folder left in place: %s (open it and "
                        "double-click the program inside to run it)"
                        % path.name)
                self._mark_failed(path, fingerprint,
                                  _folder_reason(path, kind),
                                  permanent=True)
            return

        ext = path.suffix.lower()
        if kind == "program":
            ok, reason = self._run_program(path)
            if ok:
                self._mark_handled(path, fingerprint, "ran")
            else:
                self._mark_failed(path, fingerprint, reason)
            return
        if ext in (".exe", ".msi"):
            ok, reason = self._run_installer_then_sync(path)
            if ok:
                self._mark_handled(path, fingerprint, "installed+synced")
            elif reason == "sync failed":
                # The installer itself ran: do not re-run it on retry.
                # The plug-in folders can be re-synced from the
                # manager instead - but say so once, or the plug-in
                # is silently missing from the DAW.
                self._mark_handled(path, fingerprint, "installed",
                                   quiet=True)
                log("WARNING: %s installed but the plug-in folders "
                    "could not be re-synced" % path.name)
                common.notify(
                    "WinLinx",
                    "%s installed, but the plug-in folders could not "
                    "be re-synced - press Re-sync Bridges in the "
                    "Program Manager" % path.name)
            else:
                self._mark_failed(path, fingerprint, reason)
        elif ext == ".vst3":
            self._handle_plugin_file(path, fingerprint,
                                     common.vst3_dir(), "bridged-vst3")
        elif ext == ".dll":
            self._handle_plugin_file(path, fingerprint,
                                     common.vst2_dir(), "bridged-vst2")
        elif ext == ".clap":
            self._handle_plugin_file(path, fingerprint,
                                     common.vst3_dir(), "bridged-clap")

    def _handle_plugin_file(self, path, fingerprint, dest_dir, action):
        ok, reason = self._copy_plugin(path, dest_dir)
        if not ok:
            self._mark_failed(path, fingerprint, reason)
            return
        ok, reason = self._sync()
        if not ok:
            # Re-copying is harmless, so a sync failure is retryable.
            self._mark_failed(path, fingerprint, reason)
            return
        self._mark_handled(path, fingerprint, action)

    def _run_program(self, path):
        if not common.find_wine():
            reason = "Wine is not installed"
            log("WARNING: Wine is not installed, cannot run %s"
                % path.name)
            return False, reason
        log("Running: %s" % path.name)
        try:
            self.executor.run_detached(
                ["wine", str(path)], env=common.launch_env(str(path)))
        except OSError as exc:
            return False, str(exc)
        return True, ""

    def _run_installer_then_sync(self, path):
        if not common.find_wine():
            reason = "Wine is not installed"
            log("WARNING: Wine is not installed, cannot install %s"
                % path.name)
            return False, reason
        log("Installing: %s (this can take a while)" % path.name)
        code = self.executor.run_wait(["wine", str(path)],
                                      timeout=INSTALLER_TIMEOUT)
        if code is None:
            log("WARNING: installer %s timed out" % path.name)
            return False, "installer timed out"
        log("Installer finished: %s (exit %s)" % (path.name, code))
        ok, _reason = self._sync()
        if not ok:
            return False, "sync failed"
        return True, ""

    def _copy_plugin(self, path, dest_dir):
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(path), str(dest_dir / path.name))
            log("Copied %s to %s" % (path.name, dest_dir))
            return True, ""
        except OSError as exc:
            reason = "copy failed: %s" % exc
            log("ERROR copying %s: %s" % (path.name, exc))
            return False, reason

    def _copy_plugin_dir(self, path, dest_dir):
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / path.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(str(path), str(target))
            log("Copied %s to %s" % (path.name, dest_dir))
            return True, ""
        except OSError as exc:
            reason = "copy failed: %s" % exc
            log("ERROR copying %s: %s" % (path.name, exc))
            return False, reason

    def _sync(self):
        ok, message = self.executor.sync_bridges()
        if ok:
            log("Bridges re-synced")
            return True, ""
        reason = _plain_sync_reason(message)
        log("WARNING: %s" % reason)
        return False, reason


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
    parser.add_argument("--version", action="version",
                        version="WinLinx watcher %s"
                        % common.__version__)
    parser.add_argument("--prefix-for", metavar="EXE",
                        help="print the per-program prefix path for "
                        "an .exe name (empty = shared prefix) and exit")
    args = parser.parse_args(argv)
    if args.prefix_for:
        print(common.prefix_for_exe(args.prefix_for) or "")
        return 0
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
        # The next scan pass would flush these; --once has no next
        # pass, so flush before exiting or the notifications would
        # silently never be sent.
        watcher._flush_failure_notifications()
        return 0
    watcher.run_forever(interval=args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
