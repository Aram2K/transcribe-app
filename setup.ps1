Write-Host "Setting up Transcribe App..." -ForegroundColor Green

if (-Not (Test-Path ".\venv")) { python -m venv venv }

& .\venv\Scripts\Activate.ps1

pip install -r requirements.txt

Write-Host "Done! Double-click run.bat to start." -ForegroundColor Green
