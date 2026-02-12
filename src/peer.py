# src/peer.py
'''
for peer connection. Performing handshake
and exchanging torrent protocol messages like
"interested", "choke"
Also handles bitfield to get pieces from other peers
'''

import socket
import struct
import time
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
    def __init__(self, ip, port, info_hash, peer_id):
        self.ip = ip
        self.port = port
        self.info_hash = info_hash
        self.peer_id = peer_id
        self.socket = None
        self.connected = False

        self.am_choking = True
        self.am_interested = False
        self.peer_choking = True
        self.peer_interested = False
        self.peer_pieces = set()  #which peer has piece

    def connect(self, timeout=5): #TCP peer connection
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(timeout)
            self.socket.connect((self.ip, self.port))
            self.connected = True
            return True
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            self.connected = False
            return False

    def handshake(self):
        if not self.connected:
            raise Exception("Not connected to peer")

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

        self.socket.send(handshake_msg)

        response = self._recv_exactly(68)

        if len(response) < 68:
            raise Exception("Invalid handshake response length")

        recv_pstrlen = response[0]
        recv_pstr = response[1:20]
        recv_reserved = response[20:28]
        recv_info_hash = response[28:48]
        recv_peer_id = response[48:68]

        #validation checks from malicious peers
        if recv_pstr != pstr:
            raise Exception(f"Invalid protocol: {recv_pstr}")

        if recv_info_hash != self.info_hash:
            raise Exception("Info hash mismatch")

        return True

    def _recv_exactly(self, n): #receive exactly n bytes from TCP-socket
        data = b'' #byte buffer
        while len(data) < n:
            try:
                chunk = self.socket.recv(n - len(data))
                if not chunk:
                    raise Exception("Connection closed by peer")
                data += chunk
            except socket.timeout:
                raise Exception("timed out")
        return data

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

    def _send_message(self, message_type, payload=b''):
        length = 1 + len(payload)
        message = struct.pack(">I", length) + struct.pack("B", message_type) + payload
        self.socket.send(message)

    def receive_message(self, timeout=5):
        self.socket.settimeout(timeout)

        try:
            length_data = self._recv_exactly(4)
            length = struct.unpack(">I", length_data)[0]

            if length == 0:
                return (None, b'')

            message_id = self._recv_exactly(1)[0] #takes 1st byte

            payload = b''
            if length > 1:
                payload = self._recv_exactly(length - 1) #without id

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

            return (message_id, payload)

        except socket.timeout:
            return None

    def _handle_bitfield(self, bitfield): #bitfield, what pieces peer has
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
                self.socket.close()
            except:
                pass
        self.connected = False

    def __repr__(self): #sttroka
        status = "connected" if self.connected else "disconnected"
        return f"Peer({self.ip}:{self.port}, {status})"
