@echo off
rem ScholarBot launcher for Windows - double-click it or run it from a terminal. No administrator rights needed.
rem Works with Python 3.8 and every newer version.
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
rem Persian/emoji text in log lines must never break the console on Windows code pages
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title ScholarBot

set "MIN_MAJOR=3"
set "MIN_MINOR=8"
set "PY="
set "PYVER="

echo.
echo === ScholarBot (Windows) ===
echo.
echo ==^> Looking for Python %MIN_MAJOR%.%MIN_MINOR% or newer

rem --- 1/2. find a usable Python (the py launcher first, then python / python3) ------------------------
call :try_python "py -3"
call :try_python "py"
call :try_python "python"
call :try_python "python3"
if not defined PY goto :no_python
echo Using Python %PYVER%  [%PY%]
goto :have_python

rem ----- subroutine: test one candidate command; sets PY and PYVER when it is Python 3.8+ -----------------
:try_python
if defined PY exit /b 0
set "CAND=%~1"
set "MAJ=0"
set "MIN=0"
set "PAT=0"
set "L="
%CAND% -c "import sys; print(sys.version_info[0], sys.version_info[1], sys.version_info[2])" > "%TEMP%\scholarbot_pyver.txt" 2>nul
if errorlevel 1 (
  echo   - %CAND%: not available
  exit /b 0
)
set /p L=<"%TEMP%\scholarbot_pyver.txt"
for /f "tokens=1,2,3" %%a in ("%L%") do (
  set "MAJ=%%a"
  set "MIN=%%b"
  set "PAT=%%c"
)
set "OKFLAG=0"
if %MAJ% GTR %MIN_MAJOR% set "OKFLAG=1"
if %MAJ% EQU %MIN_MAJOR% if %MIN% GEQ %MIN_MINOR% set "OKFLAG=1"
if "%OKFLAG%"=="1" (
  set "PY=%CAND%"
  set "PYVER=%MAJ%.%MIN%.%PAT%"
  exit /b 0
)
echo   - %CAND%: Python %MAJ%.%MIN% is too old
exit /b 0

:no_python
echo.
echo [ERROR] Python %MIN_MAJOR%.%MIN_MINOR% or newer was not found.
echo         Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this file again.
echo         If Python is already installed, open a new terminal and run:   py -0p     (lists installed versions)
echo         The Microsoft Store "python" shortcut does not count: install Python from python.org instead.
goto :fail

:have_python
rem --- 3. virtual environment (created only if missing) ---------------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo ==^> Creating virtual environment ^(.venv^)
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Could not create the virtual environment.
    goto :fail
  )
)
call ".venv\Scripts\activate.bat"

rem --- 4. pip ------------------------------------------------------------------------------------------
echo.
echo ==^> Updating pip
python -m pip install --quiet --disable-pip-version-check --upgrade pip
if errorlevel 1 echo [WARN] Could not update pip - continuing.

rem --- 5. .env from .env.example -----------------------------------------------------------------------
if not exist ".env" (
  echo.
  echo ==^> Creating .env from .env.example
  copy /y ".env.example" ".env" >nul
)

rem --- 6. dependencies from requirements.txt (+ local session key in .env) ------------------------------
echo.
echo ==^> Installing dependencies and checking .env
python tools\bootstrap.py
if errorlevel 1 goto :fail

rem --- 7. database migrations ------------------------------------------------------------------------------
echo.
echo ==^> Applying database migrations
python -m app.cli migrate
if errorlevel 1 goto :fail

rem --- 8. first administrator (asks interactively only when none exists yet) ------------------------------
echo.
echo ==^> Checking administrator account
python -m app.cli create-admin --if-none
if errorlevel 1 goto :fail

echo.
echo ==^> Configuration summary
python -m app.cli check
echo.
echo To enable the Bale bot, put its token in .env (BALE_BOT_TOKEN=...) or in Dashboard ^> Settings, then restart.

rem --- 9. run (the dashboard address is printed below; press Ctrl+C to stop) --------------------------------
echo.
echo ==^> Starting ScholarBot (press Ctrl+C to stop)
python run.py
if errorlevel 1 goto :fail
goto :done

:fail
echo.
echo ScholarBot could not start. Read the message above, or see "Troubleshooting" in README.md.
echo Common causes: no internet during the first run, the port is already in use ^(change DASHBOARD_PORT in .env^).
pause
exit /b 1

:done
endlocal
