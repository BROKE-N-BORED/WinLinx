#!/bin/bash
# WinLinx v0.1 — installer for Linux Mint 22.x (terminal fallback)
#
# Normal install: unzip the folder and double-click "Install WinLinx".
# This script does the same steps from a terminal, for when the
# double-click installer cannot run:
#   ./install.sh
#
# What it does, in plain English:
#   1. Installs Wine Staging (runs Windows programs) + winetricks
#   2. Installs ClamAV (scans files for bloatware/spyware first)
#      and downloads the virus signatures
#   3. Installs yabridge (bridges Windows VST plug-ins into Linux)
#   4. Sets up the Wine prefix and the VST folders
#   5. Installs the watcher (auto-runs dropped files) + the
#      WinLinx Program Manager window
#   6. Makes double-clicking .exe/.msi files just work
#
# It never fails silently: every step prints what happened, and a
# failed step prints a warning and moves on (warn-never-refuse).
# Safe to run again — it will just redo the steps.

set -u
cd "$(dirname "$0")"

say()  { echo ""; echo "==> $1"; }
warn() { echo ""; echo "!!  WARNING: $1"; }
ok()   { echo "    OK: $1"; }

echo "WinLinx v0.1 — installer"
echo "This takes a few minutes and will ask for your password (sudo)."

# ---------------------------------------------------------------- helpers

# Download a URL to a file: curl first, wget as backup.
download() {
    url="$1"; out="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$out" "$url" 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$out" "$url" 2>/dev/null
    else
        return 1
    fi
}

# ---------------------------------------------------------------- 1. Wine

say "Step 1 of 7: installing Wine (runs Windows programs)"
sudo dpkg --add-architecture i386 2>/dev/null \
    || warn "could not enable 32-bit packages (some old programs may not run)"

CODENAME="$(. /etc/os-release 2>/dev/null; echo "${UBUNTU_CODENAME:-noble}")"
echo "    Ubuntu base detected: $CODENAME"
sudo mkdir -pm755 /etc/apt/keyrings 2>/dev/null || true
if download "https://dl.winehq.org/wine-builds/winehq.key" /tmp/winehq.key \
   && sudo cp /tmp/winehq.key /etc/apt/keyrings/winehq-archive.key 2>/dev/null \
   && download "https://dl.winehq.org/wine-builds/ubuntu/dists/$CODENAME/winehq-$CODENAME.sources" /tmp/winehq.sources \
   && sudo cp /tmp/winehq.sources "/etc/apt/sources.list.d/winehq-$CODENAME.sources" 2>/dev/null; then
    ok "WineHQ repository added"
else
    warn "could not add the WineHQ repository (no internet?) — will try the older Wine from Mint instead"
fi
sudo apt update 2>/dev/null || warn "apt update had errors — continuing anyway"
if sudo apt install --install-recommends -y winehq-staging winetricks 2>/dev/null; then
    ok "Wine Staging installed: $(wine --version 2>/dev/null)"
elif sudo apt install -y wine 2>/dev/null; then
    warn "Wine Staging was not available, installed the older Wine from Mint instead — most programs still work, but yabridge prefers Staging"
    ok "Wine installed: $(wine --version 2>/dev/null)"
else
    warn "Wine could not be installed — Windows programs will NOT run until Wine is installed"
fi

# ---------------------------------------------------------------- 2. ClamAV

say "Step 2 of 7: installing ClamAV (scans files before they run)"
if sudo apt install -y clamav clamav-freshclam 2>/dev/null; then
    ok "ClamAV installed"
else
    warn "ClamAV could not be installed — files will run UNSCANNED until you install it (sudo apt install clamav clamav-freshclam)"
fi
if command -v freshclam >/dev/null 2>&1; then
    echo "    Downloading virus signatures (big download, a few minutes)…"
    sudo systemctl stop clamav-freshclam 2>/dev/null || true
    if sudo freshclam 2>/dev/null; then
        ok "Virus signatures downloaded: $(clamscan --version 2>/dev/null)"
    else
        warn "signature download failed (no internet?) — scans will fail until signatures download; run: sudo freshclam"
    fi
    sudo systemctl enable --now clamav-freshclam 2>/dev/null \
        && ok "Automatic daily signature updates enabled" \
        || warn "could not enable automatic signature updates"
