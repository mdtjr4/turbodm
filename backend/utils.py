from __future__ import annotations

import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from backend.models import MediaCategory

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE_RUN = re.compile(r"\s+")

VIDEO_EXTS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".wmv", ".m4v",
    ".mpg", ".mpeg", ".3gp", ".ts", ".m2ts", ".ogv", ".vob",
}
AUDIO_EXTS = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma", ".alac",
}
COMPRESSED_EXTS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".iso", ".cab",
}
DOCUMENT_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt",
    ".rtf", ".csv", ".epub", ".md", ".odt",
}
PROGRAM_EXTS = {
    ".exe", ".msi", ".msix", ".bat", ".cmd", ".ps1", ".apk", ".dmg",
    ".deb", ".rpm", ".appimage", ".jar", ".whl",
}

CONTENT_TYPE_TO_EXT = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-flv": ".flv",
    "video/3gpp": ".3gp",
    "video/mp2t": ".ts",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/webm": ".weba",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/wav": ".wav",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-7z-compressed": ".7z",
    "application/x-rar-compressed": ".rar",
    "application/gzip": ".gz",
    "application/x-msdownload": ".exe",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def clean_url(raw_url: str) -> str:
    """Normalize and sanitize a user-pasted URL."""
    url = (raw_url or "").strip().strip('"').strip("'").strip("<>").strip()
    if not url:
        return ""
    # Handle URLs pasted without protocol like youtube.com/watch?... or www.site.com/file.mp4
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        if url.startswith("//"):
            url = "https:" + url
        else:
            url = "https://" + url
    return url


def is_valid_http_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def parse_content_disposition(header: Optional[str]) -> Optional[str]:
    """Extract filename from Content-Disposition header (supports RFC 5987 filename*=)."""
    if not header:
        return None

    # RFC 5987: filename*=UTF-8''encoded%20name.mp4
    match_ext = re.search(
        r"filename\*\s*=\s*(?:[A-Za-z0-9_-]+)?'(?:[^']*)'([^;\r\n]+)",
        header,
        flags=re.IGNORECASE,
    )
    if match_ext:
        raw = match_ext.group(1).strip().strip('"').strip("'")
        try:
            decoded = urllib.parse.unquote(raw)
            if decoded.strip():
                return decoded.strip()
        except Exception:
            pass

    # Standard: filename="example.mp4" or filename=example.mp4
    match_std = re.search(
        r'filename\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^;\r\n]+))',
        header,
        flags=re.IGNORECASE,
    )
    if match_std:
        val = (match_std.group(1) or match_std.group(2) or match_std.group(3) or "").strip()
        if val:
            try:
                return urllib.parse.unquote(val)
            except Exception:
                return val
    return None


def guess_ext_from_content_type(content_type: str) -> str:
    if not content_type:
        return ""
    mime = content_type.split(";")[0].strip().lower()
    return CONTENT_TYPE_TO_EXT.get(mime, "")


def extract_filename_from_url(url: str, content_type: str = "") -> str:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path or "")
    candidate = os.path.basename(path.rstrip("/"))
    if not candidate or candidate in (".", ".."):
        # Check query parameters like ?filename=... or ?file=...
        qs = urllib.parse.parse_qs(parsed.query)
        for key in ("filename", "file", "name", "download", "title"):
            if key in qs and qs[key]:
                candidate = qs[key][0]
                break
    if not candidate:
        host = parsed.netloc.split(":")[0].replace("www.", "") or "download"
        ext = guess_ext_from_content_type(content_type) or ".bin"
        candidate = f"{host}_{int(time.time())}{ext}"
    else:
        _, ext = os.path.splitext(candidate)
        if not ext or len(ext) > 8:
            guessed = guess_ext_from_content_type(content_type)
            if guessed:
                candidate = candidate + guessed
    return sanitize_filename(candidate)


