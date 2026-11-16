#!/usr/bin/env python3
"""ClamAV scan gate for WinLinx.

Everything that enters through the watched folders is scanned BEFORE it
is allowed to install or run. Flagged files/folders are moved to
quarantine and never executed; the user decides their fate in the
Program Manager (Allow = install anyway, Delete = remove for good).

Facts verified against the ClamAV docs / man pages for Ubuntu 24.04:
  - packages: `clamav` (clamscan) + `clamav-freshclam` (updater +
    automatic-update service). No daemon needed: clamscan loads the
    signatures itself on each run.
  - `clamscan --detect-pua=yes` enables Possibly Unwanted Application
    detection (bloatware, adware, toolbars).
  - exit codes: 0 = clean, 1 = threat(s) found, 2 = error.
  - default --max-filesize (25 MB) SKIPS larger files and calls them
    clean, so installers must be scanned with raised limits.
  - --max-scantime=0 disables the default 2-minute per-file cap.
  - infected output lines look like: /path/file: Threat.Name FOUND
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

import common

SCAN_TIMEOUT = 600          # seconds; big installers on a slow CPU
MAX_SIZE = "1000M"          # raised limits so big installers get scanned
_THREAT_LINE_RE = re.compile(r"^(.+): (.+) FOUND$")


# ---------------------------------------------------------------- discovery

def find_clamscan():
    return shutil.which("clamscan")


def scanner_status():
    """{'installed': bool, 'version': str, 'sig_version': str,
    'sig_date': str}. Version line looks like:
    'ClamAV 1.5.3/28062/Thu Jul 16 02:25:13 2026'."""
    status = {"installed": False, "version": "",
              "sig_version": "", "sig_date": ""}
    clamscan = find_clamscan()
    if not clamscan:
        return status
    status["installed"] = True
    try:
        proc = subprocess.run([clamscan, "--version"], capture_output=True,
                              text=True, timeout=30)
        out = (proc.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return status
    status["version"] = out
    parts = out.split("/")
    if len(parts) >= 3:
        status["sig_version"] = parts[1].strip()
        status["sig_date"] = parts[2].strip()
    return status


# ---------------------------------------------------------------- scanning

def scan_path(path, timeout=SCAN_TIMEOUT):
    """Scan a file or folder. Returns a dict:
      clean: True / False / None (None = could not scan)
      threats: [(file_path, threat_name), ...]
      error: human-readable error string, or ""
      missing: True when clamscan is not installed at all
    """
    clamscan = find_clamscan()
    if not clamscan:
        return {"clean": None, "threats": [],
                "error": "ClamAV is not installed", "missing": True}
    cmd = [clamscan, "-r", "-i",
           "--detect-pua=yes",
           "--max-filesize=%s" % MAX_SIZE,
           "--max-scansize=%s" % MAX_SIZE,
           "--max-scantime=0",
           str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"clean": None, "threats": [],
                "error": "scan timed out after %d seconds" % timeout,
                "missing": False}
    except OSError as exc:
        return {"clean": None, "threats": [], "error": str(exc),
                "missing": False}
    threats = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        m = _THREAT_LINE_RE.match(line)
        if m:
            threats.append((m.group(1), m.group(2)))
    if proc.returncode == 0:
        return {"clean": True, "threats": [], "error": "", "missing": False}
    if proc.returncode == 1:
        return {"clean": False, "threats": threats, "error": "",
                "missing": False}
    err = (proc.stderr or "").strip().splitlines()
    detail = err[-1] if err else ("exit code %d" % proc.returncode)
    return {"clean": None, "threats": threats,
            "error": "clamscan error: %s" % detail, "missing": False}


# ---------------------------------------------------------------- hashing

def sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- quarantine

def quarantine_dir():
    return common.layer_dir() / "quarantine"


def manifest_path():
    return common.layer_dir() / "quarantine.json"


def allowlist_path():
    return common.layer_dir() / "allowed.json"


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def load_quarantine():
    """Manifest entries, newest first."""
    items = _load_json(manifest_path(), [])
    items.sort(key=lambda e: e.get("date", ""), reverse=True)
    return items


def quarantine_item(src_path, threats):
    """Move a flagged file/folder into quarantine and record it.
    Returns the manifest entry."""
    src = Path(src_path)
    qdir = quarantine_dir()
    qdir.mkdir(parents=True, exist_ok=True)
    dest = qdir / src.name
    n = 1
    while dest.exists():
        n += 1
        dest = qdir / ("%s (%d)%s" % (src.stem, n, src.suffix))
    shutil.move(str(src), str(dest))
    entry = {
        # Unique id: microsecond timestamp + random hex. Two files
        # with the same name quarantined in the same second used to
        # share an id, and deleting one deleted both.
        "id": "%s-%s-%s" % (
            datetime.now().strftime("%Y%m%d%H%M%S%f"),
            uuid.uuid4().hex[:8], src.name),
        "name": src.name,
        "original": str(src),
        "quarantined": str(dest),
        "threats": sorted({name for _, name in threats}),
        "files": [f for f, _ in threats],
        "date": datetime.now().isoformat(timespec="seconds"),
        "is_dir": dest.is_dir(),
    }
    items = _load_json(manifest_path(), [])
    items.append(entry)
    _save_json(manifest_path(), items)
    return entry


def delete_quarantined(entry_id):
    """Permanently delete ONE quarantined item (the first entry with
    this id) and its manifest entry. The quarantined file is only
    deleted when it is actually inside the quarantine folder."""
    items = _load_json(manifest_path(), [])
    kept = []
    deleted = False
    qdir = quarantine_dir().resolve()
    for entry in items:
        if not deleted and entry.get("id") == entry_id:
            target = Path(entry["quarantined"])
            try:
                inside = qdir in target.resolve().parents \
                    or target.resolve() == qdir
            except OSError:
                inside = False
            if inside:
                try:
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    elif target.exists() or target.is_symlink():
                        target.unlink()
                except OSError:
                    pass
            deleted = True
        else:
            kept.append(entry)
    _save_json(manifest_path(), kept)
    return deleted


def _drop_one_entry(entry_id):
    """Remove exactly one manifest entry with this id (the first)."""
    items = _load_json(manifest_path(), [])
    kept = []
    dropped = False
    for entry in items:
        if not dropped and entry.get("id") == entry_id:
            dropped = True
        else:
            kept.append(entry)
    _save_json(manifest_path(), kept)


# ---------------------------------------------------------------- allowlist

def _load_allowlist():
    data = _load_json(allowlist_path(), {})
    if not isinstance(data, dict):
        return {"files": {}, "folders": []}
    data.setdefault("files", {})
    data.setdefault("folders", [])
    return data


def is_allowlisted(path):
    """Files are allowlisted by content hash, folders by path. A file
    that changed since it was allowed is scanned again."""
    path = Path(path)
    data = _load_allowlist()
    if path.is_dir():
        return str(path) in data["folders"]
    if not path.is_file():
        return False
    try:
        return sha256_file(path) in data["files"]
    except OSError:
        return False


def allow_item(entry):
    """The user trusts this item: record it in the allowlist, move it
    back to where it came from, and drop its manifest entry.
    Returns the restored Path, or None if the quarantined copy is gone."""
    target = Path(entry["quarantined"])
    if not target.exists():
        _drop_one_entry(entry.get("id"))
        return None
    data = _load_allowlist()
    if target.is_dir():
        if entry["original"] not in data["folders"]:
            data["folders"].append(entry["original"])
    else:
        try:
            digest = sha256_file(target)
        except OSError:
            digest = ""
        if digest:
            data["files"][digest] = entry["name"]
    _save_json(allowlist_path(), data)
    original = Path(entry["original"])
    original.parent.mkdir(parents=True, exist_ok=True)
    dest = original
    n = 1
    while dest.exists():
        n += 1
        dest = original.with_name("%s (%d)%s" % (original.stem, n,
                                                 original.suffix))
    shutil.move(str(target), str(dest))
    _drop_one_entry(entry.get("id"))
    return dest


# ---------------------------------------------------------------- CLI gate
# Used by run-windows-file (the double-click handler) so the scan gate
# lives in exactly one place. Exit codes: 0 = clean, 1 = quarantined
# (threat names printed), 2 = could not scan (reason printed).

def gate_cli(path):
    result = scan_path(path)
    if result.get("missing") or result.get("error"):
        print(result.get("error") or "ClamAV is not installed")
        return 2
    if result["clean"]:
        return 0
    names = sorted({name for _, name in result["threats"]})
    quarantine_item(path, result["threats"])
    print(", ".join(names))
    return 1


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2 and sys.argv[1] in ("--version", "-V"):
        print("WinLinx scanner %s" % common.__version__)
        sys.exit(0)
    if len(sys.argv) == 3 and sys.argv[1] == "gate":
        sys.exit(gate_cli(sys.argv[2]))
    print("usage: scanner.py [--version | gate <path>]")
    sys.exit(2)
