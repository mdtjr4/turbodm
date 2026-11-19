from __future__ import annotations

import asyncio
import glob
import os
import re
import subprocess
import time
from typing import Any, Callable, Optional

from backend.config import get_ffmpeg_path, get_node_path
from backend.direct_engine import (
    DirectEngineError,
    DownloadCancelledError,
    DownloadPausedError,
    ProgressCallback,
    run_direct_download,
)
from backend.models import DownloadItem, SegmentInfo
from backend.utils import safe_remove, safe_rename, sanitize_filename

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _YtDlpPaused(Exception):
    pass


class _YtDlpCancelled(Exception):
    pass


class _SilentYtDlpLogger:
    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


async def _mux_video_audio_ffmpeg(
    video_path: str, audio_path: str, output_path: str
) -> None:
    """Losslessly mux separate video + audio streams into MP4 via bundled FFmpeg."""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to merge video and audio streams.")

    tmp_out = output_path + ".muxtmp.mp4"
    safe_remove(tmp_out)

    # Pass 1: Fast lossless stream copy
    cmd_copy = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", video_path,
        "-i", audio_path,
        "-c", "copy",
        "-movflags", "+faststart",
        tmp_out,
    ]

    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            timeout=600,
        )

    res = await asyncio.to_thread(_run, cmd_copy)
    if res.returncode != 0 or not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
        safe_remove(tmp_out)
        # Pass 2: Copy video stream losslessly, transcode audio to universal AAC
        cmd_aac = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            tmp_out,
        ]
        res2 = await asyncio.to_thread(_run, cmd_aac)
        if res2.returncode != 0 or not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
            err = res2.stderr.decode("utf-8", errors="ignore").strip()
            safe_remove(tmp_out)
            raise RuntimeError(f"FFmpeg merge failed: {err or 'unknown error'}")

    safe_rename(tmp_out, output_path)


