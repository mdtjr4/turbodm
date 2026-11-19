from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class DownloadStatus(str, Enum):
    QUEUED = "queued"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    MERGING = "merging"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


class DownloadMode(str, Enum):
    DIRECT = "direct"        # Multi-connection HTTP Range engine
    VIDEO_TURBO = "video_turbo"  # yt-dlp extracted direct streams -> Multi-connection engine + FFmpeg mux
    VIDEO_YTDLP = "video_ytdlp"  # yt-dlp concurrent fragment/native engine + FFmpeg


class MediaCategory(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    COMPRESSED = "compressed"
    DOCUMENTS = "documents"
    PROGRAMS = "programs"
    OTHER = "other"


class SegmentInfo(BaseModel):
    index: int
    start: int
    end: int
    downloaded: int = 0
    speed: float = 0.0
    status: str = "idle"  # idle, active, done, error, paused

    @property
    def total(self) -> int:
        if self.end < self.start:
            return 0
        return self.end - self.start + 1

    @property
    def progress(self) -> float:
        t = self.total
        if t <= 0:
            return 0.0
        return min(100.0, (self.downloaded / t) * 100.0)


class FormatOption(BaseModel):
    format_id: str
    label: str
    ext: str = "mp4"
    resolution: str = ""
    height: int = 0
    fps: Optional[float] = None
    filesize: Optional[int] = None
    vcodec: str = ""
    acodec: str = ""
    is_audio_only: bool = False
    is_recommended: bool = False
    protocol: str = ""
    direct_video_url: Optional[str] = None
    direct_audio_url: Optional[str] = None
    video_size: Optional[int] = None
    audio_size: Optional[int] = None
    http_headers: dict[str, str] = Field(default_factory=dict)


class ProbeResult(BaseModel):
    url: str
    final_url: str
    kind: str  # "direct", "video", "playlist", "sniffed"
    title: str
    filename: str
    category: MediaCategory = MediaCategory.OTHER
    filesize: Optional[int] = None
    supports_range: bool = False
    content_type: str = ""
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    uploader: Optional[str] = None
    extractor: Optional[str] = None
    formats: list[FormatOption] = Field(default_factory=list)
    playlist_count: int = 0
    playlist_entries: list[dict[str, Any]] = Field(default_factory=list)
    http_headers: dict[str, str] = Field(default_factory=dict)
    warning: Optional[str] = None


class DownloadItem(BaseModel):
    id: str
    url: str
    final_url: str = ""
    title: str
    filename: str
    save_dir: str
    filepath: str = ""
    category: MediaCategory = MediaCategory.OTHER
    mode: DownloadMode = DownloadMode.DIRECT
    status: DownloadStatus = DownloadStatus.QUEUED
    status_text: str = "Queued"
    error_message: str = ""

    total_bytes: int = 0
    downloaded_bytes: int = 0
    progress: float = 0.0
    speed: float = 0.0         # bytes/sec
    eta: Optional[float] = None  # seconds

    connections: int = 8
    active_connections: int = 0
    supports_resume: bool = False
    segments: list[SegmentInfo] = Field(default_factory=list)
    speed_history: list[float] = Field(default_factory=list)

    # Video-specific metadata
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    extractor: Optional[str] = None
    format_id: str = "best"
    format_label: str = ""
    is_audio_only: bool = False
    audio_format: str = "mp3"
    direct_video_url: Optional[str] = None
    direct_audio_url: Optional[str] = None
    http_headers: dict[str, str] = Field(default_factory=dict)

    created_at: float = 0.0
    completed_at: Optional[float] = None


class StartDownloadRequest(BaseModel):
    url: str
    save_dir: Optional[str] = None
    filename: Optional[str] = None
    connections: Optional[int] = None
    format_id: Optional[str] = None
    format_label: Optional[str] = None
    is_audio_only: bool = False
    audio_format: str = "mp3"
    kind: Optional[str] = None  # "direct" | "video"
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    extractor: Optional[str] = None
    filesize: Optional[int] = None
    supports_range: Optional[bool] = None
    direct_video_url: Optional[str] = None
    direct_audio_url: Optional[str] = None
    http_headers: dict[str, str] = Field(default_factory=dict)
    start_immediately: bool = True


class BatchDownloadRequest(BaseModel):
    urls: list[str]
    save_dir: Optional[str] = None
    connections: Optional[int] = None
    quality: str = "best"  # best, 1080, 720, 480, audio


class AppSettings(BaseModel):
    download_dir: str
    default_connections: int = 12
    max_concurrent_downloads: int = 3
    speed_limit_kbps: int = 0          # 0 = unlimited
    ytdlp_concurrent_fragments: int = 16
    preferred_video_quality: str = "best"
    cookies_file: str = ""
    auto_categorize_folders: bool = False
    enable_turbo_video_segments: bool = True
