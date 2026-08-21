@echo off
:: ============================================================
:: Run CoS Triage Workbook.bat
:: Project: 26-119 Chief of Staff
::
:: Description:
::   Builds the CoS Triage Workbook.xlsm by running
::   create_triage_workbook.py, which embeds VBA modules and
::   the API key fetched from GCP Secret Manager, then opens
::   the finished workbook in Excel.
::
:: Usage:
::   Run CoS Triage Workbook.bat [/unattended]
::
:: Options:
::   /unattended   Run without pausing at end (for schedulers)
:: ============================================================

setlocal

set "BAT_DIR=%~dp0"
cd /d "%BAT_DIR%"

set "UNATTENDED=0"
if /i "%1"=="/unattended" set "UNATTENDED=1"

set EXITCODE=0

set GCP_PROJECT=cfm-front-mail
set LEDGER_BACKEND=firestore
set FIRESTORE_PROJECT=cfm-qbo-mcp

powershell -NoProfile -Command "if (Test-Path 'CoS Triage Workbook.xlsm') { try { [IO.File]::Open('CoS Triage Workbook.xlsm','Open','ReadWrite','None').Close() } catch { exit 1 } }" >nul 2>&1
if errorlevel 1 (
    echo [WARNING] CoS Triage Workbook.xlsm is open -- close it first, then re-run.
    set EXITCODE=1
    goto :log
)

echo ============================================
echo   CoS Triage Workbook Builder (26-119)
echo ============================================
echo.
echo This will:
echo   1. Build CoS Triage Workbook.xlsm
echo   2. Embed VBA + API key from Secret Manager
echo   3. Open the workbook in Excel
echo.

if "%UNATTENDED%"=="0" (
    echo Close the workbook if it is already open, then press any key.
    pause >nul
)

echo.
echo Building workbook...
.venv\Scripts\python.exe create_triage_workbook.py
if errorlevel 1 (
    echo.
    echo ERROR: Build failed. See messages above.
    set EXITCODE=1
    goto :log
)

echo.
echo Opening workbook...
start "" "%BAT_DIR%CoS Triage Workbook.xlsm"

echo.
echo Done. Enable macros when prompted, then click Refresh Triage.

:log
set "BAT_NAME=Run CoS Triage Workbook.bat"
set "PROJECT=26-119 Chief of Staff"
set "MODE=interactive"
if "%UNATTENDED%"=="1" set "MODE=unattended"
curl -s "https://qbo-mcp-server-xltaug3m6q-ue.a.run.app/bat-log?token=br8HFCPItvpItThdLesujiGhBS7mkzHtiqKVVU_vkoE&bat=%BAT_NAME%&project=%PROJECT%&machine=%COMPUTERNAME%&user=%USERNAME%&exit=%EXITCODE%&mode=%MODE%" >nul 2>&1

:end
if /i not "%1"=="/unattended" pause
endlocal
exit /b %EXITCODE%