def sanitize_filename(name: str, default_ext: str = "", max_len: int = 140) -> str:
    """Create a 100% Windows-safe filename while preserving extension and Unicode."""
    raw = (name or "").strip()
    raw = INVALID_FILENAME_CHARS.sub("_", raw)
    raw = WHITESPACE_RUN.sub(" ", raw).strip(" .")

    if not raw:
        raw = f"download_{int(time.time())}"

    stem, ext = os.path.splitext(raw)
    # Guard against weird non-extension dots (e.g. "My Video. Part 1")
    if ext and (len(ext) > 8 or " " in ext or not re.match(r"^\.[A-Za-z0-9]+$", ext)):
        stem = raw
        ext = ""

    if not ext and default_ext:
        ext = default_ext if default_ext.startswith(".") else f".{default_ext}"

    stem = stem.strip(" .")
    if not stem:
        stem = f"download_{int(time.time())}"

    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"

    allowed_stem_len = max(20, max_len - len(ext))
    if len(stem) > allowed_stem_len:
        stem = stem[:allowed_stem_len].rstrip(" .")

    return f"{stem}{ext}"


def get_unique_filepath(directory: str, filename: str) -> str:
    """Return a unique file path in directory, appending (1), (2), ... if needed."""
    os.makedirs(directory, exist_ok=True)
    safe_name = sanitize_filename(filename)
    stem, ext = os.path.splitext(safe_name)
    candidate = os.path.join(directory, safe_name)
    part_candidate = candidate + ".turbopart"

    counter = 1
    while os.path.exists(candidate) or os.path.exists(part_candidate):
        candidate = os.path.join(directory, f"{stem} ({counter}){ext}")
        part_candidate = candidate + ".turbopart"
        counter += 1
    return candidate


def detect_category(filename: str = "", content_type: str = "", is_video_site: bool = False, is_audio_only: bool = False) -> MediaCategory:
    if is_audio_only:
        return MediaCategory.AUDIO
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in VIDEO_EXTS:
        return MediaCategory.VIDEO
    if ext in AUDIO_EXTS:
        return MediaCategory.AUDIO
    if ext in COMPRESSED_EXTS:
        return MediaCategory.COMPRESSED
    if ext in DOCUMENT_EXTS:
        return MediaCategory.DOCUMENTS
    if ext in PROGRAM_EXTS:
        return MediaCategory.PROGRAMS

    mime = (content_type or "").split(";")[0].strip().lower()
    if mime.startswith("video/"):
        return MediaCategory.VIDEO
    if mime.startswith("audio/"):
        return MediaCategory.AUDIO
    if "zip" in mime or "rar" in mime or "7z" in mime or "tar" in mime or "gzip" in mime:
        return MediaCategory.COMPRESSED
    if "pdf" in mime or "document" in mime or "text/" in mime:
        return MediaCategory.DOCUMENTS
    if "msdownload" in mime or "executable" in mime:
        return MediaCategory.PROGRAMS

    if is_video_site:
        return MediaCategory.VIDEO
    return MediaCategory.OTHER


def safe_rename(src: str, dst: str, retries: int = 8, delay: float = 0.25) -> None:
    """Safely rename/replace a file on Windows, retrying brief AV/handle locks."""
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_err = exc
            time.sleep(delay * (attempt + 1))
        except OSError as exc:
            last_err = exc
            time.sleep(delay)
    if last_err:
        raise last_err


def safe_remove(path: str) -> None:
    if not path:
        return
    for attempt in range(4):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except PermissionError:
            time.sleep(0.15 * (attempt + 1))
        except OSError:
            return


def format_bytes(num: Optional[float]) -> str:
    if num is None or num < 0:
        return "Unknown"
    val = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{val:.2f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.2f} TB"


def resolve_category_dir(base_dir: str, category: MediaCategory, auto_categorize: bool) -> str:
    if not auto_categorize:
        os.makedirs(base_dir, exist_ok=True)
        return base_dir
    folder_map = {
        MediaCategory.VIDEO: "Videos",
        MediaCategory.AUDIO: "Audio",
        MediaCategory.COMPRESSED: "Compressed",
        MediaCategory.DOCUMENTS: "Documents",
        MediaCategory.PROGRAMS: "Programs",
        MediaCategory.OTHER: "Other",
    }
    target = os.path.join(base_dir, folder_map.get(category, "Other"))
    os.makedirs(target, exist_ok=True)
    return target
