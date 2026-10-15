"""
torrent file parser and info_hash calculator
Calculates SHA1 hash of the 'info' dictionary
Gives tracker url
"""

import hashlib
from pathlib import Path
from .bencode import BencodeDecoder
from .paths import validate_component


class TorrentFile:
    def __init__(self, filepath):
        self.filepath = Path(filepath)
        self._parse()

    @classmethod
    def from_bytes(cls, content):
        torrent = cls.__new__(cls)
        torrent.filepath = None
        torrent._load(content)
        return torrent

    def _parse(self):
        if not self.filepath.exists():
            raise FileNotFoundError(f"Torrent file not found: {self.filepath}")

        with open(self.filepath, 'rb') as f:
            content = f.read(BencodeDecoder.MAX_SIZE + 1)
        self._load(content)

    def _load(self, content):
        decoder = BencodeDecoder(content)
        self.data = decoder.decode()
        if not isinstance(self.data, dict) or not isinstance(self.data.get('info'), dict):
            raise ValueError("Invalid torrent info dictionary")
        self._validate()
        start, end = decoder.info_span
        self.info_hash = hashlib.sha1(content[start:end]).digest()

    def _validate(self):
        info = self.data['info']
        validate_component(info.get('name'))
        if not isinstance(self.data.get('announce'), bytes):
            raise ValueError("Missing tracker URL")
        if not self.announce:
            raise ValueError("Missing tracker URL")
        length = info.get('piece length')
        if type(length) is not int or not 0 < length <= 64 * 1024 * 1024:
            raise ValueError("Invalid piece length")
        if ('length' in info) == ('files' in info):
            raise ValueError("Torrent needs either length or files")
        if self.is_multi_file:
            files = info['files']
            if not isinstance(files, list) or not files:
                raise ValueError("Invalid file list")
            paths = set()
            for file_info in files:
                if not isinstance(file_info, dict):
                    raise ValueError("Invalid file entry")
                self._validate_length(file_info.get('length'))
                parts = file_info.get('path')
                if not isinstance(parts, list) or not parts:
                    raise ValueError("Invalid file path")
                parts = tuple(validate_component(part).casefold() for part in parts)
                if parts in paths:
                    raise ValueError("Duplicate file path")
                paths.add(parts)
            ordered = sorted(paths)
            if any(right[:len(left)] == left for left, right in zip(ordered, ordered[1:])):
                raise ValueError("Conflicting file paths")
        else:
            self._validate_length(info['length'])
        hashes = info.get('pieces')
        expected = (self.total_size + length - 1) // length
        if not isinstance(hashes, bytes) or len(hashes) != expected * 20:
            raise ValueError("Piece hashes do not match torrent size")

    @staticmethod
    def _validate_length(length):
        if type(length) is not int or length < 0:
            raise ValueError("Invalid file length")

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