fi

# ---------------------------------------------------------------- 3. yabridge

say "Step 3 of 7: installing yabridge (bridges Windows VST plug-ins)"
YABRIDGE_OK=0
if command -v yabridgectl >/dev/null 2>&1; then
    ok "yabridge already installed"
    YABRIDGE_OK=1
else
    TAG=""
    if command -v curl >/dev/null 2>&1; then
        TAG="$(curl -fsSL https://api.github.com/repos/robbert-vdh/yabridge/releases/latest 2>/dev/null \
            | grep '"tag_name":' | head -1 | sed 's/.*"tag_name": *"//; s/".*//')"
    fi
    if [ -n "$TAG" ] \
       && download "https://github.com/robbert-vdh/yabridge/releases/download/$TAG/yabridge-$TAG.tar.gz" /tmp/yabridge.tar.gz; then
        TMPD="$(mktemp -d)"
        if tar -xzf /tmp/yabridge.tar.gz -C "$TMPD" 2>/dev/null; then
            YBDIR="$(find "$TMPD" -maxdepth 2 -name yabridgectl -printf '%h\n' 2>/dev/null | head -1)"
            if [ -n "$YBDIR" ] && [ -x "$YBDIR/yabridgectl" ]; then
                mkdir -p "$HOME/.local/share/yabridge"
                cp -r "$YBDIR"/. "$HOME/.local/share/yabridge/"
                mkdir -p "$HOME/.local/bin"
                ln -sf "$HOME/.local/share/yabridge/yabridgectl" "$HOME/.local/bin/yabridgectl"
                ok "yabridge $TAG installed"
                YABRIDGE_OK=1
            fi
        fi
        rm -rf "$TMPD" /tmp/yabridge.tar.gz
    fi
    if [ "$YABRIDGE_OK" = "0" ]; then
        warn "yabridge could not be downloaded (no internet?) — VST bridging will NOT work until yabridge is installed"
    fi
fi

# ---------------------------------------------------------------- 4. PySide6 (manager window)

say "Step 4 of 7: installing PySide6 (the Program Manager window)"
if python3 -c "import PySide6" 2>/dev/null; then
    ok "PySide6 already installed"
elif sudo apt install -y python3-pyside6 2>/dev/null \
     && python3 -c "import PySide6" 2>/dev/null; then
    ok "PySide6 installed"
else
    echo "    apt did not work, trying pip…"
    python3 -m pip --version 2>/dev/null \
        || sudo apt install -y python3-pip 2>/dev/null || true
    if python3 -m pip install --user --break-system-packages PySide6-Essentials 2>/dev/null \
       && python3 -c "import PySide6" 2>/dev/null; then
        ok "PySide6 installed (pip)"
    else
        warn "PySide6 could not be installed — the Program Manager window will NOT open until it is installed"
    fi
fi

# ---------------------------------------------------------------- 5. Wine prefix + VST folders

say "Step 5 of 7: setting up the Wine prefix and VST folders"
if command -v wineboot >/dev/null 2>&1; then
    echo "    First Wine start takes a minute…"
    if timeout 300 wineboot --init 2>/dev/null; then
        ok "Wine prefix ready at $HOME/.wine"
    else
        warn "wineboot was slow or failed — the prefix may still work; try running a Windows program and see"
    fi
else
    warn "wineboot not found — skipping prefix setup"
fi

mkdir -p "$HOME/.wine/drive_c/Program Files/Common Files/VST3"
mkdir -p "$HOME/.wine/drive_c/Program Files (x86)/Common Files/VST3"
mkdir -p "$HOME/.wine/drive_c/Program Files/Common Files/VST2"

YABRIDGECTL=""
if command -v yabridgectl >/dev/null 2>&1; then
    YABRIDGECTL="yabridgectl"
elif [ -x "$HOME/.local/share/yabridge/yabridgectl" ]; then
    YABRIDGECTL="$HOME/.local/share/yabridge/yabridgectl"
