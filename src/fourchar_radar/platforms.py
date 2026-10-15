"""Platform differences, resolved in one place.

Three things differ between Linux, macOS and Windows in ways that matter for
this tool:

* **Where the database lives.** A relative ``state/radar.db`` looks convenient
  and then quietly breaks the moment the user runs the radar from a systemd
  unit, a cron job, or a different terminal tab. The XDG / Apple / Windows
  convention for per-user data is the right home for it.
* **Signals.** ``loop.add_signal_handler`` does not exist on Windows, and
  ``SIGTERM`` is not a thing there. Ctrl-C arrives as ``KeyboardInterrupt``.
* **The venv/bootstrap story.** Whether ``ensurepip`` is present, and whether
  the system Python is externally managed, varies by distro.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "fourchar-radar"

IS_WINDOWS = sys.platform.startswith("win")


def user_data_dir() -> Path:
    """Per-user data directory for the radar, following platform convention.

    * Linux/BSD: ``$XDG_DATA_HOME/fourchar-radar`` (default
      ``~/.local/share/fourchar-radar``)
    * macOS: ``~/Library/Application Support/fourchar-radar``
    * Windows: ``%LOCALAPPDATA%\\fourchar-radar``
    """
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base) if base else Path.home() / ".local" / "share"

    return root / APP_NAME


def default_db_path() -> str:
    """Absolute path to the SQLite database, with its parent created."""
    path = user_data_dir() / "radar.db"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Read-only home (some containers): fall back to the working directory
        # so the radar still runs rather than refusing to start.
        path = Path.cwd() / "state" / "radar.db"
        path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def supports_signal_handlers() -> bool:
    """True if ``loop.add_signal_handler`` is usable on this platform."""
    return not IS_WINDOWS


def which(name: str) -> str | None:
    """Locate an executable without depending on `shutil` semantics."""
    import shutil

    return shutil.which(name)


def python_command() -> str:
    """The interpreter to use for subprocesses (pip, venv creation)."""
    if IS_WINDOWS:
        return sys.executable or "python"
    return sys.executable or "python3"
