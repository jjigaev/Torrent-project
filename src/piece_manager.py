import hashlib
import os
from pathlib import Path
import tempfile
import threading

from .paths import safe_join


class PieceManager:
    BLOCK_SIZE = 16384

    def __init__(self, torrent, download_dir="downloads"):
        self.torrent = torrent
        self.download_dir = Path(download_dir).resolve()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.have_pieces = [False] * len(torrent.pieces)
        self.piece_data = {}
        self.pending_blocks = {}
        self._lock = threading.RLock()

    def get_next_piece_to_download(self, peer):
        with self._lock:
            for index in range(len(self.have_pieces)):
                if not self.have_pieces[index] and index not in self.pending_blocks and peer.has_piece(index):
                    return index
        return None

    def reserve_next_piece(self, peer):
        # Selection and reservation must be atomic across peer workers.
        with self._lock:
            index = self.get_next_piece_to_download(peer)
            if index is None:
                return None
            return index, self.init_piece_download(index)

    def init_piece_download(self, piece_index):
        with self._lock:
            if not 0 <= piece_index < len(self.have_pieces):
                raise ValueError("Invalid piece index")
            if self.have_pieces[piece_index] or piece_index in self.pending_blocks:
                raise ValueError("Piece is already reserved or complete")
            length = self.get_piece_length(piece_index)
            blocks = [(offset, min(self.BLOCK_SIZE, length - offset))
                      for offset in range(0, length, self.BLOCK_SIZE)]
            self.pending_blocks[piece_index] = {offset: None for offset, _ in blocks}
            return blocks

    def abort_piece(self, piece_index):
        with self._lock:
            self.pending_blocks.pop(piece_index, None)

    def has_pending(self):
        with self._lock:
            return bool(self.pending_blocks)

    def has_piece(self, piece_index):
        with self._lock:
            return self.have_pieces[piece_index]

    def get_bitfield(self):
        with self._lock:
            bitfield = bytearray((len(self.have_pieces) + 7) // 8)
            for index, complete in enumerate(self.have_pieces):
                if complete:
                    bitfield[index // 8] |= 1 << (7 - index % 8)
            return bytes(bitfield)

    def needs_pieces_from(self, peer):
        with self._lock:
            return any(not complete and peer.has_piece(index)
                       for index, complete in enumerate(self.have_pieces))

    def get_piece_length(self, piece_index):
        return min(self.torrent.piece_length,
                   self.torrent.total_size - piece_index * self.torrent.piece_length)

    def add_block(self, piece_index, block_offset, block_data):
        with self._lock:
            blocks = self.pending_blocks.get(piece_index)
            if blocks is None or block_offset not in blocks:
                raise ValueError("Unrequested block")
            expected = min(self.BLOCK_SIZE, self.get_piece_length(piece_index) - block_offset)
            if len(block_data) != expected or blocks[block_offset] is not None:
                raise ValueError("Invalid or duplicate block")
            blocks[block_offset] = block_data
            if all(value is not None for value in blocks.values()):
                return self._complete_piece(piece_index)
            return False

    def _complete_piece(self, piece_index):
        blocks = self.pending_blocks.pop(piece_index)
        data = b''.join(blocks[offset] for offset in sorted(blocks))
        if hashlib.sha1(data).digest() != self.torrent.pieces[piece_index]:
            return False
        self.piece_data[piece_index] = data
        self.have_pieces[piece_index] = True
        return True

    def get_progress(self):
        with self._lock:
            downloaded = sum(len(data) for data in self.piece_data.values())
            total = self.torrent.total_size
            return {
                "completed_pieces": sum(self.have_pieces),
                "total_pieces": len(self.have_pieces),
                "percentage": 100 * downloaded / total if total else 100,
                "downloaded_bytes": downloaded,
                "total_bytes": total,
            }

    def save_to_disk(self, stop_event=None):
        with self._lock:
            if not all(self.have_pieces):
                raise ValueError("Cannot save an incomplete torrent")
            pieces = [memoryview(self.piece_data[index]) for index in range(len(self.have_pieces))]

        if self.torrent.is_multi_file:
            files = [(safe_join(self.download_dir, self.torrent.name, *item["path"]), item["length"])
                     for item in self.torrent.data["info"]["files"]]
        else:
            files = [(safe_join(self.download_dir, self.torrent.name), self.torrent.total_size)]

        # Preflight every destination before writing any output.
        piece_index = piece_offset = 0
        for destination, length in files:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("Download stopped")
            destination.parent.mkdir(parents=True, exist_ok=True)
            relative = destination.relative_to(self.download_dir)
            destination = safe_join(self.download_dir, *relative.parts)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".minitorrent-", delete=False) as output:
                    temporary = Path(output.name)
                    remaining = length
                    while remaining:
                        if stop_event is not None and stop_event.is_set():
                            raise InterruptedError("Download stopped")
                        piece = pieces[piece_index]
                        count = min(remaining, len(piece) - piece_offset)
                        output.write(piece[piece_offset:piece_offset + count])
                        remaining -= count
                        piece_offset += count
                        if piece_offset == len(piece):
                            piece_index += 1
                            piece_offset = 0
                if stop_event is not None and stop_event.is_set():
                    raise InterruptedError("Download stopped")
                safe_join(self.download_dir, *relative.parts)
                os.replace(temporary, destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

