@echo off
REM Build the Windows application. Double-click this file, or run it from
REM a Command Prompt in the project folder.
REM
REM It produces dist\HIV Pipeline Runner\HIV Pipeline Runner.exe — an icon
REM you can double-click. If Inno Setup is installed it also builds a
REM one-file installer that puts a shortcut on your desktop.
REM
REM Run this in WINDOWS, not in WSL. PyInstaller cannot build a Windows
REM program from inside Linux: a build started in WSL produces a Linux
REM binary that Windows cannot open.

setlocal
cd /d "%~dp0\.."

echo.
echo === HIV Pipeline Runner: Windows build ===
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found.
    echo Install it from https://www.python.org/downloads/windows/
    echo and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

python -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo This Python has no tkinter, which the window needs.
    echo Re-run the Python installer, choose Modify, and tick
    echo "tcl/tk and IDLE".
    echo.
    pause
    exit /b 1
)

echo Installing build dependencies...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
python -m pip install pyinstaller pillow --quiet
if errorlevel 1 (
    echo Could not install the build dependencies. Check your connection.
    pause
    exit /b 1
)

if not exist packaging\icon.ico (
    echo Drawing the application icon...
    python packaging\make_icon.py
)

if not exist packaging\vendor mkdir packaging\vendor

echo Building the application...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
python -m PyInstaller packaging\hivpipe.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo The build failed. The messages above say why.
    pause
    exit /b 1
)

echo.
echo Built: dist\HIV Pipeline Runner\HIV Pipeline Runner.exe
echo.

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" (
    echo Building the installer...
    "%ISCC%" packaging\installer.iss
    echo.
    echo Installer: Output\HIVPipeRunner-Setup.exe
    echo Run it to put a shortcut on your desktop.
) else (
    echo Inno Setup is not installed, so no installer was built.
    echo The program above still runs - double-click the .exe.
    echo To build an installer, get Inno Setup 6 from jrsoftware.org
    echo and run this script again.
)

echo.
pause
