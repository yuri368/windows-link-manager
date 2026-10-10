; Compile with Inno Setup 6 on Windows after scripts/build_windows.py:
; ISCC /DAppVersion=<version> /DBundleDir=<absolute onedir> /DOutputDir=<releases> this-file.iss
#ifndef AppVersion
  #error AppVersion must match linkmanager.__version__
#endif
#ifndef BundleDir
  #error BundleDir must point to the built WindowsLinkManager directory
#endif
#ifndef OutputDir
  #error OutputDir must point to the release output directory
#endif
#if !FileExists(BundleDir + "\WindowsLinkManager.exe")
  #error BundleDir must contain the built GUI executable
#endif
#if !FileExists(BundleDir + "\THIRD_PARTY_NOTICES.md")
  #error BundleDir must include the distribution license notices
#endif

[Setup]
AppId={{E72EF5CA-CDB8-43BE-9CC7-9E4D3F1C81FD}
AppName=Windows Link Manager
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Windows Link Manager
DefaultGroupName=Windows Link Manager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=windows-link-manager-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\WindowsLinkManager.exe
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Excludes: ".state\*,软链接.txt,*.log"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Windows Link Manager"; Filename: "{app}\WindowsLinkManager.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Windows Link Manager"; Filename: "{app}\WindowsLinkManager.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\WindowsLinkManager.exe"; Description: "Open Windows Link Manager"; Flags: nowait postinstall skipifsilent
