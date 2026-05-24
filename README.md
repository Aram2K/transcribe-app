<div align="center">

<img src="assets/banner.svg" alt="Transcribe App" width="820"/>

<br/>

[![License: Non-Commercial](https://img.shields.io/badge/license-Non--Commercial-red?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-blue?style=flat-square)]()
[![Python](https://img.shields.io/badge/python-3.9%2B-green?style=flat-square)]()
[![Offline](https://img.shields.io/badge/works-offline-success?style=flat-square)]()
[![Armenian](https://img.shields.io/badge/language-Armenian%20%7C%20English%20%7C%20Russian-orange?style=flat-square)]()

<h3>Press a key. Speak. Your words appear — wherever your cursor is.</h3>

<p>A lightweight, always-on speech-to-text tool that lives in your system tray.<br/>
Works completely offline using Whisper AI, or connects to Google Cloud for best Armenian accuracy.</p>

**[⬇ Download for Windows (Installer)](https://github.com/Aram2K/transcribe-app/releases/latest/download/TranscribeApp-Windows-Setup.exe)** &nbsp;·&nbsp;
**[⬇ Download for macOS (.dmg)](https://github.com/Aram2K/transcribe-app/releases/latest/download/TranscribeApp-Mac.dmg)** &nbsp;·&nbsp;
[View all releases](https://github.com/Aram2K/transcribe-app/releases)

<sub>Prefer a portable build? Grab the [Windows .zip](https://github.com/Aram2K/transcribe-app/releases/latest/download/TranscribeApp-Windows.zip) instead.</sub>

</div>

---

## What is Transcribe?

Transcribe is a hotkey-triggered dictation tool that runs silently in the background on your computer. Press your configured key, speak naturally, and when you stop — the transcription is automatically pasted wherever your cursor is: a chat window, a document, an email, a code editor, anything.

It was built especially for **Armenian speakers** who need accurate, native-script transcription (not Latin transliteration), while also supporting English, Russian, French, German, Spanish, and Arabic.

---

## Features

| | |
|---|---|
| 🎙 **Hotkey recording** | Press any key combo (or mouse button) to start/stop |
| 📋 **Smart paste** | Auto-pastes at your cursor; falls back to clipboard |
| 🌐 **Armenian-first** | Optimized offline models with vibrant Armenian Flag Tricolor visual style |
| 🔒 **Fully offline** | Local Whisper AI and Qwen/Gemma action summaries offline |
| ☁️ **Google Cloud option** | Best accuracy for Armenian via Speech-to-Text API |
| ⚡ **Streaming results** | Background chunks transcribed while you speak |
| 📊 **Waveform overlay** | Premium floating visualizer overlay |
| 🕘 **History log** | Searchable list of all past transcriptions |
| ⚙️ **Settings panel** | Staged transactional settings (Save &amp; Cancel buttons) for full layout configuration |
| 📝 **Custom vocabulary** | Seed recognition with names or domain terms |
| 🪟 **System tray** | Zero footprint background daemon |

---

## Download & Install

### Windows

1. Download **[TranscribeApp-Windows-Setup.exe](https://github.com/Aram2K/transcribe-app/releases/latest/download/TranscribeApp-Windows-Setup.exe)**
2. Run it — the installer wizard handles everything (Start Menu shortcut, optional autostart, optional desktop icon)
3. Click **Finish** — Transcribe launches automatically and a microphone icon appears in your system tray
4. The first-run welcome window walks you through setting your hotkey, language, and backend
5. To uninstall later: **Settings → Apps → Transcribe → Uninstall**

> **Note:** Windows may show a "Windows protected your PC" warning the first time because the app isn't code-signed. Click **More info → Run anyway**. The app is open-source and safe.

**Updates** are automatic — when a new version is published, Transcribe shows a notification and adds an "Install update" item to the tray menu. One click downloads and installs the update without re-downloading the full app manually.

### macOS

1. Download **[TranscribeApp-Mac.dmg](https://github.com/Aram2K/transcribe-app/releases/latest/download/TranscribeApp-Mac.dmg)**
2. Open the `.dmg` and drag the app to your Applications folder
3. **On first launch**, macOS will block the app with *"Apple could not verify TranscribeApp is free of malware"*. This is because the app is not notarized (notarization requires a paid Apple Developer ID — the app is open-source and safe). Use **one** of these to bypass the warning:
   - **Easiest:** Open **System Settings → Privacy & Security**, scroll to the bottom and click **Open Anyway** next to the TranscribeApp message, then confirm.
   - **Or:** In Finder, right-click (or Control-click) **TranscribeApp.app → Open**, then click **Open** in the dialog.
   - **Or via Terminal:** `xattr -dr com.apple.quarantine /Applications/TranscribeApp.app` then launch normally.
4. Grant **Accessibility** permission when prompted (required for global hotkeys):
   `System Settings → Privacy & Security → Accessibility → add TranscribeApp`

---

## How to Use

```
1.  Press Alt+R  →  overlay appears, recording starts
2.  Speak        →  waveform pulses, partial results preview
3.  Press Enter  →  transcription is pasted at your cursor
    Press Esc    →  cancel without pasting
```

Right-click the **tray icon** for History, Settings, Record Meeting, or Quit.

---

## Record meetings 🎙

Capture a Google Meet, Zoom, or any conference call and automatically get back a transcript, a short summary, and a checkbox list of action items.

```
1.  Tray icon → Record Meeting…
2.  (Optional) Add the meeting title + attendee names — these anchor
    action-item ownership in the final notes
3.  Pick your audio source, click Start meeting
4.  A red REC banner with a pulsing dot, live timer, and audio-level
    meter makes it obvious the recording is on. The window pins itself
    on top and the tray icon turns red — you won't miss it.
5.  Type your own bullet points in the right-hand "Your notes" pane
    while you listen. They'll be merged into the AI summary.
6.  Click Stop & generate notes → wait 10-60s while the AI engine runs
7.  Read / Copy as Markdown / Copy as email / Copy as Slack / Save to
    file — everything is also stored on disk
```

The AI notes follow a research-backed structure (Microsoft Research, 2023): `## Summary` in third person, `## Key decisions`, `## Action items` as `- [ ] task (Owner: name)` checkboxes, `## Open questions`. When you provide an attendee list, owner attribution kicks in: phrases like *"I'll draft the spec"* and *"Aram should review it"* get attributed to the right person.

Long silence gaps (>1.4s) are tagged as `[speaker change]` in the transcript so the LLM can attribute who said what — a lightweight diarization signal that needs no extra ML dependency.

Each meeting is stored in `%APPDATA%\Transcribe\meetings\<timestamp>\` with:
- `chunks.jsonl` — every transcribed chunk as it arrived (crash-recoverable)
- `transcript.txt` — full transcript
- `notes.md` — Summary + Key decisions + Action items + Open questions
- `meta.json` — duration, chunk count, language

### Capturing the *other* participants' audio

By default the meeting recorder captures your microphone. To also capture what the other people in the meeting are saying, you need to pick a **system loopback input**:

- **Windows:** the app bundles `pyaudiowpatch`, so any **`[Loopback]` entry** in the device picker captures system audio directly. No setup needed.
- **macOS:** install [BlackHole](https://existential.audio/blackhole/) (free), route Google Meet / Zoom output through it, then select the BlackHole input in the meeting picker.
- **Linux:** use PulseAudio's monitor source (`pactl load-module module-loopback`) and select the monitor device.

### Notes quality

The summary and action items use whatever action engine you've configured in **Settings → Actions**:

| Engine | Notes quality | Cost |
|---|---|---|
| **Rule-based** (default) | Basic extractive summary + keyword-based action items | Free |
| **Local Qwen** (1.5B/3B/7B) | Good summarisation, no network required | Free (one-time download) |
| **OpenAI / Gemini / Anthropic** | Best quality | Pennies per meeting |

For 1-hour+ meetings, a cloud engine is recommended because local Qwen has a smaller context window.

---

## Backends

| Backend | Best for | Speed | Cost | Internet |
|---------|----------|-------|------|----------|
| **Local (Whisper AI)** | Privacy, offline use | 0.5–15s | Free forever | ❌ No |
| **Google Cloud** | Armenian accuracy | ~1s | 60 min/month free | ✅ Yes |

### Setting up Google Cloud (for best Armenian)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → enable **Cloud Speech-to-Text API**
3. Go to **Credentials** → Create API Key
4. Paste it in **Settings → Google API Key → Test Key** to verify
5. Switch backend to **Google Cloud** and set Language to **Auto-detect** or **Armenian**

---

## Local Models

| Model | Size | Speed | Quality | RAM needed |
|-------|------|-------|---------|------------|
| `tiny` | 75 MB | ~0.5s | Good | 2 GB |
| `base` | 140 MB | ~1s | Better | 4 GB |
| `small` | 460 MB | ~3s | Great | 6 GB |
| `medium` | 1.4 GB | ~8s | Excellent | 10 GB |
| `large-v3` | 3 GB | ~15s | Best | 16 GB |

Models are downloaded automatically on first use. The app grays out models your machine can't run.

---

## Settings

Open via **right-click tray icon → Settings**.

| Setting | Description |
|---------|-------------|
| **Backend** | Local (offline) or Google Cloud |
| **Google API Key** | Paste your key; click Test to verify |
| **Model** | Whisper model size (local only) |
| **Hotkey** | Click the badge, press any key combo or mouse button |
| **Language** | Auto-detect, Armenian, English, Russian, and more |
| **Accent color** | Blue, Green, Purple, Pink, Orange, White |
| **Custom vocabulary** | Words/names to help recognition (e.g. "Aram, Aibuben, AI") |

---

## Auto-start on Login

**Windows** — run once:
```powershell
powershell -ExecutionPolicy Bypass -File setup_autostart.ps1
```

**macOS** — add the app to `System Settings → General → Login Items`.

---

## For Developers — Run from Source

### Requirements
- Python 3.9+
- Windows 10/11 or macOS 12+
- A working microphone

### Windows
```bash
git clone https://github.com/Aram2K/transcribe-app.git
cd transcribe-app
powershell -ExecutionPolicy Bypass -File setup.ps1
# then:
run.bat
```

### macOS
```bash
git clone https://github.com/Aram2K/transcribe-app.git
cd transcribe-app
bash setup_mac.sh
# then:
bash run.sh
```

### Configuration
Copy `config.example.json` → `config.json` and edit. The file is git-ignored (it contains your API key).

---

## Building Distributables

### Windows `.exe`
```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
# Output: dist\TranscribeApp\TranscribeApp.exe  (folder, not single file)
```

### Automated CI (GitHub Actions)
Push a version tag to trigger automatic builds for both platforms:
```bash
git tag v1.0.0
git push --tags
```
GitHub Actions builds `TranscribeApp-Windows.zip` and `TranscribeApp-Mac.dmg` and creates a public release automatically.

---

## Roadmap

- [x] Local offline transcription (Whisper AI)
- [x] Google Cloud backend for best Armenian accuracy
- [x] Auto language detection (Armenian / English / Russian)
- [x] Smart paste at cursor
- [x] Streaming / chunked transcription
- [x] System tray, settings panel, history log
- [x] Custom vocabulary / prompt
- [x] macOS support
- [x] GitHub Actions CI — auto-builds .exe and .dmg on tag
- [ ] One-click `.dmg` installer (macOS)
- [ ] Configurable silence detection (auto-stop)
- [ ] Export history to CSV / TXT

---

## License

**Transcribe App** is free for personal and non-commercial use.

You **may**:
- Use it for personal dictation, study, or research
- Modify and share it (with attribution)

You **may not**:
- Sell it, sublicense it, or use it in a paid product or service
- Generate revenue from it in any way

See the full [LICENSE](LICENSE) for details. For commercial licensing inquiries: **aramatamian15@gmail.com**

---

## Author

Created by **Aram Adamyan**, Founder of [Aibuben.xyz](https://aibuben.xyz).

[Aibuben.xyz](https://aibuben.xyz) · [GitHub](https://github.com/Aram2K) · [Report an issue](https://github.com/Aram2K/transcribe-app/issues)

---

<div align="center">
<sub>Made with ❤️ for Armenian speakers and anyone who types too slowly.</sub>
</div>
