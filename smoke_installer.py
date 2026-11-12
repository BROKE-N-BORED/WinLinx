#!/usr/bin/env python3
"""Smoke-test the WinLinx installer window offscreen (dry run).

Launches the window, screenshots it, clicks Install (dry run — no
privileged commands, no writes), waits for the worker to finish,
screenshots the done state. Screenshots land in /tmp/winlinx-installer-*.png

Usage:
  QT_QPA_PLATFORM=offscreen LD_LIBRARY_PATH=... \\
    ~/workspace/forge-v2-testenv/bin/python smoke_installer.py
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["WINLINX_INSTALLER_DRYRUN"] = "1"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "winlinx"))

from PySide6.QtWidgets import QApplication  # noqa: E402
import installer  # noqa: E402


def main():
    app = QApplication(sys.argv)
    window = installer.InstallerWindow(dryrun=True)
    window.show()
    app.processEvents()
    time.sleep(0.3)
    app.processEvents()
    window.grab().save("/tmp/winlinx-installer-before.png")
    print("saved /tmp/winlinx-installer-before.png")
    print("steps:", [window.steps_list.item(i).text()
                     for i in range(window.steps_list.count())])
    window.install_btn.click()
    deadline = time.time() + 30
    while time.time() < deadline:
        app.processEvents()
        if window._worker is None and not window._installing:
            break
        time.sleep(0.05)
    app.processEvents()
    time.sleep(0.3)
    window.grab().save("/tmp/winlinx-installer-after.png")
    print("saved /tmp/winlinx-installer-after.png")
    print("progress:", window.progress_bar.value())
    print("steps:", [window.steps_list.item(i).text()
                     for i in range(window.steps_list.count())])
    print("launch enabled:", window.launch_btn.isEnabled())
    print("status:", window.statusBar().currentMessage())
    log = window.log_view.toPlainText()
    print("log lines:", len(log.splitlines()))
    for needle in ("winehq-staging", "freshclam", "yabridge",
                   "winlinx-watcher.service"):
        print("log contains %r: %s" % (needle, needle in log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
