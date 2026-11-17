# WinLinx changelog

## 0.3 - 2026-11-16

The rest of the 2026-11-16 dogfood review (tasks 3-45): everything the
review found wrong, confusing, or missing, fixed in one release.

Plain words everywhere:

- Double-clicking a plug-in no longer says "bridged" when the sync
  failed. It says so plainly and points at the Program Manager.
- Launch, Re-sync Bridges, update checks, and ClamAV installs no
  longer show raw exceptions, tool names, or exit codes. The detail
  stays in the log; the user gets plain words and what to do next.
- Failed Drops reasons say "yabridge is not installed, so the plug-in
  could not be bridged" instead of leaking `yabridgectl`.

The manager tells you what happened:

- Action confirmations (Kill, Allow, Delete, Forget, Retry,
  Quarantine, Uninstall, Remove) survive the refresh instead of being
  overwritten by the count summary.
- Empty Programs and Plug-ins tabs explain what to do next, with an
  F1 pointer to How to Use.
- Columns no longer cut off the important part: wider defaults,
  middle-elided paths (`.../FooApp/foo.exe`), full text in tooltips,
  all threats shown.
- Action buttons stay disabled until a row is selected. Forget asks
  first (default No). The uninstall dialog's safe button has focus.
- Programs get per-program glyph icons instead of the same penguin on
  every row. The Prefix column reads "Own prefix" / "Shared prefix".
- The Plug-ins tab explains "Not bridged" / "Source missing" and what
  to do; Source File shows the file you dropped.
- Quarantine search covers threat names; a no-match search says so
  and the status bar shows the filtered count.
- The threats dialog lists the file once with deduplicated threat
  names. "Can't uninstall" offers an Open Install Folder button.
- How to Use has a Close button; the scan picker has a Cancel button.
  The password-prompt fallbacks explain why before giving a terminal
  command.
- Activity warnings and errors are coloured, with a warnings-only
  filter. Tab counts hide when zero. The System tab says "No prefix
  yet" when there is none. The Windows version menu is newest-first
  with radio behaviour.

Installer honesty:

- A step that skips its core work (no wineboot, no yabridgectl,
  wineboot failed) reports a warning, so the run says "Installed with
  warning(s)" instead of a green "Installed!".
- The progress bar goes busy while a step runs, and stops at the
  completed fraction on a failed run instead of hitting 100%.
- The step list no longer renders a selection and is sized to its
  content.

Watcher and double-click:

- Unsupported drops (.zip, .txt, ...) are logged and recorded as
  failed with a plain reason, so they show in Failed Drops instead of
  vanishing. (.zip extraction itself is still a follow-up.)
