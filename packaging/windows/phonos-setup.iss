; Phonos Windows Installer (Inno Setup 6)
; Produces Phonos-Setup-x64.exe with Start Menu/Desktop shortcuts, optional
; first Production Admin bootstrap page, optional Private-LAN Firewall rule
; (5040-5041), and clean uninstaller.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{B4E82C91-9A41-4D7E-8C10-5F3B2E9A7D64}
AppName=Phonos
AppVersion={#AppVersion}
AppVerName=Phonos {#AppVersion}
AppPublisher=mxsp / FyreFly Systems GbR
AppPublisherURL=https://github.com/mxsp/phonos
AppSupportURL=https://github.com/mxsp/phonos/issues
AppUpdatesURL=https://github.com/mxsp/phonos/releases
DefaultDirName={autopf}\Phonos
DefaultGroupName=Phonos
DisableProgramGroupPage=yes
DisableDirPage=auto
AllowNoIcons=yes
LicenseFile=..\..\LICENSE
OutputDir=..\dist
OutputBaseFilename=Phonos-Setup-x64
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "lanfirewall"; Description: "Optional: Allow Phonos on private LAN in Windows Firewall (ports 5040-5041, requires Admin)"; GroupDescription: "Network"; Flags: unchecked
Name: "installdocker"; Description: "Optional: Install Docker Desktop in background via winget (only needed for isolated container notebook backends)"; GroupDescription: "Optional Components"; Flags: unchecked

[Files]
Source: "..\dist\phonos\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "install-deps.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\DISCLAIMER.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Phonos Production (:5041)"; Filename: "{app}\Phonos.exe"; Parameters: "--mode production"
Name: "{group}\Phonos Playground Sandbox (:5040)"; Filename: "{app}\Phonos.exe"; Parameters: "--mode playground"
Name: "{group}\{cm:UninstallProgram,Phonos}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Phonos"; Filename: "{app}\Phonos.exe"; Parameters: "--mode production"; Tasks: desktopicon

[Run]
Filename: "{app}\Phonos.exe"; Parameters: "--mode production --init-only --admin-user ""{code:GetAdminUser}"" --admin-password ""{code:GetAdminPassword}"""; Flags: runhidden waituntilterminated; StatusMsg: "Creating initial Production Admin account..."; Check: HasAdminCredentials
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""Phonos Localnet (5040-5041)"" dir=in action=allow protocol=TCP localport=5040-5041 profile=private"; Flags: runhidden waituntilterminated; Tasks: lanfirewall
Filename: "winget.exe"; Parameters: "install --id Docker.DockerDesktop -e --silent --accept-package-agreements --accept-source-agreements"; Flags: runhidden waituntilterminated skipifdoesntexist; Tasks: installdocker
Filename: "{app}\Phonos.exe"; Parameters: "--mode production"; Description: "Launch Phonos Lab Workbench now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""Phonos Localnet (5040-5041)"""; Flags: runhidden; RunOnceId: "RemovePhonosFirewallRule"

[Code]
var
  AdminPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  AdminPage := CreateInputQueryPage(
    wpSelectTasks,
    'Production Admin Account (Optional)',
    'Create your first local Administrator account',
    'Set an optional Username + Password for Production now, or leave the password blank to configure it in your browser at http://127.0.0.1:5041/setup.'
  );
  AdminPage.Add('Admin Username:', False);
  AdminPage.Add('Admin Password (min. 8 characters, leave blank to skip):', True);
  AdminPage.Values[0] := 'admin';
  AdminPage.Values[1] := '';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Pw: String;
begin
  Result := True;
  if CurPageID = AdminPage.ID then
  begin
    Pw := Trim(AdminPage.Values[1]);
    if (Pw <> '') and (Length(Pw) < 8) then
    begin
      MsgBox('Admin password must be at least 8 characters (or leave blank to skip).', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function HasAdminCredentials: Boolean;
begin
  Result := (Trim(AdminPage.Values[0]) <> '') and (Length(Trim(AdminPage.Values[1])) >= 8);
end;

function GetAdminUser(Param: String): String;
begin
  Result := Trim(AdminPage.Values[0]);
end;

function GetAdminPassword(Param: String): String;
begin
  Result := Trim(AdminPage.Values[1]);
end;
