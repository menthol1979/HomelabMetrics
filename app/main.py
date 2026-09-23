"""
FastAPI app: serves the dashboard frontend, exposes history over REST,
and pushes each live poll to connected browsers over a WebSocket.
"""

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, poller

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("homelab_metrics.main")

STATIC_DIR = Path(__file__).parent / "static"


def _compute_asset_version() -> str:
    """Content hash of the frontend files that actually change between
    deploys. Used as a cache-busting ?v= query param on index.html's
    <script>/<link> tags so a rebuild always serves fresh JS/CSS instead
    of whatever the browser cached from before - this is what silently
    bit us more than once: Portainer/the browser reporting a successful
    "update" while still running the previous app.js underneath."""
    h = hashlib.sha256()
    for name in ("app.js", "style.css"):
        h.update((STATIC_DIR / name).read_bytes())
    return h.hexdigest()[:10]


ASSET_VERSION = _compute_asset_version()


class Broadcaster:
    """Fan-out of live metric dicts to every connected WebSocket client."""

    def __init__(self):
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, metric: dict) -> None:
        payload = json.dumps({"type": "metric", "data": metric})
        dead = []
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


broadcaster = Broadcaster()
_stop_event = asyncio.Event()
_background_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    interrupted = db.interrupt_stale_backup_events()
    if interrupted:
        logger.warning("marked %d stale in-progress backup event(s) as interrupted on startup", interrupted)

    _stop_event.clear()
    slow_cache = poller.SlowFieldsCache()
    for host_key in config.HOSTS:
        _background_tasks.append(
            asyncio.create_task(poller.run_slow_poller(host_key, slow_cache, _stop_event))
        )
    _background_tasks.append(
        asyncio.create_task(poller.run_fast_loop(slow_cache, broadcaster.broadcast, _stop_event))
    )
    _background_tasks.append(asyncio.create_task(poller.run_retention_sweeper(_stop_event)))
    logger.info(
        "started fast loop (mirror=%s, every %ss) + slow pollers (every %ss) for hosts: %s",
        config.MIRROR_URL, config.MIRROR_POLL_INTERVAL, config.SLOW_POLL_INTERVAL, list(config.HOSTS),
    )

    yield

    _stop_event.set()
    for task in _background_tasks:
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)


app = FastAPI(title="Homelab Metrics Dashboard", lifespan=lifespan)


@app.get("/api/hosts")
async def get_hosts():
    return [
        {
            "key": key,
            "display_name": cfg["display_name"],
            "is_backup_host": cfg["is_backup_host"],
            "has_fan": cfg["has_fan"],
        }
        for key, cfg in config.HOSTS.items()
    ]


@app.get("/api/metrics")
async def get_metrics(host: str | None = None, since: str | None = None, limit: int = 2000):
    return db.get_recent_metrics(host=host, since_ts=since, limit=limit)


@app.get("/api/metrics/latest")
async def get_latest_metric(host: str):
    return db.get_latest_metric(host)


@app.get("/api/backup-events")
async def get_backup_events(host: str | None = None, limit: int = 200):
    return db.get_backup_events(host=host, limit=limit)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await broadcaster.connect(ws)
    try:
        while True:
            # Frontend doesn't send anything meaningful; just keep the
            # connection open and detect disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.disconnect(ws)


# Static frontend last, so /api/* and /ws above take precedence.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    html = (STATIC_DIR / "index.html").read_text()
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={ASSET_VERSION}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={ASSET_VERSION}"')
    # The HTML shell itself must never be cached, or the browser can keep
    # serving an old version's ?v= links forever and never notice a
    # rebuild happened at all.
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
