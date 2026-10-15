import asyncio
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
import socket
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import web_server as server
from src.piece_manager import PieceManager
from tests.helpers import FakePeer, serve_seed, torrent_bytes


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.directory = Path(self.scratch.name)
        self.patches = [patch.object(server, "TORRENTS_DIR", self.directory / "torrents"),
                        patch.object(server, "DOWNLOAD_DIR", self.directory / "downloads")]
        for item in self.patches:
            item.start()
        server.active_torrents.clear()
        self.transport = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 50000))
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://localhost:8000")
        self.headers = {"Authorization": f"Bearer {server.SESSION_TOKEN}"}

    async def asyncTearDown(self):
        for data in tuple(server.active_torrents.values()):
            await server._dispose_download(data)
        server.active_torrents.clear()
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        self.scratch.cleanup()

    async def add(self, raw=None, filename="sample.torrent"):
        return await self.client.post("/api/torrents/add", headers=self.headers,
                                      files={"file": (filename, raw if raw is not None else torrent_bytes())})

    async def wait_until(self, predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("Background operation did not finish")

    def mock_network(self, peer):
        tracker = SimpleNamespace(peer_id=b"p" * 20, get_peers=lambda: [("127.0.0.1", 6881)])

        def connect(*args):
            data = args[4]
            with data["connections_lock"]:
                data["connections"].add(peer)
            return peer

        return patch.object(server, "TrackerClient", return_value=tracker), patch.object(server, "try_connect_peer_sync", side_effect=connect)

    async def test_api_requires_authentication_for_reads_and_mutations(self):
        for method, url in [("GET", "/api/torrents"), ("POST", "/api/torrents/add"),
                            ("POST", "/api/torrents/hash/start"), ("POST", "/api/torrents/hash/pause"),
                            ("DELETE", "/api/torrents/hash")]:
            with self.subTest(method=method, url=url):
                response = await self.client.request(method, url)
                self.assertEqual(response.status_code, 401)
        response = await self.client.get("/api/torrents", headers=[(b"authorization", b"Bearer \xff")])
        self.assertEqual(response.status_code, 401)

    async def test_rejects_cross_origin_dns_rebinding_and_remote_clients(self):
        for headers in [{**self.headers, "Origin": "https://evil.invalid"},
                        {**self.headers, "Host": "evil.invalid"},
                        {**self.headers, "Origin": "null"}]:
            response = await self.client.get("/api/torrents", headers=headers)
            self.assertEqual(response.status_code, 403)
        remote = httpx.ASGITransport(app=server.app, client=("203.0.113.1", 50000))
        async with httpx.AsyncClient(transport=remote, base_url="http://localhost:8000") as client:
            response = await client.get("/")
            self.assertEqual(response.status_code, 403)
            self.assertNotIn(server.SESSION_TOKEN, response.text)

    async def test_authentication_scheme_is_case_insensitive(self):
        response = await self.client.get("/api/torrents", headers={"Authorization": f"bearer {server.SESSION_TOKEN}"})
        self.assertEqual(response.status_code, 200)

    async def test_invalid_content_length_is_rejected_without_internal_error(self):
        for length, status in [("invalid", 400), ("9" * 5000, 413)]:
            response = await self.client.post("/api/torrents/add", content=b"",
                                              headers={**self.headers, "Content-Length": length})
            self.assertEqual(response.status_code, status)

    async def test_root_bootstraps_session_and_all_local_assets_load(self):
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'content="{server.SESSION_TOKEN}"', response.text)
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.assertIn("samesite=strict", response.headers["set-cookie"].lower())
        self.assertEqual(response.headers["cache-control"], "no-store")

        class Assets(HTMLParser):
            def __init__(self):
                super().__init__()
                self.urls = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                url = attrs.get("src") if tag == "script" else attrs.get("href") if tag == "link" else None
                if url and "://" not in url:
                    self.urls.append(url)

        assets = Assets()
        assets.feed(response.text)
        self.assertIn("jss/app.js", assets.urls)
        for url in assets.urls:
            asset = await self.client.get("/" + url)
            self.assertEqual(asset.status_code, 200, url)

    async def test_upload_uses_hash_name_and_invalid_files_never_overwrite(self):
        outside = self.directory / "escaped.torrent"
        outside.write_bytes(b"original")
        for filename in ["../escaped.torrent", "..\\escaped.torrent", "CON.torrent"]:
            with self.subTest(filename=filename):
                response = await self.add(filename=filename)
                self.assertEqual(response.status_code, 400)
        response = await self.add(raw=b"not a torrent")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(outside.read_bytes(), b"original")
        self.assertFalse((self.directory / "torrents").exists())
        response = await self.add()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item.name for item in server.TORRENTS_DIR.iterdir()],
                         [response.json()["info_hash"] + ".torrent"])

    async def test_normalized_windows_upload_name_still_uses_server_generated_path(self):
        # Multipart parsers may strip a browser-supplied Windows drive/path.
        # Storage must remain independent of that normalized filename.
        response = await self.add(filename="C:\\escaped.torrent")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item.name for item in server.TORRENTS_DIR.iterdir()],
                         [response.json()["info_hash"] + ".torrent"])

    async def test_upload_rejects_unsafe_torrent_paths_before_storage(self):
        for raw in [torrent_bytes(name=b"../escape"),
                    torrent_bytes(files=[{"path": [b"..", b"escape"], "length": 4}])]:
            response = await self.add(raw)
            self.assertEqual(response.status_code, 400)
        self.assertFalse(server.TORRENTS_DIR.exists())

    async def test_large_upload_and_streaming_body_are_bounded(self):
        with patch.object(server, "MAX_UPLOAD_SIZE", 10):
            response = await self.add()
            self.assertEqual(response.status_code, 413)

        async def chunks():
            yield b'--x\r\nContent-Disposition: form-data; name="file"; filename="sample.torrent"\r\n\r\n'
            yield b"x" * 200
            yield b"\r\n--x--\r\n"

        with patch.object(server, "MAX_REQUEST_SIZE", 100):
            response = await self.client.post("/api/torrents/add", content=chunks(),
                                              headers={**self.headers, "Content-Type": "multipart/form-data; boundary=x"})
            self.assertEqual(response.status_code, 413)
        self.assertFalse(server.TORRENTS_DIR.exists())

    async def test_storage_errors_do_not_expose_internal_paths(self):
        with patch.object(server, "_store_upload", side_effect=OSError("private/path/secret")):
            response = await self.add()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Unable to store torrent"})

    async def test_repeated_start_and_duplicate_upload_preserve_one_task(self):
        info_hash = (await self.add()).json()["info_hash"]
        peer = FakePeer(block=True)
        tracker_patch, peer_patch = self.mock_network(peer)
        with tracker_patch, peer_patch as connect_mock:
            response = await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            await self.wait_until(peer.entered.is_set)
            data = server.active_torrents[info_hash]
            task = data["task"]
            await asyncio.gather(*(self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
                                   for _ in range(3)))
            await self.add()
            self.assertIs(server.active_torrents[info_hash], data)
            self.assertIs(data["task"], task)
            self.assertEqual(connect_mock.call_count, 1)
            await self.client.post(f"/api/torrents/{info_hash}/pause", headers=self.headers)
            self.assertTrue(task.done())
            self.assertTrue(peer.closed)
            self.assertEqual(data["status"], "paused")
            self.assertFalse(data["piece_manager"].has_pending())

    async def test_delete_waits_for_worker_and_prevents_further_writes(self):
        info_hash = (await self.add()).json()["info_hash"]
        peer = FakePeer(block=True)
        tracker_patch, peer_patch = self.mock_network(peer)
        with tracker_patch, peer_patch:
            await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
            await self.wait_until(peer.entered.is_set)
            data = server.active_torrents[info_hash]
            task = data["task"]
            response = await self.client.delete(f"/api/torrents/{info_hash}", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(task.done())
            self.assertTrue(peer.closed)
            self.assertNotIn(info_hash, server.active_torrents)
            self.assertFalse((server.DOWNLOAD_DIR / "data.bin").exists())
            self.assertTrue(data["piece_manager"].closed)

    async def test_success_marks_completed_only_after_file_is_saved(self):
        info_hash = (await self.add()).json()["info_hash"]
        peer = FakePeer()
        tracker_patch, peer_patch = self.mock_network(peer)
        with tracker_patch, peer_patch:
            await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
            data = server.active_torrents[info_hash]
            await asyncio.wait_for(data["task"], 3)
        self.assertEqual(data["status"], "completed")
        self.assertEqual((server.DOWNLOAD_DIR / "data.bin").read_bytes(), b"ABCD")
        self.assertEqual(data["download_speed"], 0)
        self.assertTrue(peer.closed)

    async def test_ready_peer_starts_while_slow_peer_waits_and_both_are_cleaned_up(self):
        for complete in [False, True]:
            raw = torrent_bytes(name=f"case-{complete}.bin".encode())
            info_hash = (await self.add(raw)).json()["info_hash"]
            good, slow = FakePeer(block=True), FakePeer(block=True)
            slow_entered = threading.Event()
            tracker = SimpleNamespace(peer_id=b"p" * 20,
                                      get_peers=lambda: [("127.0.0.1", 1), ("127.0.0.1", 2)])

            def connect(ip, port, digest, peer_id, data, cancel_event):
                peer = good if port == 1 else slow
                with data["connections_lock"]:
                    data["connections"].add(peer)
                if port == 1:
                    if not slow_entered.wait(2):
                        raise TimeoutError("Slow test peer did not enter")
                    return good
                slow_entered.set()
                if not slow.release.wait(3):
                    raise TimeoutError("Slow test peer was not closed")
                with data["connections_lock"]:
                    data["connections"].discard(slow)
                return None

            with patch.object(server, "TrackerClient", return_value=tracker), \
                    patch.object(server, "try_connect_peer_sync", side_effect=connect):
                try:
                    await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
                    await self.wait_until(good.entered.is_set)
                    self.assertFalse(slow.release.is_set())
                    data = server.active_torrents[info_hash]
                    self.assertEqual(data["peers_connected"], 1)
                    if complete:
                        good.release.set()
                        await asyncio.wait_for(data["task"], 2)
                        self.assertEqual(data["status"], "completed")
                    else:
                        response = await self.client.post(f"/api/torrents/{info_hash}/pause", headers=self.headers)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(data["status"], "paused")
                    self.assertTrue(data["task"].done())
                    self.assertTrue(good.closed and slow.closed)
                    self.assertFalse(data["connections"])
                    self.assertEqual(data["peers_connected"], 0)
                    self.assertEqual(data["piece_manager"].closed, complete)
                finally:
                    good.close()
                    slow.close()

    async def test_resume_does_not_report_retained_bytes_as_new_speed(self):
        info_hash = (await self.add(torrent_bytes(payload=b"ABCDEFGH"))).json()["info_hash"]
        data = server.active_torrents[info_hash]
        pieces = PieceManager(data["torrent"], server.DOWNLOAD_DIR)
        data["piece_manager"] = pieces
        pieces.init_piece_download(0)
        pieces.add_block(0, 0, b"ABCD")
        peer = FakePeer(payload=b"ABCDEFGH", block=True)
        tracker_patch, peer_patch = self.mock_network(peer)
        with tracker_patch, peer_patch:
            await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
            await self.wait_until(peer.entered.is_set)
            await self.wait_until(lambda: data["downloaded_pieces"] == 1)
            self.assertEqual(data["download_speed"], 0)
            self.assertEqual(pieces.get_progress()["downloaded_bytes"], 4)
            await self.client.post(f"/api/torrents/{info_hash}/pause", headers=self.headers)
        self.assertFalse(pieces.closed)
        self.assertEqual(pieces.read_verified_block(0, 0, 4), b"ABCD")
        next_peer = FakePeer(payload=b"ABCDEFGH")
        tracker_patch, peer_patch = self.mock_network(next_peer)
        with tracker_patch, peer_patch:
            await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
            await asyncio.wait_for(data["task"], 2)
        self.assertEqual(data["status"], "completed")
        self.assertEqual((server.DOWNLOAD_DIR / "data.bin").read_bytes(), b"ABCDEFGH")

    async def test_save_failure_at_full_progress_can_retry_without_redownloading(self):
        info_hash = (await self.add()).json()["info_hash"]
        server.DOWNLOAD_DIR.mkdir()
        target = server.DOWNLOAD_DIR / "data.bin"
        target.write_bytes(b"original")
        tracker_patch, peer_patch = self.mock_network(FakePeer())
        with tracker_patch, peer_patch, patch("src.piece_manager.os.replace", side_effect=OSError("disk full")):
            await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
            data = server.active_torrents[info_hash]
            await asyncio.wait_for(data["task"], 2)
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["progress"], 100)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertFalse(data["piece_manager"].closed)
        with patch.object(server, "TrackerClient", side_effect=AssertionError("Already verified")):
            await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
            await asyncio.wait_for(data["task"], 2)
        self.assertEqual(data["status"], "completed")
        self.assertEqual(target.read_bytes(), b"ABCD")
        self.assertTrue(data["piece_manager"].closed)

    async def test_real_tcp_seed_choke_resume_and_full_file_save(self):
        payload = b"wire" * (256 * 1024)
        info_hash = (await self.add(torrent_bytes(payload=payload, piece_length=262144))).json()["info_hash"]
        data = server.active_torrents[info_hash]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener, ThreadPoolExecutor(max_workers=1) as pool:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(3)
            future = pool.submit(serve_seed, listener, data["torrent"], payload)
            tracker = SimpleNamespace(peer_id=b"p" * 20,
                                      get_peers=lambda: [("127.0.0.1", listener.getsockname()[1])])
            try:
                with patch.object(server, "TrackerClient", return_value=tracker):
                    await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
                    await asyncio.wait_for(data["task"], 5)
            finally:
                await server._stop_download(data)
            requests = future.result(timeout=3)
        self.assertEqual(data["status"], "completed")
        self.assertEqual((server.DOWNLOAD_DIR / "data.bin").read_bytes(), payload)
        self.assertGreater(requests, len(payload) // PieceManager.BLOCK_SIZE)
        self.assertTrue(data["piece_manager"].closed)
        self.assertFalse(data["connections"])

    async def test_all_failed_workers_terminate_with_error(self):
        info_hash = (await self.add()).json()["info_hash"]
        peer = FakePeer(payload=None)
        tracker_patch, peer_patch = self.mock_network(peer)
        with tracker_patch, peer_patch:
            await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
            data = server.active_torrents[info_hash]
            await asyncio.wait_for(data["task"], 3)
        self.assertEqual(data["status"], "error")
        self.assertFalse(data["piece_manager"].has_pending())
        self.assertTrue(peer.closed)

    async def test_worker_waits_for_have_after_completing_first_piece(self):
        info_hash = (await self.add(torrent_bytes(payload=b"ABCDEFGH"))).json()["info_hash"]
        clock = [0]

        class LateHavePeer(FakePeer):
            def __init__(self):
                super().__init__(payload=b"ABCDEFGH")
                self.peer_pieces = {0}
                self.waited = False

            def has_piece(self, index):
                return index in self.peer_pieces

            def receive_message(self, timeout=15):
                if self.requests:
                    return super().receive_message(timeout)
                if 0 not in self.announced_pieces:
                    raise AssertionError("Peer needs HAVE before advertising the next piece")
                if self.am_interested:
                    raise AssertionError("Interest must clear when no advertised pieces are needed")
                if not self.waited:
                    self.waited = True
                    clock[0] += 20
                    return None
                self.peer_pieces.add(1)
                return 4, struct.pack(">I", 1)

            def request_piece(self, index, offset, length):
                if not self.am_interested:
                    raise AssertionError("New HAVE must restore interest before requesting data")
                super().request_piece(index, offset, length)

        peer = LateHavePeer()
        tracker_patch, peer_patch = self.mock_network(peer)
        with tracker_patch, peer_patch, patch.object(server, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
            data = server.active_torrents[info_hash]
            await asyncio.wait_for(data["task"], 3)
        self.assertEqual(data["status"], "completed")
        self.assertEqual((server.DOWNLOAD_DIR / "data.bin").read_bytes(), b"ABCDEFGH")

    async def test_tracker_and_connections_do_not_block_api(self):
        info_hash = (await self.add()).json()["info_hash"]
        entered, release = threading.Event(), threading.Event()
        thread_ids = []

        def tracker_request():
            thread_ids.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return []

        tracker = SimpleNamespace(get_peers=tracker_request)
        with patch.object(server, "TrackerClient", return_value=tracker):
            try:
                await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
                await self.wait_until(entered.is_set)
                response = await asyncio.wait_for(self.client.get("/api/torrents", headers=self.headers), 0.5)
                self.assertEqual(response.status_code, 200)
                self.assertNotEqual(thread_ids[0], threading.get_ident())
            finally:
                release.set()
                await asyncio.wait_for(server.active_torrents[info_hash]["task"], 3)

    async def test_resume_after_pause_uses_a_new_task_without_stale_blocks(self):
        info_hash = (await self.add()).json()["info_hash"]
        first = FakePeer(block=True)
        tracker_patch, peer_patch = self.mock_network(first)
        with tracker_patch, peer_patch:
            await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
            await self.wait_until(first.entered.is_set)
            data = server.active_torrents[info_hash]
            old_task = data["task"]
            await self.client.post(f"/api/torrents/{info_hash}/pause", headers=self.headers)
        second = FakePeer()
        tracker_patch, peer_patch = self.mock_network(second)
        with tracker_patch, peer_patch:
            await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
            self.assertIsNot(data["task"], old_task)
            await asyncio.wait_for(data["task"], 3)
        self.assertEqual(data["status"], "completed")
        self.assertEqual((server.DOWNLOAD_DIR / "data.bin").read_bytes(), b"ABCD")

    async def test_shutdown_stops_active_downloads(self):
        info_hash = (await self.add()).json()["info_hash"]
        peer = FakePeer(block=True)
        tracker_patch, peer_patch = self.mock_network(peer)
        with tracker_patch, peer_patch:
            async with server.app.router.lifespan_context(server.app):
                await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
                await self.wait_until(peer.entered.is_set)
                task = server.active_torrents[info_hash]["task"]
            self.assertTrue(task.done())
            self.assertTrue(peer.closed)

    async def test_shutdown_cleans_up_even_when_lifespan_exits_with_error(self):
        info_hash = (await self.add()).json()["info_hash"]
        peer = FakePeer(block=True)
        tracker_patch, peer_patch = self.mock_network(peer)
        with tracker_patch, peer_patch:
            with self.assertRaisesRegex(RuntimeError, "controlled shutdown"):
                async with server.app.router.lifespan_context(server.app):
                    await self.client.post(f"/api/torrents/{info_hash}/start", headers=self.headers)
                    await self.wait_until(peer.entered.is_set)
                    task = server.active_torrents[info_hash]["task"]
                    raise RuntimeError("controlled shutdown")
            self.assertTrue(task.done())
            self.assertTrue(peer.closed)


class PeerNegotiationTests(unittest.TestCase):
    def test_unchoke_before_late_have_does_not_return_prematurely(self):
        class Peer:
            peer_choking = True

        peer = Peer()
        peer.peer_pieces = set()
        peer.close = lambda: None
        peer.connect = lambda **kwargs: True
        peer.handshake = lambda: True
        peer.send_interested = lambda: None
        peer.send_bitfield = lambda bitfield: None
        messages = iter([1, None, None, None, None, None, None, 4])
        calls = []

        def receive(timeout=1):
            kind = next(messages)
            calls.append(kind)
            if kind == 1:
                peer.peer_choking = False
            if kind == 4:
                peer.peer_pieces.add(0)
            return kind, b""

        peer.receive_message = receive
        data = {"stop_event": threading.Event(), "connections": set(),
                "piece_count": 1,
                "connections_lock": threading.Lock(),
                "piece_manager": SimpleNamespace(get_bitfield=lambda: b"\x00")}
        clock = [0]

        def monotonic():
            clock[0] += 4
            return clock[0]

        with patch.object(server, "PeerConnection", return_value=peer), \
                patch.object(server, "time", SimpleNamespace(monotonic=monotonic)):
            result = server.try_connect_peer_sync("127.0.0.1", 6881, b"i" * 20, b"p" * 20, data)
        self.assertIs(result, peer)
        self.assertEqual(len(calls), 8)
        self.assertGreater(clock[0], 15)

    def test_stop_during_negotiation_closes_and_unregisters_peer(self):
        data = {"stop_event": threading.Event(), "connections": set(),
                "piece_count": 1,
                "connections_lock": threading.Lock(),
                "piece_manager": SimpleNamespace(get_bitfield=lambda: b"\x00")}
        peer = FakePeer()
        peer.peer_pieces = set()
        peer.connect = lambda **kwargs: True
        peer.handshake = lambda: True
        peer.send_interested = lambda: None
        peer.send_bitfield = lambda bitfield: None

        def receive(timeout=1):
            data["stop_event"].set()
            return None

        peer.receive_message = receive
        with patch.object(server, "PeerConnection", return_value=peer):
            self.assertIsNone(server.try_connect_peer_sync("127.0.0.1", 6881, b"i" * 20, b"p" * 20, data))
        self.assertTrue(peer.closed)
        self.assertFalse(data["connections"])

    def test_resume_announces_verified_bitfield_before_interested(self):
        events = []
        peer = FakePeer()
        peer.peer_pieces = {1}
        peer.connect = lambda **kwargs: True
        peer.handshake = lambda: events.append("handshake")
        peer.send_bitfield = lambda bits: events.append(("bitfield", bits))
        peer.send_interested = lambda: events.append("interested")
        data = {"stop_event": threading.Event(), "connections": set(),
                "piece_count": 2,
                "connections_lock": threading.Lock(),
                "piece_manager": SimpleNamespace(get_bitfield=lambda: b"\x80")}
        with patch.object(server, "PeerConnection", return_value=peer):
            result = server.try_connect_peer_sync("127.0.0.1", 6881, b"i" * 20, b"p" * 20, data)
        self.assertIs(result, peer)
        self.assertEqual(events, ["handshake", ("bitfield", b"\x80"), "interested"])


class WebSocketTests(unittest.TestCase):
    def client(self):
        return TestClient(server.app, base_url="http://localhost:8000", client=("127.0.0.1", 50000))

    def test_rejects_unauthenticated_and_cross_origin_connections(self):
        with self.client() as client:
            with self.assertRaises(WebSocketDisconnect) as result:
                with client.websocket_connect("ws://localhost:8000/ws", headers={"origin": "http://localhost:8000"}):
                    pass
            self.assertEqual(result.exception.code, 1008)
            client.get("/")
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("ws://localhost:8000/ws", headers={"origin": "https://evil.invalid"}):
                    pass

    def test_authenticated_connection_receives_broadcast(self):
        with self.client() as client:
            client.get("/")
            with client.websocket_connect("ws://localhost:8000/ws", headers={"origin": "http://localhost:8000"}) as websocket:
                websocket.send_text("ping")
                client.portal.call(server.manager.broadcast, {"type": "test"})
                self.assertEqual(websocket.receive_json(), {"type": "test"})


if __name__ == "__main__":
    unittest.main()
