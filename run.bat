@echo off
rem Keep this file ASCII: cmd.exe misreads UTF-8 batch files on some Windows versions.
rem The Korean messages live in run-messages.txt and are printed with :say.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Double-clicking run.bat inside a ZIP extracts only this file to a temp folder.
if not exist "main.py" goto :notextracted

rem Speech-to-text (ctranslate2) only has Windows wheels for 64-bit x86 Python 3.10-3.14:
rem not for 3.15, 32-bit, ARM or free-threaded builds.
set "CHECK=import sys, sysconfig; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 14) and sysconfig.get_platform() == 'win-amd64' and not sysconfig.get_config_var('Py_GIL_DISABLED')))"
set "MODULES=import importlib.util, sys; sys.exit(any(importlib.util.find_spec(m) is None for m in ('imageio_ffmpeg', 'faster_whisper', 'ctranslate2', 'onnxruntime', 'numpy')))"
set "VPY=.venv\Scripts\python.exe"

rem Rebuild a virtual environment made with an unsupported Python, or copied from another PC.
if exist ".venv" "%VPY%" -c "%CHECK%" >nul 2>nul || (
    call :say rebuild
    rmdir /s /q ".venv"
    if exist ".venv" goto :inuse
)
if exist "%VPY%" goto :install

set "PY="
call :findpython
if not defined PY call :installpython
if not defined PY goto :nopython

call :say venv
%PY% -m venv .venv || goto :fail

:install
rem Install on the first run, after requirements.txt changes, and whenever a component is missing.
fc /b requirements.txt ".venv\installed.flag" >nul 2>nul && "%VPY%" -c "%MODULES%" >nul 2>nul && goto :start
call :say install
del /q install-log.txt 2>nul
"%VPY%" -m pip install --disable-pip-version-check --timeout 60 --log install-log.txt -r requirements.txt || goto :installfail
copy /y requirements.txt ".venv\installed.flag" >nul
del /q install-log.txt 2>nul

:start
start "" ".venv\Scripts\pythonw.exe" "%~dp0main.py"
exit /b 0

:findpython
rem Prefer the newest supported version, even if a newer unsupported one (3.15) is also installed.
for %%V in (3.14 3.13 3.12 3.11 3.10) do if not defined PY ( py -%%V -c "%CHECK%" <nul >nul 2>nul && set "PY=py -%%V" )
if not defined PY ( python -c "%CHECK%" <nul >nul 2>nul && set "PY=python" )
rem Where the Python 3.14 installer puts it. Right after installing, PATH is not updated in this window yet.
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python314\python.exe" "%ProgramFiles%\Python314\python.exe") do if not defined PY if exist %%P ( %%P -c "%CHECK%" >nul 2>nul && set "PY=%%P" )
exit /b 0

:installpython
where winget >nul 2>nul || exit /b 0
call :say installpython
winget install -e --id Python.Python.3.14 --architecture x64 --scope user --custom "PrependPath=1" --accept-package-agreements --accept-source-agreements
call :findpython
exit /b 0

:say
for /f "usebackq tokens=1* delims=|" %%a in ("%~dp0run-messages.txt") do if "%%a"=="%~1" echo(%%b
exit /b 0

:notextracted
rem run-messages.txt is not here either, so this one stays in English.
echo The program files are missing next to run.bat.
echo If you opened the ZIP file, extract it first: right-click the ZIP ^> "Extract All",
echo then run run.bat from the extracted folder.
pause
exit /b 1

:nopython
call :say nopython
pause
exit /b 1

:inuse
call :say inuse
pause
exit /b 1

:installfail
set "MSG=installfail"
findstr /c:"Long Path support" install-log.txt >nul 2>nul && set "MSG=longpath"
call :say %MSG%
pause
exit /b 1

:fail
call :say fail
if exist ".venv" rmdir /s /q ".venv"
pause
exit /b 1
