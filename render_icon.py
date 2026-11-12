#!/usr/bin/env python3
"""Regenerate winlinx-{256,128,48}.png from winlinx.svg (dev tool).

The PNGs ship in the release zip but cannot be pushed to GitHub via
the snapshot push (UTF-8 text only), so the SVG is the source of
truth and the PNGs are rebuilt from it. Usage:
  render_icon.py [svg-path] [out-dir]
Defaults: repo winlinx/icons/winlinx.svg -> same dir.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "winlinx"))

SVG = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "winlinx" / "icons" / "winlinx.svg"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else SVG.parent

from PySide6.QtWidgets import QApplication  # noqa: E402
import installer  # noqa: E402


def main():
    app = QApplication(sys.argv)
    if installer.render_icons_from_svg(SVG, OUT):
        for size in (256, 128, 48):
            out = OUT / ("winlinx-%d.png" % size)
            print("wrote %s (%d bytes)" % (out, out.stat().st_size))
        return 0
    print("ERROR: could not render %s" % SVG)
    return 1


if __name__ == "__main__":
    sys.exit(main())
