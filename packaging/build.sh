#!/usr/bin/env bash
#
# Build the desktop application for whichever platform this runs on.
#
#   ./packaging/build.sh
#
# PyInstaller cannot cross-compile: a Windows .exe must be built on Windows,
# a .app on macOS. The GitHub Actions workflow in .github/workflows builds
# all three; this script is for building one by hand.

set -euo pipefail
cd "$(dirname "$0")/.."

echo "Installing build dependencies"
python3 -m pip install -q --upgrade pip
python3 -m pip install -q -r requirements.txt
python3 -m pip install -q pyinstaller

# pywebview gives a native window. Without it the app opens the default
# browser instead, which still works, so a failure here is not fatal.
python3 -m pip install -q pywebview 2>/dev/null \
    || echo "  pywebview unavailable — the app will use the default browser"

mkdir -p packaging/vendor

# Bundle the Nextflow launcher if it is here. Nextflow needs Java, which is
# not bundled: local runs go through Docker Desktop, which supplies its own.
if [ ! -f packaging/vendor/nextflow ] && [ -f nextflow ]; then
    cp nextflow packaging/vendor/nextflow
fi

echo "Building"
rm -rf build dist
python3 -m PyInstaller packaging/hivpipe.spec --noconfirm --clean

OUT="dist/HIV Pipeline Runner"
if [ -d "$OUT.app" ]; then OUT="$OUT.app"; fi

echo
echo "Built: $OUT"
du -sh "$OUT" 2>/dev/null || true
echo
case "$(uname -s)" in
    Darwin) echo "Distribute by zipping the .app, or build a .dmg with create-dmg." ;;
    Linux)  echo "Distribute as a tarball of the folder, or build an AppImage." ;;
    *)      echo "On Windows, build an installer with Inno Setup: see packaging/installer.iss" ;;
esac
