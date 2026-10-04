; ─── Transcribe — Inno Setup installer ──────────────────────────────────────
; Builds a single TranscribeApp-Setup.exe that installs the app, registers it
; in Add/Remove Programs, optionally creates a desktop shortcut + start-at-login entry, and
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
; Fresh installs only: after that the app's own Settings checkbox owns the
; setting, and an update's wizard must not silently re-tick or untick it.
Name: "startup";     Description: "Start {#MyAppName} automatically when Windows starts"; GroupDescription: "Startup options:"; Check: not IsUpgrade

[Files]
Source: "..\dist\TranscribeApp\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"; Parameters: "--show-settings"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; Parameters: "--show-settings"; Tasks: desktopicon

[Registry]
; Start at login: the same HKCU Run value the app manages from Settings
; (autostart.py), so Task Manager > Startup apps lists and toggles it. Only a
; fresh install writes it (the task exists only then) - so it also runs at the
; next login if the app isn't opened before. Per-user installs only: an
; all-users install may run elevated as ANOTHER account, whose HKCU this would
; be; there each user's first launch registers from {app}\autostart.default.
; The old Startup-folder shortcut (installers before 1.9.4) is migrated by the
; app, which first reads whether Task Manager had it switched off.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"" --background"; Tasks: startup; Check: not IsAdminInstallMode

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--show-settings"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{userappdata}\Transcribe\action_models"
; The optional NVIDIA GPU acceleration download (gpu_accel.py, ~736 MB) -
; kept in Local AppData so it never syncs with a roaming profile.
Type: filesandordirs; Name: "{localappdata}\Transcribe\gpu"
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
  UpgradeInstall: Boolean;

// An earlier version is installed in this install mode: this run updates it.
function IsUpgrade(): Boolean;
begin
  Result := UpgradeInstall;
end;

function FindPreviousInstall(): Boolean;
var
  Key, Uninst: string;
  RootKey: Integer;
begin
  Key := ExpandConstant('Software\Microsoft\Windows\CurrentVersion\Uninstall\{#emit SetupSetting("AppId")}_is1');
  // Only the hive of THIS install mode: another account's all-users install
  // doesn't make a per-user install an update (and vice versa).
  if IsAdminInstallMode then
    RootKey := HKLM
  else
    RootKey := HKCU;
  Uninst := '';
  RegQueryStringValue(RootKey, Key, 'UninstallString', Uninst);
  Result := Uninst <> '';
end;

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
  ConsentDir, Choice: string;
begin
  if (CurStep = ssPostInstall) and (not IsUpgrade) then
  begin
    // The start-at-login box, next to the exe (autostart.INSTALL_DEFAULT_NAME)
    // so every user of an all-users install gets it. The timestamp makes each
    // install's choice new to a config left by an earlier install; each user's
    // first launch adopts it once. Removed with {app} on uninstall.
    if WizardIsTaskSelected('startup') then
      Choice := 'on'
    else
      Choice := 'off';
    SaveStringToFile(ExpandConstant('{app}\autostart.default'),
      Choice + ' ' + GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':'), False);
    // Ticked on purpose: no stale Task Manager switch-off (e.g. from the
    // portable zip) may keep the fresh entry disabled.
    if (Choice = 'on') and (not IsAdminInstallMode) then
      RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run', '{#MyAppName}');
  end;
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

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  RunValue: string;
begin
  // The app may have written the Run value itself (Settings toggle), so
  // remove it whether or not the install task created it - but only when it
  // starts THIS install, not a portable copy or another install.
  // Known gap: on an all-users install each user's Run value lives in their
  // own HKCU; only the uninstalling account's is removed here. Others are left
  // pointing at the removed exe, which Windows simply skips at sign-in.
  if (CurUninstallStep = usUninstall) and
     RegQueryStringValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#MyAppName}', RunValue) and
     (Pos(Lowercase(ExpandConstant('{app}\{#MyAppExeName}')), Lowercase(RunValue)) > 0) then
  begin
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#MyAppName}');
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run', '{#MyAppName}');
  end;
end;

function InitializeSetup(): Boolean;
begin
  // Decided once, before anything is written (the uninstall key of THIS
  // install appears at the end of it).
  UpgradeInstall := FindPreviousInstall();
  Result := True;
end;
