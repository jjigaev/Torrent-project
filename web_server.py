import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from ipaddress import ip_address
import logging
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer
from fastapi.staticfiles import StaticFiles

from src.bencode import BencodeDecoder
from src.downloader import Downloader
from src.paths import safe_join, validate_component
from src.peer import PeerConnection
from src.piece_manager import PieceManager
from src.torrent import TorrentFile
from src.tracker import TrackerClient

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"
TORRENTS_DIR = BASE_DIR / "torrents"
DOWNLOAD_DIR = BASE_DIR / "downloads"
MAX_UPLOAD_SIZE = BencodeDecoder.MAX_SIZE
MAX_REQUEST_SIZE = MAX_UPLOAD_SIZE + 65536
# A healthy peer can delay availability or unchoking through multiple cycles.
PEER_READY_TIMEOUT = 120
PEER_IDLE_TIMEOUT = 120
SESSION_TOKEN = secrets.token_urlsafe(32)
SESSION_COOKIE = "minitorrent_session"
logger = logging.getLogger(__name__)
active_torrents = {}


class ConnectionManager:
    def __init__(self):
        self.active_connections = set()

    async def connect(self, websocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket):
        self.active_connections.discard(websocket)

    async def broadcast(self, message):
        async def send(connection):
            try:
                await asyncio.wait_for(connection.send_json(message), timeout=2)
            except Exception:
                self.disconnect(connection)

        await asyncio.gather(*(send(connection) for connection in tuple(self.active_connections)))


manager = ConnectionManager()


def _close_connections(data):
    with data["connections_lock"]:
        peers = tuple(data["connections"])
    for peer in peers:
        peer.close()


async def _stop_download(data):
    data["stop_event"].set()
    await asyncio.to_thread(_close_connections, data)
    task = data.get("task")
    if task is not None and not task.done():
        # A thread cannot be cancelled safely; wait until it observes the stop
        # event and releases its sockets before allowing restart or deletion.
        await asyncio.shield(task)


async def _dispose_download(data):
    await _stop_download(data)
    if data["piece_manager"] is not None:
        await asyncio.to_thread(data["piece_manager"].close)


@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        await asyncio.gather(*(_dispose_download(data) for data in tuple(active_torrents.values())))


app = FastAPI(title="MiniTorrentAPI", lifespan=lifespan)
api = APIRouter(prefix="/api", dependencies=[Depends(HTTPBearer())])


class UploadLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_REQUEST_SIZE:
                    raise HTTPException(status_code=413, detail="Torrent upload is too large")
            return message

        limited = scope["type"] == "http" and scope["path"] == "/api/torrents/add"
        await self.app(scope, limited_receive if limited else receive, send)


app.add_middleware(UploadLimitMiddleware)


def _is_local(connection):
    try:
        return (connection.url.hostname in {"localhost", "127.0.0.1", "::1"}
                and connection.client is not None
                and ip_address(connection.client.host).is_loopback)
    except ValueError:
        return False


def _same_origin(connection):
    origin = connection.headers.get("origin")
    if origin is None:
        return True
    try:
        supplied = urlsplit(origin)
        scheme = {"ws": "http", "wss": "https"}.get(connection.url.scheme, connection.url.scheme)
        expected_port = connection.url.port or (443 if scheme == "https" else 80)
        supplied_port = supplied.port or (443 if supplied.scheme == "https" else 80)
        return (supplied.scheme == scheme and supplied.hostname == connection.url.hostname
                and supplied_port == expected_port and not supplied.username
                and not supplied.password and not supplied.path and not supplied.query and not supplied.fragment)
    except ValueError:
        return False


