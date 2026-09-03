"""HTTP app: serves each selected RTSP stream as its own MJPEG URL plus a dashboard."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .config import Registry, Stream
from .worker import WorkerPool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

BOUNDARY = "rtsphubframe"
TEMPLATE = (Path(__file__).parent / "templates" / "index.html").read_text()

registry = Registry()
pool = WorkerPool()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await pool.shutdown()


app = FastAPI(title="rtsp-hub", lifespan=lifespan)


class StreamIn(BaseModel):
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    enabled: bool = True
    width: int = Field(default=640, ge=64, le=3840)
    fps: int = Field(default=10, ge=1, le=60)
    quality: int = Field(default=5, ge=2, le=31)
    transport: str = "tcp"


class StreamPatch(BaseModel):
    name: str | None = None
    url: str | None = None
    enabled: bool | None = None
    width: int | None = Field(default=None, ge=64, le=3840)
    fps: int | None = Field(default=None, ge=1, le=60)
    quality: int | None = Field(default=None, ge=2, le=31)
    transport: str | None = None


def _require(stream_id: str, must_be_enabled: bool = False) -> Stream:
    stream = registry.get(stream_id)
    if stream is None:
        raise HTTPException(404, f"unknown stream '{stream_id}'")
    if must_be_enabled and not stream.enabled:
        raise HTTPException(409, f"stream '{stream_id}' is disabled")
    return stream


def _describe(stream: Stream, base: str) -> dict:
    worker = pool.peek(stream.id)
    return {
        "id": stream.id,
        "name": stream.name,
        "url": stream.redacted_url,
        "enabled": stream.enabled,
        "width": stream.width,
        "fps": stream.fps,
        "quality": stream.quality,
        "transport": stream.transport,
        "mjpeg_url": f"{base}/streams/{stream.id}/mjpeg",
        "snapshot_url": f"{base}/streams/{stream.id}/snapshot.jpg",
        "status": worker.status() if worker else {"running": False, "clients": 0, "live": False},
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return TEMPLATE


@app.get("/api/streams")
async def list_streams(request: Request) -> JSONResponse:
    base = str(request.base_url).rstrip("/")
    return JSONResponse([_describe(s, base) for s in registry.list()])


@app.post("/api/streams", status_code=201)
async def create_stream(payload: StreamIn, request: Request) -> JSONResponse:
    stream = registry.add(**payload.model_dump())
    base = str(request.base_url).rstrip("/")
    return JSONResponse(_describe(stream, base), status_code=201)


@app.patch("/api/streams/{stream_id}")
async def patch_stream(stream_id: str, payload: StreamPatch, request: Request) -> JSONResponse:
    _require(stream_id)
    stream = registry.update(stream_id, **payload.model_dump(exclude_unset=True))
    assert stream is not None
    await pool.drop(stream_id)  # restart with the new settings on next request
    base = str(request.base_url).rstrip("/")
    return JSONResponse(_describe(stream, base))


@app.delete("/api/streams/{stream_id}", status_code=204)
async def delete_stream(stream_id: str) -> Response:
    _require(stream_id)
    registry.remove(stream_id)
    await pool.drop(stream_id)
    return Response(status_code=204)


@app.get("/streams/{stream_id}/snapshot.jpg")
async def snapshot(stream_id: str) -> Response:
    stream = _require(stream_id, must_be_enabled=True)
    worker = pool.get(stream)
    frame = await worker.snapshot(timeout=8.0)
    if frame is None:
        raise HTTPException(504, worker.error or "no frame received from source")
    return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/streams/{stream_id}/mjpeg")
async def mjpeg(stream_id: str, request: Request) -> StreamingResponse:
    stream = _require(stream_id, must_be_enabled=True)
    worker = pool.get(stream)
    sub = worker.subscribe()

    async def frames():
        stalled = 0.0
        try:
            while not await request.is_disconnected():
                try:
                    frame = await sub.get(timeout=1.0)
                except asyncio.TimeoutError:
                    stalled += 1.0
                    if stalled >= 30.0:
                        break
                    continue
                stalled = 0.0
                yield (
                    f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode() + frame + b"\r\n"
        finally:
            worker.unsubscribe(sub)

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={"Cache-Control": "no-store", "Connection": "close"},
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "streams": len(registry.list())}
