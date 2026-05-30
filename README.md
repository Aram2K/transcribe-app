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
| 🌐 **Armenian-first** | Optimized offline models with Armenian Flag Tricolor branding and native AIBUBEN Yerevan AI Community integration |
| 🔒 **Fully offline** | Local Whisper AI and Qwen/Gemma action summaries offline |
| ☁️ **Google Cloud option** | Best accuracy for Armenian via Speech-to-Text API |
| ⚡ **Streaming results** | Background chunks transcribed while you speak |
| 📊 **Waveform overlay** | Premium floating visualizer overlay |
| 🕘 **History log** | Searchable list of all past transcriptions |
| ⚙️ **Settings panel** | Staged transactional settings (Save &amp; Cancel) with dynamic provider model isolation and real-world 2026 token pricing display |
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
3. **First launch — Gatekeeper warning.** macOS will block the app with *"Apple could not verify TranscribeApp is free of malware"* because it isn't notarized (notarization needs a paid Apple Developer ID — the app is open-source and safe). Use **one** of these:
   - **Easiest:** Open **System Settings → Privacy & Security**, scroll to the bottom and click **Open Anyway** next to the TranscribeApp message.
   - **Or:** In Finder, right-click (or Control-click) **TranscribeApp.app → Open**, then click **Open** in the dialog.
   - **Or via Terminal:** `xattr -dr com.apple.quarantine /Applications/TranscribeApp.app` then launch normally.
4. **Where did the app go?** Transcribe has no Dock window — it runs as a **microphone icon in the menu bar** at the top-right of your screen, next to the wifi/battery icons. Click it any time to reach Settings, History, Record Meeting, and Quit.
5. **Grant Accessibility permission** — required for the global hotkey:
   - The first time you press your hotkey, macOS prompts you to allow Transcribe under **System Settings → Privacy & Security → Accessibility**. Toggle TranscribeApp **on**. Until you do, the hotkey silently won't fire.
6. **Hotkey:** the default is **Option + R** (`alt+r`). To change it: menu bar icon → Settings → Hotkey → click the field and press any combination of `⌘ ⌥ ⌃ ⇧` plus a key. It's read by physical key code, so any keyboard layout (French, German, Armenian, …) works.

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

By default, the meeting recorder is set to **Smart Meeting Mode (Record BOTH Computer Sound + My Microphone)**. This dynamically scans for the active default playback device (Speakers or Headphones) and automatically captures/mixes what the other people in the meeting are saying directly, with zero manual host API configuration needed!

- **Windows:** Completely automatic using the dynamic WASAPI loopback mixer. No virtual cables or complicated drivers needed.
- **macOS:** Install [BlackHole](https://existential.audio/blackhole/) (free) to enable native loopback routing.
- **Linux:** Select standard PulseAudio monitor inputs.

### Notes quality

The summary and action items use whatever action engine you've configured in **Settings → Actions**:

| Engine | Notes quality | Cost |
|---|---|---|
| **Rule-based** (default) | Basic extractive summary + keyword-based action items | Free |
| **Local Qwen** (1.5B/3B/7B) | Good summarisation, no network required | Free (one-time download) |
| **OpenAI / Gemini / Anthropic** | Best quality (Gemini 2.5 Flash, Claude 4.6/4.8, GPT-5.4/5.5 with integrated real-time pricing display) | Pennies per meeting |

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

Open via **right-click tray icon → Settings**. The settings window is cleanly split into task-oriented tabs:

### General Tab
*   **Dictation Hotkey:** Configure your custom keyboard shortcut or mouse button trigger.
*   **Default Spoken Language:** Select auto-detect or a specific language (Armenian, English, Russian, etc.).
*   **Custom Vocabulary:** Guide recognition by providing names or domain-specific terms.
*   **Meeting Recording Mode:** Toggle standard microphone-only recording or dynamic system loopback mixing.
*   **Privacy Mode:** Disable history and enforce local offline models.

### Models Tab
Unifies all transcription backends as first-class card choices in a single view:
*   **Local Models:** Whisper Tiny, Base, Small, Medium, Large-Turbo, and Large-v3.
*   **Mistral AI Models:** Voxtral Mini, Small, and Large cloud STT models.
*   **Google Cloud Models:** Enterprise Speech-to-Text API.
*   **Cloud API Credentials:** Sleek, integrated input frame at the bottom to configure Google and Mistral API keys.

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

Created by **[Aram Adamyan](https://www.linkedin.com/in/aram-adamyan-2k/)**, Founder of [Aibuben.xyz](https://aibuben.xyz).

[Aibuben.xyz](https://aibuben.xyz) · [LinkedIn](https://www.linkedin.com/in/aram-adamyan-2k/) · [GitHub](https://github.com/Aram2K) · [Report an issue](https://github.com/Aram2K/transcribe-app/issues)

---

<div align="center">
<sub>Made with ❤️ for Armenian speakers and anyone who types too slowly.</sub>
</div>
