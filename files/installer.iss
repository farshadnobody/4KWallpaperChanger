; Inno Setup 6 script - installs per-user (no admin needed), adds an uninstaller.
#define AppName "4K Wallpaper Changer"
#define AppExe  "4KWallpaperChanger.exe"

[Setup]
AppId={{6B0C2F0E-4D7A-4C57-9E41-4A5B1C2D3E01}
AppName={#AppName}
AppVersion=2.0
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=4KWallpaperChanger_Setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked
Name: "autostart";   Description: "Start with &Windows (tray only)"

[Files]
Source: "dist\4KWallpaperChanger\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}";          Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}";    Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; same value name the app itself uses for its "Start with Windows" checkbox
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "4KWallpaperChanger"; ValueData: """{app}\{#AppExe}"" --hidden"; \
  Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
procedure KillApp();
var
  rc: Integer;
begin
  Exec('taskkill.exe', '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, rc);
end;

function InitializeSetup(): Boolean;
begin
  KillApp();                       // close a running copy before installing / updating
  Result := True;
end;

function InitializeUninstall(): Boolean;
begin
  KillApp();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data, Pics: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // make sure the autostart entry is gone even if the app changed it itself
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '4KWallpaperChanger');

    Data := ExpandConstant('{%USERPROFILE}\.4kwall');
    if DirExists(Data) then
      if MsgBox('Also delete settings, history and likes list?' + #13#10 + Data,
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Data, True, True, True);

    Pics := ExpandConstant('{%USERPROFILE}\Pictures\4KWallpaperChanger');
    if DirExists(Pics) then
      if MsgBox('Also delete the saved wallpaper images (recent + liked, including your own files)?' + #13#10 + Pics,
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Pics, True, True, True);
  end;
end;
