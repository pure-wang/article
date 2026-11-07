@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem  Double-click to pull the latest daily reports into this Obsidian vault.
rem  All Chinese output lives in sync_reports.py (Python prints Unicode correctly).
rem  This wrapper stays pure ASCII: a .bat file is parsed with the OEM code page,
rem  so UTF-8 Chinese in here breaks the parser on another machine.
rem
rem  Python lookup: PATH, then common install locations, then DSH runtime.
rem ---------------------------------------------------------------------------

set "PY="

where python >nul 2>nul && set "PY=python"
if defined PY goto run

for %%P in (  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"  "C:\Program Files\Python312\python.exe"  "C:\Program Files\Python311\python.exe"
) do if exist "%%~P" set "PY=%%~P"

if not defined PY for /d %%D in ("%USERPROFILE%\.dsh\dsh-runtimes\*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"

if defined PY goto run

echo [ERROR] Python not found. Install Python 3.10+ and run this again.
goto end

:run
"%PY%" "%~dp0sync_reports.py" %*
if errorlevel 1 echo [ERROR] Sync did not complete - see the message above.

:end
echo.
pause