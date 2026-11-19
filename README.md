# TurboDM — Easy High-Speed Download Manager

TurboDM is a high-speed desktop download manager inspired by Internet Download Manager (IDM), now redesigned with a beginner-friendly interface and Light & Dark Mode support.

## Key Features
- **Beginner-Friendly UI + Light & Dark Mode**: Clean layout with a 3-step welcome guide, one-click theme toggle (saved automatically), collapsible live details panel, and a Quick Start Guide (`?`).
- **IDM-Style Multi-Segment Acceleration**: Splits direct HTTP/HTTPS downloads into up to 32 parallel byte-range connections with Dynamic Work-Stealing.
- **Universal Video Link Downloader**: Paste links from YouTube, Facebook, Instagram, TikTok, Vimeo, X/Twitter, Reddit, Dailymotion, Twitch, SoundCloud, HLS (`.m3u8`), DASH (`.mpd`), or embedded HTML5 video pages.
- **Turbo Video + FFmpeg Muxing**: Downloads HD/4K video and audio streams in parallel and merges them into `.mp4` (or extracts `.mp3` / `.m4a` audio).
- **True Pause & Resume**: Persists exact per-segment byte offsets in SQLite so paused or interrupted downloads resume cleanly.
- **Batch & Playlist Support**: Queue multiple URLs or entire video playlists in one click.

## How to Run on PC (Windows)
1. Make sure **Python 3.10+** is installed on your PC.
2. Extract the `TurboDM` folder.
3. Double-click **`Launch_TurboDM.bat`** (first launch automatically creates `.venv` and installs packages from `requirements.txt`).
   - For silent launch without a console window (after first setup), double-click **`Launch_TurboDM.vbs`**.
4. Or run manually in PowerShell / Command Prompt inside the `TurboDM` folder:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\pip install -r requirements.txt
   .\.venv\Scripts\python.exe app.py
   ```
The app opens automatically at `http://127.0.0.1:8765`.
