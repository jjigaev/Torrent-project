from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
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
        return PieceManager(TorrentFile.from_bytes(raw or torrent_bytes()), self.directory / "downloads")

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
            with self.subTest(message=message):
                self.assertFalse(downloader.download_piece(peer, 0))
                self.assertFalse(manager.has_pending())
                self.assertFalse(manager.has_piece(0))
                self.assertFalse(peer.announced_pieces)

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
    def peer(self, data=b""):
        peer = PeerConnection("127.0.0.1", 6881, b"i" * 20, b"p" * 20)
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
