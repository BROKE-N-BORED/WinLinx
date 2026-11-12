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

import os
import re
import shutil
import subprocess
from pathlib import Path


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
    yabridgectl subcommands v0.1 relies on are add/sync."""
    return "Installed" if find_yabridgectl() else "Not installed"


def run_yabridgectl_sync(timeout=180):
    """Run `yabridgectl sync`. Returns (ok, message)."""
    ctl = find_yabridgectl()
    if not ctl:
        return False, "yabridgectl not found"
    try:
        proc = subprocess.run([ctl, "sync"], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip()
    err = (proc.stderr or proc.stdout or "").strip()
    return False, err or ("exit code %d" % proc.returncode)


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
    """Wine's generated .desktop files: [{name, exe}] where exe is a
    unix path to the program's main .exe (or None)."""
    entries = []
    base = home() / ".local" / "share" / "applications" / "wine" / "Programs"
    if not base.is_dir():
        return entries
    for desk in sorted(base.rglob("*.desktop")):
        name, exe = None, None
        try:
            text = desk.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("Name="):
                name = line[5:].strip()
            elif line.startswith("Exec="):
                exe = _exe_from_exec_line(line[5:].strip())
        if name:
            entries.append({"name": name, "exe": exe})
    return entries


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


def _pick_exe(location):
    """Best guess at a program's main .exe inside its install folder:
    an exe whose name resembles the folder name, else the largest exe
    in the top level (plus one level of subfolders)."""
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
    want = _norm_name(folder.name)
    for exe in exes:
        if want and want in _norm_name(exe.stem):
            return str(exe)
    try:
        return str(max(exes, key=lambda p: p.stat().st_size))
    except OSError:
        return str(exes[0])


def scan_programs():
    """Every installed Windows program in the Wine prefix.

    Primary source: the Uninstall registry keys (real names, uninstall
    strings, install dates, estimated sizes). Falls back to Wine's
    generated .desktop files, then to Program Files folders.
    Returns a list of dicts sorted by name:
      name, key, uninstall, location, install_date, size_bytes, exe
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
            # "FooApp 2.0") — the registry entry already covers it.
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
        })
        seen_names.add(_norm_name(desk["name"]))

    # Last resort: bare folders under Program Files.
    if not programs:
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
                exe = _pick_exe(entry)
                if not exe:
                    continue
                programs.append({
                    "name": entry.name,
                    "key": "",
                    "uninstall": "",
                    "location": str(entry),
                    "install_date": "",
                    "size_bytes": None,
                    "exe": exe,
                })
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
    uninstaller; plain entries run their uninstaller exe."""
    raw = (prog.get("uninstall") or "").strip()
    if not raw:
        return None
    m = _MSIEXEC_RE.search(raw)
    if m:
        return ["wine", "uninstaller", "--remove", "{%s}" % m.group(1)]
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
    for kind, folders, suffixes in (
        ("VST3", (vst3_dir(), vst3_dir_x86()), (".vst3",)),
        ("VST2", (vst2_dir(),), (".dll",)),
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
    plugins.sort(key=lambda p: (p["name"].lower(), p["kind"]))
    return plugins
