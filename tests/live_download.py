"""Opt-in network check: python -m tests.live_download --torrent FILE.

Uses real HTTP routes, tracker requests and peer connections. Only storage
locations are changed. The ISO is sampled until pause/resume/delete have been
verified; this check does not download or assemble the complete ISO.
"""

import argparse
import asyncio
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import socket
import tempfile
import time

import httpx
import uvicorn

import web_server as application
from src.torrent import TorrentFile


def report(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=True), flush=True)


class SessionParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name") == "api-token":
            self.token = attrs.get("content")


async def check(args, directory):
    torrent_path = args.torrent.resolve()
    raw = torrent_path.read_bytes()
    torrent = TorrentFile.from_bytes(raw)
    if args.minimum_mib <= 0 or args.stage_timeout <= 0 or args.pause_seconds <= 0:
        raise ValueError("Test limits must be positive")
    threshold = max(1, int(args.minimum_mib * 1024 * 1024))
    if torrent.total_size <= threshold * 2 + torrent.piece_length:
        raise ValueError("Torrent must be large enough to remain active during the check")
    application.TORRENTS_DIR = directory / "metadata"
    application.DOWNLOAD_DIR = directory / "downloads"
    if application.active_torrents:
        raise RuntimeError("Run this check in a fresh process")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(application.app, host="127.0.0.1", port=port,
                                          log_level="warning", access_log=False))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    data = None
    started_at = time.monotonic()
    try:
        for _ in range(500):
            if server.started:
                break
            if serving.done():
                await serving
                raise RuntimeError("Test server did not start")
            await asyncio.sleep(0.01)
        else:
            raise TimeoutError("Test server did not start")
        report("server", url=f"http://127.0.0.1:{port}")

        # Heartbeats are 5 seconds apart, matching Uvicorn's idle timeout.
        # Expire pooled sockets earlier rather than racing its idle close.
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=90,
                                     limits=httpx.Limits(keepalive_expiry=2), trust_env=False) as client:
            root = await client.get("/")
            root.raise_for_status()
            parser = SessionParser()
            parser.feed(root.text)
            if not parser.token:
                raise RuntimeError("Session bootstrap failed")
            client.headers["Authorization"] = f"Bearer {parser.token}"
            added = await client.post("/api/torrents/add", files={"file": (torrent_path.name, raw)})
            added.raise_for_status()
            info_hash = added.json()["info_hash"]
            data = application.active_torrents[info_hash]
            endpoint = f"/api/torrents/{info_hash}"

            def snapshot():
                manager = data["piece_manager"]
                progress = manager.get_progress() if manager is not None else {}
                return {"status": data["status"], "peers": data["peers_connected"],
                        "verified_bytes": progress.get("downloaded_bytes", 0),
                        "pieces": progress.get("completed_pieces", 0)}

            def verify_hashes():
                manager = data["piece_manager"]
                verified = manager.verified_piece_indices()
                if not verified:
                    raise AssertionError("No real payload pieces were received")
                for index in verified:
                    digest = hashlib.sha1()
                    length = manager.get_piece_length(index)
                    for offset in range(0, length, manager.BLOCK_SIZE):
                        digest.update(manager.read_verified_block(index, offset, min(manager.BLOCK_SIZE, length - offset)))
                    if digest.digest() != torrent.pieces[index]:
                        raise AssertionError(f"Invalid SHA-1 for piece {index}")
                return set(verified)

            async def wait_for_bytes(target, stage):
                deadline = time.monotonic() + args.stage_timeout
                previous_report = 0
                while time.monotonic() < deadline:
                    current = snapshot()
                    if current["verified_bytes"] >= target:
                        report(stage, **current)
                        return current
                    if current["status"] == "error":
                        raise RuntimeError(f"Download entered error state during {stage}")
                    if time.monotonic() - previous_report >= 5:
                        response = await client.get("/api/torrents")
                        response.raise_for_status()
                        report("progress", stage=stage, **current)
                        previous_report = time.monotonic()
                    await asyncio.sleep(0.2)
                raise TimeoutError(f"No sufficient verified data during {stage}")

            report("torrent", name=torrent.name, total_bytes=torrent.total_size,
                   info_hash=info_hash, minimum_stage_bytes=threshold)
            response = await client.post(endpoint + "/start")
            response.raise_for_status()
            await wait_for_bytes(threshold, "downloaded_before_pause")

            pause_started = time.monotonic()
            response = await client.post(endpoint + "/pause")
            response.raise_for_status()
            paused = snapshot()
            before = verify_hashes()
            if paused["status"] != "paused" or not data["task"].done():
                raise AssertionError("Pause did not join the download task")
            if data["piece_manager"].has_pending() or data["connections"]:
                raise AssertionError("Pause left active blocks or connections")
            await asyncio.sleep(args.pause_seconds)
            if snapshot() != paused:
                raise AssertionError("Download advanced while paused")
            report("pause_passed", seconds=round(time.monotonic() - pause_started, 3), **paused)

            response = await client.post(endpoint + "/start")
            response.raise_for_status()
            await wait_for_bytes(paused["verified_bytes"] + threshold, "downloaded_after_resume")
            manager = data["piece_manager"]
            verified = verify_hashes()
            if not before.issubset(verified):
                raise AssertionError("Resume lost previously verified pieces")

            deletion_started = time.monotonic()
            response = await client.delete(endpoint)
            response.raise_for_status()
            deleted = snapshot()
            if info_hash in application.active_torrents or not data["task"].done():
                raise AssertionError("Delete left an active torrent or worker")
            if manager.has_pending() or data["connections"]:
                raise AssertionError("Delete left blocks or sockets active")
            if not manager.closed or list(application.DOWNLOAD_DIR.glob(".minitorrent-*")):
                raise AssertionError("Delete left partial storage open or on disk")
            disk_state = {str(item.relative_to(directory)): item.stat().st_size
                          for item in directory.rglob("*") if item.is_file()}
            await asyncio.sleep(args.pause_seconds)
            later = {str(item.relative_to(directory)): item.stat().st_size
                     for item in directory.rglob("*") if item.is_file()}
            if snapshot() != deleted or later != disk_state:
                raise AssertionError("Activity continued after deletion")
            listed = await client.get("/api/torrents")
            listed.raise_for_status()
            if listed.json()["torrents"]:
                raise AssertionError("Deleted torrent is still listed")
            report("delete_passed", seconds=round(time.monotonic() - deletion_started, 3), **deleted)
            report("passed", verified_pieces=len(verified),
                   verified_bytes=sum(manager.get_piece_length(index) for index in verified),
                   downloaded_bytes_at_delete=deleted["verified_bytes"], storage_closed=manager.closed,
                   seconds=round(time.monotonic() - started_at, 3), full_iso=False)
    finally:
        if data is not None:
            await application._dispose_download(data)
        server.should_exit = True
        await serving
        listener.close()
        application.active_torrents.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--torrent", required=True, type=Path)
    parser.add_argument("--minimum-mib", type=float, default=1)
    parser.add_argument("--stage-timeout", type=float, default=180)
    parser.add_argument("--pause-seconds", type=float, default=3)
    parser.add_argument("--scratch-parent", type=Path,
                        help="Parent directory for an automatically cleaned temporary workspace")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ubuntu-live-", dir=args.scratch_parent) as temporary:
        scratch = Path(temporary).resolve()
        expected_parent = (args.scratch_parent or Path(tempfile.gettempdir())).resolve()
        if scratch.parent != expected_parent:
            raise RuntimeError("Unexpected scratch directory")
        asyncio.run(check(args, scratch))


if __name__ == "__main__":
    main()
