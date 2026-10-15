import hashlib
import struct
import threading

from src.bencode import BencodeEncoder


def torrent_bytes(payload=b"ABCD", name=b"data.bin", files=None, piece_length=4):
    info = {
        "name": name,
        "piece length": piece_length,
        "pieces": b"".join(hashlib.sha1(payload[offset:offset + piece_length]).digest()
                            for offset in range(0, len(payload), piece_length)),
    }
    info.update({"length": len(payload)} if files is None else {"files": files})
    return BencodeEncoder.encode({"announce": b"https://tracker.invalid/announce", "info": info})


class FakePeer:
    ip = "127.0.0.1"
    peer_choking = False

    def __init__(self, payload=b"ABCD", block=False):
        self.payload = payload
        self.requests = []
        self.announced_pieces = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.closed = False
        self.am_interested = True

    def has_piece(self, index):
        return True

    def request_piece(self, index, offset, length):
        self.requests.append((index, offset, length))

    def send_have(self, index):
        self.announced_pieces.append(index)

    def send_interested(self):
        self.am_interested = True

    def send_not_interested(self):
        self.am_interested = False

    def receive_message(self, timeout=15):
        self.entered.set()
        if not self.release.wait(3):
            raise TimeoutError("Test peer was not released")
        if self.closed or self.payload is None:
            return None
        index, offset, length = self.requests.pop(0)
        absolute_offset = index * 4 + offset
        return 7, struct.pack(">II", index, offset) + self.payload[absolute_offset:absolute_offset + length]

    def close(self):
        self.closed = True
        self.release.set()
