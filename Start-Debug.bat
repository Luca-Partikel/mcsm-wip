@echo off
setlocal
title Minecraft Server Manager (Debug)
chcp 65001 >nul
cd /d "%~dp0"

rem Wie Start.bat, aber MIT sichtbarer Konsole - fuer die Fehlersuche.
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -u app.py
  goto :done
)
where python >nul 2>nul
if not errorlevel 1 (
  python -u app.py
  goto :done
)
echo Python 3 wurde nicht gefunden. Bitte zuerst Start.bat ausfuehren (installiert Python automatisch).
pause
exit /b 1

:done
echo.
echo Der Manager wurde beendet. Logdatei: data\manager.log
pause
endlocal
