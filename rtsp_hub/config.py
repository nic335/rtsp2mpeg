"""Persistent stream registry backed by a JSON file."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("RTSP_HUB_CONFIG", Path.home() / ".config" / "rtsp-hub" / "streams.json")
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


@dataclass
class Stream:
    id: str
    name: str
    url: str
    enabled: bool = True
    width: int = 640
    fps: int = 10
    quality: int = 5
    transport: str = "tcp"

    @property
    def redacted_url(self) -> str:
        return re.sub(r"//[^/@]+@", "//***@", self.url)


@dataclass
class Config:
    streams: list[Stream] = field(default_factory=list)


class Registry:
    """Thread-safe, file-backed collection of streams."""

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._streams: dict[str, Stream] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._streams = {}
                return
            raw = json.loads(self.path.read_text() or "{}")
            self._streams = {}
            for item in raw.get("streams", []):
                stream = Stream(**item)
                self._streams[stream.id] = stream

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"streams": [asdict(s) for s in self._streams.values()]}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self.path)

    def list(self) -> list[Stream]:
        with self._lock:
            return list(self._streams.values())

    def get(self, stream_id: str) -> Stream | None:
        with self._lock:
            return self._streams.get(stream_id)

    def add(self, **kwargs) -> Stream:
        with self._lock:
            base = slugify(kwargs.get("name") or kwargs["url"])
            stream_id = base
            suffix = 2
            while stream_id in self._streams:
                stream_id = f"{base}-{suffix}"
                suffix += 1
            stream = Stream(id=stream_id, **kwargs)
            self._streams[stream.id] = stream
            self.save()
            return stream

    def update(self, stream_id: str, **kwargs) -> Stream | None:
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None:
                return None
            for key, value in kwargs.items():
                if value is not None and hasattr(stream, key):
                    setattr(stream, key, value)
            self.save()
            return stream

    def remove(self, stream_id: str) -> bool:
        with self._lock:
            if self._streams.pop(stream_id, None) is None:
                return False
            self.save()
            return True
