from __future__ import annotations

import asyncio
import glob
import os
import subprocess
import time
import uuid
from typing import Any, Optional

from fastapi import WebSocket

from backend.config import CLOUD_MODE, SettingsStore, get_ffmpeg_path, get_node_path
from backend.database import DownloadDB
from backend.direct_engine import (
    DirectEngineError,
    DownloadCancelledError,
    DownloadPausedError,
    run_direct_download,
)
from backend.models import (
    AppSettings,
    BatchDownloadRequest,
    DownloadItem,
    DownloadMode,
    DownloadStatus,
    MediaCategory,
    ProbeResult,
    SegmentInfo,
    StartDownloadRequest,
)
from backend.probe import probe_url
from backend.utils import (
    clean_url,
    detect_category,
    get_unique_filepath,
    is_valid_http_url,
    resolve_category_dir,
    safe_remove,
    sanitize_filename,
)
from backend.video_engine import run_video_turbo_download, run_video_ytdlp_download


class _TaskControl:
    def __init__(self) -> None:
        self.pause_event = asyncio.Event()
        self.cancel_event = asyncio.Event()
        self.task: Optional[asyncio.Task] = None


class DownloadManager:
    def __init__(self) -> None:
        self.settings_store = SettingsStore()
        self.db = DownloadDB()
        self.items: dict[str, DownloadItem] = {}
        self.controls: dict[str, _TaskControl] = {}
        self.ws_clients: set[WebSocket] = set()
        self._last_broadcast: dict[str, float] = {}
        self._last_db_save: dict[str, float] = {}
        self._lock = asyncio.Lock()

        # Provision ffmpeg immediately on startup
        self.ffmpeg_path = get_ffmpeg_path()
        self.node_path = get_node_path()

        # Load persisted downloads from SQLite
        for item in self.db.load_all():
            self.items[item.id] = item

    # ------------------------------------------------------------------
    # WebSocket management
    # ------------------------------------------------------------------
    async def register_ws(self, ws: WebSocket) -> None:
        await ws.accept()
        self.ws_clients.add(ws)
        await self._send_ws(ws, self._snapshot_message())

    def unregister_ws(self, ws: WebSocket) -> None:
        self.ws_clients.discard(ws)

    async def _send_ws(self, ws: WebSocket, payload: dict[str, Any]) -> None:
        try:
            await ws.send_json(payload)
        except Exception:
            self.ws_clients.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self.ws_clients:
            return
        dead: list[WebSocket] = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)

    def _snapshot_message(self) -> dict[str, Any]:
        ordered = sorted(self.items.values(), key=lambda x: x.created_at, reverse=True)
        return {
            "type": "snapshot",
            "items": [i.model_dump() for i in ordered],
            "settings": self.settings_store.get().model_dump(),
            "system": self.get_system_info(),
        }

    async def _broadcast_item(self, item: DownloadItem, force: bool = False) -> None:
        now = time.monotonic()
        last = self._last_broadcast.get(item.id, 0.0)
        if not force and (now - last) < 0.14:
            return
        self._last_broadcast[item.id] = now

        # Persist to DB at most every 2s during active downloading, or immediately on force
        last_save = self._last_db_save.get(item.id, 0.0)
        if force or (now - last_save) >= 2.0:
            self._last_db_save[item.id] = now
            self.db.save(item)

        await self.broadcast({"type": "item_update", "item": item.model_dump()})

    # ------------------------------------------------------------------
    # Info & Settings
    # ------------------------------------------------------------------
    def get_system_info(self) -> dict[str, Any]:
        total_speed = sum(
            i.speed for i in self.items.values() if i.status == DownloadStatus.DOWNLOADING
        )
        active_count = sum(
            1
            for i in self.items.values()
            if i.status in (DownloadStatus.DOWNLOADING, DownloadStatus.MERGING, DownloadStatus.PROBING)
        )
        return {
            "ffmpeg_ready": bool(self.ffmpeg_path),
            "node_ready": bool(self.node_path),
            "cloud_mode": CLOUD_MODE,
            "total_speed": total_speed,
            "active_count": active_count,
            "total_count": len(self.items),
        }

    def get_settings(self) -> AppSettings:
        return self.settings_store.get()

    async def update_settings(self, new_settings: AppSettings) -> AppSettings:
        updated = self.settings_store.update(new_settings)
        await self.broadcast({"type": "settings_update", "settings": updated.model_dump()})
        await self._schedule_queue()
        return updated

    def list_items(self) -> list[DownloadItem]:
        return sorted(self.items.values(), key=lambda x: x.created_at, reverse=True)

    # ------------------------------------------------------------------
    # URL Analysis
    # ------------------------------------------------------------------
    async def analyze_url(self, raw_url: str) -> ProbeResult:
        settings = self.settings_store.get()
        return await probe_url(raw_url, cookies_file=settings.cookies_file)

    # ------------------------------------------------------------------
    # Add / Batch Downloads
    # ------------------------------------------------------------------
    async def add_download(self, req: StartDownloadRequest) -> DownloadItem:
        settings = self.settings_store.get()
        url = clean_url(req.url)
        if not is_valid_http_url(url):
            raise ValueError("Please provide a valid HTTP/HTTPS URL.")

        dl_id = uuid.uuid4().hex[:12]
        connections = max(1, min(32, int(req.connections or settings.default_connections)))
        base_dir = (req.save_dir or settings.download_dir).strip() or settings.download_dir

        # Determine if this needs probing first (quick-add) or already probed from modal
        needs_probe = req.kind is None

        if needs_probe:
            os.makedirs(base_dir, exist_ok=True)
            item = DownloadItem(
                id=dl_id,
                url=url,
                final_url=url,
                title=url,
                filename="Analyzing link...",
                save_dir=base_dir,
                filepath="",
                category=MediaCategory.OTHER,
                mode=DownloadMode.DIRECT,
                status=DownloadStatus.QUEUED if req.start_immediately else DownloadStatus.PAUSED,
                status_text="Queued (Will analyze)" if req.start_immediately else "Paused",
                connections=connections,
                format_id=req.format_id or "bestvideo+bestaudio/best",
                format_label=req.format_label or "",
                is_audio_only=req.is_audio_only,
                audio_format=req.audio_format or "mp3",
                http_headers=dict(req.http_headers or {}),
                created_at=time.time(),
            )
            self.items[dl_id] = item
            self.db.save(item)
            await self._broadcast_item(item, force=True)
            if req.start_immediately:
                await self._schedule_queue()
            return item

        # Already probed -> build exact target path & engine mode
        is_video_kind = req.kind in ("video", "sniffed")
        default_ext = f".{req.audio_format}" if req.is_audio_only else (".mp4" if is_video_kind else "")
        raw_name = req.filename or req.title or "download"
        safe_name = sanitize_filename(raw_name, default_ext=default_ext)

        # Ensure audio-only or mp4 merge gets right extension
        if req.is_audio_only:
            stem, _ = os.path.splitext(safe_name)
            safe_name = sanitize_filename(f"{stem}.{req.audio_format or 'mp3'}")
        elif is_video_kind and req.kind == "video":
            stem, ext = os.path.splitext(safe_name)
            if not ext:
                safe_name = sanitize_filename(f"{stem}.mp4")

        category = detect_category(
            safe_name,
            is_video_site=is_video_kind,
            is_audio_only=req.is_audio_only,
        )
        final_dir = resolve_category_dir(base_dir, category, settings.auto_categorize_folders)
        filepath = get_unique_filepath(final_dir, safe_name)
        actual_filename = os.path.basename(filepath)

        # Pick fastest engine mode
        if req.kind == "direct":
            mode = DownloadMode.DIRECT
        elif req.kind == "sniffed" and req.direct_video_url and not (req.format_id or "").startswith("stream:"):
            mode = DownloadMode.DIRECT
        elif (
            settings.enable_turbo_video_segments
            and req.direct_video_url
            and not req.is_audio_only
            and not (req.format_id or "").startswith("stream:")
        ):
            mode = DownloadMode.VIDEO_TURBO
        else:
            mode = DownloadMode.VIDEO_YTDLP

        target_url = req.direct_video_url if (mode == DownloadMode.DIRECT and req.direct_video_url) else url

        item = DownloadItem(
            id=dl_id,
            url=target_url,
            final_url=target_url,
            title=req.title or os.path.splitext(actual_filename)[0],
            filename=actual_filename,
            save_dir=final_dir,
            filepath=filepath,
            category=category,
            mode=mode,
            status=DownloadStatus.QUEUED if req.start_immediately else DownloadStatus.PAUSED,
            status_text="Queued" if req.start_immediately else "Paused",
            total_bytes=int(req.filesize or 0),
            connections=connections,
            supports_resume=bool(req.supports_range) if req.supports_range is not None else True,
            thumbnail=req.thumbnail,
            duration=req.duration,
            extractor=req.extractor or ("Direct HTTP" if mode == DownloadMode.DIRECT else "Video"),
            format_id=req.format_id or "bestvideo+bestaudio/best",
            format_label=req.format_label or "",
            is_audio_only=req.is_audio_only,
            audio_format=req.audio_format or "mp3",
            direct_video_url=req.direct_video_url,
            direct_audio_url=req.direct_audio_url,
            http_headers=dict(req.http_headers or {}),
            created_at=time.time(),
        )
        self.items[dl_id] = item
        self.db.save(item)
        await self._broadcast_item(item, force=True)

        if req.start_immediately:
            await self._schedule_queue()
        return item

    async def add_batch(self, req: BatchDownloadRequest) -> list[DownloadItem]:
        added: list[DownloadItem] = []
        quality = (req.quality or "best").lower()
        is_audio = quality == "audio"
        if quality == "1080":
            fmt = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
            fmt_label = "Up to 1080p Full HD"
        elif quality == "720":
            fmt = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
            fmt_label = "Up to 720p HD"
        elif quality == "480":
            fmt = "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
            fmt_label = "Up to 480p SD"
        elif is_audio:
            fmt = "bestaudio/best"
            fmt_label = "Audio MP3"
        else:
            fmt = "bestvideo+bestaudio/best"
            fmt_label = "Best Quality"

        for raw in req.urls:
            cleaned = clean_url(raw)
            if not is_valid_http_url(cleaned):
                continue
            item = await self.add_download(
                StartDownloadRequest(
                    url=cleaned,
                    save_dir=req.save_dir,
                    connections=req.connections,
                    format_id=fmt,
                    format_label=fmt_label,
                    is_audio_only=is_audio,
                    audio_format="mp3",
                    kind=None,  # Auto-probe each URL in background queue
                    start_immediately=True,
                )
            )
            added.append(item)
        return added

    # ------------------------------------------------------------------
    # Queue Scheduler & Execution
    # ------------------------------------------------------------------
    async def _schedule_queue(self) -> None:
        async with self._lock:
            settings = self.settings_store.get()
            active = sum(
                1
                for i in self.items.values()
                if i.status in (DownloadStatus.DOWNLOADING, DownloadStatus.PROBING, DownloadStatus.MERGING)
            )
            queued = sorted(
                [i for i in self.items.values() if i.status == DownloadStatus.QUEUED],
                key=lambda x: x.created_at,
            )
            for item in queued:
                if active >= settings.max_concurrent_downloads:
                    break
                self._launch_item_locked(item)
                active += 1

    def _launch_item_locked(self, item: DownloadItem) -> None:
        ctrl = _TaskControl()
        self.controls[item.id] = ctrl
        item.status = DownloadStatus.DOWNLOADING if item.filepath else DownloadStatus.PROBING
        item.status_text = "Connecting..." if item.filepath else "Analyzing link..."
        item.error_message = ""
        ctrl.task = asyncio.create_task(self._run_item(item.id, ctrl))

    async def _run_item(self, download_id: str, ctrl: _TaskControl) -> None:
        item = self.items.get(download_id)
        if not item:
            return
        settings = self.settings_store.get()

        try:
            # Step 0: If item was added via quick/batch add (no filepath yet), probe first!
            if not item.filepath:
                item.status = DownloadStatus.PROBING
                item.status_text = "Analyzing link..."
                await self._broadcast_item(item, force=True)

                probe = await probe_url(item.url, cookies_file=settings.cookies_file)
                if ctrl.cancel_event.is_set():
                    raise DownloadCancelledError()
                if ctrl.pause_event.is_set():
                    raise DownloadPausedError()

                if probe.kind == "playlist" and probe.playlist_entries:
                    # Pick first entry if a playlist link was pasted in quick mode
                    first = probe.playlist_entries[0]
                    probe = await probe_url(first["url"], cookies_file=settings.cookies_file)
                    item.url = probe.final_url

                self._apply_probe_to_item(item, probe, settings)
                item.status = DownloadStatus.DOWNLOADING
                item.status_text = "Starting download..."
                await self._broadcast_item(item, force=True)

            # Shared progress callback
            async def on_progress(
                dl: int,
                total: int,
                speed: float,
                eta: Optional[float],
                active_conns: int,
                segs: list[SegmentInfo],
                status_txt: str,
            ) -> None:
                if download_id not in self.items:
                    return
                item.downloaded_bytes = dl
                if total > 0:
                    item.total_bytes = total
                    item.progress = min(100.0, (dl / total) * 100.0)
                item.speed = speed
                item.eta = eta
                item.active_connections = active_conns
                item.segments = segs
                item.status_text = status_txt
                if speed > 0:
                    item.speed_history.append(round(speed, 1))
                    if len(item.speed_history) > 60:
                        item.speed_history = item.speed_history[-60:]
                await self._broadcast_item(item)

            def on_status_change(new_status: str, status_txt: str) -> None:
                if new_status == "merging":
                    item.status = DownloadStatus.MERGING
                item.status_text = status_txt
                asyncio.create_task(self._broadcast_item(item, force=True))

            get_limit = lambda: self.settings_store.get().speed_limit_kbps

            # Execute using optimal engine with automatic fallback!
            if item.mode == DownloadMode.DIRECT:
                final_path, final_size = await run_direct_download(
                    url=item.url,
                    dest_filepath=item.filepath,
                    connections=item.connections,
                    headers=item.http_headers,
                    total_bytes=item.total_bytes if item.total_bytes > 0 else None,
                    supports_range=item.supports_resume if item.segments else None,
                    existing_segments=item.segments if item.segments else None,
                    pause_event=ctrl.pause_event,
                    cancel_event=ctrl.cancel_event,
                    get_speed_limit_kbps=get_limit,
                    on_progress=on_progress,
                )
            elif item.mode == DownloadMode.VIDEO_TURBO:
                try:
                    final_path, final_size = await run_video_turbo_download(
                        item,
                        connections=item.connections,
                        pause_event=ctrl.pause_event,
                        cancel_event=ctrl.cancel_event,
                        get_speed_limit_kbps=get_limit,
                        on_progress=on_progress,
                        on_status_change=on_status_change,
                    )
                except DirectEngineError:
                    # Seamless zero-error fallback to yt-dlp concurrent engine!
                    item.mode = DownloadMode.VIDEO_YTDLP
                    item.segments = []
                    item.downloaded_bytes = 0
                    item.progress = 0.0
                    item.status = DownloadStatus.DOWNLOADING
                    item.status_text = "Switching to Universal Stream Engine..."
                    await self._broadcast_item(item, force=True)

                    final_path, final_size = await run_video_ytdlp_download(
                        item,
                        concurrent_fragments=settings.ytdlp_concurrent_fragments,
                        cookies_file=settings.cookies_file,
                        pause_event=ctrl.pause_event,
                        cancel_event=ctrl.cancel_event,
                        get_speed_limit_kbps=get_limit,
                        on_progress=on_progress,
                        on_status_change=on_status_change,
                    )
            else:
                final_path, final_size = await run_video_ytdlp_download(
                    item,
                    concurrent_fragments=settings.ytdlp_concurrent_fragments,
                    cookies_file=settings.cookies_file,
                    pause_event=ctrl.pause_event,
                    cancel_event=ctrl.cancel_event,
                    get_speed_limit_kbps=get_limit,
                    on_progress=on_progress,
                    on_status_change=on_status_change,
                )

            # Completed!
            item.filepath = final_path
            item.filename = os.path.basename(final_path)
            if final_size > 0:
                item.total_bytes = final_size
                item.downloaded_bytes = final_size
            item.progress = 100.0
            item.speed = 0.0
            item.eta = 0.0
            item.active_connections = 0
            item.status = DownloadStatus.COMPLETED
            item.status_text = "Completed"
            item.completed_at = time.time()
            for seg in item.segments:
                seg.status = "done"
                seg.speed = 0.0
                if seg.end >= seg.start:
                    seg.downloaded = seg.end - seg.start + 1
            await self._broadcast_item(item, force=True)

        except DownloadPausedError:
            item.status = DownloadStatus.PAUSED
            item.status_text = "Paused"
            item.speed = 0.0
            item.eta = None
            item.active_connections = 0
            for seg in item.segments:
                if seg.status == "active":
                    seg.status = "paused"
                    seg.speed = 0.0
            await self._broadcast_item(item, force=True)

        except DownloadCancelledError:
            if download_id in self.items:
                item.status = DownloadStatus.CANCELLED
                item.status_text = "Cancelled"
                item.speed = 0.0
                item.eta = None
                item.active_connections = 0
                await self._broadcast_item(item, force=True)

        except asyncio.CancelledError:
            if download_id in self.items and item.status not in (
                DownloadStatus.PAUSED,
                DownloadStatus.CANCELLED,
                DownloadStatus.COMPLETED,
            ):
                item.status = DownloadStatus.PAUSED
                item.status_text = "Paused"
                item.speed = 0.0
                item.eta = None
                item.active_connections = 0
                await self._broadcast_item(item, force=True)

        except Exception as exc:
            item.status = DownloadStatus.ERROR
            item.error_message = str(exc) or "Unexpected download error."
            item.status_text = f"Error: {item.error_message[:90]}"
            item.speed = 0.0
            item.eta = None
            item.active_connections = 0
            await self._broadcast_item(item, force=True)

        finally:
            self.controls.pop(download_id, None)
            await self._schedule_queue()

    def _apply_probe_to_item(
        self, item: DownloadItem, probe: ProbeResult, settings: AppSettings
    ) -> None:
        item.title = probe.title
        item.thumbnail = probe.thumbnail
        item.duration = probe.duration
        item.extractor = probe.extractor
        item.http_headers = dict(probe.http_headers or item.http_headers)

        if probe.kind == "direct":
            item.mode = DownloadMode.DIRECT
            item.url = probe.final_url
            item.final_url = probe.final_url
            item.supports_resume = probe.supports_range
            if probe.filesize:
                item.total_bytes = probe.filesize
            safe_name = sanitize_filename(probe.filename)
            category = probe.category
        else:
            # Choose matching format option based on requested quality/audio
            chosen = None
            if item.is_audio_only:
                for f in probe.formats:
                    if f.is_audio_only and f.ext == item.audio_format:
                        chosen = f
                        break
            elif probe.formats:
                chosen = probe.formats[0]
                if "1080" in item.format_id:
                    chosen = next((f for f in probe.formats if not f.is_audio_only and f.height <= 1080 and f.height > 0), chosen)
                elif "720" in item.format_id:
                    chosen = next((f for f in probe.formats if not f.is_audio_only and f.height <= 720 and f.height > 0), chosen)
                elif "480" in item.format_id:
                    chosen = next((f for f in probe.formats if not f.is_audio_only and f.height <= 480 and f.height > 0), chosen)

            if chosen:
                item.format_id = chosen.format_id
                item.format_label = chosen.label
                item.direct_video_url = chosen.direct_video_url
                item.direct_audio_url = chosen.direct_audio_url
                if chosen.filesize:
                    item.total_bytes = chosen.filesize
                if chosen.http_headers:
                    item.http_headers = dict(chosen.http_headers)
                ext = chosen.ext or ("mp3" if item.is_audio_only else "mp4")
            else:
                ext = item.audio_format if item.is_audio_only else "mp4"

            safe_name = sanitize_filename(probe.title, default_ext=f".{ext}")
            category = detect_category(safe_name, is_video_site=True, is_audio_only=item.is_audio_only)

            if probe.kind == "sniffed" and item.direct_video_url and not item.format_id.startswith("stream:"):
                item.mode = DownloadMode.DIRECT
                item.url = item.direct_video_url
                item.final_url = item.direct_video_url
            elif settings.enable_turbo_video_segments and item.direct_video_url and not item.is_audio_only:
                item.mode = DownloadMode.VIDEO_TURBO
            else:
                item.mode = DownloadMode.VIDEO_YTDLP

        final_dir = resolve_category_dir(item.save_dir, category, settings.auto_categorize_folders)
        item.save_dir = final_dir
        item.category = category
        item.filepath = get_unique_filepath(final_dir, safe_name)
        item.filename = os.path.basename(item.filepath)

    # ------------------------------------------------------------------
    # Controls: Pause / Resume / Retry / Delete / Batch Actions
    # ------------------------------------------------------------------
    async def pause(self, download_id: str) -> Optional[DownloadItem]:
        item = self.items.get(download_id)
        if not item:
            return None
        ctrl = self.controls.get(download_id)
        if ctrl:
            ctrl.pause_event.set()
            item.status_text = "Pausing..."
            await self._broadcast_item(item, force=True)
        elif item.status == DownloadStatus.QUEUED:
            item.status = DownloadStatus.PAUSED
            item.status_text = "Paused"
            await self._broadcast_item(item, force=True)
        return item

    async def resume(self, download_id: str) -> Optional[DownloadItem]:
        item = self.items.get(download_id)
        if not item:
            return None
        if item.status in (DownloadStatus.DOWNLOADING, DownloadStatus.PROBING, DownloadStatus.MERGING):
            return item
        if item.status == DownloadStatus.COMPLETED and item.filepath and os.path.exists(item.filepath):
            return item

        # If previously errored in turbo mode on expired link, refresh via yt-dlp on retry
        if item.status == DownloadStatus.ERROR and item.mode == DownloadMode.VIDEO_TURBO:
            item.mode = DownloadMode.VIDEO_YTDLP
            item.segments = []

        item.status = DownloadStatus.QUEUED
        item.status_text = "Queued to resume..."
        item.error_message = ""
        await self._broadcast_item(item, force=True)
        await self._schedule_queue()
        return item

    async def retry(self, download_id: str) -> Optional[DownloadItem]:
        item = self.items.get(download_id)
        if not item:
            return None
        # Reset partial state cleanly if retrying an error/cancelled item
        if item.status in (DownloadStatus.ERROR, DownloadStatus.CANCELLED):
            if item.mode == DownloadMode.VIDEO_TURBO:
                item.mode = DownloadMode.VIDEO_YTDLP
            item.error_message = ""
            if item.filepath and not os.path.exists(item.filepath + ".turbopart"):
                item.segments = []
                item.downloaded_bytes = 0
                item.progress = 0.0
        return await self.resume(download_id)

    async def pause_all(self) -> None:
        for dl_id, item in list(self.items.items()):
            if item.status in (
                DownloadStatus.DOWNLOADING,
                DownloadStatus.PROBING,
                DownloadStatus.QUEUED,
            ):
                await self.pause(dl_id)

    async def resume_all(self) -> None:
        for dl_id, item in list(self.items.items()):
            if item.status in (DownloadStatus.PAUSED, DownloadStatus.ERROR):
                await self.resume(dl_id)

    async def delete(self, download_id: str, delete_file: bool = False) -> bool:
        item = self.items.get(download_id)
        if not item:
            return False

        ctrl = self.controls.get(download_id)
        if ctrl:
            ctrl.cancel_event.set()
            if ctrl.task:
                try:
                    await asyncio.wait_for(asyncio.shield(ctrl.task), timeout=3.0)
                except Exception:
                    ctrl.task.cancel()

        # Remove partial files
        if item.filepath:
            safe_remove(item.filepath + ".turbopart")
            safe_remove(item.filepath + ".turbovideo")
            safe_remove(item.filepath + ".turbovideo.part")
            safe_remove(item.filepath + ".turboaudio")
            safe_remove(item.filepath + ".turboaudio.part")
            stem = os.path.splitext(os.path.basename(item.filepath))[0]
            if item.save_dir and stem:
                pattern = os.path.join(item.save_dir, f"{glob.escape(stem)}*")
                for p in glob.glob(pattern):
                    if p.endswith((".part", ".ytdl", ".temp", ".turbopart")) or ".part-Frag" in p:
                        safe_remove(p)
            if delete_file and os.path.exists(item.filepath):
                safe_remove(item.filepath)

        self.items.pop(download_id, None)
        self.db.delete(download_id)
        await self.broadcast({"type": "item_deleted", "id": download_id})
        await self._schedule_queue()
        return True

    async def clear_completed(self) -> list[str]:
        removed = []
        for dl_id, item in list(self.items.items()):
            if item.status in (DownloadStatus.COMPLETED, DownloadStatus.CANCELLED):
                self.items.pop(dl_id, None)
                self.db.delete(dl_id)
                removed.append(dl_id)
        if removed:
            await self.broadcast({"type": "items_cleared", "ids": removed})
        return removed

    # ------------------------------------------------------------------
    # OS Integration (Windows Explorer / Open File / Native Folder Picker)
    # ------------------------------------------------------------------
    def get_download_file(self, download_id: str) -> Optional[tuple[str, str]]:
        item = self.items.get(download_id)
        if not item or not item.filepath or not os.path.exists(item.filepath):
            return None
        filename = item.filename or os.path.basename(item.filepath)
        return (item.filepath, filename)

    def open_file(self, download_id: str) -> bool:
        item = self.items.get(download_id)
        if not item or not item.filepath or not os.path.exists(item.filepath):
            return False
        try:
            os.startfile(item.filepath)  # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    def reveal_in_folder(self, download_id: str) -> bool:
        item = self.items.get(download_id)
        if not item:
            return False
        if item.filepath and os.path.exists(item.filepath):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(item.filepath)])
            return True
        if item.save_dir and os.path.exists(item.save_dir):
            os.startfile(item.save_dir)  # type: ignore[attr-defined]
            return True
        return False

    def open_download_folder(self, path: Optional[str] = None) -> bool:
        folder = path or self.settings_store.get().download_dir
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    async def pick_folder_dialog(self, initial_dir: Optional[str] = None) -> Optional[str]:
        """Opens native Windows folder browser dialog safely on a worker thread."""
        start = initial_dir or self.settings_store.get().download_dir

        def _pick() -> Optional[str]:
            try:
                import tkinter as tk
                from tkinter import filedialog

                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                selected = filedialog.askdirectory(
                    title="Select Download Folder — TurboDM",
                    initialdir=start if os.path.exists(start) else None,
                )
                root.destroy()
                return selected or None
            except Exception:
                return None

        return await asyncio.to_thread(_pick)

    async def shutdown(self) -> None:
        await self.pause_all()
        tasks = [c.task for c in self.controls.values() if c.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.db.close()
