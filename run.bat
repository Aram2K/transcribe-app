@echo off
cd /d "%~dp0"

echo Activating virtual environment...
call venv\Scripts\activate.bat

echo Starting Transcribe App...
python main.py

echo.
echo App exited. Press any key to close.
pause
