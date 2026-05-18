; ─── Transcribe — Inno Setup installer ──────────────────────────────────────
; Builds a single TranscribeApp-Setup.exe that installs the app, registers it
; in Add/Remove Programs, optionally creates desktop + startup shortcuts, and
; closes any running instance during silent updates.

#define MyAppName "Transcribe"
#define MyAppPublisher "Aram Adamyan"
#define MyAppPublisherURL "https://aibuben.xyz"
#define MyAppURL "https://github.com/Aram2K/transcribe-app"
#define MyAppExeName "TranscribeApp.exe"

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{8E3B7C8A-9D54-4F61-9F6C-2E8C7F0A1B23}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppPublisherURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename=TranscribeApp-Setup
OutputDir=.
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
CloseApplications=force
RestartApplications=yes
; Don't ask the user to choose components/etc — make the install fast.
DisableReadyPage=yes
DisableFinishedPage=no
ShowLanguageDialog=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "startup";     Description: "Start {#MyAppName} automatically when Windows starts"; GroupDescription: "Startup options:"

[Files]
Source: "..\dist\TranscribeApp\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"; Parameters: "--show-settings"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; Parameters: "--show-settings"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; Parameters: "--background"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--show-settings"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-tiny"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-base"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-small"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-medium"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-large-v3-turbo"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-large-v3"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
