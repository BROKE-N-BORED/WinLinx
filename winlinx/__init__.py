"""WinLinx - Windows programs and VST plug-ins on Linux, zero setup.

The version lives in common.py (__version__) - this module only
re-exports it, so there is exactly one place to bump.
"""

try:
    from .common import __version__
except ImportError:  # direct load (flat install layout has no package)
    import re
    from pathlib import Path
    _match = re.search(
        r'^__version__ = "([^"]+)"',
        (Path(__file__).parent / "common.py").read_text(
            encoding="utf-8"),
        re.M)
    __version__ = _match.group(1) if _match else "unknown"
