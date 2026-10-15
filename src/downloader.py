from collections import deque
import struct
import threading
import time

from .piece_manager import PieceManager


class Downloader:
    MAX_INFLIGHT_PER_PEER = 10
    BLOCK_TIMEOUT = 15
    CHOKE_TIMEOUT = 120

    def __init__(self, torrent, peers, piece_manager=None, stop_event=None):
        self.torrent = torrent
        self.peers = list(peers)
        self._peers_lock = threading.Lock()
        self.piece_manager = piece_manager if piece_manager is not None else PieceManager(torrent)
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.peer_inflight = {peer: 0 for peer in peers}

    def add_peer(self, peer):
        with self._peers_lock:
            self.peers.append(peer)
            self.peer_inflight[peer] = 0
        # Pieces may finish between this connection's initial BITFIELD and its
        # admission to the worker pool. Subscribe first, then catch up the gap.
        try:
            for index in self.piece_manager.verified_piece_indices():
                byte, bit = divmod(index, 8)
                if byte >= len(peer.sent_bitfield) or not peer.sent_bitfield[byte] & (1 << (7 - bit)):
                    peer.send_have(index)
        except OSError:
            self.remove_peer(peer)
            peer.close()
            return False
        return True

    def remove_peer(self, peer):
        with self._peers_lock:
            self.peers.remove(peer)

    def snapshot_peers(self):
        with self._peers_lock:
            return tuple(self.peers)

    def download_piece(self, peer, piece_index, blocks=None):
        if blocks is None:
            blocks = self.piece_manager.init_piece_download(piece_index)
        outstanding = {}
        requested = {}
        received = set()
        pending = deque(blocks)
        choked = peer.peer_choking
        deadline = time.monotonic() + (self.CHOKE_TIMEOUT if choked else self.BLOCK_TIMEOUT)
        try:
            while len(received) < len(blocks):
                if self.stop_event.is_set() or time.monotonic() >= deadline:
                    return False
                while not choked and pending and len(outstanding) < self.MAX_INFLIGHT_PER_PEER:
                    offset, length = pending.popleft()
                    if offset in received:
                        continue
                    peer.request_piece(piece_index, offset, length)
                    outstanding[offset] = length
                    requested[offset] = length
                self.peer_inflight[peer] = len(outstanding)
                message = peer.receive_message(timeout=max(0.01, min(1, deadline - time.monotonic())))
                if message is None:
                    continue
                message_type, payload = message
                if message_type == 0:
                    if not choked:
                        choked = peer.peer_choking = True
                        deadline = time.monotonic() + self.CHOKE_TIMEOUT
                        outstanding.clear()
                        self.peer_inflight[peer] = 0
                        pending = deque((offset, length) for offset, length in blocks if offset not in received)
                    continue
                if message_type == 1:
                    if choked:
                        choked = peer.peer_choking = False
                        deadline = time.monotonic() + self.BLOCK_TIMEOUT
                    continue
                if message_type != 7:
                    continue
                if len(payload) < 8:
                    return False
                index, begin = struct.unpack(">II", payload[:8])
                block = payload[8:]
                if index != piece_index:
                    if 0 <= index < len(self.piece_manager.have_pieces) and self.piece_manager.has_piece(index):
                        continue
                    return False
                if begin in received:
                    # A repeated response must not count as a new block.
                    continue
                if begin not in requested or len(block) != requested[begin]:
                    return False
                self.piece_manager.add_block(piece_index, begin, block)
                outstanding.pop(begin, None)
                self.peer_inflight[peer] = len(outstanding)
                received.add(begin)
                deadline = time.monotonic() + (self.CHOKE_TIMEOUT if choked else self.BLOCK_TIMEOUT)
            if not self.piece_manager.has_piece(piece_index):
                return False
            # Super-seeding peers may wait for HAVE before offering more data.
            # Announce only after SHA-1 verification, to every connected peer.
            for target in self.snapshot_peers():
                try:
                    target.send_have(piece_index)
                except OSError:
                    # A closed observer must not discard an already valid piece.
                    pass
            return True
        except (OSError, ValueError, struct.error):
            return False
        finally:
            self.peer_inflight[peer] = 0
            self.piece_manager.abort_piece(piece_index)

    def download_pieces(self, num_pieces=5):
        downloaded = 0
        for peer in self.snapshot_peers():
            if downloaded >= num_pieces or self.stop_event.is_set():
                break
            reservation = self.piece_manager.reserve_next_piece(peer)
            if reservation is not None and self.download_piece(peer, *reservation):
                downloaded += 1
        return downloaded
