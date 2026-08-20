; Inno Setup script — OpenClaw Email Agent Windows installer (build spec §10).
;
; Flow:
;   1. PyInstaller folder-mode build produces dist\openclaw-email\ (see
;      packaging/pyinstaller.spec — NEVER onefile).
;   2. This script packages that folder into a single Setup.exe.
;   3. On install it lays the folder under {autopf}\OpenClaw Email Agent and
;      registers the WinSW service via the bundled WinSW-x64.exe.
;   4. On uninstall it stops + removes the service before deleting files.
;
; Code-sign the resulting Setup.exe (signtool) to reduce AV false positives
; (spec §10). The PyInstaller exe inside should be signed too.

#define MyAppName "OpenClaw Email Agent"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "OpenClaw"
#define MyAppExeName "openclaw-email.exe"
#define ServiceId "openclaw-email"

[Setup]
AppId={{B7E1F0A2-0C3D-4E5F-9A8B-OPENCLAWEMAIL}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputBaseFilename=openclaw-email-setup
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=admin          ; needed to register a Windows service
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern

[Files]
; The entire PyInstaller folder-mode output.
Source: "..\..\dist\openclaw-email\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs
; The WinSW service wrapper (MIT) — vendored per installer/winsw/README.md.
Source: "..\winsw\WinSW-x64.exe"; DestDir: "{app}"; DestName: "{#ServiceId}.exe"; Flags: ignoreversion

[Run]
; Generate/refresh the WinSW descriptor, then install + start the service.
; (winsw.py also writes this XML; here we install via the bundled exe directly.)
Filename: "{app}\{#ServiceId}.exe"; Parameters: "install"; Flags: runhidden waituntilterminated
Filename: "{app}\{#ServiceId}.exe"; Parameters: "start"; Flags: runhidden waituntilterminated

[UninstallRun]
; Stop and remove the service before files are deleted.
Filename: "{app}\{#ServiceId}.exe"; Parameters: "stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopSvc"
Filename: "{app}\{#ServiceId}.exe"; Parameters: "uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "UninstallSvc"

[Icons]
Name: "{group}\{#MyAppName} (config)"; Filename: "{app}\{#MyAppExeName}"; Parameters: "setup-wizard"
