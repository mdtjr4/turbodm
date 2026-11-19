from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
from typing import Any, Optional

import aiohttp
from bs4 import BeautifulSoup

from backend.config import DEFAULT_USER_AGENT, get_ffmpeg_path, get_js_runtimes
from backend.models import FormatOption, MediaCategory, ProbeResult
from backend.utils import (
    clean_url,
    detect_category,
    extract_filename_from_url,
    guess_ext_from_content_type,
    is_valid_http_url,
    parse_content_disposition,
    sanitize_filename,
)

# Domains that should bypass direct HTTP probing and go straight to yt-dlp
KNOWN_VIDEO_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
    "facebook.com",
    "fb.watch",
    "fb.com",
    "instagram.com",
    "tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "twitter.com",
    "x.com",
    "vimeo.com",
    "dailymotion.com",
    "dai.ly",
    "twitch.tv",
    "clips.twitch.tv",
    "reddit.com",
    "v.redd.it",
    "soundcloud.com",
    "bilibili.com",
    "b23.tv",
    "rumble.com",
    "streamable.com",
    "loom.com",
    "ted.com",
    "kick.com",
    "ok.ru",
    "vk.com",
    "vkvideo.ru",
    "pinterest.com",
    "pin.it",
    "linkedin.com",
    "odysee.com",
    "bitchute.com",
    "bandcamp.com",
    "mixcloud.com",
    "veoh.com",
    "metacafe.com",
    "archive.org",
    "drive.google.com",
)

STREAM_MANIFEST_EXTS = (".m3u8", ".mpd", ".f4m", ".ism")
STREAM_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "application/dash+xml",
)

MEDIA_URL_REGEX = re.compile(
    r"""(https?:\\?/\\?/[^\s"'<>\\)]+?\.(?:mp4|webm|m4v|mov|m3u8|mpd)(?:\?[^\s"'<>\\)]*)?)""",
    re.IGNORECASE,
)


