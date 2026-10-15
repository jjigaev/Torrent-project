import struct
import threading
import time

from .piece_manager import PieceManager


class Downloader:
    MAX_INFLIGHT_PER_PEER = 10
    BLOCK_TIMEOUT = 15

    def __init__(self, torrent, peers, piece_manager=None, stop_event=None):
        self.torrent = torrent
        self.peers = peers
        self.piece_manager = piece_manager if piece_manager is not None else PieceManager(torrent)
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.peer_inflight = {peer: 0 for peer in peers}

    def download_piece(self, peer, piece_index, blocks=None):
        if blocks is None:
            blocks = self.piece_manager.init_piece_download(piece_index)
        outstanding = {}
        requested = received = 0
        deadline = time.monotonic() + self.BLOCK_TIMEOUT
        try:
            while received < len(blocks):
                if self.stop_event.is_set() or time.monotonic() >= deadline:
                    return False
                while requested < len(blocks) and len(outstanding) < self.MAX_INFLIGHT_PER_PEER:
                    offset, length = blocks[requested]
                    peer.request_piece(piece_index, offset, length)
                    outstanding[offset] = length
                    requested += 1
                self.peer_inflight[peer] = len(outstanding)
                message = peer.receive_message(timeout=max(0.01, deadline - time.monotonic()))
                if message is None:
                    return False
                message_type, payload = message
                if message_type == 0:
                    return False
                if message_type != 7:
                    continue
                if len(payload) < 8:
                    return False
                index, begin = struct.unpack(">II", payload[:8])
                block = payload[8:]
                if index != piece_index:
                    return False
                if begin not in outstanding:
                    # A repeated response must not count as a new block.
                    continue
                if len(block) != outstanding[begin]:
                    return False
                self.piece_manager.add_block(piece_index, begin, block)
                del outstanding[begin]
                self.peer_inflight[peer] = len(outstanding)
                received += 1
                deadline = time.monotonic() + self.BLOCK_TIMEOUT
            if not self.piece_manager.has_piece(piece_index):
                return False
            # Super-seeding peers may wait for HAVE before offering more data.
            # Announce only after SHA-1 verification, to every connected peer.
            for target in self.peers:
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
        for peer in self.peers:
            if downloaded >= num_pieces or self.stop_event.is_set():
                break
            reservation = self.piece_manager.reserve_next_piece(peer)
            if reservation is not None and self.download_piece(peer, *reservation):
                downloaded += 1
        return downloaded
