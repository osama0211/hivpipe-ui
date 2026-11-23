; Inno Setup script for the Windows installer.
;
; Build the application folder first (packaging\build.sh or the GitHub
; Actions workflow), then compile this with Inno Setup 6:
;     iscc packaging\installer.iss
;
; Produces a single HIVPipeRunner-Setup.exe. The user double-clicks it,
; clicks through, and gets a Start Menu entry. No Python, no pip, no WSL.

#define AppName "HIV Pipeline Runner"
#define AppVersion "1.1.0"
#define AppPublisher "HIV Pipeline Runner"
#define AppExe "HIV Pipeline Runner.exe"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\HIVPipeRunner
DefaultGroupName={#AppName}
OutputBaseFilename=HIVPipeRunner-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Per-user install by default, so no administrator password is needed.
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Checked by default: the desktop icon is how most people will start this.
Name: "desktopicon"; Description: "Create a desktop shortcut"; \
    GroupDescription: "Shortcuts"

[Files]
Source: "..\dist\HIV Pipeline Runner\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; \
    IconFilename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; \
    IconFilename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[Messages]
; Set expectations during install rather than after the first failed run.
WelcomeLabel2=This installs {#AppName} on your computer.%n%nIt includes everything needed to browse samples, manage references, check assembled consensus sequences, and send runs to an HPC cluster.%n%nRunning the assembly pipelines on this computer additionally needs Docker Desktop, because those pipelines are Linux programs. The application will tell you if it is missing.
