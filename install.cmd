@echo off
rem install.cmd - Set up Quest Hub on Windows (double-click, or run from a command prompt).
rem Passes its arguments to install.ps1, e.g.  install.cmd -Doctor
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo [install] Failed with exit code %RC%.
rem Keep the window open when started by double-click.
echo %CMDCMDLINE% | find /i "%~0" >/dev/null && pause
exit /b %RC%
