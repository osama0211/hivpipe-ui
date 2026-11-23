# PyInstaller spec for the HIV Pipeline Runner desktop application.
#
# Build with:  pyinstaller packaging/hivpipe.spec --noconfirm
#
# Produces a one-folder application. One-folder rather than one-file on
# purpose: a single .exe unpacks itself to a temporary directory on every
# launch, which on Windows means a multi-second delay and frequent
# false positives from antivirus software. The folder version starts
# immediately, and the installer hides the folder from the user anyway.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent

# The workflow travels inside the bundle: a user who installs the app gets
# the pipeline definitions with it, and never sees these files.
datas = [
    (str(ROOT / "static"), "static"),
    (str(ROOT / "modules"), "modules"),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "bin"), "bin"),
    (str(ROOT / "refs"), "refs"),
    (str(ROOT / "main.nf"), "."),
    (str(ROOT / "nextflow.config"), "."),
    (str(ROOT / "settings.yaml"), "."),
    (str(ROOT / "qc.py"), "."),
]

# The PNG travels with the build so a Linux .desktop entry has something to
# point at; Windows and macOS take the icon from the executable itself.
_png = ROOT / "packaging" / "icon.png"
if _png.exists():
    datas.append((str(_png), "."))

# A bundled Nextflow launcher, if one was placed here before building.
for candidate in ("nextflow", "nextflow.bat"):
    p = ROOT / "packaging" / "vendor" / candidate
    if p.exists():
        datas.append((str(p), "."))

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("paramiko")
    + [
        "app", "assistant", "diagnostics", "gui", "hpc", "qc", "resources",
        "tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox",
        "fastapi", "starlette", "pydantic",
        "multipart", "yaml",
        "uvicorn.logging", "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ]
)

# pywebview's backend differs per platform and is imported dynamically.
try:
    import webview  # noqa: F401
    hiddenimports += collect_submodules("webview")
except ImportError:
    pass

# launcher.py picks the native window first and falls back to the served
# interface, so the executable never depends on a browser being reachable.
a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter.test", "test", "unittest", "pydoc_data",
              "matplotlib", "numpy", "pandas", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# Each platform wants its own format, and handing Windows an .icns (or the
# reverse) produces an executable with the generic blank icon.
_preferred = {"win32": ("icon.ico", "icon.png"),
              "darwin": ("icon.icns", "icon.png")}.get(
                  sys.platform, ("icon.png", "icon.ico"))
icon = None
for name in _preferred:
    p = ROOT / "packaging" / name
    if p.exists():
        icon = str(p)
        break
if icon is None:
    print("packaging: no icon found — run python packaging/make_icon.py")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HIV Pipeline Runner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console window: this is a desktop application, not a script.
    console=False,
    disable_windowed_traceback=False,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HIV Pipeline Runner",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="HIV Pipeline Runner.app",
        icon=icon,
        bundle_identifier="org.hivpipe.runner",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            # The app talks only to its own loopback port and the user's
            # chosen cluster.
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
