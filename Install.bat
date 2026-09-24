@echo off
setlocal
title Minecraft Server Manager - Installation
chcp 65001 >nul

rem ------------------------------------------------------------------
rem  Installiert den Minecraft Server Manager fuer den aktuellen Benutzer:
rem    - Programm nach %LocalAppData%\Programs\MinecraftServerManager
rem    - Verknuepfungen auf Desktop und im Startmenue
rem    - Python wird beim ersten Start automatisch nachinstalliert
rem  Funktioniert sowohl aus dem Setup (ZIP liegt daneben) als auch aus
rem  einem entpackten Ordner heraus.
rem ------------------------------------------------------------------

set "TARGET=%LocalAppData%\Programs\MinecraftServerManager"
if defined MCSM_INSTALL_DIR set "TARGET=%MCSM_INSTALL_DIR%"
set "SRC=%~dp0"

echo.
echo  ==============================================================
echo   Minecraft Server Manager wird installiert
echo   Ziel: %TARGET%
echo  ==============================================================
echo.

if not exist "%TARGET%" mkdir "%TARGET%"

if exist "%SRC%MinecraftServerManager.zip" (
  echo  Dateien werden entpackt ...
  rem Pfade ueber die Umgebung uebergeben - ein Apostroph im Benutzernamen wie O'Brien wuerde
  rem einen PowerShell-String vorzeitig beenden.
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath ($env:SRC + 'MinecraftServerManager.zip') -DestinationPath $env:TARGET -Force"
) else (
  echo  Dateien werden kopiert ...
  robocopy "%SRC%." "%TARGET%" /E /NFL /NDL /NJH /NJS /NP /XD servers cache runtime data dist .git build plugin .github __pycache__ /XF Install.bat *.log *.part >nul
)

if not exist "%TARGET%\app.py" (
  echo.
  echo  Installation fehlgeschlagen: app.py wurde nicht gefunden.
  pause
  exit /b 1
)

rem Ein bereits laufender Manager wuerde nach dem Update mit dem alten Code weiterlaufen -
rem deshalb sauber beenden (Server werden gestoppt, Welten gespeichert) und unten neu starten.
if exist "%TARGET%\data\instance.json" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%TARGET%\tools\stop-instance.ps1" -Target "%TARGET%"
)

if not defined MCSM_NO_SHORTCUTS (
  echo  Verknuepfungen werden angelegt ...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%TARGET%\tools\shortcuts.ps1" -Target "%TARGET%"
)

echo.
echo  Fertig. Der Manager startet jetzt - beim ersten Mal wird Python
echo  automatisch eingerichtet, falls es fehlt (ca. 30 MB).
echo.
if defined MCSM_NO_START exit /b 0
call "%TARGET%\Start.bat"
endlocal
exit /b 0
