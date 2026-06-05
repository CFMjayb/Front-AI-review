@echo off
setlocal
cd /d "%~dp0"

set PYTHON=.venv\Scripts\python.exe
set LEDGER_BACKEND=firestore
set GCP_PROJECT=cfm-front-mail

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
        pause
        goto END
    )
    echo.
)

echo  1. Export loops to Excel (for review)
echo  2. Import reviewed Excel (apply actions)
echo  3. Send briefing now
echo  4. Exit
echo.
set /p CHOICE="Select option: "

if "%CHOICE%"=="1" goto EXPORT
if "%CHOICE%"=="2" goto IMPORT
if "%CHOICE%"=="3" goto BRIEF
if "%CHOICE%"=="4" goto END
echo Invalid choice.
goto END

:EXPORT
echo.
echo Exporting active loops to Excel...
%PYTHON% cos_triage_export.py
echo.
echo Done. Open the file in data\triage\ to review.
echo Fill in the Action column, save, then run option 2 to apply changes.
goto END

:BRIEF
echo.
echo Generating and sending briefing now...
%PYTHON% -m cos.briefing
goto END

:IMPORT
echo.
set /p FILEPATH="Path to reviewed Excel file (or press Enter for latest in data\triage\): "
if "%FILEPATH%"=="" (
    %PYTHON% cos_triage_import.py
) else (
    %PYTHON% cos_triage_import.py "%FILEPATH%"
)
goto END

:END
echo.
pause
endlocal
