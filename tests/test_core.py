from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from src.bencode import BencodeDecoder, BencodeEncoder
from src.downloader import Downloader
from src.peer import PeerConnection
from src.piece_manager import PieceManager
from src.torrent import TorrentFile
from src.tracker import TrackerClient
from tests.helpers import FakePeer, torrent_bytes


class BencodeTests(unittest.TestCase):
    def test_round_trip(self):
        value = {"a": [-12, 0, b"\x00\xff"], "z": {"x": b"hello"}}
        self.assertEqual(BencodeDecoder(BencodeEncoder.encode(value)).decode(), value)

    def test_rejects_malformed_inputs(self):
        inputs = [b"", b"10:abc", b"03:abc", b"i03e", b"i-0e", b"i+1e",
                  b"i e", b"ie", b"l", b"d", b"li1e", b"d1:ae", b"1:aJUNK",
                  b"di1ei2ee", b"d1:bi1e1:ai2ee", b"d1:ai1e1:ai2ee",
                  b"9" * 5000 + b":x"]
        for data in inputs:
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                BencodeDecoder(data).decode()

    def test_depth_and_input_size_are_bounded(self):
        with self.assertRaises(ValueError):
            BencodeDecoder(b"l" * 66 + b"e" * 66).decode()
        with self.assertRaises(ValueError):
            BencodeDecoder(b"x" * (BencodeDecoder.MAX_SIZE + 1))

    def test_binary_dictionary_keys_are_preserved_without_loss(self):
        value = {"ascii": b"value", b"\xff": 1}
        self.assertEqual(BencodeDecoder(BencodeEncoder.encode(value)).decode(), value)
        with self.assertRaises(ValueError):
            BencodeEncoder.encode({"a": 1, b"a": 2})


