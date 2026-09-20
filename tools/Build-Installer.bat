@echo off
setlocal
cd /d "%~dp0.."
where py >nul 2>nul && (py -3 tools\build_installer.py) || (python tools\build_installer.py)
echo.
pause
