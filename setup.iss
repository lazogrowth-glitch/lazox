; ============================================================
;  VocalType - Script Inno Setup
;  Telecharger Inno Setup (gratuit) : https://jrsoftware.org/isdl.php
;  Puis ouvrir ce fichier dans Inno Setup pour compiler l'installateur
; ============================================================

[Setup]
AppName=VocalType
AppVersion=1.1
AppPublisher=VocalType
AppPublisherURL=https://github.com
DefaultDirName={localappdata}\VocalType
DefaultGroupName=VocalType
UninstallDisplayIcon={app}\VocalType.exe
OutputDir=.
OutputBaseFilename=VocalType_Setup
Compression=lzma2
SolidCompression=yes
SetupIconFile=icon.ico
WizardStyle=modern
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=yes

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon";  Description: "Creer un raccourci sur le Bureau";            GroupDescription: "Raccourcis additionnels:"; Flags: unchecked
Name: "startupentry"; Description: "Lancer VocalType automatiquement au demarrage"; GroupDescription: "Demarrage:"; Flags: checkedonce

[Files]
Source: "dist\VocalType.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "icon.ico";           DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\VocalType";            Filename: "{app}\VocalType.exe"; IconFilename: "{app}\icon.ico"
Name: "{group}\Desinstaller VocalType"; Filename: "{uninstallexe}"
Name: "{userdesktop}\VocalType";      Filename: "{app}\VocalType.exe"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Registry]
; Demarrage automatique avec Windows (optionnel)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "VocalType"; \
  ValueData: """{app}\VocalType.exe"""; \
  Flags: uninsdeletevalue; Tasks: startupentry

[Run]
; Proposer de lancer VocalType apres installation
Filename: "{app}\VocalType.exe"; \
  Description: "Lancer VocalType maintenant"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Fermer VocalType avant desinstallation
Filename: "taskkill"; Parameters: "/F /IM VocalType.exe"; \
  Flags: runhidden; RunOnceId: "KillVocalType"
