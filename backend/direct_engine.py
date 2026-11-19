from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import aiohttp

from backend.config import DEFAULT_USER_AGENT
from backend.models import SegmentInfo
from backend.utils import safe_remove, safe_rename

CHUNK_READ_SIZE = 256 * 1024        # 256 KB network read buffer for high throughput
MIN_SEGMENT_SIZE = 512 * 1024       # Don't split files smaller than 512 KB per segment
MIN_STEAL_REMAINING = 2 * 1024 * 1024  # Only steal work if >= 2 MB left in a segment
MAX_RETRIES_PER_SEG = 8


class DownloadPausedError(Exception):
    pass


class DownloadCancelledError(Exception):
    pass


class RangeNotSupportedFallback(Exception):
    pass


class DirectEngineError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OffsetFileWriter:
    """Thread-safe, Windows-safe single-handle random-access file writer."""

    def __init__(self, path: str, total_size: Optional[int], resume: bool) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._closed = False
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        mode = "r+b" if (resume and os.path.exists(path)) else "w+b"
        self._fp = open(path, mode)
        if total_size and total_size > 0:
            current_size = os.path.getsize(path)
            if current_size < total_size:
                self._fp.seek(total_size - 1)
                self._fp.write(b"\x00")
                self._fp.flush()

    def _write_sync(self, offset: int, data: bytes) -> None:
        with self._lock:
            if self._closed:
                return
            self._fp.seek(offset)
            self._fp.write(data)

    async def write_at(self, offset: int, data: bytes) -> None:
        await asyncio.to_thread(self._write_sync, offset, data)

    def _append_sync(self, data: bytes) -> None:
        with self._lock:
            if self._closed:
                return
            self._fp.write(data)

    async def append(self, data: bytes) -> None:
        await asyncio.to_thread(self._append_sync, data)

    def _close_sync(self) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self._fp.flush()
                finally:
                    self._fp.close()
                    self._closed = True

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)


class SpeedLimiter:
    """Async token-bucket bandwidth throttle (0 = unlimited)."""

    def __init__(self, get_limit_kbps: Callable[[], int]) -> None:
        self._get_limit_kbps = get_limit_kbps
        self._lock = asyncio.Lock()
        self._tokens = 0.0
        self._last_time = time.monotonic()

    async def consume(self, num_bytes: int) -> None:
        limit_kbps = self._get_limit_kbps()
        if limit_kbps <= 0:
            return
        rate_bps = limit_kbps * 1024.0
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_time
            self._last_time = now
            self._tokens = min(rate_bps, self._tokens + elapsed * rate_bps)
            self._tokens -= num_bytes
            if self._tokens < 0:
                sleep_for = -self._tokens / rate_bps
                await asyncio.sleep(min(sleep_for, 1.0))


@dataclass
class MutableSegment:
    index: int
    start: int
    end: int
    downloaded: int = 0
    speed: float = 0.0
    status: str = "idle"  # idle, active, done, paused, error

    @property
    def current_pos(self) -> int:
        return self.start + self.downloaded

    @property
    def remaining(self) -> int:
        if self.end < self.start:
            return 0
        return max(0, (self.end - self.start + 1) - self.downloaded)

    def to_model(self) -> SegmentInfo:
        return SegmentInfo(
            index=self.index,
            start=self.start,
            end=self.end,
            downloaded=self.downloaded,
            speed=self.speed,
            status=self.status,
        )


ProgressCallback = Callable[
    [int, int, float, Optional[float], int, list[SegmentInfo], str],
    Awaitable[None],
]


