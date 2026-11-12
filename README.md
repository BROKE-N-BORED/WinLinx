# WinLinx v0.1

After installing this once, Windows programs and VST plug-ins just
work on Linux Mint — no manual setup per file, ever.

## Install (no terminal)

Unzip the folder, then **double-click "Install WinLinx"**.

It installs Wine Staging, ClamAV (+ virus signatures), yabridge,
the watcher, and the WinLinx Program Manager window. It takes a few
minutes and asks for your password when a step needs it. When it is
done, **WinLinx Program Manager** is in your app menu — launch it
from there, like any real program.

Terminal fallback, if the double-click installer cannot run:

```
./install.sh
```

Same steps, same result.

## How to use it

**Drop files in the watched folders:**

- `~/WinLinx/Programs` — drop a Windows program or installer
  (`.exe` / `.msi`). It is scanned, then it runs.
- `~/WinLinx/Plugins` — drop a VST installer (`.exe` / `.msi`)
  or a plug-in file (`.vst3` / `.dll`). It is scanned, installed,
  and bridged into `~/.vst3` (or `~/.vst`) so whatever DAW you use
  can find it. No DAW is assumed — the bridges live in the standard
  folders every Linux DAW scans.

**Or just double-click** any `.exe` / `.msi` anywhere on the PC.
It is scanned first, then it runs. Double-clicking a `.vst3` / `.dll`
bridges it the same way.

**Open "WinLinx Program Manager"** from the menu to see everything:
installed programs (launch / uninstall / open folder), bridged
plug-ins (re-sync / remove), quarantined files, and the log.

## The scanner

Every file is scanned with ClamAV (PUA detection on, so bloatware
and adware get caught too) BEFORE it is allowed to install or run.
A flagged file is moved to quarantine and never executes. In the
Program Manager you decide: **Allow** (install anyway — your call)
or **Delete**. Signatures update automatically every day; the
"Update Signatures" button in the manager updates them on demand.
Scanning runs locally on your PC — only signature updates need
the internet.

## Files

- Watcher + manager + scanner:
  `~/.local/share/winlinx/`
- Log: `~/.local/share/winlinx/layer.log`
- Quarantine: `~/.local/share/winlinx/quarantine`
- Wine prefix: `~/.wine`

## Uninstall

```
./uninstall.sh
```

Removes the watcher, the file associations, and WinLinx's own files.
Wine, yabridge, and ClamAV stay installed; your Wine prefix,
`~/WinLinx`, and quarantined files are left alone (quarantined
files move to `~/WinLinx/Quarantine`).

## Honest limits (v0.1)

- Not every Windows program runs under Wine — some copy protection
  and some iLok-protected plug-ins fail. That is a Wine limit, not
  something WinLinx can fix yet.
- The first Wine start is slow (about a minute). So is the first
  scan of a big installer.
- If ClamAV is not installed, files still go through — but the log
  says they ran unscanned. Install ClamAV to close that gap:
  `sudo apt install clamav clamav-freshclam`
