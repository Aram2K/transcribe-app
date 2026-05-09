Write-Host "Setting up Transcribe App..." -ForegroundColor Green

if (-Not (Test-Path ".\venv")) { python -m venv venv }

& .\venv\Scripts\Activate.ps1

pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install openai-whisper pyaudio numpy keyboard pyperclip pynput pystray Pillow psutil

Write-Host "Done! Double-click run.bat to start." -ForegroundColor Green
