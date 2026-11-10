; Inno Setup 6 script for House Cinema. Built by packaging\build_windows.ps1, which passes:
;   /DAppVersion=0.1.0 /DSourceDir=<dist\HouseCinema> /DOutputDir=<dist> /DIconFile=<icon.ico>

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\HouseCinema"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef IconFile
  #define IconFile "..\..\build\icons\icon.ico"
#endif
#define AppName "House Cinema"
#define AppExe "HouseCinema.exe"
#define FirewallRule "House Cinema (app)"

[Setup]
; Keep AppId the same in every version: it is how an update finds the installed copy.
AppId={{40C67EF0-7E0E-4B85-9887-F5E250C42072}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Admin rights: the Windows Firewall rule (phones and TVs connect to this computer).
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=HouseCinema-{#AppVersion}-Windows-x64-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; A running copy (tray) holds its files: close it before updating / uninstalling.
CloseApplications=force
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#if FileExists(CompilerPath + "Languages\Vietnamese.isl")
Name: "vietnamese"; MessagesFile: "compiler:Languages\Vietnamese.isl"
#endif

[CustomMessages]
english.FirewallTask=Allow phones, computers and TVs on my home network to connect (Windows Firewall, private networks only)
english.FirewallGroup=Network:
#if FileExists(CompilerPath + "Languages\Vietnamese.isl")
vietnamese.FirewallTask=Cho phép điện thoại, máy tính và TV trong mạng gia đình kết nối (Tường lửa Windows, chỉ mạng riêng tư)
vietnamese.FirewallGroup=Mạng:
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "firewall"; Description: "{cm:FirewallTask}"; GroupDescription: "{cm:FirewallGroup}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; An update replaces the whole program folder (files left from an older version could be loaded).
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; \
    Flags: runhidden waituntilterminated; Tasks: firewall
Filename: "{sys}\netsh.exe"; \
    Parameters: "advfirewall firewall add rule name=""{#FirewallRule}"" dir=in action=allow program=""{app}\{#AppExe}"" enable=yes profile=private,domain"; \
    Flags: runhidden waituntilterminated; Tasks: firewall
; Start the app as the user who ran the installer (not elevated): its data is per user.
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; \
    Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; \
    Flags: runhidden waituntilterminated; RunOnceId: "RemoveFirewallRule"

; The library index, thumbnails and settings in %LOCALAPPDATA%\HouseCinema are kept on
; uninstall (a reinstall finds them again); delete that folder by hand to remove them.
