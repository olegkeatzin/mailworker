; Inno Setup 6: установщик службы MailWorker.
; Соберите dist\MailWorkerService\build.ps1, затем откройте этот файл в Inno Setup.

#define AppName "MailWorker"
#define AppVersion "1.0.0"
#define ServiceName "MailWorker"
#define UiPort "4000"

[Setup]
AppId={{8D2C7B3E-4A21-4C6B-9E0B-3B5F9D1A7C11}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=MailWorkerSetup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Files]
Source: "..\..\dist\MailWorkerService\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "config.windows.ini.example"; DestDir: "{commonappdata}\{#AppName}"; DestName: "config.ini"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "README-windows.md"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{commonappdata}\{#AppName}\data"; Permissions: users-modify
Name: "{commonappdata}\{#AppName}\logs"; Permissions: users-modify

[Run]
Filename: "{app}\MailWorkerService.exe"; Parameters: "--install-service --config ""{commonappdata}\{#AppName}\config.ini"""; Flags: runhidden waituntilterminated
Filename: "netsh.exe"; Parameters: "advfirewall firewall add rule name=""MailWorker UI"" dir=in action=allow protocol=TCP localport={#UiPort}"; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "{app}\MailWorkerService.exe"; Parameters: "--remove-service"; Flags: runhidden waituntilterminated
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""MailWorker UI"""; Flags: runhidden waituntilterminated

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
