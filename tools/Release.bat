@echo off
setlocal
cd /d "%~dp0.."
if "%~1"=="" (
  echo Aufruf: Release.bat 1.6.0 [--push]
  pause
  exit /b 1
)
where py >nul 2>nul && (py -3 tools\release.py %*) || (python tools\release.py %*)
echo.
pause
