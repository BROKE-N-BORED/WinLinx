#!/bin/bash
# WinLinx v0.1 — uninstaller
#
# Run it from the extracted folder:
#   ./uninstall.sh
#
# What it removes:
#   - the watcher service
#   - the file associations (double-click goes back to Wine's own
#     handler if it exists)
#   - the Program Manager menu entry and WinLinx's own files
#
# What it LEAVES alone (on purpose):
#   - Wine, yabridge, ClamAV (still installed, still usable)
#   - your Wine prefix (~/.wine) and everything installed in it
#   - ~/WinLinx and the files inside it
#   - quarantined files (moved to ~/WinLinx/Quarantine so you
#     can still decide what to do with them)

set -u

say()  { echo ""; echo "==> $1"; }
warn() { echo ""; echo "!!  WARNING: $1"; }
ok()   { echo "    OK: $1"; }

echo "WinLinx v0.1 — uninstaller"

say "Stopping the watcher service"
systemctl --user disable --now winlinx-watcher.service 2>/dev/null \
    && ok "watcher stopped" \
    || echo "    (watcher was not running)"
rm -f "$HOME/.config/systemd/user/winlinx-watcher.service"
systemctl --user daemon-reload 2>/dev/null || true

say "Removing file associations"
if [ -f /usr/share/applications/wine.desktop ] \
   || [ -f "$HOME/.local/share/applications/wine.desktop" ]; then
    if command -v xdg-mime >/dev/null 2>&1; then
        xdg-mime default wine.desktop application/x-msdownload 2>/dev/null || true
        xdg-mime default wine.desktop application/x-msi 2>/dev/null || true
        ok ".exe/.msi files go back to Wine's own handler"
    fi
else
    # No Wine handler to restore: just drop our entries.
    MIMEAPPS="$HOME/.config/mimeapps.list"
    if [ -f "$MIMEAPPS" ]; then
        sed -i '/winlinx-runner.desktop/d' "$MIMEAPPS" 2>/dev/null || true
        ok "our file associations removed"
    fi
fi
rm -f "$HOME/.local/share/applications/winlinx-manager.desktop"
rm -f "$HOME/.local/share/applications/winlinx-runner.desktop"

say "Removing WinLinx's files"
LAYER="$HOME/.local/share/winlinx"
if [ -d "$LAYER/quarantine" ] && [ -n "$(ls -A "$LAYER/quarantine" 2>/dev/null)" ]; then
    mkdir -p "$HOME/WinLinx/Quarantine"
    cp -r "$LAYER/quarantine"/. "$HOME/WinLinx/Quarantine/" 2>/dev/null \
        && ok "quarantined files kept in ~/WinLinx/Quarantine" \
        || warn "could not keep the quarantined files"
fi
rm -rf "$LAYER" && ok "WinLinx files removed" || warn "could not remove $LAYER"

say "Done!"
echo ""
echo "  Left installed on purpose: Wine, yabridge, ClamAV."
echo "  Left alone: your Wine prefix (~/.wine), ~/WinLinx,"
echo "  and any quarantined files (now in ~/WinLinx/Quarantine)."
echo "  To remove Wine/yabridge/ClamAV too, use the Software Manager"
echo "  or: sudo apt remove winehq-staging clamav clamav-freshclam"
echo ""
