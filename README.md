# Transcribe App

A lightweight offline speech-to-text tool for Windows. Press a hotkey anywhere on your computer, speak, and the transcription is pasted directly where your cursor is.

## Features

- **Local & offline** — uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper), no internet required
- **Google Cloud** option for best Armenian accuracy
- **Auto language detection** — detects Armenian, English, Russian and more
- **Smart paste** — pastes at cursor, falls back to clipboard
- **Streaming transcription** — background chunking so results feel instant
- **System tray** — lives in the background, zero UI until you need it
- **Settings panel** — hotkey, model, color, language, all configurable

## Quick Start

### Requirements
- Python 3.9+
- Windows 10/11 (Mac support coming)

### Install

```bash
git clone https://github.com/YOUR_USERNAME/transcribe-app.git
cd transcribe-app
powershell -ExecutionPolicy Bypass -File setup.ps1
```

### Run

Double-click `run.bat` — a mic icon appears in your system tray.

### Auto-start on boot

```powershell
powershell -ExecutionPolicy Bypass -File setup_autostart.ps1
```

## Usage

| Action | Result |
|--------|--------|
| `Alt+R` | Start recording |
| `Enter` or `Alt+R` | Stop & transcribe |
| `Esc` | Cancel recording |

Right-click the tray icon → **Settings** to change hotkey, model, language, color.

## Backends

| Backend | Language | Speed | Cost | Internet |
|---------|----------|-------|------|----------|
| Local (faster-whisper) | Auto-detect | ~1–3s | Free | No |
| Google Cloud | Best Armenian | ~1s | 60 min/mo free | Yes |

### Google Cloud Setup
1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Enable **Cloud Speech-to-Text API**
3. Create an API key under **Credentials**
4. Paste it in Settings → Google API Key

## Models (local)

| Model | Size | Speed | Quality |
|-------|------|-------|---------|
| tiny | 75 MB | ~0.5s | Good |
| base | 140 MB | ~1s | Better |
| small | 460 MB | ~3s | Great |
| medium | 1.4 GB | ~8s | Excellent |
| large-v3 | 3 GB | ~15s | Best |

## Config

Copy `config.example.json` → `config.json` and edit. Never commit `config.json` (it contains your API key).

## Roadmap

- [ ] macOS support
- [ ] One-click installer (.exe for Windows, .dmg for Mac)
- [ ] Custom vocabulary / prompt support
- [ ] History log of transcriptions

## Contributing

PRs welcome. Open an issue first for large changes.
