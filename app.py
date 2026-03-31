import mimetypes
import os
import re
import threading
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

import modal

from terminal import add_terminal_routes

APP_NAME = "alex-server"
VOLUME_NAME = "vibe-media"
MOUNT_PATH = "/vol"
MEDIA_ROOT = Path(MOUNT_PATH).resolve()
IDLE_TIMEOUT_SECONDS = 600

image = (
    modal.Image.debian_slim()
    .apt_install(
        "ca-certificates",
        "curl",
        "ffmpeg",
        "unzip",
        "aria2",
        "sudo",
        "python3",
        "python3-pip",
        "git",
        "bubblewrap",
    )
    .run_commands(
        "pip3 install -U pip",
        "pip3 install fastapi uvicorn[standard]",
        "curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp",
        "chmod a+rx /usr/local/bin/yt-dlp",
    )
    .run_commands(
        "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -",
        "apt-get install -y nodejs",
        "npm install -g @openai/codex",
        "curl -fsSL https://bun.sh/install | bash",
    )
    .run_commands(
        "curl -fsSL https://cli.kiro.dev/install | bash",
        "ln -sf /root/.local/bin/kiro-cli /usr/local/bin/kiro-cli",
    )
    .add_local_file(
        os.path.join(os.path.dirname(__file__), "terminal.html"),
        "/app/terminal.html",
        copy=True,
    )
    .add_local_file(
        os.path.join(os.path.dirname(__file__), "terminal.py"),
        "/root/terminal.py",
        copy=True,
    )
)

app = modal.App(APP_NAME, image=image)
media_volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
tunnel_cache = modal.Dict.from_name(f"{APP_NAME}-tunnel-cache", create_if_missing=True)


def _resolve_path(raw_path: str) -> Path:
    if not raw_path:
        raw_path = "/"
    if not raw_path.startswith("/"):
        raw_path = f"/{raw_path}"
    rel = raw_path.lstrip("/")
    full = (MEDIA_ROOT / rel).resolve()
    if full != MEDIA_ROOT and MEDIA_ROOT not in full.parents:
        raise ValueError("Invalid path")
    return full


def _iter_file(
    path: Path,
    start: int,
    length: int,
    chunk_size: int = 1024 * 1024,
    touch=None,
) -> Iterator[bytes]:
    with path.open("rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            if touch:
                try:
                    touch()
                except Exception:
                    pass
            yield chunk


class _ActivityTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def touch(self) -> None:
        with self._lock:
            self._last = time.monotonic()

    def idle_for(self) -> float:
        with self._lock:
            return time.monotonic() - self._last


def create_api_app(activity: _ActivityTracker):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    api_app = FastAPI()

    @api_app.middleware("http")
    async def _touch_activity(request: Request, call_next):
        activity.touch()
        return await call_next(request)

    @api_app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @api_app.get("/favicon.ico")
    def favicon():
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' width='32' height='32' "
            "viewBox='0 0 24 24' fill='none' stroke='#ffffff' stroke-width='2' "
            "stroke-linecap='round' stroke-linejoin='round'>"
            "<polyline points='4 17 10 11 4 5'/>"
            "<line x1='12' x2='20' y1='19' y2='19'/>"
            "</svg>"
        )
        return StreamingResponse(iter([svg.encode("utf-8")]), media_type="image/svg+xml")

    @api_app.get("/list")
    def list_items(path: str = "/") -> JSONResponse:
        try:
            target = _resolve_path(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.exists():
            raise HTTPException(status_code=404, detail="Path not found")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="Path is not a directory")

        items = []
        for entry in sorted(
            target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())
        ):
            rel_path = "/" + entry.relative_to(MEDIA_ROOT).as_posix()
            items.append(
                {
                    "type": "folder" if entry.is_dir() else "file",
                    "name": entry.name,
                    "path": rel_path,
                    "thumb": "",
                    "size": entry.stat().st_size,
                }
            )
        return JSONResponse({"path": path, "items": items})

    @api_app.get("/stream")
    def stream_file(request: Request, path: str):
        try:
            target = _resolve_path(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        file_size = target.stat().st_size
        range_header = request.headers.get("range")
        mime_type, _ = mimetypes.guess_type(target.name)
        media_type = mime_type or "application/octet-stream"

        if range_header is None:
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
            }
            return StreamingResponse(
                _iter_file(target, 0, file_size, touch=activity.touch),
                media_type=media_type,
                headers=headers,
            )

        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if not match:
                raise HTTPException(status_code=416, detail="Invalid Range header")
            start_str, end_str = match.groups()
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1
            if start >= file_size:
                raise HTTPException(status_code=416, detail="Range out of bounds")
            end = min(end, file_size - 1)
            length = end - start + 1
            headers = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
            }
            return StreamingResponse(
                _iter_file(target, start, length, touch=activity.touch),
                status_code=206,
                media_type=media_type,
                headers=headers,
            )

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        }
        return StreamingResponse(
            _iter_file(target, 0, file_size, touch=activity.touch),
            media_type=media_type,
            headers=headers,
        )

    @api_app.get("/download-url")
    def download_url(request: Request, path: str):
        try:
            target = _resolve_path(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        base = str(request.base_url).rstrip("/")
        return {"url": f"{base}/stream?path={quote(path)}"}

    add_terminal_routes(api_app, touch=activity.touch)

    return api_app


def _is_tunnel_alive(url: str, timeout_s: float = 2.0) -> bool:
    try:
        from urllib.request import Request, urlopen

        health_url = url.rstrip("/") + "/health"
        req = Request(health_url, method="GET")
        with urlopen(req, timeout=timeout_s) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


@app.function(
    timeout=14400,
    max_containers=1,
    volumes={MOUNT_PATH: media_volume},
    env={"HOME": f"{MOUNT_PATH}/.home"},
)
def tunnel(q: modal.Queue):
    # Start a live tunnel to port 8000 and keep the FastAPI app running.
    os.makedirs(f"{MOUNT_PATH}/.home", exist_ok=True)
    os.makedirs(f"{MOUNT_PATH}/media", exist_ok=True)
    import uvicorn

    activity = _ActivityTracker()

    with modal.forward(8000) as tunnel:
        # Send the URL back to the caller immediately.
        q.put(tunnel.url)

        config = uvicorn.Config(
            create_api_app(activity),
            host="0.0.0.0",
            port=8000,
            log_level="info",
        )
        server = uvicorn.Server(config)

        def _serve():
            server.run()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()

        # Stop the server after a period of inactivity to save credits.
        while thread.is_alive():
            if activity.idle_for() > IDLE_TIMEOUT_SECONDS:
                server.should_exit = True
                break
            time.sleep(1.0)

        thread.join(timeout=5.0)


@app.function()
@modal.fastapi_endpoint(method="GET")
def start():
    # Web endpoint that starts a tunnel and returns its URL.
    cached_url = tunnel_cache.get("url")
    if cached_url and _is_tunnel_alive(cached_url):
        return {
            "url": cached_url,
            "terminal_url": f"{cached_url.rstrip('/')}/terminal",
            "cached": True,
        }

    with modal.Queue.ephemeral() as q:
        tunnel.spawn(q)
        url = q.get(timeout=30)
        tunnel_cache["url"] = url
        return {
            "url": url,
            "terminal_url": f"{url.rstrip('/')}/terminal",
            "cached": False,
        }
