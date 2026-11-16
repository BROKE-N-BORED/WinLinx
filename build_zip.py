#!/usr/bin/env python3
"""Build winlinx-v<version>.zip with correct Unix modes.

Packs the repo-root product files into a top-level winlinx-v<version>/
folder. Tests and git metadata are NOT in the zip (render_icon.py IS:
install.sh uses it to render the icons from the SVG on a clone).

The zip is reproducible from the repo alone: when the icon PNGs are
missing (a git clone ships only winlinx.svg - the PNGs are gitignored),
they are rendered from the SVG first. That needs PySide6; without it
the build says so plainly instead of failing on missing files.

GUI extractors (Thunar's) drop the execute bit unless external_attr
carries the regular-file type bits: 0o100755 for scripts, 0o100644
for plain files. Bare 0o755 extracts fine under the `unzip` CLI but
breaks GUI extraction - see ~/AGENTS.md (Design Shell v1.0 lesson).
The .desktop launcher MUST be executable or Nemo will not run it.
"""

import os
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "winlinx"))

import common  # noqa: E402

VERSION = common.__version__
OUT = HERE / ("winlinx-v%s.zip" % VERSION)
TOP = "winlinx-v%s" % VERSION

# repo-relative paths that ship in the zip, in order.
INCLUDE = [
    "README.md",
    "CHANGELOG.md",
    "Install WinLinx.desktop",
    "install-winlinx.py",
    "install.sh",
    "uninstall.sh",
    "render_icon.py",
    "winlinx/__init__.py",
    "winlinx/common.py",
    "winlinx/installer.py",
    "winlinx/manager.py",
    "winlinx/scanner.py",
    "winlinx/watcher.py",
    "winlinx/run-windows-file",
    "winlinx/icons/winlinx.svg",
    "winlinx/icons/winlinx-256.png",
    "winlinx/icons/winlinx-128.png",
    "winlinx/icons/winlinx-48.png",
    "desktop/winlinx-manager.desktop",
    "desktop/winlinx-runner.desktop",
    "desktop/winlinx-mime.xml",
    "systemd/winlinx-watcher.service",
]

EXECUTABLES = {
    "Install WinLinx.desktop",
    "install-winlinx.py",
    "install.sh",
    "uninstall.sh",
    "render_icon.py",
    "winlinx/run-windows-file",
}

ICON_PNGS = (
    "winlinx/icons/winlinx-256.png",
    "winlinx/icons/winlinx-128.png",
    "winlinx/icons/winlinx-48.png",
)


def ensure_icons():
    """Render any missing icon PNGs from winlinx.svg. Returns True
    when all three PNGs exist afterwards."""
    missing = [p for p in ICON_PNGS if not (HERE / p).is_file()]
    if not missing:
        return True
    svg = HERE / "winlinx" / "icons" / "winlinx.svg"
    if not svg.is_file():
        print("ERROR: no winlinx.svg to render icons from")
        return False
    print("icon PNGs missing (%s) - rendering from winlinx.svg…"
          % ", ".join(missing))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
        import installer
    except ImportError:
        print("ERROR: PySide6 is not installed, so the icon PNGs "
              "cannot be rendered. Install python3-pyside6 (or pip "
              "install PySide6-Essentials) and run build_zip.py again.")
        return False
    app = QApplication(sys.argv)  # noqa: F841 - Qt needs an app object
    if not installer.render_icons_from_svg(svg, svg.parent):
        print("ERROR: could not render icons from %s" % svg)
        return False
    for rel in ICON_PNGS:
        path = HERE / rel
        print("rendered %s (%d bytes)" % (rel, path.stat().st_size))
    return True


def main():
    if not ensure_icons():
        return 1
    missing = [p for p in INCLUDE if not (HERE / p).is_file()]
    if missing:
        print("ERROR: missing files: %s" % ", ".join(missing))
        return 1
    if OUT.exists():
        OUT.unlink()
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in INCLUDE:
            path = HERE / rel
            mode = 0o100755 if rel in EXECUTABLES else 0o100644
            info = zipfile.ZipInfo("%s/%s" % (TOP, rel))
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    print("built %s (%d bytes)" % (OUT, OUT.stat().st_size))
    with zipfile.ZipFile(OUT) as zf:
        for info in zf.infolist():
            print("%s %s" % (oct(info.external_attr >> 16),
                             info.filename))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
