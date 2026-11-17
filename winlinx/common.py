#!/usr/bin/env python3
"""Shared helpers for WinLinx.

Pure stdlib. No Qt here, so the watcher and the manager can both use it.

Path rules:
  - Everything hangs off the user's home directory.
  - WINLINX_HOME env var overrides home (test hook only).
  - WINEPREFIX env var is honoured; default is ~/.wine.
  - VST bridging is host-agnostic: wrappers live in the standard
    ~/.vst3/yabridge, ~/.vst/yabridge and ~/.clap/yabridge paths so
    whatever DAW the user runs can scan them. No DAW is assumed.
"""

import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# Single source of truth for the WinLinx version. Every other file
# imports this (Python) or reads it out of this file (shell scripts).
__version__ = "0.4"

UPDATE_API_URL = ("https://api.github.com/repos/BROKE-N-BORED/"
                  "WinLinx/releases/latest")
RELEASES_URL = "https://github.com/BROKE-N-BORED/WinLinx/releases"


def _update_api_url():
    # WINLINX_UPDATE_API_URL overrides the update check endpoint
    # (test hook only - the tests point it at a fake GitHub).
    override = os.environ.get("WINLINX_UPDATE_API_URL")
    if override:
        return override
    return UPDATE_API_URL


# ---------------------------------------------------------------- paths

def home() -> Path:
    override = os.environ.get("WINLINX_HOME")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~"))


def layer_dir() -> Path:
    return home() / ".local" / "share" / "winlinx"


def log_path() -> Path:
    return layer_dir() / "layer.log"


def state_path() -> Path:
    return layer_dir() / "handled.json"


def prefixes_path() -> Path:
    """Per-program prefix map: {exe basename (lowercase): prefix path}."""
    return layer_dir() / "prefixes.json"


def prefixes_root() -> Path:
    """Where per-program Wine prefixes live."""
    return layer_dir() / "prefixes"


def program_settings_path() -> Path:
    """Per-program settings: {exe basename (lowercase):
    {"windows_version": "win10" | ...}}."""
    return layer_dir() / "programs.json"


def backups_dir() -> Path:
    return home() / "WinLinx" / "Backups"


def wine_prefix() -> Path:
    return Path(os.environ.get("WINEPREFIX", str(home() / ".wine")))


def drive_c() -> Path:
    return wine_prefix() / "drive_c"


def program_files_dirs():
    """Both Program Files locations inside the prefix (if they exist)."""
    out = []
    for name in ("Program Files", "Program Files (x86)"):
        p = drive_c() / name
        if p.is_dir():
            out.append(p)
    return out


def vst3_dir() -> Path:
    return drive_c() / "Program Files" / "Common Files" / "VST3"


def vst3_dir_x86() -> Path:
    return drive_c() / "Program Files (x86)" / "Common Files" / "VST3"


def vst2_dir() -> Path:
    return drive_c() / "Program Files" / "Common Files" / "VST2"


def programs_folder() -> Path:
    return home() / "WinLinx" / "Programs"


def plugins_folder() -> Path:
    return home() / "WinLinx" / "Plugins"


def bridged_dirs():
    """kind -> folder where yabridge puts the Linux wrapper .so files."""
    return {
        "VST3": home() / ".vst3" / "yabridge",
        "VST2": home() / ".vst" / "yabridge",
        "CLAP": home() / ".clap" / "yabridge",
    }