- Failure notifications are batched per reason ("3 files could not be
  handled: Wine is not installed") and point at the Program Manager.
- Dropped folders are recorded as failed with guidance instead of
  being silently ignored; the log says "Folder left in place" when no
  scan ran.
- An installer that installs but fails the bridge sync notifies once
  instead of failing silently. Double-click failures are recorded as
  failed drops, so they show in the manager with Retry and Forget.

System tab and maintenance:

- Missing Wine or yabridge gets an "Install or Repair" action that
  re-runs the installer; the watcher status line explains itself.
- Check for Updates lives in the Help menu only (was three places);
  the System tab drops its duplicate Re-sync, Updates, and Debug
  Report buttons.
- Update Signatures checks ClamAV is installed first and offers to
  install it when it is not. Kill no longer claims success when
  nothing was running.
- Restore names where the old prefix went, and old aside copies are
  pruned to the newest per prefix. The debug report adds watcher
  status, quarantine/failed counts, and system info. Backup progress
  reports by bytes as well as files.

## 0.2.1 - 2026-11-16

Two fixes from running v0.2 as a user (2026-11-16 dogfood review):

- The Program Manager starts again. `QLockFile.tryLock()` needs an
  argument on PySide6 6.12, and `main()` died before the window opened,
  so double-clicking the menu icon did nothing. The lock call now
  passes 0, and a new test launches the real entry point as a
  subprocess and asserts the window stays up. The single-instance
  path (a second launch focuses the running window) is verified live
  by a second new test. While verifying, one more start-up defect
  was fixed in place: if the lock file cannot even be created (the
  layer folder is not writable), the manager used to claim WinLinx
  was already running and hang on that dialog; it now says plainly
  it cannot start and exits.
- Cancelling the installer no longer reports "Installed!". A
  cancelled run says how many steps finished and asks you to press
  Install to finish; it never enables Launch and never shows 100%.
  A step that stops because you cancelled is reported as cancelled,
  not failed. Two new tests drive both cancel variants (step
  completes normally after cancel; step notices the flag and bails
  out).

## 0.2 - 2026-11-15

Fixed (all proven live in the 2026-11-13 deep audit):

- Quarantine ids are unique now (microsecond timestamp + random hex).
  Two files with the same name quarantined in the same second used to
  share an id, and deleting one deleted both. Delete and Allow now act
  on exactly one entry.
- The watcher no longer records a failed drop as handled. A file that
  fails (no Wine, a copy error, a failed sync) is recorded with the
  real reason and retried on later passes with backoff. Failed drops
  show in the manager with a Retry button.
- Destructive confirmations (Uninstall, Remove plug-in, Allow, Delete)
  default to No, so pressing Enter no longer confirms them.
- `build_zip.py` works from a clean clone: missing icon PNGs are
  rendered from the SVG before packing, so the release zip is
  reproducible from the repo alone.
- Double-clicking a `.vst3` / `.dll` / `.clap` now reaches WinLinx:
  custom MIME types are defined and registered by both installers.
  The runner also handles `.vst3` bundle folders, like the watcher.
- yabridge is pinned (5.1.1) and SHA-256 verified after download in
  both install paths; extraction uses `filter="data"`. The WineHQ apt
  key fingerprint is checked with gpg before the key is trusted.
- A visible banner in the manager warns when ClamAV is missing, so
  files no longer run unscanned with only a log line to show for it.

New:

- CLAP plug-ins are first-class: drop a `.clap` in the Plugins folder
  or double-click one and it is bridged like any other plug-in.
- Per-program prefixes: right-click a program and give it its own Wine
  prefix, with its own Windows version (10, 11, 7, XP, 8). Programs
  without one keep sharing the main prefix.
- Prefix backup and restore (File menu): backs up the main prefix and
  every per-program prefix into `~/WinLinx/Backups`. A restore moves
  the current prefix aside instead of deleting it.
- Self-update check (Help menu): asks GitHub for the latest release
  and offers to download it when a newer version exists.
- GUI uninstaller (File menu): the same steps as `uninstall.sh`,
  in a window, with a confirm first.
- Live activity feed (Activity tab) and desktop notifications when a
  drop is quarantined, fails, or starts working again.
- Install buttons in the manager (Programs and Plug-ins tabs) that
  drop a picked file into the watched folders for the watcher.
- Program icons (from Wine's menu entries) and a Running column in
  the Programs tab; auto-refresh every 30 seconds.
- Menu bar (File / View / Help) with a built-in How to Use index.
- Debug log layer: rotating logs, a last-200 ring buffer, and a Debug
  report button (Help menu) that writes a scrubbed report.
- Single instance: opening the manager twice focuses the running
  window instead of starting a second copy.
- Window geometry is remembered between runs; keyboard access
  (Enter to launch, Delete key, Ctrl+R refresh, Ctrl+I install);
  search on every table; refresh keeps your scroll position and
  selection.
- The version lives in one place (`winlinx/common.py`, `__version__`)
  and shows in the manager title; every tool takes `--version`.
- GitHub Actions runs the test suite on every push.

## 0.1 - 2026-11-12

First release.

- Watched folders: drop a `.exe` / `.msi` in `~/WinLinx/Programs`
  and it is scanned, then run with Wine. Drop a VST installer or a
  `.vst3` / `.dll` in `~/WinLinx/Plugins` and it is scanned,
  installed, and bridged with yabridge into the standard `~/.vst3`
  and `~/.vst` folders every Linux DAW scans.
- Double-click handler: `.exe` / `.msi` files run (scanned first)
  from anywhere on the PC.
- ClamAV scan gate: nothing runs before it is scanned. Flagged files
  go to quarantine and never execute; the manager Allows (by content
  hash) or Deletes them.
- Program Manager (Qt): Programs, Plug-ins, Quarantine, and System
  tabs; launch, uninstall, re-sync, manual scan, signature updates.
- GUI installer (double-click "Install WinLinx") plus a terminal
  fallback (`install.sh`); `uninstall.sh` removes WinLinx and keeps
  Wine, yabridge, ClamAV, the prefix, and quarantined files.
