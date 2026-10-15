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
    sent_bitfield = b""

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
            raise ConnectionError("Test peer closed")
        index, offset, length = self.requests.pop(0)
        absolute_offset = index * 4 + offset
        return 7, struct.pack(">II", index, offset) + self.payload[absolute_offset:absolute_offset + length]

    def close(self):
        self.closed = True
        self.release.set()


def serve_seed(listener, torrent, payload):
    """One real TCP seed; choke once and drop a request to exercise recovery."""
    connection, _ = listener.accept()
    with connection:
        connection.settimeout(3)

        def receive_exact(length):
            content = bytearray()
            while len(content) < length:
                try:
                    chunk = connection.recv(length - len(content))
                except ConnectionResetError:
                    return None
                if not chunk:
                    if content:
                        raise ConnectionError("Truncated test peer frame")
                    return None
                content.extend(chunk)
            return bytes(content)

        handshake = receive_exact(68)
        if handshake is None or handshake[28:48] != torrent.info_hash:
            raise AssertionError("Invalid client handshake")
        connection.sendall(handshake[:48] + b"s" * 20)
        count = len(torrent.pieces)
        bitfield = bytearray((count + 7) // 8)
        for index in range(count):
            bitfield[index // 8] |= 1 << (7 - index % 8)
        connection.sendall(struct.pack(">IB", len(bitfield) + 1, 5) + bitfield + struct.pack(">IB", 1, 1))
        requests = 0
        while True:
            header = receive_exact(4)
            if header is None:
                return requests
            length = struct.unpack(">I", header)[0]
            if length == 0:
                continue
            if length > 1024 * 1024:
                raise AssertionError("Invalid client message length")
            message = receive_exact(length)
            if message is None:
                raise ConnectionError("Truncated client message")
            if message[0] != 6:
                continue
            index, offset, block_length = struct.unpack(">III", message[1:])
            start = index * torrent.piece_length + offset
            if not (0 <= index < count and 0 < block_length <= 16384 and offset >= 0
                    and offset + block_length <= min(torrent.piece_length, len(payload) - index * torrent.piece_length)):
                raise AssertionError("Invalid client block request")
            requests += 1
            if requests == 1:
                connection.sendall(struct.pack(">IBIB", 1, 0, 1, 1))
                continue
            block = payload[start:start + block_length]
            frame = struct.pack(">IBII", 9 + len(block), 7, index, offset) + block
            try:
                connection.sendall(frame)
            except ConnectionError:
                return requests
