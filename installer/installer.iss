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
Type: filesandordirs; Name: "{userappdata}\Transcribe\action_models"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-tiny"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-base"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-small"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-medium"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-large-v3-turbo"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--Systran--faster-whisper-large-v3"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface\hub\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"

[Code]
var
  AnalyticsPage: TInputOptionWizardPage;

procedure InitializeWizard;
begin
  AnalyticsPage := CreateInputOptionPage(
    wpSelectTasks,
    'Privacy',
    'Anonymous usage analytics',
    'Help improve Transcribe by sharing safe usage events. Analytics is optional. It never includes audio, transcription text, clipboard content, API keys, file paths, microphone names, or window titles.',
    False,
    False
  );
  AnalyticsPage.Add('Share anonymous usage analytics (recommended)');
  AnalyticsPage.Values[0] := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ConsentDir: string;
begin
  if (CurStep = ssPostInstall) and (not WizardSilent) then
  begin
    ConsentDir := ExpandConstant('{userappdata}\Transcribe');
    ForceDirectories(ConsentDir);
    // Always write the marker once the wizard has been seen; the app uses
    // it only to set analytics_consent_applied. The user's actual choice
    // is reflected by the default analytics_enabled value and the in-app
    // Settings toggle.
    SaveStringToFile(ConsentDir + '\analytics_consent.accepted', 'accepted', False);
    if not AnalyticsPage.Values[0] then
    begin
      // User opted out during install: write a declined marker so the
      // app can honour it on first launch.
      SaveStringToFile(ConsentDir + '\analytics_consent.declined', 'declined', False);
    end;
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
end;
