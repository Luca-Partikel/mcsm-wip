@echo off
setlocal EnableDelayedExpansion
title Minecraft Server Manager
cd /d "%~dp0"

rem ------------------------------------------------------------------
rem  Startet den Manager OHNE Konsolenfenster (pythonw.exe).
rem  Fehlt Python, wird es automatisch installiert (winget, sonst python.org).
rem  Zum Beenden gibt es in der Oberflaeche den Knopf "Manager beenden".
rem  Fehlersuche mit sichtbarer Ausgabe: Start-Debug.bat
rem ------------------------------------------------------------------

call :find_python
if defined PYW goto :run

rem Aus der Verknuepfung (Start.vbs) laeuft dieses Fenster unsichtbar. Die Python-Einrichtung
rem braucht aber ein sichtbares Fenster - also sichtbar neu starten und hier aussteigen.
if /i "%~1"=="/hidden" (
  start "Minecraft Server Manager" "%~f0"
  exit /b 0
)

echo.
echo  Python 3 ist noch nicht installiert - wird jetzt automatisch eingerichtet
echo  (einmalig, ca. 30 MB). Bitte kurz warten ...
echo.

where winget >nul 2>nul
if not errorlevel 1 (
  winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
  call :find_python
  if defined PYW goto :run
)

set "INST=%TEMP%\python-3.13.7-amd64.exe"
echo  Lade Installer von python.org ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.13.7/python-3.13.7-amd64.exe' -OutFile $env:INST"
if exist "%INST%" (
  "%INST%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0 SimpleInstall=1
  del "%INST%" >nul 2>nul
  call :find_python
  if defined PYW goto :run
)

echo.
echo  Python konnte nicht automatisch installiert werden.
echo  Bitte manuell installieren: https://www.python.org/downloads/windows/
echo  (Haken "Add python.exe to PATH" setzen) und Start.bat erneut ausfuehren.
start "" https://www.python.org/downloads/windows/
pause
exit /b 1

:run
start "" "%PYW%" app.py
exit /b 0

rem ------------------------------------------------------------------
rem  Sucht pythonw.exe. Der Store-Platzhalter in WindowsApps zaehlt nicht,
rem  weil er nur den Microsoft Store oeffnet.
rem ------------------------------------------------------------------
:find_python
set "PYW="
for /f "delims=" %%P in ('py -3 -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do set "PYW=%%P"
if defined PYW if exist "!PYW!" exit /b 0
set "PYW="
for /f "delims=" %%P in ('where python 2^>nul') do (
  if not defined PYW (
    echo %%P | find /i "WindowsApps" >nul
    if errorlevel 1 if exist "%%~dpPpythonw.exe" set "PYW=%%~dpPpythonw.exe"
  )
)
if defined PYW exit /b 0
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
  if not defined PYW if exist "%%~D\pythonw.exe" set "PYW=%%~D\pythonw.exe"
)
if defined PYW exit /b 0
exit /b 1
