#!/usr/bin/env python3
"""Render the Program Manager window offscreen with a rich fake home
and save screenshots for eyeballing. Usage:

  QT_QPA_PLATFORM=offscreen LD_LIBRARY_PATH=... \\
    ~/workspace/forge-v2-testenv/bin/python screenshot_manager.py

Screenshots land in /tmp/winlinx-manager-*.png
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "winlinx"))
sys.path.insert(0, str(HERE / "tests"))

import fakehome  # noqa: E402
import scanner  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402
import manager  # noqa: E402

FAKE_HOME = Path("/tmp/winlinx-shot-home")


def build_rich_home():
    import shutil
    if FAKE_HOME.exists():
        shutil.rmtree(FAKE_HOME)
    fakehome.build_fake_home(FAKE_HOME)
    prefix = FAKE_HOME / ".wine"
    # A third program with no registry size (exercises the dir-size path).
    extra = (prefix / "drive_c" / "Program Files" / "RipX DAW")
    extra.mkdir(parents=True, exist_ok=True)
    (extra / "RipX.exe").write_bytes(b"X" * 4096)
    reg_extra = (
        '[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion'
        '\\\\Uninstall\\\\RipX] 1234567893\n'
        '"DisplayName"="RipX DAW"\n'
        '"UninstallString"="C:\\\\Program Files\\\\RipX DAW'
        '\\\\uninstall.exe"\n'
        '"InstallLocation"="C:\\\\Program Files\\\\RipX DAW"\n'
        '"InstallDate"="20260320"\n'
    )
    with open(prefix / "system.reg", "a", encoding="utf-8") as fh:
        fh.write("\n" + reg_extra)
    # A couple more plug-ins, one unbridged VST2.
    vst2 = prefix / "drive_c" / "Program Files" / "Common Files" / "VST2"
    (vst2 / "OldVerb.dll").write_bytes(b"DLL" * 10)
    # Two quarantined items.
    for name, threat in (("shady-setup.exe", "PUA.Toolbar.Bundler"),
                         ("free-crack.exe", "Win.Malware.Agent")):
        bad = FAKE_HOME / name
        bad.write_bytes(b"x")
        scanner.quarantine_item(bad, [(str(bad), threat)])
    # Richer log.
    log = (FAKE_HOME / ".local" / "share" / "winlinx" / "layer.log")
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("[2026-11-12 09:02:00] QUARANTINED shady-setup.exe: "
                 "PUA.Toolbar.Bundler\n")
        fh.write("[2026-11-12 09:03:00] Bridges re-synced\n")


def main():
    os.environ["WINLINX_HOME"] = str(FAKE_HOME)
    build_rich_home()
    app = QApplication(sys.argv)
    window = manager.ManagerWindow()
    window.show()
    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        if window._worker is None:
            break
        time.sleep(0.05)
    app.processEvents()
    for index, name in ((0, "programs"), (1, "plugins"),
                        (2, "quarantine"), (3, "system")):
        window.tabs.setCurrentIndex(index)
        app.processEvents()
        time.sleep(0.2)
        path = "/tmp/winlinx-manager-%s.png" % name
        window.grab().save(path)
        print("saved", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
