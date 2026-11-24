@echo off
rem questhub.cmd - Start the Quest Hub GUI with the project environment (double-clickable).
rem Set up first with install.cmd. Arguments are passed on, e.g.  questhub.cmd --no-browser
setlocal
set "ROOT=%~dp0"
set "ENV_DIR=%ROOT%.mamba-env"
if defined QH_ENV_DIR set "ENV_DIR=%QH_ENV_DIR%"
if not exist "%ENV_DIR%\python.exe" (
  echo [questhub] Environment not found. Run install.cmd first.
  pause
  exit /b 1
)
set "PATH=%ENV_DIR%\vendor\scrcpy;%ENV_DIR%;%ENV_DIR%\Library\bin;%ENV_DIR%\Scripts;%PATH%"
cd /d "%ROOT%"
"%ENV_DIR%\python.exe" -m questhub %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo [questhub] Exited with code %RC%.
  pause
)
exit /b %RC%
