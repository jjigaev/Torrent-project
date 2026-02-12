from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading

sys.path.append(str(Path(__file__).parent))
from src.torrent import TorrentFile
from src.tracker import TrackerClient
from src.peer import PeerConnection
from src.downloader import Downloader

app = FastAPI(title="MiniTorrentAPI")

app.add_middleware(
    CORSMiddleware, #allows browser to safe request from different domains
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

active_torrents = {}

class ConnectionManager:
    def __init__(self):
        self.active_connections = []
    #WebSocket connnections
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass

manager = ConnectionManager()

@app.get("/")
async def read_root():
    return FileResponse("frontend/index.html")

@app.get("/api/torrents")
async def get_torrents():
    torrents = []
    for info_hash, data in active_torrents.items():
        torrents.append({
            "info_hash": info_hash,
            "name": data.get("name"),
            "total_size": data.get("total_size"),
            "progress": data.get("progress", 0),
            "download_speed": data.get("download_speed", 0),
            "upload_speed": data.get("upload_speed", 0),
            "peers_connected": data.get("peers_connected", 0),
            "status": data.get("status", "paused"),
        })
    return {"torrents": torrents}

@app.post("/api/torrents/add")
async def add_torrent(file: UploadFile = File(...)):
    try:
        torrents_dir = Path("torrents")
        torrents_dir.mkdir(exist_ok=True)

        file_path = torrents_dir / file.filename
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)

        torrent = TorrentFile(str(file_path))
        info_hash = torrent.info_hash.hex()

        active_torrents[info_hash] = {
            "name": torrent.name,
            "total_size": torrent.total_size,
            "piece_count": len(torrent.pieces),
            "tracker": torrent.announce,
            "status": "paused",
            "progress": 0,
            "download_speed": 0,
            "upload_speed": 0,
            "peers_connected": 0,
            "downloaded_pieces": 0,
            "torrent": torrent,
        }

        await manager.broadcast({
            "type": "torrent_added",
            "torrent": {"info_hash": info_hash, "name": torrent.name}
        })

        return {"success": True, "info_hash": info_hash, "name": torrent.name}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/torrents/{info_hash}/start")
async def start_torrent(info_hash: str):
    if info_hash not in active_torrents:
        raise HTTPException(status_code=404, detail="Torrent not found")

    active_torrents[info_hash]["status"] = "downloading"
    asyncio.create_task(download_torrent_threading(info_hash))

    await manager.broadcast({
        "type": "status_update",
        "info_hash": info_hash,
        "status": "downloading"
    })

    return {"success": True}

@app.post("/api/torrents/{info_hash}/pause")
async def pause_torrent(info_hash: str):
    if info_hash not in active_torrents:
        raise HTTPException(status_code=404, detail="Torrent not found")

    active_torrents[info_hash]["status"] = "paused"

    await manager.broadcast({
        "type": "status_update",
        "info_hash": info_hash,
        "status": "paused"
    })

    return {"success": True}

@app.delete("/api/torrents/{info_hash}")
async def delete_torrent(info_hash: str):
    if info_hash not in active_torrents:
        raise HTTPException(status_code=404, detail="Torrent not found")

    del active_torrents[info_hash]
    await manager.broadcast({"type": "torrent_removed", "info_hash": info_hash})
    return {"success": True}

@app.websocket("/ws") #connect to server
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

def try_connect_peer_sync(ip, port, info_hash, peer_id):
    peer = PeerConnection(ip, port, info_hash, peer_id)
    try:
        if not peer.connect(timeout=1):
            return None

        peer.handshake()
        peer.send_interested()

        for _ in range(5):
            msg = peer.receive_message(timeout=1)
            if msg is None:
                continue
            if msg[0] == 5:
                continue
            if msg[0] == 1:
                return peer

        peer.close()
        return None

    except:
        try:
            peer.close()
        except:
            pass
        return None

async def download_torrent_threading(info_hash: str):
    if info_hash not in active_torrents:
        return

    data = active_torrents[info_hash]
    torrent = data["torrent"]

    last_downloaded = 0
    last_time = asyncio.get_event_loop().time() #last time speed

    try:
        print(f"Starting download: {torrent.name}")

        tracker = TrackerClient(torrent)
        peers_list = tracker.get_peers()

        if not peers_list:
            data["status"] = "error"
            return

        print(f"Got {len(peers_list)} peers from tracker")
        print(f"Connecting to peers in parallel")

        with ThreadPoolExecutor(max_workers=50) as executor: #parallelism
            futures = [
                executor.submit(try_connect_peer_sync, ip, port, torrent.info_hash, tracker.peer_id)
                for ip, port in peers_list[:50]
            ]
            results = [f.result() for f in futures]

        active_peers = [p for p in results if p is not None][:20] #limit to number of peers

        if not active_peers:
            print("No active peers available")
            data["status"] = "error"
            return

        data["peers_connected"] = len(active_peers)
        print(f"Connected to {len(active_peers)} peers")

        downloader = Downloader(torrent, active_peers)
        total_pieces = len(torrent.pieces)
        piece_lock = threading.Lock()

        def download_pieces_worker(peer): #потоки
            while data["status"] == "downloading":
                with piece_lock:
                    piece_idx = downloader.piece_manager.get_next_piece_to_download(peer) #index of not downloaded piece
                    if piece_idx is None:
                        break

                print(f"Downloading piece {piece_idx} from {peer.ip}")
                result = downloader.download_piece(peer, piece_idx)

                if not result:
                    break

        with ThreadPoolExecutor(max_workers=len(active_peers)) as executor: #поток активных пиров
            futures = [executor.submit(download_pieces_worker, peer) for peer in active_peers]

            #progress speed and show
            while data["status"] == "downloading":
                await asyncio.sleep(0.5)

                progress = downloader.piece_manager.get_progress()

                if progress["percentage"] >= 100:
                    data["status"] = "completed"
                    data["progress"] = 100.0
                    break

                current_time = asyncio.get_event_loop().time()
                time_diff = current_time - last_time

                if time_diff >= 0.5:
                    bytes_diff = progress["downloaded_bytes"] - last_downloaded
                    data["download_speed"] = bytes_diff / time_diff if time_diff > 0 else 0
                    data["upload_speed"] = 0
                    data["progress"] = progress["percentage"]
                    data["downloaded_pieces"] = progress["completed_pieces"]

                    last_downloaded = progress["downloaded_bytes"]
                    last_time = current_time

                    await manager.broadcast({
                        "type": "progress_update",
                        "info_hash": info_hash,
                        "progress": data["progress"],
                        "downloaded_pieces": data["downloaded_pieces"],
                        "download_speed": data["download_speed"],
                        "upload_speed": 0,
                        "peers_connected": len(active_peers),
                        "status": "downloading"
                    })

                    print(f"Progress: {data['progress']:.1f}% Speed: {data['download_speed']/1024/1024:.2f} MB/s Pieces: {data['downloaded_pieces']}/{total_pieces}")

            for f in futures:
                f.result()

        if data["status"] == "completed":
            print("Saving to disk")
            downloader.piece_manager.save_to_disk()
            print(f"Done: downloads/{torrent.name}")

            await manager.broadcast({
                "type": "completed",
                "info_hash": info_hash,
                "status": "completed"
            })

        for peer in active_peers:
            peer.close()

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        data["status"] = "error"

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="localhost", port=8000)