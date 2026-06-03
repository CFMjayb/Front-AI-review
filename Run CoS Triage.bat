@echo off
setlocal
cd /d "%~dp0"

set PYTHON=.venv\Scripts\python.exe
set LEDGER_BACKEND=firestore
set GCP_PROJECT=cfm-front-mail

echo.
echo ==========================================
echo   Chief of Staff -- Triage Tool
echo ==========================================
echo.
echo  1. Export loops to Excel (for review)
echo  2. Import reviewed Excel (apply actions)
echo  3. Exit
echo.
set /p CHOICE="Select option: "

if "%CHOICE%"=="1" goto EXPORT
if "%CHOICE%"=="2" goto IMPORT
if "%CHOICE%"=="3" goto END
echo Invalid choice.
goto END

:EXPORT
echo.
echo Exporting active loops to Excel...
%PYTHON% cos_triage_export.py
echo.
echo Done. Open the file in data\triage\ to review.
echo Fill in the Action column, save, then run option 2 to apply changes.
pause
goto END

:IMPORT
echo.
set /p FILEPATH="Path to reviewed Excel file (or press Enter for latest in data\triage\): "
if "%FILEPATH%"=="" (
    %PYTHON% cos_triage_import.py
) else (
    %PYTHON% cos_triage_import.py "%FILEPATH%"
)
echo.
pause
goto END

:END
endlocal
