# PDF Voice Reader

A portable Windows app that reads PDF files aloud in an **Indian-English female voice**. It highlights each word on the page as it is spoken.

It is a single file, `PDFVoiceReader.exe`, with nothing to install. Copy it to a pen drive, email it or share it on WhatsApp or Google Drive, and it runs on any Windows 10 or 11 PC.

## Features

| Feature | How |
|---|---|
| Indian-English female voice | **Neerja**, a natural Microsoft Neural voice and the default. Neerja Expressive and Prabhat (male) are also available. |
| Works offline too | Any voice installed in Windows can be used, e.g. **Microsoft Heera – English (India)**. |
| Play / Pause / Stop at any moment | Use the buttons or press **Space** / **Esc**. |
| Jump anywhere on the timeline | Click or drag the timeline. **Ctrl+G** jumps to a typed time such as `12:30`. |
| Start from any sentence | **Double-click** any text in the PDF, or click **Read this page**. |
| Live highlighter | The current sentence is shown in yellow and the current word in orange. The page turns and scrolls by itself. |
| Speed control | 0.5x to 2.5x with the slider, the **–/+** buttons or the **[** / **]** keys. **=** resets to normal speed. |
| Volume | Use the volume slider. |
| Remembers your place | When you reopen a PDF, it offers to continue from where you stopped. |
| Export audiobook | **File → Export audio** saves any page range as MP3 (online voices) or WAV (offline voices). |
| Navigation and zoom | Page arrows and a page box, PgUp/PgDn, zoom with **Ctrl + mouse wheel**, and *Fit width*. |
| Recent files, Open-with | **File → Recent files**. You can also drag a PDF onto the .exe. |

## Keyboard shortcuts

| Key | Action |
|---|---|
| Space | Play / Pause |
| Esc | Stop (go back to the start of the sentence) |
| ← / → | Previous / next sentence |
| Home | Restart from the beginning |
| Ctrl+G | Jump to a time |
| [ / ] / = | Slower / faster / normal speed |
| PgUp / PgDn | Previous / next page |
| Ctrl + / Ctrl – / Ctrl 0 | Zoom in / zoom out / fit width |
| Ctrl+O | Open a PDF |

## Voices: online vs offline

* **Neerja (Neural, online)** is the default and sounds the most natural. It needs an internet connection. The word highlighter is exact with this voice.
* **Windows voices (offline)** work without internet. To add the Indian female voice **Heera**, open
  *Settings → Time & Language → Speech → Add voices → English (India)* and restart the app. Heera will then appear in the **Voice** list.

## Getting the .exe

### Option A: download the ready-made build (recommended)
Each push builds the exe automatically with GitHub Actions on Windows:

1. Open the repository on GitHub and go to **Actions → Build PDF Voice Reader (Windows .exe)**.
2. Open the latest green run and download the **PDFVoiceReader-Windows** artifact. It is a zip that contains `PDFVoiceReader.exe`.

If you push a version tag such as `v1.0.0`, the exe is also attached to a **GitHub Release**, which gives you a permanent download link to share.

### Option B: build it yourself on a Windows PC
1. Install Python 3.10 or newer from python.org and tick **"Add python.exe to PATH"**.
2. Double-click **`build_exe.bat`** in this folder.
3. Your app will be at `dist\PDFVoiceReader.exe`.

### Run from source (any OS)
```
pip install -r requirements.txt
python pdf_voice_reader.py  [optional\file.pdf]
```

## Notes

* **Scanned PDFs** (photos of pages) have no text to read. Convert them with OCR first, for example with Adobe "Recognize Text" or an online OCR tool.
* The first time the .exe runs, Windows SmartScreen may say *"Windows protected your PC"* because the file is not code-signed. Click **More info → Run anyway**.
* Settings and reading positions are stored in `%APPDATA%\PDFVoiceReader\settings.json`.

## Files

| File | Purpose |
|---|---|
| `pdf_voice_reader.py` | The app: window, player, timeline and highlighter |
| `reader_core.py` | PDF text extraction, sentence splitting, word-timing alignment |
| `tts_engines.py` | Voices: Microsoft Neural (online) and Windows SAPI (offline) |
| `audio_player.py` | Audio playback with pause/resume |
| `make_icon.py` | Creates the app icon at build time |
| `build_exe.bat` | One-click Windows build |