@app.middleware("http")
async def protect_local_session(request: Request, call_next):
    if not _is_local(request) or not _same_origin(request):
        return JSONResponse({"detail": "Local same-origin access required"}, status_code=403)
    if request.url.path.startswith("/api/"):
        supplied = request.headers.get("authorization", "")
        scheme, _, token = supplied.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token.encode(), SESSION_TOKEN.encode()):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
    if request.url.path == "/api/torrents/add":
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdigit():
                return JSONResponse({"detail": "Invalid content length"}, status_code=400)
            if len(length) > 20 or int(length) > MAX_REQUEST_SIZE:
                return JSONResponse({"detail": "Torrent upload is too large"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
async def read_root(request: Request):
    html = await asyncio.to_thread((FRONTEND_DIR / "index.html").read_text, encoding="utf-8")
    response = HTMLResponse(html.replace("__MINITORRENT_API_TOKEN__", SESSION_TOKEN),
                            headers={"Cache-Control": "no-store"})
    response.set_cookie(SESSION_COOKIE, SESSION_TOKEN, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https")
    return response


def _public_data(info_hash, data):
    fields = ("name", "total_size", "piece_count", "progress", "download_speed",
              "upload_speed", "peers_connected", "downloaded_pieces", "status")
    return {"info_hash": info_hash, **{key: data[key] for key in fields}}


@api.get("/torrents")
async def get_torrents():
    return {"torrents": [_public_data(key, data) for key, data in active_torrents.items()]}


def _store_upload(torrent, content):
    TORRENTS_DIR.mkdir(parents=True, exist_ok=True)
    destination = safe_join(TORRENTS_DIR, f"{torrent.info_hash.hex()}.torrent")
    try:
        with destination.open("xb") as output:
            output.write(content)
    except FileExistsError:
        pass


@api.post("/torrents/add")
async def add_torrent(file: UploadFile = File(...)):
    try:
        filename = validate_component(file.filename)
        if not filename.lower().endswith(".torrent"):
            raise ValueError("A .torrent file is required")
        content = await file.read(MAX_UPLOAD_SIZE + 1)
        if len(content) > MAX_UPLOAD_SIZE:
            raise HTTPException(status_code=413, detail="Torrent upload is too large")
        torrent = await asyncio.to_thread(TorrentFile.from_bytes, content)
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400, detail="Invalid torrent file") from None
    finally:
        await file.close()

    info_hash = torrent.info_hash.hex()
    if info_hash not in active_torrents:
        try:
            await asyncio.to_thread(_store_upload, torrent, content)
        except (OSError, ValueError):
            raise HTTPException(status_code=500, detail="Unable to store torrent") from None
        # Recheck after disk I/O: another upload may have registered this hash.
        if info_hash not in active_torrents:
            active_torrents[info_hash] = {
                "name": torrent.name, "total_size": torrent.total_size,
                "piece_count": len(torrent.pieces), "tracker": torrent.announce,
                "status": "paused", "progress": 0, "download_speed": 0,
                "upload_speed": 0, "peers_connected": 0, "downloaded_pieces": 0,
                "torrent": torrent, "piece_manager": None, "task": None,
                "stop_event": threading.Event(), "lock": asyncio.Lock(),
                "connections": set(), "connections_lock": threading.Lock(),
            }
            await manager.broadcast({"type": "torrent_added", "torrent": {
                "info_hash": info_hash, "name": torrent.name}})
    return {"success": True, "info_hash": info_hash, "name": torrent.name}


def _get_torrent(info_hash):
    data = active_torrents.get(info_hash)
    if data is None:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return data


def _check_current(info_hash, data):
    if active_torrents.get(info_hash) is not data:
        raise HTTPException(status_code=404, detail="Torrent not found")


@api.post("/torrents/{info_hash}/start")
async def start_torrent(info_hash: str):
    data = _get_torrent(info_hash)
    async with data["lock"]:
        _check_current(info_hash, data)
        task = data["task"]
        if data["status"] != "completed" and (task is None or task.done()):
            data["stop_event"] = threading.Event()
            data["status"] = "downloading"
            data["task"] = asyncio.create_task(download_torrent_threading(info_hash))
            await manager.broadcast({"type": "status_update", **_public_data(info_hash, data)})
    return {"success": True, "status": data["status"]}


@api.post("/torrents/{info_hash}/pause")
async def pause_torrent(info_hash: str):
    data = _get_torrent(info_hash)
    async with data["lock"]:
        _check_current(info_hash, data)
        if data["status"] != "completed":
            data["status"] = "paused"
        await _stop_download(data)
        await manager.broadcast({"type": "status_update", **_public_data(info_hash, data)})
    return {"success": True, "status": data["status"]}


@api.delete("/torrents/{info_hash}")
async def delete_torrent(info_hash: str):
    data = _get_torrent(info_hash)
    async with data["lock"]:
        _check_current(info_hash, data)
        data["status"] = "paused"
        await _dispose_download(data)
        del active_torrents[info_hash]
        await manager.broadcast({"type": "torrent_removed", "info_hash": info_hash})
    return {"success": True}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.cookies.get(SESSION_COOKIE, "")
    if (not _is_local(websocket) or not _same_origin(websocket)
            or websocket.headers.get("origin") is None
            or not secrets.compare_digest(token.encode(), SESSION_TOKEN.encode())):
        await websocket.close(code=1008)
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


def try_connect_peer_sync(ip, port, info_hash, peer_id, data, cancel_event=None):
    peer = PeerConnection(ip, port, info_hash, peer_id, data["piece_count"])
    ready = False

    def stopped():
        return data["stop_event"].is_set() or (cancel_event is not None and cancel_event.is_set())

    with data["connections_lock"]:
        data["connections"].add(peer)
    try:
        if stopped() or not peer.connect(timeout=1) or stopped():
            return None
        peer.handshake()
        peer.send_bitfield(data["piece_manager"].get_bitfield())
        peer.send_interested()
        deadline = time.monotonic() + PEER_READY_TIMEOUT
        while not stopped():
            if not peer.peer_choking and peer.peer_pieces:
                ready = True
                return peer
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            peer.receive_message(timeout=min(1, remaining))
        return None
    except (OSError, ValueError):
        return None
    finally:
        if stopped() or not ready:
            peer.close()
            with data["connections_lock"]:
                data["connections"].discard(peer)


def _download_sync(data):
    stop = data["stop_event"]
    if stop.is_set():
        return False
    torrent = data["torrent"]
    if data["piece_manager"] is None:
        data["piece_manager"] = PieceManager(torrent, download_dir=DOWNLOAD_DIR)
    pieces = data["piece_manager"]
    try:
        if pieces.get_progress()["percentage"] < 100:
            tracker = TrackerClient(torrent)
            peers_list = tracker.get_peers()
            if stop.is_set():
                return False
            downloader = Downloader(torrent, [], piece_manager=pieces, stop_event=stop)
            negotiation_stop = threading.Event()

            def worker(peer):
                idle_deadline = time.monotonic() + PEER_IDLE_TIMEOUT
                try:
                    while not stop.is_set():
                        if pieces.get_progress()["percentage"] >= 100:
                            break
                        try:
                            interested = pieces.needs_pieces_from(peer)
                            if interested and not peer.am_interested:
                                peer.send_interested()
                            elif not interested and peer.am_interested:
                                peer.send_not_interested()
                        except OSError:
                            break
                        reservation = None if peer.peer_choking else pieces.reserve_next_piece(peer)
                        if reservation is None:
                            if pieces.has_pending():
                                idle_deadline = time.monotonic() + PEER_IDLE_TIMEOUT
                            remaining = idle_deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            # Availability can arrive after UNCHOKE or after
                            # completing a piece. Keep processing HAVE/BITFIELD
                            # instead of dropping an otherwise healthy peer.
                            try:
                                peer.receive_message(timeout=min(1, remaining))
                            except (OSError, ValueError):
                                break
                            continue
                        if not downloader.download_piece(peer, *reservation):
                            break
                        idle_deadline = time.monotonic() + PEER_IDLE_TIMEOUT
                finally:
                    peer.close()
                    downloader.remove_peer(peer)
                    with data["connections_lock"]:
                        data["connections"].discard(peer)
                        data["peers_connected"] -= 1
                    if pieces.get_progress()["percentage"] >= 100:
                        negotiation_stop.set()
                        _close_connections(data)

            with ThreadPoolExecutor(max_workers=20) as connecting, ThreadPoolExecutor(max_workers=20) as workers:
                connections = [connecting.submit(try_connect_peer_sync, ip, port, torrent.info_hash,
                                                  tracker.peer_id, data, negotiation_stop)
                               for ip, port in peers_list[:50]]
                downloads = []
                for future in as_completed(connections):
                    peer = future.result()
                    if peer is None:
                        continue
                    if stop.is_set() or negotiation_stop.is_set() or sum(not job.done() for job in downloads) >= 20:
                        peer.close()
                        with data["connections_lock"]:
                            data["connections"].discard(peer)
                        continue
                    if not downloader.add_peer(peer):
                        with data["connections_lock"]:
                            data["connections"].discard(peer)
                        continue
                    with data["connections_lock"]:
                        data["peers_connected"] += 1
                    downloads.append(workers.submit(worker, peer))
                for future in downloads:
                    future.result()
        if stop.is_set():
            return False
        if pieces.get_progress()["percentage"] < 100:
            raise ConnectionError("Peers could not complete the torrent")
        pieces.save_to_disk(stop_event=stop)
        return True
    finally:
        _close_connections(data)
        with data["connections_lock"]:
            data["connections"].clear()


async def download_torrent_threading(info_hash: str):
    data = active_torrents.get(info_hash)
    if data is None:
        return
    loop = asyncio.get_running_loop()
    previous_time = loop.time()
    pieces = data["piece_manager"]
    previous_bytes = pieces.get_progress()["downloaded_bytes"] if pieces is not None else 0
    worker = asyncio.create_task(asyncio.to_thread(_download_sync, data))
    try:
        while not worker.done():
            done, _ = await asyncio.wait({worker}, timeout=0.2)
            pieces = data["piece_manager"]
            if pieces is not None:
                progress = pieces.get_progress()
                now = loop.time()
                data["download_speed"] = max(0, progress["downloaded_bytes"] - previous_bytes) / max(now - previous_time, 0.001)
                data["progress"] = progress["percentage"]
                data["downloaded_pieces"] = progress["completed_pieces"]
                previous_bytes, previous_time = progress["downloaded_bytes"], now
                if not done and not data["stop_event"].is_set():
                    await manager.broadcast({"type": "progress_update", **_public_data(info_hash, data)})
        completed = await worker
        if completed and not data["stop_event"].is_set():
            data["status"] = "completed"
            data["progress"] = 100.0
        elif data["stop_event"].is_set():
            data["status"] = "paused"
    except asyncio.CancelledError:
        data["stop_event"].set()
        await asyncio.to_thread(_close_connections, data)
        await asyncio.gather(asyncio.shield(worker), return_exceptions=True)
        data["status"] = "paused"
        raise
    except Exception as error:
        if not data["stop_event"].is_set():
            data["status"] = "error"
            logger.warning("Download failed (%s)", type(error).__name__)
        else:
            data["status"] = "paused"
    finally:
        data["download_speed"] = data["upload_speed"] = data["peers_connected"] = 0
        kind = {"completed": "completed", "error": "error"}.get(data["status"], "status_update")
        await manager.broadcast({"type": kind, **_public_data(info_hash, data),
                                 **({"message": "Download failed"} if kind == "error" else {})})


app.include_router(api)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="localhost", port=8000)
