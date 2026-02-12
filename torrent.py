"""
torrent file parser and info_hash calculator
Calculates SHA1 hash of the 'info' dictionary
Gives tracker url
"""

import hashlib
from pathlib import Path
from .bencode import BencodeDecoder, BencodeEncoder


class TorrentFile:
    def __init__(self, filepath):
        self.filepath = Path(filepath)
        self.data = None
        self.info_hash = None #SHA1 from bencoded info
        self._parse()

    def _parse(self):
        if not self.filepath.exists():
            raise FileNotFoundError(f"Torrent file not found: {self.filepath}")

        with open(self.filepath, 'rb') as f:
            content = f.read()

        decoder = BencodeDecoder(content)
        self.data = decoder.decode()

        self._calculate_info_hash() #calcucate (SHA1 of bencoded 'info' dict)

    def _calculate_info_hash(self):
        if 'info' not in self.data:
            raise ValueError("Invalid torrent file: missing 'info' key")
        info_bencoded = BencodeEncoder.encode(self.data['info'])
        self.info_hash = hashlib.sha1(info_bencoded).digest()

    @property
    def announce(self):
        url = self.data.get('announce', b'')
        return url.decode('utf-8') if isinstance(url, bytes) else url

    @property
    def name(self): #file name
        name = self.data['info'].get('name', b'unknown')
        return name.decode('utf-8') if isinstance(name, bytes) else name

    @property
    def piece_length(self): #length of each piece in bytes
        return self.data['info']['piece length']

    @property
    def pieces(self): #SHA1 hash of every piece
        pieces_data = self.data['info']['pieces']
        return [pieces_data[i:i+20] for i in range(0, len(pieces_data), 20)]

    @property
    def total_size(self):
        info = self.data['info']

        if 'length' in info:
            return info['length']

        elif 'files' in info:
            return sum(f['length'] for f in info['files'])
        else:
            raise ValueError("Invalid torrent: no length or files")

    @property
    def is_multi_file(self): #check
        return 'files' in self.data['info']

    def __repr__(self):
        return (f"TorrentFile(name='{self.name}', "
                f"size={self.total_size}, "
                f"pieces={len(self.pieces)})")