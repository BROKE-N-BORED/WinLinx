"""Build a fake home directory (fake Wine prefix, fake yabridge
wrappers, fake .desktop files) for the winlinx tests."""

import os
from pathlib import Path


def build_fake_home(root, with_reg=True, with_desktops=True):
    root = Path(root)
    prefix = root / ".wine"
    drive_c = prefix / "drive_c"
    pf = drive_c / "Program Files"
    pf86 = drive_c / "Program Files (x86)"
    vst3 = pf / "Common Files" / "VST3"
    vst2 = pf / "Common Files" / "VST2"
    for d in (pf / "FooApp", pf86 / "OldApp", vst3, vst2):
        d.mkdir(parents=True, exist_ok=True)
    (pf / "FooApp" / "foo.exe").write_bytes(b"FAKEEXE" * 100)
    (pf / "FooApp" / "uninstall.exe").write_bytes(b"FAKEEXE" * 10)
    (pf86 / "OldApp" / "old.exe").write_bytes(b"FAKEEXE" * 50)
    (vst3 / "Remix.vst3").write_bytes(b"FAKEVST3" * 10)
    (vst3 / "Lonely.vst3").write_bytes(b"FAKEVST3" * 10)

    # dosdevices c: -> ../drive_c (what real Wine creates)
    dosdevices = prefix / "dosdevices"
    dosdevices.mkdir(parents=True, exist_ok=True)
    link = dosdevices / "c:"
    if not link.exists() and not link.is_symlink():
        os.symlink("../drive_c", link)

    if with_reg:
        reg = (
            '[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion'
            '\\\\Uninstall\\\\FooApp] 1234567890\n'
            '#time=1da23456789abcd\n'
            '"DisplayName"="FooApp 2.0"\n'
            '"UninstallString"="C:\\\\Program Files\\\\FooApp'
            '\\\\uninstall.exe"\n'
            '"InstallLocation"="C:\\\\Program Files\\\\FooApp"\n'
            '"InstallDate"="20260115"\n'
            '"EstimatedSize"=dword:00000400\n'
            '\n'
            '[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion'
            '\\\\Uninstall\\\\{12345678-1234-1234-1234-123456789012}]'
            ' 1234567891\n'
            '"DisplayName"="MSI Thing"\n'
            '"UninstallString"="MsiExec.exe '
            '/X{12345678-1234-1234-1234-123456789012}"\n'
            '"InstallLocation"="C:\\\\Program Files (x86)\\\\OldApp"\n'
            '"InstallDate"="20260201"\n'
            '"EstimatedSize"=dword:00000800\n'
            '\n'
            '[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion'
            '\\\\Uninstall\\\\HiddenRuntime] 1234567892\n'
            '"DisplayName"="Hidden Runtime"\n'
            '"SystemComponent"=dword:00000001\n'
        )
        (prefix / "system.reg").write_text(reg, encoding="utf-8")

    if with_desktops:
        desk_dir = (root / ".local" / "share" / "applications" / "wine"
                    / "Programs" / "FooApp")
        desk_dir.mkdir(parents=True, exist_ok=True)
        exe_unix = (prefix / "dosdevices" / "c:" / "Program Files"
                    / "FooApp" / "foo.exe")
        desk = (
            "[Desktop Entry]\n"
            "Name=FooApp\n"
            "Exec=env WINEPREFIX=\"%s\" wine "
            "C:\\\\windows\\\\command\\\\start.exe /Unix %s\n"
            "Type=Application\n"
            % (prefix, exe_unix)
        )
        (desk_dir / "FooApp.desktop").write_text(desk, encoding="utf-8")

    # yabridge wrappers: one good, one orphaned (source missing)
    yab = root / ".vst3" / "yabridge"
    yab.mkdir(parents=True, exist_ok=True)
    (yab / "Remix.vst3.so").write_bytes(b"FAKESO")
    (yab / "Broken.vst3.so").write_bytes(b"FAKESO")

    # watched folders + a few log lines
    (root / "WinLinx" / "Programs").mkdir(parents=True, exist_ok=True)
    (root / "WinLinx" / "Plugins").mkdir(parents=True, exist_ok=True)
    layer = root / ".local" / "share" / "winlinx"
    layer.mkdir(parents=True, exist_ok=True)
    (layer / "layer.log").write_text(
        "[2026-11-12 09:00:00] Watcher started\n"
        "[2026-11-12 09:01:00] Scan clean: foo.exe\n",
        encoding="utf-8")
    return root
