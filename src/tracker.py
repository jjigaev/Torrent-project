"""
handles HTTP/HTTPS tracker requests
and peer list parsing
"""
import os
import socket
import struct
import requests
from urllib.parse import urlencode, quote


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

        if not tracker_url.startswith(('http://', 'https://')): #check if tracker is supported
            raise ValueError(f"Unsupported tracker protocol: {tracker_url}")

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
        query_string = self._build_query_string(params) #encode params for simplicit
        full_url = f"{tracker_url}?{query_string}"

        print(f"Connecting to tracker: {tracker_url}")
        print(f"Info hash: {self.torrent.info_hash.hex()}")
        print(f"Peer ID: {self.peer_id.hex()}")

        try:
            response = requests.get(full_url, timeout=15)
            response.raise_for_status()

            from .bencode import BencodeDecoder
            tracker_response = BencodeDecoder(response.content).decode()

            if b'failure reason' in tracker_response or 'failure reason' in tracker_response:
                reason = tracker_response.get(b'failure reason') or tracker_response.get('failure reason')
                if isinstance(reason, bytes):
                    reason = reason.decode('utf-8')
                raise Exception(f"Tracker error: {reason}")

            return tracker_response

        except requests.RequestException as e:
            raise Exception(f"Failed connection to tracker: {e}")

    def _build_query_string(self, params): #query
        parts = []
        for key, value in params.items():
            if isinstance(value, bytes): #if value is bytes then:
                encoded_value = quote(value, safe='') #coded to url
            else:
                encoded_value = str(value)
            parts.append(f"{key}={encoded_value}")
        return '&'.join(parts)

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
                ip = peer.get(b'ip') or peer.get('ip')
                port = peer.get(b'port') or peer.get('port')

                if isinstance(ip, bytes):
                    ip = ip.decode('utf-8')

                peers.append((ip, port))
            return peers

        if isinstance(peers_data, bytes):
            peer_size = 6  #4 IP, 2 port
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
