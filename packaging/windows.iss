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

[Registry]
Root: HKCU; Subkey: "Software\Classes\APKLoader.AndroidPackage"; ValueType: string; ValueData: "Android package (APK Loader)"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\APKLoader.AndroidPackage\shell\open"; ValueType: string; ValueData: "Install with APK Loader"
Root: HKCU; Subkey: "Software\Classes\APKLoader.AndroidPackage\shell\open\command"; ValueType: string; ValueData: """{app}\APKLoader.exe"" --install ""%1"""
Root: HKCU; Subkey: "Software\Classes\.apk\OpenWithProgids"; ValueType: none; ValueName: "APKLoader.AndroidPackage"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.xapk\OpenWithProgids"; ValueType: none; ValueName: "APKLoader.AndroidPackage"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.apks\OpenWithProgids"; ValueType: none; ValueName: "APKLoader.AndroidPackage"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.aab\OpenWithProgids"; ValueType: none; ValueName: "APKLoader.AndroidPackage"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\APKLoader.exe"; Description: "Launch APK Loader"; Flags: nowait postinstall skipifsilent

[Code]
function PlainDesktopDiffers: Boolean;
var Plain: String;
begin
  Plain := ExpandConstant('{%USERPROFILE}\Desktop');
  Result := DirExists(Plain) and (CompareText(Plain, ExpandConstant('{userdesktop}')) <> 0);
end;
