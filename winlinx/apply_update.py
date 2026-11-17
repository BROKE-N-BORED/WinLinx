#!/usr/bin/env python3
"""WinLinx update helper: applies a staged update after the manager
exits, then restarts the manager.

Run detached by the manager when the user picks "Restart Now" - not
for direct use. Stdlib only, no Qt.

  1. Wait for the manager process (--wait-pid) to exit, so no
     WinLinx code is running while its files are swapped.
  2. Apply the deferred update (updater.apply_pending). If the
     apply fails, the old version is already rolled back in place.
  3. Relaunch the manager. If it crashes at once (a bad update),
     roll back to the backup and relaunch the old version instead.
     A clean exit - the user closing the window - is not a crash.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import updater  # noqa: E402

# How long to wait for the manager to exit before giving up (the
# update stays staged and applies at the next launch instead).
WAIT_TIMEOUT = 60
# How long to watch the relaunched manager for an instant crash.
CRASH_WATCH = 5


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _relaunch(manager_path):
    return subprocess.Popen(
        [sys.executable, str(manager_path)],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv=None):
    args = sys.argv if argv is None else argv
    if "--version" in args or "-V" in args:
        print("WinLinx Update Helper %s" % common.__version__)
        return 0
    wait_pid = None
    manager_path = None
    rest = args[1:]
    while rest:
        flag, *rest = rest
        if flag == "--wait-pid" and rest:
            try:
                wait_pid = int(rest[0])
            except ValueError:
                wait_pid = None
            rest = rest[1:]
        elif flag == "--manager" and rest:
            manager_path = rest[0]
            rest = rest[1:]
    if wait_pid is None or not manager_path:
        print("usage: apply_update.py --wait-pid <pid> "
              "--manager <manager.py>", file=sys.stderr)
        return 2
    deadline = time.time() + WAIT_TIMEOUT
    while _pid_alive(wait_pid) and time.time() < deadline:
        time.sleep(0.2)
    if _pid_alive(wait_pid):
        common.append_log("Update helper: the manager did not exit, "
                          "so the update stays staged for next time")
        return 1
    version, message, backup = updater.apply_pending()
    if version:
        common.append_log("Update applied by helper: now running %s"
                          % version)
    elif message:
        common.append_log("Update could not be applied: %s" % message)
    proc = _relaunch(manager_path)
    try:
        code = proc.wait(timeout=CRASH_WATCH)
    except subprocess.TimeoutExpired:
        return 0
    if code != 0 and backup is not None:
        common.append_log("Relaunched manager exited at once (code "
                          "%s) - rolling back to the previous version"
                          % code)
        updater.restore_backup(backup)
        _relaunch(manager_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