class TorrentTests(unittest.TestCase):
    def test_valid_metadata_and_info_hash(self):
        raw = torrent_bytes(payload=b"ABCDEFG")
        torrent = TorrentFile.from_bytes(raw)
        decoder = BencodeDecoder(raw)
        decoder.decode()
        start, end = decoder.info_span
        self.assertEqual(torrent.info_hash, hashlib.sha1(raw[start:end]).digest())
        self.assertEqual(torrent.total_size, 7)
        self.assertEqual(len(torrent.pieces), 2)

    def test_rejects_unsafe_names_on_every_platform(self):
        for name in [b"../outside", b"..\\outside", b"/absolute", b"C:\\file", b"file:stream",
                     b"", b".", b"..", b"NUL.txt", b"CON", b"CON .txt", b"file.", b"file ", b"a\x00b"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                TorrentFile.from_bytes(torrent_bytes(name=name))

    def test_rejects_unsafe_multi_file_paths(self):
        for parts in [[b"..", b"escape"], [b"/escape"], [b"a\\b"], [b"C:", b"x"], [], [b"NUL"]]:
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                TorrentFile.from_bytes(torrent_bytes(files=[{"path": parts, "length": 4}]))

    def test_rejects_conflicting_paths_and_invalid_piece_metadata(self):
        cases = [
            [{"path": [b"a"], "length": 2}, {"path": [b"A"], "length": 2}],
            [{"path": [b"a"], "length": 2}, {"path": [b"a", b"b"], "length": 2}],
            [{"path": [b"a"], "length": -1}],
        ]
        for files in cases:
            with self.subTest(files=files), self.assertRaises(ValueError):
                TorrentFile.from_bytes(torrent_bytes(files=files))
        for key, value in [("piece length", 0), ("piece length", -1), ("length", -1), ("pieces", b"x")]:
            metadata = BencodeDecoder(torrent_bytes()).decode()
            metadata["info"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                TorrentFile.from_bytes(BencodeEncoder.encode(metadata))


class PieceTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)

    def manager(self, raw=None):
        manager = PieceManager(TorrentFile.from_bytes(raw or torrent_bytes()), self.directory / "downloads")
        self.addCleanup(manager.close)
        return manager

    def complete(self, manager, payload=b"ABCD"):
        for index in range(len(manager.have_pieces)):
            for offset, length in manager.init_piece_download(index):
                start = index * manager.torrent.piece_length + offset
                manager.add_block(index, offset, payload[start:start + length])

    def test_single_file_saves_verified_content(self):
        manager = self.manager(torrent_bytes(payload=b"ABCDEFG"))
        self.complete(manager, b"ABCDEFG")
        manager.save_to_disk()
        self.assertEqual((manager.download_dir / "data.bin").read_bytes(), b"ABCDEFG")

    def test_multi_file_splits_pieces_across_boundaries(self):
        files = [{"path": [b"nested", b"first"], "length": 3}, {"path": [b"second"], "length": 4},
                 {"path": [b"empty"], "length": 0}]
        manager = self.manager(torrent_bytes(payload=b"ABCDEFG", name=b"bundle", files=files))
        self.complete(manager, b"ABCDEFG")
        manager.save_to_disk()
        self.assertEqual((manager.download_dir / "bundle/nested/first").read_bytes(), b"ABC")
        self.assertEqual((manager.download_dir / "bundle/second").read_bytes(), b"DEFG")
        self.assertEqual((manager.download_dir / "bundle/empty").read_bytes(), b"")

    def test_save_rechecks_paths_even_if_metadata_changes(self):
        manager = self.manager()
        self.complete(manager)
        manager.torrent.data["info"]["name"] = b"../escaped.bin"
        with self.assertRaises(ValueError):
            manager.save_to_disk()
        self.assertFalse((self.directory / "escaped.bin").exists())

    def test_save_rechecks_multi_file_paths(self):
        raw = torrent_bytes(name=b"bundle", files=[{"path": [b"file"], "length": 4}])
        manager = self.manager(raw)
        self.complete(manager)
        manager.torrent.data["info"]["files"][0]["path"] = [b"..", b"..", b"escaped.bin"]
        with self.assertRaises(ValueError):
            manager.save_to_disk()
        self.assertFalse((self.directory / "escaped.bin").exists())

    def test_symlink_or_windows_junction_escape_is_rejected(self):
        raw = torrent_bytes(name=b"bundle", files=[{"path": [b"file"], "length": 4}])
        manager = self.manager(raw)
        self.complete(manager)
        outside = self.directory / "outside"
        outside.mkdir()
        (outside / "file").write_bytes(b"original")
        link = manager.download_dir / "bundle"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        with self.assertRaises(ValueError):
            manager.save_to_disk()
        self.assertEqual((outside / "file").read_bytes(), b"original")

    def test_cancelled_or_incomplete_save_does_not_replace_output(self):
        manager = self.manager()
        target = manager.download_dir / "data.bin"
        target.write_bytes(b"original")
        with self.assertRaises(ValueError):
            manager.save_to_disk()
        self.complete(manager)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            manager.save_to_disk(stop_event=stop)
        self.assertEqual(target.read_bytes(), b"original")

    def test_reservations_are_atomic(self):
        manager = self.manager()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: manager.reserve_next_piece(FakePeer()), range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_failed_peer_releases_piece_for_another_peer(self):
        manager = self.manager()
        bad, good = FakePeer(payload=None), FakePeer()
        downloader = Downloader(manager.torrent, [bad, good], piece_manager=manager)
        self.assertFalse(downloader.download_piece(bad, 0))
        self.assertFalse(manager.has_pending())
        self.assertEqual(downloader.peer_inflight[bad], 0)
        reservation = manager.reserve_next_piece(good)
        self.assertIsNotNone(reservation)
        self.assertTrue(downloader.download_piece(good, *reservation))

    def test_verified_piece_is_announced_to_all_peers_even_if_one_disconnects(self):
        manager = self.manager()
        source, disconnected, observer = FakePeer(), FakePeer(), FakePeer()
        disconnected.send_have = Mock(side_effect=OSError("closed"))
        downloader = Downloader(manager.torrent, [source, disconnected, observer], piece_manager=manager)
        self.assertTrue(downloader.download_piece(source, 0))
        self.assertTrue(manager.has_piece(0))
        self.assertEqual(source.announced_pieces, [0])
        self.assertEqual(observer.announced_pieces, [0])
        disconnected.send_have.assert_called_once_with(0)

    def test_new_peer_catches_up_pieces_finished_during_negotiation(self):
        manager = self.manager(torrent_bytes(payload=b"ABCDEFGH"))
        manager.init_piece_download(0)
        manager.add_block(0, 0, b"ABCD")
        downloader = Downloader(manager.torrent, [], piece_manager=manager)
        late, informed, disconnected = FakePeer(), FakePeer(), FakePeer()
        informed.sent_bitfield = b"\x80"
        disconnected.send_have = Mock(side_effect=ConnectionError("closed"))
        self.assertTrue(downloader.add_peer(late))
        self.assertEqual(late.announced_pieces, [0])
        self.assertTrue(downloader.add_peer(informed))
        self.assertEqual(informed.announced_pieces, [])
        self.assertFalse(downloader.add_peer(disconnected))
        self.assertTrue(disconnected.closed)
        self.assertNotIn(disconnected, downloader.snapshot_peers())

    def test_bitfield_contains_only_verified_pieces_and_zero_padding(self):
        manager = self.manager(torrent_bytes(payload=b"A" * 36))
        self.assertEqual(manager.get_bitfield(), b"\x00\x00")
        manager.init_piece_download(1)
        manager.add_block(1, 0, b"AAAA")
        manager.init_piece_download(8)
        manager.add_block(8, 0, b"AAAA")
        manager.init_piece_download(0)
        self.assertEqual(manager.get_bitfield(), b"\x40\x80")

    def test_interest_tracks_remote_availability_and_local_verification(self):
        manager = self.manager(torrent_bytes(payload=b"ABCDEFGH"))
        peer = FakePeer()
        available = {0}
        peer.has_piece = lambda index: index in available
        self.assertTrue(manager.needs_pieces_from(peer))
        manager.init_piece_download(0)
        manager.add_block(0, 0, b"ABCD")
        self.assertFalse(manager.needs_pieces_from(peer))
        available.add(1)
        self.assertTrue(manager.needs_pieces_from(peer))

    def test_bad_hash_and_invalid_block_are_retryable(self):
        for message in [(7, b"short"), (7, struct.pack(">II", 0, 0) + b"BAD!"),
                        (7, struct.pack(">II", 0, 0) + b"x"), (0, b"")]:
            manager = self.manager()
            peer = FakePeer()
            peer.receive_message = Mock(return_value=message)
            downloader = Downloader(manager.torrent, [peer], piece_manager=manager)
            with self.subTest(message=message), patch.object(Downloader, "CHOKE_TIMEOUT", 0.01):
                self.assertFalse(downloader.download_piece(peer, 0))
                self.assertFalse(manager.has_pending())
                self.assertFalse(manager.has_piece(0))
                self.assertFalse(peer.announced_pieces)

    def test_choke_reissues_only_missing_blocks_and_ignores_duplicates(self):
        payload = b"A" * PieceManager.BLOCK_SIZE + b"Z"
        manager = self.manager(torrent_bytes(payload=payload, piece_length=len(payload)))
        peer = FakePeer()
        first = (7, struct.pack(">II", 0, 0) + payload[:-1])
        last = (7, struct.pack(">II", 0, PieceManager.BLOCK_SIZE) + b"Z")
        peer.receive_message = Mock(side_effect=[first, (0, b""), (1, b""), first, last])
        downloader = Downloader(manager.torrent, [peer], piece_manager=manager)
        self.assertTrue(downloader.download_piece(peer, 0))
        self.assertEqual(peer.requests, [(0, 0, PieceManager.BLOCK_SIZE), (0, PieceManager.BLOCK_SIZE, 1),
                                        (0, PieceManager.BLOCK_SIZE, 1)])
        self.assertEqual(manager.get_progress()["downloaded_bytes"], len(payload))
        self.assertFalse(manager.has_pending())

    def test_repeated_choke_is_bounded_and_stop_while_choked_is_prompt(self):
        for cancel in [False, True]:
            manager = self.manager()
            peer = FakePeer()
            stop = threading.Event()
            clock = [0]

            def monotonic():
                clock[0] += 1
                return clock[0]

            def receive(**kwargs):
                if cancel:
                    stop.set()
                return 0, b""

            peer.receive_message = Mock(side_effect=receive)
            downloader = Downloader(manager.torrent, [peer], piece_manager=manager, stop_event=stop)
            with patch("src.downloader.time", SimpleNamespace(monotonic=monotonic)), \
                    patch.object(Downloader, "CHOKE_TIMEOUT", 5):
                self.assertFalse(downloader.download_piece(peer, 0))
            self.assertLess(peer.receive_message.call_count, 10)
            self.assertFalse(manager.has_pending())
            self.assertEqual(downloader.peer_inflight[peer], 0)

    def test_duplicate_blocks_do_not_count_as_progress(self):
        payload = b"A" * (PieceManager.BLOCK_SIZE + 1)
        manager = self.manager(torrent_bytes(payload=payload, piece_length=len(payload)))
        peer = FakePeer()
        first = (7, struct.pack(">II", 0, 0) + payload[:-1])
        last = (7, struct.pack(">II", 0, PieceManager.BLOCK_SIZE) + payload[-1:])
        peer.receive_message = Mock(side_effect=[first, first, last])
        downloader = Downloader(manager.torrent, [peer], piece_manager=manager)
        self.assertTrue(downloader.download_piece(peer, 0))
        self.assertEqual(peer.receive_message.call_count, 3)

    def test_socket_failure_and_cancellation_release_reservation(self):
        for cancelled in [False, True]:
            manager = self.manager()
            peer = FakePeer()
            peer.receive_message = Mock(side_effect=ConnectionError("closed"))
            stop = threading.Event()
            if cancelled:
                stop.set()
            downloader = Downloader(manager.torrent, [peer], piece_manager=manager, stop_event=stop)
            self.assertFalse(downloader.download_piece(peer, 0))
            self.assertFalse(manager.has_pending())

    def test_empty_torrent_creates_empty_file(self):
        manager = self.manager(torrent_bytes(payload=b""))
        manager.save_to_disk()
        self.assertEqual((manager.download_dir / "data.bin").read_bytes(), b"")

    def test_large_torrent_is_saved_with_bounded_memory_and_releases_storage(self):
        piece_length, count = 1024 * 1024, 32
        block = b"A" * PieceManager.BLOCK_SIZE
        digest = hashlib.sha1(block * (piece_length // len(block))).digest()
        raw = BencodeEncoder.encode({"announce": b"https://tracker.invalid/announce", "info": {
            "name": b"large.bin", "piece length": piece_length, "length": piece_length * count,
            "pieces": digest * count}})
        tracemalloc.start()
        try:
            manager = self.manager(raw)
            for index in range(count):
                for offset, length in manager.init_piece_download(index):
                    manager.add_block(index, offset, block[:length])
            self.assertEqual(manager.get_progress()["downloaded_bytes"], piece_length * count)
            self.assertEqual(manager.read_verified_block(count - 1, 0, len(block)), block)
            manager.save_to_disk()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * 1024 * 1024)
        self.assertTrue(manager.closed)
        with (manager.download_dir / "large.bin").open("rb") as saved:
            for _ in range(count):
                self.assertEqual(hashlib.sha1(saved.read(piece_length)).digest(), digest)
            self.assertEqual(saved.read(1), b"")
        self.assertEqual(list(manager.download_dir.glob(".minitorrent-*")), [])

    def test_pending_piece_budget_is_bounded(self):
        manager = self.manager(torrent_bytes(payload=b"A" * 84))
        for _ in range(manager.MAX_PENDING_PIECES):
            self.assertIsNotNone(manager.reserve_next_piece(FakePeer()))
        self.assertIsNone(manager.reserve_next_piece(FakePeer()))
        manager.abort_piece(0)
        self.assertIsNotNone(manager.reserve_next_piece(FakePeer()))

    def test_out_of_order_pieces_do_not_create_holes_and_assemble_in_torrent_order(self):
        payload = b"AAAABBBBCCCC"
        manager = self.manager(torrent_bytes(payload=payload))
        for index in [2, 0, 1]:
            manager.init_piece_download(index)
            manager.add_block(index, 0, payload[index * 4:index * 4 + 4])
            manager._storage.flush()
            self.assertEqual(os.fstat(manager._storage.fileno()).st_size,
                             manager.get_progress()["downloaded_bytes"])
        manager.save_to_disk()
        self.assertEqual((manager.download_dir / "data.bin").read_bytes(), payload)

    def test_short_block_write_is_not_marked_verified(self):
        manager = self.manager()
        manager.init_piece_download(0)
        with patch.object(manager._storage, "write", return_value=1):
            with self.assertRaises(OSError):
                manager.add_block(0, 0, b"ABCD")
        self.assertFalse(manager.has_piece(0))
        self.assertEqual(manager.get_progress()["downloaded_bytes"], 0)

    def test_unverified_disk_blocks_cannot_be_read_and_failed_hash_is_retryable(self):
        manager = self.manager()
        manager.init_piece_download(0)
        self.assertFalse(manager.add_block(0, 0, b"BAD!"))
        with self.assertRaises(ValueError):
            manager.read_verified_block(0, 0, 4)
        manager.init_piece_download(0)
        self.assertTrue(manager.add_block(0, 0, b"ABCD"))
        self.assertEqual(manager.read_verified_block(0, 0, 4), b"ABCD")

    def test_disk_failure_preserves_existing_output_and_allows_save_retry(self):
        manager = self.manager()
        self.complete(manager)
        target = manager.download_dir / "data.bin"
        target.write_bytes(b"original")
        with patch("src.piece_manager.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                manager.save_to_disk()
        self.assertEqual(target.read_bytes(), b"original")
        self.assertFalse(manager.closed)
        self.assertEqual(list(manager.download_dir.glob(".minitorrent-*")),
                         [Path(manager._storage.name)] if isinstance(manager._storage.name, str) else [])
        manager.save_to_disk()
        self.assertEqual(target.read_bytes(), b"ABCD")
        self.assertTrue(manager.closed)

    def test_cancel_during_disk_copy_preserves_output_and_can_resume_save(self):
        payload = b"A" * (PieceManager.COPY_SIZE + 1)
        manager = self.manager(torrent_bytes(payload=payload, piece_length=len(payload)))
        self.complete(manager, payload)
        target = manager.download_dir / "data.bin"
        target.write_bytes(b"original")
        stop = threading.Event()
        read = manager._read_at

        def cancel_after_read(offset, length):
            content = read(offset, length)
            stop.set()
            return content

        with patch.object(manager, "_read_at", side_effect=cancel_after_read):
            with self.assertRaises(InterruptedError):
                manager.save_to_disk(stop_event=stop)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertFalse(manager.closed)
        stop.clear()
        manager.save_to_disk(stop_event=stop)
        self.assertEqual(target.read_bytes(), payload)


class TrackerTests(unittest.TestCase):
    def tracker(self, url="https://tracker.invalid/announce"):
        return TrackerClient(SimpleNamespace(announce=url, info_hash=bytes(range(20)), total_size=4))

    def response(self, content=b"de"):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [content]
        return response

    def test_preserves_existing_query_and_binary_hash(self):
        tracker = self.tracker("https://tracker.invalid/announce?passkey=a%2Bb&empty=&info_hash=wrong")
        with patch("src.tracker.requests.get", return_value=self.response()) as get:
            tracker.announce()
        query = parse_qs(urlsplit(get.call_args.args[0]).query, keep_blank_values=True, encoding="latin1")
        self.assertEqual(query["passkey"], ["a+b"])
        self.assertEqual(query["empty"], [""])
        self.assertEqual(query["info_hash"][0].encode("latin1"), tracker.torrent.info_hash)
        self.assertEqual(len(query["info_hash"]), 1)

    def test_rejects_unsafe_url_without_request(self):
        for url in ["file:///secret", "http://user:password@tracker.invalid", "https://", "http://host/\n"]:
            with self.subTest(url=url), patch("src.tracker.requests.get") as get, self.assertRaises(ValueError):
                self.tracker(url).announce()
            get.assert_not_called()

    def test_rejects_oversized_response_and_malformed_peers(self):
        with patch("src.tracker.requests.get", return_value=self.response(b"x" * (BencodeDecoder.MAX_SIZE + 1))):
            with self.assertRaises(ValueError):
                self.tracker().announce()
        with self.assertRaises(ValueError):
            self.tracker()._parse_peers(b"short")
        with self.assertRaises(ValueError):
            self.tracker()._parse_peers([{"ip": b"127.0.0.1", "port": 70000}])


class PeerTests(unittest.TestCase):
    def peer(self, data=b"", piece_count=10):
        peer = PeerConnection("127.0.0.1", 6881, b"i" * 20, b"p" * 20, piece_count)
        peer.socket = Mock()
        chunks = bytearray(data)

        def recv(count):
            result = bytes(chunks[:count])
            del chunks[:count]
            return result

        peer.socket.recv.side_effect = recv
        return peer

    def test_timeout_is_reported_without_generic_exception(self):
        peer = self.peer()
        peer.socket.recv.side_effect = socket.timeout()
        self.assertIsNone(peer.receive_message())

    def test_partial_frame_survives_timeout_in_header_or_payload(self):
        frame = struct.pack(">IBI", 5, 4, 9)
        for split in [1, 2, 3, 4, 5, 6, 8]:
            with self.subTest(split=split):
                peer = self.peer()
                chunks = bytearray(frame)
                read_bytes = 0
                timed_out = False

                def recv(count):
                    nonlocal read_bytes, timed_out
                    if read_bytes == split and not timed_out:
                        timed_out = True
                        raise socket.timeout()
                    limit = count if timed_out else min(count, split - read_bytes)
                    result = bytes(chunks[:limit])
                    del chunks[:limit]
                    read_bytes += len(result)
                    return result

                peer.socket.recv.side_effect = recv
                self.assertIsNone(peer.receive_message())
                self.assertEqual(peer.receive_message(), (4, struct.pack(">I", 9)))
                self.assertTrue(peer.has_piece(9))
                self.assertFalse(peer._receive_buffer)

    def test_have_updates_available_pieces(self):
        peer = self.peer(struct.pack(">IBI", 5, 4, 9))
        peer.receive_message()
        self.assertTrue(peer.has_piece(9))

    def test_bitfield_is_bounded_before_payload_read(self):
        peer = self.peer(struct.pack(">IB", 1025, 5), piece_count=1)
        with self.assertRaises(ValueError):
            peer.receive_message()
        self.assertEqual(peer.socket.recv.call_count, 2)
        self.assertFalse(peer.peer_pieces)

    def test_bitfield_padding_and_have_indices_are_validated(self):
        for frame in [struct.pack(">IB", 2, 5) + b"\xff", struct.pack(">IBI", 5, 4, 1),
                      struct.pack(">IBI", 5, 4, 2**32 - 1)]:
            peer = self.peer(frame, piece_count=1)
            with self.subTest(frame=frame), self.assertRaises(ValueError):
                peer.receive_message()
            self.assertFalse(peer.peer_pieces)
        peer = self.peer(struct.pack(">IB", 3, 5) + b"\x80\x40", piece_count=10)
        peer.receive_message()
        self.assertEqual(peer.peer_pieces, {0, 9})

    def test_have_and_bitfield_are_encoded_as_protocol_frames(self):
        peer = self.peer()
        peer.send_bitfield(b"\x80\x00")
        peer.send_have(9)
        self.assertEqual([call.args[0] for call in peer.socket.sendall.call_args_list],
                         [struct.pack(">IB", 3, 5) + b"\x80\x00", struct.pack(">IBI", 5, 4, 9)])

    def test_parallel_announcements_and_requests_do_not_interleave_frames(self):
        peer = self.peer()
        wire = bytearray()
        start = threading.Barrier(2)

        def sendall(frame):
            wire.extend(frame[:3])
            time.sleep(0.001)  # Yield while a simulated socket write is partial.
            wire.extend(frame[3:])

        peer.socket.sendall.side_effect = sendall

        def announce():
            start.wait()
            for index in range(10):
                peer.send_have(index)

        def request():
            start.wait()
            for index in range(10):
                peer.request_piece(index, 0, 16384)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(announce), executor.submit(request)]
            for future in futures:
                future.result(timeout=3)
        frames = []
        while wire:
            length = struct.unpack(">I", wire[:4])[0]
            self.assertIn(length, [5, 13])
            frames.append(bytes(wire[4:4 + length]))
            del wire[:4 + length]
        self.assertCountEqual(frames, [struct.pack(">BI", 4, index) for index in range(10)] +
                              [struct.pack(">BIII", 6, index, 0, 16384) for index in range(10)])

    def test_message_length_is_bounded_before_payload_read(self):
        peer = self.peer(struct.pack(">I", PeerConnection.MAX_MESSAGE_LENGTH + 1))
        with self.assertRaises(ValueError):
            peer.receive_message()
        self.assertEqual(peer.socket.recv.call_count, 1)

    def test_messages_use_sendall_and_close_interrupts_receive(self):
        peer = self.peer()
        peer.send_interested()
        peer.socket.sendall.assert_called_once()
        peer.socket.send.assert_not_called()
        peer.close()
        peer.socket.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        peer.socket.close.assert_called_once()
