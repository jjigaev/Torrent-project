"""
handles HTTP/HTTPS tracker requests
and peer list parsing
"""
import os
import socket
import struct
import requests
from urllib.parse import parse_qsl, urlencode, quote, urlsplit, urlunsplit

from .bencode import BencodeDecoder


class TrackerClient:
    def __init__(self, torrent):
        self.torrent = torrent
        self.peer_id = self._generate_peer_id()
        self.port = 6881
        self.uploaded = 0
        self.downloaded = 0
        self.left = torrent.total_size

    @staticmethod
    def _generate_peer_id():
        return b'-MT0001-' + os.urandom(12)

    def announce(self, event='started'):
        """
        send announce request to tracker
        Args:
            event: 'started', 'completed', or 'stopped'
        Returns:
            dict: tracker response with peers list
        """
        tracker_url = self.torrent.announce

        parts = urlsplit(tracker_url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            raise ValueError("Unsupported tracker URL")
        if any(ord(char) < 32 for char in tracker_url):
            raise ValueError("Invalid tracker URL")

        params = {
            'info_hash': self.torrent.info_hash,
            'peer_id': self.peer_id,
            'port': self.port,
            'uploaded': self.uploaded,
            'downloaded': self.downloaded,
            'left': self.left,
            'compact': 1, #compacting peer list
            'event': event
        }
        existing = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                    if key not in params]
        query_string = urlencode(existing + list(params.items()), quote_via=quote, safe='')
        full_url = urlunsplit((parts.scheme, parts.netloc, parts.path, query_string, ''))

        try:
            with requests.get(full_url, timeout=(5, 15), stream=True) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > BencodeDecoder.MAX_SIZE:
                        raise ValueError("Tracker response exceeds size limit")
            tracker_response = BencodeDecoder(bytes(content)).decode()
            if not isinstance(tracker_response, dict):
                raise ValueError("Invalid tracker response")

            if b'failure reason' in tracker_response or 'failure reason' in tracker_response:
                reason = tracker_response.get(b'failure reason') or tracker_response.get('failure reason')
                if isinstance(reason, bytes):
                    reason = reason.decode('utf-8')
                raise ValueError("Tracker rejected the announce request")

            return tracker_response

        except requests.RequestException as e:
            raise ConnectionError("Failed connection to tracker") from e

    def _build_query_string(self, params): #query
        return urlencode(params, quote_via=quote, safe='')

    def get_peers(self):
        response = self.announce()
        peers_data = response.get(b'peers') or response.get('peers')

        if not peers_data:
            print("No peers returned from tracker")
            return []

        peers = self._parse_peers(peers_data)

        print(f"\nReceived {len(peers)} peers from tracker")
        return peers

    def _parse_peers(self, peers_data):
        peers = []

        if isinstance(peers_data, list): #dictionary with list of ip,port
            for peer in peers_data:
                if not isinstance(peer, dict):
                    raise ValueError("Invalid peer entry")
                ip = peer.get(b'ip') or peer.get('ip')
                port = peer.get(b'port') or peer.get('port')

                if isinstance(ip, bytes):
                    ip = ip.decode('utf-8')
                socket.inet_aton(ip)
                if type(port) is not int or not 0 < port < 65536:
                    raise ValueError("Invalid peer port")
                peers.append((ip, port))
            return peers

        if isinstance(peers_data, bytes):
            peer_size = 6  #4 IP, 2 port
            if len(peers_data) % peer_size:
                raise ValueError("Truncated peer list")
            for i in range(0, len(peers_data), peer_size):
                chunk = peers_data[i:i + peer_size]

                if len(chunk) < peer_size:
                    break

                ip = socket.inet_ntoa(chunk[:4])
                port = struct.unpack(">H", chunk[4:6])[0]
                peers.append((ip, port))
            return peers
        return peers

    def update_stats(self, uploaded=0, downloaded=0): #shows statistics
        self.uploaded += uploaded
        self.downloaded += downloaded
        self.left = self.torrent.total_size - self.downloaded
