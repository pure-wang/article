@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem  Scheduled-task entry point: syncs daily reports into this Obsidian vault.
rem  - no "pause"  (a scheduled task has no console; pause would hang forever)
rem  - no Chinese in the batch body (OEM code page would break the parser)
rem  - output goes to .sync.log next to this file (dot-prefix = hidden from Obsidian)
rem
rem  Python lookup, in order:
rem    1) python on PATH
rem    2) common install locations
rem    3) DSH managed runtime (if installed)
rem ---------------------------------------------------------------------------

set "LOG=%~dp0.sync.log"
set "PY="

where python >nul 2>nul && set "PY=python"
if defined PY goto run

rem -- common install locations --
for %%P in (  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"  "C:\Program Files\Python312\python.exe"  "C:\Program Files\Python311\python.exe"
) do if exist "%%~P" set "PY=%%~P"

rem -- DSH managed runtime --
if not defined PY for /d %%D in ("%USERPROFILE%\.dsh\dsh-runtimes\*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"

if defined PY goto run

echo [%DATE% %TIME%] ERROR - Python not found. >> "%LOG%"
exit /b 2

:run
rem Keep the log from growing forever (~10 years of runs, then cycle).
for %%A in ("%LOG%") do if %%~zA GTR 524288 del "%LOG%"

rem Only Python writes to the log (--stamp prints the run timestamp from Python,
rem keeping the log in a single consistent encoding).
echo. >> "%LOG%"
"%PY%" "%~dp0sync_reports.py" --stamp >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%