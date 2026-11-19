from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional

from backend.models import AppSettings

BASE_DIR = Path(__file__).resolve().parent.parent
CLOUD_MODE = os.environ.get("TURBODM_CLOUD_MODE", "").strip().lower() in ("1", "true", "yes")
DATA_DIR = Path(os.environ.get("TURBODM_DATA_DIR", str(BASE_DIR / "data")))
BIN_DIR = BASE_DIR / "bin"
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_FILE = DATA_DIR / "turbodm.db"

DEFAULT_DOWNLOAD_DIR = os.environ.get("TURBODM_DOWNLOAD_DIR") or (
    str(DATA_DIR / "downloads") if CLOUD_MODE else str(Path.home() / "Downloads" / "TurboDM")
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    Path(DEFAULT_DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)


def get_ffmpeg_path() -> Optional[str]:
    """
    Guarantees a working `ffmpeg.exe` in TurboDM/bin/ffmpeg.exe (from system PATH
    or imageio-ffmpeg bundled binary) and prepends TurboDM/bin to PATH so both
    yt-dlp and direct FFmpeg muxing always succeed without external setup.
    """
    ensure_directories()
    local_ffmpeg = BIN_DIR / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")

    if local_ffmpeg.exists() and local_ffmpeg.stat().st_size > 100_000:
        _prepend_bin_to_path()
        return str(local_ffmpeg)

    # 1. Check system PATH first
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg and os.path.exists(sys_ffmpeg):
        return sys_ffmpeg

    # 2. Provision bundled static binary from imageio-ffmpeg
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and os.path.exists(bundled):
            try:
                shutil.copy2(bundled, local_ffmpeg)
                _prepend_bin_to_path()
                return str(local_ffmpeg)
            except OSError:
                return bundled
    except Exception:
        pass

    return None


def _prepend_bin_to_path() -> None:
    bin_str = str(BIN_DIR)
    current = os.environ.get("PATH", "")
    if bin_str.lower() not in current.lower():
        os.environ["PATH"] = bin_str + os.pathsep + current


def get_node_path() -> Optional[str]:
    """Locate Node.js executable for yt-dlp JavaScript challenge solving."""
    node = shutil.which("node")
    if node and os.path.exists(node):
        return node
    common = [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
    ]
    for path in common:
        if os.path.exists(path):
            return path
    return None


class SettingsStore:
    def __init__(self) -> None:
        ensure_directories()
        self._settings = self._load()

    def _load(self) -> AppSettings:
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                settings = AppSettings(**{**self._defaults().model_dump(), **data})
                if CLOUD_MODE and (":\\" in settings.download_dir or not settings.download_dir.strip()):
                    settings.download_dir = DEFAULT_DOWNLOAD_DIR
                Path(settings.download_dir).mkdir(parents=True, exist_ok=True)
                return settings
            except Exception:
                pass
        settings = self._defaults()
        self._save(settings)
        return settings

    def _defaults(self) -> AppSettings:
        return AppSettings(
            download_dir=DEFAULT_DOWNLOAD_DIR,
            default_connections=12,
            max_concurrent_downloads=3,
            speed_limit_kbps=0,
            ytdlp_concurrent_fragments=16,
            preferred_video_quality="best",
            cookies_file="",
            auto_categorize_folders=False,
            enable_turbo_video_segments=True,
        )

    def _save(self, settings: AppSettings) -> None:
        ensure_directories()
        SETTINGS_FILE.write_text(
            json.dumps(settings.model_dump(), indent=2),
            encoding="utf-8",
        )

    def get(self) -> AppSettings:
        return self._settings

    def update(self, new_settings: AppSettings) -> AppSettings:
        # Clamp sane boundaries
        new_settings.default_connections = max(1, min(32, int(new_settings.default_connections)))
        new_settings.max_concurrent_downloads = max(1, min(10, int(new_settings.max_concurrent_downloads)))
        new_settings.speed_limit_kbps = max(0, int(new_settings.speed_limit_kbps))
        new_settings.ytdlp_concurrent_fragments = max(1, min(32, int(new_settings.ytdlp_concurrent_fragments)))
        if not new_settings.download_dir.strip():
            new_settings.download_dir = DEFAULT_DOWNLOAD_DIR
        Path(new_settings.download_dir).mkdir(parents=True, exist_ok=True)
        self._settings = new_settings
        self._save(new_settings)
        return self._settings
