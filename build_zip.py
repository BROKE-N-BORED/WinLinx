#!/usr/bin/env python3
"""Build winlinx-v0.1.zip with correct Unix modes.

Packs the repo-root product files into a top-level winlinx-v0.1/
folder. Tests, build scripts, and git metadata are NOT in the zip.

GUI extractors (Thunar's) drop the execute bit unless external_attr
carries the regular-file type bits: 0o100755 for scripts, 0o100644
for plain files. Bare 0o755 extracts fine under the `unzip` CLI but
breaks GUI extraction — see ~/AGENTS.md (Design Shell v1.0 lesson).
The .desktop launcher MUST be executable or Nemo will not run it.
"""

import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "winlinx-v0.1.zip"
TOP = "winlinx-v0.1"

# repo-relative paths that ship in the zip, in order.
INCLUDE = [
    "README.md",
    "Install WinLinx.desktop",
    "install-winlinx.py",
    "install.sh",
    "uninstall.sh",
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
    "systemd/winlinx-watcher.service",
]

EXECUTABLES = {
    "Install WinLinx.desktop",
    "install-winlinx.py",
    "install.sh",
    "uninstall.sh",
    "winlinx/run-windows-file",
}


def main():
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