def find_yabridgectl():
    """Locate yabridgectl: PATH first, then the install.sh locations."""
    found = shutil.which("yabridgectl")
    if found:
        return found
    for cand in (
        home() / ".local" / "share" / "yabridge" / "yabridgectl",
        home() / ".local" / "bin" / "yabridgectl",
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def find_wine():
    return shutil.which("wine")


# ---------------------------------------------------------------- debug log layer (U12)

LOG_MAX_BYTES = 1024 * 1024   # rotate layer.log at 1 MB
LOG_BACKUPS = 3               # keep layer.log.1 .. layer.log.3
RING_SIZE = 200               # last-200 ring buffer, in memory


def _rotate_log(path):
    """Rotate path -> path.1 -> path.2 -> path.3, dropping the oldest.
    Never raises."""
    try:
        if not path.is_file():
            return
        if path.stat().st_size < LOG_MAX_BYTES:
            return
        oldest = path.with_name(path.name + ".%d" % LOG_BACKUPS)
        try:
            oldest.unlink()
        except OSError:
            pass
        for n in range(LOG_BACKUPS - 1, 0, -1):
            src = path.with_name(path.name + ".%d" % n)
            dst = path.with_name(path.name + ".%d" % (n + 1))
            try:
                if src.is_file():
                    os.replace(src, dst)
            except OSError:
                pass
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def append_log(message):
    """Append one timestamped line to layer.log (rotating). Also
    feeds the process-wide ring buffer. Never raises."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (stamp, message)
    RING.append(line)
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    return line


class LogRing:
    """Last-N log lines held in memory (the U12 ring buffer)."""

    def __init__(self, size=RING_SIZE):
        self._size = size
        self._lines = []

    def append(self, line):
        self._lines.append(line)
        if len(self._lines) > self._size:
            del self._lines[:-self._size]

    def lines(self):
        return list(self._lines)

    def text(self):
        return "\n".join(self._lines)


RING = LogRing()


def _tail_lines(path, max_lines, max_bytes=65536):
    """Last max_lines lines of a file, read from the end (never loads
    the whole file). Returns [] when the file is missing."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, size - max_bytes))
            blob = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return blob.splitlines()[-max_lines:]


def read_log_tail(max_lines=200):
    """Last max_lines lines of the current layer.log."""
    return "\n".join(_tail_lines(log_path(), max_lines))


_SECRET_RE = re.compile(
    r"(api[_-]?key|token|password|passwd|secret|bearer)\s*[:=]\s*"
    r"[^\s\"']+",
    re.I)


def scrub_text(text):
    """Scrub a report: home dir becomes ~, anything shaped like a
    credential value is redacted. Reports never carry secrets."""
    out = (text or "").replace(str(home()), "~")
    return _SECRET_RE.sub(r"\1=<redacted>", out)


def watcher_service_status():
    """The user watcher service state ("active", "inactive",
    "failed", "unknown"). Never raises."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active",
             "winlinx-watcher.service"],
            capture_output=True, text=True, timeout=10)
        state = (proc.stdout or "").strip()
        return state if state else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def system_info():
    """(os description, total RAM) from /etc/os-release and
    /proc/meminfo. Either element is "" when it cannot be read."""
    os_name = ""
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as fh:
            fields = {}
            for line in fh:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    fields[key] = value.strip().strip('"')
        os_name = fields.get("PRETTY_NAME") or (
            "%s %s" % (fields.get("NAME", ""),
                       fields.get("VERSION", ""))).strip()
    except OSError:
        pass
    ram = ""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        ram = format_size(int(parts[1]) * 1024)
                    break
    except (OSError, ValueError):
        pass
    return os_name, ram


def debug_report_text():
    """The scrubbed debug report (U12): versions, status, and the
    last 200 log lines across the current and rotated logs."""
    import scanner  # local import: scanner imports common
    lines = []
    lines.append("WinLinx debug report - %s"
                 % datetime.now().isoformat(timespec="seconds"))
    lines.append("WinLinx version: %s" % __version__)
    lines.append("Wine: %s" % (get_wine_version() or "Not installed"))
    lines.append("yabridge: %s" % get_yabridge_status())
    scanstat = scanner.scanner_status()
    lines.append("Scanner: %s"
                 % (scanstat["version"] if scanstat["installed"]
                    else "ClamAV not installed"))
    prefix = wine_prefix()
    lines.append("Prefix: %s (%s used)"
                 % (prefix, format_size(dir_size(prefix))
                    if prefix.is_dir() else "missing"))
    lines.append("Per-program prefixes: %d"
                 % len(load_prefix_map()))
    lines.append("Watcher service: %s" % watcher_service_status())
    lines.append("Quarantined items: %d"
                 % len(scanner.load_quarantine()))
    try:
        import watcher  # local import: watcher imports common
        failed_count = len(watcher.load_failed())
    except Exception:  # noqa: BLE001 - a broken state file must not
        failed_count = -1  # break the report
    lines.append("Failed drops: %d" % failed_count
                 if failed_count >= 0 else "Failed drops: unknown")
    os_name, ram = system_info()
    lines.append("System: %s" % (os_name or "unknown"))
    lines.append("Memory: %s" % (ram or "unknown"))
    lines.append("")
    lines.append("Recent log (last 200 lines):")
    gathered = []
    for suffix in (".3", ".2", ".1", ""):
        name = "layer.log" + suffix
        gathered.extend(_tail_lines(layer_dir() / name, 200))
    lines.extend(gathered[-200:])
    return scrub_text("\n".join(lines)) + "\n"


def write_debug_report():
    """Write the scrubbed debug report under ~/WinLinx and return
    its path. Returns None if it could not be written."""
    try:
        folder = home() / "WinLinx"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("debug-report-%s.txt"
                         % datetime.now().strftime("%Y%m%d-%H%M%S"))
        path.write_text(debug_report_text(), encoding="utf-8")
        return path
    except OSError:
        return None


# ---------------------------------------------------------------- notifications

def notify(title, message):
    """Desktop notification via notify-send. Fail-soft: nothing
    happens when notify-send is missing. Never raises, never blocks."""
    sender = shutil.which("notify-send")
    if not sender:
        return False
    try:
        subprocess.Popen([sender, title, message],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- processes

def is_running(exe_path):
    """True when a process whose command line mentions this program's
    .exe is running (Wine shows the exe in the child cmdline).
    Never raises; False when pgrep is missing."""
    if not exe_path:
        return False
    pgrep = shutil.which("pgrep")
    if not pgrep:
        return False
    name = Path(exe_path).name
    try:
        proc = subprocess.run([pgrep, "-f", name],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def kill_program(exe_path):
    """Kill processes running this program's .exe. Returns
    (ok, message). Never raises. When nothing is running, that is
    reported, not claimed as a successful kill."""
    if not exe_path:
        return False, "no program to kill"
    name = Path(exe_path).name
    if shutil.which("pgrep") and not is_running(exe_path):
        return False, "%s is not running" % name
    pkill = shutil.which("pkill")
    if not pkill:
        return False, "pkill is not installed"
    try:
        proc = subprocess.run([pkill, "-f", name],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    if proc.returncode == 1:  # nothing matched after all (raced exit)
        return False, "%s is not running" % name
    return False, "could not stop %s" % name


# ---------------------------------------------------------------- windows paths

_WIN_PATH_RE = re.compile(r"""^["']?([A-Za-z]):[\\/](.*)$""")


def win_to_unix(win_path):
    """Convert a Windows path (C:\\Program Files\\Foo\\bar.exe) to the
    matching path inside the Wine prefix. Returns a Path or None."""
    if not win_path:
        return None
    text = str(win_path).strip().strip('"').strip("'")
    m = _WIN_PATH_RE.match(text)
    if not m:
        return None
    drive, rest = m.group(1).lower(), m.group(2)
    rest = rest.replace("\\", "/")
    dosdevices = wine_prefix() / "dosdevices"
    link = dosdevices / (drive + ":")
    base = None
    try:
        if link.is_symlink():
            target = os.readlink(link)
            base = (dosdevices / target).resolve() if not os.path.isabs(target) \
                else Path(target)
    except OSError:
        base = None
    if base is None:
        if drive == "c":
            base = drive_c()
        else:
            return None
    return base / rest


# ---------------------------------------------------------------- formatting

def format_size(num_bytes):
    if num_bytes is None:
        return ""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d B" % size
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f TB" % size


def format_install_date(raw):
    """Registry InstallDate is YYYYMMDD; show YYYY-MM-DD. Anything else
    passes through untouched."""
    if not raw:
        return ""
    text = str(raw).strip()
    if re.fullmatch(r"\d{8}", text):
        return "%s-%s-%s" % (text[0:4], text[4:6], text[6:8])
    return text


def dir_size(path):
    """Total bytes under a directory. Never follows symlinks, never
    raises: unreadable entries are skipped."""
    total = 0
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except OSError:
        pass
    return total


# ---------------------------------------------------------------- tool status

def get_wine_version(timeout=30):
    """'wine-10.x (Staging)' style string, or None if wine is missing."""
    wine = find_wine()
    if not wine:
        return None
    try:
        proc = subprocess.run([wine, "--version"], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or "").strip()
    return out or None


def get_yabridge_status():
    """'Installed' / 'Not installed'. Kept simple on purpose: the only
    yabridgectl subcommands WinLinx relies on are add/sync."""
    return "Installed" if find_yabridgectl() else "Not installed"


def run_yabridgectl_sync(timeout=180):
    """Run `yabridgectl sync`. Returns (ok, message).

    The message is plain words for the user: it never names the
    tool, because the manager, the Failed Drops list, and the
    watcher notification all show it as-is. A failure is also
    logged here, once, in those same plain words - callers must
    not log it again."""
    ctl = find_yabridgectl()
    if not ctl:
        append_log("WARNING: yabridge is not installed, so plug-ins "
                   "can't be bridged")
        return False, ("yabridge is not installed, so plug-ins can't "
                       "be bridged")
    try:
        proc = subprocess.run([ctl, "sync"], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        append_log("WARNING: the plug-in folders could not be "
                   "re-synced")
        return False, "the plug-in folders could not be re-synced"
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip()
    append_log("WARNING: the plug-in folders could not be re-synced")
    return False, "the plug-in folders could not be re-synced"


def plain_drop_reason(reason):
    """Plain-words version of a failed-drop reason for display.

    Older handled.json entries (and the watcher's own wording) can
    still carry the technical form "bridge sync failed
    (yabridgectl not found)"; the user sees the plain form instead.
    Unknown reasons pass through untouched."""
    text = reason or ""
    low = text.lower()
    if "yabridgectl" in low or "yabridge is not installed" in low:
        return ("yabridge is not installed, so the plug-in could not "
                "be bridged")
    if "bridge sync failed" in low:
        return "the plug-in folders could not be re-synced"
    return text


# ---------------------------------------------------------------- registry

_UNINSTALL_PREFIX = (
    r"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"
)
_SECTION_RE = re.compile(r"^\[(.+)\](?:\s+\d+)?\s*$")
_VALUE_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"=(.*)$')
_DWORD_RE = re.compile(r"^dword:([0-9a-fA-F]{1,8})$")


def _unescape(text):
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in ('"', "\\"):
                out.append(nxt)
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_system_reg(reg_path):
    """Parse Wine's system.reg into {section: {name: value}}.

    Only string and dword values are decoded; anything else is kept as
    its raw text. A malformed file yields what could be parsed, never
    an exception."""
    sections = {}
    current = None
    try:
        with open(reg_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                m = _SECTION_RE.match(line)
                if m:
                    current = _unescape(m.group(1))
                    sections.setdefault(current, {})
                    continue
                if current is None:
                    continue
                m = _VALUE_RE.match(line)
                if not m:
                    continue
                name = _unescape(m.group(1))
                raw = m.group(2).strip()
                if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
                    value = _unescape(raw[1:-1])
                else:
                    dm = _DWORD_RE.match(raw)
                    value = int(dm.group(1), 16) if dm else raw
                sections[current][name] = value
    except OSError:
        pass
    return sections


def _norm_name(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _desktop_entries():
    """Wine's generated .desktop files: [{name, exe, icon}] where exe
    is a unix path to the program's main .exe (or None) and icon is
    the Icon= value (a path or theme name, or None)."""
    entries = []
    base = home() / ".local" / "share" / "applications" / "wine" / "Programs"
    if not base.is_dir():
        return entries
    for desk in sorted(base.rglob("*.desktop")):
        name, exe, icon = None, None, None
        try:
            text = desk.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("Name="):
                name = line[5:].strip()
            elif line.startswith("Exec="):
                exe = _exe_from_exec_line(line[5:].strip())
            elif line.startswith("Icon="):
                icon = line[5:].strip() or None
        if name:
            entries.append({"name": name, "exe": exe, "icon": icon})
    return entries


def _icon_for_program(name, exe, desktops):
    """Best icon path for a program: the Icon= value from Wine's
    .desktop file for it (only when it points at a real file).
    Returns a path string or None."""
    for desk in desktops:
        if not desk.get("icon"):
            continue
        icon = desk["icon"]
        if not (icon.startswith("/") and Path(icon).is_file()):
            continue  # theme names and missing files are not usable
        if exe and desk.get("exe") == exe:
            return icon
        dn, pn = _norm_name(desk["name"]), _norm_name(name)
        if dn and pn and (dn in pn or pn in dn):
            return icon
    return None


_EXE_IN_EXEC_RE = re.compile(
    r"""["']?((?:/[^\s"']+?|(?:[A-Za-z]:)?[^\s"']+?)\.exe)["']?""", re.I)


def _exe_from_exec_line(exec_line):
    """Pull the .exe path out of a Wine .desktop Exec line. Handles the
    usual `start.exe /Unix /unix/path/foo.exe` form (the path often
    contains a space, e.g. 'Program Files'), quoted Windows paths, and
    plain `wine /unix/path/foo.exe`."""
    if not exec_line:
        return None
    marker = "/Unix "
    idx = exec_line.find(marker)
    if idx != -1:
        rest = exec_line[idx + len(marker):].strip().strip('"').strip("'")
        m = re.search(r"\.exe", rest, re.I)
        if m:
            return rest[:m.end()]
        return rest or None
    m = re.search(r'"([^"]+\.exe)"', exec_line, re.I)
    if m:
        cand = m.group(1)
        if cand.startswith("/"):
            return cand
        unix = win_to_unix(cand)
        if unix is not None:
            return str(unix)
    for cand in _EXE_IN_EXEC_RE.findall(exec_line):
        cand = cand.strip()
        if cand.lower().endswith("start.exe"):
            continue
        if cand.startswith("/"):
            return cand
        unix = win_to_unix(cand)
        if unix is not None:
            return str(unix)
    return None


_SKIP_EXE_NAMES = ("uninstall", "unins", "update", "updater",
                  "setup", "installer", "repair")


def _pick_exe(location):
    """Best guess at a program's main .exe inside its install folder:
    an exe whose name matches the folder name (either way round),
    else the largest exe that is not an uninstaller/updater/setup
    program. Returns None when the only exes are uninstallers -
    launching one of those by mistake is worse than no launch."""
    if not location:
        return None
    folder = Path(location)
    if not folder.is_dir():
        return None
    exes = []
    try:
        for entry in folder.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".exe":
                exes.append(entry)
            elif entry.is_dir() and not entry.is_symlink():
                try:
                    for sub in entry.iterdir():
                        if sub.is_file() and sub.suffix.lower() == ".exe":
                            exes.append(sub)
                except OSError:
                    continue
    except OSError:
        return None
    if not exes:
        return None

    def is_skip(exe):
        stem = _norm_name(exe.stem)
        return any(skip in stem for skip in _SKIP_EXE_NAMES)

    want = _norm_name(folder.name)
    for exe in exes:
        stem = _norm_name(exe.stem)
        if want and stem and (want in stem or stem in want) \
                and not is_skip(exe):
            return str(exe)
    candidates = [exe for exe in exes if not is_skip(exe)]
    if not candidates:
        return None
    try:
        return str(max(candidates, key=lambda p: p.stat().st_size))
    except OSError:
        return str(candidates[0])


def scan_programs():
    """Every installed Windows program in the Wine prefix.

    Primary source: the Uninstall registry keys (real names, uninstall
    strings, install dates, estimated sizes). Falls back to Wine's
    generated .desktop files, then to Program Files folders (the
    folder scan always runs and merges by name, location, and exe,
    so a portable program shows up even when registry programs
    exist).
    Returns a list of dicts sorted by name:
      name, key, uninstall, location, install_date, size_bytes, exe,
      icon, prefix (None = shared prefix, else the per-program
      prefix path)
    """
    programs = []
    seen_names = set()
    reg_path = wine_prefix() / "system.reg"
    sections = parse_system_reg(reg_path) if reg_path.is_file() else {}
    desktops = _desktop_entries()

    prefix = _UNINSTALL_PREFIX.replace("\\\\", "\\")
    for section, values in sections.items():
        if not section.startswith(prefix):
            continue
        name = values.get("DisplayName")
        if not name:
            continue
        if values.get("SystemComponent"):
            continue
        key = section[len(prefix):]
        location = None
        if values.get("InstallLocation"):
            loc = win_to_unix(values["InstallLocation"])
            location = str(loc) if loc is not None else None
        size_bytes = None
        if isinstance(values.get("EstimatedSize"), int):
            size_bytes = values["EstimatedSize"] * 1024
        exe = None
        for desk in desktops:
            if desk["exe"] and location and desk["exe"].startswith(location):
                exe = desk["exe"]
                break
        if not exe:
            for desk in desktops:
                dn, pn = _norm_name(desk["name"]), _norm_name(name)
                if dn and pn and (dn in pn or pn in dn):
                    exe = desk["exe"]
                    break
        if not exe:
            exe = _pick_exe(location)
        programs.append({
            "name": name,
            "key": key,
            "uninstall": values.get("UninstallString") or "",
            "location": location,
            "install_date": format_install_date(values.get("InstallDate")),
            "size_bytes": size_bytes,
            "exe": exe,
            "icon": _icon_for_program(name, exe, desktops),
            "prefix": prefix_for_exe(exe),
        })
        seen_names.add(_norm_name(name))

    # .desktop entries with no registry match (portable apps Wine saw).
    seen_exes = {p["exe"] for p in programs if p["exe"]}
    for desk in desktops:
        norm = _norm_name(desk["name"])
        if norm in seen_names:
            continue
        if desk["exe"] and desk["exe"] in seen_exes:
            continue  # same program, different name (e.g. "FooApp" vs
            # "FooApp 2.0") - the registry entry already covers it.
        if any(len(norm) >= 3 and len(seen) >= 3
               and (norm in seen or seen in norm)
               for seen in seen_names):
            continue
        location = None
        if desk["exe"]:
            try:
                location = str(Path(desk["exe"]).parent)
            except (TypeError, ValueError):
                location = None
        programs.append({
            "name": desk["name"],
            "key": "",
            "uninstall": "",
            "location": location,
            "install_date": "",
            "size_bytes": None,
            "exe": desk["exe"],
            "icon": desk.get("icon")
            if desk.get("icon", "").startswith("/")
            and Path(desk["icon"]).is_file() else None,
            "prefix": prefix_for_exe(desk["exe"]),
        })
        seen_names.add(_norm_name(desk["name"]))
        if desk["exe"]:
            seen_exes.add(desk["exe"])

    # Bare folders under Program Files (portable programs Wine never
    # registered). Always runs; merges with what is already known so
    # a folder that belongs to a registry program is never listed
    # twice.
    known_locations = {p["location"] for p in programs if p["location"]}
    for base in program_files_dirs():
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink():
                continue
            if entry.name in ("Common Files", "WindowsApps"):
                continue
            if str(entry) in known_locations:
                continue
            norm = _norm_name(entry.name)
            if norm in seen_names:
                continue
            if any(len(norm) >= 3 and len(seen) >= 3
                   and (norm in seen or seen in norm)
                   for seen in seen_names):
                continue
            exe = _pick_exe(entry)
            if not exe or exe in seen_exes:
                continue
            programs.append({
                "name": entry.name,
                "key": "",
                "uninstall": "",
                "location": str(entry),
                "install_date": "",
                "size_bytes": None,
                "exe": exe,
                "icon": _icon_for_program(entry.name, exe, desktops),
                "prefix": prefix_for_exe(exe),
            })
            seen_names.add(norm)
            seen_exes.add(exe)
    programs.sort(key=lambda p: p["name"].lower())
    return programs


_MSIEXEC_RE = re.compile(
    r"msiexec(?:\.exe)?\"?\s+/[xX]\s*\{?([0-9A-Fa-f]{8}-[0-9A-Fa-f-]{27,31})\}?",
    re.I)
_QUOTED_EXE_RE = re.compile(r'"([^"]+\.exe)"', re.I)
# Bare Windows paths may contain spaces ("Program Files"), so match
# from the drive letter up to the first .exe, not up to whitespace.
_BARE_EXE_RE = re.compile(r"([A-Za-z]:\\[^\"\n]+?\.exe)", re.I)


def uninstall_command(prog):
    """Build the command that uninstalls a program, or None if there
    is no usable uninstall string. MSI entries go through Wine's own
    uninstaller; plain entries run their uninstaller exe. An MSI
    entry with no GUID in its uninstall string still gets a command:
    the registry key itself is usually the product GUID, and if the
    string names an .msi file, msiexec /x runs against it."""
    raw = (prog.get("uninstall") or "").strip()
    if not raw:
        return None
    m = _MSIEXEC_RE.search(raw)
    if m:
        return ["wine", "uninstaller", "--remove", "{%s}" % m.group(1)]
    if "msiexec" in raw.lower():
        key = (prog.get("key") or "").strip()
        if re.fullmatch(r"\{?[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{27,31}\}?",
                        key):
            guid = key if key.startswith("{") else "{%s}" % key
            return ["wine", "uninstaller", "--remove", guid]
        msi = re.search(r"/[xX]\s+\"?([^\s\"]+\.msi)\"?", raw, re.I)
        if msi:
            unix = win_to_unix(msi.group(1))
            target = str(unix) if unix is not None else msi.group(1)
            return ["wine", "msiexec", "/x", target]
        # msiexec with no usable product reference: open Wine's
        # uninstaller list so the user can pick the program.
        return ["wine", "uninstaller"]
    m = _QUOTED_EXE_RE.search(raw) or _BARE_EXE_RE.search(raw)
    if m:
        unix = win_to_unix(m.group(1))
        if unix is not None:
            return ["wine", str(unix)]
    return None


# ---------------------------------------------------------------- plugins

def _source_file_for_wrapper(kind, wrapper_name):
    """Given a yabridge wrapper .so name, find the Windows plugin file
    it wraps. Returns (source_path or None)."""
    stem = wrapper_name
    if stem.endswith(".so"):
        stem = stem[:-3]
    if kind == "VST3":
        # e.g. Remix.vst3.so -> Remix.vst3 ; Some.vst3 dir bundles too
        for base in (vst3_dir(), vst3_dir_x86()):
            for cand in (base / stem, base / (stem + ".vst3")):
                if cand.exists():
                    return cand
        return None
    if kind == "VST2":
        cand = vst2_dir() / (stem + ".dll")
        if cand.exists():
            return cand
        cand = vst2_dir() / stem
        return cand if cand.exists() else None
    if kind == "CLAP":
        for base in (vst3_dir(), vst3_dir_x86(), vst2_dir()):
            cand = base / (stem + ".clap")
            if cand.exists():
                return cand
        return None
    return None


def scan_plugins():
    """Every bridged plugin plus any Windows plugin file that has no
    bridge yet. Returns dicts sorted by name:
      name, kind, status, source, wrapper
    status: 'Bridged' | 'Source missing' | 'Not bridged'"""
    plugins = []
    seen_sources = set()
    for kind, folder in bridged_dirs().items():
        if not folder.is_dir():
            continue
        try:
            wrappers = sorted(folder.glob("*.so"))
        except OSError:
            continue
        for wrapper in wrappers:
            source = _source_file_for_wrapper(kind, wrapper.name)
            if source is not None:
                seen_sources.add(str(source))
                status = "Bridged"
            else:
                status = "Source missing"
            plugins.append({
                "name": wrapper.name[:-3] if wrapper.name.endswith(".so")
                        else wrapper.name,
                "kind": kind,
                "status": status,
                "source": str(source) if source else "",
                "wrapper": str(wrapper),
            })
    # Windows plugin files with no wrapper yet.
    dropped_folder = plugins_folder()
    for kind, folders, suffixes in (
        ("VST3", (vst3_dir(), vst3_dir_x86()), (".vst3",)),
        ("VST2", (vst2_dir(),), (".dll",)),
        ("CLAP", (vst3_dir(), vst3_dir_x86(), vst2_dir()), (".clap",)),
    ):
        for folder in folders:
            if not folder.is_dir():
                continue
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.suffix.lower() not in suffixes:
                    continue
                if str(entry) in seen_sources:
                    continue
                plugins.append({
                    "name": entry.name,
                    "kind": kind,
                    "status": "Not bridged",
                    "source": str(entry),
                    "wrapper": "",
                })
    # The file the user actually dropped, when it is still in
    # ~/WinLinx/Plugins: that is the "Source File" the UI shows.
    for plug in plugins:
        dropped = dropped_folder / plug["name"]
        plug["dropped"] = str(dropped) if dropped.exists() else ""
    plugins.sort(key=lambda p: (p["name"].lower(), p["kind"]))
    return plugins


# ---------------------------------------------------------------- per-program prefixes

WINDOWS_VERSIONS = ("win11", "win10", "win8", "win7", "winxp")
WINDOWS_VERSION_LABELS = {
    "win10": "Windows 10",
    "win11": "Windows 11",
    "win7": "Windows 7",
    "winxp": "Windows XP",
    "win8": "Windows 8",
}


def _load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _save_json_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _exe_key(exe_path):
    """Map key for a program: its .exe file name, lowercased.

    Windows paths use backslashes, which pathlib on Linux does not
    treat as separators, so they are normalised first.
    """
    if not exe_path:
        return None
    return Path(str(exe_path).replace("\\", "/")).name.lower()


def load_prefix_map():
    """{exe basename (lowercase): prefix path}."""
    data = _load_json_file(prefixes_path(), {})
    return {str(k).lower(): str(v) for k, v in data.items()
            if isinstance(v, str)}


def save_prefix_map(mapping):
    _save_json_file(prefixes_path(), dict(mapping))


def prefix_for_exe(exe_path):
    """The per-program prefix path for an exe, or None when the
    program runs in the shared prefix. A mapped prefix that no
    longer exists on disk counts as no mapping."""
    key = _exe_key(exe_path)
    if not key:
        return None
    path = load_prefix_map().get(key)
    if path and Path(path).is_dir():
        return path
    return None


def program_prefix_dir(exe_path):
    """Where a per-program prefix for this exe would live."""
    key = _exe_key(exe_path) or "program"
    safe = re.sub(r"[^a-z0-9_.-]", "_", key)
    return prefixes_root() / safe


def set_program_prefix(exe_path, prefix_path):
    """Map an exe to a per-program prefix (prefix_path None clears
    the mapping)."""
    key = _exe_key(exe_path)
    if not key:
        return
    mapping = load_prefix_map()
    if prefix_path is None:
        mapping.pop(key, None)
    else:
        mapping[key] = str(prefix_path)
    save_prefix_map(mapping)


def ensure_program_prefix(exe_path, timeout=300):
    """Create (wineboot --init) the per-program prefix for an exe
    and map it. Returns (ok, prefix_path or message)."""
    if not find_wine():
        return False, "Wine is not installed"
    dest = program_prefix_dir(exe_path)
    if not dest.is_dir():
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            append_log("WARNING: could not create prefix folder %s: %s"
                       % (dest, exc))
            return False, "the new prefix folder could not be created"
    env = dict(os.environ)
    env["WINEPREFIX"] = str(dest)
    try:
        proc = subprocess.run(["wineboot", "--init"], env=env,
                              capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        append_log("WARNING: could not create prefix for %s: %s"
                   % (exe_path, exc))
        return False, "Wine could not start the new prefix"
    if proc.returncode != 0:
        return False, "Wine could not start the new prefix"
    set_program_prefix(exe_path, dest)
    return True, str(dest)


def effective_prefix(exe_path):
    """The prefix a program actually runs in: its own when mapped,
    else the shared prefix."""
    own = prefix_for_exe(exe_path)
    return Path(own) if own else wine_prefix()


def launch_env(exe_path):
    """Environment for launching a program: WINEPREFIX set to its
    effective prefix."""
    env = dict(os.environ)
    env["WINEPREFIX"] = str(effective_prefix(exe_path))
    return env


def load_program_settings():
    return _load_json_file(program_settings_path(), {})


def program_windows_version(exe_path):
    key = _exe_key(exe_path)
    if not key:
        return None
    settings = load_program_settings().get(key)
    if isinstance(settings, dict):
        return settings.get("windows_version")
    return None


def set_program_windows_version(exe_path, version):
    """Set the Windows version for a program's effective prefix via
    winecfg. Returns (ok, message)."""
    if version not in WINDOWS_VERSIONS:
        return False, "unknown Windows version: %s" % version
    if not find_wine():
        return False, "Wine is not installed"
    env = launch_env(exe_path)
    try:
        proc = subprocess.run(["wine", "winecfg", "-v", version],
                              env=env, capture_output=True, text=True,
                              timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        append_log("WARNING: could not set Windows version for %s: %s"
                   % (exe_path, exc))
        return False, "the Windows version could not be set"
    if proc.returncode != 0:
        return False, "the Windows version could not be set"
    key = _exe_key(exe_path)
    settings = load_program_settings()
    entry = settings.get(key) if isinstance(settings.get(key), dict) \
        else {}
    entry["windows_version"] = version
    settings[key] = entry
    _save_json_file(program_settings_path(), settings)
    return True, ""


# ---------------------------------------------------------------- prefix backup / restore

def backup_prefixes(dest_path, progress=None):
    """Tar (gzipped) the shared prefix plus every per-program prefix
    into dest_path. progress(done_files, done_bytes) is called as
    files are added; when it returns False the backup is cancelled
    and the partial file removed. Returns (ok, message)."""
    dest = Path(dest_path)
    sources = []
    if wine_prefix().is_dir():
        sources.append((wine_prefix(), "wine"))
    if prefixes_root().is_dir():
        sources.append((prefixes_root(), "prefixes"))
    if not sources:
        return False, "no Wine prefix to back up"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, str(exc)
    done_files, done_bytes = 0, 0
    cancelled = False
    # Report progress by bytes as well as files: a backup of a few
    # huge files would otherwise show nothing until file 50.
    last_report_bytes = 0
    try:
        with tarfile.open(dest, "w:gz") as tf:
            for source, arcbase in sources:
                for root, dirs, files in os.walk(source,
                                                 followlinks=False):
                    for name in files:
                        full = Path(root) / name
                        arc = Path(arcbase) / full.relative_to(source)
                        try:
                            tf.add(str(full), arcname=str(arc),
                                   recursive=False)
                            done_files += 1
                            done_bytes += full.stat().st_size
                        except (OSError, tarfile.TarError):
                            continue
                        if progress and (
                                done_files % 50 == 0
                                or done_bytes - last_report_bytes
                                >= 1024 * 1024):
                            last_report_bytes = done_bytes
                            if progress(done_files, done_bytes) is False:
                                cancelled = True
                                break
                    if cancelled:
                        break
                if cancelled:
                    break
    except (OSError, tarfile.TarError) as exc:
        try:
            dest.unlink()
        except OSError:
            pass
        return False, str(exc)
    if cancelled:
        try:
            dest.unlink()
        except OSError:
            pass
        return False, "cancelled"
    if progress:
        progress(done_files, done_bytes)
    return True, "%d files, %s" % (done_files, format_size(done_bytes))


def list_aside_prefixes():
    """Every ".before-restore-<stamp>" prefix copy left by earlier
    restores, newest first (by name, which carries the stamp)."""
    aside = []
    for target in (wine_prefix(), prefixes_root()):
        parent = target.parent
        try:
            entries = list(parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(target.name + ".before-restore-"):
                aside.append(entry)
    aside.sort(key=lambda p: p.name, reverse=True)
    return aside


def prune_aside_prefixes(keep_latest=1):
    """Remove old aside prefix copies, keeping the newest
    keep_latest per prefix (the shared prefix and the per-program
    prefixes each keep their own latest). Returns the list of
    removed paths. Never raises."""
    aside = list_aside_prefixes()
    groups = {}
    for entry in aside:
        base = entry.name.split(".before-restore-")[0]
        groups.setdefault(base, []).append(entry)
    removed = []
    for entries in groups.values():
        for entry in entries[keep_latest:]:
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed.append(entry)
            except OSError:
                continue
    return removed


def restore_prefixes(archive_path, progress=None):
    """Restore a backup made by backup_prefixes over the current
    prefixes. The current shared prefix is moved aside (not deleted)
    so a failed restore can be undone by hand. progress(done, 0) is
    called as files are restored; when it returns False the restore
    is cancelled (the moved-aside prefix stays where it is).
    Returns (ok, message).
    Extraction is filtered (filter="data") and confined to the two
    known destinations."""
    archive = Path(archive_path)
    if not archive.is_file():
        return False, "backup file not found"
    targets = {"wine": wine_prefix(), "prefixes": prefixes_root()}
    try:
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()
    except (OSError, tarfile.TarError) as exc:
        return False, "could not read the backup: %s" % exc
    for member in members:
        top = member.name.split("/", 1)[0]
        if top not in targets:
            return False, ("backup contains an unexpected entry: %s"
                           % member.name)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    moved_aside = []
    try:
        for label, target in targets.items():
            if not any(m.name.split("/", 1)[0] == label
                       for m in members):
                continue
            if target.is_dir():
                aside = target.with_name(target.name
                                         + ".before-restore-" + stamp)
                try:
                    os.replace(target, aside)
                    moved_aside.append((target, aside))
                except OSError as exc:
                    return False, ("could not move the current prefix "
                                   "aside: %s" % exc)
        done = 0
        cancelled = False
        with tarfile.open(archive, "r:gz") as tf:
            for member in members:
                top = member.name.split("/", 1)[0]
                dest = targets[top]
                dest.mkdir(parents=True, exist_ok=True)
                member.name = member.name.split("/", 1)[1] \
                    if "/" in member.name else member.name
                if not member.name:
                    continue
                tf.extract(member, path=str(dest), filter="data")
                done += 1
                if progress and done % 50 == 0:
                    if progress(done, 0) is False:
                        cancelled = True
                        break
    except (OSError, tarfile.TarError) as exc:
        return False, "restore failed partway: %s (the old prefix is \
still on disk with .before-restore-%s in its name)" % (exc, stamp)
    if cancelled:
        return False, ("cancelled after %d files (the old prefix is "
                       "still on disk with .before-restore-%s in its "
                       "name)" % (done, stamp))
    if progress:
        progress(done, 0)
    # Repeated restores used to pile up full prefix duplicates; keep
    # only the newest aside copy.
    pruned = prune_aside_prefixes(keep_latest=1)
    message = "%d files restored" % done
    if moved_aside:
        message += (". The old prefix was moved aside to %s"
                    % ", ".join(str(aside) for _target, aside
                                in moved_aside))
    if pruned:
        message += (" (%d older aside %s removed)"
                    % (len(pruned),
                       "copy was" if len(pruned) == 1 else "copies were"))
    return True, message


# ---------------------------------------------------------------- self-update check

def _parse_version(text):
    """'v0.2' / '0.2' / '0.2.1' -> (0, 2) / (0, 2, 1). Junk -> (0,)."""
    parts = []
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        chunk = re.sub(r"[^0-9].*$", "", chunk)
        if chunk == "":
            break
        try:
            parts.append(int(chunk))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


def check_for_update(timeout=30):
    """Ask GitHub for the latest WinLinx release and compare it with
    this install. Returns {"available": bool, "latest": str,
    "url": str, "zip_url": str, "zip_name": str, "sha_url": str,
    "error": str}. The sidecar URL is the release's SHA-256 checksum
    file: an update is only ever installed when its checksum
    matches. Never raises."""
    result = {"available": False, "latest": "", "url": RELEASES_URL,
              "zip_url": "", "zip_name": "", "sha_url": "", "error": ""}
    try:
        request = urllib.request.Request(
            _update_api_url(),
            headers={"User-Agent": "WinLinx/%s" % __version__,
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced as error text
        result["error"] = str(exc)
        return result
    tag = str(data.get("tag_name") or "")
    result["latest"] = tag
    result["url"] = str(data.get("html_url") or RELEASES_URL)
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name.startswith("winlinx-v") and name.endswith(".zip"):
            result["zip_url"] = url
            result["zip_name"] = name
        elif name.startswith("winlinx-v") \
                and name.endswith(".zip.sha256"):
            result["sha_url"] = url
    if tag and _parse_version(tag) > _parse_version(__version__):
        result["available"] = True
    return result


def download_file(url, dest, timeout=300, progress=None):
    """Download a URL to dest (with retries). progress(done, total)
    is called as bytes arrive; when it returns False the download
    is cancelled and the partial file removed. Returns (ok, message).
    """
    last_error = "download failed"
    for attempt in range(3):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "WinLinx/%s" % __version__})
            with urllib.request.urlopen(request,
                                        timeout=timeout) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                dest = Path(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                cancelled = False
                with open(dest, "wb") as fh:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if progress and progress(done, total) is False:
                            cancelled = True
                            break
                if cancelled:
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    return False, "cancelled"
            return True, ""
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last_error = str(exc)
            time.sleep(2 * (attempt + 1))
    return False, last_error
