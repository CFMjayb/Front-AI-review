@echo off
:: ============================================================
:: Run CoS Triage.bat
:: Project: 26-119 Chief of Staff
::
:: Description:
::   Interactive triage tool for the Chief of Staff open-loop
::   tracker. Options: export active loops to Excel for review,
::   import reviewed Excel to apply actions, or send the daily
::   briefing on demand. Checks and refreshes GCP ADC credentials
::   before running.
::
::   Export writes ONE WORKBOOK PER MAILBOX (see cos/mailboxes.py).
::   Import with a blank path applies every workbook in the latest
::   batch, so all mailboxes are picked up in one pass.
::
:: Usage:
::   Run CoS Triage.bat [/unattended]
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

set PYTHON=.venv\Scripts\python.exe
set LEDGER_BACKEND=firestore
set GCP_PROJECT=cfm-front-mail
set PYTHONUTF8=1

set PATH=%LOCALAPPDATA%\Google\Cloud SDK\google-cloud-sdk\bin;%PATH%

echo.
echo ==========================================
echo   Chief of Staff -- Triage Tool
echo ==========================================
echo.

CALL gcloud auth application-default print-access-token >nul 2>&1
if errorlevel 1 (
    echo Google Cloud credentials expired -- a browser window will open.
    echo.
    CALL gcloud auth application-default login
    if errorlevel 1 (
        echo.
        echo ERROR: Re-authentication failed. Run "gcloud auth application-default login" and try again.
        set EXITCODE=1
        goto :log
    )
    echo.
)

if "%UNATTENDED%"=="1" (
    echo /unattended mode -- skipping interactive menu.
    set EXITCODE=1
    goto :log
)

echo  1. Export loops to Excel (one workbook per mailbox)
echo  2. Import reviewed Excel (apply actions)
echo  3. Send briefing now
echo  4. Exit
echo.
set /p CHOICE="Select option: "

if "%CHOICE%"=="1" goto EXPORT
if "%CHOICE%"=="2" goto IMPORT
if "%CHOICE%"=="3" goto BRIEF
if "%CHOICE%"=="4" goto :log
echo Invalid choice.
goto :log

:EXPORT
echo.
echo Exporting active loops to Excel (one workbook per mailbox)...
%PYTHON% cos_triage_export.py
set EXITCODE=%ERRORLEVEL%
echo.
echo Done. Open the files listed above (in data\triage\) to review.
echo Fill in the Action column in each, save, then run option 2 --
echo it applies every workbook in the latest batch, not just one.
goto :log

:BRIEF
echo.
echo Generating and sending briefing now...
%PYTHON% -m cos.briefing
set EXITCODE=%ERRORLEVEL%
goto :log

:IMPORT
echo.
echo Press Enter to apply ALL mailbox workbooks from the latest export batch,
echo or give one file's path to apply just that mailbox.
set /p FILEPATH="Path to reviewed Excel file (blank = latest batch): "
if "%FILEPATH%"=="" (
    %PYTHON% cos_triage_import.py
) else (
    %PYTHON% cos_triage_import.py "%FILEPATH%"
)
set EXITCODE=%ERRORLEVEL%
goto :log

:log
set "BAT_NAME=Run CoS Triage.bat"
set "PROJECT=26-119 Chief of Staff"
set "MODE=interactive"
if "%UNATTENDED%"=="1" set "MODE=unattended"
curl -s "https://qbo-mcp-server-xltaug3m6q-ue.a.run.app/bat-log?token=br8HFCPItvpItThdLesujiGhBS7mkzHtiqKVVU_vkoE&bat=%BAT_NAME%&project=%PROJECT%&machine=%COMPUTERNAME%&user=%USERNAME%&exit=%EXITCODE%&mode=%MODE%" >nul 2>&1

:end
if /i not "%1"=="/unattended" pause
endlocal
exit /b %EXITCODE%
