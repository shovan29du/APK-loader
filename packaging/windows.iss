; Inno Setup script. Build: iscc /DAppVersion=1.1.0 /DSourceDir=dist\pyi\APKLoader packaging\windows.iss
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
[Setup]
AppName=APK Loader
AppVersion={#AppVersion}
DefaultDirName={localappdata}\APKLoader-app
DefaultGroupName=APK Loader
PrivilegesRequired=lowest
OutputBaseFilename=APKLoader-Setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayName=APK Loader

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\APK Loader"; Filename: "{app}\APKLoader.exe"
; {userdesktop} follows OneDrive folder redirection; the second entry is the plain profile Desktop.
Name: "{userdesktop}\APK Loader"; Filename: "{app}\APKLoader.exe"
Name: "{%USERPROFILE}\Desktop\APK Loader"; Filename: "{app}\APKLoader.exe"; Check: PlainDesktopDiffers

[Run]
Filename: "{app}\APKLoader.exe"; Description: "Launch APK Loader"; Flags: nowait postinstall skipifsilent

[Code]
function PlainDesktopDiffers: Boolean;
var Plain: String;
begin
  Plain := ExpandConstant('{%USERPROFILE}\Desktop');
  Result := DirExists(Plain) and (CompareText(Plain, ExpandConstant('{userdesktop}')) <> 0);
end;
