# src/peer.py
'''
for peer connection. Performing handshake
and exchanging torrent protocol messages like
"interested", "choke"
Also handles bitfield to get pieces from other peers
'''

import socket
import struct
import threading
from enum import IntEnum

class MessageType(IntEnum):
    CHOKE = 0
    UNCHOKE = 1
    INTERESTED = 2
    NOT_INTERESTED = 3
    HAVE = 4
    BITFIELD = 5
    REQUEST = 6
    PIECE = 7
    CANCEL = 8


class PeerConnection:
    MAX_MESSAGE_LENGTH = 1024 * 1024

    def __init__(self, ip, port, info_hash, peer_id, piece_count):
        if type(piece_count) is not int or piece_count < 0:
            raise ValueError("Invalid piece count")
        self.ip = ip
        self.port = port
        self.info_hash = info_hash
        self.peer_id = peer_id
        self.piece_count = piece_count
        self.socket = None
        self.connected = False

        self.am_choking = True
        self.am_interested = False
        self.peer_choking = True
        self.peer_interested = False
        self.peer_pieces = set()  #which peer has piece
        self.sent_bitfield = b""
        self._receive_buffer = bytearray()
        self._send_lock = threading.Lock()

    def connect(self, timeout=5): #TCP peer connection
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(timeout)
            self.socket.connect((self.ip, self.port))
            self.connected = True
            return True
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            self.close()
            return False

    def handshake(self):
        if not self.connected:
            raise ConnectionError("Not connected to peer")

        pstr = b"BitTorrent protocol"
        pstrlen = 19
        reserved = b'\x00' * 8

        handshake_msg = (
                struct.pack("B", pstrlen) +
                pstr +
                reserved +
                self.info_hash +
                self.peer_id
        )

        self.socket.sendall(handshake_msg)

        response = self._recv_exactly(68)

        if len(response) < 68:
            raise ValueError("Invalid handshake response length")

        recv_pstrlen = response[0]
        recv_pstr = response[1:20]
        recv_reserved = response[20:28]
        recv_info_hash = response[28:48]
        recv_peer_id = response[48:68]

        #validation checks from malicious peers
        if recv_pstrlen != pstrlen or recv_pstr != pstr:
            raise ValueError("Invalid peer protocol")

        if recv_info_hash != self.info_hash:
            raise ValueError("Info hash mismatch")

        return True

    def _recv_exactly(self, n): #receive exactly n bytes from TCP-socket
        self._buffer_until(n)
        data = bytes(self._receive_buffer[:n])
        del self._receive_buffer[:n]
        return data

    def _buffer_until(self, length):
        while len(self._receive_buffer) < length:
            chunk = self.socket.recv(length - len(self._receive_buffer))
            if not chunk:
                raise ConnectionError("Connection closed by peer")
            self._receive_buffer.extend(chunk)

    def send_interested(self):
        self._send_message(MessageType.INTERESTED)
        self.am_interested = True

    def send_not_interested(self):
        self._send_message(MessageType.NOT_INTERESTED)
        self.am_interested = False

    def send_unchoke(self):
        self._send_message(MessageType.UNCHOKE)
        self.am_choking = False

    def send_choke(self):
        self._send_message(MessageType.CHOKE)
        self.am_choking = True

    def send_have(self, piece_index):
        self._send_message(MessageType.HAVE, struct.pack(">I", piece_index))

    def send_bitfield(self, bitfield):
        self._send_message(MessageType.BITFIELD, bitfield)
        self.sent_bitfield = bytes(bitfield)

    def _send_message(self, message_type, payload=b''):
        length = 1 + len(payload)
        message = struct.pack(">I", length) + struct.pack("B", message_type) + payload
        # HAVE broadcasts can share this socket with its download worker.
        with self._send_lock:
            self.socket.sendall(message)

    def receive_message(self, timeout=5):
        self.socket.settimeout(timeout)

        try:
            # Consume a frame only after it is complete. A socket timeout can
            # happen halfway through its header or payload; keep those bytes
            # for the next receive instead of losing framing synchronization.
            self._buffer_until(4)
            length = struct.unpack(">I", self._receive_buffer[:4])[0]
            if length > self.MAX_MESSAGE_LENGTH:
                raise ValueError("Peer message exceeds size limit")

            if length == 0:
                del self._receive_buffer[:4]
                return (None, b'')

            self._buffer_until(5)
            message_id = self._receive_buffer[4]
            if message_id == MessageType.BITFIELD and length != 1 + (self.piece_count + 7) // 8:
                raise ValueError("Bitfield does not match torrent metadata")
            if message_id == MessageType.HAVE and length != 5:
                raise ValueError("Invalid HAVE message")
            if message_id in {0, 1, 2, 3} and length != 1:
                raise ValueError("Invalid control message")
            self._buffer_until(4 + length)
            payload = bytes(self._receive_buffer[5:4 + length])
            del self._receive_buffer[:4 + length]

            if message_id == MessageType.CHOKE:
                self.peer_choking = True
            elif message_id == MessageType.UNCHOKE:
                self.peer_choking = False
            elif message_id == MessageType.INTERESTED:
                self.peer_interested = True
            elif message_id == MessageType.NOT_INTERESTED:
                self.peer_interested = False
            elif message_id == MessageType.BITFIELD:
                self._handle_bitfield(payload)
            elif message_id == MessageType.HAVE:
                if len(payload) != 4:
                    raise ValueError("Invalid HAVE message")
                index = struct.unpack(">I", payload)[0]
                if index >= self.piece_count:
                    raise ValueError("HAVE index exceeds torrent metadata")
                self.peer_pieces.add(index)

            return (message_id, payload)

        except socket.timeout:
            return None

    def _handle_bitfield(self, bitfield): #bitfield, what pieces peer has
        if len(bitfield) != (self.piece_count + 7) // 8:
            raise ValueError("Bitfield does not match torrent metadata")
        padding = (-self.piece_count) % 8
        if padding and bitfield[-1] & ((1 << padding) - 1):
            raise ValueError("Nonzero bitfield padding")
        self.peer_pieces = set()

        for byte_index, byte in enumerate(bitfield):
            for bit_index in range(8):
                if byte & (1 << (7 - bit_index)): #Most significant byte
                    piece_index = byte_index * 8 + bit_index
                    self.peer_pieces.add(piece_index)

    def has_piece(self, piece_index):
        return piece_index in self.peer_pieces

    def request_piece(self, piece_index, begin, length):
        payload = struct.pack(">III", piece_index, begin, length)
        self._send_message(MessageType.REQUEST, payload)

    def close(self):
        if self.socket:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.socket.close()
            except OSError:
                pass
        self.connected = False

    def __repr__(self): #sttroka
        status = "connected" if self.connected else "disconnected"
        return f"Peer({self.ip}:{self.port}, {status})"
