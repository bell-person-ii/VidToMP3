@echo off
setlocal
cd /d "%~dp0"

rem Double-clicking run.bat inside a ZIP extracts only this file to a temp folder.
if not exist "main.py" goto :notextracted

rem First run: create a private virtual environment and install dependencies (incl. ffmpeg).
if not exist ".venv\Scripts\pythonw.exe" (
    set "PY="
    rem Prefer a version that speech-to-text supports, even if a newer Python is also installed.
    for %%V in (3.14 3.13 3.12 3.11 3.10) do if not defined PY ( py -%%V -c "" >nul 2>nul && set "PY=py -%%V" )
    if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
    if not defined PY ( where python >nul 2>nul && set "PY=python" )
    if not defined PY goto :nopython

    echo [1/2] Creating virtual environment...
    call %%PY%% -m venv .venv || goto :fail
)

rem Install dependencies on the first run, and again whenever requirements.txt changes.
rem If only the speech-to-text part fails, keep going: audio conversion does not need it.
fc /b requirements.txt ".venv\installed.flag" >nul 2>nul || (
    echo [2/2] Installing dependencies ^(first run or after an update, takes a few minutes^)...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || (
        echo.
        echo Speech-to-text components could not be installed. Audio conversion still works.
        ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q imageio-ffmpeg || goto :fail
    )
    copy /y requirements.txt ".venv\installed.flag" >nul
)

start "" ".venv\Scripts\pythonw.exe" "%~dp0main.py"
exit /b 0

:notextracted
echo The program files are missing next to run.bat.
echo If you opened the ZIP file, extract it first: right-click the ZIP ^> "Extract All",
echo then run run.bat from the extracted folder.
pause
exit /b 1

:nopython
echo Python 3 is not installed.
echo Download it from https://www.python.org/downloads/ ^(check "Add python.exe to PATH"^).
pause
exit /b 1

:fail
echo.
echo Setup failed. Check your internet connection and try again.
if exist ".venv" rmdir /s /q ".venv"
pause
exit /b 1
