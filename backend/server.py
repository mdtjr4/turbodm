from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.manager import DownloadManager
from backend.models import AppSettings, BatchDownloadRequest, StartDownloadRequest

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
    return manager.get_settings().model_dump()


@app.put("/api/settings")
async def api_update_settings(settings: AppSettings):
    updated = await manager.update_settings(settings)
    return updated.model_dump()


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