def _is_known_video_site(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    for domain in KNOWN_VIDEO_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return True
    path_lower = (parsed.path or "").lower()
    if any(path_lower.endswith(ext) for ext in STREAM_MANIFEST_EXTS):
        return True
    return False


def _build_base_headers(referer: Optional[str] = None) -> dict[str, str]:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    if referer:
        headers["Referer"] = referer
    return headers


async def _probe_direct_http(url: str, extra_headers: Optional[dict[str, str]] = None) -> Optional[dict[str, Any]]:
    """
    Fast HTTP HEAD -> GET(Range: 0-0) probe.
    Returns dict with final_url, content_type, filesize, supports_range, filename, is_html, is_manifest.
    """
    headers = _build_base_headers()
    if extra_headers:
        headers.update(extra_headers)

    timeout = aiohttp.ClientTimeout(total=15, connect=8, sock_read=10)
    connector = aiohttp.TCPConnector(ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        # Attempt 1: GET with Range: bytes=0-0 (most reliable; works even when HEAD is blocked or omits Accept-Ranges)
        range_headers = {**headers, "Range": "bytes=0-0"}
        try:
            async with session.get(url, headers=range_headers, allow_redirects=True) as resp:
                status = resp.status
                final_url = str(resp.url)
                resp_headers = resp.headers
                content_type = resp_headers.get("Content-Type", "")
                cd = resp_headers.get("Content-Disposition", "")
                accept_ranges = resp_headers.get("Accept-Ranges", "").lower()
                content_range = resp_headers.get("Content-Range", "")
                content_length = resp_headers.get("Content-Length", "")

                # Parse filesize & range capability
                filesize: Optional[int] = None
                supports_range = False

                if status == 206:
                    supports_range = True
                    # Content-Range: bytes 0-0/123456
                    m = re.search(r"/(\d+)$", content_range)
                    if m:
                        filesize = int(m.group(1))
                elif status in (200, 201):
                    supports_range = "bytes" in accept_ranges
                    if content_length.isdigit():
                        filesize = int(content_length)
                elif status == 416:
                    # Range Not Satisfiable (e.g. empty file or server dislikes 0-0) -> fallback to HEAD
                    return await _probe_head_fallback(session, url, headers)
                elif status >= 400:
                    return None

                mime = content_type.split(";")[0].strip().lower()
                path_lower = urllib.parse.urlparse(final_url).path.lower()
                is_manifest = (
                    mime in STREAM_CONTENT_TYPES
                    or any(path_lower.endswith(ext) for ext in STREAM_MANIFEST_EXTS)
                )
                is_attachment = "attachment" in cd.lower()
                is_html = (
                    mime in ("text/html", "application/xhtml+xml")
                    and not is_attachment
                )

                cd_name = parse_content_disposition(cd)
                filename = (
                    sanitize_filename(cd_name, default_ext=guess_ext_from_content_type(content_type))
                    if cd_name
                    else extract_filename_from_url(final_url, content_type)
                )

                return {
                    "final_url": final_url,
                    "content_type": content_type,
                    "filesize": filesize,
                    "supports_range": supports_range,
                    "filename": filename,
                    "is_html": is_html,
                    "is_manifest": is_manifest,
                    "headers": headers,
                }
        except Exception:
            return await _probe_head_fallback(session, url, headers)


async def _probe_head_fallback(
    session: aiohttp.ClientSession, url: str, headers: dict[str, str]
) -> Optional[dict[str, Any]]:
    try:
        async with session.head(url, headers=headers, allow_redirects=True) as resp:
            if resp.status >= 400:
                return None
            final_url = str(resp.url)
            content_type = resp.headers.get("Content-Type", "")
            cd = resp.headers.get("Content-Disposition", "")
            cl = resp.headers.get("Content-Length", "")
            ar = resp.headers.get("Accept-Ranges", "").lower()
            filesize = int(cl) if cl.isdigit() else None
            mime = content_type.split(";")[0].strip().lower()
            path_lower = urllib.parse.urlparse(final_url).path.lower()
            is_manifest = (
                mime in STREAM_CONTENT_TYPES
                or any(path_lower.endswith(ext) for ext in STREAM_MANIFEST_EXTS)
            )
            is_html = mime in ("text/html", "application/xhtml+xml") and "attachment" not in cd.lower()
            cd_name = parse_content_disposition(cd)
            filename = (
                sanitize_filename(cd_name, default_ext=guess_ext_from_content_type(content_type))
                if cd_name
                else extract_filename_from_url(final_url, content_type)
            )
            return {
                "final_url": final_url,
                "content_type": content_type,
                "filesize": filesize,
                "supports_range": "bytes" in ar,
                "filename": filename,
                "is_html": is_html,
                "is_manifest": is_manifest,
                "headers": headers,
            }
    except Exception:
        return None


def _is_direct_http_format(fmt: dict[str, Any]) -> bool:
    """Check whether a yt-dlp format entry is a single direct HTTP/HTTPS URL (no HLS/DASH manifests)."""
    proto = (fmt.get("protocol") or "").lower()
    url = fmt.get("url") or ""
    if proto not in ("http", "https"):
        return False
    if not url.startswith(("http://", "https://")):
        return False
    if fmt.get("fragments") or fmt.get("manifest_url"):
        return False
    lower_url = url.lower()
    if ".m3u8" in lower_url or ".mpd" in lower_url:
        return False
    return True


def _estimate_format_size(fmt: dict[str, Any], duration: Optional[float]) -> Optional[int]:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size and isinstance(size, (int, float)) and size > 0:
        return int(size)
    tbr = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
    if tbr and duration and duration > 0:
        return int((float(tbr) * 1000.0 / 8.0) * float(duration))
    return None


def _quality_tag(height: int) -> str:
    if height >= 4320:
        return "8K UHD"
    if height >= 2160:
        return "4K UHD"
    if height >= 1440:
        return "2K QHD"
    if height >= 1080:
        return "Full HD"
    if height >= 720:
        return "HD"
    if height >= 480:
        return "SD"
    return "Low"


def _build_format_options(info: dict[str, Any]) -> list[FormatOption]:
    """Turn raw yt-dlp formats into clean, deduplicated, high-reliability quality choices."""
    raw_formats = info.get("formats") or []
    duration = info.get("duration")
    base_headers = dict(info.get("http_headers") or {})

    # Separate audio-only, video-only, and muxed (video+audio) streams
    audio_only: list[dict[str, Any]] = []
    video_streams: list[dict[str, Any]] = []

    for f in raw_formats:
        if not f or not f.get("url"):
            continue
        # Skip storyboards / mhtml / non-media formats
        ext = (f.get("ext") or "").lower()
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        if ext in ("mhtml", "jpg", "jpeg", "png", "webp", "vtt", "srt", "json"):
            continue
        if vcodec == "none" and acodec == "none":
            # Some generic/direct extractors don't populate vcodec/acodec -> treat as muxed video
            if ext in ("mp4", "mkv", "webm", "mov", "flv", "ts", "m4v"):
                video_streams.append(f)
            continue
        if vcodec == "none" and acodec != "none":
            audio_only.append(f)
        elif vcodec != "none":
            video_streams.append(f)

    # Pick best m4a/aac audio and best overall audio
    def _audio_rank(f: dict[str, Any]) -> tuple[int, int, float]:
        ext = (f.get("ext") or "").lower()
        acodec = (f.get("acodec") or "").lower()
        is_m4a = 1 if (ext in ("m4a", "mp4") or "mp4a" in acodec or "aac" in acodec) else 0
        is_direct = 1 if _is_direct_http_format(f) else 0
        abr = float(f.get("abr") or f.get("tbr") or 0)
        return (is_m4a, is_direct, abr)

    best_audio = max(audio_only, key=_audio_rank) if audio_only else None
    best_audio_size = _estimate_format_size(best_audio, duration) if best_audio else None

    # Group video streams by height
    by_height: dict[int, list[dict[str, Any]]] = {}
    for f in video_streams:
        h = int(f.get("height") or 0)
        by_height.setdefault(h, []).append(f)

    def _video_rank(f: dict[str, Any]) -> tuple[int, int, int, float, float]:
        ext = (f.get("ext") or "").lower()
        vcodec = (f.get("vcodec") or "").lower()
        has_audio = 1 if (f.get("acodec") not in (None, "none")) else 0
        # Prefer MP4/H.264 for compatibility at <=1080p, prefer direct https for speed
        is_mp4 = 1 if (ext == "mp4" or "avc" in vcodec or "h264" in vcodec) else 0
        is_direct = 1 if _is_direct_http_format(f) else 0
        fps = float(f.get("fps") or 0)
        tbr = float(f.get("vbr") or f.get("tbr") or 0)
        # If no separate audio exists, strongly prefer muxed streams
        mux_pref = has_audio if not best_audio else 0
        return (mux_pref, is_direct, is_mp4, fps, tbr)

    options: list[FormatOption] = []
    sorted_heights = sorted([h for h in by_height.keys() if h > 0], reverse=True)

    # Create Best Auto option first if we have formats
    if sorted_heights:
        top_h = sorted_heights[0]
        top_v = max(by_height[top_h], key=_video_rank)
        top_has_audio = top_v.get("acodec") not in (None, "none")
        v_size = _estimate_format_size(top_v, duration)
        total_est = v_size
        if not top_has_audio and v_size and best_audio_size:
            total_est = v_size + best_audio_size

        direct_v_url = top_v.get("url") if _is_direct_http_format(top_v) else None
        direct_a_url = (
            best_audio.get("url")
            if (not top_has_audio and best_audio and _is_direct_http_format(best_audio))
            else None
        )
        # Only use turbo split-mux if either muxed direct OR both video & audio are direct HTTP
        if not top_has_audio and not direct_a_url:
            direct_v_url = None

        fps_val = top_v.get("fps")
        fps_str = f" {int(round(fps_val))}fps" if fps_val and fps_val >= 48 else ""
        options.append(
            FormatOption(
                format_id="bestvideo+bestaudio/best",
                label=f"Best Quality ({top_h}p {_quality_tag(top_h)}{fps_str}) — MP4",
                ext="mp4",
                resolution=f"{top_h}p",
                height=top_h,
                fps=fps_val,
                filesize=total_est,
                vcodec=str(top_v.get("vcodec") or ""),
                acodec=str(top_v.get("acodec") or (best_audio.get("acodec") if best_audio else "")),
                is_recommended=True,
                protocol=str(top_v.get("protocol") or ""),
                direct_video_url=direct_v_url,
                direct_audio_url=direct_a_url,
                video_size=v_size,
                audio_size=best_audio_size if not top_has_audio else None,
                http_headers=dict(top_v.get("http_headers") or base_headers),
            )
        )

        # Deduplicated per-resolution options
        seen_heights: set[int] = set()
        for h in sorted_heights:
            # Bucket close heights (e.g. 1088 vs 1080)
            bucket = h
            for std in (4320, 2160, 1440, 1080, 720, 480, 360, 240, 144):
                if abs(h - std) <= 24:
                    bucket = std
                    break
            if bucket in seen_heights:
                continue
            seen_heights.add(bucket)

            best_v = max(by_height[h], key=_video_rank)
            has_audio = best_v.get("acodec") not in (None, "none")
            v_id = best_v.get("format_id")
            v_size = _estimate_format_size(best_v, duration)

            if has_audio:
                fmt_selector = f"{v_id}/best[height<={h}]/best"
                est_size = v_size
                d_v = best_v.get("url") if _is_direct_http_format(best_v) else None
                d_a = None
            elif best_audio:
                a_id = best_audio.get("format_id")
                fmt_selector = (
                    f"{v_id}+{a_id}/bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
                )
                est_size = (v_size + best_audio_size) if (v_size and best_audio_size) else v_size
                d_v = best_v.get("url") if _is_direct_http_format(best_v) else None
                d_a = best_audio.get("url") if _is_direct_http_format(best_audio) else None
                if not d_a:
                    d_v = None
            else:
                fmt_selector = f"{v_id}/bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
                est_size = v_size
                d_v = best_v.get("url") if _is_direct_http_format(best_v) else None
                d_a = None

            fps_val = best_v.get("fps")
            fps_str = f" {int(round(fps_val))}fps" if fps_val and fps_val >= 48 else ""
            options.append(
                FormatOption(
                    format_id=fmt_selector,
                    label=f"{bucket}p {_quality_tag(bucket)}{fps_str} (MP4)",
                    ext="mp4",
                    resolution=f"{bucket}p",
                    height=bucket,
                    fps=fps_val,
                    filesize=est_size,
                    vcodec=str(best_v.get("vcodec") or ""),
                    acodec=str(
                        best_v.get("acodec") if has_audio else (best_audio.get("acodec") if best_audio else "")
                    ),
                    protocol=str(best_v.get("protocol") or ""),
                    direct_video_url=d_v,
                    direct_audio_url=d_a,
                    video_size=v_size,
                    audio_size=best_audio_size if not has_audio else None,
                    http_headers=dict(best_v.get("http_headers") or base_headers),
                )
            )
    else:
        # Unknown resolution / single stream (e.g. generic video or single direct URL)
        direct_url = info.get("url")
        est_size = _estimate_format_size(info, duration)
        ext = info.get("ext") or "mp4"
        is_direct = _is_direct_http_format(info)
        options.append(
            FormatOption(
                format_id="best",
                label=f"Best Available ({ext.upper()})",
                ext=ext,
                resolution="Auto",
                filesize=est_size,
                is_recommended=True,
                protocol=str(info.get("protocol") or ""),
                direct_video_url=direct_url if is_direct else None,
                http_headers=base_headers,
            )
        )

    # Always provide clean Audio-Only presets
    options.append(
        FormatOption(
            format_id="bestaudio/best",
            label="Audio Only — High Quality MP3 (320 kbps)",
            ext="mp3",
            resolution="Audio",
            height=0,
            filesize=best_audio_size,
            acodec="mp3",
            is_audio_only=True,
            http_headers=base_headers,
        )
    )
    options.append(
        FormatOption(
            format_id="bestaudio[ext=m4a]/bestaudio/best",
            label="Audio Only — Original M4A",
            ext="m4a",
            resolution="Audio",
            height=0,
            filesize=best_audio_size,
            acodec="m4a",
            is_audio_only=True,
            direct_video_url=best_audio.get("url") if (best_audio and _is_direct_http_format(best_audio)) else None,
            http_headers=dict(best_audio.get("http_headers") or base_headers) if best_audio else base_headers,
        )
    )

    return options


class _SilentYtDlpLogger:
    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


def _run_ytdlp_extract_sync(url: str, cookies_file: str = "") -> dict[str, Any]:
    import yt_dlp

    ffmpeg_path = get_ffmpeg_path()
    js_runtimes = get_js_runtimes()

    # If URL has both a video ID and playlist param (like YouTube watch?v=X&list=Y),
    # probe single video fast unless it's explicitly a /playlist URL.
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    is_explicit_playlist = (
        "/playlist" in (parsed.path or "").lower()
        or ("list" in qs and "v" not in qs)
    )

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "logger": _SilentYtDlpLogger(),
        "skip_download": True,
        "noplaylist": not is_explicit_playlist,
        "extract_flat": "in_playlist" if is_explicit_playlist else False,
        "socket_timeout": 20,
        "extractor_retries": 3,
        "ignoreerrors": False,
        "nocheckcertificate": True,
        "remote_components": ["ejs:github"],
    }
    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = os.path.dirname(ffmpeg_path)
    if js_runtimes:
        ydl_opts["js_runtimes"] = js_runtimes
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise ValueError("No downloadable media found at this URL.")
            return ydl.sanitize_info(info)
    except Exception as exc:
        if "requested format is not available" in str(exc).lower():
            fallback_opts = dict(ydl_opts)
            fallback_opts["extractor_args"] = {"youtube": {"player_client": ["tv", "web", "mweb"]}}
            with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise ValueError("No downloadable media found at this URL.")
                return ydl.sanitize_info(info)
        raise


async def _sniff_html_media(url: str) -> Optional[ProbeResult]:
    """
    Fallback media sniffer for generic web pages that embed public HTML5 <video>,
    <source>, OpenGraph og:video, JSON-LD contentUrl, or direct .mp4/.webm/.m3u8 URLs.
    """
    headers = _build_base_headers()
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    timeout = aiohttp.ClientTimeout(total=15, connect=8, sock_read=10)
    connector = aiohttp.TCPConnector(ssl=False)

    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status >= 400:
                    return None
                final_url = str(resp.url)
                raw_bytes = await resp.content.read(2 * 1024 * 1024)
                html = raw_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return None

    soup = BeautifulSoup(html, "html.parser")
    page_title = (soup.title.string.strip() if soup.title and soup.title.string else "") or ""

    candidates: list[str] = []

    def add_candidate(raw: Optional[str]) -> None:
        if not raw:
            return
        cleaned = raw.strip().replace("\\/", "/")
        if cleaned.startswith("//"):
            cleaned = "https:" + cleaned
        elif cleaned.startswith("/") or not cleaned.startswith(("http://", "https://")):
            cleaned = urllib.parse.urljoin(final_url, cleaned)
        if is_valid_http_url(cleaned) and cleaned not in candidates:
            candidates.append(cleaned)

    # 1. <video> and <source> tags
    for tag in soup.find_all(["video", "source"]):
        add_candidate(tag.get("src"))
        add_candidate(tag.get("data-src"))

    # 2. OpenGraph / Twitter meta tags
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if prop in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
            add_candidate(meta.get("content"))

    # 3. JSON-LD VideoObject
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for obj in items:
                if isinstance(obj, dict):
                    add_candidate(obj.get("contentUrl"))
        except Exception:
            continue

    # 4. Regex scan for direct media URLs in page source
    for match in MEDIA_URL_REGEX.findall(html):
        add_candidate(match)

    if not candidates:
        return None

    # Sort direct mp4/webm ahead of m3u8/mpd
    def _cand_priority(u: str) -> int:
        lu = u.lower()
        if ".mp4" in lu:
            return 0
        if ".webm" in lu or ".mov" in lu or ".m4v" in lu:
            return 1
        if ".m3u8" in lu:
            return 2
        return 3

    candidates.sort(key=_cand_priority)
    ref_headers = _build_base_headers(referer=final_url)

    formats: list[FormatOption] = []
    primary_size: Optional[int] = None
    primary_range = False
    primary_url = candidates[0]

    for idx, cand in enumerate(candidates[:8]):
        path_lower = urllib.parse.urlparse(cand).path.lower()
        is_manifest = any(path_lower.endswith(ext) for ext in STREAM_MANIFEST_EXTS)
        if not is_manifest:
            probe = await _probe_direct_http(cand, extra_headers={"Referer": final_url})
            if not probe or probe["is_html"]:
                continue
            ext = os.path.splitext(probe["filename"])[1].lstrip(".") or "mp4"
            if idx == 0 or primary_size is None:
                primary_size = probe["filesize"]
                primary_range = probe["supports_range"]
                primary_url = probe["final_url"]
            formats.append(
                FormatOption(
                    format_id=f"sniff:{len(formats)}",
                    label=f"Detected Video #{len(formats) + 1} ({ext.upper()})",
                    ext=ext,
                    resolution="Direct",
                    filesize=probe["filesize"],
                    is_recommended=(len(formats) == 0),
                    protocol="https",
                    direct_video_url=probe["final_url"],
                    http_headers=ref_headers,
                )
            )
        else:
            formats.append(
                FormatOption(
                    format_id=f"stream:{cand}",
                    label=f"Detected Stream #{len(formats) + 1} (HLS/DASH -> MP4)",
                    ext="mp4",
                    resolution="Stream",
                    is_recommended=(len(formats) == 0),
                    protocol="m3u8",
                    direct_video_url=None,
                    http_headers=ref_headers,
                )
            )

    if not formats:
        return None

    title = page_title or os.path.splitext(extract_filename_from_url(primary_url))[0]
    ext = formats[0].ext or "mp4"
    filename = sanitize_filename(title, default_ext=f".{ext}")

    return ProbeResult(
        url=url,
        final_url=primary_url,
        kind="sniffed",
        title=title,
        filename=filename,
        category=MediaCategory.VIDEO,
        filesize=primary_size,
        supports_range=primary_range,
        content_type=f"video/{ext}",
        extractor="HTML5 Media Sniffer",
        formats=formats,
        http_headers=ref_headers,
    )


async def probe_url(raw_url: str, cookies_file: str = "") -> ProbeResult:
    """
    Universal URL analyzer:
    1. Direct file / direct media URL -> fast multi-connection HTTP metadata
    2. Video sites (1000+ via yt-dlp) / HLS / DASH -> rich video qualities + direct stream hints
    3. Generic webpage fallback -> HTML5 media sniffer
    """
    url = clean_url(raw_url)
    if not is_valid_http_url(url):
        raise ValueError("Please enter a valid HTTP or HTTPS URL.")

    # Step 1: If not a known video platform, probe direct HTTP first
    direct_info: Optional[dict[str, Any]] = None
    if not _is_known_video_site(url):
        direct_info = await _probe_direct_http(url)
        if direct_info and not direct_info["is_html"] and not direct_info["is_manifest"]:
            filename = direct_info["filename"]
            category = detect_category(filename, direct_info["content_type"])
            return ProbeResult(
                url=url,
                final_url=direct_info["final_url"],
                kind="direct",
                title=os.path.splitext(filename)[0],
                filename=filename,
                category=category,
                filesize=direct_info["filesize"],
                supports_range=direct_info["supports_range"],
                content_type=direct_info["content_type"],
                extractor="Direct HTTP",
                formats=[],
                http_headers=direct_info["headers"],
            )

    # Step 2: Run yt-dlp extraction in thread pool (use resolved redirect URL if available)
    ytdlp_target_url = direct_info["final_url"] if (direct_info and direct_info.get("final_url")) else url
    ytdlp_error: Optional[str] = None
    try:
        info = await asyncio.to_thread(_run_ytdlp_extract_sync, ytdlp_target_url, cookies_file)

        # Playlist handling
        if info.get("_type") == "playlist" and info.get("entries"):
            entries = []
            for entry in info.get("entries") or []:
                if not entry:
                    continue
                entry_url = entry.get("webpage_url") or entry.get("url")
                if not entry_url:
                    continue
                if not entry_url.startswith(("http://", "https://")) and entry.get("ie_key") == "Youtube":
                    entry_url = f"https://www.youtube.com/watch?v={entry_url}"
                entries.append(
                    {
                        "title": entry.get("title") or "Untitled Video",
                        "url": entry_url,
                        "duration": entry.get("duration"),
                        "thumbnail": entry.get("thumbnail"),
                    }
                )
            title = info.get("title") or "Playlist"
            return ProbeResult(
                url=url,
                final_url=info.get("webpage_url") or url,
                kind="playlist",
                title=title,
                filename=sanitize_filename(title),
                category=MediaCategory.VIDEO,
                thumbnail=info.get("thumbnail") or (entries[0]["thumbnail"] if entries else None),
                uploader=info.get("uploader") or info.get("channel"),
                extractor=info.get("extractor_key") or info.get("extractor") or "Playlist",
                playlist_count=len(entries),
                playlist_entries=entries[:200],
            )

        # Single video / media item
        title = info.get("title") or "video"
        formats = _build_format_options(info)
        recommended = formats[0] if formats else None
        ext = (recommended.ext if recommended else info.get("ext")) or "mp4"
        filename = sanitize_filename(title, default_ext=f".{ext}")

        return ProbeResult(
            url=url,
            final_url=info.get("webpage_url") or url,
            kind="video",
            title=title,
            filename=filename,
            category=detect_category(filename, is_video_site=True),
            filesize=recommended.filesize if recommended else _estimate_format_size(info, info.get("duration")),
            supports_range=True,
            thumbnail=info.get("thumbnail"),
            duration=info.get("duration"),
            uploader=info.get("uploader") or info.get("channel"),
            extractor=info.get("extractor_key") or info.get("extractor") or "Video",
            formats=formats,
            http_headers=dict(info.get("http_headers") or {}),
        )
    except Exception as exc:
        ytdlp_error = str(exc)

    # Check if yt-dlp explicitly reported DRM protection — never bypass DRM
    if ytdlp_error and ("drm" in ytdlp_error.lower() or "widevine" in ytdlp_error.lower()):
        raise ValueError(
            "This stream is DRM-protected and cannot be downloaded."
        )

    # Step 3: Fallback HTML5 Media Sniffer for generic web pages
    sniffed = await _sniff_html_media(url)
    if sniffed:
        return sniffed

    # Step 4: Last resort if initial direct probe succeeded even as HTML or generic content
    if direct_info:
        filename = direct_info["filename"]
        return ProbeResult(
            url=url,
            final_url=direct_info["final_url"],
            kind="direct",
            title=os.path.splitext(filename)[0],
            filename=filename,
            category=detect_category(filename, direct_info["content_type"]),
            filesize=direct_info["filesize"],
            supports_range=direct_info["supports_range"],
            content_type=direct_info["content_type"],
            extractor="Direct HTTP",
            http_headers=direct_info["headers"],
            warning="No embedded video stream was found; ready to download the URL directly.",
        )

    # Clean, user-friendly error message
    clean_err = re.sub(r"\x1b\[[0-9;]*m", "", ytdlp_error or "Could not analyze this URL.")
    clean_err = clean_err.replace("ERROR:", "").strip()
    raise ValueError(clean_err or "Could not find a downloadable file or video at this URL.")