async def run_direct_download(
    url: str,
    dest_filepath: str,
    *,
    connections: int = 12,
    headers: Optional[dict[str, str]] = None,
    total_bytes: Optional[int] = None,
    supports_range: Optional[bool] = None,
    existing_segments: Optional[list[SegmentInfo]] = None,
    pause_event: asyncio.Event,
    cancel_event: asyncio.Event,
    get_speed_limit_kbps: Callable[[], int],
    on_progress: ProgressCallback,
    part_suffix: str = ".turbopart",
    finalize_rename: bool = True,
) -> tuple[str, int]:
    """
    High-speed IDM-style multi-connection HTTP downloader with dynamic work-stealing,
    exact byte-range pause/resume, automatic single-stream fallback, and integrity checks.

    Returns (final_path, downloaded_bytes).
    """
    req_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    if headers:
        for k, v in headers.items():
            if k.lower() != "range":
                req_headers[k] = v
    req_headers["Accept-Encoding"] = "identity"

    part_path = dest_filepath + part_suffix
    connections = max(1, min(32, int(connections)))

    timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_connect=20, sock_read=45)
    connector = aiohttp.TCPConnector(
        limit=max(connections + 8, 32),
        limit_per_host=max(connections + 4, 32),
        ttl_dns_cache=300,
        ssl=False,
        force_close=False,
        enable_cleanup_closed=True,
    )

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        # Step 1: Probe exact size & range capability if unknown
        if total_bytes is None or total_bytes <= 0 or supports_range is None:
            probed_url, probed_size, probed_range = await _quick_probe(session, url, req_headers)
            if probed_url:
                url = probed_url
            if probed_size and probed_size > 0:
                total_bytes = probed_size
            if supports_range is None:
                supports_range = probed_range

        total_size = int(total_bytes or 0)
        can_segment = bool(supports_range) and total_size >= (MIN_SEGMENT_SIZE * 2) and connections > 1

        # Decide whether we can resume existing segments cleanly
        resuming = (
            can_segment
            and existing_segments
            and len(existing_segments) > 0
            and os.path.exists(part_path)
            and sum((s.end - s.start + 1) for s in existing_segments if s.end >= s.start) == total_size
        )

        if can_segment:
            try:
                final_bytes = await _download_segmented(
                    session=session,
                    url=url,
                    req_headers=req_headers,
                    part_path=part_path,
                    total_size=total_size,
                    max_connections=connections,
                    existing_segments=existing_segments if resuming else None,
                    pause_event=pause_event,
                    cancel_event=cancel_event,
                    get_speed_limit_kbps=get_speed_limit_kbps,
                    on_progress=on_progress,
                )
            except RangeNotSupportedFallback:
                # Server advertised range support or rejected range -> cleanly fall back to single stream
                safe_remove(part_path)
                final_bytes = await _download_single_stream(
                    session=session,
                    url=url,
                    req_headers=req_headers,
                    part_path=part_path,
                    expected_size=total_size,
                    pause_event=pause_event,
                    cancel_event=cancel_event,
                    get_speed_limit_kbps=get_speed_limit_kbps,
                    on_progress=on_progress,
                )
        else:
            final_bytes = await _download_single_stream(
                session=session,
                url=url,
                req_headers=req_headers,
                part_path=part_path,
                expected_size=total_size,
                pause_event=pause_event,
                cancel_event=cancel_event,
                get_speed_limit_kbps=get_speed_limit_kbps,
                on_progress=on_progress,
            )

    if finalize_rename:
        safe_rename(part_path, dest_filepath)
        return dest_filepath, final_bytes
    return part_path, final_bytes


async def _quick_probe(
    session: aiohttp.ClientSession, url: str, headers: dict[str, str]
) -> tuple[str, Optional[int], bool]:
    range_h = {**headers, "Range": "bytes=0-0"}
    try:
        async with session.get(url, headers=range_h, allow_redirects=True) as resp:
            if resp.status == 206:
                cr = resp.headers.get("Content-Range", "")
                if "/" in cr:
                    tail = cr.rsplit("/", 1)[1].strip()
                    if tail.isdigit():
                        return str(resp.url), int(tail), True
                return str(resp.url), None, True
            if resp.status == 200:
                cl = resp.headers.get("Content-Length", "")
                ar = resp.headers.get("Accept-Ranges", "").lower()
                return str(resp.url), (int(cl) if cl.isdigit() else None), ("bytes" in ar)
            if resp.status in (403, 404, 410):
                raise DirectEngineError(f"Server returned HTTP {resp.status}", status_code=resp.status)
    except DirectEngineError:
        raise
    except Exception:
        pass

    try:
        async with session.head(url, headers=headers, allow_redirects=True) as resp:
            if resp.status < 400:
                cl = resp.headers.get("Content-Length", "")
                ar = resp.headers.get("Accept-Ranges", "").lower()
                return str(resp.url), (int(cl) if cl.isdigit() else None), ("bytes" in ar)
    except Exception:
        pass
    return url, None, False


