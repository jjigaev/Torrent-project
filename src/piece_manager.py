import hashlib
import os
from pathlib import Path
import tempfile
import threading

from .paths import safe_join


class PieceManager:
    BLOCK_SIZE = 16384
    COPY_SIZE = 65536
    MAX_PENDING_PIECES = 20

    def __init__(self, torrent, download_dir="downloads"):
        self.torrent = torrent
        self.download_dir = Path(download_dir).resolve()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._piece_hashes = tuple(torrent.pieces)
        self.have_pieces = [False] * len(self._piece_hashes)
        self._downloaded_bytes = 0
        self._piece_offsets = {}
        self._next_storage_offset = 0
        self.pending_blocks = {}
        self._lock = threading.RLock()
        self._storage = tempfile.TemporaryFile(mode="w+b", dir=self.download_dir, prefix=".minitorrent-parts-")
        self._saved = False

    @property
    def closed(self):
        return self._storage.closed

    def close(self):
        with self._lock:
            self.pending_blocks.clear()
            self._storage.close()
            self._piece_offsets.clear()
            self._piece_hashes = ()

    def get_next_piece_to_download(self, peer):
        with self._lock:
            for index in range(len(self.have_pieces)):
                if not self.have_pieces[index] and index not in self.pending_blocks and peer.has_piece(index):
                    return index
        return None

    def reserve_next_piece(self, peer):
        # Selection and reservation must be atomic across peer workers.
        with self._lock:
            if len(self.pending_blocks) >= self.MAX_PENDING_PIECES:
                return None
            index = self.get_next_piece_to_download(peer)
            if index is None:
                return None
            return index, self.init_piece_download(index)

    def init_piece_download(self, piece_index):
        with self._lock:
            if self.closed:
                raise ValueError("Piece storage is closed")
            if not 0 <= piece_index < len(self.have_pieces):
                raise ValueError("Invalid piece index")
            if self.have_pieces[piece_index] or piece_index in self.pending_blocks:
                raise ValueError("Piece is already reserved or complete")
            if len(self.pending_blocks) >= self.MAX_PENDING_PIECES:
                raise ValueError("Too many pending pieces")
            length = self.get_piece_length(piece_index)
            blocks = [(offset, min(self.BLOCK_SIZE, length - offset))
                      for offset in range(0, length, self.BLOCK_SIZE)]
            if piece_index not in self._piece_offsets:
                # Allocate in arrival order, avoiding multi-GB holes when a seed
                # first offers a high piece index on a filesystem without sparse files.
                self._piece_offsets[piece_index] = self._next_storage_offset
                self._next_storage_offset += length
            self.pending_blocks[piece_index] = {offset: False for offset, _ in blocks}
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
            if len(block_data) != expected or blocks[block_offset]:
                raise ValueError("Invalid or duplicate block")
            self._storage.seek(self._piece_offsets[piece_index] + block_offset)
            if self._storage.write(block_data) != len(block_data):
                raise OSError("Incomplete piece write")
            blocks[block_offset] = True
            if all(blocks.values()):
                return self._complete_piece(piece_index)
            return False

    def _complete_piece(self, piece_index):
        self.pending_blocks.pop(piece_index)
        digest = hashlib.sha1()
        remaining = self.get_piece_length(piece_index)
        self._storage.seek(self._piece_offsets[piece_index])
        while remaining:
            block = self._storage.read(min(self.BLOCK_SIZE, remaining))
            if not block:
                raise OSError("Incomplete piece storage")
            digest.update(block)
            remaining -= len(block)
        if digest.digest() != self._piece_hashes[piece_index]:
            return False
        self.have_pieces[piece_index] = True
        self._downloaded_bytes += self.get_piece_length(piece_index)
        return True

    def verified_piece_indices(self):
        with self._lock:
            return tuple(index for index, complete in enumerate(self.have_pieces) if complete)

    def read_verified_block(self, piece_index, offset, length):
        with self._lock:
            if (self.closed or not 0 <= piece_index < len(self.have_pieces) or not self.have_pieces[piece_index]
                    or offset < 0 or not 0 < length <= self.BLOCK_SIZE
                    or offset + length > self.get_piece_length(piece_index)):
                raise ValueError("Invalid verified block")
            return self._read_at(piece_index * self.torrent.piece_length + offset, length)

    def _read_at(self, offset, length):
        with self._lock:
            content = bytearray()
            while length:
                index, begin = divmod(offset, self.torrent.piece_length)
                count = min(length, self.get_piece_length(index) - begin)
                self._storage.seek(self._piece_offsets[index] + begin)
                block = self._storage.read(count)
                if len(block) != count:
                    raise OSError("Incomplete piece storage")
                content.extend(block)
                offset += count
                length -= count
            return bytes(content)

    def get_progress(self):
        with self._lock:
            downloaded = self._downloaded_bytes
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
            if self._saved:
                return
            if not all(self.have_pieces):
                raise ValueError("Cannot save an incomplete torrent")

        if self.torrent.is_multi_file:
            files = [(safe_join(self.download_dir, self.torrent.name, *item["path"]), item["length"])
                     for item in self.torrent.data["info"]["files"]]
        else:
            files = [(safe_join(self.download_dir, self.torrent.name), self.torrent.total_size)]

        # Preflight every destination before writing any output.
        absolute_offset = 0
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
                        count = min(remaining, self.COPY_SIZE)
                        output.write(self._read_at(absolute_offset, count))
                        remaining -= count
                        absolute_offset += count
                if stop_event is not None and stop_event.is_set():
                    raise InterruptedError("Download stopped")
                safe_join(self.download_dir, *relative.parts)
                os.replace(temporary, destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        with self._lock:
            self._saved = True
            self.close()

