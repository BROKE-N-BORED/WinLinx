#!/usr/bin/env python3
"""Drive the full auto-update journey in a subprocess: launch the
INSTALLED manager (from WINLINX_HOME/.local/share/winlinx, not the
repo tree), tap Update on the update popup, tap Restart Now on the
restart prompt, and exit when the manager quits. The parent test
then checks the helper applied the update and relaunched the new
manager.

Env: WINLINX_HOME (fake home with an installed tree),
WINLINX_UPDATE_API_URL (fake GitHub), QT_QPA_PLATFORM=offscreen.
Exit 0 means both dialogs were seen and tapped and the manager
quit for the restart. Exit 2 means the flow stalled.
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HOME = Path(os.environ["WINLINX_HOME"])
LAYER = HOME / ".local" / "share" / "winlinx"
sys.path.insert(0, str(LAYER))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
import manager  # noqa: E402

WATCHDOG_SECONDS = 60


def _click_button(widget, text):
    for button in widget.buttons():
        if button.text() == text:
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            return True
    return False


def main():
    app = QApplication(sys.argv)
    window = manager.ManagerWindow()  # startup check ON (default)
    window.show()
    state = {"update_seen": False, "restart_seen": False}
    started = time.time()

    def poll():
        widget = app.activeModalWidget()
        if widget is not None:
            title = widget.windowTitle()
            if title == "Update available":
                state["update_seen"] = True
                _click_button(widget, "Update")
            elif title == "Update ready":
                state["restart_seen"] = True
                _click_button(widget, "Restart Now")
        if time.time() - started > WATCHDOG_SECONDS:
            print("DRIVER TIMEOUT: update_seen=%s restart_seen=%s"
                  % (state["update_seen"], state["restart_seen"]),
                  file=sys.stderr)
            app.exit(2)

    timer = QTimer()
    timer.setInterval(200)
    timer.timeout.connect(poll)
    timer.start()
    code = app.exec()
    if code != 0:
        return code
    if not state["update_seen"] or not state["restart_seen"]:
        print("DRIVER: manager quit without the full journey "
              "(update_seen=%s restart_seen=%s)"
              % (state["update_seen"], state["restart_seen"]),
              file=sys.stderr)
        return 1
    print("DRIVER: update popup tapped, restart tapped, manager quit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
