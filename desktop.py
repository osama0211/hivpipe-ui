"""Desktop entry point — the thing the installed application actually runs.

Double-clicking the app lands here. It starts the server on a private port
nobody else can reach, opens a window, and shuts everything down when that
window closes. No terminal, no URL to type, no Python to install.

The window is a real native one when pywebview is available. Without it the
app falls back to the default browser, which looks different but behaves the
same — the fallback exists so a missing GUI library degrades instead of
failing.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "HIV Pipeline Runner"
WINDOW_SIZE = (1280, 860)
MIN_SIZE = (900, 600)


def bundle_dir():
    """Where the app's own files live, bundled or not."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def user_dir():
    """Per-user folder for settings, runs and references.

    A packaged app must not write next to its executable: on Windows that is
    Program Files, which is read-only for a normal user.
    """
    env = os.environ.get("HIVPIPE_HOME")
    if env:
        base = Path(env)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA",
                                   Path.home() / "AppData" / "Local")) / "HIVPipeRunner"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "HIVPipeRunner"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME",
                                   Path.home() / ".local" / "share")) / "hivpipe-runner"
    base.mkdir(parents=True, exist_ok=True)
    return base


def prepare_environment():
    """Point the app at per-user folders before it is imported."""
    home = user_dir()
    for name in ("reads", "runs", "refs", "work"):
        (home / name).mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("HIVPIPE_DATA_DIR", str(home / "reads"))
    os.environ.setdefault("HIVPIPE_RUNS_DIR", str(home / "runs"))
    os.environ.setdefault("HIVPIPE_REFS_DIR", str(home / "refs"))
    os.environ.setdefault("HIVPIPE_WORK_DIR", str(home / "work"))
    os.environ.setdefault("HIVPIPE_APP_DIR", str(bundle_dir()))

    # A bundled Nextflow, if one shipped with the app.
    bundled_nf = bundle_dir() / ("nextflow.bat" if sys.platform == "win32"
                                 else "nextflow")
    if bundled_nf.exists():
        os.environ.setdefault("HIVPIPE_NEXTFLOW", str(bundled_nf))

    # Seed the reference folder on first run so the app is usable immediately.
    shipped_refs = bundle_dir() / "refs"
    if shipped_refs.is_dir():
        import shutil
        for src in shipped_refs.glob("*.fasta"):
            dst = home / "refs" / src.name
            if not dst.exists():
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    pass
    return home


def in_wsl():
    """True inside WSL, where there is no Linux GUI but Windows can show one."""
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def open_in_browser(url):
    """Open the interface in whatever browser this system can reach.

    Under WSL the Linux-side browser usually does not exist, but Windows can
    open it and reaches the server through WSL's localhost forwarding.
    """
    if in_wsl():
        for argv in (["wslview", url],
                     ["explorer.exe", url],
                     ["cmd.exe", "/c", "start", "", url]):
            try:
                subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True
            except (FileNotFoundError, OSError):
                continue
        return False
    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001
        return False


def free_port():
    """An unused port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until_up(port, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def serve(port):
    import uvicorn

    import app as application

    uvicorn.run(application.app, host="127.0.0.1", port=port,
                log_level="warning", access_log=False)


def main():
    home = prepare_environment()
    sys.path.insert(0, str(bundle_dir()))

    port = free_port()
    thread = threading.Thread(target=serve, args=(port,), daemon=True)
    thread.start()

    if not wait_until_up(port):
        _fatal("The application could not start its own service.\n\n"
               f"Settings and results folder:\n{home}")
        return 1

    url = f"http://127.0.0.1:{port}/"

    # Leave the address somewhere findable. A packaged app has no console, so
    # this is the only way to reach the interface if the window fails to open.
    try:
        (home / "address.txt").write_text(url + "\n")
    except OSError:
        pass

    # A native window needs a GUI toolkit. WSL has none, and a bare Linux
    # server may not either, so the browser is the fallback rather than an
    # error. pywebview reports a missing toolkit when start() runs, not when
    # it is imported, so both failures have to be caught.
    if not in_wsl():
        try:
            import webview

            window = webview.create_window(
                APP_NAME, url,
                width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                min_size=MIN_SIZE, confirm_close=False,
            )

            def on_closed():
                # The server thread is a daemon; ending the process stops it.
                os._exit(0)

            window.events.closed += on_closed
            webview.start()
            return 0
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001 — any toolkit failure
            print(f"No desktop window available ({type(exc).__name__}); "
                  f"opening a browser instead.", flush=True)

    return run_in_browser(url, thread)


def run_in_browser(url, thread):
    opened = open_in_browser(url)
    print(f"\n  {APP_NAME} is running.", flush=True)
    print(f"  {url}", flush=True)
    if not opened:
        print("  Could not open a browser automatically — "
              "paste that address into one.", flush=True)
    print("  Press Ctrl+C to quit.\n", flush=True)
    try:
        while thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping.", flush=True)
    return 0


def _fatal(message):
    """Report a startup failure without assuming there is a console."""
    sys.stderr.write(message + "\n")
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
        else:
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror(APP_NAME, message)
    except Exception:  # noqa: BLE001 — nothing left to try
        pass


if __name__ == "__main__":
    sys.exit(main())
