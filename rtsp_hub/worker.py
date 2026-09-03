"""FFmpeg-backed RTSP -> MJPEG workers with lazy start and multi-client fanout."""
from __future__ import annotations

import asyncio
import logging
import os
import time

from .config import Stream

log = logging.getLogger("rtsp_hub.worker")

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
IDLE_TIMEOUT = 15.0
RESTART_DELAY = 2.0
MAX_RESTART_DELAY = 30.0
READ_CHUNK = 65536
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")


def ffmpeg_command(stream: Stream) -> list[str]:
    return [
        FFMPEG,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-rtsp_transport", stream.transport,
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-i", stream.resolved_url,
        "-an",
        "-vf", f"fps={stream.fps},scale={stream.width}:-2",
        "-q:v", str(stream.quality),
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]


class Subscriber:
    """A single HTTP client waiting for the newest frame."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._frame: bytes | None = None

    def push(self, frame: bytes) -> None:
        self._frame = frame
        self._event.set()

    async def get(self, timeout: float = 30.0) -> bytes:
        await asyncio.wait_for(self._event.wait(), timeout)
        self._event.clear()
        assert self._frame is not None
        return self._frame


class StreamWorker:
    """Owns one ffmpeg process per stream and fans its frames out to subscribers."""

    def __init__(self, stream: Stream) -> None:
        self.stream = stream
        self._subscribers: set[Subscriber] = set()
        self._task: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None
        self.last_frame: bytes | None = None
        self.last_frame_at: float = 0.0
        self.frames_served: int = 0
        self.error: str | None = None
        self._activity = time.monotonic()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def clients(self) -> int:
        return len(self._subscribers)

    def status(self) -> dict:
        age = time.monotonic() - self.last_frame_at if self.last_frame_at else None
        return {
            "running": self.running,
            "clients": self.clients,
            "frames": self.frames_served,
            "live": age is not None and age < 5.0,
            "last_frame_age": round(age, 2) if age is not None else None,
            "error": self.error,
        }

    def touch(self) -> None:
        self._activity = time.monotonic()
        self.ensure_started()

    def ensure_started(self) -> None:
        if not self.running:
            self.error = None
            self._task = asyncio.create_task(self._run(), name=f"worker-{self.stream.id}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        if self.last_frame is not None:
            sub.push(self.last_frame)
        self._subscribers.add(sub)
        self.touch()
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)
        self._activity = time.monotonic()

    async def snapshot(self, timeout: float = 15.0) -> bytes | None:
        sub = self.subscribe()
        try:
            return await sub.get(timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.unsubscribe(sub)

    def _publish(self, frame: bytes) -> None:
        self.last_frame = frame
        self.last_frame_at = time.monotonic()
        self.frames_served += 1
        for sub in list(self._subscribers):
            sub.push(frame)

    async def _run(self) -> None:
        delay = RESTART_DELAY
        try:
            while True:
                frames_before = self.frames_served
                await self._run_ffmpeg_once()
                if self._idle():
                    return
                delay = RESTART_DELAY if self.frames_served > frames_before else min(delay * 2, MAX_RESTART_DELAY)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            await self._terminate()
            raise

    def _idle(self) -> bool:
        return not self._subscribers and (time.monotonic() - self._activity) > IDLE_TIMEOUT

    async def _terminate(self) -> None:
        proc, self._process = self._process, None
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()

    async def _run_ffmpeg_once(self) -> None:
        log.info("starting ffmpeg for %s (%s)", self.stream.id, self.stream.redacted_url)
        try:
            proc = await asyncio.create_subprocess_exec(
                *ffmpeg_command(self.stream),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            self.error = (
                f"'{FFMPEG}' not found — install ffmpeg and put it on PATH, "
                "or set the FFMPEG environment variable to its full path"
            )
            log.error("%s: %s", self.stream.id, self.error)
            return
        self._process = proc
        try:
            await self._pump_frames(proc)
        finally:
            stderr = b""
            if proc.stderr is not None:
                try:
                    stderr = await asyncio.wait_for(proc.stderr.read(), 1)
                except asyncio.TimeoutError:
                    pass
            await self._terminate()
            if stderr:
                self.error = stderr.decode(errors="replace").strip()[-500:]
                log.warning("ffmpeg %s: %s", self.stream.id, self.error)

    async def _pump_frames(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        buf = bytearray()
        while True:
            if self._idle():
                return
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(READ_CHUNK), IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                continue
            if not chunk:
                return
            buf.extend(chunk)
            while True:
                start = buf.find(SOI)
                if start < 0:
                    buf.clear()
                    break
                end = buf.find(EOI, start + 2)
                if end < 0:
                    del buf[:start]
                    break
                frame = bytes(buf[start:end + 2])
                del buf[:end + 2]
                self._publish(frame)


class WorkerPool:
    def __init__(self) -> None:
        self._workers: dict[str, StreamWorker] = {}

    def get(self, stream: Stream) -> StreamWorker:
        worker = self._workers.get(stream.id)
        if worker is None:
            worker = StreamWorker(stream)
            self._workers[stream.id] = worker
        return worker

    def peek(self, stream_id: str) -> StreamWorker | None:
        return self._workers.get(stream_id)

    async def drop(self, stream_id: str) -> None:
        worker = self._workers.pop(stream_id, None)
        if worker:
            await worker.stop()

    async def shutdown(self) -> None:
        for stream_id in list(self._workers):
            await self.drop(stream_id)
