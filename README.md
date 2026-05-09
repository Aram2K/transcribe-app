# Transcribe App

A lightweight offline speech-to-text tool for Windows and macOS. Press a hotkey anywhere on your computer, speak, and the transcription is pasted directly where your cursor is.

## Features

- **Local & offline** — uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper), no internet required
- **Google Cloud** option for best Armenian accuracy
- **Auto language detection** — detects Armenian, English, Russian and more
- **Smart paste** — pastes at cursor, falls back to clipboard
- **Streaming transcription** — background chunking so results feel instant
- **System tray** — lives in the background, zero UI until you need it
- **Settings panel** — hotkey, model, color, language, custom vocabulary, all configurable
- **History log** — scrollable list of all transcriptions with copy button
- **Custom vocabulary** — seed Whisper with names/terms for better accuracy

## Quick Start

### Requirements
- Python 3.9+
- Windows 10/11 or macOS 12+

### Install (Windows)

```bash
git clone https://github.com/Aram2k/transcribe-app.git
cd transcribe-app
powershell -ExecutionPolicy Bypass -File setup.ps1
```

### Install (macOS)

```bash
git clone https://github.com/Aram2k/transcribe-app.git
cd transcribe-app
bash setup_mac.sh
```

### Run

**Windows:** Double-click `run.bat` — a mic icon appears in your system tray.

**macOS:** `bash run.sh`

### Auto-start on boot (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File setup_autostart.ps1
```

### Build standalone .exe (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

The `.exe` will appear in `dist\TranscribeApp.exe`.

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

- [x] macOS support (`setup_mac.sh`, `run.sh`, cross-platform paste)
- [x] Windows installer build script (`build.ps1` → `dist/TranscribeApp.exe`)
- [x] Custom vocabulary / prompt support (Settings → Custom Vocabulary)
- [x] History log of transcriptions
- [ ] macOS `.dmg` installer / one-click installer
- [ ] macOS auto-start on login

## Contributing

PRs welcome. Open an issue first for large changes.
