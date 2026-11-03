@echo off
rem ACMS: double-click to open. The first time it makes .venv and installs ACMS there (a few minutes), installing
rem Python 3.12 for this user first (with winget) if no Python 3.10 or newer is found.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto check

call :find_python
if not defined PY call :install_python
if not defined PY (
    echo ACMS needs Python 3.10 or newer. If it was just installed, open launch.bat again;
    echo otherwise install it from https://www.python.org/downloads/
    pause
    exit /b 1
)
echo Setting up ACMS (first run)...
%PY% -m venv .venv || goto failed

:check
rem Installed, and here: a moved or freshly cloned folder installs again
".venv\Scripts\python.exe" -c "import traffic_simulator, pathlib, sys; sys.exit(not pathlib.Path(traffic_simulator.__file__).resolve().is_relative_to(pathlib.Path('src').resolve()))" >nul 2>&1 && goto run
echo Installing ACMS (downloads PySide6 the first time)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e . || goto failed

:run
start "" ".venv\Scripts\pythonw.exe" -m traffic_simulator.main %*
exit /b 0

:failed
echo Setup failed: see the messages above.
pause
exit /b 1

:find_python
set "PY="
for %%V in (3.13 3.12 3.11 3.10) do if not defined PY (py -%%V -c "" >nul 2>&1 && set "PY=py -%%V")
if not defined PY (python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=python")
exit /b 0

:install_python
rem winget comes with Windows 10 and 11. A per-user install needs no administrator; this console's PATH is not
rem refreshed, so the new Python is used from where a per-user install puts it.
where winget >nul 2>&1 || exit /b 0
echo Python 3.10 or newer was not found: installing Python 3.12 for this user...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
exit /b 0