async def _download_segmented(
    *,
    session: aiohttp.ClientSession,
    url: str,
    req_headers: dict[str, str],
    part_path: str,
    total_size: int,
    max_connections: int,
    existing_segments: Optional[list[SegmentInfo]],
    pause_event: asyncio.Event,
    cancel_event: asyncio.Event,
    get_speed_limit_kbps: Callable[[], int],
    on_progress: ProgressCallback,
) -> int:
    # Build initial segment table
    segments: list[MutableSegment] = []
    if existing_segments:
        for idx, s in enumerate(existing_segments):
            status = "done" if (s.end >= s.start and s.downloaded >= (s.end - s.start + 1)) else "idle"
            segments.append(
                MutableSegment(
                    index=idx,
                    start=s.start,
                    end=s.end,
                    downloaded=s.downloaded,
                    speed=0.0,
                    status=status,
                )
            )
    else:
        num_segs = min(max_connections, max(1, total_size // MIN_SEGMENT_SIZE))
        seg_size = total_size // num_segs
        for i in range(num_segs):
            start = i * seg_size
            end = (total_size - 1) if (i == num_segs - 1) else (start + seg_size - 1)
            segments.append(MutableSegment(index=i, start=start, end=end, status="idle"))

    writer = OffsetFileWriter(part_path, total_size=total_size, resume=bool(existing_segments))
    limiter = SpeedLimiter(get_speed_limit_kbps)
    speed_window: deque[tuple[float, int]] = deque()
    active_workers = 0
    fatal_error: Optional[Exception] = None
    fallback_triggered = False

    def total_downloaded() -> int:
        return sum(s.downloaded for s in segments)

    def pick_next_segment() -> Optional[MutableSegment]:
        # 1. Pick any idle segment with remaining bytes
        for s in segments:
            if s.status == "idle" and s.remaining > 0:
                s.status = "active"
                return s
        # 2. Dynamic Work-Stealing (IDM core): split the active segment with largest remaining bytes
        candidates = [s for s in segments if s.status == "active" and s.remaining >= MIN_STEAL_REMAINING]
        if not candidates:
            return None
        largest = max(candidates, key=lambda s: s.remaining)
        rem = largest.remaining
        if rem < MIN_STEAL_REMAINING:
            return None
        cur = largest.current_pos
        mid = cur + (rem // 2)
        old_end = largest.end
        if mid <= cur or mid > old_end:
            return None
        largest.end = mid - 1
        new_seg = MutableSegment(
            index=len(segments),
            start=mid,
            end=old_end,
            downloaded=0,
            speed=0.0,
            status="active",
        )
        segments.append(new_seg)
        return new_seg

    async def worker_loop() -> None:
        nonlocal active_workers, fatal_error, fallback_triggered

        while not pause_event.is_set() and not cancel_event.is_set() and not fatal_error and not fallback_triggered:
            seg = pick_next_segment()
            if seg is None:
                return

            active_workers += 1
            try:
                retries = 0
                while seg.remaining > 0:
                    if pause_event.is_set() or cancel_event.is_set() or fatal_error or fallback_triggered:
                        seg.status = "paused" if pause_event.is_set() else "idle"
                        seg.speed = 0.0
                        return

                    req_start = seg.current_pos
                    req_end = seg.end
                    if req_start > req_end:
                        break

                    h = {**req_headers, "Range": f"bytes={req_start}-{req_end}"}
                    try:
                        async with session.get(url, headers=h, allow_redirects=True) as resp:
                            if resp.status == 200:
                                # Server ignored Range header!
                                fallback_triggered = True
                                return
                            if resp.status == 416:
                                if seg.remaining == 0:
                                    break
                                fallback_triggered = True
                                return
                            if resp.status in (403, 404, 410):
                                raise DirectEngineError(
                                    f"HTTP {resp.status} while downloading segment {seg.index}",
                                    status_code=resp.status,
                                )
                            if resp.status >= 400:
                                raise aiohttp.ClientError(f"HTTP {resp.status}")

                            retries = 0
                            seg_last_t = time.monotonic()
                            seg_bytes_acc = 0

                            while True:
                                if pause_event.is_set() or cancel_event.is_set() or fatal_error or fallback_triggered:
                                    seg.status = "paused" if pause_event.is_set() else "idle"
                                    seg.speed = 0.0
                                    return

                                if seg.remaining <= 0:
                                    break

                                chunk = await resp.content.read(CHUNK_READ_SIZE)
                                if not chunk:
                                    break

                                # Synchronously claim exact non-overlapping byte window before awaiting write
                                write_offset = seg.current_pos
                                allowed = seg.end - write_offset + 1
                                if allowed <= 0:
                                    break
                                if len(chunk) > allowed:
                                    chunk = chunk[:allowed]

                                chunk_len = len(chunk)
                                seg.downloaded += chunk_len
                                try:
                                    await writer.write_at(write_offset, chunk)
                                except Exception:
                                    seg.downloaded -= chunk_len
                                    raise

                                now = time.monotonic()
                                speed_window.append((now, chunk_len))
                                seg_bytes_acc += chunk_len
                                dt = now - seg_last_t
                                if dt >= 0.35:
                                    seg.speed = seg_bytes_acc / dt
                                    seg_bytes_acc = 0
                                    seg_last_t = now

                                await limiter.consume(chunk_len)

                                if seg.remaining <= 0:
                                    break

                    except DirectEngineError as derr:
                        seg.status = "error"
                        seg.speed = 0.0
                        fatal_error = derr
                        return
                    except asyncio.CancelledError:
                        seg.status = "paused"
                        seg.speed = 0.0
                        raise
                    except Exception as exc:
                        retries += 1
                        seg.speed = 0.0
                        if retries > MAX_RETRIES_PER_SEG:
                            seg.status = "error"
                            fatal_error = DirectEngineError(
                                f"Segment {seg.index + 1} failed after {MAX_RETRIES_PER_SEG} retries: {exc}"
                            )
                            return
                        await asyncio.sleep(min(0.6 * (2 ** (retries - 1)), 6.0))

                seg.status = "done"
                seg.speed = 0.0
            finally:
                active_workers = max(0, active_workers - 1)

    async def progress_reporter() -> None:
        while True:
            now = time.monotonic()
            cutoff = now - 1.8
            while speed_window and speed_window[0][0] < cutoff:
                speed_window.popleft()
            win_bytes = sum(b for _, b in speed_window)
            win_span = (now - speed_window[0][0]) if len(speed_window) > 1 else 1.0
            speed = win_bytes / max(win_span, 0.35) if win_bytes > 0 else 0.0

            dl = total_downloaded()
            rem = max(0, total_size - dl)
            eta = (rem / speed) if speed > 100 else None

            status_txt = (
                f"Downloading ({active_workers} conn)" if active_workers > 0 else "Connecting..."
            )
            await on_progress(
                dl,
                total_size,
                speed,
                eta,
                active_workers,
                [s.to_model() for s in segments],
                status_txt,
            )
            await asyncio.sleep(0.18)

    reporter_task = asyncio.create_task(progress_reporter())
    worker_tasks = [asyncio.create_task(worker_loop()) for _ in range(max_connections)]

    try:
        await asyncio.gather(*worker_tasks, return_exceptions=True)
    finally:
        reporter_task.cancel()
        try:
            await reporter_task
        except asyncio.CancelledError:
            pass
        await writer.close()

    # Final state dispatch
    dl_now = total_downloaded()
    await on_progress(
        dl_now,
        total_size,
        0.0,
        None,
        0,
        [s.to_model() for s in segments],
        "Paused" if pause_event.is_set() else ("Cancelled" if cancel_event.is_set() else "Finalizing..."),
    )

    if cancel_event.is_set():
        safe_remove(part_path)
        raise DownloadCancelledError()
    if pause_event.is_set():
        raise DownloadPausedError()
    if fallback_triggered:
        raise RangeNotSupportedFallback()
    if fatal_error:
        raise fatal_error

    # Integrity verification
    if dl_now != total_size:
        raise DirectEngineError(
            f"Incomplete download: received {dl_now} of {total_size} bytes."
        )

    return dl_now


async def _download_single_stream(
    *,
    session: aiohttp.ClientSession,
    url: str,
    req_headers: dict[str, str],
    part_path: str,
    expected_size: int,
    pause_event: asyncio.Event,
    cancel_event: asyncio.Event,
    get_speed_limit_kbps: Callable[[], int],
    on_progress: ProgressCallback,
) -> int:
    """Clean single-connection fallback when server does not support HTTP byte ranges."""
    writer = OffsetFileWriter(part_path, total_size=None, resume=False)
    limiter = SpeedLimiter(get_speed_limit_kbps)
    speed_window: deque[tuple[float, int]] = deque()
    downloaded = 0
    total_size = expected_size
    last_report = 0.0

    try:
        retries = 0
        while True:
            try:
                async with session.get(url, headers=req_headers, allow_redirects=True) as resp:
                    if resp.status >= 400:
                        raise DirectEngineError(
                            f"Server returned HTTP {resp.status}",
                            status_code=resp.status,
                        )
                    cl = resp.headers.get("Content-Length", "")
                    if cl.isdigit() and int(cl) > 0:
                        total_size = int(cl)

                    while True:
                        if cancel_event.is_set():
                            raise DownloadCancelledError()
                        if pause_event.is_set():
                            raise DownloadPausedError()

                        chunk = await resp.content.read(CHUNK_READ_SIZE)
                        if not chunk:
                            break
                        await writer.append(chunk)
                        chunk_len = len(chunk)
                        downloaded += chunk_len

                        now = time.monotonic()
                        speed_window.append((now, chunk_len))
                        await limiter.consume(chunk_len)

                        if now - last_report >= 0.18:
                            last_report = now
                            cutoff = now - 1.8
                            while speed_window and speed_window[0][0] < cutoff:
                                speed_window.popleft()
                            win_bytes = sum(b for _, b in speed_window)
                            win_span = (now - speed_window[0][0]) if len(speed_window) > 1 else 1.0
                            speed = win_bytes / max(win_span, 0.35) if win_bytes > 0 else 0.0
                            eta = (
                                (max(0, total_size - downloaded) / speed)
                                if (total_size > 0 and speed > 100)
                                else None
                            )
                            seg_end = max(total_size - 1, downloaded - 1, 0)
                            seg = SegmentInfo(
                                index=0,
                                start=0,
                                end=seg_end,
                                downloaded=downloaded,
                                speed=speed,
                                status="active",
                            )
                            await on_progress(
                                downloaded,
                                total_size,
                                speed,
                                eta,
                                1,
                                [seg],
                                "Downloading (Single Stream)",
                            )
                    break
            except (DownloadCancelledError, DownloadPausedError, DirectEngineError):
                raise
            except Exception as exc:
                retries += 1
                if retries > 4 or downloaded > 0:
                    raise DirectEngineError(f"Stream interrupted: {exc}")
                await asyncio.sleep(min(1.0 * retries, 4.0))
    finally:
        await writer.close()
        if cancel_event.is_set():
            safe_remove(part_path)

    seg_end = max(total_size - 1, downloaded - 1, 0)
    await on_progress(
        downloaded,
        total_size or downloaded,
        0.0,
        0.0,
        0,
        [SegmentInfo(index=0, start=0, end=seg_end, downloaded=downloaded, speed=0.0, status="done")],
        "Finalizing...",
    )
    return downloaded
