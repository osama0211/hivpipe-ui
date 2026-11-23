"""Entry point for the packaged executable.

Tries the native window first, because it needs no port, no browser and no
webview runtime. Falls back to serving the web interface only when there is
no display to draw on — a headless server, or WSL without WSLg.

    python launcher.py          # native window, or the web interface if none
    python launcher.py --web    # force the web interface
    python launcher.py --gui    # force the native window, fail if impossible
"""

from __future__ import annotations

import os
import sys

import desktop


def native_available():
    """Can Tk actually open a window here?

    Importing tkinter succeeds on a headless machine; only creating a root
    window fails, so that is what gets tested.
    """
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False
    try:
        import tkinter
    except ImportError:
        return False
    try:
        probe = tkinter.Tk()
    except Exception:                                       # noqa: BLE001
        return False
    probe.destroy()
    return True


def main():
    args = set(sys.argv[1:])
    desktop.prepare_environment()

    if "--web" in args:
        return desktop.main()

    if "--gui" in args or native_available():
        import gui
        return gui.main()

    print("No display found, so the window cannot open. Serving the web "
          "interface instead.")
    return desktop.main()


if __name__ == "__main__":
    main()