fi
if [ -n "$YABRIDGECTL" ]; then
    "$YABRIDGECTL" add "$HOME/.wine/drive_c/Program Files/Common Files/VST3" 2>/dev/null || true
    "$YABRIDGECTL" add "$HOME/.wine/drive_c/Program Files (x86)/Common Files/VST3" 2>/dev/null || true
    "$YABRIDGECTL" add "$HOME/.wine/drive_c/Program Files/Common Files/VST2" 2>/dev/null || true
    if "$YABRIDGECTL" sync 2>/dev/null; then
        ok "Plug-in folders registered with yabridge"
    else
        warn "yabridgectl sync reported a problem — bridging may still work; use Re-sync in the Program Manager"
    fi
else
    warn "yabridgectl not found — skipping plug-in folder setup"
fi

# ---------------------------------------------------------------- 6. Install the WinLinx files

say "Step 6 of 7: installing WinLinx itself"
DEST="$HOME/.local/share/winlinx"
mkdir -p "$DEST"
cp winlinx/*.py "$DEST/" || warn "could not copy the WinLinx programs"
cp winlinx/run-windows-file "$DEST/" || warn "could not copy the file runner"
chmod +x "$DEST/run-windows-file" 2>/dev/null || true
mkdir -p "$DEST/icons"
cp winlinx/icons/* "$DEST/icons/" 2>/dev/null \
    || warn "could not copy the icons"
mkdir -p "$DEST/quarantine"

mkdir -p "$HOME/.local/share/applications"
sed "s|@HOME@|$HOME|g" desktop/winlinx-manager.desktop \
    > "$HOME/.local/share/applications/winlinx-manager.desktop" \
    || warn "could not install the Program Manager menu entry"
sed "s|@HOME@|$HOME|g" desktop/winlinx-runner.desktop \
    > "$HOME/.local/share/applications/winlinx-runner.desktop" \
    || warn "could not install the file runner entry"
if command -v xdg-mime >/dev/null 2>&1; then
    xdg-mime default winlinx-runner.desktop application/x-msdownload 2>/dev/null || true
    xdg-mime default winlinx-runner.desktop application/x-msi 2>/dev/null || true
    ok "Double-clicking .exe/.msi files now runs them (scanned first)"
else
    warn "xdg-mime not found — double-click may not work; use the watched folders instead"
fi

mkdir -p "$HOME/WinLinx/Programs" "$HOME/WinLinx/Plugins"
ok "Watched folders ready: ~/WinLinx/Programs and ~/WinLinx/Plugins"

mkdir -p "$HOME/.config/systemd/user"
cp systemd/winlinx-watcher.service \
   "$HOME/.config/systemd/user/winlinx-watcher.service" \
   || warn "could not install the watcher service"
if systemctl --user daemon-reload 2>/dev/null \
   && systemctl --user enable --now winlinx-watcher.service 2>/dev/null; then
    ok "Watcher service running"
else
    warn "could not start the watcher service — start it by hand with: systemctl --user enable --now winlinx-watcher.service"
fi

# ---------------------------------------------------------------- 7. Summary

say "Done!"
echo ""
echo "  HOW TO USE IT"
echo "  - Drop a Windows installer or program (.exe/.msi) into"
echo "    ~/WinLinx/Programs — it is scanned, then it runs."
echo "  - Drop a VST installer (.exe/.msi) or a plug-in file"
echo "    (.vst3/.dll) into ~/WinLinx/Plugins — it is scanned,"
echo "    installed, and bridged into ~/.vst3 for your DAW."
echo "  - Or just double-click any .exe/.msi anywhere — it is"
echo "    scanned first, then it runs."
echo "  - Open 'WinLinx Program Manager' from the menu to see your"
echo "    programs, plug-ins, quarantined files, and the log."
echo ""
echo "  If something gets flagged, it goes to quarantine — nothing"
echo "  flagged ever installs or runs. You decide in the manager:"
echo "  Allow it, or Delete it."
echo ""
echo "  Log file: $DEST/layer.log"
echo ""