async def run_video_turbo_download(
    item: DownloadItem,
    *,
    connections: int,
    pause_event: asyncio.Event,
    cancel_event: asyncio.Event,
    get_speed_limit_kbps: Callable[[], int],
    on_progress: ProgressCallback,
    on_status_change: Callable[[str, str], Any],
) -> tuple[str, int]:
    """
    Strategy A: Downloads extracted direct HTTP video (and optional separate audio)
    streams simultaneously using TurboDM's multi-connection byte-range engine,
    then muxes them losslessly via FFmpeg.
    Raises DirectEngineError if the CDN rejects ranges/URL expired so caller can
    seamlessly fall back to yt-dlp Strategy B.
    """
    v_url = item.direct_video_url
    a_url = item.direct_audio_url
    if not v_url:
        raise DirectEngineError("No direct stream URL available for turbo mode.")

    # Case 1: Single muxed direct video (or direct M4A audio)
    if not a_url:
        return await run_direct_download(
            url=v_url,
            dest_filepath=item.filepath,
            connections=connections,
            headers=item.http_headers,
            total_bytes=item.total_bytes if item.total_bytes > 0 else None,
            supports_range=True,
            existing_segments=item.segments if item.segments else None,
            pause_event=pause_event,
            cancel_event=cancel_event,
            get_speed_limit_kbps=get_speed_limit_kbps,
            on_progress=on_progress,
        )

    # Case 2: Adaptive DASH (separate direct video + audio streams downloaded in parallel!)
    base_tmp = item.filepath
    v_dest = base_tmp + ".turbovideo"
    a_dest = base_tmp + ".turboaudio"

    state = {
        "v_dl": 0,
        "v_total": 0,
        "v_speed": 0.0,
        "v_conns": 0,
        "v_segs": [],
        "a_dl": 0,
        "a_total": 0,
        "a_speed": 0.0,
        "a_conns": 0,
        "a_segs": [],
    }
    last_emit = 0.0

    async def emit_combined(force: bool = False) -> None:
        nonlocal last_emit
        now = time.monotonic()
        if not force and (now - last_emit) < 0.16:
            return
        last_emit = now
        dl = state["v_dl"] + state["a_dl"]
        total = state["v_total"] + state["a_total"]
        speed = state["v_speed"] + state["a_speed"]
        conns = state["v_conns"] + state["a_conns"]
        rem = max(0, total - dl)
        eta = (rem / speed) if (total > 0 and speed > 100) else None
        # Display video segments primarily (plus audio segments re-indexed)
        combined_segs: list[SegmentInfo] = list(state["v_segs"])
        base_idx = len(combined_segs)
        for s in state["a_segs"]:
            combined_segs.append(s.model_copy(update={"index": base_idx + s.index}))
        await on_progress(
            dl,
            total,
            speed,
            eta,
            conns,
            combined_segs,
            f"Turbo Downloading ({conns} conn)",
        )

    async def v_progress(dl, total, speed, eta, conns, segs, status_txt):
        state["v_dl"] = dl
        state["v_total"] = total
        state["v_speed"] = speed
        state["v_conns"] = conns
        state["v_segs"] = segs
        await emit_combined()

    async def a_progress(dl, total, speed, eta, conns, segs, status_txt):
        state["a_dl"] = dl
        state["a_total"] = total
        state["a_speed"] = speed
        state["a_conns"] = conns
        state["a_segs"] = segs
        await emit_combined()

    v_conns = max(4, connections - 2)
    a_conns = max(2, min(4, connections // 3))

    try:
        v_task = asyncio.create_task(
            run_direct_download(
                url=v_url,
                dest_filepath=v_dest,
                connections=v_conns,
                headers=item.http_headers,
                total_bytes=None,
                supports_range=True,
                existing_segments=None,
                pause_event=pause_event,
                cancel_event=cancel_event,
                get_speed_limit_kbps=get_speed_limit_kbps,
                on_progress=v_progress,
                part_suffix=".part",
                finalize_rename=True,
            )
        )
        a_task = asyncio.create_task(
            run_direct_download(
                url=a_url,
                dest_filepath=a_dest,
                connections=a_conns,
                headers=item.http_headers,
                total_bytes=None,
                supports_range=True,
                existing_segments=None,
                pause_event=pause_event,
                cancel_event=cancel_event,
                get_speed_limit_kbps=get_speed_limit_kbps,
                on_progress=a_progress,
                part_suffix=".part",
                finalize_rename=True,
            )
        )

        results = await asyncio.gather(v_task, a_task, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                raise r

        # Both streams completed -> Mux with FFmpeg!
        await emit_combined(force=True)
        on_status_change("merging", "Merging HD video & audio with FFmpeg...")
        await _mux_video_audio_ffmpeg(v_dest, a_dest, item.filepath)
        final_size = os.path.getsize(item.filepath) if os.path.exists(item.filepath) else (state["v_dl"] + state["a_dl"])
        return item.filepath, final_size

    except (DownloadPausedError, DownloadCancelledError):
        if cancel_event.is_set():
            safe_remove(v_dest)
            safe_remove(v_dest + ".part")
            safe_remove(a_dest)
            safe_remove(a_dest + ".part")
        raise
    except Exception:
        safe_remove(v_dest)
        safe_remove(v_dest + ".part")
        safe_remove(a_dest)
        safe_remove(a_dest + ".part")
        raise
    finally:
        if os.path.exists(item.filepath) and os.path.getsize(item.filepath) > 0:
            safe_remove(v_dest)
            safe_remove(a_dest)


async def run_video_ytdlp_download(
    item: DownloadItem,
    *,
    concurrent_fragments: int,
    cookies_file: str,
    pause_event: asyncio.Event,
    cancel_event: asyncio.Event,
    get_speed_limit_kbps: Callable[[], int],
    on_progress: ProgressCallback,
    on_status_change: Callable[[str, str], Any],
) -> tuple[str, int]:
    """
    Strategy B: Universal high-speed yt-dlp engine supporting 1000+ sites, HLS, DASH,
    chunked HTTP (10MB chunks to avoid throttling), concurrent fragment downloading,
    automatic FFmpeg merge/audio extraction, and clean pause/resume via .part files.
    """
    loop = asyncio.get_running_loop()
    ffmpeg_path = get_ffmpeg_path()
    node_path = get_node_path()

    os.makedirs(item.save_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(item.filepath))[0]
    stem = sanitize_filename(stem)
    outtmpl = os.path.join(item.save_dir, f"{stem}.%(ext)s")

    # Track stream phases (video + audio) so progress reports accurately
    phase_state = {
        "final_filepath": "",
        "last_emit": 0.0,
        "completed_files_bytes": 0,
        "current_file": "",
        "last_total": item.total_bytes,
    }

    def schedule_progress(
        dl: int,
        total: int,
        speed: float,
        eta: Optional[float],
        conns: int,
        segs: list[SegmentInfo],
        txt: str,
    ) -> None:
        asyncio.run_coroutine_threadsafe(
            on_progress(dl, total, speed, eta, conns, segs, txt), loop
        )

    def progress_hook(d: dict[str, Any]) -> None:
        if cancel_event.is_set():
            raise _YtDlpCancelled()
        if pause_event.is_set():
            raise _YtDlpPaused()

        status = d.get("status")
        filename = d.get("filename") or ""

        if status == "downloading":
            now = time.monotonic()
            if (now - phase_state["last_emit"]) < 0.18:
                return
            phase_state["last_emit"] = now

            if filename and phase_state["current_file"] and filename != phase_state["current_file"]:
                # Switched from video stream to audio stream
                pass
            if filename:
                phase_state["current_file"] = filename

            dl = int(d.get("downloaded_bytes") or 0)
            total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
            speed = float(d.get("speed") or 0.0)
            eta = d.get("eta")
            eta_f = float(eta) if isinstance(eta, (int, float)) else None

            frag_idx = d.get("fragment_index")
            frag_cnt = d.get("fragment_count")

            if frag_cnt and frag_cnt > 1:
                # Build visual fragment segments (bucketed cleanly to 8-12 visual bars like IDM)
                bars = min(12, max(4, concurrent_fragments))
                segs = _build_fragment_segments(int(frag_idx or 0), int(frag_cnt), total or max(dl, 1), bars, speed)
                txt = f"Downloading Fragments ({frag_idx or 0}/{frag_cnt}, {concurrent_fragments}x)"
                active_conns = min(concurrent_fragments, max(1, int(frag_cnt) - int(frag_idx or 0)))
            else:
                seg_total = max(total, dl, 1)
                segs = [
                    SegmentInfo(
                        index=0,
                        start=0,
                        end=seg_total - 1,
                        downloaded=dl,
                        speed=speed,
                        status="active",
                    )
                ]
                txt = "Downloading Stream (Turbo Chunked)"
                active_conns = max(1, min(concurrent_fragments, 4))

            if total > 0:
                phase_state["last_total"] = total

            schedule_progress(dl, total or phase_state["last_total"], speed, eta_f, active_conns, segs, txt)

        elif status == "finished":
            if filename:
                phase_state["final_filepath"] = filename
                try:
                    phase_state["completed_files_bytes"] += os.path.getsize(filename)
                except OSError:
                    pass
            loop.call_soon_threadsafe(
                on_status_change, "merging", "Processing / Merging media..."
            )

    def postprocessor_hook(d: dict[str, Any]) -> None:
        if cancel_event.is_set():
            raise _YtDlpCancelled()
        st = d.get("status")
        pp_name = d.get("postprocessor") or "FFmpeg"
        info_dict = d.get("info_dict") or {}
        fp = info_dict.get("filepath") or info_dict.get("_filename")
        if fp:
            phase_state["final_filepath"] = fp
        if st == "started":
            loop.call_soon_threadsafe(
                on_status_change, "merging", f"Finalizing ({pp_name})..."
            )

    def _run_sync() -> str:
        import yt_dlp

        fmt = item.format_id or "bestvideo+bestaudio/best"
        is_sniffed_stream = fmt.startswith(("stream:", "sniff:"))
        if fmt.startswith("stream:"):
            target_url = fmt[len("stream:"):]
            fmt = "best"
        elif fmt.startswith("sniff:"):
            target_url = item.direct_video_url or item.url
            fmt = "best"
        else:
            target_url = item.url

        is_youtube = any(
            d in target_url.lower() for d in ("youtube.com", "youtu.be", "music.youtube.com")
        )

        # Build resilient attempt strategies (handles transient CDN 403s / client glitches seamlessly)
        strategies: list[dict[str, Any]] = [
            {"label": "primary", "extra": {}},
        ]
        if is_youtube:
            strategies.append(
                {
                    "label": "yt_retry_fresh",
                    "extra": {"extractor_args": {"youtube": {"player_client": ["web_safari", "tv", "web"]}}},
                }
            )
            strategies.append(
                {
                    "label": "yt_retry_android",
                    "extra": {"extractor_args": {"youtube": {"player_client": ["android_vr", "android", "mweb"]}}},
                }
            )
        else:
            strategies.append({"label": "retry_fresh", "extra": {}})

        last_exc: Optional[Exception] = None

        for attempt_idx, strat in enumerate(strategies):
            if cancel_event.is_set():
                raise _YtDlpCancelled()
            if pause_event.is_set():
                raise _YtDlpPaused()

            if attempt_idx > 0:
                loop.call_soon_threadsafe(
                    on_status_change,
                    "downloading",
                    f"Refreshing stream link (attempt {attempt_idx + 1})...",
                )
                time.sleep(0.9)

            ydl_opts: dict[str, Any] = {
                "format": fmt,
                "outtmpl": outtmpl,
                "noplaylist": True,
                "continuedl": True,
                "overwrites": False,
                "quiet": True,
                "no_warnings": True,
                "logger": _SilentYtDlpLogger(),
                "nocheckcertificate": True,
                "concurrent_fragment_downloads": max(1, min(32, int(concurrent_fragments))),
                "http_chunk_size": 10 * 1024 * 1024,
                "retries": 10,
                "fragment_retries": 10,
                "file_access_retries": 5,
                "extractor_retries": 5,
                "socket_timeout": 30,
                "progress_hooks": [progress_hook],
                "postprocessor_hooks": [postprocessor_hook],
            }

            # Only forward custom Referer/headers for sniffed/direct streams; let yt-dlp manage native site client headers
            if is_sniffed_stream and item.http_headers:
                ydl_opts["http_headers"] = dict(item.http_headers)

            speed_limit_kbps = get_speed_limit_kbps()
            if speed_limit_kbps > 0:
                ydl_opts["ratelimit"] = speed_limit_kbps * 1024

            if ffmpeg_path:
                ydl_opts["ffmpeg_location"] = os.path.dirname(ffmpeg_path)
            if node_path:
                ydl_opts["js_runtimes"] = {"node": {"path": node_path}}
            if cookies_file and os.path.exists(cookies_file):
                ydl_opts["cookiefile"] = cookies_file

            if item.is_audio_only:
                codec = item.audio_format or "mp3"
                ydl_opts["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": codec,
                        "preferredquality": "320",
                    }
                ]
            else:
                ydl_opts["merge_output_format"] = "mp4"

            ydl_opts.update(strat["extra"])

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(target_url, download=True)
                    candidates: list[str] = []
                    if phase_state["final_filepath"]:
                        candidates.append(phase_state["final_filepath"])
                    if info:
                        for rd in info.get("requested_downloads") or []:
                            if rd.get("filepath"):
                                candidates.append(rd["filepath"])
                        try:
                            prepared = ydl.prepare_filename(info)
                            candidates.append(prepared)
                            base, _ = os.path.splitext(prepared)
                            candidates.append(base + (f".{item.audio_format}" if item.is_audio_only else ".mp4"))
                            candidates.append(base + ".mkv")
                            candidates.append(base + ".webm")
                        except Exception:
                            pass

                    for c in candidates:
                        if c and os.path.exists(c) and not c.endswith(".part"):
                            return c

                    pattern = os.path.join(item.save_dir, f"{glob.escape(stem)}.*")
                    for match in sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True):
                        if not match.endswith((".part", ".ytdl", ".temp", ".turbopart")):
                            return match

                    return item.filepath

            except (_YtDlpPaused, _YtDlpCancelled):
                raise
            except Exception as exc:
                last_exc = exc
                err_low = str(exc).lower()
                # Never retry DRM errors
                if "drm" in err_low or "widevine" in err_low:
                    raise
                # Retry on HTTP 403 / 429 / format / transient network errors
                retryable = any(
                    k in err_low
                    for k in ("403", "429", "forbidden", "unable to download", "format is not available", "timed out", "connection")
                )
                if not retryable or attempt_idx == len(strategies) - 1:
                    raise
                if "format is not available" in err_low:
                    fmt = "bestaudio/best" if item.is_audio_only else "bestvideo+bestaudio/best"
                # Remove corrupt/empty .part before switching client
                _cleanup_ytdlp_partials(item.save_dir, stem)

        if last_exc:
            raise last_exc
        return item.filepath

    try:
        resolved_path = await asyncio.to_thread(_run_sync)
    except _YtDlpPaused:
        raise DownloadPausedError()
    except _YtDlpCancelled:
        _cleanup_ytdlp_partials(item.save_dir, stem)
        raise DownloadCancelledError()
    except Exception as exc:
        msg = ANSI_RE.sub("", str(exc)).replace("ERROR:", "").strip()
        if "drm" in msg.lower() or "widevine" in msg.lower():
            raise RuntimeError("This video is DRM-protected and cannot be downloaded.")
        raise RuntimeError(msg or "Video download failed.")

    final_size = os.path.getsize(resolved_path) if os.path.exists(resolved_path) else phase_state["last_total"]
    return resolved_path, final_size


def _build_fragment_segments(
    frag_idx: int, frag_cnt: int, total_bytes: int, bars: int, total_speed: float
) -> list[SegmentInfo]:
    segs: list[SegmentInfo] = []
    chunk = max(1, total_bytes // bars)
    ratio = min(1.0, max(0.0, frag_idx / max(1, frag_cnt)))
    filled_bars = ratio * bars
    for i in range(bars):
        start = i * chunk
        end = (total_bytes - 1) if i == bars - 1 else (start + chunk - 1)
        seg_len = max(1, end - start + 1)
        if i < int(filled_bars):
            dl = seg_len
            st = "done"
            sp = 0.0
        elif i == int(filled_bars):
            frac = filled_bars - int(filled_bars)
            dl = int(seg_len * frac)
            st = "active"
            sp = total_speed
        else:
            dl = 0
            st = "active" if i == int(filled_bars) + 1 else "idle"
            sp = 0.0
        segs.append(SegmentInfo(index=i, start=start, end=end, downloaded=dl, speed=sp, status=st))
    return segs


def _cleanup_ytdlp_partials(save_dir: str, stem: str) -> None:
    pattern = os.path.join(save_dir, f"{glob.escape(stem)}*")
    for path in glob.glob(pattern):
        if any(path.endswith(suf) for suf in (".part", ".ytdl", ".temp", ".turbopart")) or ".part-Frag" in path:
            safe_remove(path)
