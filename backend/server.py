from __future__ import annotations

import asyncio
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import aiohttp
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.config import CLOUD_MODE, DEFAULT_USER_AGENT, get_ffmpeg_path
from backend.manager import DownloadManager
from backend.models import AppSettings, BatchDownloadRequest, StartDownloadRequest
from backend.utils import sanitize_filename

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

manager = DownloadManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await manager.shutdown()


app = FastAPI(title="TurboDM — High-Speed Download Manager", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProbeRequest(BaseModel):
    url: str


class FolderRequest(BaseModel):
    initial_dir: Optional[str] = None


class StreamPrepareRequest(BaseModel):
    url: str
    filename: str = "download.mp4"
    kind: str = "direct"
    direct_video_url: Optional[str] = None
    direct_audio_url: Optional[str] = None
    is_audio_only: bool = False
    audio_format: str = "mp3"
    http_headers: dict[str, str] = {}


_STREAM_TOKENS: dict[str, dict[str, Any]] = {}


def _cleanup_stream_tokens() -> None:
    now = time.time()
    expired = [k for k, v in _STREAM_TOKENS.items() if now - v.get("created_at", 0) > 600]
    for k in expired:
        _STREAM_TOKENS.pop(k, None)


def _content_disposition_header(filename: str) -> str:
    safe = sanitize_filename(filename or "download")
    ascii_fallback = safe.encode("ascii", "ignore").decode("ascii").strip() or "download"
    ascii_fallback = ascii_fallback.replace('"', "")
    utf8_quoted = urllib.parse.quote(safe, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{utf8_quoted}"


def _build_ffmpeg_headers_arg(headers: dict[str, str]) -> str:
    merged = {"User-Agent": DEFAULT_USER_AGENT}
    for k, v in (headers or {}).items():
        if k and v and k.lower() not in ("host", "accept-encoding", "content-length"):
            merged[k] = str(v)
    return "".join(f"{k}: {v}\r\n" for k, v in merged.items())


# ------------------------------------------------------------------
# REST API
# ------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {"status": "ok", "app": "TurboDM", "system": manager.get_system_info()}


@app.post("/api/probe")
async def api_probe(req: ProbeRequest):
    try:
        result = await manager.analyze_url(req.url)
        return result.model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}")


@app.get("/api/downloads")
async def api_list_downloads():
    return {
        "items": [i.model_dump() for i in manager.list_items()],
        "system": manager.get_system_info(),
    }


@app.post("/api/downloads")
async def api_add_download(req: StartDownloadRequest):
    try:
        item = await manager.add_download(req)
        return item.model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/downloads/batch")
async def api_add_batch(req: BatchDownloadRequest):
    items = await manager.add_batch(req)
    if not items:
        raise HTTPException(status_code=400, detail="No valid HTTP/HTTPS URLs provided.")
    return {"count": len(items), "items": [i.model_dump() for i in items]}


@app.post("/api/downloads/{download_id}/pause")
async def api_pause(download_id: str):
    item = await manager.pause(download_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download not found.")
    return item.model_dump()


@app.post("/api/downloads/{download_id}/resume")
async def api_resume(download_id: str):
    item = await manager.resume(download_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download not found.")
    return item.model_dump()


@app.post("/api/downloads/{download_id}/retry")
async def api_retry(download_id: str):
    item = await manager.retry(download_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download not found.")
    return item.model_dump()


@app.delete("/api/downloads/{download_id}")
async def api_delete(download_id: str, delete_file: bool = Query(default=False)):
    ok = await manager.delete(download_id, delete_file=delete_file)
    if not ok:
        raise HTTPException(status_code=404, detail="Download not found.")
    return {"deleted": True, "id": download_id}


@app.post("/api/downloads/actions/pause-all")
async def api_pause_all():
    await manager.pause_all()
    return {"ok": True}


@app.post("/api/downloads/actions/resume-all")
async def api_resume_all():
    await manager.resume_all()
    return {"ok": True}


@app.post("/api/downloads/actions/clear-completed")
async def api_clear_completed():
    ids = await manager.clear_completed()
    return {"cleared": ids}


@app.post("/api/stream/prepare")
async def api_stream_prepare(req: StreamPrepareRequest):
    _cleanup_stream_tokens()
    token = uuid.uuid4().hex
    safe_name = sanitize_filename(req.filename or "download")
    _STREAM_TOKENS[token] = {
        "created_at": time.time(),
        "url": req.url,
        "filename": safe_name,
        "kind": req.kind,
        "direct_video_url": req.direct_video_url,
        "direct_audio_url": req.direct_audio_url,
        "is_audio_only": req.is_audio_only,
        "audio_format": req.audio_format or "mp3",
        "http_headers": req.http_headers or {},
    }
    return {"stream_url": f"/api/stream/{token}", "filename": safe_name}


@app.get("/api/stream/{token}")
async def api_stream_download(token: str):
    _cleanup_stream_tokens()
    spec = _STREAM_TOKENS.get(token)
    if not spec:
        raise HTTPException(status_code=404, detail="Stream link expired. Please click Check Link again.")

    filename = spec["filename"]
    video_url = spec.get("direct_video_url")
    audio_url = spec.get("direct_audio_url")
    is_audio_only = bool(spec.get("is_audio_only"))
    audio_format = (spec.get("audio_format") or "mp3").lower()
    raw_headers = spec.get("http_headers") or {}
    disp_header = _content_disposition_header(filename)

    # Case 1: Dual-stream HD/2K/4K Video + Audio -> Live FFmpeg mux straight to browser (ZERO server wait!)
    if not is_audio_only and video_url and audio_url:
        ffmpeg_path = get_ffmpeg_path()
        if not ffmpeg_path:
            raise HTTPException(status_code=500, detail="FFmpeg not available on server.")
        hdr_arg = _build_ffmpeg_headers_arg(raw_headers)
        cmd = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-headers",
            hdr_arg,
            "-i",
            video_url,
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-headers",
            hdr_arg,
            "-i",
            audio_url,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]

        async def ffmpeg_mux_generator():
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                assert proc.stdout is not None
                while True:
                    chunk = await proc.stdout.read(131072)
                    if not chunk:
                        break
                    yield chunk
            finally:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        await proc.wait()
                    except Exception:
                        pass

        return StreamingResponse(
            ffmpeg_mux_generator(),
            media_type="video/mp4",
            headers={"Content-Disposition": disp_header},
        )

    # Case 2: Audio-only MP3 transcoding live via FFmpeg
    src_url = audio_url or video_url or spec["url"]
    if is_audio_only and audio_format == "mp3":
        ffmpeg_path = get_ffmpeg_path()
        if ffmpeg_path:
            hdr_arg = _build_ffmpeg_headers_arg(raw_headers)
            cmd = [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
                "-headers",
                hdr_arg,
                "-i",
                src_url,
                "-vn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                "-f",
                "mp3",
                "pipe:1",
            ]

            async def ffmpeg_mp3_generator():
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    assert proc.stdout is not None
                    while True:
                        chunk = await proc.stdout.read(65536)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    if proc.returncode is None:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        try:
                            await proc.wait()
                        except Exception:
                            pass

            return StreamingResponse(
                ffmpeg_mp3_generator(),
                media_type="audio/mpeg",
                headers={"Content-Disposition": disp_header},
            )

    # Case 3: Single direct stream (Direct files, Facebook, TikTok, single-stream MP4/M4A) -> Live proxy!
    req_headers = {"User-Agent": DEFAULT_USER_AGENT}
    for k, v in raw_headers.items():
        if k and v and k.lower() not in ("host", "accept-encoding", "content-length"):
            req_headers[k] = str(v)

    timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=60)
    connector = aiohttp.TCPConnector(ssl=False)
    session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    try:
        upstream = await session.get(src_url, headers=req_headers, allow_redirects=True)
        if upstream.status >= 400:
            upstream.close()
            await session.close()
            raise HTTPException(status_code=502, detail=f"Source server returned HTTP {upstream.status}.")
    except HTTPException:
        raise
    except Exception as exc:
        await session.close()
        raise HTTPException(status_code=502, detail=f"Failed to connect to source: {exc}")

    resp_headers = {"Content-Disposition": disp_header}
    if upstream.headers.get("Content-Length"):
        resp_headers["Content-Length"] = upstream.headers["Content-Length"]
    content_type = upstream.headers.get("Content-Type") or "application/octet-stream"

    async def direct_proxy_generator():
        try:
            async for chunk in upstream.content.iter_chunked(131072):
                if chunk:
                    yield chunk
        finally:
            upstream.close()
            await session.close()

    return StreamingResponse(
        direct_proxy_generator(),
        media_type=content_type,
        headers=resp_headers,
    )


@app.get("/api/downloads/{download_id}/file")
async def api_download_file(download_id: str):
    file_info = manager.get_download_file(download_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found on server.")
    filepath, filename = file_info
    return FileResponse(
        path=filepath,
        filename=filename,
        media_type="application/octet-stream",
    )


@app.post("/api/downloads/{download_id}/open")
async def api_open_file(download_id: str):
    ok = manager.open_file(download_id)
    if not ok:
        raise HTTPException(status_code=404, detail="File not found on disk.")
    return {"ok": True}


@app.post("/api/downloads/{download_id}/reveal")
async def api_reveal_file(download_id: str):
    ok = manager.reveal_in_folder(download_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Folder not found.")
    return {"ok": True}


@app.post("/api/system/open-folder")
async def api_open_default_folder(req: Optional[FolderRequest] = None):
    ok = manager.open_download_folder(req.initial_dir if req else None)
    return {"ok": ok}


@app.post("/api/system/browse-folder")
async def api_browse_folder(req: Optional[FolderRequest] = None):
    selected = await manager.pick_folder_dialog(req.initial_dir if req else None)
    return {"path": selected}


@app.get("/api/settings")
async def api_get_settings():
    data = manager.get_settings().model_dump()
    if CLOUD_MODE:
        data["cookies_file"] = ""
    return data


@app.put("/api/settings")
async def api_update_settings(settings: AppSettings):
    if CLOUD_MODE:
        current = manager.get_settings()
        settings.download_dir = current.download_dir
        settings.cookies_file = current.cookies_file
    updated = await manager.update_settings(settings)
    data = updated.model_dump()
    if CLOUD_MODE:
        data["cookies_file"] = ""
    return data


# ------------------------------------------------------------------
# WebSocket Live Stream
# ------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.register_ws(ws)
    try:
        while True:
            # Keep connection alive; accept optional ping messages
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_json({"type": "pong", "system": manager.get_system_info()})
    except WebSocketDisconnect:
        manager.unregister_ws(ws)
    except Exception:
        manager.unregister_ws(ws)


# ------------------------------------------------------------------
# Static Frontend
# ------------------------------------------------------------------
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))
