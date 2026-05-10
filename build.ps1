# build.ps1 — Build a standalone Windows .exe with PyInstaller
# Run from the project root: powershell -ExecutionPolicy Bypass -File build.ps1

$ErrorActionPreference = "Stop"

Write-Host "Activating venv..." -ForegroundColor Cyan
& ".\venv\Scripts\Activate.ps1"

Write-Host "Installing PyInstaller..." -ForegroundColor Cyan
pip install pyinstaller --quiet

Write-Host "Building .exe..." -ForegroundColor Cyan
pyinstaller `
    --onedir `
    --windowed `
    --name "TranscribeApp" `
    --add-data "config.example.json;." `
    --add-data "history.py;." `
    --hidden-import "faster_whisper" `
    --hidden-import "ctranslate2" `
    --hidden-import "huggingface_hub" `
    --hidden-import "tokenizers" `
    --collect-all "faster_whisper" `
    --collect-all "ctranslate2" `
    --collect-all "pystray" `
    --collect-all "pynput" `
    --collect-all "pyaudio" `
    --exclude-module "torch" `
    --exclude-module "openai" `
    --exclude-module "numba" `
    --exclude-module "llvmlite" `
    --exclude-module "matplotlib" `
    --exclude-module "IPython" `
    main.py

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "Build complete! Find your exe at:" -ForegroundColor Green
    Write-Host "  dist\TranscribeApp\TranscribeApp.exe" -ForegroundColor Green
    Write-Host ""
    Write-Host "Note: The first run will download the Whisper model (~140 MB for 'base')." -ForegroundColor Yellow
    Write-Host "      Ship config.example.json alongside the exe (rename to config.json)." -ForegroundColor Yellow
} else {
    Write-Host "Build failed. Check errors above." -ForegroundColor Red
    exit 1
}
